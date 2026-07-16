import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from recollect_lines.adapters import AdapterCapabilities
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.opencode_adapter import DEFAULT_COMMAND_PREFIX, OpenCodeAdapter
from recollect_lines.service import Broker

FIXTURE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


def fake_adapter(grace_period_seconds=2.0):
    return OpenCodeAdapter(command_prefix=(sys.executable, str(FIXTURE)), grace_period_seconds=grace_period_seconds)


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class OpenCodeAdapterUnitTests(unittest.TestCase):
    def test_default_command_matches_the_known_opencode_invocation(self):
        adapter = OpenCodeAdapter()
        self.assertEqual(adapter.command_prefix, DEFAULT_COMMAND_PREFIX)
        command = adapter.build_command("/repo", "do the thing")
        self.assertEqual(
            command,
            ["npx", "--yes", "opencode-ai@1.17.18", "run", "--pure", "--format", "json", "--dir", "/repo", "do the thing"],
        )

    def test_injected_command_prefix_still_requests_json_format_and_dir(self):
        adapter = fake_adapter()
        command = adapter.build_command("/some/workspace", "inspect tests")
        self.assertIn("--format", command)
        self.assertIn("json", command[command.index("--format") + 1 :])
        self.assertIn("--dir", command)
        self.assertEqual(command[command.index("--dir") + 1], "/some/workspace")
        self.assertEqual(command[-1], "inspect tests")

    def test_mock_and_opencode_adapters_report_distinct_capabilities(self):
        from recollect_lines.service import MockAdapter

        self.assertIsInstance(MockAdapter.capabilities, AdapterCapabilities)
        self.assertIsInstance(OpenCodeAdapter.capabilities, AdapterCapabilities)
        self.assertFalse(MockAdapter.capabilities.requires_subprocess)
        self.assertTrue(OpenCodeAdapter.capabilities.requires_subprocess)
        self.assertFalse(OpenCodeAdapter.capabilities.reports_broker_verified_tests)


class OpenCodeBrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.broker = Broker(self.home, opencode_adapter=fake_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, task="Inspect tests", **kwargs):
        return self.broker.create(TaskRequest(task, "/repo", profile="opencode", **kwargs))

    def test_start_creates_events_and_stderr_artifacts_and_records_pid(self):
        record = self.create()
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        stderr_path = self.home / "artifacts" / record.id / "stderr.log"
        wait_until(lambda: events_path.exists() and events_path.stat().st_size > 0)
        self.assertTrue(events_path.exists())
        self.assertTrue(stderr_path.exists())
        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        self.assertIn("pid", run_event["metadata"])
        self.assertIn("pgid", run_event["metadata"])
        self.broker.collect(record.id)

    def test_collect_finds_last_text_event_as_summary_and_marks_runtime_reported(self):
        record = self.create()
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], "All checks passed for /repo")
        self.assertFalse(result["runtime"]["verification"]["tests_broker_verified"])
        self.assertEqual(result["runtime"]["verification"]["source"], "runtime_reported")
        manifest_names = [f["name"] for f in self.broker.store.artifact_manifest(record.id)["files"]]
        self.assertIn("events.jsonl", manifest_names)
        self.assertIn("stderr.log", manifest_names)

    def test_collect_reads_summary_from_the_real_cli_nested_part_shape(self):
        record = self.create(task="NESTED_PART")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], "nested summary for /repo")

    def test_collect_is_defensive_against_malformed_jsonl_lines(self):
        record = self.create(task="MALFORMED")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], "partial result despite malformed line")
        self.assertGreaterEqual(result["runtime"]["malformed_event_lines"], 1)

    def test_nonzero_exit_is_reported_as_failed_not_broker_verified(self):
        record = self.create(task="NONZERO_EXIT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["exit_code"], 1)
        self.assertFalse(result["runtime"]["verification"]["tests_broker_verified"])

    def test_collect_without_a_process_handle_confirms_dead_process_group_and_fails(self):
        record = self.create()
        self.broker.start(record.id)
        orphaned_handle = self.broker._process_handles.pop(record.id)  # simulate a broker restart losing the handle
        orphaned_handle.popen.wait(timeout=5)  # let the fixture's short-lived process actually exit first

        completed = self.broker.collect(record.id)

        self.assertEqual(completed.state, TaskState.FAILED)
        self.assertEqual(completed.state, self.broker.store.get(record.id).state)
        self.assertEqual(self.broker.store.events(record.id)[-1]["metadata"]["reason"], "process_group_confirmed_dead")

    def test_cancel_terminates_process_group_for_a_ready_long_running_fixture(self):
        record = self.create(task="SLEEP")
        self.broker.start(record.id)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        pgid = run_event["metadata"]["pgid"]

        cancelled = self.broker.cancel(record.id, "no longer needed")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        cancel_event = self.broker.store.events(record.id)[-1]
        self.assertEqual(cancel_event["metadata"]["cancellation"]["group_terminated"], True)
        self.assertIn("SIGTERM", cancel_event["metadata"]["cancellation"]["signals_sent"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)

    def test_cancel_escalates_to_sigkill_when_process_ignores_sigterm(self):
        self.broker = Broker(self.home, opencode_adapter=fake_adapter(grace_period_seconds=0.3))
        record = self.create(task="SLEEP_IGNORE_TERM")
        self.broker.start(record.id)
        events_path = self.home / "artifacts" / record.id / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))

        cancelled = self.broker.cancel(record.id, "force stop")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        cancel_event = self.broker.store.events(record.id)[-1]
        signals_sent = cancel_event["metadata"]["cancellation"]["signals_sent"]
        self.assertEqual(signals_sent, ["SIGTERM", "SIGKILL"])
        self.assertTrue(cancel_event["metadata"]["cancellation"]["group_terminated"])


if __name__ == "__main__":
    unittest.main()
