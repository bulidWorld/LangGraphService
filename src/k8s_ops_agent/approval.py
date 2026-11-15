"""定义人工审批提案和结构化审核输入。"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import interrupt

from k8s_ops_agent.approval_store import create_approval_request
from k8s_ops_agent.observability import logger


INPUT_TYPES = {"confirm", "single_select", "multi_select", "text"}


def _validate_input_definitions(inputs: list[dict[str, Any]]) -> None:
    """校验审批表单定义，拒绝前端无法安全解释的控件。

    参数：
        inputs: 审批请求中声明的表单控件。
    返回值：校验通过时无返回；定义非法时抛出 ValueError。
    """
    keys: set[str] = set()
    for field in inputs:
        if not isinstance(field, dict):
            raise ValueError("Approval inputs must be objects")
        key = field.get("key")
        label = field.get("label")
        field_type = field.get("type")
        if not isinstance(key, str) or not key or key in keys:
            raise ValueError("Approval input keys must be non-empty and unique")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"Approval input {key!r} needs a label")
        if field_type not in INPUT_TYPES:
            raise ValueError(f"Unsupported approval input type: {field_type!r}")
        if not isinstance(field.get("required", False), bool):
            raise ValueError(f"Approval input {key!r} required must be boolean")
        keys.add(key)

        options = field.get("options")
        if field_type in {"single_select", "multi_select"}:
            if not isinstance(options, list) or not options:
                raise ValueError(f"Approval input {key!r} needs options")
            values: set[str] = set()
            for option in options:
                value = option.get("value") if isinstance(option, dict) else None
                option_label = option.get("label") if isinstance(option, dict) else None
                if (
                    not isinstance(value, str)
                    or not value
                    or value in values
                    or not isinstance(option_label, str)
                    or not option_label.strip()
                ):
                    raise ValueError(f"Approval input {key!r} has invalid options")
                values.add(value)
        elif options is not None:
            raise ValueError(f"Approval input {key!r} does not accept options")

        if field_type == "text" and "max_length" in field:
            max_length = field["max_length"]
            if not isinstance(max_length, int) or not 1 <= max_length <= 10000:
                raise ValueError(f"Approval input {key!r} max_length is invalid")


async def request_mcp_tool_approval(
    *,
    request_id: str,
    thread_id: str,
    run_id: str | None,
    server_name: str,
    tool_name: str,
    tool_arguments: dict[str, Any],
    approval_role: str | None,
    inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """为 MCP 工具调用创建审批请求并等待人工决定。

    参数：
        request_id: 此次工具调用稳定的审批请求 UUID。
        thread_id: LangGraph 对话标识。
        run_id: 发起调用的运行标识，可为空。
        server_name: 提供工具的 MCP 服务器名称。
        tool_name: MCP 工具名称。
        tool_arguments: 本次工具调用参数，仅保存到审批请求中供审核。
        approval_role: MCP 元数据声明的审核角色。
        inputs: 审批人需要填写的结构化表单定义。
    返回值：恢复后 LangGraph interrupt 收到的审批决定。
    """
    _validate_input_definitions(inputs)
    request_payload = {
        "type": "approval_request",
        "schema_version": 1,
        "request_id": request_id,
        "action": tool_name,
        "title": f"审批 MCP 工具调用：{tool_name}",
        "rationale": f"MCP 服务器 {server_name} 发布的工具策略要求人工审批。",
        "change_plan": json.dumps(tool_arguments, ensure_ascii=False, default=str),
        "evidence": [],
        "inputs": inputs,
        "mcp_server": server_name,
        "tool_name": tool_name,
        "approval_role": approval_role,
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


@tool
async def request_human_approval(
    action: Literal["business_fix", "restart_container"],
    rationale: str,
    change_plan: str,
    evidence: list[dict[str, Any]],
    config: Annotated[RunnableConfig, InjectedToolArg],
    title: str = "人工审批请求",
    inputs: list[dict[str, Any]] | None = None,
) -> str:
    """提交一项结构化修复建议供人工审核，不会应用变更或重启容器。

    参数：
        action: 需要审核的动作类型。
        rationale: 触发审批的原因。
        change_plan: 拟议修改方案或操作步骤。
        evidence: 日志、指标、代码等证据摘要列表。
        config: LangGraph 注入的运行配置，包含 thread 和稳定审批请求标识。
        title: 前端展示的审批标题。
        inputs: 可选的确认、单选、多选或文本控件定义。
    返回值：人工决定的 JSON 文本，交回排障模型继续流程。
    """
    configurable = config.get("configurable", {})
    request_id = configurable.get("approval_request_id")
    thread_id = configurable.get("thread_id")
    if not request_id or not thread_id:
        raise RuntimeError("Approval request requires a thread and stable request id")

    normalized_inputs = inputs or []
    _validate_input_definitions(normalized_inputs)
    request_payload = {
        "type": "approval_request",
        "schema_version": 1,
        "request_id": request_id,
        "action": action,
        "title": title,
        "rationale": rationale,
        "change_plan": change_plan,
        "evidence": evidence,
        "inputs": normalized_inputs,
    }
    await create_approval_request(
        request_id=request_id,
        thread_id=thread_id,
        run_id=str(config.get("run_id")) if config.get("run_id") else None,
        payload=request_payload,
    )
    logger.info(
        "Human approval requested",
        extra={"log_type": "business", "action": action, "request_id": request_id},
    )
    # LangGraph 会将中断保存在当前 thread；同一 tool call 重放时请求 ID 和 payload 保持稳定。
    decision = interrupt(request_payload)
    if isinstance(decision, dict) and decision.get("__agui_cancelled__"):
        decision = {"request_id": request_id, "status": "cancelled"}
    logger.info(
        "Human approval decision received",
        extra={
            "log_type": "business",
            "action": action,
            "request_id": request_id,
        },
    )
    return json.dumps(decision, ensure_ascii=False, default=str)
