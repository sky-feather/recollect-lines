"""Wave 3 / PR 6: `recollect-lines init` -- local state/config bootstrap.

Covers idempotency, overwrite safety, secret-free generated content, POSIX
file/directory permissions, truthful diagnostic surfacing, and the CLI
surface (`init` subcommand registration, --json, --force).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from recollect_lines import cli
from recollect_lines.init import InitError, run_init
from recollect_lines.providers import load_providers_config

SECRET = "sk-super-secret-value-must-not-appear"


class RunInitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.repo_root = self.tmp / "repo"
        self.user_home = self.tmp / "home"
        self.repo_root.mkdir()
        self.user_home.mkdir()
        self.home = self.repo_root / ".recollect"

    def _run(self, *, force: bool = False, explicit=None, environ=None):
        return run_init(
            home=self.home,
            force=force,
            explicit_providers_config=explicit,
            environ=environ if environ is not None else {},
            repo_root=self.repo_root,
            user_home=self.user_home,
        )

    def test_fresh_init_creates_home_and_config(self):
        result, exit_code = self._run()
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["home_created"])
        self.assertEqual(result["config_action"], "created")
        config_path = Path(result["config_path"])
        self.assertTrue(config_path.is_file())
        providers = load_providers_config(config_path)
        self.assertIn("local", providers)
        self.assertEqual(result["config_source"], "repo_local")

    def test_generated_config_has_no_real_secret_or_credential_fields(self):
        self._run()
        text = (self.home / "config.yaml").read_text()
        lowered = text.lower()
        for marker in ("sk-", "bearer ", "-----begin", "api_key:", "token:", "password:"):
            self.assertNotIn(marker, lowered)

    @unittest.skipUnless(os.name == "posix", "POSIX file mode is not meaningful on this platform")
    def test_posix_permissions_are_restrictive(self):
        self._run()
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.home / "config.yaml").stat().st_mode), 0o600)

    def test_second_run_is_idempotent_and_preserves_content(self):
        first, _ = self._run()
        config_path = Path(first["config_path"])
        original_text = config_path.read_text()
        original_mtime_ns = config_path.stat().st_mtime_ns

        second, exit_code = self._run()
        self.assertEqual(exit_code, 0)
        self.assertFalse(second["home_created"])
        self.assertEqual(second["config_action"], "preserved")
        self.assertEqual(config_path.read_text(), original_text)
        self.assertEqual(config_path.stat().st_mtime_ns, original_mtime_ns)

    def test_existing_non_default_basename_is_preserved_not_duplicated(self):
        self.home.mkdir(parents=True)
        existing = self.home / "config.yml"
        existing.write_text(
            "providers:\n  custom:\n    kind: openai-compatible\n"
            "    base_url: https://example.invalid/v1\n"
            "    api_key_env: CUSTOM_KEY\n    default_model: m\n"
        )
        result, _ = self._run()
        self.assertEqual(result["config_action"], "preserved")
        self.assertEqual(Path(result["config_path"]), existing)
        self.assertFalse((self.home / "config.yaml").exists())

    def test_force_overwrites_existing_config(self):
        self._run()
        config_path = self.home / "config.yaml"
        config_path.write_text("providers:\n  broken:\n    kind: not-a-real-kind\n")
        result, exit_code = self._run(force=True)
        self.assertEqual(result["config_action"], "overwritten")
        load_providers_config(config_path)  # now valid starter content again

    def test_refuses_without_force_leaves_broken_config_alone(self):
        self.home.mkdir(parents=True)
        broken = self.home / "config.yaml"
        broken.write_text("not: [valid, providers, document")
        result, exit_code = self._run(force=False)
        self.assertEqual(result["config_action"], "preserved")
        self.assertEqual(broken.read_text(), "not: [valid, providers, document")
        # init leaves the broken file alone, but the diagnostic truthfully
        # reports it as blocking rather than claiming success.
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["diagnostics"]["status"], "blocking")

    def test_diagnostics_do_not_claim_provider_configured_when_credential_missing(self):
        result, exit_code = self._run(environ={})
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["diagnostics"]["status"], "degraded")
        codes = {f["code"] for f in result["diagnostics"]["findings"]}
        self.assertIn("PROVIDER_SECRET_REFERENCE_MISSING", codes)

    def test_diagnostics_ok_when_credential_present(self):
        result, exit_code = self._run(environ={"LOCAL_PROVIDER_API_KEY": SECRET})
        self.assertEqual(result["diagnostics"]["status"], "ok")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_explicit_providers_config_takes_precedence_in_reporting(self):
        explicit = self.tmp / "explicit.yaml"
        explicit.write_text(
            "providers:\n  ex:\n    kind: openai-compatible\n"
            "    base_url: https://example.invalid/v1\n"
            "    api_key_env: EX_KEY\n    default_model: m\n"
        )
        result, _ = self._run(explicit=explicit)
        # init still writes the repo-local starter file...
        self.assertEqual(result["config_action"], "created")
        # ...but truthfully reports that a higher-precedence source is active.
        self.assertEqual(result["config_source"], "explicit")
        self.assertEqual(result["config_source_path"], str(explicit))

    def test_home_colliding_with_a_file_raises_actionable_error(self):
        self.home.write_text("not a directory")
        with self.assertRaises(InitError):
            self._run()


class InitCliTests(unittest.TestCase):
    def test_init_subcommand_registered_in_help(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(buf):
            cli.main(["--help"])
        self.assertIn("init", buf.getvalue())

    def test_cli_init_writes_config_and_reports_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".recollect"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = cli.main(["--home", str(home), "init", "--json"])
            self.assertEqual(exit_code, 0)
            report = json.loads(buf.getvalue())
            self.assertEqual(report["config_action"], "created")
            self.assertTrue((home / "config.yaml").is_file())

    def test_cli_init_second_run_preserves_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".recollect"
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["--home", str(home), "init", "--json"])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = cli.main(["--home", str(home), "init", "--json"])
            self.assertEqual(exit_code, 0)
            report = json.loads(buf.getvalue())
            self.assertEqual(report["config_action"], "preserved")

    def test_cli_init_human_output_has_no_secret_leak_and_mentions_next_pr(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".recollect"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["--home", str(home), "init"])
            output = buf.getvalue()
            self.assertNotIn(SECRET, output)
            self.assertIn("mcp install", output)

    def test_cli_init_default_home_under_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    exit_code = cli.main(["init", "--json"])
                self.assertEqual(exit_code, 0)
                self.assertTrue((Path(tmp) / ".recollect" / "config.yaml").is_file())
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
