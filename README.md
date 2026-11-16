# Kubernetes Ops AG-UI Agent

This project serves the `k8s_ops_agent` LangGraph workflow through the AG-UI
protocol. FastAPI and Uvicorn host the server; `ag-ui-langgraph` translates
LangGraph stream events to AG-UI events. The protocol endpoint is `POST /agent`
and its health check is `GET /agent/health`. A general service check is at
`GET /health`.

## Configure and run

Use Python 3.11 or newer:

```sh
pip install -e .
export OPENAI_API_KEY="..."
export MCP_BEARER_TOKEN="..."
uvicorn k8s_ops_agent.server:app --host 0.0.0.0 --port 2024
```

Set `OPENAI_MODEL` to override the default `gpt-4o-mini`. MCP server endpoints
and connection options are read from the project-root `mcp_server.json`, using
the `MultiServerMCPClient` format from `langchain-mcp-adapters`. Put the token
used by its `Authorization` header in `MCP_BEARER_TOKEN`; the JSON file uses
`${MCP_BEARER_TOKEN}` as a placeholder. Configure MCP servers that provide
fault lookup, recovery checks, Kubernetes details, repository inspection, logs,
and metrics in that file.
Set `CORS_ORIGINS` to a comma-separated list of frontend origins when the web
client is hosted on a different origin. Localhost ports `3000` and `5173` are
allowed by default.

## Logs and Langfuse

The service writes newline-delimited JSON logs to stdout. Inbound requests use
`log_type=access`; model and MCP tool requests use `log_type=http`; uncaught
failures use `log_type=exception`. Each request receives a trace ID that is
included in its logs and Langfuse trace metadata. Model and MCP tool operations
are nested spans within the request trace.

To enable Langfuse, configure `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`.
For a self-hosted installation, also set `LANGFUSE_BASE_URL` (for example,
`http://langfuse:3000`). Tracing is disabled automatically when credentials are
not configured; the service continues to log to stdout.

Langfuse spans are created in the async request flow and exported by the SDK's
background batch processor, so requests do not wait for Langfuse network calls.
If the SDK is disabled or span creation/export fails, the service records a
warning or exception log and keeps handling requests.

The AG-UI endpoint accepts a `RunAgentInput` JSON body and streams protocol
events using server-sent events. For example, an AG-UI client should point its
agent URL to `http://localhost:2024/agent` and send the thread and message data
using the AG-UI client SDK. Use the same `threadId` to continue a conversation
or resume an interrupted approval.

## Checkpoint storage

Set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` separately in
production, and set `APP_ENV=production`. The server safely builds PostgreSQL
connection info from those values, opens a psycopg async connection pool, and
runs LangGraph checkpoint setup at startup, using PostgreSQL to retain threads
and interrupt state across restarts. The container entrypoint synchronizes the
application schema from `db/` at startup before the pool opens (see Docker
below). `POSTGRES_POOL_MAX_SIZE`
controls the pool size (default `10`). The service exits at startup in
production if any database setting is missing.

When `APP_ENV` is not `production` and database settings are omitted, the server uses
an in-memory checkpointer for local development. Those threads disappear when
the process stops.

The application schema is defined by all XML files in `db/`, processed in filename
order. Run `python scripts/sync_schema.py` to synchronize manually, or pass
`--sql-only` to print the generated SQL and `--check` to validate without a
database. The Docker build does not touch the database; `entrypoint.sh`
synchronizes the schema when the container starts (see Docker below).
`incident_runs` indexes LangGraph threads, while `approval_requests` stores
versioned proposals, validated decisions, and resume claims. Conversation messages
and graph state are persisted by LangGraph's Postgres checkpointer in its own
checkpoint tables; they are not duplicated in the application business tables.

## Docker

```sh
docker build -t k8s-ops-agent .
docker run --env-file .env -p 9090:9090 k8s-ops-agent
```

When all five `DB_*` settings are present, `entrypoint.sh` runs
`scripts/sync_schema.py` before starting the server; with none set it skips the
sync and the server uses the in-memory checkpointer for local development. A
partial set fails the sync and the container exits.

Provide `OPENAI_API_KEY`, `APP_ENV=production`, all five `DB_*` settings, and
`MCP_BEARER_TOKEN` in the deployment environment. Store database passwords and
MCP tokens in the deployment secret manager. The image includes
`mcp_server.json`; PostgreSQL and the MCP servers must be reachable from the
container.

## Kubernetes

`deploy/kubernetes.yaml` declares the MCP ConfigMap, agent Deployment, and
ClusterIP Service. Build and push the image, update the Deployment image, then
create the Secret and apply the manifest:

```sh
kubectl create secret generic langgraph-k8s-ops-agent-secrets \
  --from-literal=OPENAI_API_KEY='replace-me' \
  --from-literal=MCP_BEARER_TOKEN='replace-me' \
  --from-literal=DB_HOST='postgres.example.internal' \
  --from-literal=DB_PORT='5432' \
  --from-literal=DB_NAME='langgraph' \
  --from-literal=DB_USER='langgraph' \
  --from-literal=DB_PASSWORD='replace-me'
kubectl apply -f deploy/kubernetes.yaml
```

The ConfigMap contains the non-secret MCP connection configuration and is
mounted at `/app/mcp_server.json`. Add another named object beside
`operator-agent-service` in that JSON object to configure another MCP server;
each entry accepts the `MultiServerMCPClient` connection format. Keep tokens in
the Secret and refer to them with `${MCP_BEARER_TOKEN}` in the ConfigMap. The
loader resolves `${ENV_VAR}` placeholders, so servers with different tokens
can use separate Secret keys and matching environment variables in the
Deployment. After changing the ConfigMap, restart the Deployment so the process
reloads its cached MCP configuration:

```sh
kubectl rollout restart deployment/langgraph-k8s-ops-agent
```

The Service exposes port `2024` inside the cluster. Configure an Ingress or
Gateway separately if the AG-UI endpoint needs external access. PostgreSQL and
the configured MCP services must be reachable from the agent pods.

## Human approval

The graph uses a LangGraph interrupt through `request_human_approval` for a
proposed business fix or container restart. It does not perform those actions.
The interrupt payload describes confirm, single-select, multi-select, and text
inputs. The reviewer submits a decision through `POST /agent` as an AG-UI
`ResumeEntry` on the same thread. For an approved or rejected decision, use
`status: "resolved"` and provide `request_id`, decision `status`, `answers`, and
optional `comment` in the payload. To cancel an approval, use
`status: "cancelled"` without a payload. The server validates the active
interrupt, reviewer identity and role, and form answers, then saves the decision
and claims the resume before continuing the graph. Reviewer identity must be
supplied by trusted authentication middleware as `request.state.reviewer_id` or
an authenticated ASGI principal; the service does not trust a reviewer identity
from the AG-UI payload. MCP approval roles must also be supplied by trusted
middleware as `request.state.reviewer_roles` or `principal.roles`. Operational
MCP tools should be read-only.

The investigation agent expects configured MCP tools to describe their
purpose and input fields clearly. It follows the steps in `context/design.md`
and bases its diagnosis on the returned evidence.

