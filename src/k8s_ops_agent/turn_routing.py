"""在运维诊断与 A2A 编码续聊之间选择本轮入口。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import MessagesState
from langgraph.types import Command
from pydantic import BaseModel

from k8s_ops_agent import graph_node_constants as nodes
from k8s_ops_agent.a2a_client import A2ATaskState
from k8s_ops_agent.prompts import build_turn_routing_messages


class TurnRoutingState(MessagesState, A2ATaskState, total=False):
    """入口路由所需的对话消息和 A2A 任务状态。"""

    a2a_coding: dict[str, Any]


class TurnRouting(BaseModel):
    """判断当前用户消息是否继续已有编码会话。"""

    continue_coding: bool


def _coding_context_id(state: TurnRoutingState) -> str:
    """从远端任务或流式响应读取 A2A 上下文标识，兼容已有 checkpoint。

    参数：
        state: 当前会话保存的 A2A 任务和事件。
    返回值：远端上下文标识，缺失时返回空字符串。
    """
    task = state.get("a2a_task", {})
    context_id = task.get("contextId") or task.get("context_id")
    if context_id:
        return context_id
    event_keys = (
        "task", "message", "statusUpdate", "status_update", "artifactUpdate", "artifact_update"
    )
    for event in reversed(state.get("a2a_events", [])):
        for key in event_keys:
            update = event.get(key) or {}
            context_id = update.get("contextId") or update.get("context_id")
            if context_id:
                return context_id
    # 远端本轮可能只返回消息或事件，且省略已在请求中指定的上下文。
    return state.get("a2a_payload", {}).get("message", {}).get("contextId", "")


async def route_turn(
    state: TurnRoutingState, *, model_factory: Callable[[], ChatOpenAI]
) -> Command[str]:
    """按当前用户意图开启运维诊断或续接已授权的 A2A 编码对话。

    参数：
        state: 包含当前用户消息、已有转交请求和远端回复的会话状态。
        model_factory: 创建续聊意图判断模型的函数。
    返回值：目标节点，以及续聊时使用的增量 A2A 请求。
    """
    messages = state.get("messages", [])
    context_id = _coding_context_id(state)
    if not context_id or not messages or not isinstance(messages[-1], HumanMessage):
        return Command(goto=nodes.CONTEXT_COMPRESSION)

    user_text = messages[-1].text
    message = state.get("a2a_payload", {}).get("message", {})
    parts = message.get("parts", [])
    first_part = parts[0] if parts else {}
    coding = state.get("a2a_coding") or first_part.get("data", {})
    if not user_text.strip() or coding.get("type") != "coding" or not (
        coding.get("repository") and coding.get("branch") and coding.get("tag")
    ):
        return Command(goto=nodes.CONTEXT_COMPRESSION)

    # 只取近期方案文本辅助判断，完整上下文由远端会话维护。
    remote_message = state.get("a2a_status", {}).get("message") or {}
    if state.get("a2a_events"):
        remote_message = state["a2a_events"][-1].get("message") or remote_message
    artifacts = state.get("a2a_artifacts", [])
    plan_parts = list(remote_message.get("parts", []))
    if artifacts:
        plan_parts.extend(artifacts[-1].get("parts", []))
    plan = "\n".join(part.get("text", "") for part in plan_parts if part.get("text"))
    classifier = model_factory().with_structured_output(TurnRouting)
    decision = await classifier.ainvoke(
        build_turn_routing_messages(
            repository=coding["repository"],
            branch=coding.get("branch"),
            tag=coding.get("tag"),
            previous_instruction=first_part.get("text", coding.get("instruction", "")),
            previous_coding_reply=plan,
            current_request=user_text,
        ),
        config={
            "tags": ["nostream"],
            "metadata": {"emit-messages": False, "emit-tool-calls": False},
        },
    )
    if not decision or not decision.continue_coding:
        return Command(goto=nodes.CONTEXT_COMPRESSION)

    follow_up = {
        "messageId": str(uuid.uuid4()),
        "contextId": context_id,
        "role": "ROLE_USER",
        "parts": [{"text": user_text}],
    }
    # 等待补充输入的任务需要 taskId。已结束的任务通过 contextId 续聊。
    if state.get("a2a_status", {}).get("state") == "TASK_STATE_INPUT_REQUIRED":
        task_id = state.get("a2a_task_id")
        if task_id:
            follow_up["taskId"] = task_id
    return Command(
        update={"a2a_payload": {"message": follow_up}, "a2a_coding": coding},
        goto=nodes.A2A,
    )
