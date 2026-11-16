"""通过 AG-UI 协议提供 Kubernetes 运维图的 FastAPI 应用。"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from ag_ui.core.types import RunAgentInput
from ag_ui.encoder import EventEncoder
from ag_ui_langgraph import LangGraphAgent
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from k8s_ops_agent.approval_auth import (
    authenticated_reviewer,
    authorize_approval_role,
    validate_approval_answers,
)
from k8s_ops_agent.approval_store import (
    configure_approval_store,
    get_approval_request,
    record_approval_resume,
)
from k8s_ops_agent.graph import build_graph
from k8s_ops_agent.db_config import database_conninfo
from k8s_ops_agent.middleware import AccessLogMiddleware
from k8s_ops_agent.observability import logger
from k8s_ops_agent.schema_sync import sync_schema


# 进程启动时读取一次连接配置；不记录凭据，且 database_conninfo() 会拒绝不完整配置。
DATABASE_CONNINFO = database_conninfo()
APP_ENV = os.getenv("APP_ENV", "development").lower()

if APP_ENV == "production" and not DATABASE_CONNINFO:
    raise RuntimeError(
        "DB_HOST, DB_PORT, DB_NAME, DB_USER, and DB_PASSWORD are required "
        "when APP_ENV=production"
    )

_pool: AsyncConnectionPool | None = None
_checkpointer: AsyncPostgresSaver | MemorySaver

if DATABASE_CONNINFO:
    _pool = AsyncConnectionPool(
        conninfo=DATABASE_CONNINFO,
        min_size=1,
        max_size=int(os.getenv("POSTGRES_POOL_MAX_SIZE", "10")),
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )
    _checkpointer = AsyncPostgresSaver(_pool)
else:
    # 仅供本地开发使用；生产环境启动时必须配置数据库。
    _checkpointer = MemorySaver()

configure_approval_store(_pool)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    pool_open = False
    try:
        if _pool is not None:
            await _pool.open()
            pool_open = True
            # 先创建应用业务表，再由 LangGraph 使用同一连接池创建并管理 checkpoint 表。
            await asyncio.to_thread(sync_schema, DATABASE_CONNINFO)
            await _checkpointer.setup()
        yield
    finally:
        if _pool is not None and pool_open:
            await _pool.close()


graph = build_graph(checkpointer=_checkpointer)
agent = LangGraphAgent(
    name="k8s_ops_agent",
    graph=graph,
    description="Kubernetes incident investigation agent",
)

app = FastAPI(
    title="Kubernetes Ops AG-UI Agent",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.graph = graph
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


async def _active_interrupts(thread_id: str) -> dict[str, object]:
    """读取 thread 当前 checkpoint 中尚未处理的中断。

    参数：
        thread_id: LangGraph 对话标识。
    返回值：以 interrupt ID 为键、原始中断值为值的字典。
    """
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    active: dict[str, object] = {}
    for task in snapshot.tasks:
        for interrupt in task.interrupts:
            active[interrupt.id] = interrupt.value
    return active


async def _validate_resume(
    input_data: RunAgentInput, request: Request
) -> RunAgentInput:
    """校验并持久化 AG-UI 恢复决定，返回服务端规范化后的运行输入。

    参数：
        input_data: AG-UI 运行请求。
        request: 当前 HTTP 请求，用于读取可信审核人身份和角色。
    返回值：经审批校验并填入服务端决定信息的 AG-UI 运行请求。
    """
    entries = input_data.resume or []
    if not entries:
        return input_data
    if len(entries) != 1:
        raise HTTPException(
            status_code=422,
            detail="This agent accepts one approval resume entry per run",
        )

    interrupts = await _active_interrupts(input_data.thread_id)
    if not interrupts:
        raise HTTPException(status_code=409, detail="Thread has no active interrupt")

    normalized_entries = []
    for entry in entries:
        # 只接受当前 thread checkpoint 中存在的中断，避免跨 thread 或伪造 interrupt ID。
        raw_interrupt = interrupts.get(entry.interrupt_id)
        if not isinstance(raw_interrupt, dict):
            raise HTTPException(
                status_code=409, detail="Resume entry is not an active approval"
            )

        request_id = raw_interrupt.get("request_id")
        if not isinstance(request_id, str):
            raise HTTPException(
                status_code=409, detail="Active interrupt has no approval request"
            )

        if entry.status == "cancelled":
            # AG-UI 取消不携带业务 payload；request_id 必须从可信 checkpoint 读取。
            if entry.payload is not None:
                raise HTTPException(
                    status_code=422,
                    detail="Cancelled ResumeEntry must not include a payload",
                )
            decision_status = "cancelled"
            answers: dict[str, object] = {}
            comment = None
        else:
            # resolved payload 只允许业务决定字段，审核人和决定时间由服务端补充。
            submitted = entry.payload
            if not isinstance(submitted, dict):
                raise HTTPException(
                    status_code=422, detail="Resume payload must be an object"
                )
            if set(submitted) - {"request_id", "status", "answers", "comment"}:
                raise HTTPException(
                    status_code=422, detail="Resume payload has unsupported fields"
                )
            if submitted.get("request_id") != request_id:
                raise HTTPException(
                    status_code=409,
                    detail="Resume entry does not match the active interrupt",
                )
            decision_status = submitted.get("status")
            if decision_status not in {"approved", "rejected"}:
                raise HTTPException(
                    status_code=422, detail="Invalid approval decision status"
                )
            answers = submitted.get("answers", {})
            if not isinstance(answers, dict):
                raise HTTPException(
                    status_code=422, detail="Approval answers must be an object"
                )
            comment = submitted.get("comment")
            if comment is not None and (
                not isinstance(comment, str) or len(comment) > 4000
            ):
                raise HTTPException(
                    status_code=422, detail="Approval comment is invalid"
                )

        approval = await get_approval_request(request_id)
        if (
            approval is None
            or approval.get("thread_id") != input_data.thread_id
            or approval.get("status") != "pending"
        ):
            raise HTTPException(
                status_code=409,
                detail="Approval request is missing or no longer pending",
            )

        reviewer = authenticated_reviewer(request)
        request_payload = approval.get("request_payload") or {}
        if request_payload.get("mcp_server"):
            authorize_approval_role(request, request_payload.get("approval_role"))
        if decision_status != "cancelled":
            validate_approval_answers(request_payload, answers)

        decision = {
            "request_id": request_id,
            "thread_id": input_data.thread_id,
            "status": decision_status,
            "answers": answers,
            "comment": comment,
            "reviewer": reviewer,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        # 一次写入终态和 run 占用权，防止并发请求重复审批或恢复同一中断。
        recorded = await record_approval_resume(
            request_id,
            input_data.thread_id,
            input_data.run_id,
            decision,
        )
        if not recorded:
            raise HTTPException(
                status_code=409, detail="Approval was already decided or resumed"
            )

        if entry.status == "resolved":
            normalized_entries.append(entry.model_copy(update={"payload": decision}))
        else:
            normalized_entries.append(entry)
    return input_data.model_copy(update={"resume": normalized_entries})


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
    normalized_input = await _validate_resume(input_data, request)
    encoder = EventEncoder(accept=request.headers.get("accept"))
    request_agent = agent.clone()

    async def event_generator() -> AsyncGenerator[str, None]:
        """逐条编码并发送 LangGraph agent 事件。

        参数：无。
        返回值：编码后的 AG-UI 事件文本。
        """
        async for event in request_agent.run(normalized_input):
            yield encoder.encode(event)

    return StreamingResponse(
        event_generator(),
        media_type=encoder.get_content_type(),
    )


@app.get("/agent/health")
def agent_health() -> dict[str, object]:
    """返回 AG-UI agent 路由的健康状态。

    参数：无。
    返回值：服务状态和 agent 标识。
    """
    return {"status": "ok", "agent": {"name": agent.name}}


@app.get("/health")
async def health() -> dict[str, str]:
    logger.info("Health check completed", extra={"log_type": "business"})
    return {"status": "ok", "agent": agent.name}
