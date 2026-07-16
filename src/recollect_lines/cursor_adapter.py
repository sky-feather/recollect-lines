"""Runtime adapter that runs the Cursor Agent CLI (`cursor-agent --print`) as a supervised subprocess.

Command contract is grounded in a compatibility spike against the installed CLI
(`cursor-agent` `2026.07.09-a3815c0`) — see docs/history/phases/phase-6b5.md. `--output-format
json` (not `--json`) prints exactly one JSON object to stdout when the process
exits, carrying `type`, `subtype`, `is_error`, `result`, `session_id`,
`duration_ms`, and `usage`. There is no incremental output to rely on for
liveness — cancellation targets the process group directly, exactly as
OpenCodeAdapter does, never a claimed self-report.

The broker owns cancellation evidence: Cursor Agent CLI is never trusted to
report its own termination.
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

DEFAULT_COMMAND_PREFIX = ("cursor-agent",)
DEFAULT_GRACE_PERIOD_SECONDS = 10.0
RUNTIME_DESCRIPTION = "Cursor Agent CLI via cursor-agent --print"

# Spike-validated (docs/history/phases/phase-6b5.md): `--sandbox enabled` plus `--mode plan`
# structurally limits the agent to read-only/planning behavior; `--sandbox
# disabled` with `--force` is the narrowest mode confirmed for isolated_worktree
# tasks that must edit files inside the broker-owned worktree. Cursor does not
# advertise a finer native read-only/workspace-write permission model than
# sandbox enabled/disabled plus plan mode — do not invent one here.
#
# ponytail: only the two execution_modes the broker currently defines are
# mapped; build_command() fails closed (raises) for anything else.
SANDBOX_BY_EXECUTION_MODE = {
    "read_only": "enabled",
    "isolated_worktree": "disabled",
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


class CursorUnsupportedPolicy(ValueError):
    """Raised when an execution_mode has no validated Cursor sandbox/mode mapping."""


def _classify_runtime_error(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("401", "403", "unauthorized", "authentication", "not logged in", "login")):
        return "authentication_error"
    if any(token in lowered for token in ("429", "rate limit", "quota", "too many requests")):
        return "rate_limit_or_quota_error"
    return "runtime_error"


@dataclass
class ProcessHandle:
    task_id: str
    pid: int
    pgid: int
    command: list
    stdout_path: Path
    stderr_path: Path
    popen: subprocess.Popen


class CursorAdapter:
    name = "cursor"
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
            raise CursorUnsupportedPolicy(
                f"No validated Cursor sandbox mapping for execution_mode={execution_mode!r}; "
                "refusing to launch rather than silently broadening privilege"
            )
        command = [
            *self.command_prefix,
            "--print", "--trust", "--force",
            "--output-format", "json",
            "--sandbox", sandbox,
            "--workspace", workspace,
        ]
        if execution_mode == "read_only":
            command += ["--mode", "plan"]
        effective_model = model if model is not None else self.model
        if effective_model:
            command += ["--model", effective_model]
        command.append(prompt)
        return command

    def start(self, record: TaskRecord, artifacts_dir: Path, workspace: str | None = None, *, prompt: str | None = None) -> tuple[dict, ProcessHandle]:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifacts_dir / "stdout.log"
        stderr_path = artifacts_dir / "stderr.log"
        effective_workspace = workspace or record.workspace
        command = self.build_command(
            prompt or record.task, record.execution_mode, effective_workspace, model=record.effective_model,
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
        handle = ProcessHandle(
            task_id=record.id,
            pid=popen.pid,
            pgid=pgid,
            command=command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            popen=popen,
        )
        metadata = {
            "adapter": self.name,
            "runtime_description": RUNTIME_DESCRIPTION,
            "command": command,
            "pid": popen.pid,
            "pgid": pgid,
            "events_artifact": stdout_path.name,
            "stderr_artifact": stderr_path.name,
            "workspace": effective_workspace,
            "sandbox": SANDBOX_BY_EXECUTION_MODE[record.execution_mode],
        }
        return metadata, handle

    def cancel(self, handle: ProcessHandle) -> dict:
        return cancel_process_group(handle.popen, handle.pgid, self.grace_period_seconds)

    def collect(self, handle: ProcessHandle) -> dict:
        process_exit_code = handle.popen.wait()
        raw_stdout = handle.stdout_path.read_text(errors="replace") if handle.stdout_path.exists() else ""
        stderr_text = handle.stderr_path.read_text(errors="replace") if handle.stderr_path.exists() else ""

        parsed_results = []
        malformed_output_lines = 0
        for line in raw_stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed_results.append(json.loads(line))
            except json.JSONDecodeError:
                malformed_output_lines += 1
        result_obj = parsed_results[-1] if parsed_results and isinstance(parsed_results[-1], dict) else None

        is_error = False
        if result_obj is not None:
            is_error = bool(result_obj.get("is_error")) or result_obj.get("subtype") not in (None, "success")
        elif process_exit_code != 0:
            is_error = True

        summary = None
        error_category = None
        if result_obj is not None:
            text = result_obj.get("result")
            if isinstance(text, str) and text.strip():
                summary = redact_secrets(text.strip())
            if is_error or process_exit_code != 0:
                error_message = text if isinstance(text, str) else ""
                error_category = _classify_runtime_error(error_message)
        elif process_exit_code != 0:
            error_category = "unparseable_output"

        effective_exit_code = 1 if is_error and process_exit_code == 0 else process_exit_code

        return {
            "exit_code": effective_exit_code,
            "process_exit_code": process_exit_code,
            "runtime_description": RUNTIME_DESCRIPTION,
            "malformed_output_lines": malformed_output_lines,
            "parsed_result_count": len(parsed_results),
            "summary": summary,
            "is_error": is_error,
            "error_category": error_category,
            "session_id": result_obj.get("session_id") if result_obj else None,
            "duration_ms": result_obj.get("duration_ms") if result_obj else None,
            "usage": result_obj.get("usage") if result_obj else None,
            "stderr_tail": redact_secrets(stderr_text[-4000:]),
            "verification": {"tests_broker_verified": False, "source": "runtime_reported"},
        }
