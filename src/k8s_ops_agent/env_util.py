"""读取应用构建元数据及运行环境配置。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_service_name() -> str:
    """读取项目构建元数据中的服务名称。

    参数：无。
    返回值：构建元数据中的服务名称；元数据不可用时返回默认名称。
    """
    metadata_path = Path(__file__).resolve().parents[2] / "build_meta.json"
    try:
        with metadata_path.open(encoding="utf-8") as metadata_file:
            metadata = json.load(metadata_file)
    except (OSError, json.JSONDecodeError):
        return "k8s_ops_agent"

    name = metadata.get("name") if isinstance(metadata, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else "k8s_ops_agent"


def _workspace_path(thread_id: str) -> Path:
    """根据会话标识计算受限的 workspace 路径。

    参数：
        thread_id: 当前 LangGraph 会话标识。
    返回值：当前会话 workspace 的绝对路径。
    """
    safe_service = re.sub(r"[^A-Za-z0-9._-]", "_", get_service_name()).strip("._") or "k8s_ops_agent"
    safe_thread_id = re.sub(r"[^A-Za-z0-9._-]", "_", thread_id).strip("._") or "unknown"
    return Path("/var/service") / safe_service / "share/workspace" / safe_thread_id
