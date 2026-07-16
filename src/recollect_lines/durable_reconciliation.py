"""Safe broker-restart reconciliation for durable subprocess launches (Phase 7C.3).

Adopts surviving durable launches only after independent proof (manifest, task/launch
binding, PID+start-identity, process-group ownership). Adopted handles support
status, owned-group cancel, and terminal collect only — never redispatch or session resume.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .durable_runner import (
    DURABLE_LAUNCHES_DIR,
    DurableLaunchHandle,
    DurableLaunchRecord,
    DurableSubprocessRunner,
    LaunchInspection,
    LaunchInspectionOutcome,
    STATE_CANCELLED,
    STATE_EXITED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_TIMED_OUT,
    inspect_durable_launch,
    load_launch_record,
)
from .models import now

LAUNCH_KIND_DURABLE = "durable_subprocess"
LAUNCH_KIND_LEGACY = "legacy_subprocess"
DEFAULT_LEASE_TTL_SECONDS = 60.0
BROKER_INSTANCE_FILE = "broker_instance.json"
_COLLECT_REDACT_RE = re.compile(r"sk-[A-Za-z0-9_-]{4,}|rl_secret_sentinel\w*|RL_SECRET_SENTINEL", re.IGNORECASE)


def _redact_collected_text(text: str) -> str:
    return _COLLECT_REDACT_RE.sub("<redacted>", text)


class ReconcileOutcome(StrEnum):
    ADOPTED_RUNNING = "adopted_running"
    ADOPTED_TERMINAL_COLLECTABLE = "adopted_terminal_collectable"
    ALREADY_ADOPTED = "already_adopted"
    REFUSED_NOT_ELIGIBLE = "refused_not_eligible"
    REFUSED_CORRUPT = "refused_corrupt"
    REFUSED_PATH_REJECTED = "refused_path_rejected"
    REFUSED_IDENTITY_MISMATCH = "refused_identity_mismatch"
    REFUSED_NOT_ADOPTABLE_YET = "refused_not_adoptable_yet"
    REFUSED_BINDING_MISMATCH = "refused_binding_mismatch"
    REFUSED_LEASE_CONTENDED = "refused_lease_contended"
    REFUSED_STALE_DEAD = "refused_stale_dead"
    LEGACY_RECOVERY_REQUIRED = "legacy_recovery_required"


@dataclass(frozen=True)
class BrokerIdentity:
    broker_id: str
    epoch: int

    def to_dict(self) -> dict[str, Any]:
        return {"broker_id": self.broker_id, "epoch": self.epoch}


@dataclass(frozen=True)
class ReconcileDetail:
    outcome: ReconcileOutcome
    reason: str
    remediation: tuple[str, ...]
    launch_id: str | None = None
    adapter_id: str | None = None
    inspection_outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "remediation": list(self.remediation),
        }
        if self.launch_id is not None:
            payload["launch_id"] = self.launch_id
        if self.adapter_id is not None:
            payload["adapter_id"] = self.adapter_id
        if self.inspection_outcome is not None:
            payload["inspection_outcome"] = self.inspection_outcome
        return payload


@dataclass
class AdoptedDurableHandle:
    """In-memory adopted handle after successful reconciliation proof."""

    task_id: str
    launch_id: str
    adapter_id: str
    launch_dir: Path
    manifest_path: Path
    terminal: bool
    runner: DurableSubprocessRunner


def load_broker_identity(home: Path) -> BrokerIdentity:
    path = home / BROKER_INSTANCE_FILE
    home.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            raw = json.loads(path.read_text())
            broker_id = raw.get("broker_id")
            epoch = raw.get("epoch")
            if isinstance(broker_id, str) and broker_id.strip() and isinstance(epoch, int) and epoch >= 0:
                identity = BrokerIdentity(broker_id=broker_id, epoch=epoch + 1)
            else:
                identity = BrokerIdentity(broker_id=uuid.uuid4().hex, epoch=1)
        except (json.JSONDecodeError, OSError):
            identity = BrokerIdentity(broker_id=uuid.uuid4().hex, epoch=1)
    else:
        identity = BrokerIdentity(broker_id=uuid.uuid4().hex, epoch=1)
    path.write_text(json.dumps(identity.to_dict(), indent=2, sort_keys=True) + "\n")
    os.chmod(path, 0o600)
    return identity


def is_durable_launch_row(launch: dict[str, Any] | None) -> bool:
    return launch is not None and launch.get("launch_kind") == LAUNCH_KIND_DURABLE and bool(launch.get("durable_launch_id"))


def _inspection_to_outcome(inspection: LaunchInspection) -> ReconcileOutcome:
    mapping = {
        LaunchInspectionOutcome.CORRUPT: ReconcileOutcome.REFUSED_CORRUPT,
        LaunchInspectionOutcome.PATH_REJECTED: ReconcileOutcome.REFUSED_PATH_REJECTED,
        LaunchInspectionOutcome.IDENTITY_MISMATCH: ReconcileOutcome.REFUSED_IDENTITY_MISMATCH,
        LaunchInspectionOutcome.NOT_ADOPTABLE_YET: ReconcileOutcome.REFUSED_NOT_ADOPTABLE_YET,
    }
    return mapping.get(inspection.outcome, ReconcileOutcome.REFUSED_CORRUPT)


def _default_remediation(outcome: ReconcileOutcome) -> tuple[str, ...]:
    if outcome is ReconcileOutcome.REFUSED_LEASE_CONTENDED:
        return ("Wait for the active recovery lease to expire, then reconcile again.",)
    if outcome in {
        ReconcileOutcome.REFUSED_IDENTITY_MISMATCH,
        ReconcileOutcome.REFUSED_CORRUPT,
        ReconcileOutcome.REFUSED_PATH_REJECTED,
        ReconcileOutcome.REFUSED_BINDING_MISMATCH,
    }:
        return ("Do not signal the process; inspect launch artifacts manually or cancel from the owning broker.",)
    if outcome is ReconcileOutcome.REFUSED_NOT_ELIGIBLE:
        return ("This launch kind requires legacy recovery_required handling; durable adoption is unavailable.",)
    if outcome is ReconcileOutcome.ADOPTED_RUNNING:
        return ("Use status to observe; cancel via owned process group; collect only after the payload exits.",)
    if outcome is ReconcileOutcome.ADOPTED_TERMINAL_COLLECTABLE:
        return ("Call collect to gather bounded durable artifacts; no redispatch occurs.",)
    return ()


def evaluate_durable_reconciliation(
    home: Path,
    *,
    task_id: str,
    expected_adapter_id: str,
    durable_launch_id: str,
    launch_row_adapter: str,
) -> tuple[ReconcileOutcome, LaunchInspection | None, str]:
    """Proof gate only — does not acquire a lease or mutate broker state."""
    if launch_row_adapter != expected_adapter_id:
        return (
            ReconcileOutcome.REFUSED_BINDING_MISMATCH,
            None,
            "runtime_launches adapter does not match task profile adapter",
        )
    inspection = inspect_durable_launch(home, task_id=task_id, launch_id=durable_launch_id)
    if inspection.record is not None and inspection.record.adapter_id != expected_adapter_id:
        return (
            ReconcileOutcome.REFUSED_BINDING_MISMATCH,
            inspection,
            "manifest adapter_id does not match task profile adapter",
        )
    if inspection.outcome is LaunchInspectionOutcome.RUNNING_IDENTITY_MATCHES:
        return ReconcileOutcome.ADOPTED_RUNNING, inspection, inspection.reason
    if inspection.outcome is LaunchInspectionOutcome.EXITED:
        state = inspection.record.lifecycle_state if inspection.record else ""
        if state in {STATE_EXITED, STATE_TIMED_OUT, STATE_CANCELLED, STATE_FAILED}:
            return ReconcileOutcome.ADOPTED_TERMINAL_COLLECTABLE, inspection, "terminal lifecycle state"
        return ReconcileOutcome.REFUSED_STALE_DEAD, inspection, f"unexpected terminal state: {state}"
    if inspection.outcome is LaunchInspectionOutcome.IDENTITY_MISMATCH:
        return ReconcileOutcome.REFUSED_IDENTITY_MISMATCH, inspection, inspection.reason
    return _inspection_to_outcome(inspection), inspection, inspection.reason


def adopt_durable_handle(
    home: Path,
    *,
    task_id: str,
    launch_id: str,
    adapter_id: str,
    terminal: bool,
    max_stdout_bytes: int | None = None,
    max_stderr_bytes: int | None = None,
) -> AdoptedDurableHandle:
    launch_dir = (home / DURABLE_LAUNCHES_DIR / launch_id).resolve()
    manifest_path = launch_dir / "manifest.json"
    kwargs: dict[str, Any] = {}
    if max_stdout_bytes is not None:
        kwargs["max_stdout_bytes"] = max_stdout_bytes
    if max_stderr_bytes is not None:
        kwargs["max_stderr_bytes"] = max_stderr_bytes
    runner = DurableSubprocessRunner(home, **kwargs)
    return AdoptedDurableHandle(
        task_id=task_id,
        launch_id=launch_id,
        adapter_id=adapter_id,
        launch_dir=launch_dir,
        manifest_path=manifest_path,
        terminal=terminal,
        runner=runner,
    )


def adopted_status(handle: AdoptedDurableHandle) -> dict[str, Any]:
    record = load_launch_record(handle.manifest_path)
    return {
        "adopted": True,
        "launch_id": handle.launch_id,
        "adapter_id": handle.adapter_id,
        "lifecycle_state": record.lifecycle_state,
        "terminal": handle.terminal,
        "process": {
            "pid": record.process.get("pid"),
            "pgid": record.process.get("pgid"),
            "start_identity_present": bool(record.process.get("start_identity")),
        },
        "artifacts": {
            name: {
                "bytes": meta.get("bytes"),
                "complete": meta.get("complete"),
                "truncated": meta.get("truncated"),
            }
            for name, meta in record.artifacts.items()
            if isinstance(meta, dict)
        },
    }


def adopted_cancel(handle: AdoptedDurableHandle) -> dict[str, Any]:
    record = load_launch_record(handle.manifest_path)
    if record.lifecycle_state in {STATE_EXITED, STATE_TIMED_OUT, STATE_CANCELLED, STATE_FAILED}:
        return {"group_terminated": True, "signals_sent": [], "note": "already_terminal"}
    inspection = inspect_durable_launch(
        handle.runner.home,
        task_id=handle.task_id,
        launch_id=handle.launch_id,
    )
    if inspection.outcome is not LaunchInspectionOutcome.RUNNING_IDENTITY_MATCHES:
        return {
            "group_terminated": False,
            "signals_sent": [],
            "note": "identity_proof_failed",
            "reason": inspection.reason,
        }
    pgid = record.process.get("pgid")
    if not isinstance(pgid, int) or pgid <= 0:
        return {"group_terminated": False, "signals_sent": [], "note": "invalid_pgid"}
    signals_sent: list[str] = []
    try:
        os.killpg(pgid, signal.SIGTERM)
        signals_sent.append("SIGTERM")
    except ProcessLookupError:
        return {"group_terminated": True, "signals_sent": signals_sent, "note": "group_already_gone"}
    deadline = time.monotonic() + handle.runner.grace_seconds
    while time.monotonic() < deadline:
        refreshed = inspect_durable_launch(
            handle.runner.home,
            task_id=handle.task_id,
            launch_id=handle.launch_id,
        )
        if refreshed.outcome is not LaunchInspectionOutcome.RUNNING_IDENTITY_MATCHES:
            break
        time.sleep(0.05)
    refreshed = inspect_durable_launch(
        handle.runner.home,
        task_id=handle.task_id,
        launch_id=handle.launch_id,
    )
    if refreshed.outcome is LaunchInspectionOutcome.RUNNING_IDENTITY_MATCHES:
        try:
            os.killpg(pgid, signal.SIGKILL)
            signals_sent.append("SIGKILL")
        except ProcessLookupError:
            pass
    final = inspect_durable_launch(
        handle.runner.home,
        task_id=handle.task_id,
        launch_id=handle.launch_id,
    )
    terminated = final.outcome is not LaunchInspectionOutcome.RUNNING_IDENTITY_MATCHES
    return {"group_terminated": terminated, "signals_sent": signals_sent, "exit_code": None}


def adopted_collect(handle: AdoptedDurableHandle) -> dict[str, Any]:
    record = load_launch_record(handle.manifest_path)
    if record.lifecycle_state == STATE_RUNNING:
        inspection = inspect_durable_launch(
            handle.runner.home,
            task_id=handle.task_id,
            launch_id=handle.launch_id,
        )
        if inspection.outcome is LaunchInspectionOutcome.RUNNING_IDENTITY_MATCHES:
            raise ValueError("durable payload still running; collect refused until terminal")
    exit_code = None
    if record.exit_status and isinstance(record.exit_status, dict):
        exit_code = record.exit_status.get("code")
    stdout_tail = ""
    stderr_tail = ""
    stdout_path = handle.launch_dir / "stdout.log"
    stderr_path = handle.launch_dir / "stderr.log"
    if stdout_path.is_file():
        stdout_tail = _redact_collected_text(stdout_path.read_text(errors="replace")[-4000:])
    if stderr_path.is_file():
        stderr_tail = _redact_collected_text(stderr_path.read_text(errors="replace")[-4000:])
    summary = stdout_tail.strip().splitlines()[-1] if stdout_tail.strip() else None
    return {
        "exit_code": exit_code if exit_code is not None else 127,
        "summary": summary,
        "stderr_tail": stderr_tail,
        "events_count": 0,
        "malformed_event_lines": 0,
        "durable_artifacts": {
            "stdout_bytes": record.artifacts.get("stdout", {}).get("bytes", 0),
            "stderr_bytes": record.artifacts.get("stderr", {}).get("bytes", 0),
            "stdout_truncated": record.artifacts.get("stdout", {}).get("truncated", False),
        },
        "verification": {"tests_broker_verified": False, "source": "durable_runner_artifacts"},
    }


_TERMINAL_LAUNCH_STATES = frozenset({STATE_EXITED, STATE_TIMED_OUT, STATE_CANCELLED, STATE_FAILED})


def _launch_proof_committed(record: DurableLaunchRecord) -> bool:
    process = record.process
    pid = process.get("pid")
    pgid = process.get("pgid")
    start_identity = process.get("start_identity")
    return (
        isinstance(pid, int)
        and pid > 0
        and isinstance(pgid, int)
        and pgid > 0
        and isinstance(start_identity, str)
        and bool(start_identity.strip())
    )


def _durable_launch_ready(record: DurableLaunchRecord) -> bool:
    if record.lifecycle_state == STATE_RUNNING:
        return True
    return record.lifecycle_state in _TERMINAL_LAUNCH_STATES and _launch_proof_committed(record)


def wait_for_durable_running(
    manifest_path: Path,
    *,
    timeout: float = 10.0,
    interval: float = 0.05,
    supervisor: subprocess.Popen[str] | None = None,
) -> DurableLaunchRecord:
    """Wait until the supervisor commits launch proof in the manifest.

    Fast payloads may move running -> exited between polls; terminal manifests
    with pid/pgid/start_identity are accepted once proof is durable on disk.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            record = load_launch_record(manifest_path)
            if _durable_launch_ready(record):
                return record
        except ValueError as error:
            last_error = error
        if supervisor is not None and supervisor.poll() is not None:
            try:
                record = load_launch_record(manifest_path)
            except ValueError as error:
                last_error = error
            else:
                if _durable_launch_ready(record):
                    return record
                if record.lifecycle_state == STATE_LAUNCHING:
                    raise TimeoutError(
                        "durable launch supervisor exited without committing running proof",
                    ) from last_error
        time.sleep(interval)
    if last_error is not None:
        raise TimeoutError(f"durable launch did not reach running state: {last_error}") from last_error
    raise TimeoutError("durable launch did not reach running state before timeout")


def make_reconcile_detail(
    outcome: ReconcileOutcome,
    reason: str,
    *,
    launch_id: str | None = None,
    adapter_id: str | None = None,
    inspection: LaunchInspection | None = None,
) -> ReconcileDetail:
    return ReconcileDetail(
        outcome=outcome,
        reason=reason,
        remediation=_default_remediation(outcome),
        launch_id=launch_id,
        adapter_id=adapter_id,
        inspection_outcome=inspection.outcome.value if inspection else None,
    )


def durable_launch_handle_from_record(record: DurableLaunchRecord) -> DurableLaunchHandle:
    return DurableLaunchHandle(
        launch_id=record.launch_id,
        task_id=record.task_id,
        launch_dir=record.launch_dir,
        manifest_path=record.launch_dir / "manifest.json",
        supervisor=None,
    )
