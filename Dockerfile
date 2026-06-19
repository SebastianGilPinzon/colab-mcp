# syntax=docker/dockerfile:1.7
#
# Dockerfile for colab-mcp — MCP server for controlling Google Colab.
#
# This image is primarily intended for Glama (https://glama.ai/mcp/servers)
# introspection: it must start cleanly and respond to MCP ListTools requests
# over stdio. At runtime the server can also open a Colab browser tab via
# webbrowser.open_new() — that part requires a host browser and is not
# exercised inside the container.
#
# Build:  docker build -t colab-mcp .
# Run:    docker run --rm -i colab-mcp           (stdio MCP server)
# Probe:  echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
#           | docker run --rm -i colab-mcp

FROM python:3.13-slim

# Install uv (the project's package/run manager — pip-installed uv is
# explicitly NOT supported per README, but in a fresh container that
# rule is about the host's uv, not the container build itself).
RUN pip install --no-cache-dir uv==0.5.11

WORKDIR /app

# Copy lock files first for better layer caching, then install deps.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Sync dependencies into a project-local .venv. --no-dev keeps the image
# small (no pytest etc.); --frozen ensures uv.lock is authoritative.
RUN uv sync --frozen --no-dev

# colab-mcp speaks JSON-RPC over stdio (the MCP standard transport).
# Glama introspects by piping a tools/list request into stdin.
ENTRYPOINT ["uv", "run", "--no-dev", "colab-mcp"]
