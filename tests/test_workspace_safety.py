import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from recollect_lines.models import ProfilePolicy, RecoveryRequired, TaskRequest, TaskState
from recollect_lines.opencode_adapter import OpenCodeAdapter
from recollect_lines.service import Broker
from recollect_lines.workspace import WorkspaceError, WorkspaceManager, canonical_source

FAKE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


def fake_adapter(grace_period_seconds=2.0):
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


class WorkspaceManagerUnitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_canonical_source_rejects_non_git_directory(self):
        plain_dir = Path(self.tempdir.name) / "plain"
        plain_dir.mkdir()
        with self.assertRaises(WorkspaceError):
            canonical_source(str(plain_dir))

    def test_release_refuses_to_remove_path_outside_broker_worktrees_root(self):
        source = init_repo(Path(self.tempdir.name) / "source")
        manager = WorkspaceManager(self.home)
        with self.assertRaises(WorkspaceError):
            manager.release(str(source), str(source))
        self.assertTrue(source.exists())
        self.assertEqual((source / "file.txt").read_text(), "original\n")

    def test_release_is_idempotent_when_already_absent(self):
        source = init_repo(Path(self.tempdir.name) / "source")
        manager = WorkspaceManager(self.home)
        base_sha = run_git(["rev-parse", "HEAD"], cwd=source).stdout.strip()
        allocation = manager.create_worktree(str(source), "tsk_fake", base_sha)
        first = manager.release(str(source), allocation.worktree_path)
        self.assertFalse(first["already_absent"])
        second = manager.release(str(source), allocation.worktree_path)
        self.assertTrue(second["already_absent"])
        self.assertTrue(source.exists())


class WorkspaceBrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.source = init_repo(Path(self.tempdir.name) / "source")

    def tearDown(self):
        self.tempdir.cleanup()

    def make_broker(self, profiles=None):
        return Broker(self.home, profiles=profiles)

    def test_isolated_worktree_receives_changes_while_source_stays_untouched(self):
        broker = self.make_broker()
        record = broker.create(TaskRequest("edit files", str(self.source), execution_mode="isolated_worktree"))
        started = broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)

        run_event = next(e for e in broker.store.events(record.id) if e["type"] == "task.running")
        worktree_path = Path(run_event["metadata"]["workspace"])
        self.assertNotEqual(str(worktree_path), str(self.source))
        self.assertTrue(str(worktree_path).startswith(str(self.home / "worktrees")))

        (worktree_path / "file.txt").write_text("changed by task\n")
        (worktree_path / "new.txt").write_text("brand new\n")

        completed = broker.complete(record.id, "made edits")
        self.assertEqual(completed.state, TaskState.SUCCEEDED)

        # Source untouched.
        self.assertEqual((self.source / "file.txt").read_text(), "original\n")
        self.assertFalse((self.source / "new.txt").exists())
        status = run_git(["status", "--porcelain"], cwd=self.source)
        self.assertEqual(status.stdout.strip(), "")

        # Worktree cleaned up.
        self.assertFalse(worktree_path.exists())

        status_payload = json.loads((self.home / "artifacts" / record.id / "workspace_status.json").read_text())
        self.assertEqual(status_payload["diff_status"], "changed")
        changed = {tuple(p["paths"]) for p in status_payload["changed_paths"]}
        self.assertIn(("file.txt",), changed)
        self.assertIn(("new.txt",), changed)
        diff_bytes = (self.home / "artifacts" / record.id / "diff.patch").read_bytes()
        self.assertIn(b"new.txt", diff_bytes)
        self.assertIn(b"changed by task", diff_bytes)
        broker.close()

    def test_diff_and_status_distinguish_clean_from_changed(self):
        broker = self.make_broker()
        clean = broker.create(TaskRequest("no changes", str(self.source), execution_mode="isolated_worktree"))
        broker.start(clean.id)
        broker.complete(clean.id, "nothing changed")
        clean_status = json.loads((self.home / "artifacts" / clean.id / "workspace_status.json").read_text())
        self.assertEqual(clean_status["diff_status"], "clean")
        self.assertEqual(clean_status["changed_paths"], [])
        self.assertEqual((self.home / "artifacts" / clean.id / "diff.patch").read_bytes(), b"")
        broker.close()

    def test_two_writers_contend_exactly_one_wins_read_only_stays_concurrent(self):
        profiles = {"mock": ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 5)}
        broker = self.make_broker(profiles=profiles)
        writer_a = broker.create(TaskRequest("writer a", str(self.source), execution_mode="isolated_worktree"))
        writer_b = broker.create(TaskRequest("writer b", str(self.source), execution_mode="isolated_worktree"))
        reader = broker.create(TaskRequest("reader", str(self.source), execution_mode="read_only"))

        started_a = broker.start(writer_a.id)
        self.assertEqual(started_a.state, TaskState.RUNNING)

        started_b = broker.start(writer_b.id)
        self.assertEqual(started_b.state, TaskState.FAILED)
        last_event = broker.store.events(writer_b.id)[-1]
        self.assertEqual(last_event["metadata"]["reason"], "workspace_lease_conflict")

        # Read-only concurrency is unaffected by the writer's lease.
        started_reader = broker.start(reader.id)
        self.assertEqual(started_reader.state, TaskState.RUNNING)
        self.assertIsNone(broker.store.get_lease(reader.id))

        broker.complete(writer_a.id, "writer a done")
        broker.complete(reader.id, "reader done")
        broker.close()

    def test_lease_persists_reloads_from_sqlite_and_releases_on_cleanup(self):
        broker = Broker(self.home)
        record = broker.create(TaskRequest("lease test", str(self.source), execution_mode="isolated_worktree"))
        broker.start(record.id)
        lease = broker.store.get_lease(record.id)
        self.assertEqual(lease["status"], "active")
        self.assertEqual(Path(lease["canonical_source"]).resolve(), self.source.resolve())
        broker.close()

        reloaded = Broker(self.home)
        try:
            reloaded_lease = reloaded.store.get_lease(record.id)
            self.assertEqual(reloaded_lease["status"], "active")
            self.assertEqual(reloaded_lease["worktree_path"], lease["worktree_path"])

            reloaded.complete(record.id, "done after reload")
            released = reloaded.store.get_lease(record.id)
            self.assertEqual(released["status"], "released")
            self.assertIsNotNone(released["released_at"])
            self.assertFalse(Path(lease["worktree_path"]).exists())
        finally:
            reloaded.close()

    def test_verify_captures_raw_output_and_broker_verified_truth(self):
        broker = self.make_broker()
        record = broker.create(TaskRequest("verify me", str(self.source), execution_mode="isolated_worktree"))
        broker.start(record.id)

        result = broker.verify(record.id, [
            [sys.executable, "-c", "print('all good')"],
            [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
        ])

        self.assertEqual(result["scope"], "isolated_worktree")
        passing, failing = result["commands"]
        self.assertTrue(passing["broker_verified"])
        self.assertTrue(passing["passed"])
        self.assertEqual(passing["exit_code"], 0)
        self.assertIn("all good", passing["stdout"])
        self.assertTrue(failing["broker_verified"])
        self.assertFalse(failing["passed"])
        self.assertEqual(failing["exit_code"], 3)
        self.assertIn("boom", failing["stderr"])

        artifact = json.loads((self.home / "artifacts" / record.id / "verification.json").read_text())
        self.assertEqual(artifact, result)
        events = broker.store.events(record.id)
        self.assertEqual(events[-1]["type"], "task.verified")
        self.assertFalse(events[-1]["metadata"]["all_passed"])

        broker.complete(record.id, "done")
        broker.close()

    def test_verify_rejects_non_argv_commands(self):
        broker = self.make_broker()
        record = broker.create(TaskRequest("verify me", str(self.source), execution_mode="read_only"))
        broker.start(record.id)
        with self.assertRaises(ValueError):
            broker.verify(record.id, ["pytest -q"])
        broker.close()

    def test_verify_refuses_to_fall_back_to_source_after_worktree_release(self):
        broker = self.make_broker()
        record = broker.create(TaskRequest("verify after release", str(self.source), execution_mode="isolated_worktree"))
        broker.start(record.id)
        broker.complete(record.id, "done")
        with self.assertRaises(ValueError):
            broker.verify(record.id, [[sys.executable, "-c", "print('should never run against source')"]])
        broker.close()

    def test_verify_validates_all_commands_before_running_any(self):
        broker = self.make_broker()
        record = broker.create(TaskRequest("verify validation", str(self.source), execution_mode="isolated_worktree"))
        broker.start(record.id)
        worktree_path = Path(broker.store.get_lease(record.id)["worktree_path"])
        marker = worktree_path / "marker.txt"

        with self.assertRaises(ValueError):
            broker.verify(record.id, [
                [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                "not-an-argv-array",
            ])
        self.assertFalse(marker.exists())
        broker.close()

    def test_cancel_cleanup_is_idempotent_and_never_touches_source(self):
        broker = self.make_broker()
        record = broker.create(TaskRequest("cancel me", str(self.source), execution_mode="isolated_worktree"))
        broker.start(record.id)
        lease = broker.store.get_lease(record.id)
        worktree_path = Path(lease["worktree_path"])
        (worktree_path / "scratch.txt").write_text("wip\n")

        cancelled = broker.cancel(record.id, "not needed anymore")
        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        self.assertFalse(worktree_path.exists())
        self.assertEqual(broker.store.get_lease(record.id)["status"], "released")

        # Idempotent retry, as if a crash-recovery pass re-ran cleanup.
        broker._finalize_workspace(record.id)
        result = broker.workspaces.release(lease["canonical_source"], lease["worktree_path"])
        self.assertTrue(result["already_absent"])

        self.assertTrue(self.source.exists())
        self.assertEqual((self.source / "file.txt").read_text(), "original\n")
        broker.close()

    def test_timeout_cleanup_never_removes_source_and_is_idempotent(self):
        broker = self.make_broker()
        record = broker.create(TaskRequest("timeout me", str(self.source), execution_mode="isolated_worktree"))
        broker.start(record.id)

        timed_out = broker.timeout(record.id)
        self.assertEqual(timed_out.state, TaskState.TIMED_OUT)
        lease = broker.store.get_lease(record.id)
        self.assertEqual(lease["status"], "released")
        self.assertFalse(Path(lease["worktree_path"]).exists())

        result = broker.workspaces.release(lease["canonical_source"], lease["worktree_path"])
        self.assertTrue(result["already_absent"])
        self.assertTrue(self.source.exists())
        self.assertEqual((self.source / "file.txt").read_text(), "original\n")
        broker.close()

    def test_start_fails_clearly_when_workspace_is_not_a_git_repo(self):
        plain_dir = Path(self.tempdir.name) / "plain"
        plain_dir.mkdir()
        broker = self.make_broker()
        record = broker.create(TaskRequest("bad workspace", str(plain_dir), execution_mode="isolated_worktree"))
        started = broker.start(record.id)
        self.assertEqual(started.state, TaskState.FAILED)
        self.assertEqual(broker.store.events(record.id)[-1]["metadata"]["reason"], "workspace_invalid")
        broker.close()

    def test_read_only_tasks_never_acquire_a_lease(self):
        broker = self.make_broker()
        record = broker.create(TaskRequest("read only", str(self.source), execution_mode="read_only"))
        broker.start(record.id)
        self.assertIsNone(broker.store.get_lease(record.id))
        broker.complete(record.id, "done")
        broker.close()


class OpenCodeWorkspaceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.source = init_repo(Path(self.tempdir.name) / "source")
        self.broker = Broker(self.home, opencode_adapter=fake_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_isolated_worktree_is_the_effective_workspace_and_cleans_up_on_success(self):
        record = self.broker.create(TaskRequest("inspect tests", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)

        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        worktree_path = Path(run_event["metadata"]["workspace"])
        self.assertNotEqual(str(worktree_path), str(self.source))
        self.assertTrue(str(worktree_path).startswith(str(self.home / "worktrees")))

        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        self.assertFalse(worktree_path.exists())
        self.assertEqual(self.broker.store.get_lease(record.id)["status"], "released")

    def test_collect_enters_recovery_state_when_process_group_is_still_alive_after_a_simulated_restart(self):
        record = self.broker.create(TaskRequest("SLEEP", str(self.source), execution_mode="isolated_worktree", profile="opencode"))
        self.broker.start(record.id)

        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        pgid = run_event["metadata"]["pgid"]
        worktree_path = Path(run_event["metadata"]["workspace"])
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))

        # Simulate a broker restart: the in-memory handle is gone, but the
        # real process (started with start_new_session=True) is still alive.
        orphaned_handle = self.broker._process_handles.pop(record.id)
        try:
            with self.assertRaises(RecoveryRequired):
                self.broker.collect(record.id)
            recovering = self.broker.store.get(record.id)
            self.assertEqual(recovering.state, TaskState.RECOVERY_REQUIRED)
            self.assertTrue(worktree_path.exists(), "must not delete a worktree a live process might still use")
            self.assertEqual(self.broker.store.get_lease(record.id)["status"], "active")

            # Idempotent: calling collect() again while still alive raises the
            # same way and makes no further state/lease change.
            with self.assertRaises(RecoveryRequired):
                self.broker.collect(record.id)
            self.assertEqual(self.broker.store.get(record.id).state, TaskState.RECOVERY_REQUIRED)
            self.assertEqual(self.broker.store.get_lease(record.id)["status"], "active")
        finally:
            os.killpg(pgid, signal.SIGKILL)
            orphaned_handle.popen.wait(timeout=5)

        # Once the process is confirmed dead, reconciliation can proceed to a
        # truthful failure and release the lease.
        failed = self.broker.reconcile(record.id)
        self.assertEqual(failed.state, TaskState.FAILED)
        self.assertFalse(worktree_path.exists())
        self.assertEqual(self.broker.store.get_lease(record.id)["status"], "released")
        self.assertEqual((self.source / "file.txt").read_text(), "original\n")


if __name__ == "__main__":
    unittest.main()
