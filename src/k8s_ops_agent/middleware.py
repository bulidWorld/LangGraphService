"""记录请求访问日志并维护 Langfuse trace 上下文的 ASGI 中间件。"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import parse_qsl

from k8s_ops_agent.observability import (
    logger,
    new_trace_id,
    request_observation,
    update_observation,
)

_TRUNCATION_MARKER = "................................................................................."


def _truncate_body(
    body: bytearray, capture_truncated: bool, char_limit: int
) -> tuple[str, bool]:
    """截取正文首尾并以标记连接。

    参数：
        body: 已捕获的 UTF-8 正文数据。
        capture_truncated: 捕获阶段是否已发现正文超出上限。
        char_limit: 输出正文的最大字符数，包含中间标记。
    返回值：截取后的正文及是否发生截断。
    """
    decoded_body = body.decode("utf-8", errors="replace")
    truncated = capture_truncated or len(decoded_body) > char_limit
    if not truncated:
        return decoded_body, False

    half_length = max(0, (char_limit - len(_TRUNCATION_MARKER)) // 2)
    if half_length == 0:
        return _TRUNCATION_MARKER[:char_limit], True
    return (
        decoded_body[:half_length]
        + _TRUNCATION_MARKER
        + decoded_body[-half_length:],
        True,
    )


def _append_tail(tail: bytearray, body: bytes, byte_limit: int) -> None:
    """更新尾部字节缓存并限制其大小。

    参数：
        tail: 用于保存正文尾部的可变字节缓存。
        body: 新到达的正文片段。
        byte_limit: 尾部缓存的最大字节数。
    返回值：无；缓存仅保留最新的正文尾部。
    """
    tail.extend(body)
    if len(tail) > byte_limit:
        del tail[:-byte_limit]


def _decode_utf8_tail(body: bytearray) -> str:
    """解码尾部缓存并丢弃窗口起始处不完整的 UTF-8 字符。

    参数：
        body: 可能从多字节字符中间开始的 UTF-8 字节窗口。
    返回值：有效 UTF-8 尾部字符串。
    """
    return body.decode("utf-8", errors="ignore")


def _format_captured_body(
    head: bytearray,
    tail: bytearray,
    truncated: bool,
    char_limit: int,
) -> str:
    """按长度限制组装正文日志字段。

    参数：
        head: 正文开头的字节缓存。
        tail: 正文末尾的字节缓存。
        truncated: 正文是否超出捕获范围。
        char_limit: 输出正文的最大字符数。
    返回值：完整正文或首尾截取后的正文。
    """
    decoded_head = head.decode("utf-8", errors="ignore")
    if not truncated:
        return decoded_head
    half_length = (char_limit - len(_TRUNCATION_MARKER)) // 2
    decoded_tail = _decode_utf8_tail(tail)
    return (
        decoded_head[:half_length]
        + _TRUNCATION_MARKER
        + decoded_tail[-half_length:]
    )


class AccessLogMiddleware:
    def __init__(self, app: Any) -> None:
        """初始化中间件；app 为下游 ASGI 应用，无返回值。"""
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """处理请求；scope 为上下文，receive/send 为消息通道，无返回值。"""
        # 只有 HTTP scope 包含请求和响应元数据；lifespan、websocket 等流量直接透传。
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        trace_id = new_trace_id()
        started = time.perf_counter()
        status = 500
        log_payload = os.getenv("HTTP_LOG_PAYLOAD_ENABLED", "false").strip().lower() == "true"
        request_content_length = next(
            (
                value.decode("latin-1")
                for key, value in scope.get("headers", [])
                if key.lower() == b"content-length"
            ),
            None,
        )
        body_char_limit = 20000
        body_half_limit = (body_char_limit - len(_TRUNCATION_MARKER)) // 2
        tail_byte_limit = body_half_limit * 4
        request_body = bytearray()
        response_body = bytearray()
        request_body_tail = bytearray()
        response_body_tail = bytearray()
        request_body_length = 0
        response_body_length = 0
        request_truncated = False
        response_truncated = False

        async def receive_with_body() -> dict[str, Any]:
            """读取并透传请求消息；无参数，返回原始 ASGI 消息。"""
            nonlocal request_truncated, request_body_length
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                request_body_length += len(body)
                request_head_remaining = max(0, tail_byte_limit - len(request_body))
                request_body.extend(body[:request_head_remaining])
                _append_tail(request_body_tail, body, tail_byte_limit)
                request_truncated = request_truncated or request_body_length > tail_byte_limit * 2
            return message

        async def send_with_status(message: dict[str, Any]) -> None:
            """记录状态及可选正文；message 为原始响应消息，无返回值。"""
            nonlocal status, response_truncated, response_body_length
            if message["type"] == "http.response.start":
                status = message["status"]
            if log_payload and message["type"] == "http.response.body":
                body = message.get("body", b"")
                response_body_length += len(body)
                response_head_remaining = max(0, tail_byte_limit - len(response_body))
                response_body.extend(body[:response_head_remaining])
                _append_tail(response_body_tail, body, tail_byte_limit)
                response_truncated = response_truncated or response_body_length > tail_byte_limit * 2
            await send(message)

        try:
            async with request_observation(method, path, trace_id) as observation:
                await self.app(
                    scope, receive_with_body if log_payload else receive, send_with_status
                )
                update_observation(observation, output={"status": status})
        except Exception:
            logger.exception(
                "Unhandled request exception",
                extra={"log_type": "exception", "trace_id": trace_id},
            )
            raise
        finally:
            if path != "/health":
                payload_fields = {}
                if log_payload:
                    request_body_text = _format_captured_body(
                        request_body, request_body_tail, request_truncated, body_char_limit
                    )
                    response_body_text = _format_captured_body(
                        response_body, response_body_tail, response_truncated, body_char_limit
                    )
                    payload_fields = {
                        "request_query": parse_qsl(
                            scope.get("query_string", b"").decode("utf-8", errors="replace"),
                            keep_blank_values=True,
                        ),
                        "request_body": request_body_text,
                        "response_body": response_body_text,
                        "request_body_truncated": request_truncated,
                        "response_body_truncated": response_truncated,
                    }
                logger.info(
                    "HTTP request completed",
                    extra={
                        **payload_fields,
                        "log_type": "access",
                        "trace_id": trace_id,
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        "request_content_length": request_content_length,
                        "request_body_bytes_observed": request_body_length,
                        "request_body_capture_truncated": request_truncated,
                    },
                )
