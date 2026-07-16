import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from recollect_lines.adapters import AdapterCapabilities
from recollect_lines.cursor_adapter import (
    DEFAULT_COMMAND_PREFIX,
    CursorAdapter,
    CursorUnsupportedPolicy,
    redact_secrets,
)
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

    def test_start_creates_stdout_and_stderr_artifacts_and_records_pid_pgid(self):
        record = self.create()
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        stdout_path = self.home / "artifacts" / record.id / "stdout.log"
        stderr_path = self.home / "artifacts" / record.id / "stderr.log"
        wait_until(lambda: stdout_path.exists() and stdout_path.stat().st_size > 0)
        self.assertTrue(stderr_path.exists())
        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        self.assertIn("pid", run_event["metadata"])
        self.assertIn("pgid", run_event["metadata"])
        self.assertEqual(run_event["metadata"]["runtime_description"], "Cursor Agent CLI via cursor-agent --print")
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["adapter"], "cursor")
        self.broker.collect(record.id)

    def test_cancel_terminates_process_group_read_only(self):
        record = self.broker.create(TaskRequest("SLEEP", str(self.workspace), profile="cursor", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles[record.id]
        wait_until(lambda: handle.stderr_path.exists() and b"started" in handle.stderr_path.read_bytes())

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
        wait_until(lambda: handle.stderr_path.exists() and b"started" in handle.stderr_path.read_bytes())

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

    def test_collect_without_a_process_handle_confirms_dead_process_group_and_fails(self):
        record = self.create()
        self.broker.start(record.id)
        orphaned_handle = self.broker._process_handles.pop(record.id)
        orphaned_handle.popen.wait(timeout=5)

        completed = self.broker.collect(record.id)

        self.assertEqual(completed.state, TaskState.FAILED)
        self.assertEqual(self.broker.store.events(record.id)[-1]["metadata"]["reason"], "process_group_confirmed_dead")


class CursorUnsupportedPolicyBrokerTests(unittest.TestCase):
    def test_broker_start_propagates_unsupported_policy_rather_than_launching(self):
        adapter = fake_adapter()
        with self.assertRaises(CursorUnsupportedPolicy):
            adapter.build_command("do something", "shared_write", "/tmp/ws")


if __name__ == "__main__":
    unittest.main()
