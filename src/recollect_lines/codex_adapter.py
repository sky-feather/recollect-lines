"""Runtime adapter that runs the Codex CLI (`codex exec`) as a supervised subprocess.

Command contract is grounded in a compatibility spike against the installed CLI
(codex-cli 0.144.4) — see docs/history/phases/phase-6b.md. `codex exec --json` streams
newline-delimited JSON events (`thread.started`, `turn.started`, `item.*`,
`turn.completed`/`turn.failed`) to stdout; cancellation targets the process
group directly, exactly as OpenCodeAdapter does, never a claimed self-report.

The broker owns cancellation evidence: Codex is never trusted to report its own
termination.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .adapters import AdapterCapabilities
from .recovery_contract import SUBPROCESS_CLI_RECOVERY_CONTROL
from .models import TaskRecord
from .opencode_adapter import cancel_process_group

DEFAULT_COMMAND_PREFIX = ("codex",)
DEFAULT_GRACE_PERIOD_SECONDS = 10.0
RUNTIME_DESCRIPTION = "Codex via codex exec"

# Spike-validated (docs/history/phases/phase-6b.md): `read-only` sandbox structurally limits
# shell/file mutations; `workspace-write` is the narrowest mode confirmed for
# isolated_worktree tasks that must edit files inside the broker-owned worktree.
# ponytail: only the two execution_modes the broker currently defines are
# mapped; build_command() fails closed (raises) for anything else.
SANDBOX_BY_EXECUTION_MODE = {
    "read_only": "read-only",
    "isolated_worktree": "workspace-write",
}

REDACTED_VALUE = "***REDACTED***"
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|password)\s*[:=]\s*\S+"),
)


def redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED_VALUE, text)
    return text


class CodexUnsupportedPolicy(ValueError):
    """Raised when an execution_mode has no validated Codex sandbox mapping."""


def _classify_runtime_error(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("401", "403", "unauthorized", "authentication", "not logged in", "login")):
        return "authentication_error"
    if any(token in lowered for token in ("429", "rate limit", "quota", "too many requests")):
        return "rate_limit_or_quota_error"
    return "runtime_error"


def _summary_from_agent_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return redact_secrets(stripped)
    if isinstance(parsed, dict):
        return redact_secrets(json.dumps(parsed, sort_keys=True))
    return redact_secrets(stripped)


@dataclass
class ProcessHandle:
    task_id: str
    pid: int
    pgid: int
    command: list
    events_path: Path
    stderr_path: Path
    popen: subprocess.Popen


class CodexAdapter:
    name = "codex"
    capabilities = AdapterCapabilities(
        requires_subprocess=True,
        supports_process_group_cancellation=True,
        reports_broker_verified_tests=False,
        recovery_control=SUBPROCESS_CLI_RECOVERY_CONTROL,
    )

    def __init__(
        self,
        command_prefix=DEFAULT_COMMAND_PREFIX,
        model: str | None = None,
        grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS,
    ):
        self.command_prefix = tuple(command_prefix)
        self.model = model
        self.grace_period_seconds = grace_period_seconds

    @property
    def runtime_label(self) -> str:
        return self.command_prefix[-1] if self.command_prefix else self.name

    def check_availability(self, timeout: float = 10.0) -> dict:
        try:
            completed = subprocess.run(
                [*self.command_prefix, "--version"], capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            return {"available": False, "reason": "cli_not_found", "detail": f"{self.command_prefix[0]!r} was not found on PATH"}
        except subprocess.TimeoutExpired:
            return {"available": False, "reason": "version_check_timed_out", "detail": f"--version did not return within {timeout}s"}
        if completed.returncode != 0:
            detail = redact_secrets((completed.stderr or completed.stdout or "").strip()[:500])
            return {"available": False, "reason": "version_check_failed", "detail": detail}
        return {"available": True, "version": (completed.stdout or completed.stderr).strip()}

    def build_command(self, prompt: str, execution_mode: str, workspace: str, *, model: str | None = None) -> list:
        sandbox = SANDBOX_BY_EXECUTION_MODE.get(execution_mode)
        if sandbox is None:
            raise CodexUnsupportedPolicy(
                f"No validated Codex sandbox mapping for execution_mode={execution_mode!r}; "
                "refusing to launch rather than silently broadening privilege"
            )
        command = [
            *self.command_prefix, "exec",
            "--json",
            "--sandbox", sandbox,
            "--cd", workspace,
            "--skip-git-repo-check",
            "--ephemeral",
            prompt,
        ]
        effective_model = model if model is not None else self.model
        if effective_model:
            command += ["--model", effective_model]
        return command

    def start(self, record: TaskRecord, artifacts_dir: Path, workspace: str | None = None, *, prompt: str | None = None) -> tuple[dict, ProcessHandle]:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        events_path = artifacts_dir / "events.jsonl"
        stderr_path = artifacts_dir / "stderr.log"
        effective_workspace = workspace or record.workspace
        command = self.build_command(
            prompt or record.task, record.execution_mode, effective_workspace, model=record.effective_model,
        )
        with events_path.open("wb") as events_file, stderr_path.open("wb") as stderr_file:
            popen = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=events_file, stderr=stderr_file, start_new_session=True,
            )
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
            "runtime_description": RUNTIME_DESCRIPTION,
            "command": command,
            "pid": popen.pid,
            "pgid": pgid,
            "events_artifact": events_path.name,
            "stderr_artifact": stderr_path.name,
            "workspace": effective_workspace,
            "sandbox": SANDBOX_BY_EXECUTION_MODE[record.execution_mode],
        }
        return metadata, handle

    def cancel(self, handle: ProcessHandle) -> dict:
        return cancel_process_group(handle.popen, handle.pgid, self.grace_period_seconds)

    def collect(self, handle: ProcessHandle) -> dict:
        process_exit_code = handle.popen.wait()
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

        thread_id = None
        turn_failed_message = None
        usage = None
        summary = None
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                thread_id = event.get("thread_id")
            elif event_type == "turn.failed":
                error = event.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    turn_failed_message = error["message"]
            elif event_type == "turn.completed":
                usage = event.get("usage")
            elif event_type == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict) or item.get("type") != "agent_message":
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    summary = _summary_from_agent_text(text)

        is_error = turn_failed_message is not None or process_exit_code != 0
        error_category = None
        if turn_failed_message:
            error_category = _classify_runtime_error(turn_failed_message)
        elif process_exit_code != 0:
            error_category = "unparseable_output" if summary is None else "runtime_error"

        effective_exit_code = 1 if is_error and process_exit_code == 0 else process_exit_code
        stderr_text = handle.stderr_path.read_text(errors="replace") if handle.stderr_path.exists() else ""

        return {
            "exit_code": effective_exit_code,
            "process_exit_code": process_exit_code,
            "runtime_description": RUNTIME_DESCRIPTION,
            "events_count": len(events),
            "malformed_event_lines": malformed_event_lines,
            "summary": summary,
            "is_error": is_error,
            "error_category": error_category,
            "thread_id": thread_id,
            "usage": usage,
            "stderr_tail": redact_secrets(stderr_text[-4000:]),
            "verification": {"tests_broker_verified": False, "source": "runtime_reported"},
        }
