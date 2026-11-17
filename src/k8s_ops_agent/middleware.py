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


def _truncate_body(
    body: bytearray, capture_truncated: bool, char_limit: int
) -> tuple[str, bool]:
    """解码并截断正文；body 为已捕获字节，capture_truncated 表示捕获不完整，返回正文和截断标识。"""
    decoded_body = body.decode("utf-8", errors="replace")
    return decoded_body[:char_limit], capture_truncated or len(decoded_body) > char_limit


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
        body_char_limit = 5000
        # UTF-8 单字符最多占用 4 字节，按此上限捕获即可完整保留 5000 个字符。
        body_capture_limit = body_char_limit * 4
        request_body = bytearray()
        response_body = bytearray()
        request_truncated = False
        response_truncated = False

        async def receive_with_body() -> dict[str, Any]:
            """读取并透传请求消息；无参数，返回原始 ASGI 消息。"""
            nonlocal request_truncated
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                remaining = body_capture_limit - len(request_body)
                request_body.extend(body[:remaining])
                request_truncated = request_truncated or len(body) > remaining
            return message

        async def send_with_status(message: dict[str, Any]) -> None:
            """记录状态及可选正文；message 为原始响应消息，无返回值。"""
            nonlocal status, response_truncated
            if message["type"] == "http.response.start":
                status = message["status"]
            if log_payload and message["type"] == "http.response.body":
                body = message.get("body", b"")
                remaining = body_capture_limit - len(response_body)
                response_body.extend(body[:remaining])
                response_truncated = response_truncated or len(body) > remaining
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
                    request_body_text, request_truncated = _truncate_body(
                        request_body, request_truncated, body_char_limit
                    )
                    response_body_text, response_truncated = _truncate_body(
                        response_body, response_truncated, body_char_limit
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
                    },
                )
