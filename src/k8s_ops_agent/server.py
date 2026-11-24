"""通过 AG-UI 协议提供 Kubernetes 运维图的 FastAPI 应用。"""

from __future__ import annotations

import os
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from uuid import uuid4

from ag_ui.core.types import RunAgentInput
from ag_ui.core.events import ActivityDeltaEvent, ActivitySnapshotEvent, EventType
from ag_ui.encoder import EventEncoder
from ag_ui_langgraph import LangGraphAgent
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from k8s_ops_agent.approval_auth import (
    validate_approval_answers,
)
from k8s_ops_agent.approval_store import (
    configure_approval_store,
    get_approval_request,
    record_approval_resume,
)
from k8s_ops_agent.db_config import database_conninfo
from k8s_ops_agent.graph import build_graph
from k8s_ops_agent.middleware import AccessLogMiddleware
from k8s_ops_agent.observability import logger


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
            # LangGraph 底层回调已由适配器转换为消息、工具、步骤和状态等
            # AG-UI 原生事件；关闭 RAW 透传以避免重复事件和大体积内部状态。
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


async def _active_interrupts(graph: Any, thread_id: str) -> dict[str, object]:
    """读取 thread 当前 checkpoint 中尚未处理的中断。

    参数：
        graph: 当前 FastAPI 生命周期中构建的 LangGraph。
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
    """校验并持久化 AG-UI 审批恢复决定，返回服务端规范化后的运行输入。

    参数：
        input_data: AG-UI 运行请求。
        request: 当前 HTTP 请求，用于读取图状态和审批记录。
        返回值：经审批校验并填入服务端时间信息的 AG-UI 运行请求。
    """
    entries = input_data.resume or []
    if not entries:
        return input_data
    if len({entry.interrupt_id for entry in entries}) != len(entries):
        raise HTTPException(status_code=422, detail="Duplicate resume interrupt ID")

    interrupts = await _active_interrupts(
        request.app.state.graph, input_data.thread_id
    )
    if not interrupts:
        raise HTTPException(status_code=409, detail="Thread has no active interrupt")

    normalized_entries = []
    pending_decisions: list[tuple[str, dict[str, Any]]] = []
    for entry in entries:
        # 只接受当前 thread checkpoint 中存在的中断，避免跨 thread 或伪造 interrupt ID。
        raw_interrupt = interrupts.get(entry.interrupt_id)
        if not isinstance(raw_interrupt, dict):
            logger.warning(
                "Approval resume interrupt was not found in checkpoint",
                extra={
                    "log_type": "business",
                    "thread_id": input_data.thread_id,
                    "run_id": input_data.run_id,
                    "interrupt_id": entry.interrupt_id,
                    "active_interrupt_ids": list(interrupts),
                    "checkpoint_value_type": type(raw_interrupt).__name__,
                },
            )
            raise HTTPException(
                status_code=409, detail="Resume entry is not an active approval"
            )

        metadata = raw_interrupt.get("metadata", {})
        langgraph_metadata = metadata.get("langgraph", {}) if isinstance(metadata, dict) else {}
        request_payload = langgraph_metadata.get("raw", {}) if isinstance(langgraph_metadata, dict) else {}
        if not isinstance(request_payload, dict) or request_payload.get("type") != "approval_request":
            logger.warning(
                "Active interrupt does not match expected MTAP approval wrapper",
                extra={
                    "log_type": "business",
                    "thread_id": input_data.thread_id,
                    "run_id": input_data.run_id,
                    "interrupt_id": entry.interrupt_id,
                    "checkpoint_value_type": type(raw_interrupt).__name__,
                    "checkpoint_value_keys": list(raw_interrupt),
                    "metadata_type": type(metadata).__name__,
                    "metadata_keys": list(metadata) if isinstance(metadata, dict) else [],
                    "langgraph_metadata_type": type(langgraph_metadata).__name__,
                    "langgraph_metadata_keys": (
                        list(langgraph_metadata) if isinstance(langgraph_metadata, dict) else []
                    ),
                    "request_payload_type": type(request_payload).__name__,
                    "request_payload_keys": (
                        list(request_payload) if isinstance(request_payload, dict) else []
                    ),
                    "request_payload_type_value": (
                        request_payload.get("type") if isinstance(request_payload, dict) else None
                    ),
                    "checkpoint_request_id": (
                        request_payload.get("request_id")
                        if isinstance(request_payload, dict)
                        else None
                    ),
                },
            )
            raise HTTPException(status_code=409, detail="Active interrupt is not an MTAP approval")
        request_id = request_payload.get("request_id")
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
            if decision_status == "rejected" and answers:
                raise HTTPException(status_code=422, detail="Rejected decision must not include answers")
            if decision_status == "approved" and "answers" not in submitted:
                raise HTTPException(status_code=422, detail="Approved decision requires answers")
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

        request_payload = approval.get("request_payload") or {}
        if decision_status == "approved":
            validate_approval_answers(request_payload, answers)

        decision = {
            "request_id": request_id,
            "thread_id": input_data.thread_id,
            "status": decision_status,
            "answers": answers,
            "comment": comment,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        # 一次写入终态和 run 占用权，防止并发请求重复审批或恢复同一中断。
        pending_decisions.append((request_id, decision))

        if entry.status == "resolved":
            normalized_entries.append(entry.model_copy(update={"payload": decision}))
        else:
            normalized_entries.append(entry)
    for request_id, decision in pending_decisions:
        recorded = await record_approval_resume(
            request_id, input_data.thread_id, input_data.run_id, decision
        )
        if not recorded:
            raise HTTPException(
                status_code=409, detail="Approval was already decided or resumed"
            )
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
    request_agent = request.app.state.agent.clone()

    async def event_generator() -> AsyncGenerator[str, None]:
        """逐条编码并发送 LangGraph agent 事件。

        参数：无。
        返回值：编码后的 AG-UI 事件文本。
        """
        activity_id = f"{input_data.thread_id}:agent_run"
        activity_type = "agent_run"
        started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        content: dict[str, Any] = {
            "schemaVersion": 1,
            "run": {
                "runId": input_data.run_id,
                "threadId": input_data.thread_id,
                "status": "running",
                "startedAt": started_at,
                "updatedAt": started_at,
                "finishedAt": None,
                "error": None,
            },
            "events": [],
        }
        sequence = 0
        active_by_tool_call: dict[str, dict[str, Any]] = {}
        active_models: list[dict[str, Any]] = []
        active_model: dict[str, Any] | None = None
        active_model_text: list[str] = []
        active_reasoning: dict[str, dict[str, Any]] = {}
        final_message_parts: list[str] = []

        def timestamp_ms() -> int:
            """获取当前 Unix 毫秒时间戳。

            参数：无。
            返回值：当前时间对应的毫秒时间戳。
            """
            return time.time_ns() // 1_000_000

        def iso_now() -> str:
            """获取当前 UTC ISO-8601 时间。

            参数：无。
            返回值：毫秒精度并以 Z 结尾的 UTC 时间。
            """
            return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

        def snapshot() -> ActivitySnapshotEvent:
            """创建当前 thread 时间线的完整快照。

            参数：无。
            返回值：符合 AG-UI 协议的活动快照事件。
            """
            return ActivitySnapshotEvent(
                timestamp=timestamp_ms(),
                message_id=activity_id,
                activity_type=activity_type,
                content=content,
            )

        def patch(operations: list[dict[str, Any]]) -> ActivityDeltaEvent:
            """创建指向稳定活动消息的 RFC 6902 增量事件。

            参数：
                operations: 已按顺序更新本地 content 的 JSON Patch 操作。
            返回值：符合 AG-UI 协议的活动增量事件。
            """
            return ActivityDeltaEvent(
                timestamp=timestamp_ms(),
                message_id=activity_id,
                activity_type=activity_type,
                patch=operations,
            )

        def add_activity(kind: str, title: str, extra: dict[str, Any] | None = None) -> tuple[dict[str, Any], ActivityDeltaEvent]:
            """按序追加一个活动条目并生成对应 patch。

            参数：kind 为活动类型；title 为展示标题；extra 为条目附加字段。
            返回值：新条目及追加操作对应的 AG-UI 增量事件。
            """
            nonlocal sequence
            sequence += 1
            now = iso_now()
            item = {
                "id": f"{kind}-{uuid4().hex}", "sequence": sequence, "type": kind,
                "title": title, "status": "running", "startedAt": now,
                "updatedAt": now, "finishedAt": None, "input": {},
                "output": None, "error": None,
            }
            if extra:
                item.update(extra)
            content["events"].append(item)
            content["run"]["updatedAt"] = now
            return item, patch([
                {"op": "add", "path": "/events/-", "value": item},
                {"op": "replace", "path": "/run/updatedAt", "value": now},
            ])

        def update_activity(item: dict[str, Any], values: dict[str, Any]) -> ActivityDeltaEvent:
            """更新活动条目字段并生成 RFC 6902 patch。

            参数：item 为已存在条目；values 为要更新的字段和值。
            返回值：对应更新操作的 AG-UI 增量事件。
            """
            now = iso_now()
            operations: list[dict[str, Any]] = []
            for key, value in {**values, "updatedAt": now}.items():
                operation = "replace" if key in item else "add"
                item[key] = value
                operations.append({"op": operation, "path": f"/events/{item['sequence'] - 1}/{key}", "value": value})
            content["run"]["updatedAt"] = now
            operations.append({"op": "replace", "path": "/run/updatedAt", "value": now})
            return patch(operations)

        snapshot_sent = False
        async for event in request_agent.run(normalized_input):
            event_type = getattr(event, "type", None)
            # 保存最后一条 assistant 消息文本，供 RUN_FINISHED.result 返回。
            if event_type == EventType.TEXT_MESSAGE_START and getattr(event, "role", None) == "assistant":
                message_id = event.message_id
                final_message_parts = []
                active_model_text = []
                active_model, delta = add_activity("model_inference", "推理", {
                    "correlation": {"messageId": message_id},
                    "output": {"messageId": message_id, "text": ""},
                })
                active_models.append(active_model)
                yield encoder.encode(delta)
            elif event_type == EventType.REASONING_MESSAGE_START:
                reasoning_id = getattr(event, "message_id", None)
                if reasoning_id:
                    item, delta = add_activity("reasoning", "Thinking", {
                        "correlation": {"reasoningId": reasoning_id},
                    })
                    active_reasoning[reasoning_id] = item
                    yield encoder.encode(delta)
            elif event_type == EventType.REASONING_MESSAGE_END:
                reasoning_id = getattr(event, "message_id", None)
                item = active_reasoning.pop(reasoning_id or "", None)
                if item:
                    yield encoder.encode(update_activity(item, {
                        "status": "completed", "finishedAt": iso_now(),
                    }))
            elif event_type == EventType.REASONING_MESSAGE_CHUNK:
                # chunk 可能是单条完整片段；只记录生命周期，不读取 delta 正文。
                reasoning_id = getattr(event, "message_id", None)
                if reasoning_id:
                    item = active_reasoning.get(reasoning_id)
                    if item is None:
                        item, delta = add_activity("reasoning", "Thinking", {
                            "correlation": {"reasoningId": reasoning_id},
                        })
                        active_reasoning[reasoning_id] = item
                        yield encoder.encode(delta)
                    yield encoder.encode(update_activity(item, {
                        "status": "completed", "finishedAt": iso_now(),
                    }))
            elif event_type == EventType.TEXT_MESSAGE_CONTENT and active_model:
                text_delta = str(getattr(event, "delta", ""))
                final_message_parts.append(text_delta)
                active_model_text.append(text_delta)
            elif event_type == EventType.TEXT_MESSAGE_END and active_model:
                full_text = "".join(active_model_text)
                message_id = active_model["correlation"]["messageId"]
                yield encoder.encode(update_activity(active_model, {
                    "status": "completed", "finishedAt": iso_now(),
                    "output": {"messageId": message_id, "text": full_text},
                }))
                active_models.remove(active_model)
                active_model = None
                active_model_text = []
            if event_type == EventType.RUN_STARTED:
                yield encoder.encode(event)
                if not snapshot_sent:
                    yield encoder.encode(snapshot())
                    snapshot_sent = True
                continue
            if event_type in (EventType.RUN_FINISHED, EventType.RUN_ERROR) and not snapshot_sent:
                # 底层预生成事件分支可能在 RUN_STARTED 后直接结束；终态之前补发快照。
                yield encoder.encode(snapshot())
                snapshot_sent = True
            if event_type == EventType.TOOL_CALL_START:
                item, delta = add_activity("tool_execution", event.tool_call_name, {
                    "tool": {"name": event.tool_call_name},
                    "correlation": {"toolCallId": event.tool_call_id},
                    "input": {},
                })
                active_by_tool_call[event.tool_call_id] = item
                yield encoder.encode(delta)
            elif event_type == EventType.TOOL_CALL_RESULT:
                item = active_by_tool_call.pop(event.tool_call_id, None)
                if item:
                    result = getattr(event, "result", None)
                    try:
                        normalized = json.loads(json.dumps(result, ensure_ascii=False, default=str))
                    except (TypeError, ValueError):
                        normalized = {"summary": str(result)[:2000]}
                    yield encoder.encode(update_activity(item, {
                        "status": "completed", "finishedAt": iso_now(), "output": normalized,
                    }))
            elif event_type in (EventType.RUN_FINISHED, EventType.RUN_ERROR):
                outcome = getattr(event, "outcome", None)
                interrupted = getattr(outcome, "type", None) == "interrupt"
                final_status = "failed" if event_type == EventType.RUN_ERROR else "awaiting_approval" if interrupted else "completed"
                if event_type == EventType.RUN_FINISHED:
                    # interrupt 代表运行暂停，不应把此前模型文本标成最终成功结论。
                    if not interrupted and final_message_parts:
                        event.result = "".join(final_message_parts)
                    event.timestamp = event.timestamp or timestamp_ms()
                for item in [*active_models, *active_by_tool_call.values(), *active_reasoning.values()]:
                    if item["status"] == "running":
                        terminal = "failed" if final_status == "failed" else "cancelled" if final_status == "completed" else "awaiting_approval"
                        yield encoder.encode(update_activity(item, {"status": terminal, "finishedAt": iso_now() if terminal != "awaiting_approval" else None}))
                now = iso_now()
                content["run"].update(status=final_status, updatedAt=now, finishedAt=now, error=(str(getattr(event, "message", ""))[:1000] or None) if final_status == "failed" else None)
                yield encoder.encode(patch([
                    {"op": "replace", "path": "/run/status", "value": final_status},
                    {"op": "replace", "path": "/run/updatedAt", "value": now},
                    {"op": "replace", "path": "/run/finishedAt", "value": now},
                    {"op": "replace", "path": "/run/error", "value": content["run"]["error"]},
                ]))
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
    return {"status": "ok", "agent": {"name": "k8s_ops_agent"}}


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    """返回服务健康状态。

    参数：
        request: 当前 HTTP 请求，用于读取应用级 agent 实例。
    返回值：服务状态和 agent 标识。
    """
    logger.info("Health check completed", extra={"log_type": "business"})
    return {"status": "ok", "agent": request.app.state.agent.name}
