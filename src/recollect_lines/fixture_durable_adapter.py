"""Test-only durable subprocess adapter using DurableSubprocessRunner (Phase 7C.3).

Maps task keywords to local fixture payloads — never invokes a provider or network.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .adapters import AdapterCapabilities
from .durable_reconciliation import adopt_durable_handle, adopted_collect, wait_for_durable_running
from .durable_runner import (
    DurableLaunchHandle,
    DurableSubprocessRunner,
    STATE_CANCELLED,
    STATE_EXITED,
    STATE_FAILED,
    STATE_TIMED_OUT,
)
from .models import TaskRecord
from .recovery_contract import DURABLE_SUBPROCESS_RECOVERY_CONTROL

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"

KEYWORD_FIXTURES = {
    "DURABLE_SHORT": FIXTURES_DIR / "durable_short_payload.py",
    "DURABLE_LONG": FIXTURES_DIR / "durable_long_output.py",
    "DURABLE_HANG": FIXTURES_DIR / "durable_hang.py",
    "DURABLE_SECRET": FIXTURES_DIR / "durable_secret_echo.py",
}


@dataclass
class DurableFixtureHandle:
    task_id: str
    launch_id: str
    pid: int
    pgid: int
    command: list[str]
    events_path: Path
    stderr_path: Path
    durable: DurableLaunchHandle
    runner: DurableSubprocessRunner


class FixtureDurableAdapter:
    name = "fixture_durable"
    capabilities = AdapterCapabilities(
        requires_subprocess=True,
        supports_process_group_cancellation=True,
        reports_broker_verified_tests=False,
        recovery_control=DURABLE_SUBPROCESS_RECOVERY_CONTROL,
        uses_durable_subprocess_runner=True,
    )

    def __init__(
        self,
        home: Path,
        *,
        max_stdout_bytes: int = 64 * 1024,
        max_stderr_bytes: int = 16 * 1024,
        grace_period_seconds: float = 2.0,
    ):
        self.home = home
        self.runner = DurableSubprocessRunner(
            home,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            grace_seconds=grace_period_seconds,
        )
        self.grace_period_seconds = grace_period_seconds

    @property
    def runtime_label(self) -> str:
        return "fixture_durable_runner"

    def build_command(self, record: TaskRecord, workspace: str) -> list[str]:
        fixture = KEYWORD_FIXTURES.get(record.task.strip())
        if fixture is None:
            raise ValueError(
                f"fixture_durable task must use a known keyword ({', '.join(KEYWORD_FIXTURES)})"
            )
        command = [sys.executable, str(fixture)]
        if record.task.strip() == "DURABLE_SECRET":
            command.append("prompt-with-RL_SECRET_SENTINEL-in-argv")
        return command

    def start(self, record: TaskRecord, artifacts_dir: Path, workspace: str | None = None, *, prompt: str | None = None) -> tuple[dict, DurableFixtureHandle]:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        events_path = artifacts_dir / "events.jsonl"
        stderr_path = artifacts_dir / "stderr.log"
        command = self.build_command(record, workspace or record.workspace)
        durable = self.runner.launch(
            task_id=record.id,
            adapter_id=self.name,
            command=command,
        )
        launch_record = wait_for_durable_running(durable.manifest_path, supervisor=durable.supervisor)
        pid = launch_record.process["pid"]
        pgid = launch_record.process["pgid"]
        handle = DurableFixtureHandle(
            task_id=record.id,
            launch_id=durable.launch_id,
            pid=pid,
            pgid=pgid,
            command=command,
            events_path=events_path,
            stderr_path=stderr_path,
            durable=durable,
            runner=self.runner,
        )
        metadata = {
            "adapter": self.name,
            "command": command,
            "pid": pid,
            "pgid": pgid,
            "durable_launch_id": durable.launch_id,
            "launch_kind": "durable_subprocess",
            "events_artifact": events_path.name,
            "stderr_artifact": stderr_path.name,
            "workspace": workspace or record.workspace,
        }
        return metadata, handle

    def cancel(self, handle: DurableFixtureHandle) -> dict:
        record = self.runner.cancel(handle.durable)
        terminated = record.lifecycle_state in {STATE_EXITED, STATE_TIMED_OUT, STATE_CANCELLED, STATE_FAILED}
        return {
            "signals_sent": ["SIGTERM", "SIGKILL"] if terminated else [],
            "group_terminated": terminated,
            "exit_code": record.exit_status.get("code") if record.exit_status else None,
        }

    def collect(self, handle: DurableFixtureHandle) -> dict:
        self.runner.wait(handle.durable, timeout=30.0)
        adopted = adopt_durable_handle(
            self.home,
            task_id=handle.task_id,
            launch_id=handle.launch_id,
            adapter_id=self.name,
            terminal=True,
        )
        collected = adopted_collect(adopted)
        collected["adapter"] = self.name
        collected["durable_launch_id"] = handle.launch_id
        return collected
