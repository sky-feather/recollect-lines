"""Phase 7C.1: recovery/control contract and compatibility evidence."""

from __future__ import annotations

import gc
import json
import os
import signal
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

from recollect_lines.discovery import discover_runtimes
from recollect_lines.doctor import run_doctor
from recollect_lines.mcp_server import handle_discover_capabilities, handle_message
from recollect_lines.opencode_adapter import OpenCodeAdapter
from recollect_lines.recovery_contract import (
    COMPATIBILITY_EVIDENCE_SCHEMA_VERSION,
    ControlAction,
    RecoveryControlContract,
    RecoveryLevel,
    SUBPROCESS_CLI_RECOVERY_CONTROL,
    SYNTHETIC_RECOVERY_CONTROL,
    DIRECT_API_RECOVERY_CONTROL,
    UNPROVEN_IN_FLIGHT_MESSAGE,
    UNPROVEN_PROVIDER_NATIVE_RESUME,
    build_compatibility_evidence,
    help_keyword_hits,
    offline_probe_conclusions,
    parse_control_action,
    parse_recovery_level,
    probe_version_help_only,
    recovery_control_from_mapping,
)
from recollect_lines.service import Broker

ROOT = Path(__file__).resolve().parent.parent
FAKE_COMPAT = Path(__file__).parent / "fixtures" / "fake_compat_cli.py"
FAKE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"
HANG_ON_VERSION = Path(__file__).parent / "fixtures" / "hang_on_version.py"
HANG_ON_HELP = Path(__file__).parent / "fixtures" / "hang_on_help.py"
FIXTURE_EVIDENCE = Path(__file__).parent / "fixtures" / "phase_7c1_compat_evidence.json"


def kill_launch_pgid(broker: Broker, task_id: str) -> None:
    launch = broker.store.get_launch(task_id)
    if not launch:
        return
    try:
        os.killpg(launch["pgid"], signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(launch["pid"], 0)
    except ChildProcessError:
        pass


def kill_and_reap_popen(popen, pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    popen.wait(timeout=5)


def fake_opencode_adapter() -> OpenCodeAdapter:
    return OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE)))


def fake_compat_adapter() -> OpenCodeAdapter:
    return OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_COMPAT)))


class RecoveryContractSchemaTests(unittest.TestCase):
    def test_enum_validation_fail_closed(self):
        with self.assertRaises(ValueError):
            parse_recovery_level("session_resume_proven")
        with self.assertRaises(ValueError):
            parse_control_action("steer")

    def test_contract_serializes_deterministically(self):
        payload = SUBPROCESS_CLI_RECOVERY_CONTROL.to_dict()
        self.assertEqual(payload["recovery_level"], "observe_and_cancel")
        self.assertEqual(payload["supported_control_actions"], ["cancel", "collect", "status"])
        self.assertEqual(payload["unsupported_control_actions"], ["message"])
        again = recovery_control_from_mapping(payload)
        self.assertEqual(again.to_dict(), payload)

    def test_invalid_mapping_rejected(self):
        with self.assertRaises(ValueError):
            recovery_control_from_mapping({"recovery_level": "none", "supported_control_actions": "status"})


