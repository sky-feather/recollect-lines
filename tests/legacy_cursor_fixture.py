"""Test-only producer for persisted pre-RFC-004 Cursor launch records.

Production adapters must never import this module. It exists solely to exercise
the broker's read-only sunset compatibility for broker homes that already
contain ``legacy_subprocess`` records.
"""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from recollect_lines.adaptor.cursor import CursorAdapter, RUNTIME_DESCRIPTION, SANDBOX_BY_EXECUTION_MODE
from recollect_lines.adaptor.process import group_dead_within
from recollect_lines.durable_runner import read_process_start_identity
from recollect_lines.models import TaskRecord


@dataclass
class LegacyCursorFixtureHandle:
    task_id: str
    pid: int
    pgid: int
    command: list
    stdout_path: Path
    stderr_path: Path
    popen: subprocess.Popen


class LegacyCursorFixtureAdapter(CursorAdapter):
    """Create a legacy-shaped process strictly for compatibility-reader tests."""

    def start(self, record: TaskRecord, artifacts_dir: Path, workspace: str | None = None, *, prompt: str | None = None):
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifacts_dir / "stdout.log"
        stderr_path = artifacts_dir / "stderr.log"
        effective_workspace = workspace or record.workspace
        command = self.build_command(
            prompt or record.task,
            record.execution_mode,
            effective_workspace,
            model=record.effective_model,
        )
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            popen = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=effective_workspace,
                start_new_session=True,
            )
        pgid = os.getpgid(popen.pid)
        handle = LegacyCursorFixtureHandle(
            task_id=record.id,
            pid=popen.pid,
            pgid=pgid,
            command=command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            popen=popen,
        )
        return {
            "adapter": self.name,
            "runtime_description": RUNTIME_DESCRIPTION,
            "command": command,
            "pid": popen.pid,
            "pgid": pgid,
            "leader_start_identity": read_process_start_identity(popen.pid),
            "events_artifact": stdout_path.name,
            "stderr_artifact": stderr_path.name,
            "workspace": effective_workspace,
            "sandbox": SANDBOX_BY_EXECUTION_MODE[record.execution_mode],
            "launch_kind": "legacy_subprocess",
        }, handle

    def cancel(self, handle: LegacyCursorFixtureHandle) -> dict:
        signals_sent = []
        try:
            os.killpg(handle.pgid, signal.SIGTERM)
            signals_sent.append("SIGTERM")
        except ProcessLookupError:
            pass
        try:
            handle.popen.wait(timeout=self.grace_period_seconds)
        except subprocess.TimeoutExpired:
            pass
        group_terminated = group_dead_within(handle.pgid, timeout=1.0)
        if not group_terminated:
            try:
                os.killpg(handle.pgid, signal.SIGKILL)
                signals_sent.append("SIGKILL")
            except ProcessLookupError:
                pass
            try:
                handle.popen.wait(timeout=self.grace_period_seconds)
            except subprocess.TimeoutExpired:
                pass
            group_terminated = group_dead_within(handle.pgid, timeout=2.0)
        return {
            "signals_sent": signals_sent,
            "group_terminated": group_terminated,
            "exit_code": handle.popen.returncode,
        }

    def collect(self, handle: LegacyCursorFixtureHandle) -> dict:
        process_exit_code = handle.popen.wait()
        stdout = handle.stdout_path.read_text(errors="replace") if handle.stdout_path.exists() else ""
        stderr = handle.stderr_path.read_text(errors="replace") if handle.stderr_path.exists() else ""
        return self.parse_result(stdout_text=stdout, stderr_text=stderr, process_exit_code=process_exit_code)
