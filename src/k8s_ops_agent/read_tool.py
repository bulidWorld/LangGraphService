"""提供当前 LangGraph 会话工作区内的只读文件工具。"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from k8s_ops_agent.env_util import get_service_name


def _workspace_path(thread_id: str) -> Path:
    """根据会话标识计算受限的 workspace 路径。

    参数：
        thread_id: 当前 LangGraph 会话标识。
    返回值：当前会话 workspace 的绝对路径。
    """
    safe_service = re.sub(r"[^A-Za-z0-9._-]", "_", get_service_name()).strip("._") or "k8s_ops_agent"
    safe_thread_id = re.sub(r"[^A-Za-z0-9._-]", "_", thread_id).strip("._") or "unknown"
    return Path("/var/service") / safe_service / "share/workspace" / safe_thread_id


@tool
def read_file(
    path: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """读取当前会话 workspace 中的 UTF-8 文本文件。

    参数：
        path: workspace 内文件的绝对路径或相对路径。
        config: LangGraph 注入的运行配置，包含当前 thread_id。
    返回值：文件正文；路径越界、文件不存在或内容无法解码时返回明确错误。
    """
    thread_id = str(config.get("configurable", {}).get("thread_id", ""))
    if not thread_id:
        return "读取失败：当前运行缺少会话标识。"
    workspace = _workspace_path(thread_id).resolve()
    requested = Path(path)
    candidate = requested if requested.is_absolute() else workspace / requested
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace)
    except (OSError, ValueError):
        return "读取失败：文件不存在，或路径不在当前会话 workspace 内。"
    if not resolved.is_file():
        return "读取失败：目标不是普通文件。"
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "读取失败：文件无法作为 UTF-8 文本读取。"
