"""Wave 1 / PR 3: provider configuration lifecycle diagnostics.

providers.json is only read once, when the broker/MCP process constructs its
OpenAiCompatibleDirectRuntime. These tests prove the operator-facing
diagnostic (doctor finding + discover_capabilities' provider_config) reports
the true source/path and load time of *that* snapshot, that editing the file
on disk does not retroactively change an already-running process, that a
freshly started process picks up the edit, and that no credential value ever
leaks into the diagnostic.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recollect_lines.discovery import provider_config_lifecycle
from recollect_lines.doctor import run_doctor
from recollect_lines.providers import provider_config_format
from recollect_lines.service import Broker

SECRET = "sk-super-secret-value-must-not-appear"


def _write_providers(path: Path, *, model: str) -> None:
    path.write_text(json.dumps({
        "providers": {
            "local": {
                "kind": "openai-compatible",
                "base_url": "http://127.0.0.1:8765/v1",
                "api_key_env": "LOCAL_KEY",
                "default_model": model,
                "allow_insecure_http": True,
            }
        }
    }) + "\n")


class ProviderConfigLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.config = self.tmp / "providers.json"
        _write_providers(self.config, model="model-a")
        self.environ = {"LOCAL_KEY": SECRET}

    def test_not_configured_reports_truthful_label(self):
        broker = Broker(self.tmp / "home-none")
        try:
            lifecycle = provider_config_lifecycle(broker.direct_api_runtime)
            self.assertEqual(lifecycle["source"], "not_configured")
            self.assertEqual(lifecycle["source_origin"], "not_configured")
            self.assertIsNone(lifecycle["loaded_at"])
            self.assertTrue(lifecycle["restart_required_for_changes"])
            self.assertIn("restart", lifecycle["note"].lower())
        finally:
            broker.close()

    def test_diagnostic_reflects_actual_source_and_load_event(self):
        broker = Broker(self.tmp / "home-a", providers_config=self.config, environ=self.environ)
        try:
            lifecycle = broker.discover_capabilities()["provider_config"]
            self.assertEqual(lifecycle["source"], str(self.config))
            self.assertIsNotNone(lifecycle["loaded_at"])
            # Load event matches the object's own recorded timestamp — not a
            # freshly recomputed "now".
            self.assertEqual(lifecycle["loaded_at"], broker.direct_api_runtime.loaded_at.isoformat())
        finally:
            broker.close()

    def test_diagnostic_reports_default_origin_when_unspecified(self):
        # A Broker constructed the old way (bare providers_config, no origin
        # kwarg) is indistinguishable from an explicit --providers-config use.
        broker = Broker(self.tmp / "home-origin-default", providers_config=self.config, environ=self.environ)
        try:
            lifecycle = broker.discover_capabilities()["provider_config"]
            self.assertEqual(lifecycle["source_origin"], "explicit")
        finally:
            broker.close()

    def test_diagnostic_reports_resolved_precedence_origin(self):
        broker = Broker(
            self.tmp / "home-origin-repo-local",
            providers_config=self.config,
            providers_config_origin="repo_local",
            environ=self.environ,
        )
        try:
            lifecycle = broker.discover_capabilities()["provider_config"]
            self.assertEqual(lifecycle["source"], str(self.config))
            self.assertEqual(lifecycle["source_origin"], "repo_local")
        finally:
            broker.close()

    def test_doctor_surfaces_resolved_origin(self):
        report, _ = run_doctor(
            home=self.tmp / "home-doctor-origin",
            providers_config=self.config,
            providers_config_origin="user_level",
            environ=self.environ,
        )
        findings = {f["code"]: f for f in report["findings"]}
        self.assertEqual(findings["PROVIDER_CONFIG_LIFECYCLE"]["details"]["source_origin"], "user_level")

    def test_doctor_surfaces_the_same_lifecycle_finding(self):
        report, _ = run_doctor(home=self.tmp / "home-doctor", providers_config=self.config, environ=self.environ)
        findings = {f["code"]: f for f in report["findings"]}
        self.assertIn("PROVIDER_CONFIG_LIFECYCLE", findings)
        finding = findings["PROVIDER_CONFIG_LIFECYCLE"]
        self.assertEqual(finding["status"], "ok")
        self.assertEqual(finding["details"]["source"], str(self.config))
        self.assertIsNotNone(finding["details"]["loaded_at"])
        self.assertIn("restart", finding["remediation"].lower())

    def test_editing_file_after_startup_does_not_alter_running_snapshot(self):
        broker = Broker(self.tmp / "home-b", providers_config=self.config, environ=self.environ)
        try:
            before = broker.discover_capabilities()["provider_config"]
            self.assertEqual(broker.direct_api_runtime.get_provider("local").default_model, "model-a")

            _write_providers(self.config, model="model-b")

            after = broker.discover_capabilities()["provider_config"]
            self.assertEqual(after, before)
            self.assertEqual(broker.direct_api_runtime.get_provider("local").default_model, "model-a")
        finally:
            broker.close()

    def test_new_process_observes_the_updated_configuration(self):
        broker_old = Broker(self.tmp / "home-c", providers_config=self.config, environ=self.environ)
        try:
            old_loaded_at = broker_old.direct_api_runtime.loaded_at
        finally:
            broker_old.close()

        _write_providers(self.config, model="model-b")

        broker_new = Broker(self.tmp / "home-d", providers_config=self.config, environ=self.environ)
        try:
            self.assertEqual(broker_new.direct_api_runtime.get_provider("local").default_model, "model-b")
            self.assertGreaterEqual(broker_new.direct_api_runtime.loaded_at, old_loaded_at)
        finally:
            broker_new.close()

    def test_no_secret_value_in_discover_capabilities_or_doctor(self):
        broker = Broker(self.tmp / "home-e", providers_config=self.config, environ=self.environ)
        try:
            blob = json.dumps(broker.discover_capabilities())
            self.assertNotIn(SECRET, blob)
        finally:
            broker.close()

        report, _ = run_doctor(home=self.tmp / "home-f", providers_config=self.config, environ=self.environ)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_doctor_flags_legacy_json_format_non_blocking(self):
        report, exit_code = run_doctor(home=self.tmp / "home-legacy-json", providers_config=self.config, environ=self.environ)
        findings = {f["code"]: f for f in report["findings"]}
        self.assertIn("PROVIDERS_CONFIG_LEGACY_JSON_FORMAT", findings)
        legacy = findings["PROVIDERS_CONFIG_LEGACY_JSON_FORMAT"]
        self.assertEqual(legacy["severity"], "info")
        self.assertEqual(legacy["status"], "ok")
        # Legacy JSON is informational only; other doctor checks may still yield degraded.
        self.assertIn(report["status"], {"degraded", "ok"})
        self.assertEqual(exit_code, 0)

    def test_doctor_does_not_flag_yaml_as_legacy(self):
        yaml_config = self.tmp / "config.yaml"
        yaml_config.write_text(
            "providers:\n"
            "  local:\n"
            "    kind: openai-compatible\n"
            "    base_url: http://127.0.0.1:8765/v1\n"
            "    api_key_env: LOCAL_KEY\n"
            "    default_model: model-a\n"
            "    allow_insecure_http: true\n"
        )
        self.assertEqual(provider_config_format(yaml_config), "yaml")
        report, _ = run_doctor(home=self.tmp / "home-yaml", providers_config=yaml_config, environ=self.environ)
        findings = {f["code"]: f for f in report["findings"]}
        self.assertNotIn("PROVIDERS_CONFIG_LEGACY_JSON_FORMAT", findings)
        self.assertIn("PROVIDERS_CONFIG_VALID", findings)
        self.assertEqual(findings["PROVIDERS_CONFIG_VALID"]["details"]["path"], str(yaml_config.resolve()))


if __name__ == "__main__":
    unittest.main()
