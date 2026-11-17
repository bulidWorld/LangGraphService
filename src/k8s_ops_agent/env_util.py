"""读取应用构建元数据及运行环境配置。"""

from __future__ import annotations

import json
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
