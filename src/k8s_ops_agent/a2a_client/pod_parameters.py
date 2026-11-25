"""从本轮诊断提取 A2A 编码任务所需的 Pod 参数。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_openai import ChatOpenAI
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from k8s_ops_agent.observability import logger
from k8s_ops_agent.prompts import POD_ASSESSMENT_PROMPT, POD_LOOKUP_PROMPT

MAX_POD_LOOKUP_CALLS = 4


class PodAssessment(BaseModel):
    """从本轮诊断上下文提取的 Pod 故障及源码信息。"""

    involves_pod: bool = Field(description="本轮实际发现的问题是否涉及 Pod")
    repository: str | None = Field(
        default=None, description="故障 Pod 对应的 Git 源码仓库克隆地址，缺失或不确定时为 null"
    )
    tag: str | None = Field(
        default=None, description="故障 Pod 对应的 Git 源码 tag 名称，缺失或不确定时为 null"
    )


async def extract_pod_parameters(
    messages: Sequence[BaseMessage],
    model_factory: Callable[[], ChatOpenAI],
    *,
    load_tools: Callable[[], Awaitable[Sequence[BaseTool]]] | None = None,
    execute_tool: Callable[[ToolCall], Awaitable[list[ToolMessage]]] | None = None,
) -> tuple[str, str, str] | None:
    """结合本轮诊断和证据提取故障 Pod 的源码信息。

    参数：
        messages: 包含最终诊断和历史证据的消息序列。
        model_factory: 创建用于结构化提取的模型的函数。
        load_tools: 缺少源码信息时加载可用查询工具的函数。
        execute_tool: 执行单个查询工具并返回结果消息的函数。
    返回值：诊断文本、源码仓库地址和 Git tag。无法生成 Pod 编码任务时返回 None。
    """
    if not messages:
        return None
    last_message = messages[-1]
    diagnosis = last_message.text.strip() if isinstance(last_message, AIMessage) else ""
    if not diagnosis:
        return None
    evidence: list[dict[str, str]] = []
    for message in reversed(messages[:-1]):
        if isinstance(message, HumanMessage):
            evidence.append({"source": "user", "text": message.text})
            break
        if isinstance(message, ToolMessage) and message.status != "error":
            evidence.append({"source": "tool", "text": message.text})
    evidence.reverse()
    model = model_factory()
    assessor = model.with_structured_output(PodAssessment)
    assessment = await assessor.ainvoke(
        _assessment_messages(diagnosis, evidence), config={"tags": ["nostream"]}
    )
    if not assessment.involves_pod:
        return None
    repository = (assessment.repository or "").strip()
    tag = (assessment.tag or "").strip().removeprefix("refs/tags/")
    if (not repository or not tag) and load_tools is not None and execute_tool is not None:
        extra_evidence = await _lookup_pod_evidence(
            model, diagnosis, evidence, await load_tools(), execute_tool
        )
        if extra_evidence:
            assessment = await assessor.ainvoke(
                _assessment_messages(diagnosis, evidence + extra_evidence),
                config={"tags": ["nostream"]},
            )
            if not assessment.involves_pod:
                return None
            repository = (assessment.repository or "").strip()
            tag = (assessment.tag or "").strip().removeprefix("refs/tags/")
    if not repository or not tag:
        logger.info(
            "Pod 诊断缺少源码仓库或 Git tag，未生成 A2A 编码任务",
            extra={
                "log_type": "business",
                "has_repository": bool(repository),
                "has_git_tag": bool(tag),
            },
        )
        return None
    return diagnosis, repository, tag


def _assessment_messages(
    diagnosis: str, evidence: list[dict[str, str]]
) -> list[BaseMessage]:
    """构建 Pod 评估模型的输入。

    参数：
        diagnosis: 本轮诊断正文。
        evidence: 按时间排列的用户消息和工具证据。
    返回值：包含系统提示和待分析数据的消息列表。
    """
    return [
        SystemMessage(content=POD_ASSESSMENT_PROMPT),
        HumanMessage(content=json.dumps(
            {"diagnosis": diagnosis, "evidence": evidence}, ensure_ascii=False
        )),
    ]


async def _lookup_pod_evidence(
    model: ChatOpenAI,
    diagnosis: str,
    evidence: list[dict[str, str]],
    tools: Sequence[BaseTool],
    execute_tool: Callable[[ToolCall], Awaitable[list[ToolMessage]]],
) -> list[dict[str, str]]:
    """在有限的工具调用次数内补充 Pod 源码版本证据。

    参数：
        model: 生成工具调用的模型。
        diagnosis: 本轮诊断正文。
        evidence: 首次评估使用的证据。
        tools: 可用于补查的工具。
        execute_tool: 执行工具并返回结果消息的函数。
    返回值：补查得到的成功工具结果。
    """
    if not tools:
        return []
    lookup = model.bind_tools(tools, parallel_tool_calls=False)
    messages = _assessment_messages(diagnosis, evidence)
    messages[0] = SystemMessage(content=POD_LOOKUP_PROMPT)
    allowed_names = {tool.name for tool in tools}
    seen_calls: set[str] = set()
    extra_evidence: list[dict[str, str]] = []
    for _ in range(MAX_POD_LOOKUP_CALLS):
        response = await lookup.ainvoke(messages, config={"tags": ["nostream"]})
        if not isinstance(response, AIMessage) or len(response.tool_calls) != 1:
            break
        call = response.tool_calls[0]
        signature = json.dumps([call["name"], call["args"]], sort_keys=True)
        if not call.get("id") or call["name"] not in allowed_names or signature in seen_calls:
            break
        seen_calls.add(signature)
        results = await execute_tool(call)
        messages.extend([response, *results])
        extra_evidence.extend(
            {"source": "tool", "text": result.text}
            for result in results
            if result.status != "error"
        )
    return extra_evidence
