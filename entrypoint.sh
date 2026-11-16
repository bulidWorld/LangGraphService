#!/bin/sh
# 容器入口：配置了数据库时先同步业务表结构，再启动应用。
# 未配置任何 DB_* 时跳过同步（本地开发使用内存 checkpointer）；
# 部分配置时 sync_schema.py 会直接报错退出，避免带病启动。
set -e

if [ -n "${DB_HOST:-}${DB_PORT:-}${DB_NAME:-}${DB_USER:-}${DB_PASSWORD:-}" ]; then
    echo "Syncing database schema from db/ definitions..."
    python scripts/sync_schema.py
fi

exec "$@"
