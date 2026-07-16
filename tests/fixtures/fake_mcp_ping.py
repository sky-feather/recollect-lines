#!/usr/bin/env python3
"""Minimal MCP stdio server for install verification tests.

Responds to initialize with the recollect-lines-mcp server name and exits
cleanly on stdin close. No broker, network, or credentials required.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    line = sys.stdin.readline()
    if not line:
        return 0
    request = json.loads(line)
    if request.get("method") == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "recollect-lines-mcp", "version": "0.1.0"},
            },
        }
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
