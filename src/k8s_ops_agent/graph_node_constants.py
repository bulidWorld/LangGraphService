"""集中定义运维主图和 A2A 子图的节点名称。"""

from typing import Final

from langgraph.graph import END as END, START as START

ROUTE_TURN: Final = "route_turn"
CONTEXT_COMPRESSION: Final = "context_compression"
ASSISTANT: Final = "assistant"
TOOLS: Final = "tools"
ASSESS_POD: Final = "assess_pod"
APPROVE_POD_HANDOFF: Final = "approve_pod_handoff"
FINALIZE: Final = "finalize"
A2A: Final = "a2a"
A2A_FALLBACK: Final = "a2a_fallback"
SUBMIT_TASK: Final = "submit_task"
COLLECT_TASK_EVENTS: Final = "collect_task_events"
