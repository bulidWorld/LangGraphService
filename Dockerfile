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

# 构建阶段连接数据库，扫描 db 目录下全部 XML 定义并同步表结构。
# DB_* 通过 --build-arg 传入，只用于本次 RUN，不会写入最终镜像。
ARG DB_HOST
ARG DB_PORT
ARG DB_NAME
ARG DB_USER
ARG DB_PASSWORD
RUN pip install --no-cache-dir . \
    && python scripts/sync_schema.py

EXPOSE 9090

CMD ["uvicorn", "langgraph.service:app", "--host", "0.0.0.0", "--port", "9090"]
