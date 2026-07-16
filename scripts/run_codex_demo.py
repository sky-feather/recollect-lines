#!/usr/bin/env python3
"""Codex-through-Recollect-Lines demo (dry-run by default).

Exercises delegate → status → collect over a long-lived recollect-mcp subprocess
against a disposable git fixture. Live mode requires explicit provider opt-in.

Usage:
    python3 scripts/run_codex_demo.py
    python3 scripts/run_codex_demo.py --execute-live --acknowledge-provider-call
    python3 scripts/run_codex_demo.py --demo-cancel-fixture
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FAKE_CODEX = ROOT / "tests" / "fixtures" / "fake_codex.py"
DEFAULT_EVIDENCE = ROOT / "docs" / "demos" / "codex-marker-evidence.json"
MAX_LIVE_SECONDS = 180
POLL_INTERVAL = 2.0

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"/(?:home|Users)/[^\s\"']+"),
    re.compile(r"/tmp/[^\s\"']+"),
)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _redact(value: object) -> object:
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub("<redacted>", text)
        return text
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def init_fixture(root: Path) -> None:
    (root / "alpha.txt").write_text("MARKER_ALPHA\n", encoding="utf-8")
    (root / "beta.txt").write_text("MARKER_BETA\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "demo fixture"], cwd=root, check=True)


class McpClient:
    def __init__(self, home: Path, codex_command: list[str] | None):
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{SRC}{os.pathsep}{env.get('PYTHONPATH', '')}"
        cmd = [sys.executable, "-m", "recollect_lines.mcp_server", "--home", str(home)]
        if codex_command is not None:
            cmd += ["--codex-command", json.dumps(codex_command)]
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._id = 0

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        message = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("recollect-mcp exited unexpectedly")
            response = json.loads(line)
            if response.get("id") == self._id:
                return response

    def notify(self, method: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.process.stdin.flush()

    def tool(self, name: str, arguments: dict) -> dict:
        response = self.rpc("tools/call", {"name": name, "arguments": arguments})
        text = response["result"]["content"][0]["text"]
        return json.loads(text)


def codex_version() -> str:
    try:
        completed = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unavailable"
    if completed.returncode != 0:
        return "unavailable"
    return (completed.stdout or completed.stderr).strip()


def package_version() -> str:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    out = subprocess.run(
        [sys.executable, "-c", "from recollect_lines import __version__; print(__version__)"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return out.stdout.strip()


def dry_run_plan() -> dict:
    return {
        "mode": "dry_run",
        "message": (
            "No provider call. Re-run with --execute-live --acknowledge-provider-call "
            "to delegate a bounded Codex CLI task through recollect-mcp."
        ),
        "fixture": {
            "files": {"alpha.txt": "MARKER_ALPHA", "beta.txt": "MARKER_BETA"},
            "task": "Reply with only the filename containing MARKER_ALPHA.",
        },
        "mcp_path": ["initialize", "delegate", "status", "collect"],
        "equivalent_cli": [
            "recollect-mcp --home <broker-home>",
            "# then MCP tools/call delegate, status, collect (not separate one-shot recollect-lines start/collect)",
        ],
    }


def run_live(output: Path) -> dict:
    version = package_version()

    fixture = Path(tempfile.mkdtemp(prefix="rl-codex-demo-fixture-"))
    broker = Path(tempfile.mkdtemp(prefix="rl-codex-demo-broker-"))
    init_fixture(fixture)
    client = McpClient(broker, codex_command=None)
    started = time.time()
    lifecycle: list[dict] = []
    provider_call = False
    exit_code = 1
    try:
        client.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "run_codex_demo", "version": "1"},
            },
        )
        client.notify("notifications/initialized")
        lifecycle.append({"step": "initialize", "at_s": 0})

        delegated = client.tool(
            "delegate",
            {
                "task": "Read alpha.txt and beta.txt. Reply with only the filename that contains MARKER_ALPHA.",
                "workspace": str(fixture),
                "profile": "codex",
                "execution_mode": "read_only",
                "timeout_seconds": 120,
            },
        )
        provider_call = True
        lifecycle.append({"step": "delegate", "ok": delegated.get("ok"), "state": delegated.get("data", {}).get("state")})
        if not delegated.get("ok"):
            raise RuntimeError(f"delegate failed: {delegated}")
        task_id = delegated["data"]["task_id"]

        last_state = None
        while time.time() - started < MAX_LIVE_SECONDS:
            status = client.tool("status", {"task_id": task_id})
            state = status["data"]["state"]
            if state != last_state:
                lifecycle.append({"step": "status", "state": state, "at_s": round(time.time() - started, 1)})
                last_state = state
            if state in {"succeeded", "succeeded_with_warnings", "failed", "timed_out", "cancelled"}:
                break
            time.sleep(POLL_INTERVAL)

        collected = client.tool("collect", {"task_id": task_id})
        lifecycle.append({"step": "collect", "ok": collected.get("ok"), "state": collected.get("data", {}).get("state")})
        exit_code = 0 if collected.get("ok") and collected["data"].get("state") == "succeeded" else 1

        evidence = {
            "demo_schema_version": "1",
            "recorded_at": _utc_now(),
            "mode": "live",
            "provider_call_occurred": provider_call,
            "billing": "ChatGPT/Codex subscription quota (no USD cost reported by this harness)",
            "recollect_lines_version": version,
            "codex_cli_version": codex_version(),
            "api_path": "recollect-mcp MCP tools: delegate → status → collect",
            "broker_home": "<redacted>",
            "fixture": {"description": "two-file git repo with MARKER_ALPHA / MARKER_BETA"},
            "task": {
                "profile": "codex",
                "execution_mode": "read_only",
                "timeout_seconds": 120,
                "prompt_summary": "Identify filename containing MARKER_ALPHA",
            },
            "lifecycle_transitions": lifecycle,
            "result": _redact(collected.get("data")),
            "exit_status": exit_code,
            "duration_seconds": round(time.time() - started, 1),
            "proves": [
                "Parent-style MCP host can delegate bounded work to real Codex CLI through the broker",
                "Broker records lifecycle and returns runtime-reported summary with artifact evidence",
            ],
            "does_not_prove": [
                "CLI one-shot start/collect across separate shell invocations",
                "Post-restart session resume or result recovery",
                "Continuous certification against future Codex CLI releases",
            ],
        }
    finally:
        client.close()
        shutil_rmtree(fixture)
        shutil_rmtree(broker)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def run_cancel_fixture() -> dict:
    fixture = Path(tempfile.mkdtemp(prefix="rl-codex-cancel-fixture-"))
    broker = Path(tempfile.mkdtemp(prefix="rl-codex-cancel-broker-"))
    init_fixture(fixture)
    client = McpClient(broker, codex_command=[sys.executable, str(FAKE_CODEX)])
    try:
        client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "cancel-demo", "version": "1"}})
        client.notify("notifications/initialized")
        delegated = client.tool(
            "delegate",
            {
                "task": "SLEEP",
                "workspace": str(fixture),
                "profile": "codex",
                "execution_mode": "read_only",
                "timeout_seconds": 60,
            },
        )
        task_id = delegated["data"]["task_id"]
        cancelled = client.tool("cancel", {"task_id": task_id, "reason": "demo cancellation"})
        return {
            "mode": "cancel_fixture",
            "provider_call_occurred": False,
            "task_id": task_id,
            "cancel_state": cancelled.get("data", {}).get("state"),
            "ok": cancelled.get("ok"),
        }
    finally:
        client.close()
        shutil_rmtree(fixture)
        shutil_rmtree(broker)


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex-through-Recollect-Lines demo")
    parser.add_argument("--execute-live", action="store_true", help="Run a live Codex CLI delegation (uses subscription quota)")
    parser.add_argument(
        "--acknowledge-provider-call",
        action="store_true",
        help="Required with --execute-live: acknowledge a real Codex provider call",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_EVIDENCE, help="Evidence JSON output path")
    parser.add_argument(
        "--demo-cancel-fixture",
        action="store_true",
        help="Offline cancellation demo using fake_codex (no provider call)",
    )
    args = parser.parse_args(argv)

    if args.demo_cancel_fixture:
        result = run_cancel_fixture()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1

    if not args.execute_live:
        plan = dry_run_plan()
        print(json.dumps(plan, indent=2, sort_keys=True))
        print("\nDry run only. No provider call.", file=sys.stderr)
        return 0

    if not args.acknowledge_provider_call:
        print("Refusing live execution without --acknowledge-provider-call", file=sys.stderr)
        return 2

    if codex_version() == "unavailable":
        print("Codex CLI not found on PATH; cannot run live demo", file=sys.stderr)
        return 3

    evidence = run_live(args.output)
    print(json.dumps(_redact(evidence), indent=2, sort_keys=True))
    print(f"\nWrote evidence to {args.output}", file=sys.stderr)
    return int(evidence.get("exit_status", 1))


if __name__ == "__main__":
    raise SystemExit(main())
