"""按 AG-UI 事件类型构建活动时间线。"""

import json
from collections.abc import Iterator
from typing import Any, Protocol

from ag_ui.core.events import BaseEvent, EventType

from k8s_ops_agent.a2a_client.graph import A2A_PROGRESS_EVENT

from .state import ActivityState, iso_now, timestamp_ms


class EventHandler(Protocol):
    """单个 AG-UI 事件的处理策略。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """处理输入事件并按发送顺序产生输出事件。

        参数：event 为原始事件，state 为本次运行的活动状态。
        返回值：按发送顺序产生原始事件或活动事件的迭代器。
        """
        ...


class RawEventHandler:
    """将 A2A 进度转换为远端任务活动。"""

    def handle(self, event: Any, state: ActivityState) -> Iterator[BaseEvent]:
        """处理 RAW 事件，普通 RAW 事件直接转发。

        参数：event 为 RAW 事件，state 为本次运行的活动状态。
        返回值：远端任务活动事件或原始 RAW 事件。
        """
        raw = event.event
        if not (
            isinstance(raw, dict)
            and raw.get("event") == "on_custom_event"
            and raw.get("name") == A2A_PROGRESS_EVENT
        ):
            yield event
            return

        progress = raw["data"]
        task_id = progress["taskId"]
        remote_state = progress["status"].get("state")
        status = {
            "TASK_STATE_COMPLETED": "completed",
            "TASK_STATE_FAILED": "failed",
            "TASK_STATE_CANCELED": "cancelled",
            "TASK_STATE_REJECTED": "rejected",
            "TASK_STATE_INPUT_REQUIRED": "awaiting_input",
            "TASK_STATE_AUTH_REQUIRED": "awaiting_auth",
        }.get(remote_state, "running")
        if not task_id and "message" in progress["update"]:
            status = "completed"
        values = {
            "correlation": {"taskId": task_id},
            "status": status,
            "output": progress,
            "finishedAt": iso_now() if status in {
                "completed", "failed", "cancelled", "rejected",
            } else None,
        }
        yield from state.ensure_snapshot()
        if state.a2a_activity is None:
            state.a2a_activity, delta = state.add_activity("a2a_task", "远端任务", values)
        else:
            delta = state.update_activity(state.a2a_activity, values)
        yield delta


_STEP_ACTIVITY_TYPES = {
    "route_turn": ("reasoning", "Thinking"),
    "assistant": ("reasoning", "Thinking"),
    "assess_pod": ("reasoning", "Thinking"),
    "finalize": ("model_summary", "模型总结"),
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
            state.step_activity, delta = state.add_activity(*_STEP_ACTIVITY_TYPES[step_name])
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


def _finish_run(
    state: ActivityState,
    status: str,
    error: str | None,
    interrupted: bool,
) -> Iterator[BaseEvent]:
    """收尾尚未结束的活动并更新运行状态。

    参数：state 为本次运行的活动状态，status 为运行终态，error 为错误信息，
        interrupted 表示运行是否暂停等待审批。
    返回值：按远端任务、步骤、工具调用、运行的顺序产生相关增量事件。
    """
    if status == "failed" and state.a2a_activity is not None and state.a2a_activity["status"] == "running":
        yield state.update_activity(state.a2a_activity, {
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
        if item["status"] == "running":
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
        interrupted = getattr(getattr(event, "outcome", None), "type", None) == "interrupt"
        if not interrupted and state.final_message_parts:
            event.result = "".join(state.final_message_parts)
        event.timestamp = event.timestamp or timestamp_ms()
        status = "awaiting_approval" if interrupted else "completed"
        yield from _finish_run(state, status, None, interrupted)
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
