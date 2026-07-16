"""Phase 5C: configurable verification-gate policy integrated into the real lifecycle.

Every test that spawns a real OS process group cleans it up (SIGKILL + wait)
in a finally block, whether the assertions above it passed or failed.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from recollect_lines import cli, mcp_server
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.opencode_adapter import OpenCodeAdapter
from recollect_lines.service import Broker

FAKE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"
PASSING_COMMAND = [sys.executable, "-c", "print('ok')"]
FAILING_COMMAND = [sys.executable, "-c", "import sys; sys.exit(1)"]
NONEXISTENT_COMMAND = ["/no/such/binary-recollect-lines-phase5c-test"]


def fake_adapter(grace_period_seconds=2.0):
    return OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE)), grace_period_seconds=grace_period_seconds)


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


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class VerificationGateMockTests(unittest.TestCase):
    """The mock adapter's synchronous complete() is the simplest place to
    exercise every policy outcome without a real subprocess/process-group.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, verification_policy="none", verify_commands=None, execution_mode="read_only"):
        request = TaskRequest("verify me", str(self.workspace), execution_mode=execution_mode, verification_policy=verification_policy)
        return self.broker.create(request, verify_commands=verify_commands)

    # --- "none": evidence-only, fully backward compatible ---------------

    def test_none_policy_runs_declared_commands_as_evidence_without_affecting_terminal_state(self):
        record = self.create(verification_policy="none", verify_commands=[FAILING_COMMAND])
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "done")
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        verification = json.loads((self.home / "artifacts" / record.id / "verification.json").read_text())
        self.assertFalse(verification["commands"][0]["passed"])
        gate = json.loads((self.home / "artifacts" / record.id / "verification_gate.json").read_text())
        self.assertEqual(gate, {"policy": "none", "commands_declared": True, "outcome": "failed", "verification_artifact": "verification.json"})

    def test_none_policy_with_no_commands_declared_writes_no_gate_artifact(self):
        record = self.create(verification_policy="none")
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "done")
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        names = [item["name"] for item in self.broker.store.artifact_manifest(record.id)["files"]]
        self.assertNotIn("verification_gate.json", names)
        self.assertNotIn("verification.json", names)

    # --- "advisory": downgrades on failure, never blocks -----------------

    def test_advisory_policy_downgrades_success_to_succeeded_with_warnings_on_failure(self):
        record = self.create(verification_policy="advisory", verify_commands=[FAILING_COMMAND])
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "done")
        self.assertEqual(completed.state, TaskState.SUCCEEDED_WITH_WARNINGS)
        last_event = self.broker.store.events(record.id)[-1]
        self.assertEqual(last_event["metadata"]["verification_gate"]["outcome"], "failed")
        # The original runtime-reported result is never erased or rewritten.
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["summary"], "done")

    def test_advisory_policy_leaves_success_untouched_when_verification_passes(self):
        record = self.create(verification_policy="advisory", verify_commands=[PASSING_COMMAND])
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "done")
        self.assertEqual(completed.state, TaskState.SUCCEEDED)

    # --- "required": blocks success outright ------------------------------

    def test_required_policy_blocks_success_into_failed_on_verification_failure(self):
        record = self.create(verification_policy="required", verify_commands=[FAILING_COMMAND])
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "done")
        self.assertEqual(completed.state, TaskState.FAILED)
        gate = json.loads((self.home / "artifacts" / record.id / "verification_gate.json").read_text())
        self.assertEqual(gate["policy"], "required")
        self.assertEqual(gate["outcome"], "failed")
        # Runtime-reported artifact is preserved, not erased, even though the
        # task's terminal state is failed.
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["summary"], "done")

    def test_required_policy_passes_through_to_success_when_verification_passes(self):
        record = self.create(verification_policy="required", verify_commands=[PASSING_COMMAND])
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "done")
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        gate = json.loads((self.home / "artifacts" / record.id / "verification_gate.json").read_text())
        self.assertEqual(gate["outcome"], "passed")

    def test_required_policy_with_no_commands_declared_is_blocked(self):
        record = self.create(verification_policy="required")
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "done")
        self.assertEqual(completed.state, TaskState.FAILED)
        gate = json.loads((self.home / "artifacts" / record.id / "verification_gate.json").read_text())
        self.assertEqual(gate, {"policy": "required", "commands_declared": False, "outcome": "blocked_no_commands_declared"})

    def test_required_policy_with_unrunnable_command_is_blocked_not_silently_passed(self):
        record = self.create(verification_policy="required", verify_commands=[NONEXISTENT_COMMAND])
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "done")
        self.assertEqual(completed.state, TaskState.FAILED)
        gate = json.loads((self.home / "artifacts" / record.id / "verification_gate.json").read_text())
        self.assertEqual(gate["outcome"], "blocked_verification_error")
        self.assertIn("error", gate)

    # --- idempotency --------------------------------------------------------

    def test_repeated_collect_after_complete_never_reruns_verification(self):
        record = self.create(verification_policy="required", verify_commands=[PASSING_COMMAND])
        self.broker.start(record.id)
        self.broker.complete(record.id, "done")

        calls = {"n": 0}
        original_verify = self.broker.verify

        def counting_verify(*args, **kwargs):
            calls["n"] += 1
            return original_verify(*args, **kwargs)

        self.broker.verify = counting_verify
        first = self.broker.collect(record.id)
        second = self.broker.collect(record.id)
        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 0, "collect() on an already-terminal task must not re-run verification")

    # --- CLI parity -----------------------------------------------------------

    def test_cli_create_wires_verification_policy_and_verify_commands(self):
        exit_code = cli.main([
            "--home", str(self.home), "create",
            "--task", "cli verify", "--workspace", str(self.workspace),
            "--verification-policy", "required",
            "--verify-command", json.dumps(FAILING_COMMAND),
        ])
        self.assertEqual(exit_code, 0)
        records = self.broker.store.list()
        record = next(r for r in records if r.task == "cli verify")
        self.assertEqual(record.verification_policy, "required")
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "done")
        self.assertEqual(completed.state, TaskState.FAILED)


class VerificationGateOpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.source = init_repo(Path(self.tempdir.name) / "source")

    def tearDown(self):
        self.tempdir.cleanup()

    def make_broker(self):
        return Broker(self.home, opencode_adapter=fake_adapter())

    def test_required_verification_blocks_a_successful_opencode_run(self):
        broker = self.make_broker()
        try:
            record = broker.create(
                TaskRequest("Inspect tests", str(self.source), execution_mode="isolated_worktree", profile="opencode", verification_policy="required"),
                verify_commands=[FAILING_COMMAND],
            )
            broker.start(record.id)
            collected = broker.collect(record.id)
            self.assertEqual(collected.state, TaskState.FAILED)
            result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
            self.assertEqual(result["state"], "succeeded")  # runtime claim preserved, never erased
            gate = json.loads((self.home / "artifacts" / record.id / "verification_gate.json").read_text())
            self.assertEqual(gate["outcome"], "failed")
            # Workspace still gets safely released even though the gate blocked success.
            self.assertEqual(broker.store.get_lease(record.id)["status"], "released")
        finally:
            broker.close()

    def test_required_verification_allows_a_successful_opencode_run_through(self):
        broker = self.make_broker()
        try:
            record = broker.create(
                TaskRequest("Inspect tests", str(self.source), execution_mode="isolated_worktree", profile="opencode", verification_policy="required"),
                verify_commands=[PASSING_COMMAND],
            )
            broker.start(record.id)
            collected = broker.collect(record.id)
            self.assertEqual(collected.state, TaskState.SUCCEEDED)
        finally:
            broker.close()

    def test_verification_never_rescues_a_failed_opencode_run(self):
        broker = self.make_broker()
        try:
            record = broker.create(
                TaskRequest("NONZERO_EXIT", str(self.source), execution_mode="read_only", profile="opencode", verification_policy="required"),
                verify_commands=[PASSING_COMMAND],
            )
            broker.start(record.id)
            collected = broker.collect(record.id)
            self.assertEqual(collected.state, TaskState.FAILED)
            gate = json.loads((self.home / "artifacts" / record.id / "verification_gate.json").read_text())
            self.assertEqual(gate["outcome"], "passed")  # evidence still collected...
        finally:
            broker.close()
        # ...but a passing check never turns a genuine runtime failure into a success.

    # --- interrupted/missing verification during recovery ------------------

    def test_broker_crash_after_runtime_finished_but_before_verification_never_reconciles_to_success(self):
        """Replicate exactly what collect() does up through popping the handle and
        reaping the process, then stop — modeling a broker crash after the
        runtime finished but before required verification (or even the
        candidate result) was durably finalized. A fresh Broker must never
        reconcile this into succeeded; the only honest outcome is failed.
        """
        broker1 = self.make_broker()
        record = broker1.create(
            TaskRequest("Inspect tests", str(self.source), execution_mode="isolated_worktree", profile="opencode", verification_policy="required"),
            verify_commands=[PASSING_COMMAND],
        )
        broker1.start(record.id)
        handle = broker1._process_handles.pop(record.id)
        handle.popen.wait(timeout=5)  # the runtime genuinely finished successfully...
        broker1.store.transition(record.id, TaskState.COLLECTING, "Collecting OpenCode result", {})
        # ...but the crash happens right here: no result.json, no verification,
        # no terminal transition ever gets written.
        worktree_path = Path(broker1.store.get_lease(record.id)["worktree_path"])
        broker1.close()

        broker2 = self.make_broker()
        try:
            self.assertEqual(broker2.store.get(record.id).state, TaskState.COLLECTING)
            reconciled = broker2.reconcile(record.id)
            self.assertEqual(reconciled.state, TaskState.FAILED)
            self.assertFalse((self.home / "artifacts" / record.id / "result.json").exists())
            self.assertFalse((self.home / "artifacts" / record.id / "verification_gate.json").exists())
            self.assertFalse(worktree_path.exists())
            self.assertEqual(broker2.store.get_lease(record.id)["status"], "released")

            # Idempotent: re-reconciling (or reconcile_pending) never flips this to success.
            again = broker2.reconcile(record.id)
            self.assertEqual(again.state, TaskState.FAILED)
        finally:
            broker2.close()

    def test_broker_crash_mid_collect_with_still_alive_process_group_enters_recovery_required(self):
        """The rarer twin of the above: the crash happens before the process
        even exited. A fresh broker must classify liveness before touching
        anything, exactly like the running-state case Phase 5B already covers.
        """
        broker1 = self.make_broker()
        record = broker1.create(
            TaskRequest("SLEEP", str(self.source), execution_mode="isolated_worktree", profile="opencode", verification_policy="required"),
            verify_commands=[PASSING_COMMAND],
        )
        broker1.start(record.id)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))

        handle = broker1._process_handles.pop(record.id)
        pgid = handle.pgid
        broker1.store.transition(record.id, TaskState.COLLECTING, "Collecting OpenCode result", {})
        worktree_path = Path(broker1.store.get_lease(record.id)["worktree_path"])
        broker1.close()

        broker2 = self.make_broker()
        try:
            reconciled = broker2.reconcile(record.id)
            self.assertEqual(reconciled.state, TaskState.RECOVERY_REQUIRED)
            self.assertTrue(worktree_path.exists(), "must not delete a worktree a live process might still use")
            self.assertEqual(broker2.store.get_lease(record.id)["status"], "active")
        finally:
            kill_and_reap(handle.popen, pgid)
            broker2.close()


class VerificationGateMcpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_handle_collect_surfaces_verification_gate_label(self):
        record = self.broker.create(
            TaskRequest("verify me", str(self.workspace), verification_policy="none"),
            verify_commands=[PASSING_COMMAND],
        )
        self.broker.start(record.id)
        collected = mcp_server.handle_collect(self.broker, {"task_id": record.id})
        self.assertEqual(collected["state"], "failed")  # mock has no MCP completion path
        self.assertEqual(collected["verification_gate"]["label"], "runtime_reported")
        self.assertTrue(collected["broker_verification"]["commands"][0]["passed"])


if __name__ == "__main__":
    unittest.main()
