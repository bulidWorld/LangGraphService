"""校验审批人的身份和结构化表单答案。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request


def authenticated_reviewer(request: Request) -> str:
    """从可信认证上下文读取审核人标识。

    参数：
        request: 当前 AG-UI 请求。
    返回值：审核人标识；缺少可信身份时抛出 401。
    """
    configured_id = getattr(request.state, "reviewer_id", None)
    if isinstance(configured_id, str) and configured_id.strip():
        return configured_id.strip()

    principal = request.scope.get("user")
    if principal is not None and getattr(principal, "is_authenticated", False):
        for attribute in ("username", "display_name", "identity"):
            identity = getattr(principal, attribute, None)
            if isinstance(identity, str) and identity.strip():
                return identity.strip()

    raise HTTPException(status_code=401, detail="Authenticated reviewer is required")


def validate_approval_answers(
    request_payload: dict[str, Any], answers: dict[str, Any]
) -> None:
    """依据审批请求声明的控件校验审核人答案。

    参数：
        request_payload: 已持久化的审批请求。
        answers: AG-UI ResumeEntry.payload 中提交的答案。
    返回值：全部答案合法时无返回；否则抛出 422。
    """
    fields = request_payload.get("inputs", [])
    definitions = {field["key"]: field for field in fields}
    unknown_keys = set(answers) - set(definitions)
    if unknown_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown approval answer keys: {sorted(unknown_keys)}",
        )

    for key, definition in definitions.items():
        required = definition.get("required", False)
        if key not in answers:
            if required:
                raise HTTPException(
                    status_code=422, detail=f"Answer {key!r} is required"
                )
            continue

        value = answers[key]
        field_type = definition["type"]
        if field_type == "confirm":
            valid = isinstance(value, bool) and (
                definition.get("mustBeTrue") is not True or value is True
            )
        elif field_type == "single_select":
            options = {option["value"] for option in definition["options"]}
            valid = isinstance(value, str) and value in options
        elif field_type == "multi_select":
            options = {option["value"] for option in definition["options"]}
            valid = (
                isinstance(value, list)
                and all(isinstance(item, str) for item in value)
                and len(value) == len(set(value))
                and set(value).issubset(options)
                and (not required or bool(value))
            )
        elif field_type == "text":
            maximum = definition.get("maxLength", 2000)
            valid = (
                isinstance(value, str)
                and len(value) <= maximum
                and (not required or bool(value.strip()))
            )
        else:
            valid = False

        if not valid:
            raise HTTPException(status_code=422, detail=f"Invalid answer for {key!r}")
