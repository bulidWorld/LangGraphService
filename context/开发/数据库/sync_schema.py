#!/usr/bin/env python3
"""仓库数据库结构同步器的兼容入口。"""
import runpy
from pathlib import Path

if __name__ == "__main__":
    repository_root = Path(__file__).resolve().parents[3]
    runpy.run_path(str(repository_root / "scripts" / "sync_schema.py"), run_name="__main__")
