"""按 AG-UI 事件类型构建活动时间线。"""

import json
from collections.abc import Iterator
from typing import Any, Protocol

from ag_ui.core.events import BaseEvent, EventType

from k8s_ops_agent import graph_node_constants as nodes
from k8s_ops_agent.a2a_client.graph import A2A_PROGRESS_EVENT
from k8s_ops_agent.approve.mcp_tool import tool_approval_request_id

from .state import ActivityState, iso_now, timestamp_ms


class EventHandler(Protocol):
    """单个 AG-UI 事件的处理策略。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """处理输入事件并按发送顺序产生输出事件。

        参数：event 为原始事件，state 为本次运行的活动状态。
        返回值：按发送顺序产生原始事件或活动事件的迭代器。
        """
        ...


def _a2a_error_summary(message: Any) -> str | None:
    """提取 A2A 状态消息中的安全错误摘要。

    参数：message 为 A2A TaskStatus.message 对象或其他消息值。
    返回值：最多 1000 字符的文本摘要；没有可用文本时返回 None。
    """
    if isinstance(message, str):
        return message[:1000] or None
    if not isinstance(message, dict):
        return None
    parts = message.get("parts")
    if not isinstance(parts, list):
        return None
    texts = [part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    return "\n".join(texts)[:1000] or None


def _a2a_message_text(message: Any) -> str | None:
    """提取 A2A 消息 parts 中的文本内容。

    参数：message 为 A2A Message 字典或其他值。
    返回值：按 parts 顺序拼接的文本；没有文本时返回 None。
    """
    if not isinstance(message, dict):
        return None
    parts = message.get("parts")
    if not isinstance(parts, list):
        return None
    texts = [part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    return "\n".join(texts) or None


class RawEventHandler:
    """过滤 A2A 进度的原始副本，其他 RAW 事件照常转发。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """过滤将由 CUSTOM 事件处理的 A2A 进度 RAW 副本。

        参数：event 为 RAW 事件，state 为本次运行的活动状态。
            返回值：普通 RAW 事件；A2A 进度副本不产生输出。
        """
        raw = event.event
        if not (
            isinstance(raw, dict)
            and raw.get("event") == "on_custom_event"
            and raw.get("name") == A2A_PROGRESS_EVENT
        ):
            yield event
            return

        # LangGraphAgent 还会为同一 on_custom_event 发出 AG-UI CUSTOM；由 CUSTOM 唯一创建 activity。
        return


def _a2a_progress_activity(progress: Any, state: ActivityState) -> Iterator[BaseEvent]:
    """把 A2A progress payload 转成活动时间线增量。

    参数：progress 为 A2A_PROGRESS_EVENT 的 value；state 为当前运行活动状态。
    返回值：活动快照（如尚未发送）及对应活动增量。
    """
    if not isinstance(progress, dict):
        return

    # A2A JSON 编码器可能输出协议名称或数值枚举；两种形式都归一到 schema 状态。
    task_id = progress.get("taskId") or ""
    task_status = progress.get("status")
    task_status = task_status if isinstance(task_status, dict) else {}
    remote_state = task_status.get("state")
    state_names = {
        0: "TASK_STATE_UNSPECIFIED",
        1: "TASK_STATE_SUBMITTED",
        2: "TASK_STATE_WORKING",
        3: "TASK_STATE_COMPLETED",
        4: "TASK_STATE_FAILED",
        5: "TASK_STATE_CANCELED",
        6: "TASK_STATE_INPUT_REQUIRED",
        7: "TASK_STATE_REJECTED",
        8: "TASK_STATE_AUTH_REQUIRED",
    }
    if isinstance(remote_state, int) and not isinstance(remote_state, bool):
        remote_state = state_names.get(remote_state, remote_state)
    status = {
        "TASK_STATE_COMPLETED": "completed",
        "TASK_STATE_FAILED": "failed",
        "TASK_STATE_CANCELED": "cancelled",
        "TASK_STATE_REJECTED": "rejected",
        "TASK_STATE_INPUT_REQUIRED": "awaiting_input",
        "TASK_STATE_AUTH_REQUIRED": "awaiting_auth",
    }.get(remote_state, "running")
    message = task_status.get("message")
    update = progress.get("update")
    update = update if isinstance(update, dict) else {}
    if message is None:
        message = update.get("message")
    message_text = _a2a_message_text(message)
    values = {
        "correlation": {"taskId": task_id},
        "status": "completed",
        "output": {"text": message_text} if message_text is not None else None,
        "error": _a2a_error_summary(task_status.get("message")) if status == "failed" else None,
        "finishedAt": iso_now(),
    }
    yield from state.ensure_snapshot()
    state.latest_a2a_activity, delta = state.add_activity("a2a_task", "远端任务", values)
    # 每个收到的 A2A 状态条目单独新增，并立即标记为 completed。
    yield state.update_activity(state.latest_a2a_activity, {
        "status": "completed", "finishedAt": values["finishedAt"],
        "output": values["output"], "error": values["error"],
        "correlation": values["correlation"],
    })


