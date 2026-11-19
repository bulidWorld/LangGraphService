"""定义人工审批提案和结构化审核输入。"""

from __future__ import annotations

import json
from typing import Any

from langgraph.types import interrupt

from k8s_ops_agent.approval_store import create_approval_request
from k8s_ops_agent.observability import logger


async def request_mcp_tool_approval(
    *,
    request_id: str,
    thread_id: str,
    run_id: str | None,
    server_name: str,
    tool_name: str,
    tool_arguments: dict[str, Any],
    presentation: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    """为 MCP 工具调用创建审批请求并等待人工决定。

    参数：
        request_id: 此次工具调用稳定的审批请求 UUID。
        thread_id: LangGraph 对话标识。
        run_id: 发起调用的运行标识，可为空。
        server_name: 提供工具的 MCP 服务器名称。
        tool_name: MCP 工具名称。
        tool_arguments: 本次工具调用参数，仅保存到审批请求中供审核。
        presentation: MTAP schema 声明的审批标题、说明及表单字段。
        execution: MTAP schema 声明的审批后执行语义。
    返回值：恢复后 LangGraph interrupt 收到的审批决定。
    """
    request_payload = {
        "type": "approval_request",
        "schema_version": 1,
        "request_id": request_id,
        "action": tool_name,
        "title": presentation["title"],
        "rationale": presentation["description"],
        "change_plan": json.dumps(tool_arguments, ensure_ascii=False, default=str),
        "evidence": [],
        "inputs": presentation["inputs"],
        "mcp_server": server_name,
        "tool_name": tool_name,
        "execution": execution,
    }
    await create_approval_request(
        request_id=request_id,
        thread_id=thread_id,
        run_id=run_id,
        payload=request_payload,
    )
    logger.info(
        "MCP tool approval requested",
        extra={
            "log_type": "business",
            "request_id": request_id,
            "mcp_server": server_name,
            "tool": tool_name,
        },
    )
    decision = interrupt(request_payload)
    logger.info(
        "MCP tool approval decision received",
        extra={"log_type": "business", "request_id": request_id,
               "mcp_server": server_name, "tool": tool_name,
               "status": decision.get("status") if isinstance(decision, dict) else "unknown"},
    )
    return decision if isinstance(decision, dict) else {"status": "rejected"}
