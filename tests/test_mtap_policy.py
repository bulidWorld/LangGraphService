"""验证 MTAP MCP 工具定义及审批表单答案。"""

from __future__ import annotations

import unittest

from fastapi import HTTPException
from langchain_core.tools import StructuredTool

from k8s_ops_agent.approval_auth import validate_approval_answers
from k8s_ops_agent.mcp_registry import APPROVAL_META_KEY, get_tool_policy


def _sample_tool(approval: dict) -> StructuredTool:
    """构造携带指定 MTAP 定义的测试工具。

    参数：
        approval: 待验证的 MTAP 审批定义。
    返回值：附带该定义的 LangChain 结构化工具。
    """
    def run(value: str = "") -> str:
        """返回固定文本，提供合法的工具输入 Schema。

        参数：
            value: 示例工具参数。
        返回值：固定测试文本。
        """
        return value

    tool = StructuredTool.from_function(run, name="sample", description="测试工具")
    tool.metadata = {"_meta": {APPROVAL_META_KEY: approval}, "mcp_server": "test"}
    return tool


def _base_approval(mode: str = "required") -> dict:
    """生成有效的基础审批定义。

    参数：
        mode: 审批模式。
    返回值：适用于工具策略校验的 MTAP 定义。
    """
    approval = {
        "schemaVersion": 1,
        "security": {"riskLevel": "medium", "approvalMode": mode},
    }
    if mode == "conditional":
        approval["security"]["defaultAction"] = "require_approval"
    if mode != "none":
        approval["presentation"] = {"title": "审批工具", "description": "说明", "inputs": []}
        approval["execution"] = {
            "mode": "approval_gate",
            "onApproved": "execute_original_call",
            "onRejected": "cancel_call",
        }
    return approval


class MtapPolicyTest(unittest.TestCase):
    """覆盖 MTAP schema 结构和 JSON Schema 难以表达的唯一性规则。"""

    def test_accepts_none_required_and_conditional_modes(self) -> None:
        """验证三种合法审批模式均能通过定义校验。

        参数：无。
        返回值：无；不支持的合法模式会导致断言失败。
        """
        for mode in ("none", "required", "conditional"):
            with self.subTest(mode=mode):
                policy = get_tool_policy(_sample_tool(_base_approval(mode)))
                self.assertEqual(policy["security"]["approvalMode"], mode)
                self.assertEqual(policy["mcp_server"], "test")

    def test_rejects_invalid_mode_requirements_and_execution_semantics(self) -> None:
        """验证模式条件、执行门禁常量及额外字段均受 Schema 约束。

        参数：无。
        返回值：无；非法定义未被拒绝时断言失败。
        """
        cases = []
        conditional = _base_approval("conditional")
        del conditional["security"]["defaultAction"]
        cases.append(conditional)
        none_with_form = _base_approval("none")
        none_with_form["presentation"] = {"title": "x", "description": "y", "inputs": []}
        cases.append(none_with_form)
        invalid_execution = _base_approval()
        invalid_execution["execution"]["onApproved"] = "edit_arguments"
        cases.append(invalid_execution)
        unknown_field = _base_approval()
        unknown_field["unexpected"] = True
        cases.append(unknown_field)
        for approval in cases:
            with self.subTest(approval=approval), self.assertRaises(ValueError):
                get_tool_policy(_sample_tool(approval))

    def test_rejects_duplicate_input_keys_and_option_values(self) -> None:
        """验证额外语义检查拒绝重复表单键和候选值。

        参数：无。
        返回值：无；重复项未拒绝时断言失败。
        """
        duplicated_key = _base_approval()
        duplicated_key["presentation"]["inputs"] = [
            {"key": "choice", "type": "single_select", "label": "A", "required": False,
             "options": [{"value": "x", "label": "X"}]},
            {"key": "choice", "type": "text", "label": "B", "required": False},
        ]
        duplicated_value = _base_approval()
        duplicated_value["presentation"]["inputs"] = [
            {"key": "choice", "type": "single_select", "label": "A", "required": False,
             "options": [{"value": "x", "label": "X"}, {"value": "x", "label": "Y"}]},
        ]
        for approval in (duplicated_key, duplicated_value):
            with self.subTest(approval=approval), self.assertRaises(ValueError):
                get_tool_policy(_sample_tool(approval))


class ApprovalAnswersTest(unittest.TestCase):
    """覆盖四种表单控件、必填语义和答案边界。"""

    def setUp(self) -> None:
        """设置包含全部控件类型的审批请求快照。

        参数：无。
        返回值：无。
        """
        self.payload = {"inputs": [
            {"key": "confirm", "type": "confirm", "required": True, "mustBeTrue": True},
            {"key": "one", "type": "single_select", "required": True,
             "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]},
            {"key": "many", "type": "multi_select", "required": False,
             "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]},
            {"key": "note", "type": "text", "required": True, "maxLength": 5},
        ]}

    def test_accepts_valid_answers_for_all_control_types(self) -> None:
        """验证 confirm、单选、多选和文本答案的合法形状。

        参数：无。
        返回值：无；有效答案被拒绝时断言失败。
        """
        validate_approval_answers(self.payload, {
            "confirm": True, "one": "b", "many": ["a", "b"], "note": "好的",
        })

    def test_rejects_missing_invalid_unknown_and_duplicate_answers(self) -> None:
        """验证必填、类型、候选范围、长度、未知键和多选去重规则。

        参数：无。
        返回值：无；非法答案未被拒绝时断言失败。
        """
        valid = {"confirm": True, "one": "a", "many": [], "note": "好"}
        cases = [
            {**valid, "confirm": False},
            {**valid, "one": "unknown"},
            {**valid, "many": ["a", "a"]},
            {**valid, "many": ["other"]},
            {**valid, "note": "123456"},
            {**valid, "extra": True},
            {key: value for key, value in valid.items() if key != "one"},
            {**valid, "note": "   "},
        ]
        for answers in cases:
            with self.subTest(answers=answers), self.assertRaises(HTTPException) as raised:
                validate_approval_answers(self.payload, answers)
            self.assertEqual(raised.exception.status_code, 422)

    def test_optional_empty_and_rejected_decision_answers_are_supported(self) -> None:
        """确认答案校验器允许可选字段缺省及拒绝决定不提供答案。

        参数：无。
        返回值：无；合法的空答案不应抛出异常。
        """
        validate_approval_answers({"inputs": []}, {})
        empty = {"inputs": [{"key": "note", "type": "text", "required": False}]}
        validate_approval_answers(empty, {})


if __name__ == "__main__":
    unittest.main()
