"""独立运行的 A2A 任务生命周期子图。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph
from typing_extensions import TypedDict

from k8s_ops_agent import graph_node_constants as nodes
from k8s_ops_agent.a2a_client.client import A2AClient
from k8s_ops_agent.observability import logger


class A2ATaskState(TypedDict, total=False):
    """A2A 子图内部状态及对调用方提供的生命周期数据。"""

    a2a_payload: dict[str, Any]
    a2a_response: dict[str, Any]
    a2a_task: dict[str, Any]
    a2a_task_id: str
    a2a_status: dict[str, Any]
    a2a_events: list[dict[str, Any]]
    a2a_artifacts: list[dict[str, Any]]


A2A_PROGRESS_EVENT = "a2a_task_progress"
A2A_ARTIFACT_EVENT = "a2a_artifact_update"


def _event_kind(event: dict[str, Any]) -> str:
    """返回 A2A 流事件的类别。

    参数：
        event: A2A StreamResponse 字典。
    返回值：task、statusUpdate、artifactUpdate 或 unknown。
    """
    return next((key for key in ("task", "statusUpdate", "status_update", "artifactUpdate", "artifact_update") if key in event), "unknown")


def _artifact_id(artifact: dict[str, Any]) -> str:
    """读取 A2A Artifact 的标识。

    参数：
        artifact: 远端返回的产物对象。
    返回值：产物标识，缺少标识时返回空字符串。
    """
    return artifact.get("artifactId") or artifact.get("artifact_id") or ""


async def _collect_task_events(
    stream: AsyncIterator[dict[str, Any]], state: A2ATaskState, config: RunnableConfig
) -> A2ATaskState:
    """逐条发布远端事件，累计任务状态和产物。

    参数：
        stream: A2A 任务事件流。
        state: 当前子图状态，可能包含先前保存的任务和产物。
        config: LangChain 回调配置，用于发布实时进度。
    返回值：更新后的 A2A 子图状态。
    """
    events = list(state.get("a2a_events", []))
    response = state.get("a2a_response", {})
    task = dict(state.get("a2a_task", {}))
    task_id = state.get("a2a_task_id", "")
    status = dict(state.get("a2a_status", {}))
    artifacts = list(state.get("a2a_artifacts", []))
    artifact_updates: list[dict[str, Any]] = []
    event_count = 0
    logger.info(
        "A2A 任务事件收集开始",
        extra={"log_type": "business", "existing_task_id": task_id, "existing_event_count": len(events)},
    )

    async with aclosing(stream):
        async for event in stream:
            event_count += 1
            if not response:
                response = event
            events.append(event)
            task_update = event.get("task")
            status_update = event.get("statusUpdate") or event.get("status_update")
            artifact_update = event.get("artifactUpdate") or event.get("artifact_update")
            if isinstance(task_update, dict):
                task = task_update
                task_id = task.get("id") or task_id
                status = task.get("status", status)
                artifacts = list(task.get("artifacts", artifacts))
            if isinstance(status_update, dict):
                task_id = status_update.get("taskId") or status_update.get("task_id") or task_id
                status = status_update.get("status", status)
            if isinstance(artifact_update, dict):
                task_id = artifact_update.get("taskId") or artifact_update.get("task_id") or task_id
                artifact = artifact_update.get("artifact")
                if isinstance(artifact, dict):
                    # 合并本次流中的同一 artifact；append=false 用最新快照替换，append=true 追加 parts。
                    update_index = next(
                        (i for i, item in enumerate(artifact_updates)
                         if _artifact_id(item.get("artifact", {})) == _artifact_id(artifact)
                         and _artifact_id(artifact)),
                        None,
                    )
                    if update_index is None:
                        artifact_updates.append(artifact_update)
                    else:
                        previous_update = artifact_updates[update_index]
                        previous_artifact = previous_update.get("artifact", {})
                        if artifact_update.get("append"):
                            merged_artifact = {
                                **previous_artifact, **artifact,
                                "parts": [*previous_artifact.get("parts", []), *artifact.get("parts", [])],
                            }
                            artifact_updates[update_index] = {**previous_update, **artifact_update, "artifact": merged_artifact}
                        else:
                            artifact_updates[update_index] = artifact_update
                    artifact_id = _artifact_id(artifact)
                    index = next(
                        (i for i, item in enumerate(artifacts) if artifact_id and _artifact_id(item) == artifact_id),
                        None,
                    )
                    if index is None:
                        artifacts.append(artifact)
                    else:
                        previous = artifacts[index]
                        if artifact_update.get("append"):
                            artifact = {
                                **previous,
                                **artifact,
                                "parts": [*previous.get("parts", []), *artifact.get("parts", [])],
                            }
                        artifacts[index] = artifact
            # artifactUpdate 和 message 事件不一定携带 TaskStatus；活动 schema 要求每次进度都有状态。
            if not isinstance(status, dict) or "state" not in status:
                status = {"state": "TASK_STATE_WORKING"}
            logger.info(
                "A2A 任务状态已更新",
                extra={
                    "log_type": "business", "task_id": task_id or None,
                    "event_index": event_count, "event_kind": _event_kind(event),
                    "task_state": status.get("state") if isinstance(status, dict) else None,
                    "artifact_count": len(artifacts), "event_keys": sorted(event.keys()),
                },
            )
            if _event_kind(event) != "artifactUpdate":
                await adispatch_custom_event(
                    A2A_PROGRESS_EVENT,
                    {"taskId": task_id, "status": status, "update": event},
                    config=config,
                )

    if artifact_updates:
        merged_updates = []
        for item in artifact_updates:
            merged_updates.append({
                "taskId": item.get("taskId") or item.get("task_id") or task_id,
                **({"contextId": item["contextId"]} if item.get("contextId") else {}),
                "artifact": item["artifact"],
                **({"metadata": item["metadata"]} if item.get("metadata") else {}),
            })
        if len(merged_updates) == 1:
            artifact_payload = merged_updates[0]
        else:
            artifact_payload = {"taskId": task_id, "artifacts": merged_updates}
        await adispatch_custom_event(
            A2A_ARTIFACT_EVENT,
            artifact_payload,
            config=config,
        )

    if task_id:
        task = {**task, "id": task_id, "status": status, "artifacts": artifacts}
    logger.info(
        "A2A 任务事件收集结束",
        extra={
            "log_type": "business", "task_id": task_id or None,
            "task_state": status.get("state") if isinstance(status, dict) else None,
            "event_count": event_count, "total_event_count": len(events),
            "artifact_count": len(artifacts), "response_present": bool(response),
            "empty_stream": event_count == 0,
        },
    )
    return {
        "a2a_response": response,
        "a2a_task": task,
        "a2a_task_id": task_id,
        "a2a_status": status,
        "a2a_events": events,
        "a2a_artifacts": artifacts,
    }


def build_a2a_task_graph(client: A2AClient | None = None) -> Any:
    """构造支持实时进度的 A2A 任务子图。

    参数：
        client: 可注入的 A2A 客户端，缺省时根据环境配置创建。
    返回值：编译后的 LangGraph，可单独调用或作为子图使用。
    """
    async def submit_task(
        state: A2ATaskState, config: RunnableConfig
    ) -> A2ATaskState:
        """提交消息并从同一条 SSE 连接接收任务进度。

        参数：
            state: 包含 a2a_payload 的子图状态。
            config: LangChain 回调配置。
        返回值：远端任务、状态、产物和事件。
        """
        payload = state.get("a2a_payload")
        if not isinstance(payload, dict):
            raise ValueError("A2A 子图状态缺少字典类型的 a2a_payload")
        a2a_client = client if client is not None else A2AClient()
        return await _collect_task_events(
            a2a_client.send_streaming_message(payload), {}, config
        )

    async def collect_task_events(
        state: A2ATaskState, config: RunnableConfig
    ) -> A2ATaskState:
        """恢复已保存订阅节点的任务事件流。

        参数：
            state: 包含已分配任务 ID 的子图状态。
            config: LangChain 回调配置。
        返回值：远端任务、状态、产物和事件。
        """
        a2a_client = client if client is not None else A2AClient()
        return await _collect_task_events(
            a2a_client.subscribe_to_task(state["a2a_task_id"]), state, config
        )

    builder = StateGraph(A2ATaskState)
    builder.add_node(nodes.SUBMIT_TASK, submit_task)
    builder.add_node(nodes.COLLECT_TASK_EVENTS, collect_task_events)
    builder.add_edge(nodes.START, nodes.SUBMIT_TASK)
    builder.add_edge(nodes.SUBMIT_TASK, nodes.END)
    builder.add_edge(nodes.COLLECT_TASK_EVENTS, nodes.END)
    return builder.compile()
