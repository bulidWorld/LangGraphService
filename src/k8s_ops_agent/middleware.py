"""记录请求访问日志并维护 Langfuse trace 上下文的 ASGI 中间件。"""

from __future__ import annotations

import time
from typing import Any

from k8s_ops_agent.observability import (
    logger,
    new_trace_id,
    request_observation,
    update_observation,
)


class AccessLogMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # 只有 HTTP scope 包含请求和响应元数据；lifespan、websocket 等流量直接透传。
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        trace_id = new_trace_id()
        started = time.perf_counter()
        status = 500

        async def send_with_status(message: dict[str, Any]) -> None:
            # 捕获 ASGI 实际响应状态码，不缓存或改变响应正文。
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            async with request_observation(method, path, trace_id) as observation:
                await self.app(scope, receive, send_with_status)
                update_observation(observation, output={"status": status})
        except Exception:
            logger.exception(
                "Unhandled request exception",
                extra={"log_type": "exception", "trace_id": trace_id},
            )
            raise
        finally:
            if path != "/health":
                logger.info(
                    "HTTP request completed",
                    extra={
                        "log_type": "access",
                        "trace_id": trace_id,
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
