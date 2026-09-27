# AgentCore A2A containers must be linux/arm64 and listen on 0.0.0.0:9000.
# Build only when you are ready to deploy:
#   docker build --platform linux/arm64 -f runtimes/agentcore/Dockerfile -t a2a-tracker .
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY agent ./agent
COPY a2a_server ./a2a_server

ENV A2A_TARGET=agentcore \
    A2A_HOST=0.0.0.0 \
    A2A_PORT=9000 \
    A2A_DB_PATH=/mnt/efs/a2a_tasks.db \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 9000
CMD ["python", "-m", "a2a_server"]
