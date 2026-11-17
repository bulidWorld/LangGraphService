"""验证 AG-UI 人工审批中断的协议配置。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from k8s_ops_agent.server import app


class AguiInterruptConfigTest(unittest.TestCase):
    """验证 AG-UI 审批中断使用标准结构化协议。"""

    def test_lifespan_enables_structured_interrupt_outcome(self) -> None:
        """验证服务只启用可供标准 ResumeEntry 使用的结构化中断事件。

        参数：无。
        返回值：无；配置不符合预期时断言失败。
        """
        with patch("k8s_ops_agent.server.DATABASE_CONNINFO", None):
            with TestClient(app) as client:
                self.assertTrue(client.app.state.agent.emit_interrupt_outcome)
                self.assertFalse(
                    client.app.state.agent.enable_legacy_on_interrupt_event
                )
                self.assertFalse(client.app.state.agent.emit_raw_events)

    def test_cloned_agent_keeps_raw_events_disabled(self) -> None:
        """验证请求级 agent 副本不会重新启用 LangGraph RAW 事件。

        参数：无。
        返回值：无；副本配置不符合预期时断言失败。
        """
        with patch("k8s_ops_agent.server.DATABASE_CONNINFO", None):
            with TestClient(app) as client:
                request_agent = client.app.state.agent.clone()

                self.assertFalse(request_agent.emit_raw_events)


if __name__ == "__main__":
    unittest.main()
