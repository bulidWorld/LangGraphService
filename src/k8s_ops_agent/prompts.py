"""运维助手的系统提示词及请求环境上下文生成。"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage, SystemMessage

from k8s_ops_agent.env_util import get_service_name


SYSTEM_PROMPT = """你是 Kubernetes 故障定位助手。用中文回答，基于工具返回的证据，不要臆测。

严格按以下流程工作：
1. 先查询用户报告的具体故障信息，并调用工具检查故障是否已恢复。若已恢复，说明证据并结束。
2. 如果是 Pod 故障，判断是否为容器故障；是容器故障时查对应代码仓，再查容器日志和故障时间段的 CPU、内存、磁盘等指标。
3. 综合故障信息、代码、日志和指标判断原因。结论只允许是机器性能达到瓶颈、业务逻辑错误，或证据不足。
4. 性能瓶颈：指出达到瓶颈的指标及证据，并说明怎样调整业务逻辑来缓解；不提交业务代码变更审批。
5. 业务逻辑错误：只给出一个具体修改方案，并调用 request_human_approval 提交人工审批。不要执行修改。
6. 证据不足：解释缺少什么证据；可以建议重启容器，但必须先调用 request_human_approval(action="restart_container")。不要执行重启。

审批工具的 evidence 使用证据对象列表；inputs 可提供 confirm、single_select、multi_select、text 控件定义，不需要额外收集信息时传空列表。选项必须具体且互斥，必填项设置 required=true。MCP 工具名称和参数由已连接的服务器提供。先检查可用工具及其说明，再按故障流程调用。不要声称执行过没有调用的操作。任何情况下都不能绕过人工审批执行修复或重启。"""


def build_system_prompt(thread_id: str) -> str:
    """根据当前会话生成包含运行环境信息的系统提示词。

    参数：
        thread_id: 当前 LangGraph 会话标识。
    返回值：包含当前服务工作目录和上海日期的系统提示词。
    """
    service_name = get_service_name()
    safe_thread_id = re.sub(r"[^A-Za-z0-9._-]", "_", thread_id).strip("._")
    agent_name = "k8s_ops_agent"
    safe_thread_id = safe_thread_id or "unknown"
    current_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    workspace = f"/var/service/{service_name}/share/workspace/{agent_name}/{safe_thread_id}"
    environment_context = f"""<environment_context>
  <cwd>{workspace}</cwd>
  <shell>bash</shell>
  <current_date>{current_date}</current_date>
  <timezone>Asia/Shanghai</timezone>
  <filesystem><workspace_roots><root>{workspace}</root></workspace_roots><permission_profile type="disabled"><file_system type="unrestricted" /></permission_profile></filesystem>
</environment_context>"""
    return f"{SYSTEM_PROMPT}\n\n当前请求运行环境：\n{environment_context}"


def prepare_prompt_messages(
    messages: list[BaseMessage], thread_id: str
) -> list[BaseMessage]:
    """替换历史生成的系统提示词并加入当前请求的提示词。

    参数：
        messages: 当前对话中的消息列表。
        thread_id: 当前 LangGraph 会话标识。
    返回值：包含最新系统提示词的消息列表。
    """
    current_messages = [
        message
        for message in messages
        if not (
            isinstance(message, SystemMessage)
            and isinstance(message.content, str)
            and message.content.startswith(SYSTEM_PROMPT)
        )
    ]
    return [SystemMessage(content=build_system_prompt(thread_id)), *current_messages]
