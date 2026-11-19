"""验证 AG-UI MTAP 审批恢复请求的服务端校验。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ag_ui.core.types import RunAgentInput
from fastapi import HTTPException
from starlette.requests import Request

from k8s_ops_agent.server import _validate_resume


def _request() -> Request:
    """创建包含可信审核人身份的最小 Starlette 请求。

    参数：无。
    返回值：测试用 HTTP 请求。
    """
    request = Request({"type": "http", "method": "POST", "path": "/agent", "headers": []})
    request.state.reviewer_id = "reviewer-1"
    request.scope["app"] = SimpleNamespace(state=SimpleNamespace(graph=object()))
    return request


def _input(entry: dict, run_id: str = "new-run") -> RunAgentInput:
    """构造 MTAP 恢复运行输入。

    参数：
        entry: AG-UI resume entry。
        run_id: 本次恢复请求的运行标识。
    返回值：AG-UI 运行请求对象。
    """
    return RunAgentInput(
        thread_id="thread-1", run_id=run_id, messages=[], resume=[entry]
    )


class AguiResumeValidationTest(unittest.TestCase):
    """验证恢复条目、审批答案、可信身份和一次性恢复权。"""

    def test_empty_resume_passes_through_without_checkpoint_lookup(self) -> None:
        """普通新运行没有 resume 时不查询审批中断。

        参数：无。
        返回值：无；调用了恢复相关副作用时断言失败。
        """
        with patch("k8s_ops_agent.server._active_interrupts", new=AsyncMock()) as active:
            result = asyncio.run(_validate_resume(_input_for_empty(), _request()))
        self.assertIs(result.resume, None)
        active.assert_not_awaited()

    def test_rejects_multiple_entries_and_foreign_or_inactive_interrupts(self) -> None:
        """拒绝单次 run 多条恢复项、没有活动中断及伪造中断 ID。

        参数：无。
        返回值：无；协议违规未返回对应 HTTP 错误时断言失败。
        """
        cases = [
            (_input({"interrupt_id": "i1", "status": "cancelled"}) .model_copy(update={
                "resume": [
                    _input({"interrupt_id": "i1", "status": "cancelled"}).resume[0],
                    _input({"interrupt_id": "i2", "status": "cancelled"}).resume[0],
                ]}), [], 422),
            (_input({"interrupt_id": "i1", "status": "cancelled"}), [], 409),
            (_input({"interrupt_id": "foreign", "status": "cancelled"}), ["i1"], 409),
        ]
        for data, active_ids, expected_status in cases:
            async def active_interrupts(*args):
                return {key: {"request_id": "approval-1"} for key in active_ids}
            with self.subTest(status=expected_status, active=active_ids), patch(
                "k8s_ops_agent.server._active_interrupts", new=active_interrupts
            ), self.assertRaises(HTTPException) as raised:
                asyncio.run(_validate_resume(data, _request()))
            self.assertEqual(raised.exception.status_code, expected_status)

    def test_approved_decision_is_validated_and_server_fields_are_authoritative(self) -> None:
        """答案通过校验后注入可信审核人和时间，并持久化恢复占用。

        参数：无。
        返回值：无；客户端不能覆盖服务端决定元信息。
        """
        data = _input({"interrupt_id": "interrupt-1", "status": "resolved", "payload": {
            "request_id": "approval-1", "status": "approved", "answers": {"confirm": True},
            "comment": "同意执行",
        }})
        approval = {"thread_id": "thread-1", "status": "pending", "request_payload": {
            "inputs": [{"key": "confirm", "type": "confirm", "required": True}]
        }}
        with (
            patch("k8s_ops_agent.server._active_interrupts", new=AsyncMock(return_value={
                "interrupt-1": {"request_id": "approval-1"}
            })),
            patch("k8s_ops_agent.server.get_approval_request", new=AsyncMock(return_value=approval)),
            patch("k8s_ops_agent.server.record_approval_resume", new=AsyncMock(return_value=True)) as record,
        ):
            result = asyncio.run(_validate_resume(data, _request()))
        normalized = result.resume[0].payload
        self.assertEqual(normalized["reviewer"], "reviewer-1")
        self.assertTrue(normalized["decided_at"])
        self.assertEqual(normalized["answers"], {"confirm": True})
        self.assertEqual(normalized["thread_id"], "thread-1")
        record.assert_awaited_once()

    def test_rejects_payload_tampering_pending_mismatch_and_duplicate_resume(self) -> None:
        """拒绝不匹配请求、未认证、无效答案及已被占用的审批。

        参数：无。
        返回值：无；安全边界失效时断言失败。
        """
        base = {"thread_id": "thread-1", "status": "pending", "request_payload": {"inputs": []}}
        entries = [
            {"interrupt_id": "interrupt-1", "status": "resolved", "payload": {
                "request_id": "wrong", "status": "approved", "answers": {},
            }},
            {"interrupt_id": "interrupt-1", "status": "resolved", "payload": {
                "request_id": "approval-1", "status": "approved", "answers": {}, "reviewer": "forged",
            }},
        ]
        for entry in entries:
            with self.subTest(entry=entry), patch(
                "k8s_ops_agent.server._active_interrupts", new=AsyncMock(return_value={
                    "interrupt-1": {"request_id": "approval-1"}
                })), patch(
                "k8s_ops_agent.server.get_approval_request", new=AsyncMock(return_value=base)
            ), self.assertRaises(HTTPException) as raised:
                asyncio.run(_validate_resume(_input(entry), _request()))
            self.assertIn(raised.exception.status_code, (409, 422))

        with (
            patch("k8s_ops_agent.server._active_interrupts", new=AsyncMock(return_value={
                "interrupt-1": {"request_id": "approval-1"}
            })),
            patch("k8s_ops_agent.server.get_approval_request", new=AsyncMock(return_value=base)),
            patch("k8s_ops_agent.server.record_approval_resume", new=AsyncMock(return_value=False)),
            self.assertRaises(HTTPException) as raised,
        ):
            asyncio.run(_validate_resume(_input({"interrupt_id": "interrupt-1", "status": "cancelled"}), _request()))
        self.assertEqual(raised.exception.status_code, 409)

    def test_cancelled_resume_uses_checkpoint_request_id_and_rejects_payload(self) -> None:
        """取消恢复从中断快照关联审批 ID，且要求取消 entry 不含 payload。

        参数：无。
        返回值：无；取消语义不符时断言失败。
        """
        async def run_cancel(payload=None):
            entry = {"interrupt_id": "interrupt-1", "status": "cancelled"}
            if payload is not None:
                entry["payload"] = payload
            approval = {"thread_id": "thread-1", "status": "pending", "request_payload": {}}
            with (
                patch("k8s_ops_agent.server._active_interrupts", new=AsyncMock(return_value={
                    "interrupt-1": {"request_id": "approval-1"}
                })),
                patch("k8s_ops_agent.server.get_approval_request", new=AsyncMock(return_value=approval)),
                patch("k8s_ops_agent.server.record_approval_resume", new=AsyncMock(return_value=True)) as record,
            ):
                result = await _validate_resume(_input(entry), _request())
            return result, record

        result, record = asyncio.run(run_cancel())
        self.assertEqual(result.resume[0].status, "cancelled")
        self.assertEqual(record.await_args.args[3]["status"], "cancelled")
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(run_cancel({"request_id": "approval-1"}))
        self.assertEqual(raised.exception.status_code, 422)


def _input_for_empty() -> RunAgentInput:
    """生成没有恢复条目的普通 AG-UI 运行请求。

    参数：无。
    返回值：不带 resume 的运行输入。
    """
    return RunAgentInput(thread_id="thread-1", run_id="run-1", messages=[])


if __name__ == "__main__":
    unittest.main()
