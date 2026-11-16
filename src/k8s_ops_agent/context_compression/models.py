"""上下文压缩模块使用的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage


@dataclass(frozen=True)
class CompressionConfig:
    """上下文压缩策略配置。"""

    enabled: bool
    context_window_tokens: int
    keep_recent_messages: int
    fallback_turn_threshold: int = 200


@dataclass(frozen=True)
class CompressionResult:
    """压缩后的消息视图及不含正文的统计信息。"""

    messages: list[BaseMessage]
    compressed: bool
    input_tokens_before: int | None
    input_tokens_after: int | None
    input_token_budget: int
    summarized_message_count: int
    summary_model: str | None
    warning_code: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        """将压缩结果转换为可放入 LangGraph 状态的统计字典。

        参数：
            self: 当前压缩结果。
        返回值：不包含消息正文或摘要正文的压缩统计信息。
        """
        return {
            "compressed": self.compressed,
            "input_tokens_before": self.input_tokens_before,
            "input_tokens_after": self.input_tokens_after,
            "input_token_budget": self.input_token_budget,
            "summarized_message_count": self.summarized_message_count,
            "summary_model": self.summary_model,
            "warning_code": self.warning_code,
        }
