"""运维助手的系统提示词及请求环境上下文生成。"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage, SystemMessage

from k8s_ops_agent.env_util import get_service_name


SYSTEM_PROMPT = """你是 Kubernetes 故障定位助手。用中文回答，基于工具返回的证据，不要臆测。

严格按以下流程工作：
1. 故障结合容器日志和故障时间段的 CPU、内存、磁盘等指标，如果没有问题，要明确说明。
2. 综合故障信息、代码、日志和性能指标判断原因。结论只允许是机器性能达到瓶颈、业务逻辑错误，或证据不足。
3. 性能瓶颈：指出达到瓶颈的指标及证据，并说明怎样调整业务逻辑来缓解；不提交业务代码变更审批。
4. 业务逻辑错误：可以给出一个具体修改方案。
5. 如果分析不出问题原因，解决方案，可以建议重启容器。

## 磁盘性能分析指南：
通过PromQL查询这些指标来分析磁盘性能，包括： IOPS、吞吐量、读写延迟、IO 队列深度、磁盘繁忙程度等。
指标信息：
| 指标         | node-exporter | Prometheus 指标                              |
| ---------- | ------------- | ------------------------------------------ |
| 读 IOPS     |支持      | `node_disk_reads_completed_total`          |
| 写 IOPS     |支持      | `node_disk_writes_completed_total`         |
| 读吞吐量       |支持      | `node_disk_read_bytes_total`               |
| 写吞吐量       |支持      | `node_disk_written_bytes_total`            |
| 平均读延迟      |可计算     | `node_disk_read_time_seconds_total`        |
| 平均写延迟      |可计算     | `node_disk_write_time_seconds_total`       |
| 平均在途 IO 数  |可计算     | `node_disk_io_time_weighted_seconds_total` |
| 磁盘繁忙时间占比   |可计算     | `node_disk_io_time_seconds_total`          |

其中 IOPS、吞吐量、延迟等需要通过 PromQL 对累计计数器进行速率计算。

## 特别注意：
- 先检查可用工具及其说明，再按故障流程调用。不要声称执行过没有调用的操作
"""


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
