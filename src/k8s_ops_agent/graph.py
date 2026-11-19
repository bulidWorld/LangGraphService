"""``k8s_ops_agent`` 部署图的 LangGraph 入口。"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.callbacks.manager import AsyncCallbackManager
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph

from k8s_ops_agent.approval import request_human_approval, request_mcp_tool_approval
from k8s_ops_agent.context_compression import build_context_compression_node
from k8s_ops_agent.db_config import database_conninfo
from k8s_ops_agent.mcp_registry import get_operational_tool_map, get_tool_policy
from k8s_ops_agent.observability import (
    logger,
    operation_observation,
    update_observation,
)
from k8s_ops_agent.prompts import prepare_prompt_messages
from k8s_ops_agent.read_tool import read_file
from k8s_ops_agent.search_tool import search_file_content
from k8s_ops_agent.workspace_resources import save_embedded_text_resources


class AgentState(MessagesState, total=False):
    """保留原始消息并提供本轮模型输入视图的 LangGraph 状态。"""

    context_messages: list[BaseMessage]
    compression_metadata: dict[str, Any]
    workspace_files: list[dict[str, str]]


context_compression_node = build_context_compression_node(prepare_prompt_messages)


def _model() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0,
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


async def _execute_approval_tool(
    tool: Any, call: dict[str, Any], config: RunnableConfig
) -> Any:
    """执行人工审批工具并传入稳定的审批请求标识。

    参数：
        tool: 人工审批工具。
        call: 模型生成的工具调用。
        config: 当前 LangGraph 运行配置。
    返回值：审批工具的执行结果。
    """
    thread_id = config.get("configurable", {}).get("thread_id", "")
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"k8s_ops_agent:{thread_id}:{call['id']}"))
    tool_config = {
        **config,
        "configurable": {
            **config.get("configurable", {}),
            "approval_request_id": request_id,
        },
    }
    async with operation_observation("approval.request", {"tool": tool.name}):
        return await tool.ainvoke(
            {"type": "tool_call", **call},
            config=tool_config,
        )


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
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mcp-approval:{thread_id}:{call['id']}"))
        server_name = policy["mcp_server"]
        async with operation_observation(
            "approval.request", {"mcp_server": server_name, "tool": name}
        ):
            decision = await request_mcp_tool_approval(
                request_id=request_id,
                thread_id=thread_id,
                run_id=str(config.get("run_id")) if config.get("run_id") else None,
                server_name=server_name,
                tool_name=name,
                tool_arguments=call.get("args", {}),
                presentation=policy["presentation"],
                execution=policy["execution"],
            )
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
        request_human_approval,
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
    tool_map[request_human_approval.name] = request_human_approval
    tool_map[read_file.name] = read_file
    tool_map[search_file_content.name] = search_file_content
    results: list[ToolMessage] = []
    workspace_files = list(state.get("workspace_files", []))
    for call in last_message.tool_calls:
        name = call["name"]
        tool = tool_map.get(name)
        if tool is None:
            output: Any = f"Tool {name!r} is not registered."
            call_messages = [ToolMessage(content=str(output), tool_call_id=call["id"])]
        elif name == request_human_approval.name:
            # LangGraph 中断属于流程控制，不是工具异常；让中断向外传播，服务才能
            # 暂停执行并等待人工审批。
            output = await _execute_approval_tool(tool, call, config)
            call_messages = _tool_result_messages(output, call["id"])
        elif name in {read_file.name, search_file_content.name}:
            output = await tool.ainvoke(
                {"type": "tool_call", **call},
                config=config,
            )
            call_messages = _tool_result_messages(output, call["id"])
        else:
            # MCP 工具结果统一规范化，但根据 CallToolResult 的错误、内容块和结构化
            # 返回类型分别构造消息；普通返回值则保留原有的 JSON 转换行为。
            policy = get_tool_policy(tool)
            output, new_files, is_error = await _execute_mcp_tool(
                tool, call, config, policy
            )
            workspace_files.extend(new_files)
            call_messages = _tool_result_messages(
                output, call["id"], is_error=is_error
            )
        results.extend(call_messages)
    return {"messages": results, "workspace_files": workspace_files}


def route_after_assistant(state: MessagesState) -> Literal["tools", "end"]:
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return "end"


def build_graph(*, checkpointer: Any = None):
    """构建含上下文压缩节点的运维流程图。

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
    builder.add_edge(START, "context_compression")
    builder.add_edge("context_compression", "assistant")
    builder.add_conditional_edges(
        "assistant",
        route_after_assistant,
        {"tools": "tools", "end": END},
    )
    builder.add_edge("tools", "context_compression")
    return builder.compile(checkpointer=checkpointer)
