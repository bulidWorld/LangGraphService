"""通过 A2A JSON-RPC 协议调用远端 Agent。"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import httpx


logger = logging.getLogger("k8s_ops_agent.a2a_client")


class A2AClientError(RuntimeError):
    """A2A 请求或响应校验失败。"""


class A2AClient:
    """通过 HTTP+JSON 发送 A2A JSON-RPC 请求。"""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """初始化客户端。

        参数：
            endpoint: 服务端 JSON-RPC 地址，缺省读取 A2A_ENDPOINT。
            timeout: 请求超时秒数，缺省读取 A2A_TIMEOUT_SECONDS。
            headers: 附加 HTTP 请求头，缺省读取 A2A_API_KEY 环境变量。
        返回值：无。
        """
        self.endpoint = (endpoint or os.getenv("A2A_ENDPOINT", "")).strip()
        self.timeout = timeout or float(os.getenv("A2A_TIMEOUT_SECONDS", "60"))
        self.headers = dict(headers or {})
        api_key = os.getenv("A2A_API_KEY")
        if api_key and "Authorization" not in self.headers:
            self.headers["Authorization"] = f"Bearer {api_key}"
        if not self.endpoint:
            raise ValueError("A2A_ENDPOINT 未配置")

    async def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        """向 A2A 服务端发送任务消息并提取 JSON-RPC 结果。

        参数：
            payload: A2A SendMessageRequest 对象，必须包含 message。
        返回值：服务端 JSON-RPC result 对象。
        """
        if not isinstance(payload.get("message"), dict):
            raise ValueError("A2A 请求 payload 必须包含 message 对象")

        request_id = str(uuid.uuid4())
        request_body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "message/send",
            "params": payload,
        }
        started = time.perf_counter()
        status = 500
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint,
                    json=request_body,
                    headers=self.headers,
                )
            status = response.status_code
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise A2AClientError("A2A 服务端返回的 JSON-RPC 响应不是对象")
            if body.get("id") != request_id:
                raise A2AClientError("A2A 服务端响应 id 与请求不匹配")
            if body.get("error") is not None:
                raise A2AClientError(f"A2A 服务端返回错误：{body['error']}")
            result = body.get("result")
            if not isinstance(result, dict):
                raise A2AClientError("A2A 服务端响应缺少 result 对象")
            return result
        except Exception:
            logger.exception(
                "A2A 请求失败",
                extra={"log_type": "exception", "endpoint": self.endpoint},
            )
            raise
        finally:
            logger.info(
                "A2A 外部请求完成",
                extra={
                    "log_type": "http",
                    "method": "POST",
                    "path": self.endpoint,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
