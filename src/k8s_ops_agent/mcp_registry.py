"""从已配置的 MCP 服务加载运维工具。"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from k8s_ops_agent.approval import INPUT_TYPES, _validate_input_definitions

MCP_CONFIG_PATH = Path.cwd() / "mcp_server.json"
ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_config_value(value: Any) -> Any:
    """递归解析 MCP 配置中的环境变量占位符。

    参数：
        value: JSON 解码后的配置值。
    返回值：占位符已替换的配置值。
    """
    if isinstance(value, str):
        def replace_placeholder(match: re.Match[str]) -> str:
            """用对应环境变量替换配置字符串中的占位符。

            参数：
                match: 匹配到的环境变量占位符。
            返回值：环境变量值；变量未设置时抛出配置错误。
            """
            variable_name = match.group(1)
            variable_value = os.getenv(variable_name)
            if variable_value is None:
                raise ValueError(
                    f"Environment variable {variable_name} is required by mcp_server.json"
                )
            return variable_value

        return ENV_PLACEHOLDER_PATTERN.sub(replace_placeholder, value)
    if isinstance(value, list):
        return [_resolve_config_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_config_value(item)
            for key, item in value.items()
        }
    return value


@lru_cache(maxsize=1)
def _client() -> MultiServerMCPClient:
    """读取 mcp_server.json 和环境变量中的凭据并返回客户端。

    参数：无。
    返回值：已缓存的 MCP 多服务客户端。
    """
    # 配置文件和凭据在进程内缓存；修改 MCP 地址或令牌后需要重启服务生效。
    try:
        with MCP_CONFIG_PATH.open(encoding="utf-8") as config_file:
            config: Any = json.load(config_file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{MCP_CONFIG_PATH.name} must contain a valid JSON object") from exc
    except OSError as exc:
        raise ValueError(f"Unable to read MCP configuration file: {MCP_CONFIG_PATH}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"{MCP_CONFIG_PATH.name} must be keyed by server name")
    config = _resolve_config_value(config)
    return MultiServerMCPClient(config)


async def get_operational_tools() -> list[BaseTool]:
    """返回已配置 MCP 服务提供的工具。

    参数：无。
    返回值：携带来源服务器名称的 LangChain 工具列表。

    MCP 服务定义采用 ``langchain-mcp-adapters`` 规定的格式。将工具发现逻辑集中在
    此处，使图结构不依赖具体的 Kubernetes、日志、代码仓库或监控集成。
    """
    client = _client()
    tools: list[BaseTool] = []
    # 按服务逐个发现工具，避免适配器合并结果后丢失工具来源。
    for server_name in client.connections:
        server_tools = await client.get_tools(server_name=server_name)
        for tool in server_tools:
            tool.metadata = {**(tool.metadata or {}), "mcp_server": server_name}
        tools.extend(server_tools)
    return tools


async def get_operational_tool_map() -> dict[str, BaseTool]:
    """返回按名称索引的 MCP 工具，并拒绝名称冲突。

    参数：无。
    返回值：工具名称到工具实例的映射。
    """
    tools = await get_operational_tools()
    result: dict[str, BaseTool] = {}
    for tool in tools:
        # 图通过工具名称分发调用；名称冲突会导致调用落到不确定的服务，必须报错停止。
        if tool.name in result:
            raise ValueError(f"Duplicate MCP tool name: {tool.name}")
        result[tool.name] = tool
    return result


def get_tool_policy(tool: BaseTool) -> dict[str, Any]:
    """读取并校验 MCP 工具发布的审批策略和表单元数据。

    参数：
        tool: MCP 适配器生成的 LangChain 工具。
    返回值：包含 security、approval 和 mcp_server 的策略对象。
    """
    metadata = tool.metadata or {}
    raw_meta = metadata.get("_meta", {})
    raw_meta = raw_meta if isinstance(raw_meta, dict) else {}
    security = raw_meta.get("security")
    approval = raw_meta.get("approval", {})
    if security is None:
        security = {"approvalMode": "none"}
    if not isinstance(security, dict):
        raise ValueError(f"Invalid MCP security metadata for {tool.name!r}")

    risk_level = security.get("riskLevel", "low")
    approval_mode = security.get("approvalMode", "none")
    default_action = security.get("defaultAction", "require_approval")
    approval_role = security.get("approvalRole")
    if risk_level not in {"low", "medium", "high", "critical"}:
        raise ValueError(f"Invalid MCP riskLevel for {tool.name!r}")
    if approval_mode not in {"none", "required", "conditional"}:
        raise ValueError(f"Invalid MCP approvalMode for {tool.name!r}")
    if default_action not in {"allow", "deny", "require_approval"}:
        raise ValueError(f"Invalid MCP defaultAction for {tool.name!r}")
    if approval_role is not None and (
        not isinstance(approval_role, str) or not approval_role.strip()
    ):
        raise ValueError(f"Invalid MCP approvalRole for {tool.name!r}")
    if approval_mode == "required" and not approval_role:
        raise ValueError(f"MCP tool {tool.name!r} requires approvalRole")
    if (
        approval_mode == "conditional"
        and default_action == "require_approval"
        and not approval_role
    ):
        raise ValueError(f"MCP tool {tool.name!r} requires approvalRole for its fallback")
    if approval_mode == "none" and approval_role:
        raise ValueError(f"MCP tool {tool.name!r} cannot use approvalRole with approvalMode=none")
    if not isinstance(approval, dict):
        raise ValueError(f"Invalid MCP approval metadata for {tool.name!r}")

    schema_version = approval.get("schema_version", 1)
    input_policy = approval.get("inputPolicy", "fixed")
    allowed_types = approval.get(
        "allowedInputTypes", ["confirm", "single_select", "multi_select", "text"]
    )
    inputs = approval.get("inputs", [])
    if schema_version != 1 or input_policy not in {"fixed", "model_assisted"}:
        raise ValueError(f"Unsupported MCP approval schema for {tool.name!r}")
    if not isinstance(allowed_types, list) or any(
        item not in INPUT_TYPES for item in allowed_types
    ):
        raise ValueError(f"Invalid MCP allowedInputTypes for {tool.name!r}")
    if not isinstance(inputs, list):
        raise ValueError(f"Invalid MCP approval inputs for {tool.name!r}")
    _validate_input_definitions(inputs)
    if any(item["type"] not in allowed_types for item in inputs):
        raise ValueError(f"MCP approval input type is not allowed for {tool.name!r}")
    normalized_approval = {
        "schema_version": schema_version,
        "inputPolicy": input_policy,
        "allowedInputTypes": allowed_types,
        "inputs": inputs,
    }
    return {
        "security": security,
        "approval": normalized_approval,
        "mcp_server": metadata.get("mcp_server", "unknown"),
    }
