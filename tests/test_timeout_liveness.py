"""Phase 5C: Broker.timeout() must classify process-group liveness before
finalizing a workspace, closing the gap named in docs/history/phases/phase-5b.md ("Non-goals
carried forward") where a timeout clock alone used to finalize (and delete)
a workspace an actually-still-running process might still be writing to.

Every test that spawns a real OS process group cleans it up (SIGKILL + wait)
in a finally block, whether the assertions above it passed or failed.
"""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.opencode_adapter import group_alive
from recollect_lines.service import Broker

FAKE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


def fake_adapter(grace_period_seconds=2.0):
    from recollect_lines.opencode_adapter import OpenCodeAdapter

    return OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE)), grace_period_seconds=grace_period_seconds)


def wait_until(predicate, timeout=5.0, interval=0.05):
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
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    popen.wait(timeout=5)


def reap_in_background(popen: subprocess.Popen) -> None:
    """Stand in for init's automatic reaping of an orphaned process in a real
    restart — this test process is `popen`'s real parent, so without this its
    exit would sit as a zombie (still visible to killpg) until reaped.
    """
    threading.Thread(target=popen.wait, daemon=True).start()


class TimeoutLivenessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.source = init_repo(Path(self.tempdir.name) / "source")

    def tearDown(self):
        self.tempdir.cleanup()

    # --- mock profile: unaffected regression -------------------------------

    def test_mock_task_timeout_is_unchanged_and_finalizes_immediately(self):
        broker = Broker(self.home)
        try:
            record = broker.create(TaskRequest("mock timeout", str(self.source), execution_mode="isolated_worktree"))
            broker.start(record.id)
            lease = broker.store.get_lease(record.id)
            worktree_path = Path(lease["worktree_path"])

            timed_out = broker.timeout(record.id)
            self.assertEqual(timed_out.state, TaskState.TIMED_OUT)
            self.assertFalse(worktree_path.exists())
            self.assertEqual(broker.store.get_lease(record.id)["status"], "released")
            self.assertEqual((self.source / "file.txt").read_text(), "original\n")
        finally:
            broker.close()

    # --- idempotency ---------------------------------------------------------

    def test_timeout_is_idempotent_on_an_already_terminal_task(self):
        broker = Broker(self.home)
        try:
            record = broker.create(TaskRequest("mock timeout twice", str(self.source), execution_mode="read_only"))
            broker.start(record.id)
            first = broker.timeout(record.id)
            events_before = len(broker.store.events(record.id))
            second = broker.timeout(record.id)
            self.assertEqual(second, first)
            self.assertEqual(len(broker.store.events(record.id)), events_before)
        finally:
            broker.close()

    # --- opencode profile: in-memory handle still present -------------------

    def test_timeout_with_a_live_in_memory_process_group_terminates_it_before_finalizing(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            record = broker.create(TaskRequest("SLEEP", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
            broker.start(record.id)
            handle = broker._process_handles[record.id]
            pgid = handle.pgid
            events_path = self.home / "artifacts" / record.id / "events.jsonl"
            self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
            worktree_path = Path(broker.store.get_lease(record.id)["worktree_path"])

            timed_out = broker.timeout(record.id, "exceeded bound")
            self.assertEqual(timed_out.state, TaskState.TIMED_OUT)
            self.assertFalse(group_alive(pgid), "timeout() must actually terminate a live in-memory process group, not just declare a timeout")
            self.assertFalse(worktree_path.exists())
            self.assertEqual(broker.store.get_lease(record.id)["status"], "released")
            self.assertEqual((self.source / "file.txt").read_text(), "original\n")
            self.assertNotIn(record.id, broker._process_handles)
        finally:
            broker.close()

    def test_timeout_escalates_to_sigkill_when_process_ignores_sigterm(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter(grace_period_seconds=0.3))
        try:
            record = broker.create(TaskRequest("SLEEP_IGNORE_TERM", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
            broker.start(record.id)
            pgid = broker._process_handles[record.id].pgid
            events_path = self.home / "artifacts" / record.id / "events.jsonl"
            self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))

            timed_out = broker.timeout(record.id)
            self.assertEqual(timed_out.state, TaskState.TIMED_OUT)
            signals_sent = broker.store.events(record.id)[-1]["metadata"]["cancellation"]["signals_sent"]
            self.assertEqual(signals_sent, ["SIGTERM", "SIGKILL"])
            self.assertFalse(group_alive(pgid))
        finally:
            broker.close()

    # --- opencode profile: no in-memory handle (post-restart) --------------

    def test_timeout_after_restart_with_dead_group_finalizes_and_reaches_timed_out(self):
        broker1 = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker1.create(TaskRequest("Inspect tests", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        broker1.start(record.id)
        broker1._process_handles[record.id].popen.wait(timeout=5)
        worktree_path = Path(broker1.store.get_lease(record.id)["worktree_path"])
        broker1.close()

        broker2 = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            timed_out = broker2.timeout(record.id, "exceeded bound after restart")
            self.assertEqual(timed_out.state, TaskState.TIMED_OUT)
            last_event = broker2.store.events(record.id)[-1]
            self.assertEqual(last_event["metadata"]["liveness"], "process_group_confirmed_dead")
            self.assertFalse(worktree_path.exists())
            self.assertEqual(broker2.store.get_lease(record.id)["status"], "released")
            self.assertEqual((self.source / "file.txt").read_text(), "original\n")
        finally:
            broker2.close()

    def test_timeout_after_restart_with_live_group_enters_recovery_required_and_retains_workspace(self):
        broker1 = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker1.create(TaskRequest("SLEEP", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        broker1.start(record.id)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
        launch = broker1.store.get_launch(record.id)
        pgid = launch["pgid"]
        worktree_path = Path(broker1.store.get_lease(record.id)["worktree_path"])
        handle = broker1._process_handles[record.id]
        broker1.close()  # simulate a broker restart without stopping the real process
        reap_in_background(handle.popen)

        broker2 = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            # This is the exact gap Phase 5C closes: naive Phase 5B timeout()
            # would have finalized (and deleted) this worktree unconditionally.
            timed_out = broker2.timeout(record.id, "exceeded bound after restart")
            self.assertEqual(timed_out.state, TaskState.TIMED_OUT, "a still-alive group must still be safely terminated, not just declared timed out")
            self.assertFalse(group_alive(pgid))
            self.assertFalse(worktree_path.exists())
        finally:
            kill_and_reap(handle.popen, pgid)
            broker2.close()

    def test_timeout_after_restart_with_unconfirmed_liveness_never_finalizes(self):
        broker1 = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker1.create(TaskRequest("SLEEP_IGNORE_TERM", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        broker1.start(record.id)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))

        # Simulate a broker restart that also lost/corrupted the persisted pgid
        # (e.g. partial write) — must never be treated as proof of death.
        handle = broker1._process_handles[record.id]
        with broker1.store.connection:
            broker1.store.connection.execute("UPDATE runtime_launches SET pgid = NULL WHERE task_id = ?", (record.id,))
        worktree_path = Path(broker1.store.get_lease(record.id)["worktree_path"])
        broker1.close()

        broker2 = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            result = broker2.timeout(record.id, "exceeded bound")
            self.assertEqual(result.state, TaskState.RECOVERY_REQUIRED)
            last_event = broker2.store.events(record.id)[-1]
            self.assertEqual(last_event["metadata"]["liveness"], "runtime_metadata_missing_or_invalid")
            self.assertTrue(worktree_path.exists(), "invalid metadata must never be treated as proof the process is dead")
            self.assertEqual(broker2.store.get_lease(record.id)["status"], "active")
        finally:
            kill_and_reap(handle.popen, handle.pgid)
            broker2._finalize_workspace(record.id)
            broker2.close()

    def test_timeout_no_leaked_process_groups(self):
        """Bounded fixture proof: after timeout(), the process group this test
        spawned is confirmed dead, whatever path timeout() took.
        """
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            record = broker.create(TaskRequest("SLEEP", str(self.source), execution_mode="read_only", profile="opencode"))
            broker.start(record.id)
            pgid = broker._process_handles[record.id].pgid
            events_path = self.home / "artifacts" / record.id / "events.jsonl"
            self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
            broker.timeout(record.id)
            self.assertFalse(group_alive(pgid))
        finally:
            broker.close()


if __name__ == "__main__":
    unittest.main()
