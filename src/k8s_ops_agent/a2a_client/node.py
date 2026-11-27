"""构造调用 A2A 服务端的 LangGraph 节点。"""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from langchain_core.runnables import RunnableConfig

from k8s_ops_agent.a2a_client.client import A2AClient
from k8s_ops_agent.observability import logger


def build_a2a_client_node(
    client: A2AClient | None = None,
    *,
    payload_key: str = "a2a_payload",
    result_key: str = "a2a_response",
) -> Any:
    """构造从状态读取任务载荷并调用 A2A 服务的异步节点。

    参数：
        client: 可注入的 A2A 客户端，缺省时在节点构造阶段创建客户端。
        payload_key: LangGraph 状态中 A2A SendMessageRequest 的键名。
        result_key: 节点输出响应在状态中的键名。
    返回值：接受 state 和 config 的异步 LangGraph 节点函数。
    """
    a2a_client = client or A2AClient()

    async def a2a_client_node(
        state: Mapping[str, Any], config: RunnableConfig
    ) -> dict[str, Any]:
        """发送状态中的任务载荷并返回远端响应。

        参数：
            state: 当前 LangGraph 状态，需包含配置的 payload_key。
            config: 当前 LangGraph 运行配置，预留用于节点扩展。
        返回值：仅包含 result_key 的状态更新字典。
        """
        del config
        payload = state.get(payload_key)
        if not isinstance(payload, dict):
            raise ValueError(f"LangGraph 状态缺少字典类型的 {payload_key}")
        started = time.perf_counter()
        logger.info("A2A 节点调用开始", extra={"log_type": "business", "payload_key": payload_key})
        try:
            result = await a2a_client.send_message(payload)
            logger.info(
                "A2A 节点调用完成",
                extra={"log_type": "business", "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                       "response_keys": sorted(result.keys()), "response_present": bool(result)},
            )
            return {result_key: result}
        except Exception:
            logger.exception(
                "A2A 节点调用异常",
                extra={"log_type": "exception", "duration_ms": round((time.perf_counter() - started) * 1000, 2)},
            )
            raise

    return a2a_client_node
