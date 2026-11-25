"""定义 Pod 诊断转交审批及确认条件。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from k8s_ops_agent.approval import request_mcp_tool_approval


async def request_pod_handoff_approval(
    request_id: str, payload: dict[str, Any], config: RunnableConfig
) -> bool:
    """请求人工确认是否将 Pod 诊断转交给 Coding Agent。

    参数：
        request_id: 已写入 checkpoint 的审批请求标识。
        payload: 待发送的 A2A 请求内容。
        config: 包含当前对话和运行标识的图配置。
    返回值：审批标识匹配且用户明确确认转交时为 True。
    """
    decision = await request_mcp_tool_approval(
        request_id=request_id,
        thread_id=config.get("configurable", {}).get("thread_id", ""),
        run_id=str(config["run_id"]) if config.get("run_id") else None,
        server_name="a2a-agent",
        tool_name="analyze_pod_issue",
        tool_arguments=payload,
        presentation={
            "title": "转交 Pod 诊断给 Coding Agent",
            "description": "将变更计划中展示的本轮诊断内容发送给 A2A Agent 作进一步分析。",
            "inputs": [{
                "key": "handoff_confirmed",
                "type": "confirm",
                "label": "确认发送本轮 Pod 诊断",
                "required": True,
                "mustBeTrue": True,
            }],
        },
        execution={
            "mode": "approval_gate",
            "onApproved": "execute_original_call",
            "onRejected": "cancel_call",
        },
    )
    return (
        decision.get("status") == "approved"
        and decision.get("request_id") == request_id
        and isinstance(decision.get("answers"), dict)
        and decision["answers"].get("handoff_confirmed") is True
    )
