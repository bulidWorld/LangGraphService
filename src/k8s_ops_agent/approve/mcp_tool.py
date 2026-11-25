"""根据 MTAP 策略发起 MCP 工具审批。"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig

from k8s_ops_agent.approval import request_mcp_tool_approval
from k8s_ops_agent.observability import operation_observation


async def request_tool_approval(
    call: dict[str, Any], config: RunnableConfig, policy: dict[str, Any]
) -> dict[str, Any]:
    """使用工具策略中的审批表单和执行规则请求人工决定。

    参数：
        call: 包含工具名称、调用标识和参数的模型工具调用。
        config: 包含当前对话和运行标识的图配置。
        policy: MTAP 工具策略，包含 MCP 服务、审批表单和执行规则。
    返回值：恢复图执行时收到的审批决定。
    """
    thread_id = config.get("configurable", {}).get("thread_id", "")
    run_id = config.get("run_id")
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mcp-approval:{thread_id}:{call['id']}"))
    server_name = policy["mcp_server"]
    tool_name = call["name"]
    async with operation_observation(
        "approval.request", {"mcp_server": server_name, "tool": tool_name}
    ):
        return await request_mcp_tool_approval(
            request_id=request_id,
            thread_id=thread_id,
            run_id=str(run_id) if run_id else None,
            server_name=server_name,
            tool_name=tool_name,
            tool_arguments=call.get("args", {}),
            presentation=policy["presentation"],
            execution=policy["execution"],
        )
