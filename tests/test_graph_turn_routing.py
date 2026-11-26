"""验证主图从 checkpoint 续接已审批的编码会话。"""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from google.protobuf.json_format import MessageToDict
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from k8s_ops_agent import graph as agent_graph
from k8s_ops_agent.a2a_client.client import A2AClient
from k8s_ops_agent.turn_routing import TurnRouting


class GraphTurnRoutingTest(unittest.IsolatedAsyncioTestCase):
    """用本地 checkpoint 和模拟 A2A 服务验证跨轮路由。"""

    async def test_follow_up_reuses_remote_context_and_new_issue_runs_diagnosis(self) -> None:
        """验证续聊绕过定位，新的故障仍进入诊断流程。

        参数：无。
        返回值：无；路由或增量请求不符合预期时断言失败。
        """
        request = "上一轮建议的重试能用指数退避吗？"
        coding = {"type": "coding", "repository": "https://example.org/repo.git", "branch": "v1"}
        route = AsyncMock(side_effect=[
            TurnRouting(continue_coding=True),
            TurnRouting(continue_coding=False),
        ])
        diagnosis = AsyncMock(return_value={"messages": [AIMessage(content="重新定位")]})
        assessment = AsyncMock(return_value=None)
        config = {"configurable": {"thread_id": "coding-thread"}}

        async def remote_events(payload):
            """校验实际 SDK 可解析增量请求并模拟远端任务完成。

            参数：
                payload: 主图续聊时发出的 A2A 请求。
            返回值：远端任务快照的异步迭代器。
            """
            message = A2AClient._message_request(payload).message
            self.assertEqual(message.context_id, "remote-context")
            self.assertEqual(message.task_id, "")
            self.assertNotEqual(message.message_id, "initial-message")
            self.assertEqual(MessageToDict(message.parts[0].data), {**coding, "instruction": request})
            yield {"task": {"id": "new-task", "contextId": "remote-context",
                            "status": {"state": "TASK_STATE_COMPLETED"}}}

        client = Mock(send_streaming_message=Mock(side_effect=remote_events))
        subgraph = agent_graph.build_a2a_task_graph(client)
        with (
            patch.object(agent_graph, "build_a2a_task_graph", return_value=subgraph),
            patch.object(agent_graph, "_model", return_value=Mock(
                with_structured_output=Mock(return_value=Mock(ainvoke=route)),
            )),
            patch.object(agent_graph, "assistant_node", diagnosis),
            patch.object(agent_graph, "extract_pod_parameters", assessment),
        ):
            graph = agent_graph.build_graph(checkpointer=MemorySaver())
            await graph.aupdate_state(config, {
                "messages": [HumanMessage(content="定位故障"), AIMessage(content="诊断结果")],
                "a2a_payload": {"message": {
                    "messageId": "initial-message",
                    "parts": [{"data": {**coding, "instruction": "建议调整重试"}}],
                }},
                "a2a_events": [{"statusUpdate": {"contextId": "remote-context"}}],
                "a2a_task_id": "completed-task",
                "a2a_status": {"state": "TASK_STATE_COMPLETED"},
            }, as_node="a2a")

            result = await graph.ainvoke({"messages": [HumanMessage(content=request)]}, config)
            self.assertEqual(result["a2a_task_id"], "new-task")
            diagnosis.assert_not_awaited()
            assessment.assert_not_awaited()

            await graph.ainvoke({"messages": [HumanMessage(content="检查另一个 Pod")]}, config)
            diagnosis.assert_awaited_once()
            assessment.assert_awaited_once()
            client.send_streaming_message.assert_called_once()
