"""将压缩器包装为可插入 LangGraph 的独立节点。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from k8s_ops_agent.context_compression.compressor import ContextCompressor
from k8s_ops_agent.prompts import build_workspace_files_prompt


logger = logging.getLogger("k8s_ops_agent.context_compression")
_compressor: ContextCompressor | None = None


def _default_compressor() -> ContextCompressor:
    """惰性创建进程级压缩器实例。

    参数：
        无。
    返回值：按当前进程环境配置创建或复用的上下文压缩器。
    """
    global _compressor
    if _compressor is None:
        _compressor = ContextCompressor.from_env()
    return _compressor


def build_context_compression_node(
    prompt_builder: Callable[[list[BaseMessage], str], list[BaseMessage]],
) -> Callable[[Mapping[str, Any], RunnableConfig], Any]:
    """构造生成本轮模型上下文视图的 LangGraph 节点。

    参数：
        prompt_builder: 服务注入的系统提示词和消息准备函数。
    返回值：读取原始消息并输出 context_messages 的异步图节点。
    """

    async def context_compression_node(
        state: Mapping[str, Any], config: RunnableConfig
    ) -> dict[str, Any]:
        """生成压缩消息视图并记录不含正文的运行指标。

        参数：
            state: LangGraph 当前状态，包含原始 messages。
            config: 当前 LangGraph 运行配置，包含 thread_id。
        返回值：供 assistant 节点读取的消息视图和压缩统计信息。
        """
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        prompt_messages = prompt_builder(list(state.get("messages", [])), thread_id)
        files = state.get("workspace_files", [])
        if files:
            prompt_messages.append(
                SystemMessage(content=build_workspace_files_prompt(files))
            )
        try:
            result = await _default_compressor().compress(prompt_messages)
        except Exception:
            logger.exception(
                "Context compression failed",
                extra={"log_type": "exception", "thread_id": thread_id},
            )
            raise
        logger.info(
            "Context compression completed",
            extra={
                "log_type": "business",
                "thread_id": thread_id,
                **result.to_metadata(),
            },
        )
        return {
            "context_messages": result.messages,
            "compression_metadata": result.to_metadata(),
        }

    return context_compression_node
