#!/usr/bin/env python3
"""校验或应用应用程序的 PostgreSQL 数据库结构，按文件名顺序处理 db 目录下全部 XML 定义。"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 支持在源码仓库中直接运行脚本，无需先安装项目包。
sys.path.insert(0, str(ROOT / "src"))

from k8s_ops_agent.db_config import database_conninfo  # noqa: E402

# 默认扫描 db 目录下全部 XML 定义文件；可用 DB_SCHEMA_DIR 覆盖。
SCHEMA_DIR = Path(os.getenv("DB_SCHEMA_DIR", str(ROOT / "db")))
if not SCHEMA_DIR.is_dir():
    SCHEMA_DIR = Path.cwd() / "db"
IDENTIFIER = re.compile(r"^\w+$", re.UNICODE)
SQL_TYPE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(?:\([0-9, ]+\))?(?:\[\])?$")


def load_schema_files(schema_dir: Path = SCHEMA_DIR) -> list[Path]:
    """按文件名顺序返回 schema 目录下全部 XML 定义文件。

    参数：
        schema_dir: 存放 XML 定义文件的目录，默认使用 SCHEMA_DIR。
    返回值：按文件名排序的 XML 文件列表。
    """
    files = sorted(path for path in schema_dir.glob("*.xml") if path.is_file())
    if not files:
        raise ValueError(f"No schema XML files found in {schema_dir}")
    return files


def _identifier(value: str) -> str:
    """把值转成带引号的 SQL 标识符。

    参数：
        value: 待引用的标识符原文。
    返回值：双引号包裹的标识符。
    """
    # 只允许简单的 PostgreSQL 标识符，避免 XML 内容成为标识符位置的任意 SQL 片段。
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return f'"{value}"'


def _quoted_text(value: str) -> str:
    """把值转成单引号包裹的 SQL 字符串字面量。

    参数：
        value: 原文文本。
    返回值：转义单引号后的 SQL 字符串字面量。
    """
    return "'" + value.replace("'", "''") + "'"


def _columns(element: ET.Element) -> list[ET.Element]:
    """返回 table 元素下声明的全部 column 子元素。

    参数：
        element: table 元素。
    返回值：column 子元素列表。
    """
    return list(element.findall("./columns/column"))


def _parse_schema(schema_file: Path) -> ET.Element:
    """解析单个 XML 定义文件，解析失败时带上文件名。

    参数：
        schema_file: XML 定义文件路径。
    返回值：XML 根元素。
    """
    try:
        return ET.parse(schema_file).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Cannot parse schema file {schema_file.name}: {exc}") from exc


def _validate(root: ET.Element, available: dict[str, set[str]]) -> None:
    """生成 SQL 前校验单个定义文件的项目规则。

    参数：
        root: XML 根元素。
        available: 全部文件提供的表名到列名集合的映射，用于校验引用关系。
    返回值：无。
    """
    # 生成 SQL 前校验项目规则；ref-table/ref-col 只描述引用关系，不会生成数据库外键。
    if root.tag != "database" or root.get("dialect", "postgresql") != "postgresql":
        raise ValueError("Schema root must be a PostgreSQL <database>")
    tables = root.findall("./tables/table")
    table_names = [table.get("name", "") for table in tables]
    if len(table_names) != len(set(table_names)):
        raise ValueError("Table names must be unique")
    for table in tables:
        table_name = table.get("name", "")
        _identifier(table_name)
        columns = _columns(table)
        names = [column.get("name", "") for column in columns]
        if not names or len(names) != len(set(names)):
            raise ValueError(f"Table {table_name} needs uniquely named columns")
        for column in columns:
            _identifier(column.get("name", ""))
            col_type = column.get("type", "")
            if not SQL_TYPE.fullmatch(col_type):
                raise ValueError(f"Unsupported SQL type: {col_type!r}")
            if (
                column.get("identity", "false").lower() == "true"
                and col_type != "integer"
            ):
                raise ValueError(
                    "Identity columns must use integer per database conventions"
                )
            ref_table, ref_col = column.get("ref-table"), column.get("ref-col")
            if bool(ref_table) != bool(ref_col):
                raise ValueError(
                    f"{table_name}.{column.get('name')} must set both ref-table and ref-col"
                )
            if ref_table:
                if ref_table not in available or ref_col not in available[ref_table]:
                    raise ValueError(f"Unknown reference {ref_table}.{ref_col}")
        for constraint in table.findall("./constraints/*"):
            if constraint.tag == "check":
                raise ValueError(
                    "Database CHECK constraints are disabled by project convention"
                )
            if constraint.tag == "foreign-key":
                raise ValueError(
                    "Database foreign keys are disabled by project convention"
                )
            _identifier(constraint.get("name", ""))
            for name in constraint.get("columns", "").split():
                if name not in names:
                    raise ValueError(f"Unknown constraint column {table_name}.{name}")
        for index in table.findall("./indexes/index"):
            _identifier(index.get("name", ""))
            for column in index.findall("./column"):
                if column.get("name") not in names:
                    raise ValueError(
                        f"Unknown index column {table_name}.{column.get('name')}"
                    )


def _table_sql(table: ET.Element) -> list[str]:
    """把单个 table 元素转成幂等的建表、补列、约束、注释和索引语句。

    参数：
        table: XML table 元素。
    返回值：该表对应的 SQL 语句列表。
    """
    name = table.get("name", "")
    parts: list[str] = []
    for column in _columns(table):
        col_name = _identifier(column.get("name", ""))
        col_type = column.get("type", "")
        if column.get("identity", "false").lower() == "true":
            col_type += " GENERATED BY DEFAULT AS IDENTITY"
        definition = f"{col_name} {col_type}"
        if column.get("nullable", "true").lower() == "false":
            definition += " NOT NULL"
        if "default" in column.attrib:
            definition += f" DEFAULT {column.get('default')}"
        parts.append(definition)
    for constraint in table.findall("./constraints/*"):
        kind = {"primary-key": "PRIMARY KEY", "unique": "UNIQUE"}.get(constraint.tag)
        if not kind:
            continue
        columns = ", ".join(
            _identifier(col) for col in constraint.get("columns", "").split()
        )
        parts.append(
            f"CONSTRAINT {_identifier(constraint.get('name', ''))} {kind} ({columns})"
        )
    statements = [
        f"CREATE TABLE IF NOT EXISTS {_identifier(name)} (\n    "
        + ",\n    ".join(parts)
        + "\n);"
    ]
    # 为旧版本数据库补充新增字段；字段类型等破坏性变更必须通过显式迁移完成。
    for column in _columns(table):
        col_type = column.get("type", "")
        if column.get("identity", "false").lower() == "true":
            col_type += " GENERATED BY DEFAULT AS IDENTITY"
        definition = f"{_identifier(column.get('name', ''))} {col_type}"
        if column.get("nullable", "true").lower() == "false":
            definition += " NOT NULL"
        if "default" in column.attrib:
            definition += f" DEFAULT {column.get('default')}"
        statements.append(
            f"ALTER TABLE {_identifier(name)} ADD COLUMN IF NOT EXISTS {definition};"
        )
    for constraint in table.findall("./constraints/*"):
        kind = {"primary-key": "PRIMARY KEY", "unique": "UNIQUE"}.get(constraint.tag)
        if not kind:
            continue
        constraint_name = constraint.get("name", "")
        columns = ", ".join(
            _identifier(col) for col in constraint.get("columns", "").split()
        )
        statements.append(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = {_quoted_text(constraint_name)} "
            f"AND conrelid = {_quoted_text(name)}::regclass) THEN "
            f"ALTER TABLE {_identifier(name)} ADD CONSTRAINT {_identifier(constraint_name)} "
            f"{kind} ({columns}); END IF; END $$;"
        )
    if table.get("comment"):
        statements.append(
            f"COMMENT ON TABLE {_identifier(name)} IS {_quoted_text(table.get('comment', ''))};"
        )
    for column in _columns(table):
        if column.get("comment"):
            statements.append(
                f"COMMENT ON COLUMN {_identifier(name)}.{_identifier(column.get('name', ''))} "
                f"IS {_quoted_text(column.get('comment', ''))};"
            )
    for index in table.findall("./indexes/index"):
        unique = "UNIQUE " if index.get("unique", "false").lower() == "true" else ""
        columns = ", ".join(
            f"{_identifier(column.get('name', ''))} {column.get('order', 'ASC')}"
            for column in index.findall("./column")
        )
        where = f" WHERE {index.get('where')}" if index.get("where") else ""
        statements.append(
            f"CREATE {unique}INDEX IF NOT EXISTS {_identifier(index.get('name', ''))} "
            f"ON {_identifier(name)} USING {index.get('method', 'btree')} ({columns}){where};"
        )
        if index.get("comment"):
            statements.append(
                f"COMMENT ON INDEX {_identifier(index.get('name', ''))} "
                f"IS {_quoted_text(index.get('comment', ''))};"
            )
    return statements


def schema_sql(schema_files: Sequence[Path] | None = None) -> str:
    """把所有 XML 定义文件转换为幂等的 PostgreSQL DDL。

    参数：
        schema_files: 待转换的 XML 文件列表，默认扫描 SCHEMA_DIR 下全部 XML。
    返回值：以空行分隔的完整 SQL 脚本，每个文件的语句前带来源注释。
    """
    files = list(schema_files) if schema_files is not None else load_schema_files()
    roots: list[tuple[Path, ET.Element]] = [
        (schema_file, _parse_schema(schema_file)) for schema_file in files
    ]
    # 引用校验覆盖全部文件，允许跨文件描述 ref-table/ref-col 引用关系；
    # 重名表会互相覆盖定义，直接视为错误。
    available: dict[str, set[str]] = {}
    seen: dict[str, Path] = {}
    for schema_file, root in roots:
        for table in root.findall("./tables/table"):
            name = table.get("name", "")
            if name in seen:
                raise ValueError(
                    f"Duplicate table {name!r} in {schema_file.name} "
                    f"(already defined in {seen[name].name})"
                )
            seen[name] = schema_file
            available[name] = {
                column.get("name", "") for column in _columns(table)
            }
    statements: list[str] = []
    for schema_file, root in roots:
        _validate(root, available)
        statements.append(f"-- schema: {schema_file.name}")
        for extension in root.findall("./extensions/extension"):
            statements.append(
                f"CREATE EXTENSION IF NOT EXISTS {_identifier(extension.get('name', ''))};"
            )
        for table in root.findall("./tables/table"):
            statements.extend(_table_sql(table))
        for statement in root.findall("./statements/statement"):
            statements.append(
                statement.findtext("./sql", default="").strip().rstrip(";") + ";"
            )
    return "\n\n".join(statements) + "\n"


def sync_schema(conninfo: str, schema_files: Sequence[Path] | None = None) -> None:
    """把全部 XML 定义转换为 SQL 并在一个事务中执行。

    参数：
        conninfo: libpq 连接字符串。
        schema_files: 待同步的 XML 文件列表，默认扫描 SCHEMA_DIR 下全部 XML。
    返回值：无。
    """
    import psycopg

    with psycopg.connect(conninfo) as connection:
        with connection.cursor() as cursor:
            # connection 上下文会把幂等 DDL 作为一个事务提交；任一语句失败则整体回滚。
            cursor.execute(schema_sql(schema_files))


def main() -> int:
    """解析命令行参数并执行校验、生成 SQL 或同步数据库。

    参数：无。
    返回值：进程退出码，0 表示成功。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验结构，不连接数据库")
    parser.add_argument("--sql-only", action="store_true", help="打印生成的 PostgreSQL SQL")
    args = parser.parse_args()
    # 先解析目录下全部定义文件；所有模式都先生成并校验 SQL，只有默认模式连接数据库。
    schema_files = load_schema_files()
    sql = schema_sql(schema_files)
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
    sync_schema(conninfo, schema_files)
    print("Database schema synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
