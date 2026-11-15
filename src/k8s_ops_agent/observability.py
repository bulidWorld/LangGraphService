"""结构化应用日志和可选的 Langfuse 链路追踪。"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncGenerator


_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)


class JsonFormatter(logging.Formatter):
    """将每条日志格式化为一个 JSON 对象，便于容器日志采集。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": "WARN" if record.levelno == logging.WARNING else record.levelname,
            "log_type": getattr(record, "log_type", "business"),
            "trace_id": getattr(record, "trace_id", _trace_id.get()),
            "message": record.getMessage(),
        }
        # 保留调用方传入的结构化字段，并排除 LogRecord 内部字段，避免日志内容冗长。
        standard_fields = logging.LogRecord("", 0, "", 0, "", (), None).__dict__
        for key, value in record.__dict__.items():
            if key not in standard_fields and key not in {
                "message", "asctime", "log_type", "trace_id"
            } and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exceptionInfo"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


configure_logging()
logger = logging.getLogger("k8s_ops_agent")


def current_trace_id() -> str:
    return _trace_id.get()


class _NoopObservation:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: Any) -> bool:
        return False


def _langfuse_client() -> Any | None:
    """返回已配置的 SDK 客户端；追踪不可用时只记录一次提示。"""
    global _langfuse_unconfigured_logged, _langfuse_disabled_logged
    if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
        # 链路追踪是可选功能；缺少凭据时只告警一次，避免日志被重复提示淹没。
        if not _langfuse_unconfigured_logged:
            logger.warning(
                "Langfuse is not configured; continuing with application logs only",
                extra={"log_type": "business"},
            )
            _langfuse_unconfigured_logged = True
        return None
    try:
        from langfuse import get_client

        client = get_client()
        if client.is_enabled():
            return client
        if not _langfuse_disabled_logged:
            logger.warning(
                "Langfuse is unavailable; continuing with application logs only",
                extra={"log_type": "business"},
            )
            _langfuse_disabled_logged = True
        return None
    except Exception:
        logger.exception(
            "Langfuse client initialization failed; continuing without tracing",
            extra={"log_type": "exception"},
        )
        return None


_langfuse_unconfigured_logged = False
_langfuse_disabled_logged = False


@asynccontextmanager
async def _safe_observation(scope: Any) -> AsyncGenerator[Any, None]:
    """在异步请求中安全使用 SDK 提供的本地 span 上下文。"""
    try:
        observation = scope.__enter__()
    except Exception:
        logger.exception(
            "Langfuse span start failed; continuing request without tracing",
            extra={"log_type": "exception"},
        )
        yield None
        return

    try:
        yield observation
    except BaseException as exc:
        # 使用原始异常关闭 span；即使导出器关闭失败，也不能覆盖正在传播的应用异常。
        try:
            scope.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            logger.exception(
                "Langfuse span close failed while handling request exception",
                extra={"log_type": "exception"},
            )
        raise
    else:
        try:
            scope.__exit__(None, None, None)
        except Exception:
            logger.exception(
                "Langfuse span close failed; request completed normally",
                extra={"log_type": "exception"},
            )


@asynccontextmanager
async def request_observation(
    method: str, path: str, trace_id: str
) -> AsyncGenerator[Any, None]:
    """配置了凭据时，为异步请求创建 Langfuse span。"""
    token = _trace_id.set(trace_id)
    try:
        client = _langfuse_client()
        try:
            scope = (
                client.start_as_current_observation(
                    as_type="span",
                    name="agui.request",
                    input={"method": method, "path": path},
                    metadata={"trace_id": trace_id},
                )
                if client is not None
                else _NoopObservation()
            )
        except Exception:
            logger.exception(
                "Langfuse request span creation failed; continuing without tracing",
                extra={"log_type": "exception"},
            )
            scope = _NoopObservation()
        async with _safe_observation(scope) as observation:
            yield observation
    finally:
        # ContextVar 按异步任务隔离；请求结束后恢复旧值，避免复用工作任务串用 trace ID。
        _trace_id.reset(token)


@asynccontextmanager
async def operation_observation(
    name: str, metadata: dict[str, Any] | None = None
) -> AsyncGenerator[Any, None]:
    """在当前请求 trace 中添加一个不包含敏感输入的子 span。"""
    client = _langfuse_client()
    try:
        scope = (
            client.start_as_current_observation(
                as_type="span", name=name, metadata=metadata or {}
            )
            if client is not None
            else _NoopObservation()
        )
    except Exception:
        logger.exception(
            "Langfuse operation span creation failed; continuing without tracing",
            extra={"log_type": "exception"},
        )
        scope = _NoopObservation()
    async with _safe_observation(scope) as span:
        yield span


def update_observation(observation: Any, **fields: Any) -> None:
    """更新 span 元数据；SDK 错误不能影响请求处理。"""
    if observation is None:
        return
    try:
        observation.update(**fields)
    except Exception:
        logger.exception(
            "Langfuse span update failed; continuing application work",
            extra={"log_type": "exception"},
        )


def new_trace_id() -> str:
    return uuid.uuid4().hex
