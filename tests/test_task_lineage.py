import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from recollect_lines.models import ProfilePolicy, TaskRequest, TaskState
from recollect_lines.service import Broker
from recollect_lines.store import TaskStore
from recollect_lines.task_lineage import LineagePolicy, resolve_lineage


class TaskLineageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.policy = LineagePolicy(max_active_agents=8, max_children_per_parent=2, max_delegation_depth=2)
        mock_policy = ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)
        self.broker = Broker(self.home, profiles={"mock": mock_policy}, lineage_policy=self.policy)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, task: str = "child work", workspace: str = "/repo", **kwargs):
        return self.broker.create(TaskRequest(task, workspace, **kwargs))

    def test_root_task_persists_through_restart(self):
        parent = self.create("parent")
        child = self.create("child", parent_task_id=parent.id)
        self.assertEqual(child.parent_task_id, parent.id)
        self.assertEqual(child.root_task_id, parent.id)
        self.assertEqual(child.delegation_depth, 1)
        self.assertEqual(child.relationship, "delegates")
        self.assertEqual(child.origin_kind, "host")
        self.broker.close()
        mock_policy = ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)
        reloaded = Broker(self.home, profiles={"mock": mock_policy}, lineage_policy=self.policy)
        restored = reloaded.store.get(child.id)
        self.assertEqual(restored.root_task_id, parent.id)
        self.assertEqual(restored.delegation_depth, 1)
        reloaded.close()

    def test_siblings_share_root_and_increment_depth(self):
        parent = self.create("parent")
        first = self.create("first", parent_task_id=parent.id)
        second = self.create("second", parent_task_id=parent.id)
        self.assertEqual(first.root_task_id, parent.id)
        self.assertEqual(second.root_task_id, parent.id)
        self.assertEqual(first.delegation_depth, 1)
        self.assertEqual(second.delegation_depth, 1)

    def test_external_root_without_parent(self):
        record = self.create("grouped", external_root_id="host-session-1")
        self.assertIsNone(record.parent_task_id)
        self.assertEqual(record.root_task_id, record.id)
        self.assertEqual(record.external_root_id, "host-session-1")
        self.assertEqual(record.delegation_depth, 0)
        self.assertEqual(record.origin_kind, "host")

    def test_invalid_parent_rejected_before_queue(self):
        with self.assertRaisesRegex(ValueError, "Unknown parent task"):
            self.create(parent_task_id="tsk_missing")

    def test_self_parent_rejected(self):
        parent = self.create("parent")
        with self.assertRaisesRegex(ValueError, "cannot equal"):
            resolve_id = parent.id
            self.broker._resolve_record_lineage(
                parent,
                TaskRequest("nope", "/repo", parent_task_id=resolve_id),
            )

    def test_depth_limit_rejected(self):
        root = self.create("root")
        child = self.create("child", parent_task_id=root.id)
        grandchild = self.create("grandchild", parent_task_id=child.id)
        with self.assertRaisesRegex(ValueError, "delegation_depth"):
            self.create("too deep", parent_task_id=grandchild.id)

    def test_child_limit_rejected(self):
        parent = self.create("parent")
        self.create("c1", parent_task_id=parent.id)
        self.create("c2", parent_task_id=parent.id)
        with self.assertRaisesRegex(ValueError, "maximum number of child tasks"):
            self.create("c3", parent_task_id=parent.id)

    def test_children_and_tree_queries_are_concise(self):
        parent = self.create("parent")
        child = self.create("child", parent_task_id=parent.id)
        children = self.broker.children(parent.id)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["task_id"], child.id)
        self.assertNotIn("events", children[0])
        tree = self.broker.task_tree(parent.id)
        self.assertEqual(tree["root_task_id"], parent.id)
        self.assertEqual(len(tree["tasks"]), 2)
        self.assertFalse(tree["truncated"])
        for node in tree["tasks"]:
            self.assertIn("task_id", node)
            self.assertNotIn("task", node)

    def test_agent_profile_resolution_preserves_lineage_fields(self):
        parent = self.create("parent")
        child = self.create(
            "child with profile",
            parent_task_id=parent.id,
            external_root_id="host-session-1",
            relationship="delegates",
            origin_kind="side_agent",
            agent_profile="repository-investigator",
            runtime="mock",
        )
        self.assertEqual(child.parent_task_id, parent.id)
        self.assertEqual(child.root_task_id, parent.id)
        self.assertEqual(child.external_root_id, "host-session-1")
        self.assertEqual(child.relationship, "delegates")
        self.assertEqual(child.agent_profile, "repository-investigator")

        parent = self.create("parent")
        follow_up = self.create("follow up", parent_task_id=parent.id, relationship="continues")
        self.assertEqual(follow_up.relationship, "continues")
        self.assertEqual(follow_up.parent_task_id, parent.id)
        self.assertNotEqual(follow_up.id, parent.id)
        self.assertEqual(follow_up.state, TaskState.QUEUED)

    def test_legacy_rows_backfill_root_on_migration(self):
        legacy_home = Path(self.tempdir.name) / "legacy-broker"
        db_path = legacy_home / "recollectlines.db"
        legacy_home.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                workspace TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                profile TEXT NOT NULL,
                provider TEXT,
                timeout_seconds INTEGER NOT NULL,
                verification_policy TEXT NOT NULL DEFAULT 'none',
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO tasks VALUES (
                'tsk_legacy', 'old task', '/repo', 'read_only', 'mock', NULL,
                1800, 'none', 'queued', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00'
            );
            """
        )
        connection.commit()
        connection.close()
        store = TaskStore(legacy_home)
        record = store.get("tsk_legacy")
        self.assertEqual(record.root_task_id, "tsk_legacy")
        self.assertEqual(record.delegation_depth, 0)
        store.close()

    def test_writer_lease_still_blocks_second_isolated_writer(self):
        from tests.test_workspace_safety import init_repo

        source = init_repo(Path(self.tempdir.name) / "source")
        first = self.broker.create(
            TaskRequest("first writer", str(source), execution_mode="isolated_worktree", profile="mock"),
        )
        second = self.broker.create(
            TaskRequest("second writer", str(source), execution_mode="isolated_worktree", profile="mock"),
        )
        started_first = self.broker.start(first.id)
        started_second = self.broker.start(second.id)
        self.assertEqual(started_first.state, TaskState.RUNNING)
        self.assertEqual(started_second.state, TaskState.FAILED)
        events = self.broker.store.events(second.id)
        self.assertTrue(any(event.get("metadata", {}).get("reason") == "workspace_lease_conflict" for event in events))

    def test_active_agent_limit_applies_before_launch(self):
        tight = Broker(
            self.home,
            profiles={"mock": ProfilePolicy("mock", frozenset({"read_only"}), 3600, 10)},
            lineage_policy=LineagePolicy(max_active_agents=1, max_children_per_parent=4, max_delegation_depth=3),
        )
        try:
            first = tight.create(TaskRequest("one", "/repo"))
            tight.start(first.id)
            with self.assertRaisesRegex(ValueError, "active-agent limit"):
                tight.create(TaskRequest("two", "/repo"))
        finally:
            tight.close()


class HostProvenanceDefaultTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.policy = LineagePolicy(max_active_agents=8, max_children_per_parent=4, max_delegation_depth=3)
        mock_policy = ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)
        self.broker = Broker(self.home, profiles={"mock": mock_policy}, lineage_policy=self.policy)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, task: str = "child work", workspace: str = "/repo", **kwargs):
        return self.broker.create(TaskRequest(task, workspace, **kwargs))

    def test_resolve_lineage_parented_without_origin_defaults_host(self):
        parent = self.create("parent")
        resolved = resolve_lineage(
            task_id="tsk_child",
            parent_task_id=parent.id,
            external_root_id=None,
            relationship=None,
            origin_kind=None,
            origin_ref=None,
            get_parent=self.broker.store.get,
            child_count=self.broker.store.child_count,
            active_agent_count=self.broker.store.total_active_count,
            policy=self.policy,
        )
        self.assertEqual(resolved.origin_kind, "host")

    def test_parented_task_defaults_host_not_side_agent(self):
        parent = self.create("parent", origin_kind="host")
        child = self.create("child", parent_task_id=parent.id)
        self.assertEqual(child.origin_kind, "host")

    def test_explicit_side_agent_persists_for_parented_task(self):
        parent = self.create("parent")
        child = self.create("child", parent_task_id=parent.id, origin_kind="side_agent")
        self.assertEqual(child.origin_kind, "side_agent")

    def test_parenthood_does_not_infer_side_agent_from_parent_origin(self):
        parent = self.create("parent", origin_kind="side_agent")
        child = self.create("child", parent_task_id=parent.id)
        self.assertEqual(parent.origin_kind, "side_agent")
        self.assertEqual(child.origin_kind, "host")

    def test_origin_kind_does_not_bypass_writer_lease(self):
        from tests.test_workspace_safety import init_repo

        source = init_repo(Path(self.tempdir.name) / "source")
        parent = self.create(
            "parent writer",
            workspace=str(source),
            execution_mode="isolated_worktree",
            profile="mock",
            origin_kind="side_agent",
        )
        child = self.create(
            "child writer",
            workspace=str(source),
            execution_mode="isolated_worktree",
            profile="mock",
            parent_task_id=parent.id,
            origin_kind="host",
        )
        started_parent = self.broker.start(parent.id)
        started_child = self.broker.start(child.id)
        self.assertEqual(started_parent.state, TaskState.RUNNING)
        self.assertEqual(started_child.state, TaskState.FAILED)
        events = self.broker.store.events(child.id)
        self.assertTrue(
            any(event.get("metadata", {}).get("reason") == "workspace_lease_conflict" for event in events)
        )

    def test_lineage_tree_and_limits_unchanged_with_host_parented_child(self):
        parent = self.create("parent")
        child = self.create("child", parent_task_id=parent.id, relationship="delegates")
        self.assertEqual(child.root_task_id, parent.id)
        self.assertEqual(child.delegation_depth, 1)
        self.assertEqual(child.relationship, "delegates")
        tree = self.broker.task_tree(parent.id)
        self.assertEqual(tree["root_task_id"], parent.id)
        self.assertEqual(len(tree["tasks"]), 2)
        self.assertFalse(tree["truncated"])


if __name__ == "__main__":
    unittest.main()
