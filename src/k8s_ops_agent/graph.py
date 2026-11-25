"""``k8s_ops_agent`` 部署图的 LangGraph 入口。"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_core.callbacks.manager import AsyncCallbackManager
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command

from k8s_ops_agent.a2a_client import A2ATaskState, build_a2a_task_graph
from k8s_ops_agent.a2a_client.pod_parameters import extract_pod_parameters
from k8s_ops_agent.approve.mcp_tool import request_tool_approval
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
    prepare_prompt_messages,
)
from k8s_ops_agent.read_tool import read_file
from k8s_ops_agent.search_tool import search_file_content
from k8s_ops_agent.workspace_resources import save_embedded_text_resources


class AgentState(MessagesState, A2ATaskState, total=False):
    """保留原始消息并提供本轮模型输入视图的 LangGraph 状态。"""

    context_messages: list[BaseMessage]
    compression_metadata: dict[str, Any]
    workspace_files: list[dict[str, str]]
    pod_approval_id: str | None


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


def _tool_message_content(output: Any) -> Any:
    """将工具结果转换为 ToolMessage 可接受的内容。

    参数：
        output: 工具执行结果。
    返回值：适合放入 ToolMessage.content 的内容。
    """
    if isinstance(output, (str, list)):
        return output
    value = output.model_dump(mode="python") if hasattr(output, "model_dump") else output
    return json.dumps(value, ensure_ascii=False, default=str)


def _tool_result_messages(
    output: Any, call_id: str, *, is_error: bool = False
) -> list[ToolMessage]:
    """根据工具返回的 MCP 结果结构生成消息。

    参数：
        output: MCP 工具返回结果。
        call_id: 对应的工具调用 ID。
    返回值：保留内容块和结构化结果的工具消息列表。
    """
    output_is_error = isinstance(output, dict) and output.get(
        "isError", output.get("is_error", False)
    )
    if is_error or output_is_error:
        content = output.get("content", output) if isinstance(output, dict) else output
        message = ToolMessage(
            content=_tool_message_content(content),
            tool_call_id=call_id,
            status="error",
        )
        return [message]

    if isinstance(output, dict) and isinstance(output.get("content"), list):
        content = output["content"]
        structured = output.get("structuredContent", output.get("structured_content"))
        if structured is not None:
            content = [*content, {
                "type": "text",
                "text": "结构化工具结果：" + json.dumps(structured, ensure_ascii=False, default=str),
            }]
        return [ToolMessage(content=_tool_message_content(content), tool_call_id=call_id)]

    return [ToolMessage(content=_tool_message_content(output), tool_call_id=call_id)]


async def _emit_failed_tool_result(
    tool: Any, call: dict[str, Any], config: RunnableConfig, message: ToolMessage
) -> None:
    """为已捕获异常的工具调用通过 LangChain 回调发送错误结果。

    参数：
        tool: 失败的 MCP 工具。
        call: 模型生成的工具调用。
        config: 当前 LangGraph 运行配置及其 AG-UI 回调。
        message: 与工具调用 ID 关联的错误结果消息。
    返回值：无；回调由 AG-UI adapter 转换为 TOOL_CALL_RESULT。
    """
    callback_manager = AsyncCallbackManager.configure(config.get("callbacks"))
    run_manager = await callback_manager.on_tool_start(
        {"name": tool.name, "description": getattr(tool, "description", "")},
        json.dumps(call.get("args", {}), ensure_ascii=False, default=str),
        name=tool.name,
        inputs=call.get("args", {}),
        tool_call_id=call["id"],
    )
    await run_manager.on_tool_end(message, name=tool.name)


async def _execute_mcp_tool(
    tool: Any,
    call: dict[str, Any],
    config: RunnableConfig,
    policy: dict[str, Any],
) -> tuple[Any, list[dict[str, str]], bool]:
    """按策略审批并执行 MCP 工具，转换其内嵌文本资源。

    参数：
        tool: MCP 工具对象。
        call: 模型生成的工具调用。
        config: 当前 LangGraph 运行配置。
        policy: 工具安全策略和服务元数据。
    返回值：工具返回结果及本次保存的工作区文件元数据。
    """
    name = call["name"]
    security = policy["security"]
    approval_mode = security.get("approvalMode", "none")
    default_action = security.get("defaultAction", "require_approval")
    if approval_mode == "conditional" and default_action == "deny":
        return f"Tool {name!r} was denied by its MCP security policy.", [], False

    thread_id = config.get("configurable", {}).get("thread_id", "")
    requires_approval = approval_mode == "required" or (
        approval_mode == "conditional" and default_action == "require_approval"
    )
    if requires_approval:
        decision = await request_tool_approval(call, config, policy)
        if decision.get("status") != "approved":
            return (
                f"Tool {name!r} was not executed because approval status was "
                f"{decision.get('status', 'unknown')}.",
                [],
                False,
            )

    server_name = policy["mcp_server"]
    started = time.perf_counter()
    try:
        async with operation_observation(
            "mcp.tool", {"mcp_server": server_name, "tool": name}
        ) as observation:
            output = await tool.ainvoke(
                {"type": "tool_call", **call},
                config=config,
            )
            is_error = isinstance(output, dict) and output.get(
                "isError", output.get("is_error", False)
            )
            output, new_files = save_embedded_text_resources(
                output,
                thread_id=str(thread_id),
                server_name=server_name,
                tool_name=name,
            )
            update_observation(observation, output={"status": "success"})
        logger.info(
            "MCP tool request completed",
            extra={
                "log_type": "http", "method": "POST",
                "path": f"mcp.servers/{server_name}/tools/{name}",
                "mcp_server": server_name, "tool": name, "status": 200,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return output, new_files, is_error
    except Exception as exc:
        logger.error(
            "MCP tool request failed",
            extra={
                "log_type": "http", "method": "POST",
                "path": f"mcp.servers/{server_name}/tools/{name}",
                "mcp_server": server_name, "tool": name, "status": 500,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        logger.exception(
            "MCP tool invocation failed",
            extra={"log_type": "exception", "mcp_server": server_name, "tool": name},
        )
        error_message = ToolMessage(
            content=f"Tool call failed ({type(exc).__name__}): {exc}",
            tool_call_id=call["id"],
            status="error",
        )
        await _emit_failed_tool_result(tool, call, config, error_message)
        return error_message.content, [], True


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
        call_messages, new_files = await _run_tool_call(tool, call, config)
        workspace_files.extend(new_files)
        results.extend(call_messages)
    return {"messages": results, "workspace_files": workspace_files}


async def _run_tool_call(
    tool: BaseTool | None, call: ToolCall, config: RunnableConfig
) -> tuple[list[ToolMessage], list[dict[str, str]]]:
    """执行单个已注册工具并转换结果。

    参数：
        tool: 按名称找到的工具，未注册时为 None。
        call: 模型生成的工具调用。
        config: 当前运行配置。
    返回值：工具结果消息和新增工作区文件。
    """
    name = call["name"]
    if tool is None:
        output = f"Tool {name!r} is not registered."
        return [ToolMessage(content=output, tool_call_id=call["id"])], []
    if name in {read_file.name, search_file_content.name}:
        output = await tool.ainvoke({"type": "tool_call", **call}, config=config)
        return _tool_result_messages(output, call["id"]), []

    policy = get_tool_policy(tool)
    output, new_files, is_error = await _execute_mcp_tool(tool, call, config, policy)
    return _tool_result_messages(output, call["id"], is_error=is_error), new_files


def route_after_assistant(state: MessagesState) -> Literal["tools", "assess_pod"]:
    """判断主诊断是否仍需执行工具。

    参数：
        state: 包含最新模型回复的消息状态。
    返回值：执行工具或评估最终诊断是否涉及 Pod。
    """
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return "assess_pod"


async def assess_pod(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """评估本轮最终诊断，并准备供人工审批的 A2A 消息。

    参数：
        state: 包含本轮用户消息、工具结果和主诊断最终回复的图状态。
        config: 当前会话的运行配置，用于执行补查工具。
    返回值：本轮审批标识、待审批载荷和工作区文件更新。
    """
    updates: dict[str, Any] = {
        "pod_approval_id": None,
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
        results, new_files = await _run_tool_call(tool_map[call["name"]], call, config)
        workspace_files.extend(new_files)
        return results

    parameters = await extract_pod_parameters(
        state["messages"], _model, load_tools=load_tools, execute_tool=execute_tool
    )
    if workspace_files:
        updates["workspace_files"] = workspace_files
    if parameters is None:
        return updates

    diagnosis, repository, tag = parameters
    request_id = str(uuid.uuid4())
    updates["pod_approval_id"] = request_id
    updates["a2a_payload"] = {
        "message": {
            "messageId": request_id,
            "role": "ROLE_USER",
            "parts": [{
                "data": {
                    "type": "coding",
                    "repository": repository,
                    "branch": tag,
                    "instruction": A2A_CODING_PROMPT.format(diagnosis=diagnosis),
                },
            }],
        }
    }
    return updates


async def approve_pod_handoff(
    state: AgentState, config: RunnableConfig
) -> Command[Literal["a2a", "__end__"]]:
    """用 MTAP 审批门禁控制已保存的 Pod 诊断转交请求。

    参数：
        state: 包含已经写入 checkpoint 的审批标识和 A2A 请求。
        config: 包含当前 thread 和 run 标识的运行配置。
    返回值：获批时路由到 A2A 子图，其余情况结束当前运行。
    """
    request_id = state.get("pod_approval_id")
    if not request_id:
        return Command(goto=END)
    approved = await request_pod_handoff_approval(
        request_id, state["a2a_payload"], config
    )
    return Command(goto="a2a" if approved else END)


def build_graph(*, checkpointer: Any = None):
    """构建包含主诊断、Pod 转交审批和 A2A 子图的运维流程图。

    参数：
        checkpointer: 可选的 LangGraph 持久化 checkpoint 实现。
    返回值：编译后的运维 LangGraph。
    """
    # 压缩节点只生成本轮模型输入视图，不覆盖 checkpoint 中的原始 messages。
    # 工具返回后重新计算视图，assistant 节点始终读取最新压缩结果。
    builder = StateGraph(AgentState)
    builder.add_node("context_compression", context_compression_node)
    builder.add_node("assistant", assistant_node)
    builder.add_node("tools", tools_node)
    builder.add_node("assess_pod", assess_pod)
    builder.add_node("approve_pod_handoff", approve_pod_handoff)
    builder.add_node("a2a", build_a2a_task_graph())
    builder.add_edge(START, "context_compression")
    builder.add_edge("context_compression", "assistant")
    builder.add_conditional_edges(
        "assistant",
        route_after_assistant,
        {"tools": "tools", "assess_pod": "assess_pod"},
    )
    builder.add_edge("tools", "context_compression")
    builder.add_edge("assess_pod", "approve_pod_handoff")
    builder.add_edge("a2a", END)
    return builder.compile(checkpointer=checkpointer)
