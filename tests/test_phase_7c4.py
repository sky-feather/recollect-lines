"""Phase 7C.4: bounded operator recovery/control surfaces (CLI + MCP)."""

from __future__ import annotations

import gc
import json
import os
import signal
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from recollect_lines import mcp_server
from recollect_lines.durable_reconciliation import is_durable_launch_row
from recollect_lines.durable_runner import STATE_RUNNING, inspect_durable_launch, load_launch_record
from recollect_lines.fixture_durable_adapter import FixtureDurableAdapter
from recollect_lines.models import DEFAULT_PROFILES, ProfilePolicy, TaskRequest, TaskState
from recollect_lines.opencode_adapter import OpenCodeAdapter
from recollect_lines.operator_control import OPERATOR_CONTROL_SCHEMA_VERSION, OperatorControlRefused
from recollect_lines.service import Broker

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_OPENCODE = FIXTURES / "fake_opencode.py"

FIXTURE_DURABLE_PROFILE = ProfilePolicy(
    "fixture_durable",
    frozenset({"read_only"}),
    3600,
    2,
)


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def kill_pgid(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_and_reap_popen(popen, pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    popen.wait(timeout=5)


def durable_broker(home: Path, **kwargs) -> Broker:
    adapter = FixtureDurableAdapter(home, max_stdout_bytes=4096, max_stderr_bytes=1024)
    profiles = {**DEFAULT_PROFILES, FIXTURE_DURABLE_PROFILE.name: FIXTURE_DURABLE_PROFILE}
    return Broker(home, profiles=profiles, fixture_durable_adapter=adapter, **kwargs)


def fake_opencode_broker(home: Path) -> Broker:
    return Broker(
        home,
        opencode_adapter=OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE))),
    )


def run_cli(home: Path, *args: str) -> tuple[int, dict]:
    import io
    from contextlib import redirect_stdout

    from recollect_lines import cli

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["--home", str(home), *args])
    stdout = buf.getvalue().strip()
    return code, json.loads(stdout) if stdout else {}


