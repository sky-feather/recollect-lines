import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from recollect_lines.models import InvalidTransition, TaskRequest, TaskState
from recollect_lines.service import Broker


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.broker = Broker(self.home)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, **kwargs):
        return self.broker.create(TaskRequest("Inspect tests", "/repo", **kwargs))

    def test_create_persists_queued_task_and_manifest(self):
        record = self.create()
        self.assertEqual(record.state, TaskState.QUEUED)
        payload = json.loads((self.home / "artifacts" / record.id / "request.json").read_text())
        manifest = self.broker.store.artifact_manifest(record.id)
        self.assertEqual(payload["task"], "Inspect tests")
        self.assertEqual([item["name"] for item in manifest["files"]], ["request.json"])
        self.assertEqual([event["type"] for event in self.broker.store.events(record.id)], ["task.created", "task.queued"])

    def test_complete_follows_lifecycle_validates_result_and_updates_manifest(self):
        record = self.create()
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "Found no failures")
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        names = [item["name"] for item in self.broker.store.artifact_manifest(record.id)["files"]]
        self.assertEqual(result["summary"], "Found no failures")
        self.assertEqual(result["state"], "succeeded")
        self.assertIn("request.json", names)
        self.assertIn("result.json", names)
        self.assertIn("normalized_result.json", names)

    def test_cancel_running_task_reaches_terminal_state(self):
        record = self.create()
        self.broker.start(record.id)
        cancelled = self.broker.cancel(record.id, "No longer needed")
        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        self.assertEqual(self.broker.store.events(record.id)[-1]["type"], "task.cancelled")

    def test_timeout_running_task_reaches_terminal_state(self):
        record = self.create()
        self.broker.start(record.id)
        timed_out = self.broker.timeout(record.id)
        self.assertEqual(timed_out.state, TaskState.TIMED_OUT)
        self.assertEqual(self.broker.store.events(record.id)[-1]["type"], "task.timed_out")

    def test_invalid_transition_is_rejected(self):
        record = self.create()
        with self.assertRaises(InvalidTransition):
            self.broker.complete(record.id, "too early")

    def test_profile_policy_rejects_invalid_requests(self):
        with self.assertRaisesRegex(ValueError, "Unknown runtime"):
            self.create(profile="missing")
        with self.assertRaisesRegex(ValueError, "does not permit"):
            self.create(execution_mode="shared_write")
        with self.assertRaisesRegex(ValueError, "maximum timeout"):
            self.create(timeout_seconds=3601)

    def test_profile_concurrency_limit_is_enforced(self):
        self.create()
        self.create()
        with self.assertRaisesRegex(ValueError, "concurrency limit"):
            self.create()

    def test_records_survive_service_reconstruction(self):
        record = self.create()
        self.broker.close()
        self.broker = Broker(self.home)
        restored = self.broker.status(record.id)
        self.assertEqual(restored["id"], record.id)
        self.assertEqual(restored["state"], "queued")

    def test_independent_brokers_can_create_tasks_against_wal_database(self):
        def create_one(index):
            broker = Broker(self.home)
            try:
                return broker.create(TaskRequest(f"Task {index}", "/repo", timeout_seconds=60)).id
            finally:
                broker.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(create_one, range(2)))
        self.assertEqual(len(set(ids)), 2)


if __name__ == "__main__":
    unittest.main()
