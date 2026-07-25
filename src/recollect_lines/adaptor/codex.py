"""Runtime adapter for the Codex CLI (`codex exec --json`).

Command contract is grounded in a compatibility spike against the installed CLI
(codex-cli 0.144.4) — see docs/history/phases/phase-6b.md. `codex exec --json` streams
newline-delimited JSON events (`thread.started`, `turn.started`, `item.*`,
`turn.completed`/`turn.failed`) to stdout; cancellation targets the process
group directly, never a claimed self-report.

Production launch path (RFC-004 durable-codex slice, mirroring the merged
durable-Cursor/Claude-Code migrations): this adapter's job is narrow --
validate/build the Codex command into a `LaunchSpec`
(`adaptor.contracts.LaunchSpec`), and parse Codex's terminal stdout (the JSONL
event stream) and stderr into a normalized result via `parse_result()`. It
never calls `subprocess.Popen`, never waits/polls/kills a process group, never
creates stdout/stderr files, and never constructs a `DurableSubprocessRunner`
itself -- all of that lifecycle (launch, durable persistence, artifact
capture, cancellation, restart adoption, collection) is owned by the broker
and by `durable_cli_launch`/`durable_runner.DurableSubprocessRunner`, which
the broker constructs once and injects here via `durable_runner=`.

This adapter carries no direct-Popen compatibility route: `start()`/
`cancel()`/`collect()` always go through the durable supervisor.

Codex's `--json` flag makes its terminal stdout *be* the JSONL event stream,
and this codebase's established public artifact name for that stream is
`events.jsonl` (RFC-001; the pre-RFC-004 Codex adapter and the still-legacy
OpenCode adapter both write it under that name). `build_launch_spec()` below
requests `LaunchSpec(stdout_artifact_name="events.jsonl")` so the generic
durable supervisor (see durable_runner.DurableSubprocessRunner.launch and
durable_cli_launch.start_durable_cli_launch) redirects the payload's stdout
into `events.jsonl` instead of the generic `stdout.log` default, and reports
`events_artifact: "events.jsonl"` -- preserving the public artifact name
without this adapter ever touching a file itself or standing up a
Codex-specific supervisor.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..durable_cli_launch import (
    cancel_durable_cli_launch,
    collect_durable_cli_launch,
    start_durable_cli_launch,
)
from ..durable_runner import DurableSubprocessRunner
from ..models import TaskRecord
from ..recovery_contract import DURABLE_SUBPROCESS_RECOVERY_CONTROL
from .cli_base import SubprocessCliAdapterBase, probe_cli_version
from .contracts import AdapterCapabilities, LaunchSpec

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


class CodexAdapter(SubprocessCliAdapterBase):
    name = "codex"
    capabilities = AdapterCapabilities(
        requires_subprocess=True,
        supports_process_group_cancellation=True,
        reports_broker_verified_tests=False,
        recovery_control=DURABLE_SUBPROCESS_RECOVERY_CONTROL,
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

    def build_launch_spec(self, record: TaskRecord, workspace: str, prompt: str | None = None) -> LaunchSpec:
        """Validate/build the Codex command into a provider-neutral LaunchSpec.

        This is the one place Codex decides argv and cwd; it never touches a
        process, file, or the durable runner. `stdout_artifact_name` requests
        the durable supervisor capture stdout as `events.jsonl` -- Codex's
        established public JSONL event-artifact name (see module docstring)
        -- instead of the generic `stdout.log` default.
        """
        effective_workspace = workspace or record.workspace
        command = self.build_command(
            prompt or record.task, record.execution_mode, effective_workspace, model=record.effective_model,
        )
        return LaunchSpec(argv=tuple(command), cwd=effective_workspace, stdout_artifact_name="events.jsonl")

    def start(self, record: TaskRecord, artifacts_dir: Path, workspace: str | None = None, *, prompt: str | None = None):
        if self.durable_runner is None:
            raise RuntimeError(
                "CodexAdapter.durable_runner is unset; the owning Broker must inject one before start()"
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

    def cancel(self, handle) -> dict:
        return cancel_durable_cli_launch(handle)

    def collect(self, handle) -> dict:
        return collect_durable_cli_launch(handle, parse_result=self.parse_result)

    def parse_result(self, *, stdout_text: str, stderr_text: str, process_exit_code: int) -> dict:
        """Parse Codex's `--json` NDJSON terminal stdout into a normalized
        result. The only Codex-specific parsing in this codebase; the durable
        supervisor never interprets provider output itself.
        """
        events = []
        malformed_event_lines = 0
        for line in stdout_text.splitlines():
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