class DeclaredRuntimeContractTests(unittest.TestCase):
    def test_subprocess_contract_honest(self):
        declared = SUBPROCESS_CLI_RECOVERY_CONTROL
        self.assertEqual(declared.recovery_level, RecoveryLevel.OBSERVE_AND_CANCEL)
        self.assertIn(ControlAction.STATUS, declared.supported_control_actions)
        self.assertIn(ControlAction.CANCEL, declared.supported_control_actions)
        self.assertIn(ControlAction.COLLECT, declared.supported_control_actions)
        self.assertNotIn(ControlAction.MESSAGE, declared.supported_control_actions)

    def test_mock_and_direct_api_declared_none(self):
        self.assertEqual(SYNTHETIC_RECOVERY_CONTROL.recovery_level, RecoveryLevel.NONE)
        self.assertEqual(DIRECT_API_RECOVERY_CONTROL.recovery_level, RecoveryLevel.NONE)

    def test_each_registered_runtime_exposes_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(Path(tmp) / "home", opencode_adapter=fake_compat_adapter())
            try:
                inventory = {entry["name"]: entry for entry in discover_runtimes(
                    profiles=broker.profiles,
                    subprocess_adapters=broker.subprocess_adapters,
                    mock_adapter=broker.adapter,
                    direct_api_runtime=broker.direct_api_runtime,
                    include_compatibility_evidence=False,
                )}
                expected_levels = {
                    "mock": "none",
                    "opencode": "observe_and_cancel",
                    "claude_code": "observe_and_cancel",
                    "codex": "observe_and_cancel",
                    "cursor": "observe_and_cancel",
                    "openai_compatible": "none",
                }
                for name, level in expected_levels.items():
                    declared = inventory[name]["recovery_control"]["declared"]
                    self.assertEqual(declared["recovery_level"], level)
                    self.assertEqual(declared["unsupported_control_actions"], ["message"])
            finally:
                broker.close()


