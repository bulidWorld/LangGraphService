"""通过官方 SDK 调用远端 A2A 1.0 JSON-RPC 服务。"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager
from typing import Any

import httpx
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import AgentCard, SendMessageRequest, SubscribeToTaskRequest
from a2a.utils.errors import A2AError
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

from k8s_ops_agent.http_logging import payload_log_fields, response_log_fields
from k8s_ops_agent.observability import current_trace_id, logger


class A2AClientError(RuntimeError):
    """A2A 请求或响应校验失败。"""


class A2AClient:
    """将官方 SDK 的 A2A 消息转换为工作流使用的字典。"""

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
            headers: 附加 HTTP 请求头。未提供 Authorization 时读取 A2A_API_TOKEN。
        返回值：无。
        """
        self.endpoint = (endpoint or os.getenv("A2A_ENDPOINT", "")).strip()
        self.timeout = timeout or float(os.getenv("A2A_TIMEOUT_SECONDS", "60"))
        self.headers = httpx.Headers(headers)
        self.headers["Content-Type"] = "application/json"
        self.headers["A2A-Version"] = "1.0"
        api_token = os.getenv("A2A_API_TOKEN")
        if api_token and "Authorization" not in self.headers:
            self.headers["Authorization"] = f"Bearer {api_token}"
        if not self.endpoint:
            raise ValueError("A2A_ENDPOINT 未配置")

    async def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        """向远端发送非流式消息。

        参数：
            payload: A2A SendMessageRequest 字典，必须包含 message。
        返回值：由 SDK 响应转换得到的 A2A JSON 字典。
        """
        request = self._message_request(payload)
        async with self._transport("SendMessage") as transport:
            request_fields = payload_log_fields(payload)
            logger.info(
                "A2A 请求已发送",
                extra={"log_type": "business", "rpc_method": "SendMessage", **request_fields},
            )
            response = await transport.send_message(request)
            result = MessageToDict(response)
            logger.info(
                "A2A 响应已解析",
                extra={"log_type": "business", "rpc_method": "SendMessage", **_response_summary(result), **response_log_fields(result)},
            )
            return result

    def send_streaming_message(self, payload: dict[str, Any]) -> AsyncGenerator[dict[str, Any], None]:
        """提交消息并实时接收任务事件。

        参数：
            payload: A2A SendMessageRequest 字典，必须包含 message。
        返回值：逐条返回 A2A StreamResponse 字典的异步生成器。
        """
        return self._stream(self._message_request(payload))

    def subscribe_to_task(self, task_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """订阅任务状态和产物更新。

        参数：
            task_id: A2A 服务端分配的任务 ID。
        返回值：逐条返回 A2A StreamResponse 字典的异步生成器。
        """
        return self._stream(SubscribeToTaskRequest(id=task_id))

    @staticmethod
    def _message_request(payload: dict[str, Any]) -> SendMessageRequest:
        """将工作流消息转换为官方 SDK 请求。

        参数：
            payload: 含 A2A message 的请求字典。
        返回值：保留 Part.data 业务字段的 SDK 请求。
        """
        if not isinstance(payload.get("message"), dict):
            raise ValueError("A2A 请求 payload 必须包含 message 对象")
        return ParseDict(payload, SendMessageRequest())

    async def _stream(
        self, request: SendMessageRequest | SubscribeToTaskRequest
    ) -> AsyncGenerator[dict[str, Any], None]:
        """通过 SDK 读取流式响应，逐条转换为工作流事件。

        参数：
            request: 提交消息请求或任务订阅请求。
        返回值：逐条返回 A2A StreamResponse 字典的异步生成器。
        """
        if isinstance(request, SendMessageRequest):
            method = "SendStreamingMessage"
        else:
            method = "SubscribeToTask"

        async with self._transport(method) as transport:
            if isinstance(request, SendMessageRequest):
                events = transport.send_message_streaming(request)
            else:
                events = transport.subscribe(request)
            async with aclosing(events):
                event_count = 0
                async for event in events:
                    result = MessageToDict(event)
                    event_count += 1
                    logger.info(
                        "A2A 流事件已接收",
                        extra={
                            "log_type": "business", "rpc_method": method,
                            "event_index": event_count, **_response_summary(result),
                            **response_log_fields(result),
                        },
                    )
                    yield result
                logger.info(
                    "A2A 流已结束",
                    extra={"log_type": "business", "rpc_method": method, "event_count": event_count,
                           "empty_stream": event_count == 0},
                )

    @asynccontextmanager
    async def _transport(self, method: str) -> AsyncGenerator[JsonRpcTransport, None]:
        """管理单次 SDK 调用所用的 HTTP 客户端并记录请求结果。

        参数：
            method: 请求对应的 A2A JSON-RPC 方法名。
        返回值：绑定配置地址的 SDK 传输实例，在请求结束时释放 HTTP 连接。
        """
        started = time.perf_counter()
        status = 500
        request_log = {"method": "POST", "path": self.endpoint, "rpc_method": method,
                       "trace_id": current_trace_id()}
        logger.info("A2A 请求开始", extra={"log_type": "business", **request_log})

        async def record_status(response: httpx.Response) -> None:
            """记录响应状态，供请求结束时输出日志。

            参数：
                response: HTTP 服务端响应。
            返回值：无。
            """
            nonlocal status
            status = response.status_code

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                headers=self.headers,
                event_hooks={"response": [record_status]},
            ) as client:
                yield JsonRpcTransport(client, AgentCard(), self.endpoint, is_shared_httpx=True)
        except Exception as error:
            logger.exception("A2A 请求失败", extra={"log_type": "exception", **request_log})
            if isinstance(error, (A2AError, ParseError)):
                raise A2AClientError(str(error)) from error
            raise
        finally:
            logger.info(
                "A2A 外部请求结束",
                extra={
                    "log_type": "http",
                    **request_log,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )


def _response_summary(response: dict[str, Any]) -> dict[str, Any]:
    """提取 A2A 响应结构摘要，避免在默认日志中记录业务正文。

    参数：
        response: SDK 转换后的 A2A JSON 响应。
    返回值：包含响应类别、任务标识和任务状态的结构化摘要。
    """
    task = response.get("task") if isinstance(response.get("task"), dict) else {}
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    message = response.get("message") if isinstance(response.get("message"), dict) else {}
    return {
        "response_kind": next((key for key in ("task", "message", "statusUpdate", "artifactUpdate") if key in response), "unknown"),
        "task_id": task.get("id") or response.get("taskId") or response.get("task_id"),
        "task_state": status.get("state"),
        "artifact_count": len(task.get("artifacts", [])) if isinstance(task.get("artifacts"), list) else 0,
        "message_present": bool(message),
        "response_keys": sorted(response.keys()),
    }
