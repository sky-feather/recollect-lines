"""Durable subprocess runner primitive (Phase 7C.2).

Provides crash-safe launch proof, bounded owner-private artifacts, and a
read-only inspection API for a future reconciler (Phase 7C.3). This module
does **not** adopt/reconnect broker handles or elevate recovery levels.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import now

DURABLE_LAUNCH_SCHEMA_VERSION = "1"
DURABLE_LAUNCHES_DIR = "durable_launches"
MANIFEST_NAME = "manifest.json"
STDOUT_NAME = "stdout.log"
STDERR_NAME = "stderr.log"
RESULT_NAME = "result.json"

DEFAULT_MAX_STDOUT_BYTES = 64 * 1024
DEFAULT_MAX_STDERR_BYTES = 16 * 1024
DEFAULT_GRACE_SECONDS = 2.0

# Manifest lifecycle states (durable runner truth, not broker task states).
STATE_LAUNCHING = "launching"
STATE_RUNNING = "running"
STATE_EXITED = "exited"
STATE_TIMED_OUT = "timed_out"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"

_SECRET_VALUE_RE = re.compile(r"sk-[A-Za-z0-9_-]{4,}|rl_secret_sentinel", re.IGNORECASE)


class LaunchInspectionOutcome(StrEnum):
    RUNNING_IDENTITY_MATCHES = "running_identity_matches"
    EXITED = "exited"
    CORRUPT = "corrupt"
    PATH_REJECTED = "path_rejected"
    IDENTITY_MISMATCH = "identity_mismatch"
    NOT_ADOPTABLE_YET = "not_adoptable_yet"


@dataclass(frozen=True)
class DurableLaunchRecord:
    schema_version: str
    launch_id: str
    task_id: str
    adapter_id: str
    created_at: str
    updated_at: str
    lifecycle_state: str
    process: dict[str, Any]
    artifacts: dict[str, Any]
    exit_status: dict[str, Any] | None
    diagnostics: dict[str, Any]
    launch_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "launch_id": self.launch_id,
            "task_id": self.task_id,
            "adapter_id": self.adapter_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "lifecycle_state": self.lifecycle_state,
            "process": dict(self.process),
            "artifacts": dict(self.artifacts),
            "exit_status": dict(self.exit_status) if self.exit_status else None,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class LaunchInspection:
    outcome: LaunchInspectionOutcome
    record: DurableLaunchRecord | None
    reason: str
    details: dict[str, Any]


@dataclass
class DurableLaunchHandle:
    launch_id: str
    task_id: str
    launch_dir: Path
    manifest_path: Path
    supervisor: subprocess.Popen[str] | None = None


class DurableSubprocessRunner:
    """Crash-safe subprocess launcher with durable bounded artifacts."""

    def __init__(
        self,
        home: Path,
        *,
        max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
    ):
        self.home = home.resolve()
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.grace_seconds = grace_seconds
        self._launches_root = self.home / DURABLE_LAUNCHES_DIR

    def launch(
        self,
        *,
        task_id: str,
        adapter_id: str,
        command: list[str],
        detach_supervisor: bool = False,
    ) -> DurableLaunchHandle:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        if not adapter_id.strip():
            raise ValueError("adapter_id must not be empty")
        if not command:
            raise ValueError("command must not be empty")
        launch_id = uuid.uuid4().hex
        launch_dir = _safe_launch_dir(self._launches_root, launch_id)
        launch_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(launch_dir)
        timestamp = now()
        manifest = _base_manifest(
            launch_id=launch_id,
            task_id=task_id,
            adapter_id=adapter_id,
            created_at=timestamp,
            updated_at=timestamp,
            lifecycle_state=STATE_LAUNCHING,
        )
        manifest_path = launch_dir / MANIFEST_NAME
        _atomic_write_json(manifest_path, manifest)
        supervise_cmd = [
            sys.executable,
            "-m",
            "recollect_lines.durable_runner",
            "__supervise__",
            str(manifest_path),
            str(launch_dir),
            str(self.max_stdout_bytes),
            str(self.max_stderr_bytes),
            *command,
        ]
        supervisor = subprocess.Popen(
            supervise_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return DurableLaunchHandle(launch_id, task_id, launch_dir, manifest_path, supervisor)

    def wait(self, handle: DurableLaunchHandle, *, timeout: float | None = None) -> DurableLaunchRecord:
        timed_out = False
        if handle.supervisor is not None:
            try:
                handle.supervisor.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.cancel(handle)
        record = load_launch_record(handle.manifest_path)
        if timed_out and record.lifecycle_state == STATE_CANCELLED:
            manifest = _read_manifest_dict(handle.manifest_path)
            manifest["lifecycle_state"] = STATE_TIMED_OUT
            manifest["updated_at"] = now()
            manifest.setdefault("diagnostics", {})["timeout"] = True
            _atomic_write_json(handle.manifest_path, manifest)
            record = load_launch_record(handle.manifest_path)
        return record

    def cancel(self, handle: DurableLaunchHandle) -> DurableLaunchRecord:
        record = load_launch_record(handle.manifest_path)
        if record.lifecycle_state in {STATE_EXITED, STATE_TIMED_OUT, STATE_CANCELLED, STATE_FAILED}:
            return record
        if not _verify_live_process(record):
            return load_launch_record(handle.manifest_path)
        pgid = record.process.get("pgid")
        if not isinstance(pgid, int) or pgid <= 0:
            return load_launch_record(handle.manifest_path)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return load_launch_record(handle.manifest_path)
        deadline = time.monotonic() + self.grace_seconds
        while time.monotonic() < deadline:
            if not _verify_live_process(record):
                break
            time.sleep(0.05)
        if _verify_live_process(record):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if handle.supervisor is not None and handle.supervisor.poll() is None:
            try:
                handle.supervisor.wait(timeout=self.grace_seconds)
            except subprocess.TimeoutExpired:
                handle.supervisor.kill()
                handle.supervisor.wait(timeout=self.grace_seconds)
        return load_launch_record(handle.manifest_path)


def inspect_durable_launch(
    home: Path,
    *,
    task_id: str,
    launch_id: str,
) -> LaunchInspection:
    """Read-only, fail-closed inspection for a future reconciler (7C.3)."""
    home = home.resolve()
    try:
        launch_dir = _safe_launch_dir(home / DURABLE_LAUNCHES_DIR, launch_id)
    except ValueError as error:
        return LaunchInspection(LaunchInspectionOutcome.PATH_REJECTED, None, str(error), {})
    manifest_path = launch_dir / MANIFEST_NAME
    if not _path_within(manifest_path, launch_dir):
        return LaunchInspection(LaunchInspectionOutcome.PATH_REJECTED, None, "manifest path escapes launch dir", {})
    if not manifest_path.is_file():
        return LaunchInspection(LaunchInspectionOutcome.CORRUPT, None, "manifest missing", {})
    try:
        record = load_launch_record(manifest_path)
    except ValueError as error:
        return LaunchInspection(LaunchInspectionOutcome.CORRUPT, None, str(error), {})
    if record.task_id != task_id:
        return LaunchInspection(
            LaunchInspectionOutcome.PATH_REJECTED,
            record,
            "task_id does not match manifest",
            {"expected_task_id": task_id, "manifest_task_id": record.task_id},
        )
    if not _path_within(record.launch_dir, home / DURABLE_LAUNCHES_DIR):
        return LaunchInspection(LaunchInspectionOutcome.PATH_REJECTED, record, "launch dir escapes home", {})
    if record.launch_id != launch_id:
        return LaunchInspection(LaunchInspectionOutcome.CORRUPT, record, "launch_id mismatch", {})
    if record.lifecycle_state in {STATE_EXITED, STATE_TIMED_OUT, STATE_CANCELLED, STATE_FAILED}:
        return LaunchInspection(LaunchInspectionOutcome.EXITED, record, "terminal lifecycle state", {"state": record.lifecycle_state})
    if record.lifecycle_state == STATE_LAUNCHING:
        return LaunchInspection(LaunchInspectionOutcome.NOT_ADOPTABLE_YET, record, "launch proof not yet committed", {})
    if record.lifecycle_state != STATE_RUNNING:
        return LaunchInspection(LaunchInspectionOutcome.CORRUPT, record, f"unknown lifecycle state: {record.lifecycle_state}", {})
    if not _verify_live_process(record):
        return LaunchInspection(
            LaunchInspectionOutcome.IDENTITY_MISMATCH,
            record,
            "process identity does not match manifest (PID reuse or process exited)",
            {},
        )
    return LaunchInspection(LaunchInspectionOutcome.RUNNING_IDENTITY_MATCHES, record, "running identity matches manifest", {})


def load_launch_record(manifest_path: Path) -> DurableLaunchRecord:
    manifest_path = manifest_path.resolve()
    launch_dir = manifest_path.parent.resolve()
    if manifest_path.name != MANIFEST_NAME:
        raise ValueError("not a launch manifest path")
    if not manifest_path.is_file():
        raise ValueError("manifest missing")
    try:
        raw = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as error:
        raise ValueError(f"manifest is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("manifest must be an object")
    if raw.get("schema_version") != DURABLE_LAUNCH_SCHEMA_VERSION:
        raise ValueError("unsupported manifest schema_version")
    for key in ("launch_id", "task_id", "adapter_id", "created_at", "updated_at", "lifecycle_state"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError(f"manifest field {key!r} invalid")
    process = raw.get("process")
    artifacts = raw.get("artifacts")
    diagnostics = raw.get("diagnostics")
    if not isinstance(process, dict):
        raise ValueError("manifest process must be an object")
    if not isinstance(artifacts, dict):
        raise ValueError("manifest artifacts must be an object")
    if not isinstance(diagnostics, dict):
        raise ValueError("manifest diagnostics must be an object")
    exit_status = raw.get("exit_status")
    if exit_status is not None and not isinstance(exit_status, dict):
        raise ValueError("manifest exit_status must be an object or null")
    _reject_secrets_in_mapping(raw)
    return DurableLaunchRecord(
        schema_version=raw["schema_version"],
        launch_id=raw["launch_id"],
        task_id=raw["task_id"],
        adapter_id=raw["adapter_id"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        lifecycle_state=raw["lifecycle_state"],
        process=process,
        artifacts=artifacts,
        exit_status=exit_status,
        diagnostics=diagnostics,
        launch_dir=launch_dir,
    )


def _supervise_main(argv: list[str]) -> int:
    if len(argv) < 5:
        print(
            "usage: durable_runner __supervise__ <manifest> <launch_dir> <max_stdout> <max_stderr> <command...>",
            file=sys.stderr,
        )
        return 2
    manifest_path = Path(argv[1]).resolve()
    launch_dir = Path(argv[2]).resolve()
    max_stdout = int(argv[3])
    max_stderr = int(argv[4])
    command = argv[5:]
    if not _path_within(manifest_path, launch_dir):
        return 2
    stdout_path = launch_dir / STDOUT_NAME
    stderr_path = launch_dir / STDERR_NAME
    stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.setsid()
        except OSError:
            pass
        payload_pid = os.getpid()
        try:
            pgid = os.getpgid(0)
        except OSError:
            pgid = payload_pid
        start_identity = _read_process_start_identity(payload_pid)
        if start_identity is None:
            os._exit(127)
        manifest = _read_manifest_dict(manifest_path)
        manifest.update({
            "updated_at": now(),
            "lifecycle_state": STATE_RUNNING,
            "process": _process_block(payload_pid, pgid, start_identity),
            "artifacts": _artifact_placeholders(),
            "diagnostics": {"privacy": _privacy_note()},
        })
        if os.environ.get("RECOLLECT_DURABLE_INJECT_MANIFEST_FAIL") == "before_running":
            os._exit(125)
        _atomic_write_json(manifest_path, manifest)
        if os.environ.get("RECOLLECT_DURABLE_INJECT_MANIFEST_FAIL") == "after_running":
            os._exit(124)
        # ponytail: RLIMIT_FSIZE is per-process total across fds; stderr gets a separate post-hoc cap.
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (max_stdout, max_stdout))
        except (ValueError, OSError):
            pass
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        os.close(stdout_fd)
        os.close(stderr_fd)
        try:
            os.execvp(command[0], command)
        except OSError:
            os._exit(127)
    os.close(stdout_fd)
    os.close(stderr_fd)
    exit_code = 127
    try:
        _, status = os.waitpid(child_pid, 0)
        exit_code = _status_to_exit(status)
    except ChildProcessError:
        pass
    manifest = _read_manifest_dict(manifest_path)
    if manifest.get("lifecycle_state") == STATE_LAUNCHING:
        # Payload exited before running proof was committed; leave launching for fail-closed inspection.
        return 1
    lifecycle = STATE_EXITED
    if exit_code < 0:
        lifecycle = STATE_CANCELLED
    _finalize_artifacts(manifest, launch_dir, max_stdout, max_stderr)
    manifest.update({
        "updated_at": now(),
        "lifecycle_state": lifecycle,
        "exit_status": {
            "code": exit_code if exit_code >= 0 else None,
            "signal": -exit_code if exit_code < 0 else None,
        },
        "diagnostics": {
            **manifest.get("diagnostics", {}),
            "supervisor": {"cancelled": exit_code < 0},
        },
    })
    _atomic_write_json(manifest_path, manifest)
    return 0 if exit_code == 0 else 1


def _finalize_artifacts(manifest: dict[str, Any], launch_dir: Path, max_stdout: int, max_stderr: int) -> None:
    stdout_meta = _artifact_metadata(launch_dir / STDOUT_NAME, max_stdout)
    stderr_meta = _artifact_metadata(launch_dir / STDERR_NAME, max_stderr)
    manifest["artifacts"] = {
        "stdout": stdout_meta,
        "stderr": stderr_meta,
    }
    result_path = launch_dir / RESULT_NAME
    if result_path.is_file():
        manifest["artifacts"]["result"] = _artifact_metadata(result_path, max_stdout)


def _artifact_metadata(path: Path, limit: int) -> dict[str, Any]:
    if not path.is_file():
        return {
            "name": path.name,
            "bytes": 0,
            "complete": False,
            "truncated": False,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "privacy": "owner_private_not_redacted",
        }
    data = path.read_bytes()
    truncated = len(data) >= limit
    if truncated and len(data) > limit:
        data = data[:limit]
        _atomic_write_bytes(path, data)
    return {
        "name": path.name,
        "bytes": len(data),
        "complete": True,
        "truncated": truncated,
        "sha256": hashlib.sha256(data).hexdigest(),
        "privacy": "owner_private_not_redacted",
    }


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    finally:
        if tmp_path.exists() and not path.exists():
            tmp_path.unlink(missing_ok=True)


def _artifact_placeholders() -> dict[str, Any]:
    empty = hashlib.sha256(b"").hexdigest()
    return {
        "stdout": {"name": STDOUT_NAME, "bytes": 0, "complete": False, "truncated": False, "sha256": empty},
        "stderr": {"name": STDERR_NAME, "bytes": 0, "complete": False, "truncated": False, "sha256": empty},
    }


def _process_block(pid: int, pgid: int, start_identity: str) -> dict[str, Any]:
    platform_note = "Linux /proc starttime+boot_id anti-reuse; other platforms may report limited identity"
    if sys.platform != "linux":
        platform_note = f"{sys.platform}: start_identity best-effort only"
    return {
        "pid": pid,
        "pgid": pgid,
        "start_identity": start_identity,
        "uses_process_group": True,
        "platform_note": platform_note,
    }


def _privacy_note() -> str:
    return (
        "stdout/stderr artifacts are owner-private (mode 0600) but not redacted at this layer; "
        "manifests never include environment, argv, prompts, or API secrets"
    )


def _base_manifest(**fields: Any) -> dict[str, Any]:
    return {
        "schema_version": DURABLE_LAUNCH_SCHEMA_VERSION,
        "process": {},
        "artifacts": _artifact_placeholders(),
        "exit_status": None,
        "diagnostics": {"privacy": _privacy_note()},
        **fields,
    }


def _read_manifest_dict(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _safe_launch_dir(root: Path, launch_id: str) -> Path:
    if not _SAFE_ID_RE.match(launch_id):
        raise ValueError("launch_id contains unsafe characters")
    root = root.resolve()
    launch_dir = (root / launch_id).resolve()
    if not _path_within(launch_dir, root):
        raise ValueError("launch path escapes durable_launches root")
    return launch_dir


_SAFE_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _chmod_private_dir(path: Path) -> None:
    path.chmod(0o700)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        if os.environ.get("RECOLLECT_DURABLE_INJECT_ATOMIC_FAIL") == "before_replace":
            raise OSError("injected atomic write failure")
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        leftover = list(path.parent.glob(f".{path.name}.*.tmp"))
        for item in leftover:
            if item.is_file():
                item.unlink(missing_ok=True)


def _read_process_start_identity(pid: int) -> str | None:
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            rparen = stat.rfind(")")
            rest = stat[rparen + 2 :].split()
            starttime = rest[19]
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"linux:boot={boot_id}:starttime={starttime}"
        except (OSError, IndexError):
            return None
    # ponytail: non-Linux identity is best-effort only; inspector treats mismatch fail-closed.
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return f"{sys.platform}:pid={pid}:monotonic={time.monotonic_ns()}"


def _verify_live_process(record: DurableLaunchRecord) -> bool:
    process = record.process
    pid = process.get("pid")
    pgid = process.get("pgid")
    expected = process.get("start_identity")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    if not isinstance(expected, str) or not expected:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    try:
        if os.getpgid(pid) != pgid:
            return False
    except ProcessLookupError:
        return False
    current = _read_process_start_identity(pid)
    return current is not None and current == expected


def _status_to_exit(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return 127


def _reject_secrets_in_mapping(value: Any, *, _path: str = "") -> None:
    forbidden_keys = ("api_key", "api-key", "token", "secret", "password", "credential", "bearer", "environment", "argv", "command")
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in forbidden_keys):
                raise ValueError(f"manifest contains forbidden key: {key}")
            _reject_secrets_in_mapping(item, _path=f"{_path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets_in_mapping(item, _path=f"{_path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise ValueError("manifest contains forbidden secret-like content")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "__supervise__":
        raise SystemExit(_supervise_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
