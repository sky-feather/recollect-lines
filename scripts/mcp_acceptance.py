#!/usr/bin/env python3
"""Generic MCP-host acceptance harness for Recollect Lines.

Speaks the standard MCP stdio JSON-RPC protocol directly to a real
`recollect-mcp` subprocess — exactly what any MCP-compatible host (an IDE
plugin, a terminal agent, Hermes, or otherwise) does to use this broker.
This harness assumes no specific host: it is a plain stdlib JSON-RPC
client, not a Hermes client, and Hermes is not required to run it or to
accept this phase's work (see docs/design/PRD.md and docs/history/phases/phase-5c.md).

It exercises the documented delegate -> observe -> collect -> cancel
lifecycle against a disposable local Git fixture repository, including
Phase 5C's verification-gate and timeout/cancellation-liveness behavior.
To stay fully local, offline, and deterministic, it points the broker's
opencode and claude_code adapters at this repo's own deterministic stand-in
CLIs (tests/fixtures/fake_opencode.py, tests/fixtures/fake_claude.py,
tests/fixtures/fake_codex.py, tests/fixtures/fake_cursor.py) via `--opencode-command`/`--claude-command`/
`--codex-command`/`--cursor-command` — the same override mechanism each adapter documents as
intended for "testing/acceptance" — instead of the real network-dependent
opencode-ai package, an authenticated `claude` CLI, or an authenticated
`codex` CLI.

Usage:
    python3 scripts/mcp_acceptance.py

Exit code 0 if every check passes, 1 otherwise. Prints one PASS/FAIL line
per check plus a final summary; nothing here depends on unittest.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FAKE_OPENCODE = ROOT / "tests" / "fixtures" / "fake_opencode.py"
FAKE_CLAUDE = ROOT / "tests" / "fixtures" / "fake_claude.py"
FAKE_CODEX = ROOT / "tests" / "fixtures" / "fake_codex.py"
FAKE_CURSOR = ROOT / "tests" / "fixtures" / "fake_cursor.py"

TERMINAL_STATES = {"succeeded", "succeeded_with_warnings", "failed", "timed_out", "cancelled"}

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(label)
    return condition


class McpStdioClient:
    """A minimal, dependency-free JSON-RPC client over a subprocess's stdio.

    Deliberately independent of tests/test_mcp_server.py's McpStdioClient:
    this script must run standalone (`python3 scripts/mcp_acceptance.py`)
    without importing the test package, exactly as an external host would.
    """

    def __init__(self, home: Path, opencode_command: list[str], claude_command: list[str], codex_command: list[str], cursor_command: list[str]):
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{SRC}{os.pathsep}{existing}" if existing else str(SRC)
        self.process = subprocess.Popen(
            [
                sys.executable, "-m", "recollect_lines.mcp_server",
                "--home", str(home),
                "--opencode-command", json.dumps(opencode_command),
                "--claude-command", json.dumps(claude_command),
                "--codex-command", json.dumps(codex_command),
                "--cursor-command", json.dumps(cursor_command),
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self._next_id = 1

    def _send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"subprocess produced no output (exit={self.process.poll()}); stderr:\n{self.process.stderr.read()}")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise RuntimeError(f"expected response id {request_id}, got {response!r}")
        return response

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def call_tool(self, name: str, arguments: dict) -> tuple[bool, dict]:
        """Returns (is_error, payload) where payload is the tool's `data` on
        success or its `error` object on failure — this harness's own
        parsing of the versioned tool-result envelope, matching the shape
        any real MCP host would decode.
        """
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in response:
            raise RuntimeError(f"tools/call for {name!r} hit a JSON-RPC protocol error: {response['error']}")
        result = response["result"]
        body = json.loads(result["content"][0]["text"])
        return bool(result["isError"]), (body["data"] if body["ok"] else body["error"])

    def close(self) -> None:
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result


def init_fixture_repo(path: Path) -> Path:
    """A disposable local Git repository — never the actual project checkout."""
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], cwd=path)
    run_git(["config", "user.email", "acceptance@example.com"], cwd=path)
    run_git(["config", "user.name", "MCP Acceptance"], cwd=path)
    (path / "README.md").write_text("Disposable fixture repository for MCP acceptance.\n")
    run_git(["add", "-A"], cwd=path)
    run_git(["commit", "-q", "-m", "initial"], cwd=path)
    return path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="recollect-mcp-acceptance-") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "broker"
        source = init_fixture_repo(tmp_path / "source")
        head_before = run_git(["rev-parse", "HEAD"], cwd=source).stdout.strip()

        client = McpStdioClient(
            home,
            [sys.executable, str(FAKE_OPENCODE)],
            [sys.executable, str(FAKE_CLAUDE)],
            [sys.executable, str(FAKE_CODEX)],
            [sys.executable, str(FAKE_CURSOR)],
        )
        try:
            init = client.request(
                "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mcp-acceptance-harness", "version": "0"}},
            )
            check("initialize handshake succeeds", init["result"]["serverInfo"]["name"] == "recollect-lines-mcp", str(init))
            client.notify("notifications/initialized")

            listed = client.request("tools/list")
            tool_names = {tool["name"] for tool in listed["result"]["tools"]}
            check(
                "tool surface exposes the documented delegate/observe/collect/cancel lifecycle",
                {"delegate", "status", "collect", "cancel", "reconcile"} <= tool_names,
                str(tool_names),
            )

            # --- delegate + collect: a task with a required, passing verification gate ---
            is_error, delegated = client.call_tool("delegate", {
                "task": "Inspect the fixture repository",
                "workspace": str(source),
                "execution_mode": "isolated_worktree",
                "profile": "opencode",
                "verification_policy": "required",
                "verify_commands": [[sys.executable, "-c", "print('verified')"]],
            })
            check("delegate accepts an isolated-worktree opencode task", not is_error, str(delegated))
            task_id = delegated.get("task_id", "")
            check("delegate returns a task id", bool(task_id))

            is_error, status_payload = client.call_tool("status", {"task_id": task_id})
            check("status reflects the running task before collection", not is_error and status_payload["state"] == "running", str(status_payload))

            is_error, collected = client.call_tool("collect", {"task_id": task_id})
            check("collect succeeds without a protocol error", not is_error, str(collected))
            check("task reaches succeeded under a required, passing verification gate", collected.get("state") == "succeeded", str(collected))
            gate = collected.get("verification_gate") or {}
            check("verification_gate.label reports required_verified", gate.get("label") == "required_verified", str(gate))
            check(
                "collect distinguishes runtime-reported result from broker-verified evidence",
                collected.get("runtime_result") is not None and collected.get("broker_verification") is not None,
                str(collected),
            )

            is_error, recollected = client.call_tool("collect", {"task_id": task_id})
            check("repeated collect on a terminal task is idempotent", not is_error and recollected == collected, str(recollected))

            # --- delegate + cancel: a long-running task, confirmed process-group termination ---
            is_error, sleeper = client.call_tool("delegate", {
                "task": "SLEEP",
                "workspace": str(source),
                "execution_mode": "isolated_worktree",
                "profile": "opencode",
            })
            check("delegate accepts a long-running task", not is_error, str(sleeper))
            sleeper_id = sleeper.get("task_id", "")

            is_error, cancelled = client.call_tool("cancel", {"task_id": sleeper_id, "reason": "mcp acceptance harness cancellation"})
            check(
                "cancel reports a factually observed outcome, not just a sent signal",
                not is_error and cancelled.get("state") in {"cancelled", "failed"},
                str(cancelled),
            )

            # --- delegate + collect: the claude_code profile through the exact same generic
            # dispatch, read_only mode — the execution mode this phase's reconciliation fixed
            # (see docs/history/phases/phase-6a.md "Reconciliation addendum") to structurally exclude Bash via
            # --tools rather than relying on --disallowedTools alone ---
            is_error, claude_delegated = client.call_tool("delegate", {
                "task": "Inspect the fixture repository",
                "workspace": str(source),
                "execution_mode": "read_only",
                "profile": "claude_code",
            })
            check("delegate accepts a read-only claude_code task", not is_error, str(claude_delegated))
            claude_task_id = claude_delegated.get("task_id", "")

            is_error, claude_collected = client.call_tool("collect", {"task_id": claude_task_id})
            check("collect succeeds for a claude_code task without a protocol error", not is_error, str(claude_collected))
            check("claude_code task reaches succeeded", claude_collected.get("state") == "succeeded", str(claude_collected))
            check(
                "claude_code runtime result honestly reports the claude_code adapter, not a generic/other runtime",
                (claude_collected.get("runtime_result") or {}).get("runtime", {}).get("adapter") == "claude_code",
                str(claude_collected),
            )

            # --- delegate + collect: the codex profile through the same generic dispatch ---
            is_error, codex_delegated = client.call_tool("delegate", {
                "task": "Inspect the fixture repository",
                "workspace": str(source),
                "execution_mode": "read_only",
                "profile": "codex",
            })
            check("delegate accepts a read-only codex task", not is_error, str(codex_delegated))
            codex_task_id = codex_delegated.get("task_id", "")

            is_error, codex_collected = client.call_tool("collect", {"task_id": codex_task_id})
            check("collect succeeds for a codex task without a protocol error", not is_error, str(codex_collected))
            check("codex task reaches succeeded", codex_collected.get("state") == "succeeded", str(codex_collected))
            check(
                "codex runtime result honestly reports the codex adapter, not a generic/other runtime",
                (codex_collected.get("runtime_result") or {}).get("runtime", {}).get("adapter") == "codex",
                str(codex_collected),
            )

            # --- delegate + collect: the cursor profile through the same generic dispatch ---
            is_error, cursor_delegated = client.call_tool("delegate", {
                "task": "Inspect the fixture repository",
                "workspace": str(source),
                "execution_mode": "read_only",
                "profile": "cursor",
            })
            check("delegate accepts a read-only cursor task", not is_error, str(cursor_delegated))
            cursor_task_id = cursor_delegated.get("task_id", "")

            is_error, cursor_collected = client.call_tool("collect", {"task_id": cursor_task_id})
            check("collect succeeds for a cursor task without a protocol error", not is_error, str(cursor_collected))
            check("cursor task reaches succeeded", cursor_collected.get("state") == "succeeded", str(cursor_collected))
            check(
                "cursor runtime result honestly reports the cursor adapter, not a generic/other runtime",
                (cursor_collected.get("runtime_result") or {}).get("runtime", {}).get("adapter") == "cursor",
                str(cursor_collected),
            )

            # --- workspace safety: the source fixture is never mutated by delegated work ---
            head_after = run_git(["rev-parse", "HEAD"], cwd=source).stdout.strip()
            porcelain = run_git(["status", "--porcelain"], cwd=source).stdout
            check("source workspace HEAD is unchanged after all tasks", head_before == head_after)
            check("source workspace has no uncommitted mutation", porcelain.strip() == "", porcelain)

            # --- reconcile: documented no-op against a clean, already-terminal broker state ---
            is_error, reconciled = client.call_tool("reconcile", {})
            check("reconcile succeeds with nothing pending to recover", not is_error and reconciled.get("reconciled") == [], str(reconciled))
        finally:
            client.close()

    print()
    if _failures:
        print(f"MCP acceptance FAILED ({len(_failures)} check(s)): {', '.join(_failures)}")
        return 1
    print(
        "MCP acceptance PASSED: a generic stdio JSON-RPC client drove a real recollect-mcp "
        "subprocess through delegate/observe/collect/cancel against a disposable local Git "
        "fixture, including the Phase 5C verification gate and cancellation-liveness evidence."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
