"""Wave 3 / PR 8: `recollect-lines mcp print|install` and post-install verification.

Hermetic: isolated temporary host config files, local MCP entrypoint overrides,
and no real provider/network/Cursor/Claude/Codex installations.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from recollect_lines import cli
from recollect_lines.mcp_commands import (
    MCP_SERVER_NAME,
    McpCommandError,
    build_server_entry,
    resolve_host_target,
    run_mcp_install,
    run_mcp_print,
    run_mcp_verify,
)

ROOT = Path(__file__).resolve().parent.parent
FAKE_MCP = Path(__file__).parent / "fixtures" / "fake_mcp_ping.py"
SECRET = "sk-super-secret-value-must-never-appear"


class McpHostTargetTests(unittest.TestCase):
    def test_unsupported_host_is_rejected(self):
        with self.assertRaises(McpCommandError) as ctx:
            resolve_host_target(
                host="vscode",
                scope="global",
                config_path=None,
                repo_root=Path("/tmp/repo"),
                user_home=Path("/tmp/home"),
                environ={},
            )
        self.assertEqual(ctx.exception.code, "HostNotSupported")


class McpPrintTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.repo = self.tmp / "repo"
        self.home = self.repo / ".recollect"
        self.user_home = self.tmp / "user"
        self.repo.mkdir()
        self.user_home.mkdir()
        self.home.mkdir()
        self.mcp_command = str(Path(sys.executable).resolve())

    def _print(self, **kwargs):
        return run_mcp_print(
            host=kwargs.get("host", "cursor"),
            scope=kwargs.get("scope", "global"),
            home=self.home,
            config_path=kwargs.get("config_path"),
            mcp_command=kwargs.get("mcp_command", self.mcp_command),
            repo_root=self.repo,
            user_home=self.user_home,
            environ=kwargs.get("environ", {}),
        )

    def test_print_is_side_effect_free(self):
        config_path = self.tmp / "cursor-mcp.json"
        report, exit_code = self._print(host="cursor", config_path=config_path)
        self.assertEqual(exit_code, 0)
        self.assertFalse(config_path.exists())
        self.assertIn("mcpServers", report["rendered"])
        self.assertTrue(Path(report["registration"]["command"]).is_absolute())

    def test_print_rejects_unsupported_host(self):
        report, exit_code = self._print(host="opencode")
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["error"]["code"], "HostNotSupported")

    def test_print_codex_renders_toml(self):
        config_path = self.tmp / "config.toml"
        report, exit_code = self._print(host="codex", config_path=config_path)
        self.assertEqual(exit_code, 0)
        self.assertIn("[mcp_servers.recollect-lines]", report["rendered"])
        self.assertIn('command = "', report["rendered"])


class McpInstallTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.repo = self.tmp / "repo"
        self.home = self.repo / ".recollect"
        self.user_home = self.tmp / "user"
        self.repo.mkdir()
        self.user_home.mkdir()
        self.home.mkdir()
        self.config_path = self.tmp / "mcp.json"
        self.mcp_command = str(FAKE_MCP.resolve())

    def _install(self, **kwargs):
        return run_mcp_install(
            host=kwargs.get("host", "cursor"),
            scope="global",
            home=self.home,
            config_path=kwargs.get("config_path", self.config_path),
            mcp_command=kwargs.get("mcp_command", self.mcp_command),
            repo_root=self.repo,
            user_home=self.user_home,
            verify=kwargs.get("verify", True),
            skip_delegate_ping=kwargs.get("skip_delegate_ping", False),
            environ=kwargs.get("environ", {}),
        )

    def test_install_writes_json_registration_with_absolute_paths(self):
        report, exit_code = self._install()
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["action"], "installed")
        self.assertIsNone(report["backup_path"])
        loaded = json.loads(self.config_path.read_text())
        entry = loaded["mcpServers"][MCP_SERVER_NAME]
        self.assertTrue(Path(entry["command"]).is_absolute())
        self.assertIn(str(self.home.resolve()), entry["args"])

    def test_install_is_idempotent_without_backup(self):
        first, _ = self._install()
        original_text = self.config_path.read_text()
        second, exit_code = self._install()
        self.assertEqual(exit_code, 0)
        self.assertEqual(second["action"], "unchanged")
        self.assertIsNone(second["backup_path"])
        self.assertEqual(self.config_path.read_text(), original_text)
        self.assertEqual(first["registration"], second["registration"])

    def test_install_creates_backup_only_when_mutating(self):
        self._install()
        self.config_path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}) + "\n")
        report, _ = self._install()
        self.assertEqual(report["action"], "updated")
        self.assertIsNotNone(report["backup_path"])
        backup = Path(report["backup_path"])
        self.assertTrue(backup.is_file())
        self.assertIn("other", backup.read_text())

    def test_install_preserves_unrelated_mcp_entries(self):
        self.config_path.write_text(json.dumps({
            "mcpServers": {
                "other-server": {"command": "/usr/bin/true", "args": []},
            },
        }, indent=2) + "\n")
        self._install()
        loaded = json.loads(self.config_path.read_text())
        self.assertIn("other-server", loaded["mcpServers"])
        self.assertIn(MCP_SERVER_NAME, loaded["mcpServers"])

    def test_install_rejects_conflicting_entry(self):
        entry = build_server_entry(
            home=self.home,
            mcp_command=self.mcp_command,
            environ={},
            target=resolve_host_target(
                host="cursor",
                scope="global",
                config_path=self.config_path,
                repo_root=self.repo,
                user_home=self.user_home,
                environ={},
            ),
        )
        entry["args"] = ["/different/home"]
        self.config_path.write_text(json.dumps({"mcpServers": {MCP_SERVER_NAME: entry}}, indent=2) + "\n")
        report, exit_code = self._install()
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["error"]["code"], "ConflictingMcpEntry")

    def test_claude_global_merge_preserves_foreign_keys(self):
        config_path = self.tmp / "claude.json"
        config_path.write_text(json.dumps({"numStartups": 3, "mcpServers": {}}, indent=2) + "\n")
        report, exit_code = self._install(host="claude_code", config_path=config_path)
        self.assertEqual(exit_code, 0)
        loaded = json.loads(config_path.read_text())
        self.assertEqual(loaded["numStartups"], 3)
        self.assertIn(MCP_SERVER_NAME, loaded["mcpServers"])

    def test_codex_install_writes_toml_block(self):
        config_path = self.tmp / "config.toml"
        config_path.write_text("model = \"gpt-5-codex\"\n")
        report, exit_code = self._install(host="codex", config_path=config_path)
        self.assertEqual(exit_code, 0)
        text = config_path.read_text()
        self.assertIn('model = "gpt-5-codex"', text)
        self.assertIn("[mcp_servers.recollect-lines]", text)

    def test_generated_config_has_no_embedded_secrets(self):
        self._install(environ={"RECOLLECT_CONFIG": SECRET})
        text = self.config_path.read_text()
        self.assertNotIn(SECRET, text)
        self.assertIn("RECOLLECT_CONFIG", text)

    @unittest.skipUnless(os.name == "posix", "POSIX file mode is not meaningful on this platform")
    def test_install_preserves_existing_file_mode(self):
        self.config_path.write_text("{}\n")
        os.chmod(self.config_path, 0o644)
        self._install()
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o644)

    def test_verification_runs_delegate_ping_with_fake_entrypoint(self):
        report, exit_code = self._install()
        self.assertEqual(exit_code, 0)
        codes = [check["code"] for check in report["verification"]["checks"]]
        self.assertIn("MCP_DELEGATE_PING_OK", codes)

    def test_verification_can_skip_delegate_ping(self):
        report, exit_code = self._install(skip_delegate_ping=True)
        self.assertEqual(exit_code, 0)
        codes = [check["code"] for check in report["verification"]["checks"]]
        self.assertNotIn("MCP_DELEGATE_PING_OK", codes)
        self.assertIn("MCP_REGISTRATION_PERSISTED", codes)


class McpCliTests(unittest.TestCase):
    def test_cli_registers_mcp_subcommands(self):
        with redirect_stdout(io.StringIO()) as captured:
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["mcp", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("print", captured.getvalue())
        self.assertIn("install", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
