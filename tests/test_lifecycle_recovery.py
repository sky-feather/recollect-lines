"""Phase 5B: durable runtime launch identity, restart reconciliation, idempotent collect().

Every test that spawns a real OS process group cleans it up (SIGKILL + wait)
in a finally/tearDown, whether the assertions above it passed or failed.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from recollect_lines import cli, mcp_server
from recollect_lines.models import RecoveryRequired, TaskRequest, TaskState
from recollect_lines.opencode_adapter import group_alive, redact_command
from recollect_lines.service import Broker

FAKE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


def fake_adapter(grace_period_seconds=2.0):
    from recollect_lines.opencode_adapter import OpenCodeAdapter

    return OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE)), grace_period_seconds=grace_period_seconds)


def wait_until(predicate, timeout=5.0, interval=0.05):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def run_git(args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], cwd=path)
    run_git(["config", "user.email", "test@example.com"], cwd=path)
    run_git(["config", "user.name", "Test"], cwd=path)
    (path / "file.txt").write_text("original\n")
    run_git(["add", "-A"], cwd=path)
    run_git(["commit", "-q", "-m", "initial"], cwd=path)
    return path


def kill_and_reap(popen: subprocess.Popen, pgid: int) -> None:
    """Bounded cleanup for a real process-group fixture: SIGKILL then wait, ignoring an already-dead group."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    popen.wait(timeout=5)


def reap_in_background(popen: subprocess.Popen) -> None:
    """Simulate what a real broker restart gets for free.

    In production the *old* broker OS process is gone by the time a new one
    reconciles, so the kernel reparents an orphaned child to init, which
    reaps it the instant it dies — killpg(pgid, 0) then reliably reports it
    gone. Here "broker1" and "broker2" are just objects in this one test
    process, which is the child's real parent — without an explicit wait()
    it would sit as a zombie (still visible to killpg) until this process
    reaps it. This background wait() stands in for init's automatic reaping.
    """
    threading.Thread(target=popen.wait, daemon=True).start()


class RedactCommandTests(unittest.TestCase):
    def test_redacts_only_the_value_following_a_secret_looking_flag(self):
        command = ["npx", "run", "--api-key", "sk-super-secret", "--dir", "/workspace", "do the thing"]
        redacted = redact_command(command)
        self.assertEqual(redacted[redacted.index("--api-key") + 1], "***REDACTED***")
        self.assertEqual(redacted[redacted.index("--dir") + 1], "/workspace")
        self.assertEqual(redacted[-1], "do the thing")
        self.assertEqual(command[command.index("--api-key") + 1], "sk-super-secret")  # original untouched

    def test_redacts_the_single_token_flag_equals_value_form(self):
        redacted = redact_command(["npx", "run", "--api-key=sk-super-secret", "--dir", "/workspace"])
        self.assertEqual(redacted[2], "--api-key=***REDACTED***")
        self.assertEqual(redacted[3], "--dir")
        self.assertEqual(redacted[4], "/workspace")

    def test_a_value_that_looks_like_a_flag_name_does_not_cascade_into_the_next_argument(self):
        # The redacted value itself, and any plain (non-dash) value that happens to
        # contain a marker word, must never be misread as a flag on the next pass.
        redacted = redact_command(["run", "--token", "abc-secret-token", "--dir", "/workspace"])
        self.assertEqual(redacted, ["run", "--token", "***REDACTED***", "--dir", "/workspace"])


class LifecycleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.source = init_repo(Path(self.tempdir.name) / "source")

    def tearDown(self):
        self.tempdir.cleanup()

    # --- durable launch identity -----------------------------------------

    def test_launch_metadata_survives_a_fresh_broker_and_store_instance(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker.create(TaskRequest("Inspect tests", str(self.source), execution_mode="read_only", profile="opencode"))
        broker.start(record.id)
        handle = broker._process_handles[record.id]

        original = broker.store.get_launch(record.id)
        self.assertEqual(original["adapter"], "opencode")
        self.assertEqual(original["pid"], handle.pid)
        self.assertEqual(original["pgid"], handle.pgid)
        self.assertEqual(original["adapter_label"], str(FAKE_OPENCODE))
        self.assertEqual(original["reconciliation_marker"], "unreconciled")
        self.assertIn("events.jsonl", original["events_artifact"])

        broker.close()
        fresh = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            reloaded = fresh.store.get_launch(record.id)
            self.assertEqual(reloaded["pid"], handle.pid)
            self.assertEqual(reloaded["pgid"], handle.pgid)
            self.assertEqual(reloaded["command"], original["command"])
            self.assertEqual(reloaded["workspace"], str(self.source))
        finally:
            handle.popen.wait(timeout=5)  # this run finishes on its own quickly, no SLEEP keyword
            fresh.close()

    # --- reconciliation: process confirmed dead ---------------------------

    def test_reconcile_after_restart_with_dead_process_reaches_truthful_failure_and_releases_lease(self):
        broker1 = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker1.create(TaskRequest("Inspect tests", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        broker1.start(record.id)
        broker1._process_handles[record.id].popen.wait(timeout=5)  # let the fixture's short-lived process actually exit
        worktree_path = Path(broker1.store.get_lease(record.id)["worktree_path"])
        broker1.close()  # simulate a broker restart: broker2 never had an in-memory handle

        broker2 = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            result = broker2.reconcile(record.id)
            self.assertEqual(result.state, TaskState.FAILED)
            last_event = broker2.store.events(record.id)[-1]
            self.assertEqual(last_event["metadata"]["reason"], "process_group_confirmed_dead")
            self.assertFalse(worktree_path.exists())
            self.assertEqual(broker2.store.get_lease(record.id)["status"], "released")
            self.assertEqual((self.source / "file.txt").read_text(), "original\n")
            self.assertEqual(broker2.store.get_launch(record.id)["reconciliation_marker"], "reconciled")

            # Idempotent: reconciling an already-terminal task is a no-op.
            events_before = len(broker2.store.events(record.id))
            again = broker2.reconcile(record.id)
            self.assertEqual(again.state, TaskState.FAILED)
            self.assertEqual(len(broker2.store.events(record.id)), events_before)
        finally:
            broker2.close()

    # --- reconciliation: process still alive -------------------------------

    def test_reconcile_after_restart_with_live_process_enters_recovery_without_deleting_workspace(self):
        broker1 = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker1.create(TaskRequest("SLEEP", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        broker1.start(record.id)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
        launch = broker1.store.get_launch(record.id)
        pgid = launch["pgid"]
        worktree_path = Path(broker1.store.get_lease(record.id)["worktree_path"])
        broker1.close()  # simulate a broker restart without stopping the real process

        broker2 = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            result = broker2.reconcile(record.id)
            self.assertEqual(result.state, TaskState.RECOVERY_REQUIRED)
            self.assertTrue(worktree_path.exists(), "must not delete a worktree a live process might still use")
            self.assertEqual(broker2.store.get_lease(record.id)["status"], "active")

            with self.assertRaises(RecoveryRequired):
                broker2.collect(record.id)

            # Idempotent re-check: still alive, no further state transition, but a
            # fresh audit event is appended each time (never silent).
            events_before = len(broker2.store.events(record.id))
            again = broker2.reconcile(record.id)
            self.assertEqual(again.state, TaskState.RECOVERY_REQUIRED)
            self.assertGreater(len(broker2.store.events(record.id)), events_before)
            self.assertEqual(broker2.store.events(record.id)[-1]["type"], "task.reconciliation_checked")
            self.assertTrue(worktree_path.exists())
            self.assertEqual(broker2.store.get_lease(record.id)["status"], "active")

            # verify() must also refuse: the worktree might still be mutated by the live process.
            with self.assertRaises(ValueError):
                broker2.verify(record.id, [[sys.executable, "-c", "print('should not run')"]])
        finally:
            kill_and_reap(broker1._process_handles[record.id].popen, pgid)
            broker2.close()

    # --- crash window: launch recorded but the task never reached RUNNING ----

    def _start_and_record_launch_without_reaching_running(self, broker, task="Inspect tests", execution_mode="read_only"):
        """Replicate exactly what Broker.start() does up to record_launch(), then stop.

        This is what a broker crash immediately after record_launch() (but
        before the final RUNNING transition, or before registering the
        in-memory handle) leaves behind: a durable launch row for a task
        still sitting at PREPARING.
        """
        record = broker.create(TaskRequest(task, str(self.source), execution_mode=execution_mode, profile="opencode"))
        record = broker.store.transition(record.id, TaskState.PREPARING, "Preparing execution", {})
        metadata, handle = broker.opencode_adapter.start(record, broker.store.artifacts / record.id, workspace=record.workspace)
        broker.store.record_launch(
            record.id,
            adapter=broker.opencode_adapter.name,
            adapter_label=broker.opencode_adapter.runtime_label,
            pid=handle.pid,
            pgid=handle.pgid,
            command=redact_command(metadata["command"]),
            workspace=metadata["workspace"],
            events_artifact=metadata["events_artifact"],
            stderr_artifact=metadata["stderr_artifact"],
        )
        self.assertEqual(broker.store.get(record.id).state, TaskState.PREPARING)
        return record, handle

    def test_reconcile_resolves_a_dead_process_whose_task_never_left_preparing(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        record, handle = self._start_and_record_launch_without_reaching_running(broker)
        handle.popen.wait(timeout=5)  # the default fixture finishes quickly on its own

        result = broker.reconcile(record.id)

        self.assertEqual(result.state, TaskState.FAILED)
        self.assertEqual(broker.store.events(record.id)[-1]["metadata"]["reason"], "process_group_confirmed_dead")
        broker.close()

    def test_reconcile_and_cancel_handle_a_live_process_whose_task_never_left_preparing(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        record, handle = self._start_and_record_launch_without_reaching_running(broker, task="SLEEP")
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))

        try:
            result = broker.reconcile(record.id)
            self.assertEqual(result.state, TaskState.RECOVERY_REQUIRED)

            # This broker/test process is the real parent of `handle.popen` (it was
            # never registered in _process_handles, so cancel() below takes the
            # same no-Popen, pgid-only path a genuine restart would) — reap it in
            # the background the way init would for a truly orphaned process, or
            # the liveness poll below never observes it as dead.
            reap_in_background(handle.popen)

            # cancel() must not raise InvalidTransition trying to go preparing -> cancelling,
            # and must actually terminate the real process via its persisted pgid.
            cancelled = broker.cancel(record.id, "operator cleanup")
            self.assertEqual(cancelled.state, TaskState.CANCELLED)
            self.assertFalse(group_alive(handle.pgid))
        finally:
            kill_and_reap(handle.popen, handle.pgid)

        broker.close()

    def test_reconcile_pending_also_resolves_a_task_stuck_in_cancelling_after_a_crash(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker.create(TaskRequest("Inspect tests", str(self.source), execution_mode="read_only", profile="opencode"))
        broker.start(record.id)
        handle = broker._process_handles.pop(record.id)  # simulate the handle being lost
        handle.popen.wait(timeout=5)
        # Simulate a crash mid-cancellation: CANCELLING was durably recorded, but the
        # broker disappeared before _cancel_by_pgid's outcome was ever persisted.
        broker.store.transition(record.id, TaskState.CANCELLING, "Cancellation requested", {"reason": "operator request"})

        reconciled = {r.id: r for r in broker.reconcile_pending()}

        self.assertEqual(reconciled[record.id].state, TaskState.CANCELLED)
        self.assertEqual(broker.store.events(record.id)[-1]["metadata"]["reason"], "process_group_confirmed_dead")
        broker.close()

    # --- conservative handling of missing/invalid metadata ------------------

    def test_missing_or_invalid_pgid_metadata_is_handled_conservatively(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker.create(TaskRequest("SLEEP", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        broker.start(record.id)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
        handle = broker._process_handles.pop(record.id)  # simulate a broker restart losing the handle
        worktree_path = Path(broker.store.get_lease(record.id)["worktree_path"])

        with broker.store.connection:
            broker.store.connection.execute("UPDATE runtime_launches SET pgid = NULL WHERE task_id = ?", (record.id,))

        try:
            result = broker.reconcile(record.id)
            self.assertEqual(result.state, TaskState.RECOVERY_REQUIRED)
            last_event = broker.store.events(record.id)[-1]
            self.assertEqual(last_event["metadata"]["reason"], "runtime_metadata_missing_or_invalid")
            self.assertTrue(worktree_path.exists(), "invalid metadata must never be treated as proof the process is dead")
            self.assertEqual(broker.store.get_lease(record.id)["status"], "active")

            with self.assertRaises(RecoveryRequired):
                broker.collect(record.id)

            # cancel() must equally refuse to signal an unverified pgid.
            cancelled = broker.cancel(record.id, "operator requested")
            self.assertEqual(cancelled.state, TaskState.RECOVERY_REQUIRED)
            self.assertTrue(worktree_path.exists())
        finally:
            kill_and_reap(handle.popen, handle.pgid)
            broker._finalize_workspace(record.id)  # clean teardown now that the real process is gone
            broker.close()

    # --- idempotent collect() ------------------------------------------------

    def test_terminal_collect_called_twice_returns_the_same_result_with_no_duplicate_events(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker.create(TaskRequest("Inspect tests", str(self.source), execution_mode="read_only", profile="opencode"))
        broker.start(record.id)

        first = broker.collect(record.id)
        self.assertEqual(first.state, TaskState.SUCCEEDED)
        events_after_first = broker.store.events(record.id)
        result_after_first = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())

        second = broker.collect(record.id)
        self.assertEqual(second.state, TaskState.SUCCEEDED)
        self.assertEqual(second, first)
        self.assertEqual(broker.store.events(record.id), events_after_first)
        result_after_second = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result_after_second, result_after_first)
        broker.close()

    def test_mcp_collect_does_not_rerun_verification_on_a_repeated_call(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker.create(TaskRequest("Inspect tests", str(self.source), execution_mode="read_only", profile="opencode"))
        broker.store.write_artifact(record.id, "verify_commands.json", json.dumps([[sys.executable, "-c", "print('ok')"]]) + "\n")
        broker.start(record.id)

        calls = {"n": 0}
        original_verify = broker.verify

        def counting_verify(*args, **kwargs):
            calls["n"] += 1
            return original_verify(*args, **kwargs)

        broker.verify = counting_verify

        first = mcp_server.handle_collect(broker, {"task_id": record.id})
        self.assertEqual(first["state"], "succeeded")
        self.assertEqual(calls["n"], 1)
        self.assertTrue(first["broker_verification"]["commands"][0]["passed"])

        second = mcp_server.handle_collect(broker, {"task_id": record.id})
        self.assertEqual(second["state"], "succeeded")
        self.assertEqual(calls["n"], 1, "verification must not be re-run for an already-terminal task")
        self.assertEqual(second["broker_verification"], first["broker_verification"])
        self.assertEqual(second["runtime_result"], first["runtime_result"])
        broker.close()

    # --- cancellation via persisted pgid after restart -----------------------

    def test_cancel_via_persisted_pgid_after_restart_terminates_process_and_reaches_cancelled(self):
        broker1 = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker1.create(TaskRequest("SLEEP", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        broker1.start(record.id)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
        pgid = broker1.store.get_launch(record.id)["pgid"]
        worktree_path = Path(broker1.store.get_lease(record.id)["worktree_path"])
        handle = broker1._process_handles[record.id]
        broker1.close()  # simulate a broker restart
        reap_in_background(handle.popen)

        broker2 = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            cancelled = broker2.cancel(record.id, "operator cancel after restart")
            self.assertEqual(cancelled.state, TaskState.CANCELLED)
            last_event = broker2.store.events(record.id)[-1]
            self.assertTrue(last_event["metadata"]["cancellation"]["group_terminated"])
            self.assertIn("SIGTERM", last_event["metadata"]["cancellation"]["signals_sent"])
            self.assertFalse(group_alive(pgid))
            self.assertFalse(worktree_path.exists())
            self.assertEqual(broker2.store.get_lease(record.id)["status"], "released")
            self.assertEqual((self.source / "file.txt").read_text(), "original\n")
        finally:
            handle.popen.wait(timeout=5)
            broker2.close()

    def test_cancel_via_persisted_pgid_escalates_to_sigkill_when_process_ignores_sigterm(self):
        broker1 = Broker(self.home, opencode_adapter=fake_adapter(grace_period_seconds=0.3))
        record = broker1.create(TaskRequest("SLEEP_IGNORE_TERM", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        broker1.start(record.id)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
        pgid = broker1.store.get_launch(record.id)["pgid"]
        handle = broker1._process_handles[record.id]
        broker1.close()
        reap_in_background(handle.popen)

        broker2 = Broker(self.home, opencode_adapter=fake_adapter(grace_period_seconds=0.3))
        try:
            cancelled = broker2.cancel(record.id, "force stop after restart")
            self.assertEqual(cancelled.state, TaskState.CANCELLED)
            signals_sent = broker2.store.events(record.id)[-1]["metadata"]["cancellation"]["signals_sent"]
            self.assertEqual(signals_sent, ["SIGTERM", "SIGKILL"])
        finally:
            handle.popen.wait(timeout=5)
            broker2.close()

    # --- bulk reconciliation on startup --------------------------------------

    def test_reconcile_pending_only_touches_opencode_tasks_that_need_it(self):
        broker1 = Broker(self.home, opencode_adapter=fake_adapter())
        mock_record = broker1.create(TaskRequest("mock work", str(self.source), execution_mode="read_only", profile="mock"))
        broker1.start(mock_record.id)  # legitimately RUNNING forever until complete() is called; not restart-affected

        dead_record = broker1.create(TaskRequest("Inspect tests", str(self.source), execution_mode="read_only", profile="opencode"))
        broker1.start(dead_record.id)
        broker1._process_handles[dead_record.id].popen.wait(timeout=5)

        sleep_record = broker1.create(TaskRequest("SLEEP", str(self.source), execution_mode="read_only", profile="opencode"))
        broker1.start(sleep_record.id)
        events_path = self.home / "artifacts" / sleep_record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
        pgid = broker1.store.get_launch(sleep_record.id)["pgid"]
        broker1.close()

        broker2 = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            reconciled = {r.id: r for r in broker2.reconcile_pending()}
            self.assertNotIn(mock_record.id, reconciled)
            self.assertEqual(broker2.store.get(mock_record.id).state, TaskState.RUNNING)
            self.assertEqual(reconciled[dead_record.id].state, TaskState.FAILED)
            self.assertEqual(reconciled[sleep_record.id].state, TaskState.RECOVERY_REQUIRED)
        finally:
            kill_and_reap(broker1._process_handles[sleep_record.id].popen, pgid)
            broker2.close()

    # --- CLI surface smoke ----------------------------------------------------

    def test_cli_reconcile_and_reconcile_all_smoke(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker.create(TaskRequest("Inspect tests", str(self.source), execution_mode="read_only", profile="opencode"))
        broker.start(record.id)
        broker._process_handles[record.id].popen.wait(timeout=5)
        broker.close()

        exit_code = cli.main(["--home", str(self.home), "reconcile", record.id])
        self.assertEqual(exit_code, 0)

        second = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            self.assertEqual(second.store.get(record.id).state, TaskState.FAILED)
        finally:
            second.close()

        exit_code = cli.main(["--home", str(self.home), "reconcile-all"])
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
