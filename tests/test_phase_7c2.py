"""Phase 7C.2: durable subprocess runner and crash-safety proof."""

from __future__ import annotations

import gc
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from recollect_lines.durable_runner import (
    DURABLE_LAUNCH_SCHEMA_VERSION,
    DurableSubprocessRunner,
    LaunchInspectionOutcome,
    STATE_EXITED,
    STATE_LAUNCHING,
    STATE_RUNNING,
    STATE_TIMED_OUT,
    inspect_durable_launch,
    load_launch_record,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHORT = FIXTURES / "durable_short_payload.py"
LONG = FIXTURES / "durable_long_output.py"
HANG = FIXTURES / "durable_hang.py"
SECRET = FIXTURES / "durable_secret_echo.py"


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def kill_pgid_if_alive(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class DurableRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "home"
        self.runner = DurableSubprocessRunner(self.home, max_stdout_bytes=4096, max_stderr_bytes=1024)
        self._handles = []
        self._detached = []

    def tearDown(self):
        for handle in self._handles + self._detached:
            if handle.supervisor is not None and handle.supervisor.poll() is None:
                try:
                    record = load_launch_record(handle.manifest_path)
                    pgid = record.process.get("pgid")
                    if isinstance(pgid, int):
                        kill_pgid_if_alive(pgid)
                    handle.supervisor.wait(timeout=3)
                except (ValueError, subprocess.TimeoutExpired):
                    handle.supervisor.kill()
                    handle.supervisor.wait(timeout=3)
        self.tempdir.cleanup()
        gc.collect()

    def _launch(self, fixture: Path, *, task_id: str = "task-7c2", detach: bool = False, extra_env: dict | None = None):
        command = [sys.executable, str(fixture)]
        if fixture == SECRET:
            command.append("prompt-with-RL_SECRET_SENTINEL-in-argv")
        if extra_env:
            with mock.patch.dict(os.environ, extra_env, clear=False):
                handle = self.runner.launch(task_id=task_id, adapter_id="fixture", command=command, detach_supervisor=detach)
        else:
            handle = self.runner.launch(task_id=task_id, adapter_id="fixture", command=command, detach_supervisor=detach)
        if not detach:
            self._handles.append(handle)
        else:
            self._detached.append(handle)
        return handle

    def test_short_payload_complete_manifest_private_modes_exit_evidence(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            handle = self._launch(SHORT)
            record = self.runner.wait(handle, timeout=10)
        self.assertEqual(record.lifecycle_state, STATE_EXITED)
        self.assertEqual(record.exit_status, {"code": 0, "signal": None})
        self.assertEqual(record.task_id, "task-7c2")
        self.assertTrue(record.process.get("start_identity"))
        self.assertTrue(record.process.get("pid", 0) > 0)
        stdout_path = record.launch_dir / "stdout.log"
        stderr_path = record.launch_dir / "stderr.log"
        manifest_path = record.launch_dir / "manifest.json"
        self.assertEqual(stdout_path.read_text(), "hello-durable\n")
        self.assertEqual(stderr_path.read_text(), "err-durable\n")
        self.assertFalse(record.artifacts["stdout"]["truncated"])
        self.assertEqual(stat.S_IMODE(os.stat(record.launch_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(manifest_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(stdout_path).st_mode), 0o600)

    def test_long_output_truncated_with_metadata(self):
        handle = self._launch(LONG)
        record = self.runner.wait(handle, timeout=10)
        self.assertEqual(record.lifecycle_state, STATE_EXITED)
        self.assertTrue(record.artifacts["stdout"]["truncated"])
        self.assertEqual(record.artifacts["stdout"]["bytes"], 4096)
        self.assertLessEqual((record.launch_dir / "stdout.log").stat().st_size, 4096)

    def test_timeout_reaps_owned_group_terminal_manifest(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            handle = self._launch(HANG)
            wait_until(lambda: load_launch_record(handle.manifest_path).lifecycle_state == STATE_RUNNING, timeout=5)
            record = self.runner.wait(handle, timeout=0.4)
        self.assertEqual(record.lifecycle_state, STATE_TIMED_OUT)
        pgid = record.process.get("pgid")
        if isinstance(pgid, int):
            self.assertFalse(wait_until(lambda: _pgid_alive(pgid), timeout=0.5))

    def test_broker_loss_fixture_continues_inspectable_without_adoption(self):
        handle = self._launch(HANG, detach=True)
        self.assertTrue(
            wait_until(lambda: load_launch_record(handle.manifest_path).lifecycle_state == STATE_RUNNING, timeout=5),
        )
        running = inspect_durable_launch(self.home, task_id="task-7c2", launch_id=handle.launch_id)
        self.assertEqual(running.outcome, LaunchInspectionOutcome.RUNNING_IDENTITY_MATCHES)
        self.assertIsNotNone(running.record)
        pgid = running.record.process["pgid"]
        kill_pgid_if_alive(pgid)
        self.assertTrue(
            wait_until(
                lambda: inspect_durable_launch(self.home, task_id="task-7c2", launch_id=handle.launch_id).outcome
                == LaunchInspectionOutcome.EXITED,
                timeout=5,
            ),
        )

    def test_pid_reuse_identity_mismatch_refuses_and_does_not_signal(self):
        handle = self._launch(HANG)
        self.assertTrue(
            wait_until(lambda: load_launch_record(handle.manifest_path).lifecycle_state == STATE_RUNNING, timeout=5),
        )
        tampered = json.loads(handle.manifest_path.read_text())
        tampered["process"]["start_identity"] = "linux:boot=fake:starttime=0"
        handle.manifest_path.write_text(json.dumps(tampered, indent=2) + "\n")
        inspection = inspect_durable_launch(self.home, task_id="task-7c2", launch_id=handle.launch_id)
        self.assertEqual(inspection.outcome, LaunchInspectionOutcome.IDENTITY_MISMATCH)
        with mock.patch("os.killpg") as killpg:
            self.runner.cancel(handle)
            killpg.assert_not_called()
        kill_pgid_if_alive(tampered["process"]["pgid"])
        if handle.supervisor is not None and handle.supervisor.poll() is None:
            handle.supervisor.wait(timeout=3)

    def test_corrupt_foreign_traversal_paths_rejected(self):
        bad_id = "../escape"
        inspection = inspect_durable_launch(self.home, task_id="task-7c2", launch_id=bad_id)
        self.assertEqual(inspection.outcome, LaunchInspectionOutcome.PATH_REJECTED)
        foreign = self.home / "durable_launches" / ("a" * 31 + "b")
        foreign.mkdir(parents=True)
        (foreign / "manifest.json").write_text("{}")
        inspection2 = inspect_durable_launch(self.home, task_id="task-7c2", launch_id=foreign.name)
        self.assertEqual(inspection2.outcome, LaunchInspectionOutcome.CORRUPT)

    def test_atomic_write_injected_failure_leaves_no_accepted_partial_manifest(self):
        with mock.patch.dict(os.environ, {"RECOLLECT_DURABLE_INJECT_ATOMIC_FAIL": "before_replace"}, clear=False):
            with self.assertRaises(OSError):
                launch_dir = self.home / "durable_launches" / ("c" * 32)
                launch_dir.mkdir(parents=True)
                from recollect_lines.durable_runner import _atomic_write_json, _base_manifest, now

                path = launch_dir / "manifest.json"
                _atomic_write_json(path, _base_manifest(
                    launch_id="c" * 32,
                    task_id="task",
                    adapter_id="fixture",
                    created_at=now(),
                    updated_at=now(),
                    lifecycle_state=STATE_LAUNCHING,
                ))
        temps = list(launch_dir.glob(".manifest.json.*.tmp"))
        self.assertEqual(temps, [])
        with self.assertRaises(ValueError):
            load_launch_record(launch_dir / "manifest.json")

    def test_manifest_never_includes_secret_sentinel(self):
        env = {"RL_SECRET_SENTINEL": "rl_secret_sentinel_value"}
        handle = self._launch(SECRET, extra_env=env)
        record = self.runner.wait(handle, timeout=10)
        blob = json.dumps(record.to_dict())
        self.assertNotIn("rl_secret_sentinel_value", blob)
        self.assertNotIn("RL_SECRET_SENTINEL", blob)
        self.assertIn("rl_secret_sentinel", (record.launch_dir / "stdout.log").read_text())

    def test_launching_state_not_adoptable_yet(self):
        with mock.patch.dict(os.environ, {"RECOLLECT_DURABLE_INJECT_MANIFEST_FAIL": "before_running"}, clear=False):
            handle = self._launch(SHORT)
            handle.supervisor.wait(timeout=5)
        record = load_launch_record(handle.manifest_path)
        self.assertEqual(record.lifecycle_state, STATE_LAUNCHING)
        inspection = inspect_durable_launch(self.home, task_id="task-7c2", launch_id=handle.launch_id)
        self.assertEqual(inspection.outcome, LaunchInspectionOutcome.NOT_ADOPTABLE_YET)

    def test_schema_version_and_adapter_binding(self):
        handle = self._launch(SHORT, task_id="bound-task")
        record = self.runner.wait(handle, timeout=10)
        self.assertEqual(record.schema_version, DURABLE_LAUNCH_SCHEMA_VERSION)
        self.assertEqual(record.adapter_id, "fixture")
        wrong_task = inspect_durable_launch(self.home, task_id="other-task", launch_id=handle.launch_id)
        self.assertEqual(wrong_task.outcome, LaunchInspectionOutcome.PATH_REJECTED)


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
