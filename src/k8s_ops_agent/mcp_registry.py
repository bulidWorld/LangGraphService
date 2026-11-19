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

import jsonschema
import yaml
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

MCP_CONFIG_PATH = Path(os.getenv("MCP_SERVERS_CONFIG_FILE", "/etc/langgraph-service/secrets/MCP_SERVERS_CONFIG"))
MCP_TOOL_SCHEMA_PATH = Path(os.getenv("MCP_TOOL_SCHEMA_FILE", "/app/models/mtap-tool.schema.yaml"))
APPROVAL_META_KEY = "com.zhiwenxia/approval"
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
    """从 Kubernetes Secret 挂载文件读取 MCP Server 配置。

    参数：无。
    返回值：展开环境变量占位符后的 MCP Server 配置对象。
    """
    if not MCP_CONFIG_PATH.is_file():
        raise ValueError(f"MCP Server 配置文件不存在：{MCP_CONFIG_PATH}")

    try:
        config = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"MCP Server 配置文件不是有效 JSON：{MCP_CONFIG_PATH}") from exc

    if not isinstance(config, dict):
        raise ValueError("MCP Server 配置必须是 JSON 对象")

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
            # 单个 MCP 服务不可用时跳过该服务，继续加载其余服务的工具。
            continue
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
            # 注册阶段就拒绝不符合 MTAP 的工具，避免策略错误直到模型调用时才暴露。
            try:
                get_tool_policy(tool)
            except ValueError:
                logger.exception(
                    "MCP 工具 MTAP 元数据校验失败，跳过该工具",
                    extra={
                        "log_type": "exception",
                        "mcp_server": server_name,
                        "tool_name": tool.name,
                    },
                )
                continue
            tools.append(tool)
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
    """按 MTAP JSON Schema 校验并读取 MCP 工具审批定义。

    参数：
        tool: MCP 适配器生成的 LangChain 工具。
    返回值：包含 security、presentation、execution 和 mcp_server 的策略对象。
    """
    metadata = tool.metadata or {}
    raw_meta = metadata.get("_meta")
    if not isinstance(raw_meta, dict) or APPROVAL_META_KEY not in raw_meta:
        raise ValueError(f"Missing MTAP approval metadata for {tool.name!r}")
    input_schema = getattr(tool, "args_schema", None)
    if hasattr(input_schema, "model_json_schema"):
        input_schema = input_schema.model_json_schema()
    elif not isinstance(input_schema, dict):
        input_schema = {"type": "object"}
    definition = {
        "name": tool.name,
        "description": getattr(tool, "description", "") or "",
        "inputSchema": input_schema,
        "_meta": {APPROVAL_META_KEY: raw_meta[APPROVAL_META_KEY]},
    }
    try:
        schema = _load_mcp_tool_schema()
        jsonschema.Draft202012Validator(schema).validate(definition)
        jsonschema.validators.validator_for(input_schema).check_schema(input_schema)
        approval = raw_meta[APPROVAL_META_KEY]
        _validate_approval_semantics(approval)
    except (jsonschema.ValidationError, jsonschema.SchemaError, OSError, ValueError) as exc:
        raise ValueError(f"Invalid MTAP metadata for MCP tool {tool.name!r}: {exc}") from exc

    return {
        "security": approval["security"],
        "presentation": approval.get("presentation"),
        "execution": approval.get("execution"),
        "mcp_server": metadata.get("mcp_server", "unknown"),
    }


def _validate_approval_semantics(approval: dict[str, Any]) -> None:
    """补充校验 JSON Schema 无法表达的表单字段和值唯一性。

    参数：
        approval: MTAP 扩展命名空间中的审批定义。
    返回值：语义校验通过时无返回；重复 key/value 时抛出 ValueError。
    """
    presentation = approval.get("presentation")
    if not isinstance(presentation, dict):
        return
    keys: set[str] = set()
    for field in presentation["inputs"]:
        if field["key"] in keys:
            raise ValueError(f"Duplicate approval input key: {field['key']!r}")
        keys.add(field["key"])
        values: set[str] = set()
        for option in field.get("options", []):
            if option["value"] in values:
                raise ValueError(
                    f"Duplicate option value for approval input {field['key']!r}"
                )
            values.add(option["value"])


@lru_cache(maxsize=1)
def _load_mcp_tool_schema() -> dict[str, Any]:
    """读取并缓存 MTAP 工具 JSON Schema。

    参数：无。
    返回值：Draft 2020-12 格式的工具 schema。
    """
    with MCP_TOOL_SCHEMA_PATH.open(encoding="utf-8") as schema_file:
        schema = yaml.safe_load(schema_file)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema
