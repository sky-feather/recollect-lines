"""MR 8.7: durable global completion-event cursor."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from recollect_lines.models import ProfilePolicy, TaskRequest, TaskState
from recollect_lines.service import Broker

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


class CompletionEventsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        mock_policy = ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)
        self.broker = Broker(self.home, profiles={"mock": mock_policy})

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, task: str = "work", **kwargs):
        return self.broker.create(TaskRequest(task, str(self.workspace), **kwargs))

    def complete_task(self, summary: str = "done"):
        record = self.create()
        self.broker.start(record.id)
        return self.broker.complete(record.id, summary)

    def test_empty_page_preserves_cursor_and_reports_high_water(self):
        page = self.broker.completion_events_since(0)
        self.assertEqual(page["events"], [])
        self.assertEqual(page["next_cursor"], 0)
        self.assertEqual(page["after_event_id"], 0)
        self.assertFalse(page["has_more"])
        self.assertGreaterEqual(page["high_water_mark"], 0)

    def test_terminal_completion_is_observed_in_order(self):
        first = self.complete_task("first")
        second = self.complete_task("second")
        page = self.broker.completion_events_since(0)
        states = [event["state"] for event in page["events"]]
        self.assertEqual(states, ["succeeded", "succeeded"])
        self.assertEqual(page["events"][0]["task_id"], first.id)
        self.assertEqual(page["events"][1]["task_id"], second.id)
        self.assertEqual(page["events"][0]["event_id"], page["events"][0]["event_id"])
        self.assertLess(page["events"][0]["event_id"], page["events"][1]["event_id"])

    def test_duplicate_poll_is_idempotent(self):
        self.complete_task()
        first = self.broker.completion_events_since(0)
        second = self.broker.completion_events_since(0)
        self.assertEqual(first, second)

    def test_advancing_cursor_does_not_re_emit_older_events(self):
        self.complete_task("one")
        self.complete_task("two")
        first_page = self.broker.completion_events_since(0, limit=1)
        self.assertEqual(len(first_page["events"]), 1)
        self.assertTrue(first_page["has_more"])
        second_page = self.broker.completion_events_since(first_page["next_cursor"])
        self.assertEqual(len(second_page["events"]), 1)
        self.assertNotEqual(first_page["events"][0]["task_id"], second_page["events"][0]["task_id"])

    def test_pagination_high_water_and_no_missed_terminal_event(self):
        completed = [self.complete_task(f"task-{index}") for index in range(5)]
        cursor = 0
        seen: list[str] = []
        while True:
            page = self.broker.completion_events_since(cursor, limit=2)
            seen.extend(event["task_id"] for event in page["events"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(seen, [record.id for record in completed])

    def test_concurrent_completions_remain_totally_ordered_by_event_id(self):
        def finish(index: int) -> str:
            local_home = self.home
            workspace = str(self.workspace)
            mock_policy = ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)
            broker = Broker(local_home, profiles={"mock": mock_policy})
            try:
                record = broker.create(TaskRequest(f"parallel-{index}", workspace))
                broker.start(record.id)
                broker.complete(record.id, f"summary-{index}")
                return record.id
            finally:
                broker.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            task_ids = list(pool.map(finish, range(6)))
        page = self.broker.completion_events_since(0, limit=64)
        observed = [event["task_id"] for event in page["events"] if event["task_id"] in task_ids]
        event_ids = [event["event_id"] for event in page["events"] if event["task_id"] in task_ids]
        self.assertEqual(len(observed), len(task_ids))
        self.assertEqual(event_ids, sorted(event_ids))
        self.assertEqual(set(observed), set(task_ids))

    def test_cancelled_timed_out_and_recovery_states_map_honestly(self):
        running = self.create("running")
        self.broker.start(running.id)
        cancelled = self.broker.cancel(running.id, "stop")
        self.assertEqual(cancelled.state, TaskState.CANCELLED)

        sleeper = self.create("sleep")
        self.broker.start(sleeper.id)
        timed_out = self.broker.timeout(sleeper.id)
        self.assertEqual(timed_out.state, TaskState.TIMED_OUT)

        recovery = self.create("recovery")
        self.broker.start(recovery.id)
        self.broker.store.transition(
            recovery.id,
            TaskState.RECOVERY_REQUIRED,
            "needs operator reconciliation",
            {"reason": "test"},
        )

        page = self.broker.completion_events_since(0)
        by_state = {event["state"]: event for event in page["events"]}
        self.assertEqual(by_state["cancelled"]["event_type"], "task.cancelled")
        self.assertEqual(by_state["timed_out"]["event_type"], "task.timed_out")
        self.assertEqual(by_state["recovery_required"]["event_type"], "task.recovery_required")

    def test_restart_persistence_allows_cursor_resume(self):
        completed = self.complete_task("survives restart")
        event_id = self.broker.store.events(completed.id)[-1]["id"]
        self.broker.close()
        reloaded = Broker(self.home, profiles={"mock": ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)})
        try:
            page = reloaded.completion_events_since(event_id - 1, limit=10)
            self.assertEqual(len(page["events"]), 1)
            self.assertEqual(page["events"][0]["task_id"], completed.id)
            self.assertEqual(page["events"][0]["state"], "succeeded")
        finally:
            reloaded.close()

    def test_root_task_filter_and_lineage_fields(self):
        parent = self.create("parent")
        child = self.create("child", parent_task_id=parent.id, external_root_id="host-session-1")
        self.broker.start(child.id)
        self.broker.complete(child.id, "child done")
        page = self.broker.completion_events_since(0, root_task_id=parent.id)
        self.assertEqual(len(page["events"]), 1)
        event = page["events"][0]
        self.assertEqual(event["task_id"], child.id)
        self.assertEqual(event["root_task_id"], parent.id)
        self.assertEqual(event["parent_task_id"], parent.id)
        self.assertEqual(event["external_root_id"], "host-session-1")

    def test_task_filter_limits_to_one_task(self):
        first = self.complete_task("one")
        self.complete_task("two")
        page = self.broker.completion_events_since(0, task_id=first.id)
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["events"][0]["task_id"], first.id)

    def test_payload_is_compact_without_raw_output_leakage(self):
        completed = self.complete_task("compact summary only")
        page = self.broker.completion_events_since(0)
        event = page["events"][-1]
        encoded = json.dumps(event)
        self.assertNotIn("malformed_event_lines", encoded)
        self.assertNotIn("events.jsonl", encoded)
        self.assertIn("result_summary", event)
        self.assertEqual(event["result_summary"]["summary"], "compact summary only")
        self.assertIn("artifact_count", event)
        self.assertGreaterEqual(event["artifact_count"], 2)

    def test_verification_gate_label_surfaces_on_terminal_collect(self):
        record = self.broker.create(
            TaskRequest("verified", str(self.workspace), verification_policy="required"),
            verify_commands=[[sys.executable, "-c", "print('ok')"]],
        )
        self.broker.start(record.id)
        self.broker.complete(record.id, "verified summary")
        page = self.broker.completion_events_since(0, task_id=record.id)
        gate = page["events"][0]["metadata"]["verification_gate"]
        self.assertEqual(gate["policy"], "required")
        self.assertEqual(gate["label"], "required_verified")

    def test_cli_completion_events_shape(self):
        self.complete_task("cli")
        proc = subprocess.run(
            [sys.executable, "-m", "recollect_lines", "--home", str(self.home), "completion-events", "--limit", "1"],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(SRC_DIR)},
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("next_cursor", payload)
        self.assertIn("high_water_mark", payload)
        self.assertEqual(len(payload["events"]), 1)

    def test_mcp_completion_events_tool(self):
        from tests.test_mcp_server import McpStdioClient

        client = McpStdioClient(self.home)
        try:
            client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
            client.notify("notifications/initialized")
            listed = client.request("tools/list")
            names = {tool["name"] for tool in listed["result"]["tools"]}
            self.assertIn("completion_events", names)
            self.complete_task("mcp")
            response = client.call_tool("completion_events", {"after_event_id": 0, "limit": 5})
            body = json.loads(response["result"]["content"][0]["text"])
            self.assertTrue(body["ok"])
            data = body["data"]
            self.assertIn("events", data)
            self.assertTrue(any(event["state"] == "succeeded" for event in data["events"]))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
