FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 将源码、数据库定义和同步工具一并放入镜像。
COPY pyproject.toml ./
COPY build_meta.json ./build_meta.json
COPY src ./src
COPY models ./models
COPY db ./db
COPY scripts ./scripts
COPY entrypoint.sh ./entrypoint.sh

# 使用国内 PyPI 镜像安装依赖，避免构建时拉取官方源超时。
# 构建阶段不连接数据库；表结构同步由 entrypoint.sh 在容器启动时执行。
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple/ .

EXPOSE 9090

ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["uvicorn", "k8s_ops_agent.server:app", "--host", "0.0.0.0", "--port", "9090"]
