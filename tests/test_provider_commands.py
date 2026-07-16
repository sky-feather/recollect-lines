"""Wave 3 / PR 7: `provider list/add/show/test` CLI commands.

Hermetic: uses the fixed local `FakeOpenAiServer` fixture (loopback only,
scenario selected by a keyword in the request prompt) for every "live"
network scenario -- no real provider, credential, or internal endpoint.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path

from recollect_lines import cli
from recollect_lines.provider_commands import (
    run_provider_add,
    run_provider_list,
    run_provider_show,
    run_provider_test,
)
from recollect_lines.providers import load_providers_config

ROOT = Path(__file__).resolve().parent.parent
TLS_CERT = ROOT / "tests" / "fixtures" / "tls" / "self_signed_cert.pem"
TLS_KEY = ROOT / "tests" / "fixtures" / "tls" / "self_signed_key.pem"
FIXTURE_SERVER = ROOT / "tests" / "fixtures" / "fake_openai_server.py"
_spec = importlib.util.spec_from_file_location("fake_openai_server", FIXTURE_SERVER)
assert _spec and _spec.loader
_fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake)
FakeOpenAiServer = _fake.FakeOpenAiServer
provider_document = _fake.provider_document

SECRET = "sk-super-secret-value-must-never-appear-anywhere"


def _write_config(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProviderListTests(unittest.TestCase):
    def test_not_configured_is_empty_not_blocking(self):
        report, exit_code = run_provider_list(providers_config=None, providers_config_origin=None)
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["providers"], [])
        self.assertEqual(report["source"]["origin"], "not_configured")

    def test_lists_configured_provider_metadata_without_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp) / "providers.json", provider_document(
                "https://api.example.com/v1", name="alpha", api_key_env="ALPHA_KEY",
            ))
            report, exit_code = run_provider_list(
                providers_config=path, providers_config_origin="explicit", environ={"ALPHA_KEY": SECRET},
            )
            self.assertEqual(exit_code, 0)
            self.assertNotIn(SECRET, json.dumps(report))
            names = {p["name"] for p in report["providers"]}
            self.assertEqual(names, {"alpha"})
            entry = report["providers"][0]
            self.assertEqual(entry["credential_reference"], "ALPHA_KEY")
            self.assertEqual(entry["default_model"], "fake-model")
            self.assertTrue(entry["observed_availability"]["available"])

    def test_invalid_config_is_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(json.dumps({"providers": {"bad": {"kind": "openai-compatible", "api_key": "sk-x"}}}))
            report, exit_code = run_provider_list(providers_config=path, providers_config_origin="explicit")
            self.assertEqual(exit_code, 1)
            codes = {f["code"] for f in report["findings"]}
            self.assertIn("PROVIDERS_CONFIG_INVALID", codes)


class ProviderShowTests(unittest.TestCase):
    def test_not_configured_is_an_explicit_error(self):
        report, exit_code = run_provider_show(providers_config=None, providers_config_origin=None, name="x")
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["error"]["code"], "ProviderConfigNotConfigured")

    def test_unknown_provider_is_an_explicit_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp) / "providers.json", provider_document("https://api.example.com/v1", name="alpha"))
            report, exit_code = run_provider_show(providers_config=path, providers_config_origin="explicit", name="missing")
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["error"]["code"], "ProviderNotFound")

    def test_shows_only_the_requested_provider_fully_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = provider_document("https://api.example.com/v1", name="alpha", api_key_env="ALPHA_KEY")
            doc["providers"]["beta"] = dict(doc["providers"]["alpha"])
            path = _write_config(Path(tmp) / "providers.json", doc)
            report, exit_code = run_provider_show(
                providers_config=path, providers_config_origin="explicit", name="alpha",
                environ={"ALPHA_KEY": SECRET},
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(report["redacted"])
            self.assertEqual(report["provider"]["name"], "alpha")
            self.assertNotIn(SECRET, json.dumps(report))

    def test_cli_requires_redacted_flag_syntax_but_show_is_always_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp) / "providers.json", provider_document("https://api.example.com/v1", name="alpha"))
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = cli.main([
                    "--providers-config", str(path), "provider", "show", "alpha", "--redacted", "--json",
                ])
            self.assertEqual(exit_code, 0)
            report = json.loads(buf.getvalue())
            self.assertTrue(report["redacted"])


class ProviderAddTests(unittest.TestCase):
    def test_creates_new_config_and_validates_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            report, exit_code = run_provider_add(
                name="alpha", base_url="https://api.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="alpha-model", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["action"], "created")
            providers = load_providers_config(dest)
            self.assertIn("alpha", providers)
            self.assertEqual(providers["alpha"].api_key_env, "ALPHA_KEY")

    @unittest.skipUnless(os.name == "posix", "POSIX file mode is not meaningful on this platform")
    def test_new_file_is_owner_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            run_provider_add(
                name="alpha", base_url="https://api.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="m", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file mode is not meaningful on this platform")
    def test_preserves_existing_file_permission_bits(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            run_provider_add(
                name="alpha", base_url="https://api.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="m", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            os.chmod(dest, 0o640)
            run_provider_add(
                name="beta", base_url="https://api.example.com/v1", api_key_env="BETA_KEY",
                default_model="m", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o640)

    def test_preserves_other_providers_when_adding_a_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            run_provider_add(
                name="alpha", base_url="https://api.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="m", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            report, exit_code = run_provider_add(
                name="beta", base_url="https://api2.example.com/v1", api_key_env="BETA_KEY",
                default_model="m2", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["action"], "updated")
            providers = load_providers_config(dest)
            self.assertEqual(set(providers), {"alpha", "beta"})

    def test_rejects_duplicate_name_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            run_provider_add(
                name="alpha", base_url="https://api.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="m", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            report, exit_code = run_provider_add(
                name="alpha", base_url="https://other.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="m2", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["error"]["code"], "ProviderAlreadyExists")
            # original entry must be untouched
            providers = load_providers_config(dest)
            self.assertEqual(providers["alpha"].base_url, "https://api.example.com/v1")

    def test_force_overwrites_existing_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            run_provider_add(
                name="alpha", base_url="https://api.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="m", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            report, exit_code = run_provider_add(
                name="alpha", base_url="https://other.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="m2", explicit_path=dest, force=True,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["action"], "overwritten")
            providers = load_providers_config(dest)
            self.assertEqual(providers["alpha"].base_url, "https://other.example.com/v1")

    def test_rejects_malformed_api_key_env_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            report, exit_code = run_provider_add(
                name="alpha", base_url="https://api.example.com/v1", api_key_env="not-a-valid-env-name",
                default_model="m", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["error"]["code"], "InvalidProviderEntry")
            self.assertFalse(dest.exists())

    def test_no_flag_accepts_a_raw_secret_value(self):
        """`provider add` only ever exposes --api-key-env (a name), never --api-key."""
        top_parser = cli.parser()
        command_action = next(a for a in top_parser._actions if a.dest == "command")
        provider_parser = command_action.choices["provider"]
        provider_command_action = next(a for a in provider_parser._actions if a.dest == "provider_command")
        add_parser = provider_command_action.choices["add"]
        dest_names = {action.dest for action in add_parser._actions}
        self.assertIn("api_key_env", dest_names)
        self.assertNotIn("api_key", dest_names)

    def test_refuses_legacy_default_config_without_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "providers.json"
            _write_config(legacy, provider_document("https://api.example.com/v1", name="alpha"))
            report, exit_code = run_provider_add(
                name="beta", base_url="https://other.example.com/v1", api_key_env="BETA_KEY",
                default_model="m", explicit_path=None,
                resolved_source_path=legacy, resolved_source_origin="legacy_default",
            )
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["error"]["code"], "UnsafeConfigTarget")
            # legacy file must be untouched
            providers = load_providers_config(legacy)
            self.assertEqual(set(providers), {"alpha"})

    def test_refuses_invalid_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            dest.write_text(json.dumps({"providers": {"bad": {"kind": "openai-compatible"}}}))
            report, exit_code = run_provider_add(
                name="alpha", base_url="https://api.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="m", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["error"]["code"], "ExistingConfigInvalid")

    def test_writes_atomically_no_leftover_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            run_provider_add(
                name="alpha", base_url="https://api.example.com/v1", api_key_env="ALPHA_KEY",
                default_model="m", explicit_path=dest,
                resolved_source_path=None, resolved_source_origin="not_configured",
            )
            leftovers = [p for p in Path(tmp).iterdir() if p.name != "config.yaml"]
            self.assertEqual(leftovers, [])

    def test_cli_add_then_list_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = cli.main([
                    "provider", "add",
                    "--name", "alpha", "--base-url", "https://api.example.com/v1",
                    "--api-key-env", "ALPHA_KEY", "--default-model", "m",
                    "--path", str(dest), "--json",
                ])
            self.assertEqual(exit_code, 0)
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                exit_code = cli.main(["--providers-config", str(dest), "provider", "list", "--json"])
            self.assertEqual(exit_code, 0)
            report = json.loads(buf2.getvalue())
            self.assertEqual({p["name"] for p in report["providers"]}, {"alpha"})


class ProviderTestConfigLayerTests(unittest.TestCase):
    def test_not_configured_blocks(self):
        report, exit_code = run_provider_test(name="alpha", providers_config=None, providers_config_origin=None)
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["outcome"], "blocked")
        codes = {c["code"] for c in report["checks"]}
        self.assertIn("CONFIG_SOURCE", codes)

    def test_invalid_schema_is_a_config_source_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(json.dumps({"providers": {"bad": {"kind": "openai-compatible", "api_key": "sk-x"}}}))
            report, exit_code = run_provider_test(name="bad", providers_config=path, providers_config_origin="explicit")
            self.assertEqual(exit_code, 1)
            source_check = next(c for c in report["checks"] if c["code"] == "CONFIG_SOURCE")
            self.assertEqual(source_check["status"], "error")

    def test_unknown_provider_is_its_own_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp) / "providers.json", provider_document("https://api.example.com/v1", name="alpha"))
            report, exit_code = run_provider_test(name="ghost", providers_config=path, providers_config_origin="explicit")
            self.assertEqual(exit_code, 1)
            codes = {c["code"] for c in report["checks"]}
            self.assertIn("PROVIDER_UNKNOWN", codes)

    def test_missing_credential_reference_is_a_warning_without_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp) / "providers.json", provider_document(
                "https://api.example.com/v1", name="alpha", api_key_env="MISSING_ALPHA_KEY",
            ))
            report, exit_code = run_provider_test(
                name="alpha", providers_config=path, providers_config_origin="explicit", environ={},
            )
            self.assertEqual(exit_code, 0)
            cred_check = next(c for c in report["checks"] if c["code"] == "CREDENTIAL_REFERENCE")
            self.assertEqual(cred_check["status"], "warning")
            probe_check = next(c for c in report["checks"] if c["code"] == "REMOTE_PROBE")
            self.assertEqual(probe_check["status"], "not_checked")

    def test_missing_credential_reference_blocks_when_live_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp) / "providers.json", provider_document(
                "https://api.example.com/v1", name="alpha", api_key_env="MISSING_ALPHA_KEY",
            ))
            report, exit_code = run_provider_test(
                name="alpha", providers_config=path, providers_config_origin="explicit", environ={},
                live=True, acknowledge_billed_remote_calls=True,
            )
            self.assertEqual(exit_code, 1)
            cred_check = next(c for c in report["checks"] if c["code"] == "CREDENTIAL_REFERENCE")
            self.assertEqual(cred_check["status"], "error")

    def test_capability_layer_reports_declared_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp) / "providers.json", provider_document("https://api.example.com/v1", name="alpha"))
            report, exit_code = run_provider_test(name="alpha", providers_config=path, providers_config_origin="explicit", environ={"TEST_OPENAI_API_KEY": SECRET})
            cap_check = next(c for c in report["checks"] if c["code"] == "CAPABILITY")
            self.assertEqual(cap_check["status"], "ok")
            self.assertTrue(cap_check["details"]["chat_completions"])
            self.assertNotIn(SECRET, json.dumps(report))

    def test_no_live_flag_sends_no_network_traffic(self):
        # Point at a port nothing listens on; if a probe were attempted this
        # would either hang or fail -- a fast, clean not_checked proves no
        # network I/O happened at all for the default (management) path.
        with tempfile.TemporaryDirectory() as tmp:
            port = _free_port()
            path = _write_config(Path(tmp) / "providers.json", provider_document(
                f"http://127.0.0.1:{port}/v1", name="alpha", request_timeout_seconds=1,
            ))
            report, exit_code = run_provider_test(
                name="alpha", providers_config=path, providers_config_origin="explicit",
                environ={"TEST_OPENAI_API_KEY": SECRET},
            )
            self.assertEqual(exit_code, 0)
            probe_check = next(c for c in report["checks"] if c["code"] == "REMOTE_PROBE")
            self.assertEqual(probe_check["status"], "not_checked")

    def test_live_without_acknowledgement_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp) / "providers.json", provider_document("https://api.example.com/v1", name="alpha"))
            report, exit_code = run_provider_test(
                name="alpha", providers_config=path, providers_config_origin="explicit",
                environ={"TEST_OPENAI_API_KEY": SECRET}, live=True,
            )
            self.assertEqual(exit_code, 1)
            probe_check = next(c for c in report["checks"] if c["code"] == "REMOTE_PROBE")
            self.assertEqual(probe_check["status"], "error")
            self.assertIn("i-accept-billed-remote-calls", probe_check["remediation"])


class ProviderTestLiveProbeTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeOpenAiServer()
        self.server.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.env = {"TEST_OPENAI_API_KEY": SECRET}

    def tearDown(self):
        self.server.stop()
        self.tempdir.cleanup()

    def _config(self, **overrides) -> Path:
        return _write_config(
            Path(self.tempdir.name) / "providers.json",
            provider_document(self.server.base_url, name="alpha", request_timeout_seconds=5, **overrides),
        )

    def test_successful_probe_reports_ok_without_secret_or_response_body(self):
        path = self._config()
        report, exit_code = run_provider_test(
            name="alpha", providers_config=path, providers_config_origin="explicit",
            environ=self.env, live=True, acknowledge_billed_remote_calls=True,
        )
        self.assertEqual(exit_code, 0)
        probe_check = next(c for c in report["checks"] if c["code"] == "REMOTE_PROBE")
        self.assertEqual(probe_check["status"], "ok")
        self.assertEqual(probe_check["details"]["http_status"], 200)
        self.assertNotIn(SECRET, json.dumps(report))
        # the response body/summary text is never echoed back
        self.assertNotIn("answer for", json.dumps(report))

    def test_authentication_failure_is_its_own_layer_not_a_timeout(self):
        path = self._config()
        report, exit_code = run_provider_test(
            name="alpha", providers_config=path, providers_config_origin="explicit",
            environ=self.env, live=True, acknowledge_billed_remote_calls=True,
            probe_prompt="MISSING_AUTH please",
        )
        self.assertEqual(exit_code, 1)
        probe_check = next(c for c in report["checks"] if c["code"] == "REMOTE_PROBE")
        self.assertEqual(probe_check["status"], "error")
        self.assertEqual(probe_check["details"]["layer"], "auth")
        self.assertEqual(probe_check["details"]["category"], "authentication_error")
        self.assertNotIn("timed out", probe_check["message"].lower())

    def test_rate_limit_is_an_http_layer_failure(self):
        path = self._config()
        report, exit_code = run_provider_test(
            name="alpha", providers_config=path, providers_config_origin="explicit",
            environ=self.env, live=True, acknowledge_billed_remote_calls=True,
            probe_prompt="RATE_LIMIT please",
        )
        self.assertEqual(exit_code, 1)
        probe_check = next(c for c in report["checks"] if c["code"] == "REMOTE_PROBE")
        self.assertEqual(probe_check["details"]["layer"], "http")
        self.assertEqual(probe_check["details"]["category"], "rate_limit_or_quota_error")

    def test_timeout_override_is_honored_for_the_probe_only(self):
        path = self._config()
        report, exit_code = run_provider_test(
            name="alpha", providers_config=path, providers_config_origin="explicit",
            environ=self.env, live=True, acknowledge_billed_remote_calls=True,
            timeout_override=3,
        )
        self.assertEqual(exit_code, 0)
        probe_check = next(c for c in report["checks"] if c["code"] == "REMOTE_PROBE")
        self.assertEqual(probe_check["status"], "ok")


class ProviderTestTlsLayerTests(unittest.TestCase):
    def test_untrusted_certificate_is_a_tls_layer_not_a_timeout(self):
        server = FakeOpenAiServer(certfile=str(TLS_CERT), keyfile=str(TLS_KEY))
        server.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(Path(tmp) / "providers.json", provider_document(
                    server.base_url, name="alpha", request_timeout_seconds=15, allow_insecure_http=False,
                ))
                report, exit_code = run_provider_test(
                    name="alpha", providers_config=path, providers_config_origin="explicit",
                    environ={"TEST_OPENAI_API_KEY": SECRET}, live=True, acknowledge_billed_remote_calls=True,
                )
                self.assertEqual(exit_code, 1)
                probe_check = next(c for c in report["checks"] if c["code"] == "REMOTE_PROBE")
                self.assertEqual(probe_check["details"]["layer"], "tls")
                self.assertEqual(probe_check["details"]["category"], "tls_verification_error")
                self.assertNotIn("timed out", probe_check["message"].lower())
        finally:
            server.stop()


class ProviderTestConnectionLayerTests(unittest.TestCase):
    def test_nothing_listening_fails_within_the_probe_deadline(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp) / "providers.json", provider_document(
                f"http://127.0.0.1:{port}/v1", name="alpha", request_timeout_seconds=1,
            ))
            report, exit_code = run_provider_test(
                name="alpha", providers_config=path, providers_config_origin="explicit",
                environ={"TEST_OPENAI_API_KEY": SECRET}, live=True, acknowledge_billed_remote_calls=True,
            )
            self.assertEqual(exit_code, 1)
            probe_check = next(c for c in report["checks"] if c["code"] == "REMOTE_PROBE")
            self.assertIn(probe_check["details"]["layer"], ("connection", "deadline"))


class ProviderCliHelpTests(unittest.TestCase):
    def test_provider_subcommand_registered_in_help(self):
        import contextlib
        import io
        buf = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(buf):
            cli.main(["--help"])
        self.assertIn("provider", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