class CompatibilityEvidenceTests(unittest.TestCase):
    def test_help_keywords_do_not_elevate_conclusions(self):
        hits = help_keyword_hits("resume session continue from checkpoint")
        conclusions = offline_probe_conclusions(help_keyword_hits_found=hits)
        self.assertEqual(conclusions["provider_native_session_resume"], UNPROVEN_PROVIDER_NATIVE_RESUME)
        self.assertEqual(conclusions["in_flight_message_control"], UNPROVEN_IN_FLIGHT_MESSAGE)
        self.assertIn("help_keyword_note", conclusions)

    def test_probe_records_fingerprints_not_full_help(self):
        probe = probe_version_help_only((sys.executable, str(FAKE_COMPAT)))
        self.assertTrue(probe["executable_available"])
        self.assertIn("resume", probe["help_keyword_hits"])
        self.assertIsNotNone(probe["help_fingerprint"])
        self.assertNotIn("Resume an existing session", json.dumps(probe))

    def test_evidence_fixture_round_trip(self):
        adapter = fake_compat_adapter()
        probe = probe_version_help_only(adapter.command_prefix)
        evidence = build_compatibility_evidence(
            adapter_id=adapter.name,
            runtime_kind="subprocess_cli",
            contract=adapter.capabilities.recovery_control,
            probe=probe,
            observed_at="2026-07-14T00:00:00+00:00",
        )
        payload = evidence.to_dict()
        self.assertEqual(payload["schema_version"], COMPATIBILITY_EVIDENCE_SCHEMA_VERSION)
        self.assertEqual(payload["probe_type"], "version_help_only")
        self.assertEqual(payload["conclusions"]["provider_native_session_resume"], "unproven")
        fixture = json.loads(FIXTURE_EVIDENCE.read_text())
        for key in (
            "schema_version", "probe_type", "declared_recovery_level",
            "unsupported_control_actions",
        ):
            self.assertEqual(payload[key], fixture[key])
        for key in ("in_flight_message_control", "provider_native_session_resume"):
            self.assertEqual(payload["conclusions"][key], fixture["conclusions"][key])

    def test_missing_executable_distinct_from_declared_capability(self):
        probe = probe_version_help_only(("definitely-missing-binary-7c1",))
        evidence = build_compatibility_evidence(
            adapter_id="opencode",
            runtime_kind="subprocess_cli",
            contract=SUBPROCESS_CLI_RECOVERY_CONTROL,
            probe=probe,
            observed_at="2026-07-14T00:00:00+00:00",
        )
        self.assertFalse(evidence.executable_available)
        self.assertEqual(evidence.declared_recovery_level, "observe_and_cancel")
        self.assertIn("Install the runtime CLI", evidence.remediation[0])

    def test_probe_version_timeout_reaps_child(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            probe = probe_version_help_only((sys.executable, str(HANG_ON_VERSION)), timeout=0.3)
        self.assertFalse(probe["executable_available"])
        self.assertEqual(probe["reason"], "version_check_timed_out")
        gc.collect()

    def test_probe_help_timeout_reaps_child(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            probe = probe_version_help_only((sys.executable, str(HANG_ON_HELP)), timeout=0.3)
        self.assertTrue(probe["executable_available"])
        self.assertEqual(probe["reason"], "version_help_only")
        self.assertIsNone(probe["help_fingerprint"])
        gc.collect()

    def test_redaction_no_path_or_secret_leakage(self):
        probe = {
            "executable_available": True,
            "version_fingerprint": "sk-secret123 at /home/user/bin/cli",
            "help_keyword_hits": ["resume"],
            "help_fingerprint": "abc123",
        }
        evidence = build_compatibility_evidence(
            adapter_id="claude_code",
            runtime_kind="subprocess_cli",
            contract=SUBPROCESS_CLI_RECOVERY_CONTROL,
            probe=probe,
            observed_at="2026-07-14T00:00:00+00:00",
        )
        blob = json.dumps(evidence.to_dict())
        self.assertNotIn("/home/user", blob)
        self.assertNotIn("sk-secret123", blob)
        self.assertIn("sk-<redacted>", blob)


class DiscoveryDoctorMcpVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.broker = Broker(self.home, opencode_adapter=fake_compat_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_discovery_surfaces_recovery_control_layers(self):
        inventory = {entry["name"]: entry for entry in discover_runtimes(
            profiles=self.broker.profiles,
            subprocess_adapters=self.broker.subprocess_adapters,
            mock_adapter=self.broker.adapter,
            direct_api_runtime=self.broker.direct_api_runtime,
        )}
        recovery = inventory["opencode"]["recovery_control"]
        self.assertIn("declared", recovery)
        self.assertEqual(recovery["provider_native_session_resume"], "unproven")
        self.assertIn("compatibility_evidence", recovery)

    def test_doctor_reports_recovery_inventory(self):
        report, _ = run_doctor(home=self.home, opencode_adapter=fake_compat_adapter())
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("RECOVERY_CONTRACT_INVENTORY", codes)

    def test_mcp_discover_capabilities_includes_schema_version(self):
        payload = handle_discover_capabilities(self.broker, {})
        self.assertEqual(payload["recovery_contract_schema_version"], "1")
        opencode = next(item for item in payload["runtimes"] if item["name"] == "opencode")
        self.assertIn("recovery_control", opencode)

    def test_message_remains_fail_closed(self):
        from recollect_lines.models import TaskRequest

        record = self.broker.create(TaskRequest(task="noop", workspace=str(self.home), profile="mock"))
        self.broker.start(record.id)
        before_events = len(self.broker.store.events(record.id))
        result = handle_message(self.broker, {"task_id": record.id, "content": "steer"})
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(len(self.broker.store.events(record.id)), before_events)

    def test_broker_restart_contract_unchanged(self):
        from recollect_lines.models import TaskRequest, TaskState

        broker = Broker(self.home, opencode_adapter=fake_opencode_adapter())
        record_id = None
        sleep_handle = None
        try:
            record = broker.create(TaskRequest(
                task="SLEEP", workspace=str(self.home), profile="opencode", execution_mode="read_only",
            ))
            record_id = record.id
            broker.start(record.id)
            sleep_handle = broker._process_handles.pop(record.id, None)
            home = self.home
            broker.close()
            broker2 = Broker(home, opencode_adapter=fake_opencode_adapter())
            try:
                reconciled = broker2.reconcile(record.id)
                self.assertIn(reconciled.state, {TaskState.RECOVERY_REQUIRED, TaskState.FAILED, TaskState.CANCELLED})
                self.assertNotEqual(reconciled.state, TaskState.SUCCEEDED)
            finally:
                if record_id is not None:
                    kill_launch_pgid(broker2, record_id)
                broker2.close()
        finally:
            if sleep_handle is not None:
                kill_and_reap_popen(sleep_handle.popen, sleep_handle.pgid)

if __name__ == "__main__":
    unittest.main()
