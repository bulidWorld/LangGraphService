FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 将源码、数据库定义和同步工具一并放入镜像。
COPY pyproject.toml README.md ./
COPY mcp_server.json ./mcp_server.json
COPY src ./src
COPY db ./db
COPY scripts ./scripts

# 构建阶段不连接运行数据库，只校验定义并生成可审查的 SQL。
RUN pip install --no-cache-dir .
RUN python scripts/sync_schema.py --check \
    && python scripts/sync_schema.py --sql-only > /app/db/001_故障排查业务数据定义.sql

EXPOSE 9090

CMD ["uvicorn", "langgraph.service:app", "--host", "0.0.0.0", "--port", "9090"]
