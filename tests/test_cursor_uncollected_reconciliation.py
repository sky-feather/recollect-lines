"""Cursor-only restart reconciliation: `uncollected` outcome (Wave 3 field fix).

Live field evidence (docs/history/phases/phase-7c5-cursor-uncollected.md):
a supervised Cursor CLI leader can exit while a reparented same-PGID helper
survives for minutes. Process-group liveness alone made a replacement broker
see the group as alive (recovery_required forever) or, once the group finally
died, wrongly label the task `failed` -- a result the broker never actually
observed. This file exercises the leader PID+start-identity proof that
replaces group-liveness as the death signal for Cursor specifically.

Every test that spawns a real OS process group cleans it up (SIGKILL) in a
finally block, whether the assertions above it passed or failed.
"""

import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from recollect_lines import mcp_server
from recollect_lines.adaptor.process import group_alive
from recollect_lines.models import RecoveryRequired, TaskRequest, TaskState
from recollect_lines.service import Broker
from tests.legacy_cursor_fixture import LegacyCursorFixtureAdapter

FAKE_CURSOR = Path(__file__).parent / "fixtures" / "fake_cursor.py"


def fake_cursor_adapter(grace_period_seconds=2.0):
    # This test-only producer creates persisted pre-RFC-004 records. The
    # production CursorAdapter has no direct-Popen/legacy launch option.
    return LegacyCursorFixtureAdapter(
        command_prefix=(sys.executable, str(FAKE_CURSOR)),
        grace_period_seconds=grace_period_seconds,
    )


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def kill_and_reap(popen, pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    popen.wait(timeout=5)


class CursorUncollectedReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _broker(self):
        return Broker(self.home, cursor_adapter=fake_cursor_adapter())

    def _create(self, broker, task="Inspect fact.txt"):
        return broker.create(TaskRequest(task, str(self.workspace), profile="cursor", execution_mode="read_only"))

    # --- 1. leader alive after restart -> recovery_required (unchanged) -----

    def test_leader_alive_after_restart_remains_recovery_required(self):
        broker1 = self._broker()
        record = self._create(broker1, task="SLEEP")
        broker1.start(record.id)
        handle = broker1._process_handles[record.id]
        self.assertTrue(wait_until(lambda: handle.stderr_path.exists() and b"started" in handle.stderr_path.read_bytes()))
        pgid = handle.pgid
        broker1.close()

        broker2 = self._broker()
        try:
            result = broker2.reconcile(record.id)
            self.assertEqual(result.state, TaskState.RECOVERY_REQUIRED)
            last_event = broker2.store.events(record.id)[-1]
            self.assertEqual(last_event["metadata"]["reason"], "cursor_leader_alive_after_restart")
            self.assertEqual(last_event["metadata"]["leader"]["state"], "alive")
            with self.assertRaises(RecoveryRequired):
                broker2.collect(record.id)
        finally:
            kill_and_reap(handle.popen, pgid)
            broker2.close()

    # --- 2. leader proven dead, group alive -> uncollected, no wait --------

    def test_leader_proven_dead_group_alive_reconciles_to_uncollected_without_waiting(self):
        broker1 = self._broker()
        record = self._create(broker1, task="LEADER_EXITS_HELPER_LINGERS")
        broker1.start(record.id)
        handle = broker1._process_handles[record.id]
        self.assertTrue(wait_until(lambda: handle.stderr_path.exists() and b"HELPER_PID" in handle.stderr_path.read_bytes()))
        helper_pid = int(handle.stderr_path.read_text().strip().split()[-1])
        pgid = handle.pgid
        self.assertTrue(wait_until(lambda: handle.popen.poll() is not None), "leader must exit on its own")
        handle.popen.wait(timeout=5)
        self.assertTrue(group_alive(pgid), "the lingering same-PGID helper must keep the group alive")
        broker1.close()

        broker2 = self._broker()
        try:
            started_at = time.monotonic()
            result = broker2.reconcile(record.id)
            elapsed = time.monotonic() - started_at
            self.assertLess(elapsed, 3.0, "reconcile must not wait for the lingering helper to exit")
            self.assertEqual(result.state, TaskState.UNCOLLECTED)
            last_event = broker2.store.events(record.id)[-1]
            metadata = last_event["metadata"]
            self.assertEqual(metadata["reason"], "leader_exited_uncollected")
            self.assertEqual(metadata["outcome"], "unknown")
            self.assertEqual(metadata["leader"]["state"], "dead")
            self.assertEqual(metadata["process_group"]["state"], "alive")
            self.assertTrue(metadata["process_group"]["helpers_may_linger"])
            self.assertFalse((self.home / "artifacts" / record.id / "result.json").exists())
        finally:
            for pid in (helper_pid,):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            broker2.close()

    # --- 3. leader proven dead, group also dead -> uncollected -------------

    def test_leader_proven_dead_group_dead_reconciles_to_uncollected(self):
        broker1 = self._broker()
        record = self._create(broker1)
        broker1.start(record.id)
        handle = broker1._process_handles[record.id]
        handle.popen.wait(timeout=5)
        pgid = handle.pgid
        self.assertFalse(group_alive(pgid))
        broker1.close()

        broker2 = self._broker()
        try:
            result = broker2.reconcile(record.id)
            self.assertEqual(result.state, TaskState.UNCOLLECTED)
            last_event = broker2.store.events(record.id)[-1]
            metadata = last_event["metadata"]
            self.assertEqual(metadata["reason"], "leader_exited_uncollected")
            self.assertEqual(metadata["outcome"], "unknown")
            self.assertEqual(metadata["leader"]["state"], "dead")
            self.assertEqual(metadata["process_group"]["state"], "dead")
            self.assertFalse(metadata["process_group"]["helpers_may_linger"])
        finally:
            broker2.close()

    # --- 4. missing leader identity -> stays recovery_required -------------

    def test_missing_leader_identity_remains_recovery_required_never_infers_death(self):
        broker1 = self._broker()
        record = self._create(broker1)
        broker1.start(record.id)
        handle = broker1._process_handles[record.id]
        handle.popen.wait(timeout=5)  # the leader really is dead...
        with broker1.store.connection:
            broker1.store.connection.execute(
                "UPDATE runtime_launches SET leader_start_identity = NULL WHERE task_id = ?", (record.id,),
            )  # ...but its identity was never captured/persisted.
        broker1.close()

        broker2 = self._broker()
        try:
            result = broker2.reconcile(record.id)
            self.assertEqual(result.state, TaskState.RECOVERY_REQUIRED)
            last_event = broker2.store.events(record.id)[-1]
            metadata = last_event["metadata"]
            self.assertEqual(metadata["reason"], "cursor_leader_identity_unverifiable")
            self.assertEqual(metadata["leader"]["state"], "unknown")
            self.assertFalse(metadata["leader"]["identity_captured_at_launch"])
        finally:
            broker2.close()

    # --- 5. broker-collected Cursor outcomes are unaffected -----------------

    def test_broker_collected_cursor_result_is_unaffected(self):
        broker = self._broker()
        record = self._create(broker)
        broker.start(record.id)
        completed = broker.collect(record.id)
        try:
            self.assertEqual(completed.state, TaskState.SUCCEEDED)
            result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
            self.assertIn("42", result["summary"])
        finally:
            broker.close()

    # --- 6. non-Cursor legacy subprocess reconciliation ---------------------
    #
    # Retired (RFC-004 durable-opencode slice): this test proved that a
    # *non*-Cursor legacy (adapter-owned Popen) launch reconciles via plain
    # process-group liveness, contrasted with Cursor's leader
    # PID+start-identity path above. OpenCode was that non-Cursor example;
    # now that it is durable by default too (matching Cursor/Claude
    # Code/Codex), no production adapter demonstrates the generic
    # non-durable/non-Cursor branch of Broker.reconcile() anymore, and
    # Production adapters deliberately carry no legacy-Popen opt-in, so this
    # negative-space coverage has no real subject left,
    # and was removed rather than faked with a synthetic adapter double.

    # --- 7. completion cursor / concise surfaces expose uncollected --------

    def test_completion_cursor_and_concise_surfaces_expose_uncollected(self):
        broker1 = self._broker()
        record = self._create(broker1)
        broker1.start(record.id)
        broker1._process_handles[record.id].popen.wait(timeout=5)
        broker1.close()

        broker2 = self._broker()
        try:
            broker2.reconcile(record.id)

            page = broker2.completion_events_since(0)
            matching = [event for event in page["events"] if event["task_id"] == record.id and event["state"] == "uncollected"]
            self.assertEqual(len(matching), 1)
            metadata = matching[0]["metadata"]
            self.assertEqual(metadata["reason"], "leader_exited_uncollected")
            self.assertEqual(metadata["outcome"], "unknown")
            self.assertIn("leader", metadata)
            self.assertIn("process_group", metadata)

            status = broker2.status(record.id)
            self.assertEqual(status["state"], "uncollected")

            reconciled = mcp_server.handle_reconcile(broker2, {"task_id": record.id})
            self.assertEqual(reconciled["reconciled"][0]["state"], "uncollected")
        finally:
            broker2.close()

    # --- 8. raw stdout never leaks into a concise surface -------------------

    def test_raw_stdout_never_surfaces_in_uncollected_concise_views(self):
        marker = "STDOUT_ONLY_LEAK_CANARY_7f3c"  # fixed in fake_cursor.py, independent of the prompt text
        broker1 = self._broker()
        record = self._create(broker1, task="STDOUT_CANARY")
        broker1.start(record.id)
        handle = broker1._process_handles[record.id]
        handle.popen.wait(timeout=5)
        self.assertIn(marker, handle.stdout_path.read_text())  # sanity: it really is in the raw artifact
        broker1.close()

        broker2 = self._broker()
        try:
            broker2.reconcile(record.id)
            page = broker2.completion_events_since(0)
            status = broker2.status(record.id)
            serialized = json.dumps(page, default=str) + json.dumps(status, default=str)
            self.assertNotIn(marker, serialized)
            self.assertFalse((self.home / "artifacts" / record.id / "result.json").exists())
        finally:
            broker2.close()


if __name__ == "__main__":
    unittest.main()
