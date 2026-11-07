#!/bin/sh
# Prepare the plugin's Python environment. The first run downloads TradingAgents
# and its dependencies (about a minute); later runs are a quick no-op. Never
# blocks the session on failure: the MCP server's own `uv run` retries the sync.
cd "$(dirname "$0")/.." || exit 0
if ! command -v uv >/dev/null 2>&1; then
  echo '{"systemMessage":"tradingagents plugin: uv is not installed, so its MCP server cannot start. Install it from https://docs.astral.sh/uv/ and restart Claude Code."}'
  exit 0
fi
first_run=0
[ -x .venv/bin/python ] || first_run=1
if uv sync --frozen --no-dev --quiet >/dev/null 2>&1; then
  if [ "$first_run" = 1 ]; then
    echo '{"systemMessage":"tradingagents plugin: Python environment installed (first run only). If its tools are missing in this session, run /mcp and reconnect tradingagents, or restart Claude Code."}'
  fi
else
  echo '{"systemMessage":"tradingagents plugin: could not install its Python environment (uv sync failed). Check your network, then restart Claude Code."}'
fi
exit 0
