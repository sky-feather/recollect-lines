"""Phase 7B: integration certification harness."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from recollect_lines import __version__, cli
from recollect_lines.certify import (
    CERTIFY_SCHEMA_VERSION,
    CERTIFICATION_PROMPT,
    CertifyRequest,
    format_human_report,
    run_certify,
)
from recollect_lines.direct_api_runtime import OpenAiCompatibleDirectRuntime
from recollect_lines.models import TaskRecord, TaskRequest
from recollect_lines.opencode_adapter import OpenCodeAdapter
from recollect_lines.providers import validate_providers_document

ROOT = Path(__file__).resolve().parent.parent
FAKE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"
EXAMPLES = ROOT / "examples"

import importlib.util

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "fake_openai_server.py"
_spec = importlib.util.spec_from_file_location("fake_openai_server", FIXTURE_SERVER)
assert _spec and _spec.loader
_fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake)
FakeOpenAiServer = _fake.FakeOpenAiServer
provider_document = _fake.provider_document


def fake_opencode_adapter() -> OpenCodeAdapter:
    return OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE)))


class CertifyCliTests(unittest.TestCase):
    def test_certify_subcommand_registered(self):
        args = cli.parser().parse_args([
            "--home", "/tmp/x",
            "certify", "--profile", "mock", "--json",
        ])
        self.assertEqual(args.command, "certify")
        self.assertEqual(args.profile, "mock")

    def test_help_lists_certify(self):
        result = subprocess.run(
            [sys.executable, "-m", "recollect_lines", "--help"],
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("certify", result.stdout)


class DryRunSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_default_dry_run_makes_no_remote_or_cli_invocation(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with mock.patch("subprocess.run", side_effect=AssertionError("cli")):
                report, exit_code = run_certify(CertifyRequest(
                    home=self.home,
                    profile="opencode",
                    provider=None,
                    providers_config=None,
                    output=None,
                    max_cost_usd=None,
                    execute_live=False,
                    acknowledge_billed_remote_calls=False,
                    fixture_execute=False,
                    opencode_adapter=fake_opencode_adapter(),
                ))
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["execution"]["outcome"], "dry_run")
        codes = {check["code"] for check in report["checks"]}
        self.assertIn("CLI_INVOCATION_SKIPPED", codes)
        self.assertIn("REMOTE_AVAILABILITY_NOT_CHECKED", codes)

    def test_dry_run_direct_api_skips_http(self):
        config_path = Path(self.tempdir.name) / "providers.json"
        config_path.write_text(json.dumps({
            "providers": {
                "local": {
                    "kind": "openai-compatible",
                    "base_url": "https://api.example.com/v1",
                    "api_key_env": "EXAMPLE_KEY",
                    "default_model": "m",
                }
            }
        }) + "\n")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            report, _ = run_certify(CertifyRequest(
                home=self.home,
                profile="openai_compatible",
                provider="local",
                providers_config=config_path,
                output=None,
                max_cost_usd=None,
                execute_live=False,
                acknowledge_billed_remote_calls=False,
                fixture_execute=False,
                environ={"EXAMPLE_KEY": "sk-placeholder-not-real"},
            ))
        self.assertEqual(report["execution"]["outcome"], "dry_run")
        codes = {check["code"] for check in report["checks"]}
        self.assertIn("REMOTE_REQUEST_SKIPPED", codes)


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.server = FakeOpenAiServer()
        self.server.start()
        self.env = {"TEST_OPENAI_API_KEY": "sk-fake-test-key-not-real"}
        self.config_path = Path(self.tempdir.name) / "providers.json"
        self.config_path.write_text(json.dumps(provider_document(
            self.server.base_url,
            estimated_cost_usd_upper_bound=0.01,
        )) + "\n")

    def tearDown(self):
        self.server.stop()
        self.tempdir.cleanup()

    def _base_request(self, **overrides) -> CertifyRequest:
        defaults = dict(
            home=self.home,
            profile="openai_compatible",
            provider="local",
            providers_config=self.config_path,
            output=None,
            max_cost_usd=None,
            execute_live=False,
            acknowledge_billed_remote_calls=False,
            fixture_execute=False,
            environ=self.env,
        )
        defaults.update(overrides)
        return CertifyRequest(**defaults)

    def test_missing_provider_blocks(self):
        report, exit_code = run_certify(self._base_request(provider=None))
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["execution"]["outcome"], "blocked")
        self.assertTrue(any(c["code"] == "PROVIDER_NOT_SELECTED" for c in report["checks"]))

    def test_live_without_ack_blocks(self):
        report, exit_code = run_certify(self._base_request(
            execute_live=True,
            max_cost_usd=0.05,
        ))
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["execution"]["outcome"], "blocked")
        self.assertTrue(any(c["code"] == "LIVE_ACKNOWLEDGEMENT_REQUIRED" for c in report["checks"]))

    def test_live_without_budget_blocks(self):
        report, exit_code = run_certify(self._base_request(
            execute_live=True,
            acknowledge_billed_remote_calls=True,
        ))
        self.assertEqual(exit_code, 1)
        self.assertTrue(any(c["code"] == "LIVE_BUDGET_REQUIRED" for c in report["checks"]))

    def test_live_zero_budget_blocks(self):
        report, exit_code = run_certify(self._base_request(
            execute_live=True,
            acknowledge_billed_remote_calls=True,
            max_cost_usd=0,
        ))
        self.assertEqual(exit_code, 1)
        self.assertTrue(any(c["code"] == "LIVE_BUDGET_REQUIRED" for c in report["checks"]))

    def test_live_missing_provider_cost_bound_blocks(self):
        path = Path(self.tempdir.name) / "no-cost.json"
        path.write_text(json.dumps(provider_document(self.server.base_url)) + "\n")
        report, exit_code = run_certify(self._base_request(
            providers_config=path,
            execute_live=True,
            acknowledge_billed_remote_calls=True,
            max_cost_usd=0.05,
        ))
        self.assertEqual(exit_code, 1)
        self.assertTrue(any(c["code"] == "PROVIDER_COST_BOUND_MISSING" for c in report["checks"]))

    def test_tls_policy_refusal_blocks_before_dispatch(self):
        bad_path = Path(self.tempdir.name) / "bad-tls.json"
        bad_path.write_text(json.dumps({
            "providers": {
                "remote_http": {
                    "kind": "openai-compatible",
                    "base_url": "http://203.0.113.1/v1",
                    "api_key_env": "TEST_OPENAI_API_KEY",
                    "default_model": "m",
                    "allow_insecure_http": True,
                    "estimated_cost_usd_upper_bound": 0.01,
                }
            }
        }) + "\n")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            report, exit_code = run_certify(self._base_request(
                provider="remote_http",
                providers_config=bad_path,
                fixture_execute=True,
            ))
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["execution"]["outcome"], "blocked")
        self.assertTrue(any(c["code"] == "PROVIDERS_CONFIG_INVALID" for c in report["checks"]))


class EvidenceArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.server = FakeOpenAiServer()
        self.server.start()
        self.env = {"TEST_OPENAI_API_KEY": "sk-fake-test-key-not-real"}
        self.config_path = Path(self.tempdir.name) / "providers.json"
        self.config_path.write_text(json.dumps(provider_document(
            self.server.base_url,
            estimated_cost_usd_upper_bound=0.01,
        )) + "\n")
        providers = validate_providers_document(json.loads(self.config_path.read_text()))
        self.runtime = OpenAiCompatibleDirectRuntime(providers, environ=self.env)

    def tearDown(self):
        self.server.stop()
        self.tempdir.cleanup()

    def test_redacted_stable_json_and_artifact(self):
        output = Path(self.tempdir.name) / "evidence.json"
        report, exit_code = run_certify(CertifyRequest(
            home=self.home,
            profile="openai_compatible",
            provider="local",
            providers_config=self.config_path,
            output=output,
            max_cost_usd=None,
            execute_live=False,
            acknowledge_billed_remote_calls=False,
            fixture_execute=False,
            environ=self.env,
        ))
        self.assertEqual(report["certification_schema_version"], CERTIFY_SCHEMA_VERSION)
        self.assertEqual(report["package"]["version"], __version__)
        self.assertTrue(output.exists())
        written = json.loads(output.read_text())
        self.assertEqual(written["execution"]["outcome"], "dry_run")
        blob = json.dumps(written)
        self.assertNotIn(self.env["TEST_OPENAI_API_KEY"], blob)
        self.assertNotIn("sk-fake-test-key", blob)

    def test_no_partial_artifact_on_blocked_validation(self):
        output = Path(self.tempdir.name) / "blocked.json"
        partial = output.with_name(output.name + f".{__import__('os').getpid()}.tmp")
        report, exit_code = run_certify(CertifyRequest(
            home=self.home,
            profile="openai_compatible",
            provider=None,
            providers_config=self.config_path,
            output=output,
            max_cost_usd=None,
            execute_live=False,
            acknowledge_billed_remote_calls=False,
            fixture_execute=False,
            environ=self.env,
        ))
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["execution"]["outcome"], "blocked")
        self.assertTrue(output.exists())
        self.assertFalse(partial.exists())

    def test_human_report_format(self):
        report, _ = run_certify(CertifyRequest(
            home=self.home,
            profile="mock",
            provider=None,
            providers_config=None,
            output=None,
            max_cost_usd=None,
            execute_live=False,
            acknowledge_billed_remote_calls=False,
            fixture_execute=False,
        ))
        text = format_human_report(report)
        self.assertIn("recollect-lines certify", text)
        self.assertIn("dry_run", text)


class FixtureExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.server = FakeOpenAiServer()
        self.server.start()
        self.env = {"TEST_OPENAI_API_KEY": "sk-fake-test-key-not-real"}
        self.config_path = Path(self.tempdir.name) / "providers.json"
        self.config_path.write_text(json.dumps(provider_document(
            self.server.base_url,
            estimated_cost_usd_upper_bound=0.01,
        )) + "\n")
        providers = validate_providers_document(json.loads(self.config_path.read_text()))
        self.runtime = OpenAiCompatibleDirectRuntime(providers, environ=self.env)

    def tearDown(self):
        self.server.stop()
        self.tempdir.cleanup()

    def test_fixture_direct_api_executed_is_local_proof(self):
        output = Path(self.tempdir.name) / "fixture-evidence.json"
        report, exit_code = run_certify(CertifyRequest(
            home=self.home,
            profile="openai_compatible",
            provider="local",
            providers_config=self.config_path,
            output=output,
            max_cost_usd=None,
            execute_live=False,
            acknowledge_billed_remote_calls=False,
            fixture_execute=True,
            environ=self.env,
            fixture_runtime=self.runtime,
        ))
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["execution"]["outcome"], "executed")
        self.assertEqual(report["execution"]["evidence_class"], "local_fixture")
        self.assertTrue(report["execution"]["declared_not_observed_remote_availability"])
        self.assertTrue(any(c["code"] == "FIXTURE_DIRECT_API_EXECUTED" for c in report["checks"]))
        written = json.loads(output.read_text())
        self.assertEqual(written["execution"]["evidence_class"], "local_fixture")

    def test_fixture_cli_executed(self):
        report, exit_code = run_certify(CertifyRequest(
            home=self.home,
            profile="opencode",
            provider=None,
            providers_config=None,
            output=None,
            max_cost_usd=None,
            execute_live=False,
            acknowledge_billed_remote_calls=False,
            fixture_execute=True,
            opencode_adapter=fake_opencode_adapter(),
        ))
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["execution"]["outcome"], "executed")
        self.assertTrue(any(c["code"] == "FIXTURE_CLI_EXECUTED" for c in report["checks"]))


class DirectApiTimeoutCancelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.server = FakeOpenAiServer()
        self.server.start()
        self.env = {"TEST_OPENAI_API_KEY": "sk-fake-test-key-not-real"}

    def tearDown(self):
        self.server.stop()
        self.tempdir.cleanup()

    def test_timeout_on_direct_api_path(self):
        doc = provider_document(self.server.base_url, request_timeout_seconds=1)
        providers = validate_providers_document(doc)
        runtime = OpenAiCompatibleDirectRuntime(providers, environ=self.env)
        config = providers["local"]
        record = TaskRecord.new(TaskRequest(
            "SLOW request",
            "/repo",
            profile="openai_compatible",
            provider="local",
            timeout_seconds=1,
        ))
        artifacts = Path(self.tempdir.name) / "artifacts"
        api_key = self.env["TEST_OPENAI_API_KEY"]
        _metadata, handle = runtime.start(record, artifacts)
        time.sleep(1.5)
        result = runtime.collect(handle, wait_timeout=0.1)
        self.assertNotEqual(result.get("exit_code"), 0)

    def test_cancel_on_direct_api_path(self):
        doc = provider_document(self.server.base_url, request_timeout_seconds=30)
        providers = validate_providers_document(doc)
        runtime = OpenAiCompatibleDirectRuntime(providers, environ=self.env)
        record = TaskRecord.new(TaskRequest(
            "SLOW cancel",
            "/repo",
            profile="openai_compatible",
            provider="local",
        ))
        artifacts = Path(self.tempdir.name) / "cancel-artifacts"
        _metadata, handle = runtime.start(record, artifacts)
        time.sleep(0.05)
        cancel_result = runtime.cancel(handle)
        collected = runtime.collect(handle, wait_timeout=5.0)
        self.assertIn("cancel_event", cancel_result["signals_sent"])
        self.assertIn(collected.get("error_category"), {None, "cancelled", "still_running", "runtime_error"})


class ExampleConfigCertifyTests(unittest.TestCase):
    def test_example_providers_dry_run(self):
        config = EXAMPLES / "litellm-openai-compatible" / "providers.json"
        if not config.exists():
            self.skipTest("example config missing")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "broker"
            report, exit_code = run_certify(CertifyRequest(
                home=home,
                profile="openai_compatible",
                provider="local_litellm",
                providers_config=config,
                output=Path(tmp) / "evidence.json",
                max_cost_usd=None,
                execute_live=False,
                acknowledge_billed_remote_calls=False,
                fixture_execute=False,
                environ={},
            ))
            self.assertIn(report["execution"]["outcome"], {"dry_run", "blocked"})
            self.assertEqual(report["certification_schema_version"], CERTIFY_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
