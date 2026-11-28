"""验证主图从 checkpoint 续接已审批的编码会话。"""

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ag_ui.core.types import RunAgentInput, UserMessage
from ag_ui.encoder import EventEncoder
from ag_ui_langgraph import LangGraphAgent
from google.protobuf.json_format import MessageToDict
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import MemorySaver

from k8s_ops_agent import graph as agent_graph
from k8s_ops_agent import graph_node_constants as nodes
from k8s_ops_agent.a2a_client.client import A2AClient
from k8s_ops_agent.activity.events import event_generator
from k8s_ops_agent.turn_routing import TurnRouting


class GraphTurnRoutingTest(unittest.IsolatedAsyncioTestCase):
    """用本地 checkpoint 和模拟 A2A 服务验证跨轮路由。"""

    async def test_a2a_reply_or_fallback_in_agui_stream(self) -> None:
        """验证 A2A 无内容时回复用户，有内容时跳过兜底，两种情况均隐藏路由 JSON。

        参数：无。
        返回值：无；事件流或持久化回复不符合预期时断言失败。
        """
        decision_text = '{"continue_coding":true}'
        fallback_text = "根据之前的诊断，可以先验证重试设置。"

        async def classify(messages):
            """模拟会流式输出 JSON 的意图分类模型。

            参数：
                messages: 路由提示消息。
            返回值：解析后的分类结果。
            """
            response = await FakeListChatModel(responses=[decision_text]).ainvoke(messages)
            return TurnRouting.model_validate_json(response.text)

        cases = {
            "empty": ([], True),
            "status_only": (
                [{"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}], True
            ),
            "text": ([{"message": {"parts": [{"text": "远端答复"}]}}], False),
            "artifact": ([{"artifactUpdate": {"artifact": {"parts": [{"data": {"value": 1}}]}}}], False),
        }
        for case, (events, expects_fallback) in cases.items():
            with self.subTest(case=case):
                async def remote_events(payload):
                    """发送本用例的模拟远端事件。

                    参数：
                        payload: 发送给远端的消息请求。
                    返回值：远端事件的异步迭代器。
                    """
                    for event in events:
                        yield event

                client = Mock(send_streaming_message=Mock(side_effect=remote_events))
                subgraph = agent_graph.build_a2a_task_graph(client)
                model_factory = Mock(side_effect=[
                    Mock(with_structured_output=Mock(return_value=RunnableLambda(classify))),
                    FakeListChatModel(responses=[fallback_text]),
                ])
                config = {"configurable": {"thread_id": case}}
                request = RunAgentInput(
                    thread_id=case, run_id=f"run-{case}",
                    messages=[UserMessage(id="current", content="继续处理。")],
                )
                with (
                    patch.object(agent_graph, "build_a2a_task_graph", return_value=subgraph),
                    patch.object(agent_graph, "_model", model_factory),
                ):
                    graph = agent_graph.build_graph(checkpointer=MemorySaver())
                    await graph.aupdate_state(config, {
                        "messages": [AIMessage(id="previous", content="之前的诊断")],
                        "a2a_task": {"contextId": "remote-context"},
                        "a2a_coding": {
                            "type": "coding", "repository": "https://example.org/repo.git",
                            "branch": "main", "tag": "v1",
                        },
                    }, as_node=nodes.A2A)
                    agent = LangGraphAgent(name="test_a2a", graph=graph, emit_raw_events=True)
                    outputs = [
                        json.loads(line.removeprefix("data: "))
                        async for line in event_generator(request, request, agent, EventEncoder())
                    ]
                    state = await graph.aget_state(config)
                self.assertNotIn("continue_coding", json.dumps(outputs))
                self.assertEqual(outputs[-1]["type"], "RUN_FINISHED")
                self.assertEqual(model_factory.call_count, 2 if expects_fallback else 1)
                if expects_fallback:
                    self.assertEqual(outputs[-1]["result"], fallback_text)
                    self.assertEqual(state.values["messages"][-1].text, fallback_text)
                else:
                    self.assertIsNone(outputs[-1].get("result"))

    async def test_follow_up_reuses_remote_context_and_new_issue_runs_diagnosis(self) -> None:
        """验证续聊绕过定位，新的故障仍进入诊断流程。

        参数：无。
        返回值：无；路由或增量请求不符合预期时断言失败。
        """
        request = "上一轮建议的重试能用指数退避吗？"
        explicit_request = " 继续处理。 "
        coding = {
            "type": "coding", "repository": "https://example.org/repo.git",
            "branch": "main", "tag": "v1",
        }
        route = AsyncMock(side_effect=[
            TurnRouting(continue_coding=True),
            TurnRouting(continue_coding=True),
            TurnRouting(continue_coding=False),
        ])
        diagnosis = AsyncMock(return_value={"messages": [AIMessage(content="重新定位")]})
        assessment = AsyncMock(return_value=None)
        config = {"configurable": {"thread_id": "coding-thread"}}

        requests = iter([(explicit_request, ""), (request, "new-task")])

        async def remote_events(payload):
            """校验实际 SDK 可解析增量请求并模拟远端任务完成。

            参数：
                payload: 主图续聊时发出的 A2A 请求。
            返回值：远端任务快照的异步迭代器。
            """
            message = A2AClient._message_request(payload).message
            expected_text, expected_task_id = next(requests)
            self.assertEqual(message.context_id, "remote-context")
            self.assertEqual(message.task_id, expected_task_id)
            self.assertNotEqual(message.message_id, "initial-message")
            self.assertEqual(MessageToDict(message.parts[0]), {"text": expected_text})
            self.assertEqual(len(message.parts), 1)
            yield {"task": {"id": "new-task", "status": {"state": "TASK_STATE_INPUT_REQUIRED"}}}

        client = Mock(send_streaming_message=Mock(side_effect=remote_events))
        subgraph = agent_graph.build_a2a_task_graph(client)
        with (
            patch.object(agent_graph, "build_a2a_task_graph", return_value=subgraph),
            patch.object(agent_graph, "_model", return_value=Mock(
                ainvoke=AsyncMock(return_value=AIMessage(content="诊断总结")),
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
            }, as_node=nodes.A2A)

            result = await graph.ainvoke({"messages": [HumanMessage(content=explicit_request)]}, config)
            self.assertEqual(result["a2a_task_id"], "new-task")
            route.assert_awaited_once()
            await graph.ainvoke({"messages": [HumanMessage(content=request)]}, config)
            diagnosis.assert_not_awaited()
            assessment.assert_not_awaited()

            await graph.ainvoke({"messages": [HumanMessage(content="检查另一个 Pod")]}, config)
            diagnosis.assert_awaited_once()
            assessment.assert_awaited_once()
            self.assertEqual(route.await_count, 3)
            self.assertEqual(client.send_streaming_message.call_count, 2)
