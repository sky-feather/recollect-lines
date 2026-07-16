"""Experimental adapter that runs the OpenCode CLI as a supervised subprocess.

The broker owns cancellation evidence: OpenCode is never trusted to report its
own termination, so cancel() probes the process group directly with signal 0.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .adapters import AdapterCapabilities
from .models import TaskRecord
from .recovery_contract import SUBPROCESS_CLI_RECOVERY_CONTROL

DEFAULT_COMMAND_PREFIX = ("npx", "--yes", "opencode-ai@1.17.18")
DEFAULT_GRACE_PERIOD_SECONDS = 10.0

# ponytail: a flag-name heuristic, not exhaustive secret scanning. Nothing in
# DEFAULT_COMMAND_PREFIX/build_command currently carries a secret, but any
# future argv-based flag whose name looks like one of these is redacted
# before the command is persisted durably (see Broker.start()/record_launch).
SECRET_FLAG_MARKERS = ("token", "key", "secret", "password", "credential")
REDACTED_VALUE = "***REDACTED***"


def redact_command(command: list) -> list:
    """Best-effort redaction of a suspicious flag's value, in either
    `--flag value` (two argv entries) or `--flag=value` (one entry) form.

    Only argv entries that look like a flag (leading `-`) are checked against
    SECRET_FLAG_MARKERS — otherwise an already-redacted or coincidentally
    marker-shaped *value* (e.g. a task prompt containing the word "secret")
    would itself be misread as a flag on the next iteration and cascade into
    redacting the following, unrelated argument.
    """
    redacted = list(command)
    for index, part in enumerate(redacted):
        if not part.startswith("-"):
            continue
        if "=" in part:
            flag_name, _, _value = part.partition("=")
            if any(marker in flag_name.lower().lstrip("-") for marker in SECRET_FLAG_MARKERS):
                redacted[index] = f"{flag_name}={REDACTED_VALUE}"
            continue
        if index + 1 >= len(redacted):
            continue
        flag = part.lower().lstrip("-")
        if any(marker in flag for marker in SECRET_FLAG_MARKERS):
            redacted[index + 1] = REDACTED_VALUE
    return redacted


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_dead_within(pgid: int, timeout: float, interval: float = 0.05) -> bool:
    # SIGKILL delivery is asynchronous: a grandchild (e.g. the real `opencode`
    # process under npm's `sh -c` wrapper) can outlive the top-level popen.wait()
    # by a beat while the kernel tears it down. A single instantaneous check right
    # after the signal races that teardown, so poll briefly instead.
    deadline = time.monotonic() + timeout
    while group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def cancel_process_group(popen: subprocess.Popen, pgid: int, grace_period_seconds: float) -> dict:
    """Shared SIGTERM-then-SIGKILL process-group cancellation.

    Used by every subprocess-backed adapter (OpenCodeAdapter, ClaudeCodeAdapter)
    so the broker's process-group cancellation contract has exactly one
    implementation, never a per-adapter reinvention.
    """
    signals_sent = []
    try:
        os.killpg(pgid, signal.SIGTERM)
        signals_sent.append("SIGTERM")
    except ProcessLookupError:
        pass
    try:
        popen.wait(timeout=grace_period_seconds)
    except subprocess.TimeoutExpired:
        pass
    group_terminated = group_dead_within(pgid, timeout=1.0)
    if not group_terminated:
        try:
            os.killpg(pgid, signal.SIGKILL)
            signals_sent.append("SIGKILL")
        except ProcessLookupError:
            pass
        try:
            popen.wait(timeout=grace_period_seconds)
        except subprocess.TimeoutExpired:
            pass
        group_terminated = group_dead_within(pgid, timeout=2.0)
    return {"signals_sent": signals_sent, "group_terminated": group_terminated, "exit_code": popen.returncode}


@dataclass
class ProcessHandle:
    task_id: str
    pid: int
    pgid: int
    command: list
    events_path: Path
    stderr_path: Path
    popen: subprocess.Popen


class OpenCodeAdapter:
    name = "opencode"
    capabilities = AdapterCapabilities(
        requires_subprocess=True,
        supports_process_group_cancellation=True,
        reports_broker_verified_tests=False,
        recovery_control=SUBPROCESS_CLI_RECOVERY_CONTROL,
    )

    def __init__(self, command_prefix=DEFAULT_COMMAND_PREFIX, grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS):
        self.command_prefix = tuple(command_prefix)
        self.grace_period_seconds = grace_period_seconds

    @property
    def runtime_label(self) -> str:
        """A human-readable adapter/version label for durable launch records."""
        return self.command_prefix[-1] if self.command_prefix else self.name

    def build_command(self, workspace: str, prompt: str) -> list:
        return [*self.command_prefix, "run", "--pure", "--format", "json", "--dir", workspace, prompt]

    def start(self, record: TaskRecord, artifacts_dir: Path, workspace: str | None = None, *, prompt: str | None = None) -> tuple[dict, ProcessHandle]:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        events_path = artifacts_dir / "events.jsonl"
        stderr_path = artifacts_dir / "stderr.log"
        command = self.build_command(workspace or record.workspace, prompt or record.task)
        with events_path.open("wb") as events_file, stderr_path.open("wb") as stderr_file:
            popen = subprocess.Popen(command, stdout=events_file, stderr=stderr_file, start_new_session=True)
        pgid = os.getpgid(popen.pid)
        handle = ProcessHandle(
            task_id=record.id,
            pid=popen.pid,
            pgid=pgid,
            command=command,
            events_path=events_path,
            stderr_path=stderr_path,
            popen=popen,
        )
        metadata = {
            "adapter": self.name,
            "command": command,
            "pid": popen.pid,
            "pgid": pgid,
            "events_artifact": events_path.name,
            "stderr_artifact": stderr_path.name,
            "workspace": workspace or record.workspace,
        }
        return metadata, handle

    def cancel(self, handle: ProcessHandle) -> dict:
        return cancel_process_group(handle.popen, handle.pgid, self.grace_period_seconds)

    def collect(self, handle: ProcessHandle) -> dict:
        exit_code = handle.popen.wait()
        events = []
        malformed_event_lines = 0
        if handle.events_path.exists():
            for line in handle.events_path.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    malformed_event_lines += 1
        summary = None
        for event in reversed(events):
            if not isinstance(event, dict) or event.get("type") != "text":
                continue
            text = event.get("text")
            if not isinstance(text, str):
                part = event.get("part")
                text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text.strip():
                summary = text.strip()
                break
        stderr_text = handle.stderr_path.read_text(errors="replace") if handle.stderr_path.exists() else ""
        return {
            "exit_code": exit_code,
            "events_count": len(events),
            "malformed_event_lines": malformed_event_lines,
            "summary": summary,
            "stderr_tail": stderr_text[-4000:],
            # ponytail: broker never independently re-runs tests in Phase 2, so this is
            # hardcoded false rather than derived; flip only once real verification exists.
            "verification": {"tests_broker_verified": False, "source": "runtime_reported"},
        }
