"""从独立环境变量构造 PostgreSQL 连接信息。"""

from __future__ import annotations

import os
from collections.abc import Mapping

from psycopg.conninfo import make_conninfo


DB_VARIABLES = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


def database_conninfo(env: Mapping[str, str] | None = None) -> str | None:
    """安全构造 libpq 连接字符串；未配置数据库时返回 None。"""
    values = os.environ if env is None else env
    configured = {name: values.get(name, "") for name in DB_VARIABLES}
    present = [name for name, value in configured.items() if value]
    # 本地开发允许完全不配置数据库，但拒绝不完整配置，避免环境变量拼写错误时
    # 意外退回内存 checkpointer。
    if not present:
        return None
    missing = [name for name, value in configured.items() if not value]
    if missing:
        raise ValueError(
            "Incomplete PostgreSQL configuration; set all of: "
            + ", ".join(DB_VARIABLES)
            + ". Missing: "
            + ", ".join(missing)
        )
    try:
        port = int(configured["DB_PORT"])
    except ValueError as exc:
        raise ValueError("DB_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("DB_PORT must be between 1 and 65535")
    # 使用 libpq 格式化器处理转义，避免密码等字段中的特殊字符破坏连接字符串。
    return make_conninfo(
        host=configured["DB_HOST"],
        port=port,
        dbname=configured["DB_NAME"],
        user=configured["DB_USER"],
        password=configured["DB_PASSWORD"],
    )
