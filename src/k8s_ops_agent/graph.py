"""``k8s_ops_agent`` 部署图的 LangGraph 入口。"""

from __future__ import annotations

import os
import time
import uuid
from functools import partial
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import MessagesState, StateGraph
from langgraph.types import Command

from k8s_ops_agent import graph_node_constants as nodes
from k8s_ops_agent.a2a_client import build_a2a_task_graph
from k8s_ops_agent.a2a_client.pod_parameters import extract_pod_parameters
from k8s_ops_agent.approve.pod_handoff import request_pod_handoff_approval
from k8s_ops_agent.context_compression import build_context_compression_node
from k8s_ops_agent.mcp_registry import get_operational_tool_map, get_tool_policy
from k8s_ops_agent.observability import (
    logger,
    operation_observation,
    update_observation,
)
from k8s_ops_agent.prompts import (
    A2A_CODING_PROMPT,
    FINAL_DIAGNOSIS_PROMPT,
    prepare_prompt_messages,
)
from k8s_ops_agent.read_tool import read_file
from k8s_ops_agent.search_tool import search_file_content
from k8s_ops_agent.tool_execution import run_tool_call
from k8s_ops_agent.turn_routing import TurnRoutingState, route_turn


class AgentState(TurnRoutingState, total=False):
    """保留原始消息并提供本轮模型输入视图的 LangGraph 状态。"""

    context_messages: list[BaseMessage]
    compression_metadata: dict[str, Any]
    workspace_files: list[dict[str, str]]
    pod_approval_id: str | None
    pod_handoff_ready: bool


context_compression_node = build_context_compression_node(prepare_prompt_messages)


def _model() -> ChatOpenAI:
    """创建启用推理摘要的主模型。

    参数：无。
    返回值：使用 Responses API 并请求自动推理摘要的模型实例。
    """
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0,
        use_responses_api=True,
        reasoning={"effort": "high", "summary": "detailed"},
    )


