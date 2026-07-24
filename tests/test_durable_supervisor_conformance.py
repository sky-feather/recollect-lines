"""RFC-004 P1.3: shared durable-supervisor conformance for production CLI adapters.

Runs the same lifecycle evidence matrix across cursor, claude_code, codex, and
opencode using local offline fixtures — never adapter-owned Popen, never
duplicating provider-specific argv/schema tests (those stay in per-adapter files).
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from recollect_lines.durable_runner import load_launch_record
from recollect_lines.models import TaskState
from tests.durable_supervisor_conformance_support import (
    PRODUCTION_ADAPTER_CASES,
    ConformanceHarness,
    ProductionAdapterCase,
    assert_durable_seam,
    launch_dir,
    launch_row,
    make_broker,
    manifest_path,
    stdout_artifact_path,
    wait_for_durable_launch_terminal,
    wait_for_running_probe,
    wait_for_terminal_manifest,
    wait_until,
)
from tests.liveness_contract import LinuxZombieIdentityContractTests, NonLinuxStartIdentityFallbackTests


class DurableSupervisorConformanceTests(unittest.TestCase):
    """Evidence matrix items 1–4, parameterized across production adapters."""

    def _harness(self, case: ProductionAdapterCase, *, use_git_repo: bool = False) -> ConformanceHarness:
        return ConformanceHarness(case, use_git_repo=use_git_repo)

    def test_start_uses_shared_durable_subprocess_seam(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case)
                try:
                    record = harness.broker.create(harness.create(case.success_task()))
                    started = harness.broker.start(record.id)
                    self.assertEqual(started.state, TaskState.RUNNING)
                    handle = harness.broker._process_handles[record.id]
                    assert_durable_seam(self, harness.broker, record.id, case, handle)
                    harness.track_pgid(launch_row(harness.broker, record.id).get("pgid"))
                    if not wait_for_durable_launch_terminal(handle, timeout=5):
                        self.fail(f"{case.adapter_id}: fixture did not reach terminal manifest in time")
                    harness.broker.collect(record.id)
                finally:
                    harness.close_all()

    def test_normal_exit_terminal_state_and_fresh_durable_artifacts(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case)
                try:
                    record = harness.broker.create(harness.create(case.success_task()))
                    harness.broker.start(record.id)
                    handle = harness.broker._process_handles[record.id]
                    self.assertTrue(wait_for_durable_launch_terminal(handle, timeout=5))
                    completed = harness.broker.collect(record.id)
                    self.assertEqual(completed.state, TaskState.SUCCEEDED)
                    launch = launch_row(harness.broker, record.id)
                    stdout_path = stdout_artifact_path(harness.broker, record.id, case)
                    self.assertTrue(stdout_path.is_file(), f"missing {case.stdout_artifact_name}")
                    self.assertGreater(stdout_path.stat().st_size, 0)
                    manifest = load_launch_record(manifest_path(harness.broker, record.id))
                    stdout_meta = manifest.artifacts["stdout"]
                    self.assertEqual(stdout_meta["name"], case.stdout_artifact_name)
                    self.assertGreater(stdout_meta["bytes"], 0)
                    self.assertTrue(stdout_meta["complete"])
                    self.assertEqual(launch["events_artifact"], case.stdout_artifact_name)
                    status = harness.broker.status(record.id)
                    self.assertEqual(status["state"], "succeeded")
                finally:
                    harness.close_all()

    def test_nonzero_exit_maps_to_failed_terminal_broker_state(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case)
                try:
                    record = harness.broker.create(harness.create("NONZERO_EXIT"))
                    harness.broker.start(record.id)
                    handle = harness.broker._process_handles[record.id]
                    self.assertTrue(wait_for_durable_launch_terminal(handle, timeout=5))
                    completed = harness.broker.collect(record.id)
                    self.assertEqual(completed.state, TaskState.FAILED)
                    result = json.loads((harness.home / "artifacts" / record.id / "result.json").read_text())
                    self.assertEqual(result["runtime"]["exit_code"], 1)
                    self.assertEqual(result["runtime"]["adapter"], case.adapter_id)
                finally:
                    harness.close_all()

    def test_malformed_output_preserves_documented_raw_stream(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case)
                try:
                    record = harness.broker.create(harness.create("MALFORMED"))
                    harness.broker.start(record.id)
                    handle = harness.broker._process_handles[record.id]
                    self.assertTrue(wait_for_durable_launch_terminal(handle, timeout=5))
                    completed = harness.broker.collect(record.id)
                    self.assertIn(
                        completed.state,
                        {TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS},
                    )
                    raw_path = stdout_artifact_path(harness.broker, record.id, case)
                    raw_bytes = raw_path.read_bytes()
                    self.assertGreater(len(raw_bytes), 0)
                    self.assertIn(b"not valid json", raw_bytes)
                    result = json.loads((harness.home / "artifacts" / record.id / "result.json").read_text())
                    self.assertIn("partial result", result["summary"])
                finally:
                    harness.close_all()

    def test_status_only_observation_reaches_terminal_without_explicit_collect(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case)
                try:
                    record = harness.broker.create(harness.create(case.success_task()))
                    harness.broker.start(record.id)
                    handle = harness.broker._process_handles[record.id]
                    self.assertTrue(wait_for_durable_launch_terminal(handle, timeout=5))

                    def status_reached_terminal() -> bool:
                        payload = harness.broker.status(record.id)
                        return payload["state"] in {"succeeded", "failed", "succeeded_with_warnings", "cancelled", "timed_out"}

                    self.assertTrue(
                        wait_until(status_reached_terminal, timeout=5),
                        f"{case.adapter_id}: status() never observed terminal/reaped state",
                    )
                    self.assertNotIn(record.id, harness.broker._process_handles)
                finally:
                    harness.close_all()

    def test_broker_restart_adopts_terminal_task_and_collects(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case)
                try:
                    record = harness.broker.create(harness.create(case.success_task()))
                    harness.broker.start(record.id)
                    handle = harness.broker._process_handles[record.id]
                    launch_id = launch_row(harness.broker, record.id)["durable_launch_id"]
                    self.assertTrue(wait_for_durable_launch_terminal(handle, timeout=5))
                    harness.broker._process_handles.pop(record.id)
                    harness.broker.close()
                    harness._brokers.remove(harness.broker)

                    broker2 = harness.track_broker(make_broker(harness.home, case))
                    broker2.reconcile(record.id)
                    detail = broker2.reconcile_detail(record.id)
                    self.assertEqual(detail["outcome"], "adopted_terminal_collectable")
                    self.assertEqual(detail["launch_id"], launch_id)
                    completed = broker2.collect(record.id)
                    self.assertEqual(completed.state, TaskState.SUCCEEDED)
                    self.assertNotEqual(broker2.store.get(record.id).state, TaskState.UNCOLLECTED)
                finally:
                    harness.close_all()

    def test_broker_restart_adopts_running_task_without_destructive_cleanup(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case, use_git_repo=True)
                try:
                    record = harness.broker.create(
                        harness.create("SLEEP", execution_mode="isolated_worktree"),
                    )
                    harness.broker.start(record.id)
                    handle = harness.broker._process_handles.pop(record.id)
                    launch_id = launch_row(harness.broker, record.id)["durable_launch_id"]
                    wait_for_running_probe(harness.broker, record.id, case)
                    worktree_path = harness.broker.store.get_lease(record.id)["worktree_path"]
                    harness.broker.close()
                    harness._brokers.remove(harness.broker)

                    broker2 = harness.track_broker(make_broker(harness.home, case))
                    reconciled = broker2.reconcile(record.id)
                    self.assertEqual(reconciled.state, TaskState.RUNNING)
                    detail = broker2.reconcile_detail(record.id)
                    self.assertEqual(detail["outcome"], "adopted_running")
                    self.assertEqual(detail["launch_id"], launch_id)
                    self.assertTrue(os.path.isdir(worktree_path), "adoption must not release the worktree")

                    cancelled = broker2.cancel(record.id, "conformance cleanup")
                    self.assertEqual(cancelled.state, TaskState.CANCELLED)
                    harness.track_pgid(handle.pgid)
                finally:
                    harness.close_all()

    def test_cancel_uses_process_group_control_and_terminal_manifest(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case)
                try:
                    record = harness.broker.create(harness.create("SLEEP"))
                    harness.broker.start(record.id)
                    handle = harness.broker._process_handles[record.id]
                    wait_for_running_probe(harness.broker, record.id, case)
                    pgid = handle.pgid

                    cancelled = harness.broker.cancel(record.id, "conformance cancel")

                    self.assertEqual(cancelled.state, TaskState.CANCELLED)
                    cancel_event = harness.broker.store.events(record.id)[-1]
                    self.assertTrue(cancel_event["metadata"]["cancellation"]["group_terminated"])
                    self.assertIn("SIGTERM", cancel_event["metadata"]["cancellation"]["signals_sent"])
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(pgid, 0)
                    manifest = manifest_path(harness.broker, record.id)
                    self.assertTrue(
                        wait_for_terminal_manifest(manifest, timeout=5),
                        f"{case.adapter_id}: durable manifest not terminal after cancel",
                    )
                finally:
                    harness.close_all()

    def test_timeout_terminates_sleeping_fixture_and_reaches_terminal_manifest(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case)
                try:
                    record = harness.broker.create(harness.create("SLEEP"))
                    harness.broker.start(record.id)
                    handle = harness.broker._process_handles[record.id]
                    wait_for_running_probe(harness.broker, record.id, case)
                    pgid = handle.pgid

                    timed_out = harness.broker.timeout(record.id, "conformance timeout")

                    self.assertEqual(timed_out.state, TaskState.TIMED_OUT)
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(pgid, 0)
                    manifest = manifest_path(harness.broker, record.id)
                    self.assertTrue(
                        wait_for_terminal_manifest(manifest, timeout=5),
                        f"{case.adapter_id}: durable manifest not terminal after timeout",
                    )
                finally:
                    harness.close_all()

    def test_tampered_identity_enters_recovery_required_without_workspace_release(self):
        for case in PRODUCTION_ADAPTER_CASES:
            with self.subTest(adapter=case.adapter_id):
                harness = self._harness(case, use_git_repo=True)
                try:
                    record = harness.broker.create(
                        harness.create("SLEEP", execution_mode="isolated_worktree"),
                    )
                    harness.broker.start(record.id)
                    handle = harness.broker._process_handles.pop(record.id)
                    manifest = manifest_path(harness.broker, record.id)
                    wait_for_running_probe(harness.broker, record.id, case)
                    tampered = json.loads(manifest.read_text())
                    tampered["process"]["start_identity"] = "linux:boot=fake:starttime=0"
                    manifest.write_text(json.dumps(tampered, indent=2) + "\n")
                    worktree_path = harness.broker.store.get_lease(record.id)["worktree_path"]
                    harness.broker.close()
                    harness._brokers.remove(harness.broker)

                    broker2 = harness.track_broker(make_broker(harness.home, case))
                    with mock.patch("os.killpg") as killpg:
                        result = broker2.reconcile(record.id)
                        killpg.assert_not_called()
                    self.assertEqual(result.state, TaskState.RECOVERY_REQUIRED)
                    self.assertEqual(broker2.reconcile_detail(record.id)["outcome"], "refused_identity_mismatch")
                    self.assertTrue(os.path.isdir(worktree_path))
                    harness.track_pgid(handle.pgid)
                finally:
                    harness.close_all()


# Re-export shared platform contract tests so `pytest tests/test_durable_supervisor_conformance.py`
# and full-suite runs pick them up from one conformance entrypoint.
__all__ = [
    "DurableSupervisorConformanceTests",
    "NonLinuxStartIdentityFallbackTests",
    "LinuxZombieIdentityContractTests",
]


if __name__ == "__main__":
    unittest.main()
