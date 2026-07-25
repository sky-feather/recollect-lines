"""Shared helpers for RFC-004 P1.3 durable-supervisor conformance across production adapters."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from recollect_lines.durable_runner import (
    STATE_CANCELLED,
    STATE_EXITED,
    STATE_FAILED,
    STATE_TIMED_OUT,
    load_launch_record,
)
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.service import Broker

TERMINAL_LAUNCH_STATES = frozenset({STATE_EXITED, STATE_TIMED_OUT, STATE_CANCELLED, STATE_FAILED})

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CURSOR = FIXTURES / "fake_cursor.py"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"
FAKE_CODEX = FIXTURES / "fake_codex.py"
FAKE_OPENCODE = FIXTURES / "fake_opencode.py"

QUICK_SUCCESS_TASK = "what is the magic number"
DEFAULT_OPENCODE_TASK = "Inspect tests"


@dataclass(frozen=True)
class ProductionAdapterCase:
    adapter_id: str
    profile: str
    broker_kwarg: str
    fixture_path: Path
    stdout_artifact_name: str
    liveness_probe: str  # "stderr" or "events" — where the SLEEP fixture writes its ready marker

    def success_task(self) -> str:
        return DEFAULT_OPENCODE_TASK if self.adapter_id == "opencode" else QUICK_SUCCESS_TASK


PRODUCTION_ADAPTER_CASES: tuple[ProductionAdapterCase, ...] = (
    ProductionAdapterCase("cursor", "cursor", "cursor_adapter", FAKE_CURSOR, "stdout.log", "stderr"),
    ProductionAdapterCase("claude_code", "claude_code", "claude_code_adapter", FAKE_CLAUDE, "stdout.log", "stderr"),
    ProductionAdapterCase("codex", "codex", "codex_adapter", FAKE_CODEX, "events.jsonl", "stderr"),
    ProductionAdapterCase("opencode", "opencode", "opencode_adapter", FAKE_OPENCODE, "events.jsonl", "events"),
)


def wait_for_durable_launch_terminal(handle: object, *, timeout: float, interval: float = 0.05) -> bool:
    """Bounded manifest poll matching durable_cli_launch semantics without that import cycle."""
    manifest_path = handle.durable.manifest_path  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while load_launch_record(manifest_path).lifecycle_state not in TERMINAL_LAUNCH_STATES:
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], cwd=path)
    run_git(["config", "user.email", "test@example.com"], cwd=path)
    run_git(["config", "user.name", "Test"], cwd=path)
    (path / "file.txt").write_text("original\n")
    run_git(["add", "-A"], cwd=path)
    run_git(["commit", "-q", "-m", "initial"], cwd=path)
    return path


def fake_adapter(case: ProductionAdapterCase, *, grace_period_seconds: float = 2.0):
    prefix = (sys.executable, str(case.fixture_path))
    if case.adapter_id == "cursor":
        from recollect_lines.adaptor.cursor import CursorAdapter

        return CursorAdapter(command_prefix=prefix, grace_period_seconds=grace_period_seconds)
    if case.adapter_id == "claude_code":
        from recollect_lines.adaptor.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter(command_prefix=prefix, grace_period_seconds=grace_period_seconds)
    if case.adapter_id == "codex":
        from recollect_lines.adaptor.codex import CodexAdapter

        return CodexAdapter(command_prefix=prefix, grace_period_seconds=grace_period_seconds)
    from recollect_lines.adaptor.opencode import OpenCodeAdapter

    return OpenCodeAdapter(command_prefix=prefix, grace_period_seconds=grace_period_seconds)


def make_broker(home: Path, case: ProductionAdapterCase, *, grace_period_seconds: float = 2.0) -> Broker:
    return Broker(home, **{case.broker_kwarg: fake_adapter(case, grace_period_seconds=grace_period_seconds)})


def launch_row(broker: Broker, task_id: str) -> dict:
    launch = broker.store.get_launch(task_id)
    assert launch is not None, f"missing launch row for {task_id}"
    return launch


def launch_dir(broker: Broker, task_id: str) -> Path:
    launch = launch_row(broker, task_id)
    return broker.store.home / "durable_launches" / launch["durable_launch_id"]


def manifest_path(broker: Broker, task_id: str) -> Path:
    return launch_dir(broker, task_id) / "manifest.json"


def stdout_artifact_path(broker: Broker, task_id: str, case: ProductionAdapterCase) -> Path:
    return launch_dir(broker, task_id) / case.stdout_artifact_name


def wait_for_running_probe(broker: Broker, task_id: str, case: ProductionAdapterCase) -> None:
    if case.liveness_probe == "stderr":
        stderr_path = launch_dir(broker, task_id) / "stderr.log"
        ready = wait_until(lambda: stderr_path.exists() and b"started" in stderr_path.read_bytes())
        assert ready, f"{case.adapter_id}: fixture never wrote stderr liveness marker"
        return
    events_path = stdout_artifact_path(broker, task_id, case)
    ready = wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes())
    assert ready, f"{case.adapter_id}: fixture never wrote events.jsonl liveness marker"


def wait_for_terminal_manifest(manifest: Path, *, timeout: float = 5.0) -> bool:
    return wait_until(
        lambda: load_launch_record(manifest).lifecycle_state in TERMINAL_LAUNCH_STATES,
        timeout=timeout,
    )


def kill_pgid(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class ConformanceHarness:
    """Isolated broker home/worktree with durable-safe teardown."""

    def __init__(self, case: ProductionAdapterCase, *, use_git_repo: bool = False):
        self.case = case
        self._tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tempdir.name) / "broker"
        if use_git_repo:
            self.workspace = init_repo(Path(self._tempdir.name) / "source")
        else:
            self.workspace = Path(self._tempdir.name) / "workspace"
            self.workspace.mkdir()
        self.broker = make_broker(self.home, case)
        self._pgids: list[int] = []
        self._brokers: list[Broker] = [self.broker]

    def track_broker(self, broker: Broker) -> Broker:
        self._brokers.append(broker)
        return broker

    def track_pgid(self, pgid: int | None) -> None:
        if isinstance(pgid, int) and pgid > 0:
            self._pgids.append(pgid)

    def create(self, task: str, **kwargs) -> TaskRequest:
        kwargs.setdefault("execution_mode", "read_only")
        return TaskRequest(task, str(self.workspace), profile=self.case.profile, **kwargs)

    def close_all(self) -> None:
        for broker in reversed(self._brokers):
            for task_id in list(getattr(broker, "_adopted_durable_handles", {})):
                launch = broker.store.get_launch(task_id)
                if launch and launch.get("pgid"):
                    self.track_pgid(launch["pgid"])
            for task_id in list(getattr(broker, "_process_handles", {})):
                handle = broker._process_handles[task_id]
                self.track_pgid(getattr(handle, "pgid", None))
                manifest = manifest_path(broker, task_id) if broker.store.get_launch(task_id) else None
                if manifest is not None and manifest.is_file():
                    wait_for_terminal_manifest(manifest, timeout=5)
            broker.close()
        self._brokers.clear()
        for pgid in self._pgids:
            kill_pgid(pgid)
        self._tempdir.cleanup()


def assert_durable_seam(test: unittest.TestCase, broker: Broker, task_id: str, case: ProductionAdapterCase, handle: object) -> None:
    launch = launch_row(broker, task_id)
    test.assertEqual(launch["adapter"], case.adapter_id)
    test.assertEqual(launch["launch_kind"], "durable_subprocess")
    test.assertIsNotNone(launch["durable_launch_id"])
    test.assertEqual(launch["events_artifact"], case.stdout_artifact_name)
    test.assertTrue(hasattr(handle, "durable"), "handle must be durable-cli, not legacy Popen")
    test.assertIs(getattr(handle, "popen", None), None, "production adapter must not expose adapter-owned Popen")
