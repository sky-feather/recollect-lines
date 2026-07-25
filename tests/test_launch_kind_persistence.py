"""RFC-004 sunset-preparation slice: no implicit legacy_subprocess writes.

TaskStore.record_launch() used to default an omitted launch_kind to
legacy_subprocess, so any caller that forgot the argument silently created a
new "legacy" record. This module locks down the fix: launch_kind is a
required, validated argument at the store boundary, and production's generic
adapter dispatch (Broker.start()) fails closed rather than falling back if an
adapter's start() metadata omits it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recollect_lines.adaptor.cursor import CursorAdapter
from recollect_lines.durable_reconciliation import LAUNCH_KIND_DIRECT_API, LAUNCH_KIND_DURABLE, LAUNCH_KIND_LEGACY
from recollect_lines.models import TaskRequest
from recollect_lines.service import Broker


class _AdapterMissingLaunchKind(CursorAdapter):
    """A broken adapter that forgets launch_kind -- must never persist silently."""

    def start(self, record, artifacts_dir, workspace=None, *, prompt=None):
        class _Handle:
            pid = 4242
            pgid = 4242

        return {
            "adapter": self.name,
            "command": ["echo", "hi"],
            "workspace": workspace or record.workspace,
            "events_artifact": None,
            "stderr_artifact": None,
        }, _Handle()


class RecordLaunchExplicitKindTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.broker = Broker(self.home)
        self.record = self.broker.create(TaskRequest("inspect", "/repo", profile="mock"))

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _record_launch(self, **overrides):
        kwargs = dict(
            adapter="fake",
            adapter_label="Fake",
            pid=None,
            pgid=None,
            command=["fake"],
            workspace="/repo",
            events_artifact=None,
            stderr_artifact=None,
        )
        kwargs.update(overrides)
        self.broker.store.record_launch(self.record.id, **kwargs)

    def test_launch_kind_has_no_default(self):
        with self.assertRaises(TypeError):
            self.broker.store.record_launch(
                self.record.id,
                adapter="fake",
                adapter_label="Fake",
                pid=None,
                pgid=None,
                command=["fake"],
                workspace="/repo",
                events_artifact=None,
                stderr_artifact=None,
            )

    def test_unknown_launch_kind_rejected(self):
        with self.assertRaises(ValueError):
            self._record_launch(launch_kind="mystery_kind")

    def test_durable_subprocess_without_durable_launch_id_rejected(self):
        with self.assertRaises(ValueError):
            self._record_launch(launch_kind=LAUNCH_KIND_DURABLE, durable_launch_id=None)

    def test_durable_subprocess_with_durable_launch_id_persists(self):
        self._record_launch(launch_kind=LAUNCH_KIND_DURABLE, durable_launch_id="abc123")
        launch = self.broker.store.get_launch(self.record.id)
        self.assertEqual(launch["launch_kind"], LAUNCH_KIND_DURABLE)
        self.assertEqual(launch["durable_launch_id"], "abc123")

    def test_legacy_subprocess_remains_explicitly_persistable_for_compat_tests(self):
        self._record_launch(launch_kind=LAUNCH_KIND_LEGACY)
        launch = self.broker.store.get_launch(self.record.id)
        self.assertEqual(launch["launch_kind"], LAUNCH_KIND_LEGACY)

    def test_direct_api_kind_persistable_without_durable_id(self):
        self._record_launch(launch_kind=LAUNCH_KIND_DIRECT_API)
        launch = self.broker.store.get_launch(self.record.id)
        self.assertEqual(launch["launch_kind"], LAUNCH_KIND_DIRECT_API)


class ProductionDispatchFailsClosedTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, cursor_adapter=_AdapterMissingLaunchKind())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_adapter_forgetting_launch_kind_raises_instead_of_defaulting_to_legacy(self):
        record = self.broker.create(
            TaskRequest("inspect", str(self.workspace), profile="cursor", execution_mode="read_only")
        )
        with self.assertRaises(RuntimeError):
            self.broker.start(record.id)
        # Fail closed means nothing was ever persisted as legacy_subprocess by mistake.
        self.assertIsNone(self.broker.store.get_launch(record.id))


if __name__ == "__main__":
    unittest.main()
