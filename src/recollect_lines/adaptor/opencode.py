"""Runtime adapter for the OpenCode CLI (`opencode run --format json`).

`opencode run --pure --format json --dir <workspace> <prompt>` streams
newline-delimited JSON events to stdout; the last `type: "text"` event (or its
nested `part.text`) is the task summary. Cancellation targets the process
group directly, exactly as the other subprocess-CLI adapters do, never a
claimed self-report.

Production launch path (RFC-004 durable-opencode slice, mirroring the merged
durable-Cursor/Claude-Code/Codex migrations): this adapter's job is narrow --
validate/build the OpenCode command into a `LaunchSpec`
(`adaptor.contracts.LaunchSpec`), and parse OpenCode's terminal stdout (the
JSONL event stream) and stderr into a normalized result via `parse_result()`.
It never calls `subprocess.Popen`, never waits/polls/kills a process group,
never creates stdout/stderr files, and never constructs a
`DurableSubprocessRunner` itself -- all of that lifecycle (launch, durable
persistence, artifact capture, cancellation, restart adoption, collection) is
owned by the broker and by
`durable_cli_launch`/`durable_runner.DurableSubprocessRunner`, which the
broker constructs once and injects here via `durable_runner=`.

This adapter carries no direct-Popen compatibility route: `start()`/
`cancel()`/`collect()` always go through the durable supervisor.

OpenCode's `--format json` flag makes its terminal stdout *be* the JSONL
event stream, and this codebase's established public artifact name for that
stream is `events.jsonl` (RFC-001). `build_launch_spec()` below requests
`LaunchSpec(stdout_artifact_name="events.jsonl")` so the generic durable
supervisor (see durable_runner.DurableSubprocessRunner.launch and
durable_cli_launch.start_durable_cli_launch) redirects the payload's stdout
into `events.jsonl` instead of the generic `stdout.log` default, and reports
`events_artifact: "events.jsonl"` -- preserving the public artifact name
without this adapter ever touching a file itself or standing up an
OpenCode-specific supervisor.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..durable_cli_launch import (
    cancel_durable_cli_launch,
    collect_durable_cli_launch,
    start_durable_cli_launch,
)
from ..durable_runner import DurableSubprocessRunner
from ..models import TaskRecord
from ..recovery_contract import DURABLE_SUBPROCESS_RECOVERY_CONTROL
from .cli_base import SubprocessCliAdapterBase
from .contracts import AdapterCapabilities, LaunchSpec

DEFAULT_COMMAND_PREFIX = ("npx", "--yes", "opencode-ai@1.17.18")
DEFAULT_GRACE_PERIOD_SECONDS = 10.0
RUNTIME_DESCRIPTION = "OpenCode via opencode run --pure --format json"

__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "DEFAULT_GRACE_PERIOD_SECONDS",
    "RUNTIME_DESCRIPTION",
    "OpenCodeAdapter",
]


class OpenCodeAdapter(SubprocessCliAdapterBase):
    name = "opencode"
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
        grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS,
        *,
        durable_runner: DurableSubprocessRunner | None = None,
    ):
        self.command_prefix = tuple(command_prefix)
        self.grace_period_seconds = grace_period_seconds
        # Broker-owned and broker-injected (see Broker.__init__); this adapter
        # never constructs one itself.
        self.durable_runner = durable_runner

    @property
    def runtime_label(self) -> str:
        return self.command_prefix[-1] if self.command_prefix else self.name

    def build_command(self, workspace: str, prompt: str) -> list:
        return [*self.command_prefix, "run", "--pure", "--format", "json", "--dir", workspace, prompt]

    def build_launch_spec(self, record: TaskRecord, workspace: str, prompt: str | None = None) -> LaunchSpec:
        """Validate/build the OpenCode command into a provider-neutral LaunchSpec.

        This is the one place OpenCode decides argv and cwd; it never touches
        a process, file, or the durable runner. `stdout_artifact_name`
        requests the durable supervisor capture stdout as `events.jsonl` --
        OpenCode's established public JSONL event-artifact name (see module
        docstring) -- instead of the generic `stdout.log` default.
        """
        effective_workspace = workspace or record.workspace
        command = self.build_command(effective_workspace, prompt or record.task)
        return LaunchSpec(argv=tuple(command), cwd=effective_workspace, stdout_artifact_name="events.jsonl")

    def start(self, record: TaskRecord, artifacts_dir: Path, workspace: str | None = None, *, prompt: str | None = None):
        if self.durable_runner is None:
            raise RuntimeError(
                "OpenCodeAdapter.durable_runner is unset; the owning Broker must inject one before start()"
            )
        effective_workspace = workspace or record.workspace
        spec = self.build_launch_spec(record, effective_workspace, prompt)
        metadata, handle = start_durable_cli_launch(
            self.durable_runner, record=record, adapter_id=self.name, spec=spec, artifacts_dir=artifacts_dir,
        )
        metadata = {
            **metadata,
            "runtime_description": RUNTIME_DESCRIPTION,
        }
        return metadata, handle

    def cancel(self, handle) -> dict:
        return cancel_durable_cli_launch(handle)

    def collect(self, handle) -> dict:
        return collect_durable_cli_launch(handle, parse_result=self.parse_result)

    def parse_result(self, *, stdout_text: str, stderr_text: str, process_exit_code: int) -> dict:
        """Parse OpenCode's `--format json` NDJSON terminal stdout into a
        normalized result. The only OpenCode-specific parsing in this
        codebase; the durable supervisor never interprets provider output
        itself.
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

        return {
            "exit_code": process_exit_code,
            "runtime_description": RUNTIME_DESCRIPTION,
            "events_count": len(events),
            "malformed_event_lines": malformed_event_lines,
            "summary": summary,
            "stderr_tail": stderr_text[-4000:],
            # ponytail: broker never independently re-runs tests in Phase 2, so this is
            # hardcoded false rather than derived; flip only once real verification exists.
            "verification": {"tests_broker_verified": False, "source": "runtime_reported"},
        }