class Phase74OperatorControlTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = self.tempdir.name
        self._task_ids: list[str] = []
        self._supervisors: list = []
        self._orphaned_popens: list[tuple[object, int]] = []

    def tearDown(self):
        for broker_attr in ("broker", "broker_a", "broker_b"):
            broker = getattr(self, broker_attr, None)
            if broker is not None:
                for task_id in list(broker._adopted_durable_handles):
                    launch = broker.store.get_launch(task_id)
                    if launch and launch.get("pgid"):
                        kill_pgid(launch["pgid"])
                for task_id in self._task_ids:
                    launch = broker.store.get_launch(task_id)
                    if launch and launch.get("pgid"):
                        kill_pgid(launch["pgid"])
                broker.close()
        for supervisor in self._supervisors:
            if supervisor is not None and supervisor.poll() is None:
                try:
                    supervisor.wait(timeout=2)
                except Exception:
                    supervisor.kill()
                    supervisor.wait(timeout=2)
        for popen, pgid in self._orphaned_popens:
            kill_and_reap_popen(popen, pgid)
        self.tempdir.cleanup()
        gc.collect()

    def _start_durable(self, keyword: str = "DURABLE_HANG", *, broker: Broker | None = None) -> tuple[Broker, str]:
        broker = broker or durable_broker(self.home)
        record = broker.create(
            TaskRequest(keyword, self.workspace, execution_mode="read_only", profile="fixture_durable"),
        )
        broker.start(record.id)
        launch = broker.store.get_launch(record.id)
        self.assertTrue(is_durable_launch_row(launch))
        self._task_ids.append(record.id)
        return broker, record.id

    def _simulate_broker_loss(self, broker: Broker, task_id: str) -> None:
        handle = broker._process_handles.pop(task_id)
        if hasattr(handle, "durable") and handle.durable.supervisor is not None:
            self._supervisors.append(handle.durable.supervisor)
        popen = getattr(handle, "popen", None)
        if popen is not None:
            self._orphaned_popens.append((popen, handle.pgid))

    def _adopted_broker(self, keyword: str = "DURABLE_HANG") -> tuple[Broker, str]:
        broker_a, task_id = self._start_durable(keyword)
        self._simulate_broker_loss(broker_a, task_id)
        broker_a.close()
        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        broker_b.reconcile(task_id)
        return broker_b, task_id

    def test_adopted_fixture_status_cancel_collect_succeed_in_valid_state(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            broker_b, task_id = self._adopted_broker("DURABLE_HANG")
            status_out = broker_b.operator_control(task_id, "status")
            self.assertTrue(status_out["ok"])
            self.assertEqual(status_out["schema_version"], OPERATOR_CONTROL_SCHEMA_VERSION)
            self.assertEqual(status_out["recovery_posture"], "safely_adopted")
            self.assertIn("status", status_out["permitted_actions"])
            self.assertIn("cancel", status_out["permitted_actions"])
            self.assertNotIn("collect", status_out["permitted_actions"])
            self.assertNotIn("message", status_out["permitted_actions"])

            cancel_out = broker_b.operator_control(task_id, "cancel", reason="operator test cancel")
            self.assertTrue(cancel_out["ok"])
            self.assertEqual(cancel_out["result"]["state"], "cancelled")
            broker_b.close()

            broker_b2, task_id2 = self._adopted_broker("DURABLE_SHORT")
            self.assertTrue(
                wait_until(
                    lambda: inspect_durable_launch(
                        broker_b2.store.home,
                        task_id=task_id2,
                        launch_id=broker_b2.store.get_launch(task_id2)["durable_launch_id"],
                    ).outcome.value == "exited",
                    timeout=5,
                ),
            )
            broker_b2.reconcile(task_id2)
            view = broker_b2.operator_control_view(task_id2)
            self.assertIn("collect", view["permitted_actions"])
            collected = broker_b2.operator_control(task_id2, "collect")
            self.assertTrue(collected["ok"])
            self.assertIn(collected["result"]["state"], {"succeeded", "succeeded_with_warnings"})

    def test_legacy_opencode_recovery_required_refuses_collect(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            broker_a = fake_opencode_broker(self.home)
            record = broker_a.create(TaskRequest("SLEEP", self.workspace, profile="opencode"))
            broker_a.start(record.id)
            pgid = broker_a.store.get_launch(record.id)["pgid"]
            self._simulate_broker_loss(broker_a, record.id)
            broker_a.close()

            broker_b = fake_opencode_broker(self.home)
            self.broker_b = broker_b
            broker_b.reconcile(record.id)
            view = broker_b.operator_control_view(record.id)
            self.assertEqual(view["recovery_posture"], "recovery_required")
            self.assertIn("status", view["permitted_actions"])
            self.assertIn("cancel", view["permitted_actions"])
            self.assertNotIn("collect", view["permitted_actions"])
            with self.assertRaises(OperatorControlRefused) as ctx:
                broker_b.operator_control(record.id, "collect")
            self.assertEqual(ctx.exception.code, "refused_collect")
            kill_pgid(pgid)

    def test_corrupt_and_contested_evidence_refuses_control(self):
        broker_a, task_id = self._start_durable("DURABLE_HANG")
        launch_id = broker_a.store.get_launch(task_id)["durable_launch_id"]
        manifest_path = broker_a.store.home / "durable_launches" / launch_id / "manifest.json"
        self.assertTrue(wait_until(lambda: load_launch_record(manifest_path).lifecycle_state == STATE_RUNNING, timeout=5))
        tampered = json.loads(manifest_path.read_text())
        tampered["process"]["start_identity"] = "linux:boot=fake:starttime=0"
        manifest_path.write_text(json.dumps(tampered, indent=2) + "\n")
        self._simulate_broker_loss(broker_a, task_id)
        broker_a.close()

        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        broker_b.reconcile(task_id)
        view = broker_b.operator_control_view(task_id)
        self.assertEqual(view["recovery_posture"], "refused")
        self.assertNotIn("cancel", view["permitted_actions"])
        self.assertNotIn("collect", view["permitted_actions"])
        with self.assertRaises(OperatorControlRefused):
            broker_b.operator_control(task_id, "cancel")
        kill_pgid(tampered["process"]["pgid"])

    def test_unknown_task_and_unsafe_action_rejected(self):
        broker = durable_broker(self.home)
        self.broker = broker
        with self.assertRaises(KeyError):
            broker.operator_control_view("missing-task")
        with self.assertRaises(KeyError):
            broker.operator_control("missing-task", "status")
        with self.assertRaises(ValueError):
            broker.operator_control("missing-task", "steer")

    def test_nonterminal_collect_refused(self):
        broker_b, task_id = self._adopted_broker("DURABLE_HANG")
        with self.assertRaises(OperatorControlRefused) as ctx:
            broker_b.operator_control(task_id, "collect")
        self.assertEqual(ctx.exception.code, "refused_collect")
        kill_pgid(broker_b.store.get_launch(task_id)["pgid"])

    def test_message_explicit_refusal_no_side_effects(self):
        broker_b, task_id = self._adopted_broker("DURABLE_HANG")
        before = len(broker_b.store.events(task_id))
        out = broker_b.operator_control(task_id, "message", message_content="steer mid-task")
        self.assertFalse(out["ok"])
        self.assertTrue(out["refused"])
        self.assertEqual(out["code"], "unsupported_message_steering")
        self.assertEqual(out["result"]["status"], "unsupported")
        self.assertEqual(len(broker_b.store.events(task_id)), before)
        kill_pgid(broker_b.store.get_launch(task_id)["pgid"])

    def test_action_lists_never_overclaim_and_no_secret_leak(self):
        with mock.patch.dict(os.environ, {"RL_SECRET_SENTINEL": "rl_secret_sentinel_value"}, clear=False):
            broker_a, task_id = self._start_durable("DURABLE_SECRET")
            launch_id = broker_a.store.get_launch(task_id)["durable_launch_id"]
            self._simulate_broker_loss(broker_a, task_id)
            self.assertTrue(
                wait_until(
                    lambda: inspect_durable_launch(broker_a.store.home, task_id=task_id, launch_id=launch_id).outcome.value == "exited",
                    timeout=5,
                ),
            )
            broker_a.close()
            broker_b = durable_broker(self.home)
            self.broker_b = broker_b
            broker_b.reconcile(task_id)
            out = broker_b.operator_control(task_id, "collect")
            blob = json.dumps(out).encode()
            self.assertNotIn(b"rl_secret_sentinel_value", blob)
            self.assertNotIn(b"RL_SECRET_SENTINEL", blob)
            self.assertNotIn("message", out["permitted_actions"])

    def test_cli_control_surfaces_refusal_exit_code(self):
        broker_a, task_id = self._start_durable("DURABLE_HANG")
        pgid = broker_a.store.get_launch(task_id)["pgid"]
        self._simulate_broker_loss(broker_a, task_id)
        broker_a.close()
        code, payload = run_cli(self.home, "control", task_id, "--action", "collect")
        self.assertEqual(code, 3)
        self.assertEqual(payload["error"]["code"], "refused_collect")
        self.assertIn("control", payload)
        kill_pgid(pgid)

    def test_mcp_control_matches_contract(self):
        broker_b, task_id = self._adopted_broker("DURABLE_HANG")
        out = mcp_server.handle_control(broker_b, {"task_id": task_id, "action": "status"})
        self.assertEqual(out["recovery_posture"], "safely_adopted")
        self.assertIn("launch", out)
        self.assertIn("durable_launch_id", out["launch"])
        msg = mcp_server.handle_control(broker_b, {"task_id": task_id, "action": "message", "content": "hi"})
        self.assertFalse(msg["ok"])
        self.assertEqual(msg["code"], "unsupported_message_steering")
        kill_pgid(broker_b.store.get_launch(task_id)["pgid"])

    def test_competing_lease_refuses_cancel_until_expiry(self):
        broker_a, task_id = self._start_durable("DURABLE_HANG")
        launch_id = broker_a.store.get_launch(task_id)["durable_launch_id"]
        pgid = broker_a.store.get_launch(task_id)["pgid"]
        self._simulate_broker_loss(broker_a, task_id)
        broker_a.store.try_acquire_recovery_lease(
            task_id=task_id,
            durable_launch_id=launch_id,
            broker_id="broker-a",
            broker_epoch=1,
            ttl_seconds=60.0,
        )
        broker_b = durable_broker(self.home)
        self.broker_b = broker_b
        broker_b.reconcile(task_id)
        view = broker_b.operator_control_view(task_id)
        self.assertNotIn("cancel", view["permitted_actions"])
        broker_a.store.release_recovery_lease(task_id)
        broker_a.close()
        kill_pgid(pgid)


if __name__ == "__main__":
    unittest.main()
