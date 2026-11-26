"""将 LangGraph agent 事件转换为包含活动时间线的 AG-UI 事件流。"""

from collections.abc import AsyncGenerator

from ag_ui.core.types import RunAgentInput
from ag_ui.encoder import EventEncoder
from ag_ui_langgraph import LangGraphAgent

from .handlers import EVENT_HANDLERS
from .state import ActivityState


async def event_generator(
    input_data: RunAgentInput,
    normalized_input: RunAgentInput,
    request_agent: LangGraphAgent,
    encoder: EventEncoder,
) -> AsyncGenerator[str, None]:
    """运行请求对应的 LangGraph agent，并将活动及原始事件编码为 SSE。

    参数：
        input_data: 原始请求，用于标识活动所属的 thread 和 run。
        normalized_input: 已校验并规范化的运行输入，用于执行 agent。
        request_agent: 当前请求独占的 agent 实例。
        encoder: 与 HTTP 响应媒体类型匹配的 AG-UI 事件编码器。
    返回值：逐条产生已编码事件文本的异步生成器。
    """
    state = ActivityState(input_data)
    async for event in request_agent.run(normalized_input):
        handler = EVENT_HANDLERS.get(getattr(event, "type", None))
        outgoing = handler.handle(event, state) if handler is not None else (event,)
        for output in outgoing:
            yield encoder.encode(output)