class CustomEventHandler:
    """将指定 AG-UI CUSTOM 事件转换为远端任务活动。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """处理 A2A progress 自定义事件，其他 CUSTOM 事件照常透传。

        参数：event 为 AG-UI CUSTOM 事件；state 为本次运行活动状态。
        返回值：A2A 活动快照/增量，或未识别的原始自定义事件。
        """
        if getattr(event, "name", None) != A2A_PROGRESS_EVENT:
            yield event
            return
        yield from _a2a_progress_activity(getattr(event, "value", None), state)


_STEP_ACTIVITY_TYPES = {
    nodes.ROUTE_TURN: ("mode_selection", "模式选择"),
    nodes.ASSISTANT: ("reasoning", "Thinking"),
    nodes.ASSESS_POD: ("reasoning", "Thinking"),
    nodes.FINALIZE: ("model_summary", "模型总结"),
    nodes.A2A_FALLBACK: ("model_summary", "模型总结"),
    nodes.APPROVE_POD_HANDOFF: ("tool_execution", "analyze_pod_issue"),
}


class StepStartedHandler:
    """为已配置的步骤创建活动条目。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """开始收集当前步骤的消息。

        参数：event 为步骤开始事件，state 为本次运行的活动状态。
        返回值：可选的活动增量及原始步骤开始事件。
        """
        step_name = getattr(event, "step_name", None)
        if step_name in _STEP_ACTIVITY_TYPES:
            state.step_parts.clear()
            state.step_reasoning_id = None
            state.active_step_name = step_name
            activity_type, title = _STEP_ACTIVITY_TYPES[step_name]
            extra = None
            if step_name == nodes.APPROVE_POD_HANDOFF:
                extra = {
                    "tool": {"name": "analyze_pod_issue", "server": "a2a-agent"},
                    "correlation": {"toolCallId": f"handoff:{state.content['run']['runId']}"},
                }
            state.step_activity, delta = state.add_activity(activity_type, title, extra)
            yield delta
        yield event


class StepFinishedHandler:
    """结束当前步骤活动并记录已收集的消息。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """在步骤名匹配时完成活动。

        参数：event 为步骤结束事件，state 为本次运行的活动状态。
        返回值：可选的活动增量及原始步骤结束事件。
        """
        step_name = getattr(event, "step_name", None)
        if (
            step_name in _STEP_ACTIVITY_TYPES
            and step_name == state.active_step_name
            and state.step_activity is not None
        ):
            yield state.update_activity(state.step_activity, {
                "status": "completed",
                "finishedAt": iso_now(),
                "output": state.step_output(),
            })
            state.step_activity = None
            state.active_step_name = None
            state.step_parts.clear()
        yield event


def _append_text(event: Any, state: ActivityState) -> None:
    """将助手文本片段追加到最终消息及当前步骤。

    参数：event 为带文本片段的事件，state 为本次运行的活动状态。
    返回值：无。
    """
    message_id = event.message_id or state.final_message_id
    if message_id and message_id == state.final_message_id and event.delta:
        state.final_message_parts.append(event.delta)
        if state.step_activity is not None:
            state.step_parts.setdefault(("text", message_id), []).append(event.delta)


class TextMessageStartHandler:
    """开始收集新的助手消息。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """在助手消息开始时重置累计文本。

        参数：event 为文本开始事件，state 为本次运行的活动状态。
        返回值：原始文本开始事件。
        """
        if event.role == "assistant" and event.message_id:
            state.final_message_id = event.message_id
            state.final_message_parts = []
        yield event


class TextMessageContentHandler:
    """收集当前助手消息的文本。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """追加匹配当前助手消息的内容。

        参数：event 为文本内容事件，state 为本次运行的活动状态。
        返回值：原始文本内容事件。
        """
        _append_text(event, state)
        yield event


class TextMessageChunkHandler:
    """处理可同时携带消息标识与文本的分块事件。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """切换到新的助手消息并追加分块内容。

        参数：event 为文本分块事件，state 为本次运行的活动状态。
        返回值：原始文本分块事件。
        """
        if event.role == "assistant" and event.message_id and event.message_id != state.final_message_id:
            state.final_message_id = event.message_id
            state.final_message_parts = []
        _append_text(event, state)
        yield event


