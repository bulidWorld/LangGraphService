# 数据库设计

数据库采用 PostgreSQL，LangGraph 自身继续管理 checkpoint、checkpoint writes 和
其迁移。本目录管理应用自己的业务表，避免复制 LangGraph 内部表结构。

## 表

- `incident_runs`：一次排障会话的业务索引，通过 `thread_id` 与 LangGraph
  checkpoint 关联，并保存集群、命名空间、工作负载、状态和摘要。
- `approval_requests`：版本化人工审批提案、结构化表单、最终决定和恢复占用信息，
  关联到排障会话。该服务仅记录审批状态，不执行变更或重启。

引用关系按项目规范使用 `ref-table`、`ref-col` 描述，不创建数据库外键。
状态使用 `varchar` 保存，由应用层校验；不生成数据库 `CHECK` 约束。两个表的
主键均为自增 `integer`。`approval_requests.request_id` 唯一标识审批请求，
`decision` 保存通过 AG-UI 请求校验的审批决定，`reviewer` 字段为兼容历史结构保留且审批回传不写入审核人身份，`resume_run_id` 防止其他 run 重复恢复。

AG-UI 对话消息和 LangGraph 流程状态由 Postgres checkpointer 存在 LangGraph
管理的 checkpoint 表中，不在上述业务表重复保存。生产环境配置数据库后，服务启动时
会执行 checkpointer 初始化；本地未配置数据库时使用内存 checkpointer，进程退出后会话
历史不会保留。

## 同步

源数据定义文件使用“编号_业务名称”命名，业务表名保持稳定，避免影响已有 SQL 和数据。

设置 `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD` 后执行
`python scripts/sync_schema.py`；脚本会按文件名顺序扫描 `db/` 目录下的全部 XML
定义文件并生成、执行对应 SQL，新增定义文件无需修改脚本。只生成 SQL 可执行
`python scripts/sync_schema.py --sql-only`，只校验不连接数据库可执行
`python scripts/sync_schema.py --check`。Docker 构建阶段不连接数据库；容器
启动时由 `entrypoint.sh` 在配置了 `DB_*` 环境变量的情况下执行同步，数据库
不可用或定义有误时容器启动失败，可避免带病服务进入生产环境。
