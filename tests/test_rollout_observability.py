import unittest
from types import SimpleNamespace

from recollect_lines.rollout_observability import durable_rollout_report


class FakeStore:
    def __init__(self, tasks, launches, events):
        self._tasks = tasks
        self._launches = launches
        self._events = events

    def list(self):
        return self._tasks

    def get_launch(self, task_id):
        return self._launches.get(task_id)

    def events(self, task_id):
        return self._events.get(task_id, [])


def event(kind, before, after, timestamp):
    return {
        "type": kind,
        "state_before": before,
        "state_after": after,
        "timestamp": timestamp,
        "metadata": {},
    }


class DurableRolloutObservabilityTests(unittest.TestCase):
    def test_reports_only_durable_tasks_and_persisted_event_latency(self):
        tasks = [
            SimpleNamespace(id="durable", profile="cursor", state="succeeded"),
            SimpleNamespace(id="legacy", profile="cursor", state="succeeded"),
        ]
        store = FakeStore(
            tasks,
            {
                "durable": {"launch_kind": "durable_subprocess", "adapter": "cursor"},
                "legacy": {"launch_kind": "legacy_subprocess", "adapter": "cursor"},
            },
            {
                "durable": [
                    event("task.running", "preparing", "running", "2026-07-25T00:00:00+00:00"),
                    event("task.durable_adopted", "running", "running", "2026-07-25T00:00:01+00:00"),
                    event("task.succeeded", "running", "succeeded", "2026-07-25T00:00:03.500+00:00"),
                ],
                "legacy": [event("task.succeeded", "running", "succeeded", "2026-07-25T00:00:01+00:00")],
            },
        )

        report = durable_rollout_report(store)

        self.assertEqual(report["scope"], "persisted_durable_subprocess_tasks")
        self.assertEqual(report["durable_task_count"], 1)
        self.assertEqual(report["terminal_event_latency_ms"], {"count": 1, "min": 3500, "max": 3500, "average": 3500})
        self.assertEqual(report["recovery_outcomes"], {"task.durable_adopted": 1})
        self.assertEqual(report["by_adapter"]["cursor"]["terminal_outcomes"], {"succeeded": 1})
        self.assertEqual(report["legacy_subprocess_inventory"]["task_count"], 1)
        self.assertEqual(report["legacy_subprocess_inventory"]["by_adapter"], {"cursor": {"succeeded": 1}})

    def test_legacy_inventory_groups_by_adapter_and_state(self):
        tasks = [
            SimpleNamespace(id="legacy-1", profile="cursor", state="recovery_required"),
            SimpleNamespace(id="legacy-2", profile="cursor", state="uncollected"),
            SimpleNamespace(id="legacy-3", profile="opencode", state="failed"),
            SimpleNamespace(id="durable-1", profile="cursor", state="succeeded"),
        ]
        store = FakeStore(
            tasks,
            {
                "legacy-1": {"launch_kind": "legacy_subprocess", "adapter": "cursor"},
                "legacy-2": {"launch_kind": "legacy_subprocess", "adapter": "cursor"},
                "legacy-3": {"launch_kind": "legacy_subprocess", "adapter": "opencode"},
                "durable-1": {"launch_kind": "durable_subprocess", "adapter": "cursor"},
            },
            {},
        )

        report = durable_rollout_report(store)

        self.assertEqual(report["legacy_subprocess_inventory"]["task_count"], 3)
        self.assertEqual(
            report["legacy_subprocess_inventory"]["by_adapter"],
            {
                "cursor": {"recovery_required": 1, "uncollected": 1},
                "opencode": {"failed": 1},
            },
        )
        self.assertEqual(report["durable_task_count"], 1)

    def test_unknown_or_malformed_timestamps_do_not_fabricate_latency(self):
        store = FakeStore(
            [SimpleNamespace(id="task", profile="codex")],
            {"task": {"launch_kind": "durable_subprocess", "adapter": "codex"}},
            {"task": [
                event("task.running", "preparing", "running", "not-a-time"),
                event("task.recovery_required", "running", "recovery_required", "2026-07-25T00:00:01+00:00"),
            ]},
        )

        report = durable_rollout_report(store)

        self.assertEqual(report["terminal_event_latency_ms"]["count"], 0)
        self.assertEqual(report["recovery_outcomes"], {"task.recovery_required": 1})
        self.assertEqual(report["by_adapter"]["codex"]["terminal_outcomes"], {})
        self.assertEqual(report["legacy_subprocess_inventory"], {"scope": "persisted_legacy_subprocess_tasks", "task_count": 0, "by_adapter": {}})


if __name__ == "__main__":
    unittest.main()
