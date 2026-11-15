#!/usr/bin/env python3
"""校验或应用应用程序的 PostgreSQL 数据库结构。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 支持在源码仓库中直接运行脚本，无需先安装项目包。
sys.path.insert(0, str(ROOT / "src"))

from k8s_ops_agent.schema_sync import schema_sql, sync_schema  # noqa: E402
from k8s_ops_agent.db_config import database_conninfo  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验结构，不连接数据库")
    parser.add_argument("--sql-only", action="store_true", help="打印生成的 PostgreSQL SQL")
    args = parser.parse_args()
    # 所有模式都先生成并校验 SQL；只有默认模式会连接 PostgreSQL 并执行 DDL。
    sql = schema_sql()
    if args.sql_only:
        print(sql, end="")
        return 0
    if args.check:
        print("Database schema is valid")
        return 0
    try:
        conninfo = database_conninfo()
    except ValueError as exc:
        parser.error(str(exc))
    if not conninfo:
        parser.error(
            "DB_HOST, DB_PORT, DB_NAME, DB_USER, and DB_PASSWORD are required "
            "unless --check or --sql-only is used"
        )
    sync_schema(conninfo)
    print("Database schema synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
