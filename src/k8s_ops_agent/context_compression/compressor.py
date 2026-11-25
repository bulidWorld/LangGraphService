"""按模型接口用量触发历史消息摘要。"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI

from k8s_ops_agent.context_compression.models import (
    CompressionConfig,
    CompressionResult,
)
from k8s_ops_agent.prompts import CONTEXT_SUMMARY_PROMPT, CONVERSATION_SUMMARY_TEMPLATE


logger = logging.getLogger("k8s_ops_agent.context_compression")


class ContextBudgetExceededError(RuntimeError):
    """usage 达到阈值但没有可安全压缩的历史消息时抛出的错误。"""


@dataclass(frozen=True)
class _MessageGroup:
    """不能拆分的消息组及其是否允许摘要。"""

    messages: list[BaseMessage]
    complete: bool
    summarizable: bool


class ContextCompressor:
    """按模型接口返回的 token 用量压缩历史消息。"""

    def __init__(self, config: CompressionConfig):
        """创建上下文压缩器。

        参数：
            config: 上下文窗口和近期消息保留策略配置。
        返回值：无。
        """
        self.config = config
        self.model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self._summary_model: ChatOpenAI | None = None

    @classmethod
    def from_env(cls) -> ContextCompressor:
        """从环境变量读取压缩策略并创建压缩器。

        参数：
            cls: 压缩器类型。
        返回值：使用环境配置创建的压缩器。
        """
        enabled = os.getenv("CONTEXT_COMPRESSION_ENABLED", "true").lower() in {
            "1", "true", "yes", "on"
        }
        context_window_tokens = int(os.getenv("CONTEXT_WINDOW_TOKENS", "128000"))
        keep_recent_messages = int(os.getenv("CONTEXT_KEEP_RECENT_MESSAGES", "12"))
        fallback_turn_threshold = int(
            os.getenv("CONTEXT_COMPRESSION_FALLBACK_TURNS", "200")
        )
        if context_window_tokens < 1024:
            raise ValueError("CONTEXT_WINDOW_TOKENS must be at least 1024")
        if keep_recent_messages < 1:
            raise ValueError("CONTEXT_KEEP_RECENT_MESSAGES must be at least 1")
        if fallback_turn_threshold < 1:
            raise ValueError("CONTEXT_COMPRESSION_FALLBACK_TURNS must be at least 1")
        return cls(
            CompressionConfig(
                enabled=enabled,
                context_window_tokens=context_window_tokens,
                keep_recent_messages=keep_recent_messages,
                fallback_turn_threshold=fallback_turn_threshold,
            )
        )

    async def compress(
        self, messages: Sequence[BaseMessage]
    ) -> CompressionResult:
        """生成符合模型预算的消息视图，不修改传入消息。

        参数：
            self: 当前压缩器。
            messages: 已含系统提示词和当前请求的 LangChain 消息序列。
        返回值：压缩消息、接口用量和压缩状态等统计数据。
        """
        original = list(messages)
        before = self._latest_input_usage(original)
        threshold = int(self.config.context_window_tokens * 0.70)
        fallback_turns = sum(
            isinstance(message, HumanMessage) for message in original
        )
        threshold_reached = (
            before >= threshold
            if before is not None
            else fallback_turns >= self.config.fallback_turn_threshold
        )
        should_compress = self.config.enabled and threshold_reached
        if not self.config.enabled:
            decision_reason = "compression_disabled"
        elif before is not None and threshold_reached:
            decision_reason = "usage_threshold_reached"
        elif before is not None:
            decision_reason = "usage_below_threshold"
        elif threshold_reached:
            decision_reason = "fallback_turn_threshold_reached"
        else:
            decision_reason = "fallback_turns_below_threshold"
        logger.info(
            "Context compression decision evaluated",
            extra={
                "log_type": "business",
                "compression_enabled": self.config.enabled,
                "usage_available": before is not None,
                "input_tokens_before": before,
                "context_window_tokens": self.config.context_window_tokens,
                "usage_threshold_ratio": 0.70,
                "usage_threshold_tokens": threshold,
                "fallback_turns": fallback_turns,
                "fallback_turn_threshold": self.config.fallback_turn_threshold,
                "decision_reason": decision_reason,
                "should_compress": should_compress,
            },
        )
        if not should_compress:
            return CompressionResult(
                messages=original,
                compressed=False,
                input_tokens_before=before,
                input_tokens_after=None,
                input_token_budget=threshold,
                summarized_message_count=0,
                summary_model=None,
            )

        system_messages, groups = self._group_messages(original)
        eligible_count = 0
        for group in groups:
            if not group.complete or not group.summarizable:
                break
            eligible_count += 1
        if eligible_count == 0:
            raise ContextBudgetExceededError(
                "context_budget_exceeded: no complete historical messages can be summarized"
            )

        keep_count = min(self.config.keep_recent_messages, len(groups))
        first_summarize_count = min(eligible_count, max(0, len(groups) - keep_count))
        summarize_count = first_summarize_count or eligible_count
        summarized_groups = groups[:summarize_count]
        remaining_groups = groups[summarize_count:]
        summary_text = await self._summarize(summarized_groups)
        summary_message = SystemMessage(
            content=CONVERSATION_SUMMARY_TEMPLATE.format(summary=summary_text)
        )
        candidate = [
            *system_messages,
            summary_message,
            *(message for group in remaining_groups for message in group.messages),
        ]
        summarized_message_count = sum(
            len(group.messages) for group in summarized_groups
        )
        return CompressionResult(
            messages=candidate,
            compressed=True,
            input_tokens_before=before,
            input_tokens_after=None,
            input_token_budget=threshold,
            summarized_message_count=summarized_message_count,
            summary_model=self.model_name,
        )

    def _latest_input_usage(self, messages: Sequence[BaseMessage]) -> int | None:
        """读取最近一次模型响应返回的输入 token 用量。

        参数：
            self: 当前压缩器。
            messages: 原始消息序列。
        返回值：接口返回的输入 token 数；没有用量信息时返回 None。
        """
        for message in reversed(messages):
            if not isinstance(message, AIMessage):
                continue
            usage = getattr(message, "usage_metadata", None) or {}
            provider_usage = (message.response_metadata or {}).get("token_usage", {})
            for value in (
                usage.get("input_tokens"),
                provider_usage.get("prompt_tokens"),
                provider_usage.get("input_tokens"),
            ):
                if isinstance(value, int) and value >= 0:
                    return value
            return None
        return None

    def _group_messages(
        self, messages: Sequence[BaseMessage]
    ) -> tuple[list[BaseMessage], list[_MessageGroup]]:
        """按工具调用关联关系将历史消息分组。

        参数：
            self: 当前压缩器。
            messages: 完整消息序列。
        返回值：系统消息和按顺序排列的非系统消息组。
        """
        system_messages = [message for message in messages if message.type == "system"]
        conversational = [message for message in messages if message.type != "system"]
        groups: list[_MessageGroup] = []
        index = 0
        while index < len(conversational):
            current = conversational[index]
            if isinstance(current, AIMessage) and current.tool_calls:
                group_messages = [current]
                expected_ids = {
                    str(call.get("id"))
                    for call in current.tool_calls
                    if call.get("id")
                }
                found_ids: set[str] = set()
                index += 1
                while index < len(conversational) and expected_ids - found_ids:
                    next_message = conversational[index]
                    group_messages.append(next_message)
                    if isinstance(next_message, ToolMessage):
                        found_ids.add(str(next_message.tool_call_id))
                    index += 1
                complete = bool(expected_ids) and expected_ids <= found_ids
                groups.append(
                    _MessageGroup(
                        messages=group_messages,
                        complete=complete,
                        summarizable=complete
                        and all(self._is_text_message(item) for item in group_messages),
                    )
                )
                continue
            groups.append(
                _MessageGroup(
                    messages=[current],
                    complete=True,
                    summarizable=self._is_text_message(current),
                )
            )
            index += 1

        # 当前用户请求以及它之后的内容始终保留，不进入历史摘要区。
        latest_user_group = max(
            (
                group_index
                for group_index, group in enumerate(groups)
                if any(isinstance(item, HumanMessage) for item in group.messages)
            ),
            default=-1,
        )
        if latest_user_group >= 0:
            groups = [
                _MessageGroup(
                    messages=group.messages,
                    complete=group.complete,
                    summarizable=group.summarizable and group_index < latest_user_group,
                )
                for group_index, group in enumerate(groups)
            ]
        return system_messages, groups

    def _is_text_message(self, message: BaseMessage) -> bool:
        """判断消息是否为可安全发送给摘要模型的纯文本。

        参数：
            self: 当前压缩器。
            message: 待检查的 LangChain 消息。
        返回值：消息正文和工具参数均为文本结构时返回 true。
        """
        if not isinstance(message.content, str):
            return False
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                try:
                    json.dumps(call.get("args", {}), ensure_ascii=False)
                except (TypeError, ValueError):
                    return False
        return True

    async def _summarize(self, groups: Sequence[_MessageGroup]) -> str:
        """使用主模型压缩早期完整对话片段。

        参数：
            self: 当前压缩器。
            groups: 可完整压缩的历史消息组。
        返回值：仅依据输入证据生成的中文摘要。
        """
        if self._summary_model is None:
            self._summary_model = ChatOpenAI(
                model=self.model_name,
                temperature=0,
                max_tokens=1000,
                use_responses_api=True,
                reasoning={"effort": "medium", "summary": "auto"},
            )
        transcript = self._serialize_groups(groups)
        started = time.perf_counter()
        try:
            response = await self._summary_model.ainvoke(
                [
                    SystemMessage(content=CONTEXT_SUMMARY_PROMPT),
                    HumanMessage(content=transcript),
                ]
            )
            logger.info(
                "Context summary request completed",
                extra={
                    "log_type": "http",
                    "method": "POST",
                    "path": "openai.chat.completions.context_summary",
                    "status": 200,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "model": self.model_name,
                },
            )
        except Exception:
            logger.error(
                "Context summary request failed",
                extra={
                    "log_type": "http",
                    "method": "POST",
                    "path": "openai.chat.completions.context_summary",
                    "status": 500,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "model": self.model_name,
                },
            )
            raise
        if not isinstance(response.content, str) or not response.content.strip():
            raise ContextBudgetExceededError(
                "context_summary_failed: summary model returned empty content"
            )
        return response.content.strip()

    def _serialize_groups(self, groups: Sequence[_MessageGroup]) -> str:
        """将完整历史消息组转换为摘要模型输入文本。

        参数：
            self: 当前压缩器。
            groups: 待转换的消息组。
        返回值：保留角色、工具名称和调用参数的 JSON 文本。
        """
        transcript: list[dict[str, Any]] = []
        for group in groups:
            for message in group.messages:
                entry: dict[str, Any] = {
                    "role": message.type,
                    "content": message.content,
                }
                if isinstance(message, AIMessage) and message.tool_calls:
                    entry["tool_calls"] = message.tool_calls
                if isinstance(message, ToolMessage):
                    entry["tool_call_id"] = message.tool_call_id
                    entry["tool_name"] = message.name
                transcript.append(entry)
        return json.dumps(transcript, ensure_ascii=False, default=str)