class ReasoningMessageStartHandler:
    """记录当前步骤的推理消息标识。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """记录开始接收的推理消息。

        参数：event 为推理开始事件，state 为本次运行的活动状态。
        返回值：原始推理开始事件。
        """
        if state.step_activity is not None:
            state.step_reasoning_id = event.message_id
        yield event


class ReasoningMessageContentHandler:
    """将推理内容和推理分块中的增量追加到当前步骤。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """按推理消息标识累计当前步骤的推理内容。

        参数：event 为推理内容或分块事件，state 为本次运行的活动状态。
        返回值：原始推理事件。
        """
        if state.step_activity is not None:
            state.step_reasoning_id = event.message_id or state.step_reasoning_id
            if state.step_reasoning_id and event.delta:
                state.step_parts.setdefault(("reasoning", state.step_reasoning_id), []).append(event.delta)
        yield event


class ReasoningMessageEndHandler:
    """清除当前步骤的推理消息标识。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """结束当前推理消息的接收。

        参数：event 为推理结束事件，state 为本次运行的活动状态。
        返回值：原始推理结束事件。
        """
        if state.step_activity is not None:
            state.step_reasoning_id = None
        yield event


class ToolCallStartHandler:
    """为工具调用创建活动条目。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """登记工具调用标识及其活动条目。

        参数：event 为工具调用开始事件，state 为本次运行的活动状态。
        返回值：活动增量及原始工具调用开始事件。
        """
        item, delta = state.add_activity("tool_execution", event.tool_call_name, {
            "tool": {"name": event.tool_call_name},
            "correlation": {"toolCallId": event.tool_call_id},
            "input": {},
        })
        state.active_by_tool_call[event.tool_call_id] = item
        yield delta
        yield event


class ToolCallResultHandler:
    """将工具结果写入关联的活动条目。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """结束有匹配条目的工具调用并保留其结果。

        参数：event 为工具调用结果事件，state 为本次运行的活动状态。
        返回值：可选的活动增量及原始工具结果事件。
        """
        item = state.active_by_tool_call.pop(event.tool_call_id, None)
        if item:
            result = getattr(event, "result", None)
            try:
                normalized = json.loads(json.dumps(result, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                normalized = {"summary": str(result)[:2000]}
            yield state.update_activity(item, {
                "status": "completed",
                "finishedAt": iso_now(),
                "output": normalized,
            })
        yield event


class RunStartedHandler:
    """发送运行开始事件和初始活动快照。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """转发运行开始事件并按需发送快照。

        参数：event 为运行开始事件，state 为本次运行的活动状态。
        返回值：原始运行开始事件及可选的活动快照。
        """
        yield event
        yield from state.ensure_snapshot()


def _mark_approval_activities(outcome: Any, state: ActivityState) -> Iterator[BaseEvent]:
    """根据审批中断关联工具调用，并补齐图节点直接发起的工具活动。

    参数：outcome 为运行中断结果，state 为本次运行的活动状态。
    返回值：工具活动的创建与待审批状态增量。
    """
    thread_id = state.content["run"]["threadId"]
    by_request_id = {
        tool_approval_request_id(thread_id, call_id): item
        for call_id, item in state.active_by_tool_call.items()
    }
    for interrupt in outcome.interrupts:
        request = (interrupt.metadata or {}).get("langgraph", {}).get("raw")
        if not isinstance(request, dict) or request.get("type") != "approval_request":
            continue
        request_id = request["request_id"]
        item = by_request_id.get(request_id)
        if (
            item is None
            and state.active_step_name == nodes.APPROVE_POD_HANDOFF
            and state.step_activity is not None
        ):
            item = state.step_activity
            yield state.update_activity(item, {
                "correlation": {"toolCallId": request_id},
                "request_id": request_id,
            })
            by_request_id[request_id] = item
        if item is None:
            tool_name = request["tool_name"]
            item, delta = state.add_activity("tool_execution", tool_name, {
                "tool": {"name": tool_name},
                "correlation": {"toolCallId": request_id},
                "request_id": request_id,
            })
            by_request_id[request_id] = item
            yield delta
        yield state.update_activity(item, {
            "status": "awaiting_approval",
            "finishedAt": None,
            "request_id": request_id,
        })


