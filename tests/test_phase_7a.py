"""Phase 7A: doctor, CLI rename, examples, clean-install acceptance."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines import __version__, cli
from recollect_lines.doctor import DOCTOR_SCHEMA_VERSION, format_human_report, run_doctor
from recollect_lines.opencode_adapter import OpenCodeAdapter

ROOT = Path(__file__).resolve().parent.parent
FAKE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"
EXAMPLES = ROOT / "examples"


def fake_opencode_adapter() -> OpenCodeAdapter:
    return OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE)))


class CliRenameTests(unittest.TestCase):
    def test_parser_prog_is_recollect_lines(self):
        self.assertEqual(cli.parser().prog, "recollect-lines")

    def test_doctor_subcommand_registered(self):
        args = cli.parser().parse_args(["--home", "/tmp/x", "doctor", "--json"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json)


class DoctorJsonTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_json_schema_stable_keys(self):
        report, _ = run_doctor(home=self.home, opencode_adapter=fake_opencode_adapter())
        self.assertEqual(report["doctor_schema_version"], DOCTOR_SCHEMA_VERSION)
        self.assertEqual(report["package"]["name"], "recollect-lines")
        self.assertEqual(report["package"]["version"], __version__)
        self.assertIn(report["status"], {"ok", "degraded", "blocking"})
        self.assertIn("findings", report)
        self.assertIn("summary", report)
        for finding in report["findings"]:
            for key in ("code", "severity", "status", "message"):
                self.assertIn(key, finding)

    def test_no_secret_values_in_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "providers.json"
            config.write_text(json.dumps({
                "providers": {
                    "local": {
                        "kind": "openai-compatible",
                        "base_url": "http://127.0.0.1:8765/v1",
                        "api_key_env": "LOCAL_KEY",
                        "default_model": "m",
                        "allow_insecure_http": True,
                    }
                }
            }) + "\n")
            environ = {"LOCAL_KEY": "sk-super-secret-value-must-not-appear"}
            report, _ = run_doctor(
                home=self.home,
                providers_config=config,
                environ=environ,
                opencode_adapter=fake_opencode_adapter(),
            )
            blob = json.dumps(report)
            self.assertNotIn("sk-super-secret", blob)
            self.assertNotIn("super-secret-value", blob)

    def test_blocking_invalid_providers_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"providers": {"bad-name": {}}}\n')
            report, exit_code = run_doctor(home=self.home, providers_config=bad)
            codes = {f["code"] for f in report["findings"]}
            self.assertIn("PROVIDERS_CONFIG_INVALID", codes)
            self.assertEqual(report["status"], "blocking")
            self.assertEqual(exit_code, 1)

    def test_missing_providers_config_file_is_blocking(self):
        missing = Path(self.tempdir.name) / "missing.json"
        report, exit_code = run_doctor(home=self.home, providers_config=missing)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("PROVIDERS_CONFIG_MISSING", codes)
        self.assertEqual(exit_code, 1)

    def test_missing_secret_reference_is_warning_not_blocking(self):
        config = EXAMPLES / "litellm-openai-compatible" / "providers.json"
        report, exit_code = run_doctor(
            home=self.home,
            providers_config=config,
            environ={},
            opencode_adapter=fake_opencode_adapter(),
        )
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("PROVIDER_SECRET_REFERENCE_MISSING", codes)
        self.assertIn(report["status"], {"degraded", "ok"})
        self.assertEqual(exit_code, 0)

    def test_inaccessible_workspace_is_blocking(self):
        missing = Path(self.tempdir.name) / "no-such-workspace"
        report, exit_code = run_doctor(home=self.home, workspace=missing)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("WORKSPACE_MISSING", codes)
        self.assertEqual(exit_code, 1)

    def test_endpoint_connectivity_explicitly_not_checked(self):
        report, _ = run_doctor(home=self.home, opencode_adapter=fake_opencode_adapter())
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("ENDPOINT_CONNECTIVITY_NOT_CHECKED", codes)

    def test_human_output_is_readable(self):
        report, _ = run_doctor(home=self.home, opencode_adapter=fake_opencode_adapter())
        text = format_human_report(report)
        self.assertIn("recollect-lines doctor", text)
        self.assertIn("PACKAGE_VERSION", text)

    def test_cli_doctor_json_exit_code(self):
        exit_code = cli.main([
            "--home", str(self.home),
            "doctor", "--json",
        ])
        self.assertIn(exit_code, (0, 1))


class DoctorPolicyTests(unittest.TestCase):
    def test_remote_http_without_tls_opt_in_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "providers.json"
            bad.write_text(json.dumps({
                "providers": {
                    "remote": {
                        "kind": "openai-compatible",
                        "base_url": "http://203.0.113.10/v1",
                        "api_key_env": "REMOTE_KEY",
                        "default_model": "m",
                        "allow_insecure_http": True,
                    }
                }
            }) + "\n")
            report, exit_code = run_doctor(home=Path(tmp) / "broker", providers_config=bad)
            self.assertEqual(exit_code, 1)
            self.assertTrue(any(f["code"] == "PROVIDERS_CONFIG_INVALID" for f in report["findings"]))


class ExampleConfigTests(unittest.TestCase):
    def test_litellm_example_validates_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = EXAMPLES / "litellm-openai-compatible" / "providers.json"
            report, exit_code = run_doctor(
                home=Path(tmp) / "broker",
                providers_config=config,
                environ={},
                opencode_adapter=fake_opencode_adapter(),
            )
            codes = {f["code"] for f in report["findings"]}
            self.assertIn("PROVIDERS_CONFIG_VALID", codes)
            self.assertIn("PROVIDER_SECRET_REFERENCE_MISSING", codes)
            self.assertEqual(exit_code, 0)

    def test_mixed_example_documents_placeholder_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = EXAMPLES / "mixed-cli-and-providers" / "providers.json"
            report, _ = run_doctor(
                home=Path(tmp) / "broker",
                providers_config=config,
                environ={},
            )
            codes = {f["code"] for f in report["findings"]}
            self.assertIn("PROVIDER_SECRET_REFERENCE_MISSING", codes)


if __name__ == "__main__":
    unittest.main()
