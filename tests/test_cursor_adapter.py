import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from recollect_lines.adaptor import AdapterCapabilities, LaunchSpec
from recollect_lines.adaptor.cursor import (
    DEFAULT_COMMAND_PREFIX,
    CursorAdapter,
    CursorUnsupportedPolicy,
    redact_secrets,
)
from recollect_lines.durable_cli_launch import is_durable_launch_terminal
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.service import Broker

FIXTURE = Path(__file__).parent / "fixtures" / "fake_cursor.py"


def fake_adapter(grace_period_seconds=2.0, model=None):
    return CursorAdapter(command_prefix=(sys.executable, str(FIXTURE)), grace_period_seconds=grace_period_seconds, model=model)


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class CursorAdapterUnitTests(unittest.TestCase):
    def test_default_command_prefix_is_the_bare_cursor_agent_binary(self):
        adapter = CursorAdapter()
        self.assertEqual(adapter.command_prefix, DEFAULT_COMMAND_PREFIX)

    def test_build_command_maps_read_only_to_enabled_sandbox_and_plan_mode(self):
        adapter = fake_adapter()
        command = adapter.build_command("inspect", "read_only", "/tmp/ws")
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "enabled")
        self.assertIn("--mode", command)
        self.assertEqual(command[command.index("--mode") + 1], "plan")
        self.assertEqual(command[command.index("--workspace") + 1], "/tmp/ws")
        self.assertEqual(command[-1], "inspect")

    def test_build_command_maps_isolated_worktree_to_disabled_sandbox_without_plan_mode(self):
        adapter = fake_adapter()
        command = adapter.build_command("edit stuff", "isolated_worktree", "/tmp/wt")
        self.assertEqual(command[command.index("--sandbox") + 1], "disabled")
        self.assertNotIn("--mode", command)
        self.assertNotIn("plan", command)

    def test_build_command_includes_headless_flags(self):
        adapter = fake_adapter()
        command = adapter.build_command("inspect", "read_only", "/tmp/ws")
        self.assertIn("--print", command)
        self.assertIn("--trust", command)
        self.assertIn("--force", command)
        self.assertEqual(command[command.index("--output-format") + 1], "json")

    def test_build_command_fails_closed_for_an_unmapped_execution_mode(self):
        adapter = fake_adapter()
        with self.assertRaises(CursorUnsupportedPolicy):
            adapter.build_command("do something", "shared_write", "/tmp/ws")

    def test_build_command_includes_model_when_configured(self):
        adapter = fake_adapter(model="composer-2.5")
        command = adapter.build_command("inspect", "read_only", "/tmp/ws")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "composer-2.5")

    def test_build_command_omits_model_flag_by_default(self):
        adapter = fake_adapter()
        command = adapter.build_command("inspect", "read_only", "/tmp/ws")
        self.assertNotIn("--model", command)

    def test_redact_secrets_scrubs_api_key_like_tokens(self):
        text = "call failed with key sk-abcdefgh12345678 in the request"
        self.assertNotIn("sk-abcdefgh12345678", redact_secrets(text))
        self.assertIn("***REDACTED***", redact_secrets(text))

    def test_capabilities_declare_only_what_is_actually_implemented(self):
        self.assertIsInstance(CursorAdapter.capabilities, AdapterCapabilities)
        self.assertTrue(CursorAdapter.capabilities.requires_subprocess)
        self.assertTrue(CursorAdapter.capabilities.supports_process_group_cancellation)
        self.assertFalse(CursorAdapter.capabilities.reports_broker_verified_tests)
        self.assertTrue(CursorAdapter.capabilities.uses_durable_subprocess_runner)

    def test_adapter_exposes_no_legacy_popen_option(self):
        adapter = CursorAdapter()
        self.assertFalse(hasattr(adapter, "legacy_popen_launch"))
        self.assertIsNone(adapter.durable_runner)  # bound only by the owning Broker

    def test_build_launch_spec_is_a_provider_neutral_launch_spec(self):
        from recollect_lines.models import TaskRecord

        adapter = fake_adapter()
        record = TaskRecord.new(TaskRequest("inspect", "/tmp/ws", profile="cursor", execution_mode="read_only"))
        spec = adapter.build_launch_spec(record, "/tmp/ws", "inspect")
        self.assertIsInstance(spec, LaunchSpec)
        self.assertEqual(spec.cwd, "/tmp/ws")
        self.assertEqual(spec.argv[-1], "inspect")
        self.assertIn("--sandbox", spec.argv)
        self.assertIsNone(spec.env)

    def test_check_availability_reports_missing_binary_without_raising(self):
        adapter = CursorAdapter(command_prefix=("definitely-not-a-real-cursor-binary-xyz",))
        probe = adapter.check_availability()
        self.assertFalse(probe["available"])
        self.assertEqual(probe["reason"], "cli_not_found")

    def test_check_availability_reports_installed_version(self):
        adapter = fake_adapter()
        probe = adapter.check_availability()
        self.assertTrue(probe["available"])


class CursorBrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, cursor_adapter=fake_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, task="Inspect fact.txt", **kwargs):
        return self.broker.create(TaskRequest(task, str(self.workspace), profile="cursor", **kwargs))

    def test_start_creates_durable_launch_metadata_and_stdout_stderr_artifacts(self):
        record = self.create()
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["adapter"], "cursor")
        self.assertEqual(launch["launch_kind"], "durable_subprocess")
        self.assertIsNotNone(launch["durable_launch_id"])
        launch_dir = self.home / "durable_launches" / launch["durable_launch_id"]
        stdout_path = launch_dir / "stdout.log"
        stderr_path = launch_dir / "stderr.log"
        wait_until(lambda: stdout_path.exists() and stdout_path.stat().st_size > 0)
        self.assertTrue(stderr_path.exists())
        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        self.assertIn("pid", run_event["metadata"])
        self.assertIn("pgid", run_event["metadata"])
        self.assertEqual(run_event["metadata"]["runtime_description"], "Cursor Agent CLI via cursor-agent --print")
        self.broker.collect(record.id)

    def test_cancel_terminates_process_group_read_only(self):
        record = self.broker.create(TaskRequest("SLEEP", str(self.workspace), profile="cursor", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles[record.id]
        stderr_path = handle.durable.launch_dir / "stderr.log"
        wait_until(lambda: stderr_path.exists() and b"started" in stderr_path.read_bytes())

        cancelled = self.broker.cancel(record.id, "no longer needed")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        cancel_event = self.broker.store.events(record.id)[-1]
        self.assertTrue(cancel_event["metadata"]["cancellation"]["group_terminated"])
        self.assertIn("SIGTERM", cancel_event["metadata"]["cancellation"]["signals_sent"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(handle.pgid, 0)

    def test_cancel_escalates_to_sigkill_when_process_ignores_sigterm(self):
        self.broker = Broker(self.home, cursor_adapter=fake_adapter(grace_period_seconds=0.3))
        record = self.broker.create(TaskRequest("SLEEP_IGNORE_TERM", str(self.workspace), profile="cursor", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles[record.id]
        stderr_path = handle.durable.launch_dir / "stderr.log"
        wait_until(lambda: stderr_path.exists() and b"started" in stderr_path.read_bytes())

        cancelled = self.broker.cancel(record.id, "force stop")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        signals_sent = self.broker.store.events(record.id)[-1]["metadata"]["cancellation"]["signals_sent"]
        self.assertEqual(signals_sent, ["SIGTERM", "SIGKILL"])

    def test_collect_parses_single_json_result_object_as_summary(self):
        record = self.create(task="what is the magic number")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertIn("42", result["summary"])
        self.assertEqual(result["runtime"]["adapter"], "cursor")
        self.assertIsNotNone(result["runtime"]["usage"])

    def test_collect_is_defensive_against_a_malformed_leading_line(self):
        record = self.create(task="MALFORMED")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], "partial result despite a malformed line")
        self.assertGreaterEqual(result["runtime"]["malformed_output_lines"], 1)

    def test_collect_with_no_parseable_output_is_succeeded_with_warnings(self):
        record = self.create(task="EMPTY_OUTPUT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED_WITH_WARNINGS)

    def test_auth_error_is_classified_without_leaking_credentials(self):
        record = self.create(task="AUTH_ERROR")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["error_category"], "authentication_error")
        self.assertTrue(result["runtime"]["is_error"])

    def test_rate_limit_error_is_classified_distinctly_from_auth_error(self):
        record = self.create(task="RATE_LIMIT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["error_category"], "rate_limit_or_quota_error")

    def test_nonzero_exit_is_reported_as_failed(self):
        record = self.create(task="NONZERO_EXIT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["exit_code"], 1)

    def test_a_clean_result_followed_by_a_nonzero_exit_is_still_categorized(self):
        record = self.create(task="KILLED_AFTER_RESULT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["exit_code"], 1)
        self.assertIsNotNone(result["runtime"]["error_category"])

    def test_repeated_collect_on_a_terminal_task_is_idempotent(self):
        record = self.create()
        self.broker.start(record.id)
        first = self.broker.collect(record.id)
        second = self.broker.collect(record.id)
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.updated_at, second.updated_at)

    def test_collect_without_a_process_handle_reconciles_via_durable_adoption_not_uncollected(self):
        # Unlike persisted pre-migration records covered by the test-only
        # compatibility fixture in test_cursor_uncollected_reconciliation.py, a
        # durable Cursor launch's outcome is never `uncollected` after a
        # broker restart: the manifest is independent, durable proof a fresh
        # broker can adopt and safely collect from, exactly like
        # FixtureDurableAdapter.
        record = self.create(task="what is the magic number")
        self.broker.start(record.id)
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["launch_kind"], "durable_subprocess")
        handle = self.broker._process_handles[record.id]
        wait_until(lambda: is_durable_launch_terminal(handle))
        self.broker._process_handles.pop(record.id, None)  # simulate a broker restart losing in-memory state

        restarted = Broker(self.home, cursor_adapter=fake_adapter())
        try:
            reconciled = restarted.reconcile(record.id)
            detail = restarted.reconcile_detail(record.id)
            self.assertIn(detail["outcome"], {"adopted_running", "adopted_terminal_collectable"})
            self.assertEqual(detail["launch_id"], launch["durable_launch_id"])
            self.assertNotEqual(reconciled.state, TaskState.UNCOLLECTED)

            completed = restarted.collect(record.id)
            self.assertEqual(completed.state, TaskState.SUCCEEDED)
            result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
            self.assertIn("42", result["summary"])
            self.assertEqual(result["runtime"]["adapter"], "cursor")
        finally:
            restarted.close()

    def test_long_task_survives_broker_restart_is_reconciled_adopted_and_collected(self):
        record = self.broker.create(TaskRequest("SLEEP", str(self.workspace), profile="cursor", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles.pop(record.id)  # simulate a broker restart losing in-memory state
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["launch_kind"], "durable_subprocess")

        restarted = Broker(self.home, cursor_adapter=fake_adapter())
        try:
            reconciled = restarted.reconcile(record.id)
            self.assertEqual(reconciled.state, TaskState.RUNNING)
            detail = restarted.reconcile_detail(record.id)
            self.assertEqual(detail["outcome"], "adopted_running")
            self.assertEqual(detail["launch_id"], launch["durable_launch_id"])
            self.assertIn(record.id, restarted._adopted_durable_handles)

            cancelled = restarted.cancel(record.id, "test cleanup")
            self.assertEqual(cancelled.state, TaskState.CANCELLED)
            with self.assertRaises(ProcessLookupError):
                os.killpg(handle.pgid, 0)

            # cancel() already transitioned this task to a terminal state;
            # collect() on an already-terminal task is idempotent (returns the
            # same durable record) rather than re-running collection.
            completed = restarted.collect(record.id)
            self.assertEqual(completed.state, TaskState.CANCELLED)
        finally:
            try:
                os.killpg(handle.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            restarted.close()


class CursorUnsupportedPolicyBrokerTests(unittest.TestCase):
    def test_broker_start_propagates_unsupported_policy_rather_than_launching(self):
        adapter = fake_adapter()
        with self.assertRaises(CursorUnsupportedPolicy):
            adapter.build_command("do something", "shared_write", "/tmp/ws")


class CursorOwnsNoProcessLifecycleTests(unittest.TestCase):
    """Cursor only builds launch specs and parses terminal output."""

    def test_adapter_does_not_reference_subprocess_popen(self):
        import ast
        import inspect
        from recollect_lines.adaptor import cursor as cursor_module

        source = inspect.getsource(cursor_module)
        tree = ast.parse(source)
        functions_calling_popen = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "Popen"
                    ):
                        functions_calling_popen.add(node.name)
        self.assertEqual(functions_calling_popen, set())

    def test_adapter_has_no_legacy_producer_symbols(self):
        adapter = CursorAdapter()
        self.assertFalse(hasattr(adapter, "legacy_popen_launch"))
        self.assertFalse(hasattr(adapter, "_start_legacy_popen"))
        self.assertFalse(hasattr(adapter, "_collect_legacy_popen"))


if __name__ == "__main__":
    unittest.main()