def _finish_run(
    state: ActivityState,
    status: str,
    error: str | None,
    interrupted: bool,
    *,
    approval_activities_marked: bool = False,
) -> Iterator[BaseEvent]:
    """收尾尚未结束的活动并更新运行状态。

    参数：state 为本次运行的活动状态，status 为运行终态，error 为错误信息，
        interrupted 表示运行是否暂停等待审批，approval_activities_marked 表示已按审批请求更新工具活动。
    返回值：按远端任务、步骤、工具调用、运行的顺序产生相关增量事件。
    """
    latest_a2a_activity = state.latest_a2a_activity
    if status == "failed" and latest_a2a_activity is not None and latest_a2a_activity["status"] == "running":
        yield state.update_activity(latest_a2a_activity, {
            "status": "failed",
            "finishedAt": iso_now(),
            "error": error,
        })

    terminal = "cancelled" if status == "completed" else status
    if state.step_activity is not None:
        yield state.update_activity(state.step_activity, {
            "status": terminal,
            "finishedAt": None if interrupted else iso_now(),
            "output": state.step_output(),
        })
    for item in state.active_by_tool_call.values():
        if item["status"] == "running" and not approval_activities_marked:
            yield state.update_activity(item, {
                "status": terminal,
                "finishedAt": None if terminal == "awaiting_approval" else iso_now(),
            })

    now = iso_now()
    values = {
        "status": status,
        "updatedAt": now,
        "finishedAt": now,
        "error": (error[:1000] or None) if error is not None else None,
    }
    state.content["run"].update(values)
    yield state.patch([
        {"op": "replace", "path": f"/run/{key}", "value": value}
        for key, value in values.items()
    ])


class RunFinishedHandler:
    """处理运行成功或暂停等待审批。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """填写成功结果并收尾运行中的活动。

        参数：event 为运行结束事件，state 为本次运行的活动状态。
        返回值：可选快照、活动增量及原始运行结束事件。
        """
        yield from state.ensure_snapshot()
        outcome = getattr(event, "outcome", None)
        interrupted = getattr(outcome, "type", None) == "interrupt"
        if not interrupted and state.final_message_parts:
            event.result = "".join(state.final_message_parts)
        event.timestamp = event.timestamp or timestamp_ms()
        status = "awaiting_approval" if interrupted else "completed"
        approval_activities_marked = False
        if interrupted:
            for delta in _mark_approval_activities(outcome, state):
                approval_activities_marked = True
                yield delta
        yield from _finish_run(
            state, status, None, interrupted,
            approval_activities_marked=approval_activities_marked,
        )
        yield event


class RunErrorHandler:
    """处理运行失败。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """记录运行错误并结束仍在运行的活动。

        参数：event 为运行错误事件，state 为本次运行的活动状态。
        返回值：可选快照、活动增量及原始运行错误事件。
        """
        yield from state.ensure_snapshot()
        interrupted = getattr(getattr(event, "outcome", None), "type", None) == "interrupt"
        yield from _finish_run(state, "failed", str(getattr(event, "message", "")), interrupted)
        yield event


_REASONING_CONTENT = ReasoningMessageContentHandler()
EVENT_HANDLERS: dict[EventType, EventHandler] = {
    EventType.RAW: RawEventHandler(),
    EventType.CUSTOM: CustomEventHandler(),
    EventType.STEP_STARTED: StepStartedHandler(),
    EventType.STEP_FINISHED: StepFinishedHandler(),
    EventType.TEXT_MESSAGE_START: TextMessageStartHandler(),
    EventType.TEXT_MESSAGE_CONTENT: TextMessageContentHandler(),
    EventType.TEXT_MESSAGE_CHUNK: TextMessageChunkHandler(),
    EventType.REASONING_MESSAGE_START: ReasoningMessageStartHandler(),
    EventType.REASONING_MESSAGE_CONTENT: _REASONING_CONTENT,
    EventType.REASONING_MESSAGE_CHUNK: _REASONING_CONTENT,
    EventType.REASONING_MESSAGE_END: ReasoningMessageEndHandler(),
    EventType.TOOL_CALL_START: ToolCallStartHandler(),
    EventType.TOOL_CALL_RESULT: ToolCallResultHandler(),
    EventType.RUN_STARTED: RunStartedHandler(),
    EventType.RUN_FINISHED: RunFinishedHandler(),
    EventType.RUN_ERROR: RunErrorHandler(),
}
