"""``k8s_ops_agent`` 部署图的 LangGraph 入口。"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph

from k8s_ops_agent.approval import request_human_approval, request_mcp_tool_approval
from k8s_ops_agent.context_compression import build_context_compression_node
from k8s_ops_agent.mcp_registry import get_operational_tool_map, get_tool_policy
from k8s_ops_agent.observability import (
    logger,
    operation_observation,
    update_observation,
)
from k8s_ops_agent.prompts import prepare_prompt_messages


class AgentState(MessagesState, total=False):
    """保留原始消息并提供本轮模型输入视图的 LangGraph 状态。"""

    context_messages: list[BaseMessage]
    compression_metadata: dict[str, Any]


context_compression_node = build_context_compression_node(prepare_prompt_messages)


def _model() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0,
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
    tools = list((await get_operational_tool_map()).values()) + [request_human_approval]
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
    state: MessagesState, config: RunnableConfig
) -> dict[str, list[ToolMessage]]:
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
    results: list[ToolMessage] = []
    for call in last_message.tool_calls:
        name = call["name"]
        tool = tool_map.get(name)
        if tool is None:
            output: Any = f"Tool {name!r} is not registered."
        elif name == request_human_approval.name:
            # LangGraph 中断属于流程控制，不是工具异常；让中断向外传播，服务才能
            # 暂停执行并等待人工审批。
            async with operation_observation("approval.request", {"tool": name}):
                thread_id = config.get("configurable", {}).get("thread_id", "")
                request_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"k8s_ops_agent:{thread_id}:{call['id']}",
                    )
                )
                tool_config = {
                    **config,
                    "configurable": {
                        **config.get("configurable", {}),
                        "approval_request_id": request_id,
                    },
                }
                output = await tool.ainvoke(call.get("args", {}), config=tool_config)
        else:
            # 排查用只读工具失败时，将错误作为证据返回给模型，让模型说明信息缺口，
            # 而不是直接终止整个排查流程。
            policy = get_tool_policy(tool)
            security = policy["security"]
            approval_mode = security.get("approvalMode", "none")
            default_action = security.get("defaultAction", "require_approval")
            requires_approval = approval_mode == "required" or (
                approval_mode == "conditional" and default_action == "require_approval"
            )
            if approval_mode == "conditional" and default_action == "deny":
                output = f"Tool {name!r} was denied by its MCP security policy."
                results.append(ToolMessage(content=str(output), tool_call_id=call["id"]))
                continue
            if requires_approval:
                thread_id = config.get("configurable", {}).get("thread_id", "")
                request_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL,
                              f"mcp-approval:{thread_id}:{call['id']}")
                )
                server_name = policy["mcp_server"]
                async with operation_observation(
                    "approval.request",
                    {"mcp_server": server_name, "tool": name},
                ):
                    decision = await request_mcp_tool_approval(
                        request_id=request_id,
                        thread_id=thread_id,
                        run_id=str(config.get("run_id")) if config.get("run_id") else None,
                        server_name=server_name,
                        tool_name=name,
                        tool_arguments=call.get("args", {}),
                        approval_role=security.get("approvalRole"),
                        inputs=policy["approval"]["inputs"],
                    )
                if decision.get("status") != "approved":
                    output = (
                        f"Tool {name!r} was not executed because approval status was "
                        f"{decision.get('status', 'unknown')}."
                    )
                    results.append(ToolMessage(content=output, tool_call_id=call["id"]))
                    continue

            started = time.perf_counter()
            try:
                server_name = policy["mcp_server"]
                async with operation_observation(
                    "mcp.tool", {"mcp_server": server_name, "tool": name}
                ) as observation:
                    output = await tool.ainvoke(call.get("args", {}))
                    update_observation(observation, output={"status": "success"})
                logger.info(
                    "MCP tool request completed",
                    extra={
                        "log_type": "http",
                        "method": "POST",
                        "path": f"mcp.servers/{server_name}/tools/{name}",
                        "mcp_server": server_name,
                        "tool": name,
                        "status": 200,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
            except Exception as exc:
                # 工具调用失败也作为证据返回，供助手说明或尝试其他排查路径。
                logger.error(
                    "MCP tool request failed",
                    extra={
                        "log_type": "http",
                        "method": "POST",
                        "path": f"mcp.servers/{server_name}/tools/{name}",
                        "mcp_server": server_name,
                        "tool": name,
                        "status": 500,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
                logger.exception(
                    "MCP tool invocation failed",
                    extra={"log_type": "exception", "mcp_server": server_name, "tool": name},
                )
                output = f"Tool call failed ({type(exc).__name__}): {exc}"
        results.append(ToolMessage(content=str(output), tool_call_id=call["id"]))
    return {"messages": results}


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


