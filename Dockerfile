# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS base

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first for better layer caching.
COPY pyproject.toml ./
COPY timenotes_mcp ./timenotes_mcp
COPY README.md ./

RUN uv pip install --system --no-cache ".[http]"

# Persistent state lives here (SQLite + encryption key) — mount a volume on it.
ENV TIMENOTES_MCP_STATE_DIR=/data \
    TIMENOTES_MCP_TRANSPORT=http \
    TIMENOTES_MCP_HOST=0.0.0.0 \
    TIMENOTES_MCP_PORT=8765

VOLUME ["/data"]
EXPOSE 8765

# Healthcheck — useful for Portainer's status indicator.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)" \
        || exit 1

ENTRYPOINT ["timenotes-mcp"]
