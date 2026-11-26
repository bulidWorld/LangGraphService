"""验证 MCP 工具发现失败后的降级、日志与缓存恢复。"""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.tools import StructuredTool
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from k8s_ops_agent import mcp_registry


def _tool(name: str) -> StructuredTool:
    """构造具有合法 MTAP 元数据的测试工具。

    参数：
        name: 测试工具名称。
    返回值：可通过注册校验的工具。
    """
    def run() -> str:
        """返回测试结果。参数：无。返回值：固定文本。"""
        return "ok"

    return StructuredTool.from_function(
        run,
        name=name,
        description="测试工具",
        metadata={
            "_meta": {
                mcp_registry.APPROVAL_META_KEY: {
                    "schemaVersion": 1,
                    "security": {"riskLevel": "low", "approvalMode": "none"},
                }
            }
        },
    )


class McpRegistryTest(unittest.IsolatedAsyncioTestCase):
    """覆盖 MCP 工具发现缓存策略。"""

    def setUp(self) -> None:
        """隔离缓存并设置可控时钟。参数：无。返回值：无。"""
        self.clock = Mock(return_value=100.0)
        self.client = SimpleNamespace(
            connections={"ci-build-service": {}, "healthy": {}},
            get_tools=AsyncMock(),
        )
        self.enterContext(patch.multiple(
            mcp_registry,
            _tools_cache=None,
            _tools_cache_expires_at=0.0,
            _tools_refresh_future=None,
            time=SimpleNamespace(monotonic=self.clock),
            MCP_TOOL_SCHEMA_PATH=Path(__file__).resolve().parents[1] / "models/mtap-tool.schema.yaml",
            _load_mcp_tool_schema=mcp_registry._load_mcp_tool_schema.__wrapped__,
        ))
        self.enterContext(patch.object(mcp_registry, "_client", return_value=self.client))

    async def test_nested_session_error_preserves_tools_and_recovers_after_short_cache(self) -> None:
        """验证会话错误提示、可用工具保留和短缓存恢复。参数：无。返回值：无。"""
        error = McpError(ErrorData(code=32600, message="Session terminated"))
        self.client.get_tools.side_effect = [
            ExceptionGroup("transport", [ExceptionGroup("session", [error])]),
            [_tool("healthy_tool")],
            [_tool("resolve_image_source")],
            [_tool("healthy_tool")],
        ]

        with self.assertLogs("k8s_ops_agent", level="ERROR") as logs:
            tools = await mcp_registry.get_operational_tools()
        self.assertEqual([tool.name for tool in tools], ["healthy_tool"])
        self.assertEqual(logs.records[0].mcp_server, "ci-build-service")
        self.assertIn("请检查 MCP 端点地址、部署版本及会话状态", logs.records[0].getMessage())

        self.clock.return_value = 114.0
        self.assertEqual(await mcp_registry.get_operational_tools(), tools)
        self.assertEqual(self.client.get_tools.await_count, 2)

        self.clock.return_value = 115.0
        recovered = await mcp_registry.get_operational_tools()
        self.assertEqual([tool.name for tool in recovered], ["resolve_image_source", "healthy_tool"])
        self.assertEqual(recovered[0].metadata["mcp_server"], "ci-build-service")

        self.clock.return_value = 714.0
        self.assertEqual(await mcp_registry.get_operational_tools(), recovered)
        self.assertEqual(self.client.get_tools.await_count, 4)

    async def test_empty_success_uses_normal_cache(self) -> None:
        """验证成功返回空列表时沿用正常缓存。参数：无。返回值：无。"""
        self.client.get_tools.return_value = []
        self.assertEqual(await mcp_registry.get_operational_tools(), [])
        self.clock.return_value = 115.0
        self.assertEqual(await mcp_registry.get_operational_tools(), [])
        self.assertEqual(self.client.get_tools.await_count, 2)
