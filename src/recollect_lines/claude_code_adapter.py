"""Runtime adapter that runs the Claude Code CLI (`claude -p`) as a supervised subprocess.

Command contract is grounded in a real compatibility spike against the
installed CLI (`claude` 2.1.201) — see docs/history/phases/phase-6a.md for the exact
commands and raw output this adapter's design is based on. Two findings from
that spike shape everything below:

- `--output-format json` (not `stream-json`) prints exactly one JSON object
  to stdout when the process exits, carrying `is_error`, `result`,
  `api_error_status`, `session_id`, and `permission_denials`. There is no
  incremental output to rely on for liveness — cancellation targets the
  process group directly, exactly as OpenCodeAdapter does, never a claimed
  self-report.
- Commander (the CLI's arg parser) treats `--disallowedTools`/`--allowedTools`
  as variadic: any bare, non-flag tokens *following* them on the argv are
  swallowed as additional tool names — including a prompt positional
  argument. build_command() always places the prompt immediately after `-p`
  and keeps every other flag (variadic ones last) after it, so nothing
  positional ever trails a variadic flag.

The broker owns cancellation evidence, exactly as for OpenCodeAdapter: Claude
Code is never trusted to report its own termination.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .adapters import AdapterCapabilities
from .claude_permission_mode_policy import (
    ClaudePermissionModeDecision,
    ClaudePermissionModePolicyError,
    permission_mode_policy_artifact,
    resolve_claude_permission_mode,
)
from .recovery_contract import SUBPROCESS_CLI_RECOVERY_CONTROL
from .models import TaskRecord
from .opencode_adapter import cancel_process_group

DEFAULT_COMMAND_PREFIX = ("claude",)
DEFAULT_GRACE_PERIOD_SECONDS = 10.0
RUNTIME_DESCRIPTION = "Claude Code via claude -p"

# Spike-validated (docs/history/phases/phase-6a.md): --permission-mode plan structurally
# refuses Edit/Write/NotebookEdit even when a task explicitly asks for a file
# to be created — it explains it is restricted to read-only actions instead.
# --disallowedTools is added as defense in depth, not the sole guarantee.
# acceptEdits is the narrowest mode confirmed (by the same spike) to actually
# write files; bypassPermissions/dontAsk/auto/manual are never mapped to,
# since they either bypass more than file edits or have no non-interactive
# safety evidence behind them.
#
# Reconciliation finding (2026-07-14, see docs/history/phases/phase-6a.md "Reconciliation
# addendum"): --disallowedTools Edit,Write,NotebookEdit alone leaves Bash
# nominally available in read_only mode — confirmed against the real CLI,
# a `whoami` call executed successfully via Bash under exactly this mapping.
# read_only's guarantee is meant to be structural, not cooperative (this is
# the phase's explicitly called-out critical-scope requirement), so
# --tools <allowlist> is now applied for read_only: this narrows the tool
# *set* itself, not just a deny-list, so Bash does not exist for the model to
# call at all, confirmed against the real CLI (it reports having no Bash
# tool). --disallowedTools stays layered on top as defense in depth.
#
# ponytail: read_only write-safety is structural (--tools/--disallowedTools), not
# permission-mode alone; task-aware policy picks plan vs dontAsk for read_only
# (see claude_permission_mode_policy.py). isolated_worktree always acceptEdits.
READ_ONLY_DISALLOWED_TOOLS = ("Edit", "Write", "NotebookEdit")
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")

REDACTED_VALUE = "***REDACTED***"
# Best-effort scrub applied only to the *concise* fields the broker folds into
# result.json / event metadata (summary, stderr tail, error detail) — never to
# the raw stdout/stderr artifact files, which are preserved byte-for-byte as
# forensic evidence per the adapter contract. Not exhaustive secret detection;
# matches the shape of credentials this CLI's own diagnostics could plausibly
# echo (an Anthropic API key, or a generic bearer token).
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}"),
)


def redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED_VALUE, text)
    return text


def _read_launch_policy_overrides(artifacts_dir: Path) -> tuple[str | None, str | None]:
    request_path = artifacts_dir / "request.json"
    if not request_path.is_file():
        return None, None
    try:
        payload = json.loads(request_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    task_category = payload.get("task_category")
    claude_permission_mode = payload.get("claude_permission_mode")
    if task_category is not None and not isinstance(task_category, str):
        task_category = None
    if claude_permission_mode is not None and not isinstance(claude_permission_mode, str):
        claude_permission_mode = None
    return task_category, claude_permission_mode


class ClaudeCodeUnsupportedPolicy(ClaudePermissionModePolicyError):
    """Raised when execution_mode or permission override is not permitted.

    Fail-closed by construction: raised from build_command() before any subprocess
    is spawned, so an unmapped policy never launches under a silently-broadened
    default permission mode.
    """


def _classify_runtime_error(api_error_status: object) -> str:
    if api_error_status in (401, 403):
        return "authentication_error"
    if api_error_status == 429:
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


class ClaudeCodeAdapter:
    name = "claude_code"
    capabilities = AdapterCapabilities(
        requires_subprocess=True,
        supports_process_group_cancellation=True,
        # No in-flight steering channel exists (see mcp_server.handle_message);
        # live `message` steering is unimplemented and unclaimed.
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
        """A human-readable adapter/version label for durable launch records."""
        return self.command_prefix[-1] if self.command_prefix else self.name

    def check_availability(self, timeout: float = 10.0) -> dict:
        """Best-effort, side-effect-free probe of whether the CLI is installed and runnable.

        Never touches auth or spends model quota — `--version` is a local,
        offline check. This is *availability* evidence only; an installed CLI
        that is not authenticated only surfaces that fact from a real `-p`
        invocation's result (see collect()'s error_category classification).
        """
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
        return {"available": True, "version": completed.stdout.strip()}

    def build_command(
        self,
        prompt: str,
        execution_mode: str,
        *,
        model: str | None = None,
        result_schema: str | None = None,
        agent_profile: str | None = None,
        task_category: str | None = None,
        claude_permission_mode: str | None = None,
    ) -> tuple[list, ClaudePermissionModeDecision]:
        try:
            decision = resolve_claude_permission_mode(
                execution_mode=execution_mode,
                result_schema=result_schema,
                agent_profile=agent_profile,
                task_category=task_category,
                claude_permission_mode=claude_permission_mode,
            )
        except ClaudePermissionModePolicyError as error:
            raise ClaudeCodeUnsupportedPolicy(str(error)) from error
        # Prompt goes immediately after -p, before any flag — see module
        # docstring for why order matters with commander's variadic options.
        command = [
            *self.command_prefix, "-p", prompt,
            "--output-format", "json",
            "--permission-mode", decision.permission_mode,
            "--no-session-persistence",
        ]
        effective_model = model if model is not None else self.model
        if effective_model:
            command += ["--model", effective_model]
        if execution_mode == "read_only":
            command += ["--tools", ",".join(READ_ONLY_TOOLS)]
            command += ["--disallowedTools", ",".join(READ_ONLY_DISALLOWED_TOOLS)]
        return command, decision

    def start(self, record: TaskRecord, artifacts_dir: Path, workspace: str | None = None, *, prompt: str | None = None) -> tuple[dict, ProcessHandle]:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifacts_dir / "stdout.log"
        stderr_path = artifacts_dir / "stderr.log"
        effective_workspace = workspace or record.workspace
        task_category, claude_permission_mode = _read_launch_policy_overrides(artifacts_dir)
        # Claude Code has no --dir/--workspace flag (unlike OpenCode); tool
        # access is scoped to the process's own cwd, so isolation depends on
        # launching in effective_workspace, not on an argument.
        command, decision = self.build_command(
            prompt or record.task,
            record.execution_mode,
            model=record.effective_model,
            result_schema=record.result_schema,
            agent_profile=record.agent_profile,
            task_category=task_category,
            claude_permission_mode=claude_permission_mode,
        )
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            popen = subprocess.Popen(
                command, stdout=stdout_file, stderr=stderr_file, cwd=effective_workspace, start_new_session=True,
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
            # Key names match OpenCodeAdapter's start() metadata shape so
            # Broker.start() can record a launch generically across adapters;
            # the file itself holds one captured JSON result, not an event
            # stream (see module docstring).
            "events_artifact": stdout_path.name,
            "stderr_artifact": stderr_path.name,
            "workspace": effective_workspace,
            "permission_mode": decision.permission_mode,
            "permission_mode_policy": permission_mode_policy_artifact(decision),
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

        is_error = bool(result_obj.get("is_error")) if result_obj is not None else process_exit_code != 0
        summary = None
        error_category = None
        if result_obj is not None:
            text = result_obj.get("result")
            if isinstance(text, str) and text.strip():
                summary = redact_secrets(text.strip())
            # Classify whenever the *task* failed, not just when the parsed JSON's
            # own is_error said so — a process that flushed a clean is_error:false
            # result but was then killed (e.g. an external timeout/OOM) before
            # exiting 0 is still a failure the broker must not leave uncategorized.
            if is_error or process_exit_code != 0:
                error_category = _classify_runtime_error(result_obj.get("api_error_status"))
        elif process_exit_code != 0:
            error_category = "unparseable_output"

        # A killed/cancelled run (empty or truncated stdout, is_error unknown)
        # is only distinguishable from a genuine failure by process_exit_code;
        # this normalizes both into one exit_code the broker can treat
        # generically (>0 == failed), the same contract OpenCodeAdapter uses,
        # without service.py needing any Claude-specific is_error awareness.
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
            "num_turns": result_obj.get("num_turns") if result_obj else None,
            "permission_denials": result_obj.get("permission_denials") if result_obj else None,
            "stderr_tail": redact_secrets(stderr_text[-4000:]),
            # ponytail: broker never independently re-runs tests here, so this is
            # hardcoded false rather than derived; matches OpenCodeAdapter.collect().
            "verification": {"tests_broker_verified": False, "source": "runtime_reported"},
        }
