"""主图和补查流程共用的工具执行逻辑。"""

from __future__ import annotations

import json
import time
from typing import Any

from langchain_core.callbacks.manager import AsyncCallbackManager
from langchain_core.messages import ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from k8s_ops_agent.approve.mcp_tool import request_tool_approval
from k8s_ops_agent.http_logging import payload_log_fields, response_log_fields
from k8s_ops_agent.mcp_registry import get_tool_policy
from k8s_ops_agent.observability import logger, operation_observation, update_observation
from k8s_ops_agent.read_tool import read_file
from k8s_ops_agent.search_tool import search_file_content
from k8s_ops_agent.workspace_resources import save_embedded_text_resources


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
    request_body_fields = payload_log_fields(call.get("args", {}))
    response_body_fields: dict[str, Any] = {}
    try:
        async with operation_observation(
            "mcp.tool", {"mcp_server": server_name, "tool": name}
        ) as observation:
            output = await tool.ainvoke(
                {"type": "tool_call", **call},
                config=config,
            )
            response_body_fields = response_log_fields(output)
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
                **request_body_fields, **response_body_fields,
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
                **request_body_fields, **response_body_fields,
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


async def run_tool_call(
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


