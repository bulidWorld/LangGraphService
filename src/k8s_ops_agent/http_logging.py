"""HTTP 请求日志辅助方法。"""

from __future__ import annotations

import json
import os
from typing import Any

_HTTP_LOG_BODY_LIMIT = 20000


def payload_logging_enabled() -> bool:
    """读取 HTTP 正文日志开关。

    参数：无。
    返回值：仅当 HTTP_LOG_PAYLOAD_ENABLED 为 true 时返回 True。
    """
    return os.getenv("HTTP_LOG_PAYLOAD_ENABLED", "false").strip().lower() == "true"


def serialize_log_body(value: Any) -> tuple[str, bool]:
    """将请求或响应对象序列化为限长日志文本。

    参数：
        value: 请求参数或响应对象。
    返回值：JSON 文本及是否发生截断。
    """
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= _HTTP_LOG_BODY_LIMIT:
        return text, False
    return text[:_HTTP_LOG_BODY_LIMIT], True


def payload_log_fields(request: Any, response: Any = None) -> dict[str, Any]:
    """按配置生成出站请求与响应正文日志字段。

    参数：
        request: 请求参数或请求正文。
        response: 响应对象；尚未收到响应时传入 None。
    返回值：正文日志字段；关闭开关时返回空字典。
    """
    if not payload_logging_enabled():
        return {}

    request_body, request_truncated = serialize_log_body(request)
    fields: dict[str, Any] = {
        "request_body": request_body,
        "request_body_truncated": request_truncated,
    }
    if response is not None:
        response_body, response_truncated = serialize_log_body(response)
        fields.update({
            "response_body": response_body,
            "response_body_truncated": response_truncated,
        })
    return fields


def response_log_fields(response: Any) -> dict[str, Any]:
    """按配置生成出站响应正文日志字段。

    参数：
        response: 响应对象。
    返回值：响应正文日志字段；关闭开关时返回空字典。
    """
    if not payload_logging_enabled():
        return {}
    response_body, response_truncated = serialize_log_body(response)
    return {
        "response_body": response_body,
        "response_body_truncated": response_truncated,
    }