async def assistant_node(
    state: AgentState, config: RunnableConfig
) -> dict[str, list[BaseMessage]]:
    """按当前请求配置生成上下文并调用模型。

    参数：
        state: 当前 LangGraph 消息状态。
        config: 当前运行配置，包含会话 thread_id。
    返回值：新增的模型回复消息。
    """
    # 每轮模型调用时发现 MCP 工具，确保使用当前配置的服务；串行调用工具以保持
    # 排查证据的先后顺序。
    tools = list((await get_operational_tool_map()).values()) + [
        read_file,
        search_file_content,
    ]
    model = _model().bind_tools(tools, parallel_tool_calls=False)
    messages = state["context_messages"]
    started = time.perf_counter()
    try:
        async with operation_observation(
            "openai.chat", {"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini")}
        ) as observation:
            response = await model.ainvoke(messages)
            update_observation(observation, output={"status": "success"})
        logger.info(
            "Model request completed",
            extra={
                "log_type": "http",
                "method": "POST",
                "path": "openai.chat.completions",
                "status": 200,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
    except Exception:
        logger.error(
            "Model HTTP request failed",
            extra={
                "log_type": "http",
                "method": "POST",
                "path": "openai.chat.completions",
                "status": 500,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        logger.exception(
            "Model request failed",
            extra={"log_type": "exception"},
        )
        raise
    return {"messages": [response], "context_messages": []}


async def tools_node(
    state: AgentState, config: RunnableConfig
) -> dict[str, Any]:
    """按模型给出的顺序执行工具，并为审批中断生成稳定请求标识。

    参数：
        state: 当前 LangGraph 消息状态。
        config: 当前 thread 的运行配置。
    返回值：新增工具结果消息。
    """
    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage):
        return {"messages": []}

    tool_map = await get_operational_tool_map()
    tool_map[read_file.name] = read_file
    tool_map[search_file_content.name] = search_file_content
    results: list[ToolMessage] = []
    workspace_files = list(state.get("workspace_files", []))
    for call in last_message.tool_calls:
        name = call["name"]
        tool = tool_map.get(name)
        call_messages, new_files = await run_tool_call(tool, call, config)
        workspace_files.extend(new_files)
        results.extend(call_messages)
    return {"messages": results, "workspace_files": workspace_files}


def route_after_assistant(state: MessagesState) -> str:
    """判断主诊断是否仍需执行工具。

    参数：
        state: 包含最新模型回复的消息状态。
    返回值：执行工具或评估最终诊断是否涉及 Pod。
    """
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return nodes.TOOLS
    return nodes.ASSESS_POD


async def assess_pod(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """评估本轮最终诊断，并准备供人工审批的 A2A 消息。

    参数：
        state: 包含本轮用户消息、工具结果和主诊断最终回复的图状态。
        config: 当前会话的运行配置，用于执行补查工具。
    返回值：本轮审批标识、待审批载荷和工作区文件更新。
    """
    updates: dict[str, Any] = {
        "pod_approval_id": None,
        "a2a_coding": {},
        "a2a_payload": {},
        "a2a_response": {},
        "a2a_task": {},
        "a2a_task_id": "",
        "a2a_status": {},
        "a2a_events": [],
        "a2a_artifacts": [],
    }
    tool_map: dict[str, BaseTool] = {}
    workspace_files = list(state.get("workspace_files", []))

    async def load_tools() -> list[BaseTool]:
        """加载可直接执行的只读工具。

        参数：无。
        返回值：本次 Pod 补查可用的工具列表。
        """
        for tool in (await get_operational_tool_map()).values():
            if (tool.metadata or {}).get("readOnlyHint") is not True:
                continue
            security = get_tool_policy(tool)["security"]
            mode = security["approvalMode"]
            if mode == "none" or (mode == "conditional" and security.get("defaultAction") == "allow"):
                tool_map[tool.name] = tool
        tool_map[read_file.name] = read_file
        tool_map[search_file_content.name] = search_file_content
        return list(tool_map.values())

    async def execute_tool(call: ToolCall) -> list[ToolMessage]:
        """用当前会话配置执行补查并收集生成的文件。

        参数：
            call: 本次补查的工具调用。
        返回值：供模型分析的工具结果消息。
        """
        results, new_files = await run_tool_call(tool_map[call["name"]], call, config)
        workspace_files.extend(new_files)
        return results

    parameters = await extract_pod_parameters(
        state["messages"], _model, load_tools=load_tools, execute_tool=execute_tool
    )
    if workspace_files:
        updates["workspace_files"] = workspace_files
    if parameters is None:
        updates["pod_handoff_ready"] = False
        return updates

    diagnosis, repository, branch, tag = parameters
    request_id = str(uuid.uuid4())
    updates["pod_approval_id"] = request_id
    updates["pod_handoff_ready"] = True
    updates["a2a_payload"] = {
        "message": {
            "messageId": request_id,
            "role": "ROLE_USER",
            "parts": [{
                "data": {
                    "type": "coding",
                    "repository": repository,
                    "branch": branch,
                    "tag": tag,
                    "instruction": A2A_CODING_PROMPT.format(diagnosis=diagnosis),
                },
            }],
        }
    }
    return updates


def route_after_assessment(state: AgentState) -> str:
    """按 Pod 转交准备状态选择审批流程或生成用户总结。

    参数：
        state: 包含 Pod 评估结果的当前图状态。
    返回值：审批节点或最终答复节点名称。
    """
    return nodes.APPROVE_POD_HANDOFF if state.get("pod_handoff_ready") else nodes.FINALIZE


async def finalize_node(state: AgentState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
    """将诊断结论整理为面向用户的总结性答复。

    参数：
        state: 包含诊断过程消息的图状态。
        config: 当前运行配置。
    返回值：追加一条总结性 assistant 消息。
    """
    messages = state.get("messages", [])
    model = _model()
    response = await model.ainvoke([
        *messages,
        # 结构化评估只用于内部路由，不应作为用户答复内容。
        SystemMessage(content=FINAL_DIAGNOSIS_PROMPT),
    ])
    return {"messages": [response]}


def _has_a2a_content(parts: list[dict[str, Any]]) -> bool:
    """判断远端消息或产物是否包含可交付内容。

    参数：
        parts: A2A 消息或产物的内容片段。
    返回值：是否有非空文本、结构化数据或文件内容。
    """
    return any(
        (part.get("text") or "").strip()
        or part.get("data")
        or part.get("url")
        or part.get("raw")
        for part in parts
    )


def route_after_a2a(state: AgentState) -> str:
    """按本轮 A2A 收集到的消息或产物选择后续节点。

    参数：
        state: 包含本轮 A2A 事件、状态和合并后产物的图状态。
    返回值：有有效内容时结束主图，否则进入模型兜底节点。
    """
    if any(
        _has_a2a_content(artifact.get("parts", []))
        for artifact in state.get("a2a_artifacts", [])
    ):
        return nodes.END

    for event in state.get("a2a_events", []):
        update = event.get("task") or event.get("statusUpdate") or event.get("status_update") or {}
        status = update.get("status") or {}
        message = event.get("message")
        if not message and status.get("state") not in (
            0, 1, 2, "TASK_STATE_UNSPECIFIED", "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING",
        ):
            message = status.get("message")
        message = message or {}
        if message.get("role") not in ("ROLE_USER", 1) and _has_a2a_content(
            message.get("parts", [])
        ):
            return nodes.END
    return nodes.A2A_FALLBACK


async def a2a_fallback_node(state: AgentState) -> dict[str, list[BaseMessage]]:
    """远端缺少有效答复时，依据对话历史回复用户。

    参数：
        state: 包含用户当前请求和已保存对话消息的图状态。
    返回值：追加到对话历史的助手回复。
    """
    response = await _model().ainvoke([
        SystemMessage(content=(
            "远端编码助手本轮没有返回可用答复。请根据对话历史回答用户最后一条消息，"
            "提供已有信息能支持的解释或建议。不要声称远端任务已执行或完成，"
            "也不要编造新的执行结果。若信息不足，请明确说明。"
        )),
        *state.get("messages", []),
    ])
    return {"messages": [response]}


async def approve_pod_handoff(
    state: AgentState, config: RunnableConfig
) -> Command[str]:
    """用 MTAP 审批门禁控制已保存的 Pod 诊断转交请求。

    参数：
        state: 包含已经写入 checkpoint 的审批标识和 A2A 请求。
        config: 包含当前 thread 和 run 标识的运行配置。
    返回值：获批时路由到 A2A 子图，其余情况结束当前运行。
    """
    request_id = state.get("pod_approval_id")
    if not request_id:
        return Command(goto=nodes.END)
    approved = await request_pod_handoff_approval(
        request_id, state["a2a_payload"], config
    )
    return Command(goto=nodes.A2A if approved else nodes.END)


def build_graph(*, checkpointer: Any = None):
    """构建包含主诊断、Pod 转交审批和 A2A 子图的运维流程图。

    参数：
        checkpointer: 可选的 LangGraph 持久化 checkpoint 实现。
    返回值：编译后的运维 LangGraph。
    """
    # 压缩节点只生成本轮模型输入视图，不覆盖 checkpoint 中的原始 messages。
    # 工具返回后重新计算视图，assistant 节点始终读取最新压缩结果。
    builder = StateGraph(AgentState)
    builder.add_node(
        nodes.ROUTE_TURN,
        partial(route_turn, model_factory=_model),
        destinations=(nodes.CONTEXT_COMPRESSION, nodes.A2A),
    )
    builder.add_node(nodes.CONTEXT_COMPRESSION, context_compression_node)
    builder.add_node(nodes.ASSISTANT, assistant_node)
    builder.add_node(nodes.TOOLS, tools_node)
    builder.add_node(nodes.ASSESS_POD, assess_pod)
    builder.add_node(
        nodes.APPROVE_POD_HANDOFF,
        approve_pod_handoff,
        destinations=(nodes.A2A, nodes.END),
    )
    builder.add_node(nodes.FINALIZE, finalize_node)
    builder.add_node(nodes.A2A, build_a2a_task_graph())
    builder.add_node(nodes.A2A_FALLBACK, a2a_fallback_node)
    builder.add_edge(nodes.START, nodes.ROUTE_TURN)
    builder.add_edge(nodes.CONTEXT_COMPRESSION, nodes.ASSISTANT)
    builder.add_conditional_edges(
        nodes.ASSISTANT,
        route_after_assistant,
        [nodes.TOOLS, nodes.ASSESS_POD],
    )
    builder.add_edge(nodes.TOOLS, nodes.CONTEXT_COMPRESSION)
    builder.add_conditional_edges(
        nodes.ASSESS_POD,
        route_after_assessment,
        [nodes.APPROVE_POD_HANDOFF, nodes.FINALIZE],
    )
    builder.add_edge(nodes.FINALIZE, nodes.END)
    builder.add_conditional_edges(nodes.A2A, route_after_a2a, [nodes.A2A_FALLBACK, nodes.END])
    builder.add_edge(nodes.A2A_FALLBACK, nodes.END)
    return builder.compile(checkpointer=checkpointer)
