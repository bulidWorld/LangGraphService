"""校验并持久化 AG-UI 审批恢复决定。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ag_ui.core.types import ResumeEntry, RunAgentInput
from fastapi import HTTPException, Request

from k8s_ops_agent.approval_auth import validate_approval_answers
from k8s_ops_agent.approval_store import get_approval_request, record_approval_resume
from k8s_ops_agent.observability import logger


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


def _approval_request_id(
    interrupts: dict[str, object], interrupt_id: str, input_data: RunAgentInput
) -> str:
    """从活动中断中读取审批请求 ID，兼容直接保存和 AG-UI 包装两种格式。

    参数：
        interrupts: 当前 thread 的活动中断及原始中断值。
        interrupt_id: 客户端提交的中断标识。
        input_data: 提供日志所需的 thread 和 run 标识。
    返回值：checkpoint 中的审批请求 ID。
    """
    # 只接受当前 thread checkpoint 中存在的中断，避免跨 thread 或伪造 interrupt ID。
    raw_interrupt = interrupts.get(interrupt_id)
    if not isinstance(raw_interrupt, dict):
        logger.warning(
            "Approval resume interrupt was not found in checkpoint",
            extra={
                "log_type": "business",
                "thread_id": input_data.thread_id,
                "run_id": input_data.run_id,
                "interrupt_id": interrupt_id,
                "active_interrupt_ids": list(interrupts),
                "checkpoint_value_type": type(raw_interrupt).__name__,
            },
        )
        raise HTTPException(
            status_code=409, detail="Resume entry is not an active approval"
        )

    # LangGraph checkpoint 的 interrupt.value 通常直接保存 interrupt(request_payload) 的值；
    # 部分 AG-UI 表示则会将该值包装在 metadata.langgraph.raw 中。两种形式都支持。
    if raw_interrupt.get("type") == "approval_request":
        request_payload = raw_interrupt
        metadata: object = {}
        langgraph_metadata: object = {}
    else:
        metadata = raw_interrupt.get("metadata", {})
        langgraph_metadata = (
            metadata.get("langgraph", {}) if isinstance(metadata, dict) else {}
        )
        request_payload = (
            langgraph_metadata.get("raw", {})
            if isinstance(langgraph_metadata, dict)
            else {}
        )
    if (
        not isinstance(request_payload, dict)
        or request_payload.get("type") != "approval_request"
    ):
        logger.warning(
            "Active interrupt does not match expected MTAP approval wrapper",
            extra={
                "log_type": "business",
                "thread_id": input_data.thread_id,
                "run_id": input_data.run_id,
                "interrupt_id": interrupt_id,
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

    return request_id


def _parse_resolved_decision(
    entry: ResumeEntry, request_id: str
) -> tuple[str, dict[str, object], str | None]:
    """校验 resolved 条目中的决定字段。

    参数：
        entry: 客户端提交的恢复条目。
        request_id: 从 checkpoint 读取的审批请求 ID。
    返回值：决定状态、审批答案和备注。
    """
    # resolved payload 只允许业务决定字段，对话标识和决定时间由服务端补充。
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
        raise HTTPException(
            status_code=422, detail="Rejected decision must not include answers"
        )
    if decision_status == "approved" and "answers" not in submitted:
        raise HTTPException(status_code=422, detail="Approved decision requires answers")
    comment = submitted.get("comment")
    if comment is not None and (
        not isinstance(comment, str) or len(comment) > 4000
    ):
        raise HTTPException(
            status_code=422, detail="Approval comment is invalid"
        )

    return decision_status, answers, comment


async def _prepare_decision(
    entry: ResumeEntry, request_id: str, thread_id: str
) -> dict[str, Any]:
    """校验审批状态及答案，并生成带服务端时间的决定。

    参数：
        entry: 客户端提交的恢复条目。
        request_id: 从 checkpoint 读取的审批请求 ID。
        thread_id: 本次运行所属的对话标识。
    返回值：可持久化的完整审批决定。
    """
    if entry.status == "cancelled":
        # 取消条目的审批 ID 由 checkpoint 提供。
        if entry.payload is not None:
            raise HTTPException(
                status_code=422,
                detail="Cancelled ResumeEntry must not include a payload",
            )
        decision_status, answers, comment = "cancelled", {}, None
    else:
        decision_status, answers, comment = _parse_resolved_decision(entry, request_id)

    approval = await get_approval_request(request_id)
    if (
        approval is None
        or approval.get("thread_id") != thread_id
        or approval.get("status") != "pending"
    ):
        raise HTTPException(
            status_code=409,
            detail="Approval request is missing or no longer pending",
        )

    request_payload = approval.get("request_payload") or {}
    if decision_status == "approved":
        validate_approval_answers(request_payload, answers)

    return {
        "request_id": request_id,
        "thread_id": thread_id,
        "status": decision_status,
        "answers": answers,
        "comment": comment,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }


async def _record_decisions(
    input_data: RunAgentInput, decisions: list[dict[str, Any]]
) -> None:
    """依次保存审批决定并为当前 run 取得恢复权。

    参数：
        input_data: 提供持久化所需的 thread 和 run 标识。
        decisions: 全部通过校验的审批决定。
    返回值：无。
    """
    # 一次写入单条审批的终态和 run 占用权，防止并发重复恢复。
    for decision in decisions:
        recorded = await record_approval_resume(
            decision["request_id"], input_data.thread_id, input_data.run_id, decision
        )
        if not recorded:
            raise HTTPException(
                status_code=409, detail="Approval was already decided or resumed"
            )


async def validate_resume(
    input_data: RunAgentInput, request: Request
) -> RunAgentInput:
    """校验并持久化 AG-UI 审批恢复决定，返回服务端规范化后的运行输入。

    参数：
        input_data: AG-UI 运行请求。
        request: 当前 HTTP 请求，用于取得应用中的 LangGraph 实例。
        返回值：经审批校验并填入服务端时间信息的 AG-UI 运行请求。
    """
    entries = input_data.resume or []
    if not entries:
        # 适配器兼容旧式恢复命令，但该入口会跳过 MTAP 的决定校验与持久化。
        command = (input_data.forwarded_props or {}).get("command")
        if isinstance(command, dict) and command.get("resume") is not None:
            raise HTTPException(
                status_code=422, detail="Approval resume must use RunAgentInput.resume"
            )
        return input_data
    if len({entry.interrupt_id for entry in entries}) != len(entries):
        raise HTTPException(status_code=422, detail="Duplicate resume interrupt ID")

    interrupts = await _active_interrupts(
        request.app.state.graph, input_data.thread_id
    )
    if not interrupts:
        raise HTTPException(status_code=409, detail="Thread has no active interrupt")

    normalized_entries = []
    decisions = []
    for entry in entries:
        request_id = _approval_request_id(interrupts, entry.interrupt_id, input_data)
        decision = await _prepare_decision(entry, request_id, input_data.thread_id)
        decisions.append(decision)

        if entry.status == "resolved":
            entry = entry.model_copy(update={"payload": decision})
        normalized_entries.append(entry)

    # 所有条目通过校验后才开始写入。
    await _record_decisions(input_data, decisions)
    return input_data.model_copy(update={"resume": normalized_entries})
