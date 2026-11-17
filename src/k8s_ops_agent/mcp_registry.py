"""从已配置的 MCP 服务加载运维工具。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import Future
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from k8s_ops_agent.approval import INPUT_TYPES, _validate_input_definitions

MCP_CONFIG_PATH = Path.cwd() / "mcp_server.json"
ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
logger = logging.getLogger("k8s_ops_agent")
_TOOLS_CACHE_TTL_SECONDS = 10 * 60
_tools_cache_lock = threading.Lock()
_tools_cache: list[BaseTool] | None = None
_tools_cache_expires_at = 0.0
_tools_refresh_future: Future[list[BaseTool]] | None = None


def expand_env(value: Any) -> Any:
    """递归替换配置中的环境变量占位符。"""

    if isinstance(value, str):
        def replace(match):
            key = match.group(1)
            env_value = os.getenv(key)
            if env_value is None:
                raise ValueError(f"环境变量 {key} 未配置")
            return env_value
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    if isinstance(value, dict):
        return {key: expand_env(val) for key, val in value.items()}

    if isinstance(value, list):
        return [expand_env(item) for item in value]

    return value


@lru_cache(maxsize=1)
def load_mcp_config() -> dict[str, Any]:
    """从环境变量读取 MCP Server 配置。"""

    config_str = os.getenv("MCP_SERVERS_CONFIG")

    if not config_str:
        raise ValueError("环境变量 MCP_SERVERS_CONFIG 未配置")

    config = json.loads(config_str)

    if not isinstance(config, dict):
        raise ValueError("MCP_SERVERS_CONFIG 必须是 JSON 对象")

    return expand_env(config)


@lru_cache(maxsize=1)
def _client() -> MultiServerMCPClient:
    """读取 mcp_server.json 和环境变量中的凭据并返回客户端。
    参数：无。
    返回值：已缓存的 MCP 多服务客户端。
    """
    config = load_mcp_config()
    return MultiServerMCPClient(config)


async def get_operational_tools() -> list[BaseTool]:
    """返回已配置 MCP 服务提供的工具。

    参数：无。
    返回值：携带来源服务器名称的 LangChain 工具列表。

    MCP 服务定义采用 ``langchain-mcp-adapters`` 规定的格式。将工具发现逻辑集中在
    此处，使图结构不依赖具体的 Kubernetes、日志、代码仓库或监控集成。
    """
    global _tools_cache, _tools_cache_expires_at, _tools_refresh_future

    # 锁只保护缓存状态，不跨越 await；并发调用通过共享 Future 等待同一次刷新。
    with _tools_cache_lock:
        if _tools_cache is not None and time.monotonic() < _tools_cache_expires_at:
            return list(_tools_cache)
        refresh_future = _tools_refresh_future
        is_refresh_owner = refresh_future is None
        if is_refresh_owner:
            refresh_future = Future()
            _tools_refresh_future = refresh_future

    if not is_refresh_owner:
        # shield 防止单个等待者取消时连带取消其他线程共享的刷新 Future。
        return list(await asyncio.shield(asyncio.wrap_future(refresh_future)))

    try:
        tools = await _discover_operational_tools()
    except BaseException as exc:
        with _tools_cache_lock:
            if _tools_refresh_future is refresh_future:
                _tools_refresh_future = None
        refresh_future.set_exception(exc)
        raise

    with _tools_cache_lock:
        _tools_cache = list(tools)
        _tools_cache_expires_at = time.monotonic() + _TOOLS_CACHE_TTL_SECONDS
        if _tools_refresh_future is refresh_future:
            _tools_refresh_future = None
    refresh_future.set_result(list(tools))
    return list(tools)


async def _discover_operational_tools() -> list[BaseTool]:
    """逐个 MCP 服务发现工具并记录查询过程。

    参数：无。
    返回值：携带来源服务器名称的 LangChain 工具列表。
    """
    client = _client()
    tools: list[BaseTool] = []
    # 按服务逐个发现工具，避免适配器合并结果后丢失工具来源。
    for server_name in client.connections:
        logger.info(
            "开始查询 MCP 可用工具",
            extra={"log_type": "business", "mcp_server": server_name},
        )
        try:
            server_tools = await client.get_tools(server_name=server_name)
        except Exception:
            logger.exception(
                "MCP 可用工具查询失败",
                extra={"log_type": "exception", "mcp_server": server_name},
            )
            raise
        logger.info(
            "MCP 可用工具查询结果",
            extra={
                "log_type": "business",
                "mcp_server": server_name,
                "tool_count": len(server_tools),
                "tool_names": [tool.name for tool in server_tools],
                "tools_found": bool(server_tools),
            },
        )
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
        raise ValueError(
            f"MCP tool {tool.name!r} requires approvalRole for its fallback"
        )
    if approval_mode == "none" and approval_role:
        raise ValueError(
            f"MCP tool {tool.name!r} cannot use approvalRole with approvalMode=none"
        )
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
