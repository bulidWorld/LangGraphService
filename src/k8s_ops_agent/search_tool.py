"""提供当前 LangGraph 会话工作区内的模糊文件内容搜索工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from rapidfuzz import fuzz, process

from k8s_ops_agent.read_tool import _workspace_path


@tool
def search_file_content(
    path: str,
    query: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
    context_lines: int = 3,
    max_results: int = 20,
) -> str:
    """在当前会话 workspace 的文本文件中模糊搜索并返回行号及上下文。

    参数：
        path: workspace 内文件的相对路径。
        query: 要搜索的文本，匹配时忽略大小写。
        config: LangGraph 注入的运行配置，包含当前 thread_id。
        context_lines: 每个命中行前后包含的上下文行数，范围为 0 至 20。
        max_results: 最多返回的命中行数，范围为 1 至 100。
    返回值：带行号的匹配结果和上下文；出错或无匹配时返回说明文本。
    """
    thread_id = str(config.get("configurable", {}).get("thread_id", ""))
    if not thread_id:
        return "搜索失败：当前运行缺少会话标识。"
    if not query.strip():
        return "搜索失败：搜索内容不能为空。"
    if not 0 <= context_lines <= 20:
        return "搜索失败：context_lines 必须在 0 到 20 之间。"
    if not 1 <= max_results <= 100:
        return "搜索失败：max_results 必须在 1 到 100 之间。"

    workspace = _workspace_path(thread_id).resolve()
    requested = Path(path)
    if requested.is_absolute():
        return "搜索失败：path 只能是 workspace 内的相对路径。"
    candidate = workspace / requested
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace)
    except (OSError, ValueError):
        return "搜索失败：文件不存在，或路径不在当前会话 workspace 内。"
    if not resolved.is_file():
        return "搜索失败：目标不是普通文件。"
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "搜索失败：文件无法作为 UTF-8 文本读取。"

    matches = process.extract(
        query.casefold(),
        [line.casefold() for line in lines],
        scorer=fuzz.WRatio,
        processor=None,
        score_cutoff=75,
        limit=max_results,
    )
    if not matches:
        return f"未找到与 {query!r} 相似度不低于 75% 的行。"

    hit_indexes = sorted({index for _, _, index in matches})
    ranges: list[list[int]] = []
    for index in hit_indexes:
        start = max(0, index - context_lines)
        end = min(len(lines) - 1, index + context_lines)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])

    hit_set = set(hit_indexes)
    output = [f"文件：{resolved}", f"命中行数：{len(hit_indexes)}（最多返回 {max_results} 行）"]
    for start, end in ranges:
        output.append("---")
        for index in range(start, end + 1):
            marker = "匹配" if index in hit_set else "上下文"
            output.append(f"{index + 1:>6} [{marker}] {lines[index]}")
    return "\n".join(output)
