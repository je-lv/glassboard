#!/usr/bin/env python3
"""Measure an MCP client's tool-call timeout (to choose a safe `wait` for board_sync).

A throwaway MCP server, NOT part of Glassboard: one tool `probe_wait(seconds)` that sleeps and then
answers. It binds 127.0.0.1 only and needs the optional `mcp` extra. Point Claude Code or Codex at
http://127.0.0.1:<port>/mcp, ask for waits of increasing length, and note the longest that
returns. A safe MCP `wait` is then min(20, longest_ok - 10) seconds; Glassboard caps it at 20.

    .venv-mcp/bin/python scripts/mcp-timeout-probe.py [--port 8000]
"""
import argparse
import asyncio
import sys
import time

import uvicorn
from mcp.server.mcpserver import MCPServer

MAX_WAIT_S = 600


def build() -> MCPServer:
    server = MCPServer(name="gb-timeout-probe")

    @server.tool(name="probe_wait", description="Sleep for `seconds` (1-600), then report how long it waited.")
    async def probe_wait(seconds: int) -> str:
        seconds = max(1, min(int(seconds), MAX_WAIT_S))
        t0 = time.monotonic()
        await asyncio.sleep(seconds)
        line = f"waited {time.monotonic() - t0:.1f} s"
        print(f"probe_wait({seconds}) completed: {line}", file=sys.stderr, flush=True)
        return line

    return server


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    app = build().streamable_http_app(stateless_http=True, json_response=True, host="127.0.0.1")
    print(f"probe on http://127.0.0.1:{a.port}/mcp (loopback only); Ctrl-C to stop", file=sys.stderr)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
