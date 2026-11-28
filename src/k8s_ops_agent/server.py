"""通过 AG-UI 协议提供 Kubernetes 运维图的 FastAPI 应用。"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from ag_ui.core.types import RunAgentInput
from ag_ui.encoder import EventEncoder
from ag_ui_langgraph import LangGraphAgent
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from k8s_ops_agent.activity.events import event_generator
from k8s_ops_agent.approval_store import configure_approval_store
from k8s_ops_agent.db_config import database_conninfo
from k8s_ops_agent.graph import build_graph
from k8s_ops_agent.middleware import AccessLogMiddleware
from k8s_ops_agent.observability import logger
from k8s_ops_agent.resume_validation import validate_resume


_uvicorn_access_logger = logging.getLogger("uvicorn.access")


def _filter_health_access_log(record: logging.LogRecord) -> bool:
    """将成功的健康检查访问日志调整为 DEBUG。

    参数：
        record: Uvicorn 生成的访问日志记录。
    返回值：当前日志级别是否允许输出该记录。
    """
    args = record.args
    if record.levelno != logging.INFO or not isinstance(args, tuple) or len(args) != 5:
        return True

    _, method, target, _, status = args
    if method != "GET" or target.partition("?")[0] != "/health" or status != 200:
        return True

    # Logger 已按原始 INFO 级别放行，降级后需要重新检查生效级别。
    record.levelno = logging.DEBUG
    record.levelname = "DEBUG"
    return _uvicorn_access_logger.isEnabledFor(logging.DEBUG)


_uvicorn_access_logger.addFilter(_filter_health_access_log)


# 进程启动时读取一次连接配置；不记录凭据，且 database_conninfo() 会拒绝不完整配置。
DATABASE_CONNINFO = database_conninfo()
APP_ENV = os.getenv("APP_ENV", "development").lower()

if APP_ENV == "production" and not DATABASE_CONNINFO:
    raise RuntimeError(
        "DB_HOST, DB_PORT, DB_NAME, DB_USER, and DB_PASSWORD are required "
        "when APP_ENV=production"
    )

_pool: AsyncConnectionPool | None = None

if DATABASE_CONNINFO:
    _pool = AsyncConnectionPool(
        conninfo=DATABASE_CONNINFO,
        min_size=1,
        max_size=int(os.getenv("POSTGRES_POOL_MAX_SIZE", "10")),
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )

configure_approval_store(_pool)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """在应用事件循环启动后创建 checkpoint、图和 AG-UI agent。

    参数：
        app: 当前 FastAPI 应用实例。
    返回值：应用运行期间的异步生命周期上下文。
    """
    pool_open = False
    try:
        if _pool is not None:
            await _pool.open()
            pool_open = True
            # Postgres saver 在当前运行事件循环内创建，避免导入模块时没有 event loop。
            checkpointer: AsyncPostgresSaver | MemorySaver = AsyncPostgresSaver(_pool)
            # 业务表结构由容器入口 entrypoint.sh 在启动时执行 scripts/sync_schema.py 同步；
            # 这里由 LangGraph 使用连接池创建并管理 checkpoint 表。
            await checkpointer.setup()
        else:
            # 仅供本地开发使用；生产环境启动时必须配置数据库。
            checkpointer = MemorySaver()

        graph = build_graph(checkpointer=checkpointer)
        app.state.graph = graph
        app.state.agent = LangGraphAgent(
            name="k8s_ops_agent",
            graph=graph,
            description="Kubernetes incident investigation agent",
            config={"recursion_limit": 100},
            # 前端按 AG-UI 标准从 RUN_FINISHED.outcome.interrupts 渲染审批框，
            # 并通过 RunAgentInput.resume 恢复执行；旧版 on_interrupt 自定义事件
            # 不包含标准 ResumeEntry 所需的完整中断信息。
            enable_legacy_on_interrupt_event=False,
            emit_interrupt_outcome=True,
            # A2A 进度通过适配器的 RAW 回调入口进入下方的活动事件转换。
            emit_raw_events=True,
        )
        yield
    finally:
        if _pool is not None and pool_open:
            await _pool.close()

app = FastAPI(
    title="Kubernetes Ops AG-UI Agent",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(AccessLogMiddleware)
cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:3000,http://localhost:5173"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.post("/agent", name="langgraph_agent_endpoint")
async def langgraph_agent_endpoint(
    input_data: RunAgentInput, request: Request
) -> StreamingResponse:
    """验证审批恢复请求后，以 AG-UI SSE 协议运行 LangGraph。

    参数：
        input_data: AG-UI 运行输入，包含 thread、run、消息及可选恢复决定。
        request: 当前 HTTP 请求，用于读取 SSE Accept 头。
    返回值：AG-UI 编码的服务端事件流。
    """
    normalized_input = await validate_resume(input_data, request)
    encoder = EventEncoder(accept=request.headers.get("accept"))
    request_agent = request.app.state.agent.clone()

    return StreamingResponse(
        event_generator(input_data, normalized_input, request_agent, encoder),
        media_type=encoder.get_content_type(),
    )


@app.get("/agent/health")
def agent_health() -> dict[str, object]:
    """返回 AG-UI agent 路由的健康状态。

    参数：无。
    返回值：服务状态和 agent 标识。
    """
    return {"status": "ok", "agent": {"name": "k8s_ops_agent"}}


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    """返回服务健康状态。

    参数：
        request: 当前 HTTP 请求，用于读取应用级 agent 实例。
    返回值：服务状态和 agent 标识。
    """
    logger.debug("Health check completed", extra={"log_type": "business"})
    return {"status": "ok", "agent": request.app.state.agent.name}
