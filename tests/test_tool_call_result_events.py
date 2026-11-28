"""验证 LangGraph 工具调用完成和失败时的 AG-UI 结果事件。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch
import unittest

from ag_ui.core.events import EventType
from ag_ui.core.types import AssistantMessage, FunctionCall, RunAgentInput, ToolCall
from ag_ui_langgraph import LangGraphAgent
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph

from k8s_ops_agent import graph as agent_graph
from k8s_ops_agent import graph_node_constants as nodes
from k8s_ops_agent import tool_execution


def _success_tool(value: str) -> str:
    """返回用于测试成功调用的固定结果。

    参数：
        value: 输入参数，用于确认工具 schema 可正常解析。
    返回值：工具成功结果文本。
    """
    return f"成功：{value}"


def _failed_tool(value: str) -> str:
    """模拟 MCP 工具执行过程中抛出的异常。

    参数：
        value: 输入参数；函数始终抛错。
    返回值：无；调用时抛出 ValueError。
    """
    raise ValueError(f"模拟失败：{value}")


class ToolCallResultEventTest(unittest.TestCase):
    """通过真实 LangGraphAgent 事件流验证工具调用结果事件。"""

    def test_tool_success_emits_tool_call_result(self) -> None:
        """验证成功的工具调用经 adapter 转成 TOOL_CALL_RESULT。

        参数：无。
        返回值：无；缺少或错误的结果事件会使断言失败。
        """
        asyncio.run(self._assert_tool_result(fails=False))

    def test_caught_tool_failure_emits_error_tool_call_result(self) -> None:
        """验证 MCP 异常被捕获后仍转成错误 TOOL_CALL_RESULT。

        参数：无。
        返回值：无；缺少错误结果事件会使断言失败。
        """
        asyncio.run(self._assert_tool_result(fails=True))

    async def _assert_tool_result(self, *, fails: bool) -> None:
        """运行工具节点及 AG-UI adapter，并核对产生的工具结果事件。

        参数：
            fails: 是否令模拟 MCP 工具抛出异常。
        返回值：无；事件内容不符合预期时断言失败。
        """
        tool = StructuredTool.from_function(
            _failed_tool if fails else _success_tool,
            name="sample_tool",
            description="单元测试工具",
        )

        async def tools_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
            """调用生产工具节点，保持被测处理逻辑与服务一致。

            参数：
                state: LangGraph 当前状态，含模型工具调用。
                config: LangGraph 当前运行配置，含 AG-UI callbacks。
            返回值：生产工具节点生成的消息和工作区数据。
            """
            return await agent_graph.tools_node(state, config)

        builder = StateGraph(agent_graph.AgentState)
        builder.add_node(nodes.TOOLS, tools_node)
        builder.add_edge(nodes.START, nodes.TOOLS)
        builder.add_edge(nodes.TOOLS, nodes.END)
        graph = builder.compile(checkpointer=MemorySaver())
        agent = LangGraphAgent(
            name="tool_result_test",
            graph=graph,
            emit_raw_events=False,
        )
        call = {
            "id": "call-test-1",
            "type": "function",
            "function": FunctionCall(name="sample_tool", arguments='{"value":"case"}'),
        }
        request = RunAgentInput(
            thread_id=f"thread-{fails}",
            run_id=f"run-{fails}",
            messages=[AssistantMessage(
                id="assistant-call-1",
                content="",
                tool_calls=[ToolCall(**call)],
            )],
        )

        with (
            patch.object(agent_graph, "get_operational_tool_map", new=AsyncMock(return_value={tool.name: tool})),
            patch.object(tool_execution, "get_tool_policy", return_value={
                "security": {"approvalMode": "none"},
                "mcp_server": "unit-test",
                "approval": {"inputs": []},
            }),
        ):
            events = [event async for event in agent.run(request)]

        results = [event for event in events if event.type == EventType.TOOL_CALL_RESULT]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].tool_call_id, "call-test-1")
        if fails:
            self.assertIn("Tool call failed (ValueError)", str(results[0].content))
        else:
            self.assertIn("成功：case", str(results[0].content))
        self.assertFalse(any(event.type == EventType.RUN_ERROR for event in events))


if __name__ == "__main__":
    unittest.main()
