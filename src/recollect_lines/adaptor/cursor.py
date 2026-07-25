"""Runtime adapter for the Cursor Agent CLI (`cursor-agent --print`).

Command contract is grounded in a compatibility spike against the installed CLI
(`cursor-agent` `2026.07.09-a3815c0`) — see docs/history/phases/phase-6b5.md. `--output-format
json` (not `--json`) prints exactly one JSON object to stdout when the process
exits, carrying `type`, `subtype`, `is_error`, `result`, `session_id`,
`duration_ms`, and `usage`. There is no incremental output to rely on for
liveness — cancellation targets the process group directly, never a claimed
self-report.

Production launch path (RFC-004 durable-cursor slice): this adapter's job is
narrow -- validate/build the Cursor command into a `LaunchSpec`
(`adaptor.contracts.LaunchSpec`), and parse Cursor's terminal stdout/stderr
into a normalized result via `parse_result()`. It never calls
`subprocess.Popen`, never waits/polls/kills a process group, never creates
stdout/stderr files, and never constructs a `DurableSubprocessRunner` itself
-- all of that lifecycle (launch, durable persistence, artifact capture,
cancellation, restart adoption, collection) is owned by the broker and by
`durable_cli_launch`/`durable_runner.DurableSubprocessRunner`, which the
broker constructs once and injects here via `durable_runner=`.

Historical `legacy_subprocess` launch records remain a broker/store recovery
concern during their sunset window. This adapter cannot create one and owns no
compatibility process handle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..durable_cli_launch import (
    DurableCliHandle,
    cancel_durable_cli_launch,
    collect_durable_cli_launch,
    start_durable_cli_launch,
)
from ..durable_runner import DurableSubprocessRunner
from ..models import TaskRecord
from ..recovery_contract import DURABLE_SUBPROCESS_RECOVERY_CONTROL
from .cli_base import SubprocessCliAdapterBase, probe_cli_version
from .contracts import AdapterCapabilities, LaunchSpec


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


class CursorAdapter(SubprocessCliAdapterBase):
    name = "cursor"
    capabilities = AdapterCapabilities(
        requires_subprocess=True,
        supports_process_group_cancellation=True,
        reports_broker_verified_tests=False,
        recovery_control=DURABLE_SUBPROCESS_RECOVERY_CONTROL,
        supported_result_schemas=frozenset({"plain-summary"}),
        uses_durable_subprocess_runner=True,
    )

    def __init__(
        self,
        command_prefix=DEFAULT_COMMAND_PREFIX,
        model: str | None = None,
        grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS,
        *,
        durable_runner: DurableSubprocessRunner | None = None,
    ):
        self.command_prefix = tuple(command_prefix)
        self.model = model
        self.grace_period_seconds = grace_period_seconds
        # Broker-owned and broker-injected (see Broker.__init__); this adapter
        # never constructs one itself.
        self.durable_runner = durable_runner

    @property
    def runtime_label(self) -> str:
        return self.command_prefix[-1] if self.command_prefix else self.name

    def check_availability(self, timeout: float = 10.0) -> dict:
        return probe_cli_version(self.command_prefix, timeout=timeout, redact_secrets=redact_secrets)

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

    def build_launch_spec(self, record: TaskRecord, workspace: str, prompt: str | None = None) -> LaunchSpec:
        """Validate/build the Cursor command into a provider-neutral LaunchSpec.

        This is the one place Cursor decides argv and cwd; it never touches a
        process, file, or the durable runner.
        """
        effective_workspace = workspace or record.workspace
        command = self.build_command(
            prompt or record.task, record.execution_mode, effective_workspace, model=record.effective_model,
        )
        return LaunchSpec(argv=tuple(command), cwd=effective_workspace)

    def start(self, record: TaskRecord, artifacts_dir: Path, workspace: str | None = None, *, prompt: str | None = None):
        if self.durable_runner is None:
            raise RuntimeError(
                "CursorAdapter.durable_runner is unset; the owning Broker must inject one before start()"
            )
        effective_workspace = workspace or record.workspace
        spec = self.build_launch_spec(record, effective_workspace, prompt)
        metadata, handle = start_durable_cli_launch(
            self.durable_runner, record=record, adapter_id=self.name, spec=spec, artifacts_dir=artifacts_dir,
        )
        metadata = {
            **metadata,
            "runtime_description": RUNTIME_DESCRIPTION,
            "sandbox": SANDBOX_BY_EXECUTION_MODE[record.execution_mode],
        }
        return metadata, handle

    def cancel(self, handle: DurableCliHandle) -> dict:
        return cancel_durable_cli_launch(handle)

    def collect(self, handle: DurableCliHandle) -> dict:
        return collect_durable_cli_launch(handle, parse_result=self.parse_result)

    def parse_result(self, *, stdout_text: str, stderr_text: str, process_exit_code: int) -> dict:
        """Parse Cursor's `--output-format json` terminal stdout into a
        normalized result. The only Cursor-specific parsing in this codebase;
        the durable supervisor never interprets provider output itself.
        """
        parsed_results = []
        malformed_output_lines = 0
        for line in stdout_text.splitlines():
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
