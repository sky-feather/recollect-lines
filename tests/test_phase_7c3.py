"""Phase 7C.3: safe broker-restart reconciliation for durable subprocess launches."""

from __future__ import annotations

import gc
import json
import os
import signal
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from recollect_lines import mcp_server
from recollect_lines.durable_reconciliation import ReconcileOutcome, is_durable_launch_row, wait_for_durable_running
from recollect_lines.durable_runner import (
    DurableSubprocessRunner,
    STATE_EXITED,
    STATE_RUNNING,
    inspect_durable_launch,
    load_launch_record,
)
from recollect_lines.fixture_durable_adapter import FixtureDurableAdapter
from recollect_lines.models import DEFAULT_PROFILES, ProfilePolicy, RecoveryRequired, TaskRequest, TaskState
from recollect_lines.opencode_adapter import OpenCodeAdapter
from recollect_lines.service import Broker

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_OPENCODE = FIXTURES / "fake_opencode.py"

FIXTURE_DURABLE_PROFILE = ProfilePolicy(
    "fixture_durable",
    frozenset({"read_only"}),
    3600,
    2,
)


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def kill_pgid(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_and_reap_popen(popen, pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    popen.wait(timeout=5)


def durable_broker(home: Path, **kwargs) -> Broker:
    adapter = FixtureDurableAdapter(home, max_stdout_bytes=4096, max_stderr_bytes=1024)
    profiles = {**DEFAULT_PROFILES, FIXTURE_DURABLE_PROFILE.name: FIXTURE_DURABLE_PROFILE}
    return Broker(home, profiles=profiles, fixture_durable_adapter=adapter, **kwargs)


def fake_opencode_broker(home: Path) -> Broker:
    return Broker(
        home,
        opencode_adapter=OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE))),
    )


def db_bytes(home: Path) -> bytes:
    return (home / "recollectlines.db").read_bytes()


