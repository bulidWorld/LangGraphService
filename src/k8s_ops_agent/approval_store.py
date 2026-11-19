"""保存人工审批请求、决定和恢复状态。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


_pool: AsyncConnectionPool | None = None
_memory_requests: dict[str, dict[str, Any]] = {}
_memory_lock = asyncio.Lock()
_SESSION_TABLE = '"incident_runs"'
_APPROVAL_TABLE = '"approval_requests"'


def configure_approval_store(pool: AsyncConnectionPool | None) -> None:
    """配置审批存储。

    参数：
        pool: PostgreSQL 异步连接池；为空时使用仅供开发环境使用的进程内存储。
    返回值：无。
    """
    global _pool
    _pool = pool


async def create_approval_request(
    request_id: str,
    thread_id: str,
    run_id: str | None,
    payload: dict[str, Any],
) -> None:
    """幂等创建审批请求，并确保对应排障会话索引存在。

    参数：
        request_id: 审批请求的稳定 UUID。
        thread_id: LangGraph 对话标识。
        run_id: 发起审批的 LangGraph 运行标识。
        payload: 版本化审批请求对象。
    返回值：无。
    """
    if _pool is None:
        async with _memory_lock:
            _memory_requests.setdefault(
                request_id,
                {
                    "request_id": request_id,
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "action": payload["action"],
                    "rationale": payload["rationale"],
                    "change_plan": payload["change_plan"],
                    "evidence": payload["evidence"],
                    "request_payload": payload,
                    "status": "pending",
                    "decision": None,
                    "resume_run_id": None,
                },
            )
        return

    async with _pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"INSERT INTO {_SESSION_TABLE} (thread_id, status) "
                "VALUES (%s, 'awaiting_approval') "
                "ON CONFLICT (thread_id) DO UPDATE SET "
                "status = 'awaiting_approval', updated_at = CURRENT_TIMESTAMP "
                "RETURNING id",
                (thread_id,),
            )
            incident = await cursor.fetchone()
            await cursor.execute(
                f"INSERT INTO {_APPROVAL_TABLE} "
                "(incident_run_id, request_id, thread_id, run_id, action, rationale, "
                "change_plan, evidence, request_payload, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') "
                "ON CONFLICT (request_id) DO NOTHING",
                (
                    incident[0],
                    request_id,
                    thread_id,
                    run_id,
                    payload["action"],
                    payload["rationale"],
                    payload["change_plan"],
                    json.dumps(payload["evidence"], ensure_ascii=False),
                    Jsonb(payload),
                ),
            )


async def get_approval_request(request_id: str) -> dict[str, Any] | None:
    """按请求标识读取审批请求。

    参数：
        request_id: 审批请求 UUID。
    返回值：审批记录字典；记录不存在时返回 None。
    """
    if _pool is None:
        async with _memory_lock:
            request = _memory_requests.get(request_id)
            return dict(request) if request is not None else None

    async with _pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT request_id, thread_id, run_id, action, request_payload, "
                "status, decision, reviewer, decided_at, resume_run_id "
                f"FROM {_APPROVAL_TABLE} WHERE request_id = %s",
                (request_id,),
            )
            return await cursor.fetchone()


async def record_approval_resume(
    request_id: str,
    thread_id: str,
    run_id: str,
    decision: dict[str, Any],
) -> bool:
    """原子保存审批决定并占用对应 AG-UI run 的恢复权。

    参数：
        request_id: 审批请求 UUID。
        thread_id: 必须与请求所属 thread 相同。
        run_id: 当前 AG-UI run 标识。
        decision: 已完成字段校验并包含服务端决定时间的决定对象。
    返回值：决定保存且恢复权占用成功时返回 True。
    """
    if _pool is None:
        async with _memory_lock:
            request = _memory_requests.get(request_id)
            if (
                request is None
                or request["thread_id"] != thread_id
                or request["status"] != "pending"
            ):
                return False
            request["status"] = decision["status"]
            request["decision"] = decision
            request["decided_at"] = decision["decided_at"]
            request["resume_run_id"] = run_id
            return True

    async with _pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"UPDATE {_APPROVAL_TABLE} SET status = %s, decision = %s, "
                "decided_at = %s, resume_run_id = %s "
                "WHERE request_id = %s AND thread_id = %s AND status = 'pending' "
                "AND resume_run_id IS NULL",
                (
                    decision["status"],
                    Jsonb(decision),
                    decision["decided_at"],
                    run_id,
                    request_id,
                    thread_id,
                ),
            )
            return cursor.rowcount == 1
