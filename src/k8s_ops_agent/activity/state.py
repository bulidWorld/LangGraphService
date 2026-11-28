"""单次运行内的活动时间线与事件处理状态。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ag_ui.core.events import ActivityDeltaEvent, ActivitySnapshotEvent
from ag_ui.core.types import RunAgentInput


def timestamp_ms() -> int:
    """获取当前 Unix 毫秒时间戳。

    参数：无。
    返回值：当前时间对应的毫秒时间戳。
    """
    return time.time_ns() // 1_000_000


def iso_now() -> str:
    """获取当前 UTC ISO-8601 时间。

    参数：无。
    返回值：毫秒精度并以 Z 结尾的 UTC 时间。
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ActivityState:
    """保存当前请求的活动条目及流式消息。"""

    def __init__(self, input_data: RunAgentInput) -> None:
        """根据请求初始化活动时间线。

        参数：input_data 为用于标识 thread 和 run 的原始请求。
        返回值：无。
        """
        self.activity_id = f"{input_data.thread_id}:agent_run"
        self.activity_type = "agent_run"
        started_at = iso_now()
        self.content: dict[str, Any] = {
            "schemaVersion": 2,
            "run": {
                "runId": input_data.run_id,
                "threadId": input_data.thread_id,
                "status": "running",
                "startedAt": started_at,
                "updatedAt": started_at,
                "finishedAt": None,
                "error": None,
            },
            "events": [],
        }
        self.sequence = 0
        self.active_by_tool_call: dict[str, dict[str, Any]] = {}
        self.active_step_name: str | None = None
        self.step_activity: dict[str, Any] | None = None
        self.step_parts: dict[tuple[str, str], list[str]] = {}
        self.step_reasoning_id: str | None = None
        self.final_message_id: str | None = None
        self.latest_a2a_activity: dict[str, Any] | None = None
        self.a2a_completed_result: Any = None
        self.final_message_parts: list[str] = []
        self.snapshot_sent = False

    def snapshot(self) -> ActivitySnapshotEvent:
        """创建当前 thread 时间线的完整快照。

        参数：无。
        返回值：符合 AG-UI 协议的活动快照事件。
        """
        return ActivitySnapshotEvent(
            timestamp=timestamp_ms(),
            message_id=self.activity_id,
            activity_type=self.activity_type,
            content=self.content,
        )

    def patch(self, operations: list[dict[str, Any]]) -> ActivityDeltaEvent:
        """创建指向稳定活动消息的 RFC 6902 增量事件。

        参数：
            operations: 已按顺序更新本地 content 的 JSON Patch 操作。
        返回值：符合 AG-UI 协议的活动增量事件。
        """
        return ActivityDeltaEvent(
            timestamp=timestamp_ms(),
            message_id=self.activity_id,
            activity_type=self.activity_type,
            patch=operations,
        )

    def add_activity(self, kind: str, title: str, extra: dict[str, Any] | None = None) -> tuple[dict[str, Any], ActivityDeltaEvent]:
        """按序追加一个活动条目并生成对应 patch。

        参数：kind 为活动类型；title 为展示标题；extra 为条目附加字段。
        返回值：新条目及追加操作对应的 AG-UI 增量事件。
        """
        self.sequence += 1
        now = iso_now()
        item = {
            "id": f"{kind}-{uuid4().hex}", "sequence": self.sequence, "type": kind,
            "title": title, "status": "running", "startedAt": now,
            "updatedAt": now, "finishedAt": None, "input": {},
            "output": None, "error": None,
        }
        if extra:
            item.update(extra)
        self.content["events"].append(item)
        self.content["run"]["updatedAt"] = now
        return item, self.patch([
            {"op": "add", "path": "/events/-", "value": item},
            {"op": "replace", "path": "/run/updatedAt", "value": now},
        ])

    def update_activity(self, item: dict[str, Any], values: dict[str, Any]) -> ActivityDeltaEvent:
        """更新活动条目字段并生成 RFC 6902 patch。

        参数：item 为已存在条目；values 为要更新的字段和值。
        返回值：对应更新操作的 AG-UI 增量事件。
        """
        now = iso_now()
        operations: list[dict[str, Any]] = []
        for key, value in {**values, "updatedAt": now}.items():
            operation = "replace" if key in item else "add"
            item[key] = value
            operations.append({"op": operation, "path": f"/events/{item['sequence'] - 1}/{key}", "value": value})
        self.content["run"]["updatedAt"] = now
        operations.append({"op": "replace", "path": "/run/updatedAt", "value": now})
        return self.patch(operations)

    def step_output(self) -> dict[str, Any] | None:
        """拼接当前步骤中已收到的公开文本和推理摘要。

        参数：无。
        返回值：按首次收到内容的顺序排列的消息；没有内容时返回 None。
        """
        if not self.step_parts:
            return None
        return {"messages": [
            {"messageId": message_id, "type": kind, "text": "".join(parts)}
            for (kind, message_id), parts in self.step_parts.items()
        ]}

    def ensure_snapshot(self) -> Iterator[ActivitySnapshotEvent]:
        """首次需要时间线时发送完整快照。

        参数：无。
        返回值：首次调用时产生快照，已发送时不产生事件。
        """
        if not self.snapshot_sent:
            yield self.snapshot()
            self.snapshot_sent = True