class Phase73DurableReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = self.tempdir.name
        self._task_ids: list[str] = []
        self._supervisors: list = []
        self._orphaned_popens: list[tuple[object, int]] = []

    def tearDown(self):
        for broker_attr in ("broker", "broker_a", "broker_b"):
            broker = getattr(self, broker_attr, None)
            if broker is not None:
                for task_id in list(broker._adopted_durable_handles):
                    launch = broker.store.get_launch(task_id)
                    if launch and launch.get("pgid"):
                        kill_pgid(launch["pgid"])
                for task_id in self._task_ids:
                    launch = broker.store.get_launch(task_id)
                    if launch and launch.get("pgid"):
                        kill_pgid(launch["pgid"])
                broker.close()
        for supervisor in self._supervisors:
            if supervisor is not None and supervisor.poll() is None:
                try:
                    supervisor.wait(timeout=2)
                except Exception:
                    supervisor.kill()
                    supervisor.wait(timeout=2)
        self._reap_orphaned_popens()
        self.tempdir.cleanup()
        gc.collect()

    def _start_durable(self, keyword: str = "DURABLE_HANG", *, broker: Broker | None = None) -> tuple[Broker, str]:
        broker = broker or durable_broker(self.home)
        record = broker.create(
            TaskRequest(keyword, self.workspace, execution_mode="read_only", profile="fixture_durable"),
        )
        broker.start(record.id)
        launch = broker.store.get_launch(record.id)
        self.assertTrue(is_durable_launch_row(launch))
        self._task_ids.append(record.id)
        return broker, record.id

    def _reap_orphaned_popens(self) -> None:
        for popen, pgid in self._orphaned_popens:
            kill_and_reap_popen(popen, pgid)
        self._orphaned_popens.clear()

    def _simulate_broker_loss(self, broker: Broker, task_id: str) -> None:
        handle = broker._process_handles.pop(task_id)
        if hasattr(handle, "durable") and handle.durable.supervisor is not None:
            self._supervisors.append(handle.durable.supervisor)
        popen = getattr(handle, "popen", None)
        if popen is not None:
            self._orphaned_popens.append((popen, handle.pgid))

    def test_broker_restart_adopts_exact_running_launch(self):
        broker_a, task_id = self._start_durable("DURABLE_HANG")
        launch = broker_a.store.get_launch(task_id)
        launch_id = launch["durable_launch_id"]
        self._simulate_broker_loss(broker_a, task_id)
        broker_a.close()

        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        record = broker_b.reconcile(task_id)
        self.assertEqual(record.state, TaskState.RUNNING)
        detail = broker_b.reconcile_detail(task_id)
        self.assertEqual(detail["outcome"], ReconcileOutcome.ADOPTED_RUNNING.value)
        self.assertEqual(detail["launch_id"], launch_id)
        self.assertIn(task_id, broker_b._adopted_durable_handles)
        self.assertNotIn("RL_SECRET_SENTINEL", json.dumps(detail))

    def test_adopted_handle_observe_cancel_collect_no_redispatch(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            broker_a, task_id = self._start_durable("DURABLE_SHORT")
            launch_id = broker_a.store.get_launch(task_id)["durable_launch_id"]
            self._simulate_broker_loss(broker_a, task_id)
            self.assertTrue(
                wait_until(
                    lambda: inspect_durable_launch(broker_a.store.home, task_id=task_id, launch_id=launch_id).outcome.value == "exited",
                    timeout=5,
                ),
            )
            broker_a.close()

            broker_b = durable_broker(self.home)
            self.broker_b = broker_b
            broker_b.reconcile(task_id)
            status = broker_b.status(task_id)
            self.assertTrue(status["adopted_durable"]["terminal"])
            self.assertEqual(status["adopted_durable"]["lifecycle_state"], "exited")

            record = broker_b.collect(task_id)
            self.assertIn(record.state, {TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS})
            result_path = broker_b.store.artifacts / task_id / "result.json"
            result = json.loads(result_path.read_text())
            self.assertEqual(result["runtime"]["adapter"], "fixture_durable")
            self.assertNotIn("redispatch", json.dumps(result).lower())
            self.assertEqual(broker_b.store.get_launch(task_id)["durable_launch_id"], launch_id)

    def test_adopted_running_cancel_then_collect(self):
        broker_a, task_id = self._start_durable("DURABLE_HANG")
        self.assertTrue(
            wait_until(
                lambda: load_launch_record(
                    broker_a.store.home / "durable_launches" / broker_a.store.get_launch(task_id)["durable_launch_id"] / "manifest.json",
                ).lifecycle_state == STATE_RUNNING,
                timeout=5,
            ),
        )
        self._simulate_broker_loss(broker_a, task_id)
        broker_a.close()

        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        broker_b.reconcile(task_id)
        cancelled = broker_b.cancel(task_id, "operator cancel after adoption")
        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        self.assertIsNone(broker_b.store.get_recovery_lease(task_id))

    def test_terminal_reconcile_without_false_running_claim(self):
        broker_a, task_id = self._start_durable("DURABLE_SHORT")
        self._simulate_broker_loss(broker_a, task_id)
        time.sleep(0.5)
        broker_a.close()

        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        record = broker_b.reconcile(task_id)
        detail = broker_b.reconcile_detail(task_id)
        self.assertEqual(detail["outcome"], ReconcileOutcome.ADOPTED_TERMINAL_COLLECTABLE.value)
        self.assertTrue(broker_b._adopted_durable_handles[task_id].terminal)
        self.assertEqual(record.state, TaskState.RUNNING)

    def test_identity_mismatch_refused_never_signalled(self):
        broker_a, task_id = self._start_durable("DURABLE_HANG")
        launch_id = broker_a.store.get_launch(task_id)["durable_launch_id"]
        manifest_path = broker_a.store.home / "durable_launches" / launch_id / "manifest.json"
        self.assertTrue(wait_until(lambda: load_launch_record(manifest_path).lifecycle_state == STATE_RUNNING, timeout=5))
        tampered = json.loads(manifest_path.read_text())
        tampered["process"]["start_identity"] = "linux:boot=fake:starttime=0"
        manifest_path.write_text(json.dumps(tampered, indent=2) + "\n")
        self._simulate_broker_loss(broker_a, task_id)
        broker_a.close()

        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        with mock.patch("os.killpg") as killpg:
            record = broker_b.reconcile(task_id)
            killpg.assert_not_called()
        self.assertEqual(record.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(
            broker_b.reconcile_detail(task_id)["outcome"],
            ReconcileOutcome.REFUSED_IDENTITY_MISMATCH.value,
        )
        kill_pgid(tampered["process"]["pgid"])

    def test_wrong_bindings_and_corrupt_paths_refused(self):
        broker_a, task_id = self._start_durable("DURABLE_HANG")
        launch_id = broker_a.store.get_launch(task_id)["durable_launch_id"]
        self._simulate_broker_loss(broker_a, task_id)
        broker_a.close()

        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        bad_task = inspect_durable_launch(broker_b.store.home, task_id="other-task", launch_id=launch_id)
        self.assertEqual(bad_task.outcome.value, "path_rejected")

        with broker_b.store.connection:
            broker_b.store.connection.execute(
                "UPDATE runtime_launches SET adapter = ? WHERE task_id = ?",
                ("wrong_adapter", task_id),
            )
        record = broker_b.reconcile(task_id)
        self.assertEqual(record.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(
            broker_b.reconcile_detail(task_id)["outcome"],
            ReconcileOutcome.REFUSED_BINDING_MISMATCH.value,
        )

        foreign = broker_b.store.home / "durable_launches" / ("d" * 32)
        foreign.mkdir(parents=True)
        (foreign / "manifest.json").write_text("{}")
        inspection = inspect_durable_launch(broker_b.store.home, task_id=task_id, launch_id=foreign.name)
        self.assertEqual(inspection.outcome.value, "corrupt")

    def test_competing_recovery_lease_and_expiry(self):
        broker_a, task_id = self._start_durable("DURABLE_HANG")
        launch_id = broker_a.store.get_launch(task_id)["durable_launch_id"]
        self._simulate_broker_loss(broker_a, task_id)
        self.assertTrue(
            broker_a.store.try_acquire_recovery_lease(
                task_id=task_id,
                durable_launch_id=launch_id,
                broker_id="broker-a",
                broker_epoch=1,
                ttl_seconds=60.0,
            ),
        )

        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        record = broker_b.reconcile(task_id)
        self.assertEqual(record.state, TaskState.RECOVERY_REQUIRED)
        self.assertEqual(
            broker_b.reconcile_detail(task_id)["outcome"],
            ReconcileOutcome.REFUSED_LEASE_CONTENDED.value,
        )

        broker_a.store.release_recovery_lease(task_id)
        broker_a.store.try_acquire_recovery_lease(
            task_id=task_id,
            durable_launch_id=launch_id,
            broker_id="broker-a",
            broker_epoch=1,
            ttl_seconds=0.05,
        )
        time.sleep(0.1)
        record2 = broker_b.reconcile(task_id)
        self.assertEqual(record2.state, TaskState.RUNNING)
        self.assertEqual(
            broker_b.reconcile_detail(task_id)["outcome"],
            ReconcileOutcome.ADOPTED_RUNNING.value,
        )
        broker_a.close()

    def test_legacy_opencode_stays_recovery_required_without_adoption(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            broker_a = fake_opencode_broker(self.home)
            record = broker_a.create(TaskRequest("SLEEP", self.workspace, profile="opencode"))
            broker_a.start(record.id)
            pgid = broker_a.store.get_launch(record.id)["pgid"]
            self.assertTrue(_pgid_alive(pgid))
            self._simulate_broker_loss(broker_a, record.id)
            broker_a.close()

            broker_b = fake_opencode_broker(self.home)
            self.broker_b = broker_b
            reconciled = broker_b.reconcile(record.id)
            self.assertEqual(reconciled.state, TaskState.RECOVERY_REQUIRED)
            self.assertNotIn(record.id, broker_b._adopted_durable_handles)
            with self.assertRaises(RecoveryRequired):
                broker_b.collect(record.id)
            self.assertTrue(
                _pgid_alive(pgid),
                "legacy OpenCode must stay running until explicitly reaped; durable adoption is forbidden",
            )
            self._reap_orphaned_popens()
            self.assertFalse(_pgid_alive(pgid), "legacy OpenCode child must not survive deterministic cleanup")

    def test_no_secret_sentinel_in_db_manifest_or_reconcile_output(self):
        with mock.patch.dict(os.environ, {"RL_SECRET_SENTINEL": "rl_secret_sentinel_value"}, clear=False):
            broker_a, task_id = self._start_durable("DURABLE_SECRET")
            launch_id = broker_a.store.get_launch(task_id)["durable_launch_id"]
            self._simulate_broker_loss(broker_a, task_id)
            self.assertTrue(
                wait_until(
                    lambda: inspect_durable_launch(broker_a.store.home, task_id=task_id, launch_id=launch_id).outcome.value == "exited",
                    timeout=5,
                ),
            )
            broker_a.close()
            broker_b = durable_broker(self.home)
            self.broker_b = broker_b
            broker_b.reconcile(task_id)
            broker_b.collect(task_id)
            blob = db_bytes(self.home)
            manifest_dir = broker_b.store.home / "durable_launches"
            manifests = b"".join(p.read_bytes() for p in manifest_dir.glob("*/manifest.json"))
            result_path = broker_b.store.artifacts / task_id / "result.json"
            result_bytes = result_path.read_bytes() if result_path.is_file() else b""
            combined = blob + manifests + result_bytes + json.dumps(broker_b.reconcile_detail(task_id) or {}).encode()
            self.assertNotIn(b"rl_secret_sentinel_value", combined)
            self.assertNotIn(b"RL_SECRET_SENTINEL", combined)

    def test_timeout_leaves_no_child_leak_or_stale_lease(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            broker = durable_broker(self.home)
            self.broker = broker
            _, task_id = self._start_durable("DURABLE_HANG", broker=broker)
            launch = broker.store.get_launch(task_id)
            pgid = launch["pgid"]
            handle = broker._process_handles.pop(task_id)
            if hasattr(handle, "durable") and handle.durable.supervisor is not None:
                self._supervisors.append(handle.durable.supervisor)
            broker.fixture_durable_adapter.runner.wait(handle.durable, timeout=0.3)
            self.assertFalse(wait_until(lambda: _pgid_alive(pgid), timeout=0.5))
            self.assertIsNone(broker.store.get_recovery_lease(task_id))

    def test_wait_for_durable_running_accepts_fast_exit_launch_proof(self):
        """Regression: micro-duration fixtures can reach exited between RUNNING polls."""
        runner = DurableSubprocessRunner(self.home, max_stdout_bytes=4096, max_stderr_bytes=1024)
        fixture = FIXTURES / "durable_short_payload.py"
        handle = runner.launch(
            task_id="fast-exit-proof",
            adapter_id="fixture_durable",
            command=[sys.executable, str(fixture)],
        )
        self._supervisors.append(handle.supervisor)
        record = runner.wait(handle, timeout=10)
        self.assertEqual(record.lifecycle_state, STATE_EXITED)
        self.assertTrue(record.process.get("pid"))
        ready = wait_for_durable_running(handle.manifest_path, timeout=1.0, supervisor=handle.supervisor)
        self.assertEqual(ready.launch_id, handle.launch_id)
        self.assertIn(ready.lifecycle_state, {STATE_RUNNING, STATE_EXITED})

    def test_mcp_and_cli_reconcile_surface_structured_outcome(self):
        broker_a, task_id = self._start_durable("DURABLE_HANG")
        self._simulate_broker_loss(broker_a, task_id)
        broker_a.close()
        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        mcp_out = mcp_server.handle_reconcile(broker_b, {"task_id": task_id})
        self.assertEqual(mcp_out["reconciled"][0]["reconciliation"]["outcome"], ReconcileOutcome.ADOPTED_RUNNING.value)
        self.assertNotIn("argv", json.dumps(mcp_out).lower())
        self.assertIn("remediation", mcp_out["reconciled"][0]["reconciliation"])
        kill_pgid(broker_b.store.get_launch(task_id)["pgid"])


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
