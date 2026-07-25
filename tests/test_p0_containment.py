"""RFC-004 P0 containment slice: regression tests for the most dangerous
legacy subprocess behaviors, without the full durable-supervisor migration.

Covers:
  1. status()/reconcile() must not report `running` for an in-memory legacy
     subprocess handle that has already exited -- a nonblocking poll must
     reap/collect it instead.
  2. collect() on a genuinely still-running legacy subprocess must return
     promptly with an explicit nonterminal result, never an unbounded
     Popen.wait().
  3. The Darwin/non-Linux time.monotonic_ns() start-identity fallback must
     never be treated as proof of death -- only "unknown".
     (Platform-level proofs live in tests/liveness_contract.py, collected via
     tests/test_durable_supervisor_conformance.py.)
  4. Cursor restart reconciliation must never finalize (release) a workspace
     from that unverifiable non-Linux fallback identity alone.
  5. status() must not serve a stale zero-byte artifact manifest for an
     active task.

Every test that spawns a real OS process cleans it up in a finally block,
whether the assertions above it passed or failed.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.service import Broker

FAKE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"
FAKE_CURSOR = Path(__file__).parent / "fixtures" / "fake_cursor.py"


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def fake_codex_adapter(grace_period_seconds=2.0):
    from recollect_lines.adaptor.codex import CodexAdapter

    return CodexAdapter(command_prefix=(sys.executable, str(FAKE_CODEX)), grace_period_seconds=grace_period_seconds)


def fake_cursor_adapter(grace_period_seconds=2.0):
    from tests.legacy_cursor_fixture import LegacyCursorFixtureAdapter

    # CursorDarwinFallbackReconciliationTests below exercise a test-only
    # pre-RFC-004 direct-Popen record + leader
    # PID+start-identity restart-safety fix on purpose -- see adaptor/cursor.py's
    # module docstring and test_cursor_uncollected_reconciliation.py.
    return LegacyCursorFixtureAdapter(
        command_prefix=(sys.executable, str(FAKE_CURSOR)),
        grace_period_seconds=grace_period_seconds,

    )


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


def kill_and_reap(popen, pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    popen.wait(timeout=5)


class ExitedInMemoryHandleReapTests(unittest.TestCase):
    """Requirement 1: an already-exited in-memory handle must not keep
    reporting `running` just because a ProcessHandle object still exists.

    Every production subprocess adapter is durable by default now (RFC-004
    durable-opencode slice was the last), so this generic (adapter-agnostic)
    legacy-handle-reap coverage uses tests.legacy_cursor_fixture rather than a
    real production adapter -- these tests never
    reach the Cursor-specific restart-reconciliation branch (no broker
    restart is simulated here), so it exercises exactly the same generic
    Broker._reap_exited_process_handle()/collect() code path a legacy
    OpenCode adapter used to.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, cursor_adapter=fake_cursor_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _create_and_start(self):
        record = self.broker.create(TaskRequest("Inspect fact.txt", str(self.workspace), profile="cursor", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles[record.id]
        self.assertTrue(wait_until(lambda: handle.popen.poll() is not None), "fixture must exit on its own for this test")
        return record

    def test_reconcile_reaps_an_already_exited_handle_in_the_same_broker(self):
        record = self._create_and_start()

        reconciled = self.broker.reconcile(record.id)

        self.assertEqual(reconciled.state, TaskState.SUCCEEDED)
        self.assertNotIn(record.id, self.broker._process_handles)

    def test_status_reaps_an_already_exited_handle_in_the_same_broker(self):
        record = self._create_and_start()

        status = self.broker.status(record.id)

        self.assertEqual(status["state"], "succeeded")
        self.assertNotIn(record.id, self.broker._process_handles)

    def test_status_is_unaffected_when_the_handle_is_genuinely_still_running(self):
        record = self.broker.create(TaskRequest("SLEEP", str(self.workspace), profile="cursor", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles[record.id]
        self.assertTrue(wait_until(lambda: handle.stderr_path.exists() and b"started" in handle.stderr_path.read_bytes()))
        try:
            status = self.broker.status(record.id)
            self.assertEqual(status["state"], "running")
            self.assertIn(record.id, self.broker._process_handles)
        finally:
            kill_and_reap(handle.popen, handle.pgid)


class CollectDoesNotBlockOnALiveSubprocessTests(unittest.TestCase):
    """Requirement 2: collect() on a genuinely live legacy subprocess must
    return promptly with an explicit nonterminal result, never block on an
    unbounded Popen.wait().
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_collect_on_a_live_process_returns_promptly_and_nonterminal(self):
        # Generic (adapter-agnostic) legacy-handle coverage, see
        # ExitedInMemoryHandleReapTests' class docstring for why the test-only
        # legacy fixture stands in for OpenCode here.
        broker = Broker(self.home, cursor_adapter=fake_cursor_adapter())
        try:
            record = broker.create(TaskRequest("SLEEP", str(self.workspace), profile="cursor", execution_mode="read_only"))
            broker.start(record.id)
            handle = broker._process_handles[record.id]
            self.assertTrue(wait_until(lambda: handle.stderr_path.exists() and b"started" in handle.stderr_path.read_bytes()))

            with mock.patch("recollect_lines.service.LEGACY_PROCESS_COLLECT_GRACE_SECONDS", 0.3):
                started_at = time.monotonic()
                collected = broker.collect(record.id)
                elapsed = time.monotonic() - started_at

            self.assertLess(elapsed, 3.0, "collect() must not block for anywhere near the fixture's 30s sleep")
            self.assertEqual(collected.state, TaskState.RUNNING)
            self.assertIn(record.id, broker._process_handles, "handle must be retained for a later collect() retry")
        finally:
            kill_and_reap(handle.popen, handle.pgid)
            broker.close()

    def test_collect_still_returns_the_terminal_result_for_a_fast_exiting_process(self):
        """Regression guard: the bounded grace wait must not break the very
        common case (fixtures, and real fast CLI runs) where the process has
        already finished by the time collect() is called.
        """
        broker = Broker(self.home, codex_adapter=fake_codex_adapter())
        try:
            record = broker.create(TaskRequest("what is the magic number", str(self.workspace), profile="codex", execution_mode="read_only"))
            broker.start(record.id)
            collected = broker.collect(record.id)
            self.assertEqual(collected.state, TaskState.SUCCEEDED)
        finally:
            broker.close()


class CursorDarwinFallbackReconciliationTests(unittest.TestCase):
    """Requirement 3 + 4 together: on a platform where start-identity is only
    best-effort, restart reconciliation for a still-alive Cursor leader must
    land in recovery_required and must never finalize (release) the
    worktree -- never `dead`/`uncollected` from an unverifiable identity.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.source = init_repo(Path(self.tempdir.name) / "source")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_live_leader_under_darwin_fallback_stays_recovery_required_and_keeps_the_worktree(self):
        broker1 = Broker(self.home, cursor_adapter=fake_cursor_adapter())
        record = broker1.create(
            TaskRequest("SLEEP", str(self.source), profile="cursor", execution_mode="isolated_worktree"),
        )
        with mock.patch("recollect_lines.durable_runner.sys.platform", "darwin"):
            broker1.start(record.id)  # captures the buggy monotonic-based leader_start_identity
        handle = broker1._process_handles[record.id]
        self.assertTrue(wait_until(lambda: handle.stderr_path.exists() and b"started" in handle.stderr_path.read_bytes()))
        pgid = handle.pgid
        worktree_path = Path(broker1.store.get_lease(record.id)["worktree_path"])
        launch = broker1.store.get_launch(record.id)
        self.assertFalse(launch["leader_start_identity"].startswith("linux:"))
        broker1.close()  # simulate a broker restart; the real leader process is untouched

        broker2 = Broker(self.home, cursor_adapter=fake_cursor_adapter())
        try:
            with mock.patch("recollect_lines.durable_runner.sys.platform", "darwin"):
                result = broker2.reconcile(record.id)

            self.assertEqual(
                result.state, TaskState.RECOVERY_REQUIRED,
                "an unverifiable non-Linux identity must never be inferred as proof of death",
            )
            self.assertTrue(worktree_path.exists(), "a live leader's worktree must never be released on inferred liveness")
            self.assertEqual(broker2.store.get_lease(record.id)["status"], "active")
            last_event = broker2.store.events(record.id)[-1]
            self.assertEqual(last_event["metadata"]["leader"]["state"], "unknown")
        finally:
            kill_and_reap(handle.popen, pgid)
            broker2._finalize_workspace(record.id)
            broker2.close()


class ActiveTaskArtifactManifestFreshnessTests(unittest.TestCase):
    """Requirement 5: status() must not serve a stale zero-byte artifact
    manifest for an active (non-terminal) task.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_status_refreshes_the_manifest_for_a_still_running_task(self):
        # This requirement is specifically about the legacy Popen model, where
        # an adapter writes stdout/stderr directly into the task's own
        # artifacts_dir (so a stale in-memory manifest can under-report real
        # bytes already on disk there). A durable launch's stdout/stderr live
        # under home/durable_launches/<id>/, never under artifacts_dir, so no
        # production adapter is representative here anymore (every one is
        # durable by default, RFC-004) -- the test-only legacy fixture stands
        # in instead, exactly as
        # ExitedInMemoryHandleReapTests and CollectDoesNotBlockOnALiveSubprocessTests
        # above already do.
        broker = Broker(self.home, cursor_adapter=fake_cursor_adapter())
        try:
            record = broker.create(TaskRequest("SLEEP", str(self.workspace), profile="cursor", execution_mode="read_only"))
            broker.start(record.id)
            handle = broker._process_handles[record.id]
            # Cursor's SLEEP fixture writes "started" to stderr immediately and
            # only ever writes to stdout once the run completes 30s later, so
            # stderr.log (not stdout.log) is this adapter's early-real-bytes
            # artifact -- the manifest-staleness gap requirement 5 covers
            # applies identically regardless of which stream the CLI happens
            # to write to first.
            self.assertTrue(wait_until(lambda: handle.stderr_path.exists() and b"started" in handle.stderr_path.read_bytes()))

            manifest_before = broker.store.artifact_manifest(record.id)
            stderr_entry_before = next(f for f in manifest_before["files"] if f["name"] == "stderr.log")
            self.assertEqual(stderr_entry_before["bytes"], 0, "sanity: the manifest was only ever generated once, at launch")

            status = broker.status(record.id)

            stderr_entry_after = next(f for f in status["artifacts"]["files"] if f["name"] == "stderr.log")
            self.assertGreater(
                stderr_entry_after["bytes"], 0,
                "status() must refresh the manifest for an active task instead of serving the stale launch-time snapshot",
            )
        finally:
            kill_and_reap(handle.popen, handle.pgid)
            broker.close()

    def test_status_does_not_refresh_the_manifest_for_a_terminal_task(self):
        broker = Broker(self.home, codex_adapter=fake_codex_adapter())
        try:
            record = broker.create(TaskRequest("what is the magic number", str(self.workspace), profile="codex", execution_mode="read_only"))
            broker.start(record.id)
            broker.collect(record.id)
            manifest_before = broker.store.artifact_manifest(record.id)

            broker.status(record.id)

            manifest_after = broker.store.artifact_manifest(record.id)
            self.assertEqual(manifest_before["generated_at"], manifest_after["generated_at"])
        finally:
            broker.close()


if __name__ == "__main__":
    unittest.main()
