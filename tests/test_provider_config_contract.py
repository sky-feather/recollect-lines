"""Wave 2 / PR 5: configuration contract completion.

Covers the tracked example/schema artifacts, gitignore policy for local
operator config files, the `config validate`/`config init` CLI surface, and
generated-local-config file permissions.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from recollect_lines import cli
from recollect_lines.doctor import run_config_validate
from recollect_lines.providers import (
    ProviderConfigError,
    load_providers_config,
    write_local_config_file,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SECRET = "sk-super-secret-value-must-not-appear"


class TrackedExampleAndSchemaTests(unittest.TestCase):
    def test_example_yaml_is_tracked_and_validates(self):
        path = CONFIG_DIR / "providers.example.yaml"
        self.assertTrue(path.is_file())
        providers = load_providers_config(path)
        self.assertIn("local_gateway", providers)
        self.assertIn("remote_gateway", providers)

    def test_example_contains_no_real_secrets(self):
        text = (CONFIG_DIR / "providers.example.yaml").read_text()
        lowered = text.lower()
        for marker in ("sk-ant-", "bearer ", "-----begin"):
            self.assertNotIn(marker, lowered)

    def test_schema_is_valid_json_and_describes_the_contract(self):
        schema = json.loads((CONFIG_DIR / "providers.schema.json").read_text())
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        provider_schema = schema["$defs"]["provider"]
        self.assertFalse(provider_schema["additionalProperties"])
        self.assertIn("api_key_env", provider_schema["properties"])
        self.assertNotIn("api_key", provider_schema["properties"])


class GitignorePolicyTests(unittest.TestCase):
    """Prove the shipped .gitignore hides legacy local config but not tracked examples.

    Runs against a scratch git repo (not the real one) so this is hermetic
    and doesn't depend on ambient repo state.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repo = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / ".gitignore").write_text((ROOT / ".gitignore").read_text())

    def _is_ignored(self, relative_path: str) -> bool:
        target = self.repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("providers: {}\n")
        result = subprocess.run(
            ["git", "check-ignore", "-q", relative_path],
            cwd=self.repo,
        )
        return result.returncode == 0

    def test_legacy_repo_root_config_is_ignored(self):
        self.assertTrue(self._is_ignored("providers.json"))

    def test_operator_dir_config_is_ignored(self):
        self.assertTrue(self._is_ignored(".recollect/config.yaml"))
        self.assertTrue(self._is_ignored(".recollect/config.json"))

    def test_tracked_example_is_not_ignored(self):
        self.assertFalse(self._is_ignored("config/providers.example.yaml"))

    def test_tracked_fixture_providers_json_is_not_ignored(self):
        self.assertFalse(self._is_ignored("examples/litellm-openai-compatible/providers.json"))


class WriteLocalConfigFileTests(unittest.TestCase):
    def test_writes_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / ".recollect" / "config.yaml"
            written = write_local_config_file(dest)
            self.assertEqual(written, dest)
            providers = load_providers_config(dest)
            self.assertIn("local", providers)

    @unittest.skipUnless(os.name == "posix", "POSIX file mode is not meaningful on this platform")
    def test_written_file_is_owner_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            write_local_config_file(dest)
            mode = stat.S_IMODE(dest.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            write_local_config_file(dest)
            with self.assertRaises(FileExistsError):
                write_local_config_file(dest)

    def test_force_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            write_local_config_file(dest)
            written = write_local_config_file(dest, force=True)
            self.assertEqual(written, dest)


class ConfigValidateReportTests(unittest.TestCase):
    def test_redacts_secret_values_never_prints_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(json.dumps({
                "providers": {
                    "local": {
                        "kind": "openai-compatible",
                        "base_url": "http://127.0.0.1:8765/v1",
                        "api_key_env": "LOCAL_KEY",
                        "default_model": "m",
                        "allow_insecure_http": True,
                    }
                }
            }))
            report, exit_code = run_config_validate(
                providers_config=path, environ={"LOCAL_KEY": SECRET},
            )
            self.assertEqual(exit_code, 0)
            self.assertNotIn(SECRET, json.dumps(report))
            codes = {f["code"] for f in report["findings"]}
            self.assertIn("PROVIDERS_CONFIG_VALID", codes)
            self.assertIn("PROVIDER_SECRET_REFERENCE_PRESENT", codes)
            self.assertIn("PROVIDER_CONFIG_LIFECYCLE", codes)

    def test_invalid_config_is_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(json.dumps({"providers": {"bad": {"kind": "openai-compatible", "api_key": "sk-x"}}}))
            report, exit_code = run_config_validate(providers_config=path, environ={})
            self.assertEqual(exit_code, 1)
            self.assertEqual(report["status"], "blocking")
            codes = {f["code"] for f in report["findings"]}
            self.assertIn("PROVIDERS_CONFIG_INVALID", codes)

    def test_not_configured_is_not_blocking(self):
        report, exit_code = run_config_validate(providers_config=None, environ={})
        self.assertEqual(exit_code, 0)


class ConfigCliTests(unittest.TestCase):
    def test_config_init_writes_mode_0600_and_valid_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            exit_code = cli.main(["config", "init", "--path", str(dest)])
            self.assertEqual(exit_code, 0)
            self.assertTrue(dest.is_file())
            load_providers_config(dest)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)

    def test_config_init_without_force_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "config.yaml"
            self.assertEqual(cli.main(["config", "init", "--path", str(dest)]), 0)
            self.assertEqual(cli.main(["config", "init", "--path", str(dest)]), 2)

    def test_config_validate_json_redacts_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(json.dumps({
                "providers": {
                    "local": {
                        "kind": "openai-compatible",
                        "base_url": "http://127.0.0.1:8765/v1",
                        "api_key_env": "LOCAL_KEY",
                        "default_model": "m",
                        "allow_insecure_http": True,
                    }
                }
            }))
            old_env = os.environ.get("LOCAL_KEY")
            os.environ["LOCAL_KEY"] = SECRET
            try:
                import contextlib
                import io
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    exit_code = cli.main(["--providers-config", str(path), "config", "validate", "--json"])
                output = buf.getvalue()
            finally:
                if old_env is None:
                    os.environ.pop("LOCAL_KEY", None)
                else:
                    os.environ["LOCAL_KEY"] = old_env
            self.assertEqual(exit_code, 0)
            self.assertNotIn(SECRET, output)
            report = json.loads(output)
            self.assertEqual(report["status"], "ok")

    def test_config_subcommand_registered_in_help(self):
        import contextlib
        import io
        buf = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(buf):
            cli.main(["--help"])
        self.assertIn("config", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
