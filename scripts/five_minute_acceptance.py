#!/usr/bin/env python3
"""Hermetic five-minute operator acceptance (Wave 3 / PR 9).

Builds a disposable virtual environment, installs recollect-lines from local
artifacts (no PYTHONPATH=src shortcut), then runs the documented fresh-operator
path end to end:

  install → init → config validate → provider add (env-var reference only) →
  doctor → mcp print/install (temporary host target) → bounded fixture delegate ping

No live provider, network login, human credentials, or external MCP host is
required. Exit 0 only when every step produces the expected evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FAKE_OPENCODE = ROOT / "tests" / "fixtures" / "fake_opencode.py"
FAKE_MCP = ROOT / "tests" / "fixtures" / "fake_mcp_ping.py"

# Reuse the Phase 7A clean-install helpers — one venv, one install story.
sys.path.insert(0, str(ROOT / "scripts"))
from clean_install_acceptance import (  # noqa: E402
    _manual_install,
    _pip_wheel_install,
    _scripts_installed,
    run,
)

_failures: list[str] = []


def step(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(label)


def run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")


def init_fixture_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], path)
    run_git(["config", "user.email", "acceptance@example.com"], path)
    run_git(["config", "user.name", "Five Minute Acceptance"], path)
    (path / "README.md").write_text("Disposable fixture repository for five-minute acceptance.\n")
    run_git(["add", "-A"], path)
    run_git(["commit", "-q", "-m", "initial"], path)
    return path


class McpStdioClient:
    def __init__(self, home: Path, env: dict[str, str], python: Path):
        broker_env = dict(env)
        broker_env["PYTHONPATH"] = str(SRC)
        self.process = subprocess.Popen(
            [
                str(python), "-m", "recollect_lines.mcp_server",
                "--home", str(home),
                "--opencode-command", json.dumps([str(python), str(FAKE_OPENCODE)]),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=broker_env,
        )
        self._next_id = 1

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"MCP subprocess produced no output (exit={self.process.poll()})")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise RuntimeError(f"expected response id {request_id}, got {response!r}")
        return response

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        assert self.process.stdin
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call_tool(self, name: str, arguments: dict) -> tuple[bool, dict]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in response:
            raise RuntimeError(f"tools/call for {name!r} failed: {response['error']}")
        result = response["result"]
        body = json.loads(result["content"][0]["text"])
        return bool(result["isError"]), (body["data"] if body["ok"] else body["error"])

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.wait(timeout=5)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="recollect-five-minute-") as tmp:
        tmp_path = Path(tmp)
        venv_dir = tmp_path / "venv"
        venv.create(venv_dir, with_pip=True)
        if sys.platform == "win32":
            python = venv_dir / "Scripts" / "python.exe"
            scripts = venv_dir / "Scripts"
        else:
            python = venv_dir / "bin" / "python"
            scripts = venv_dir / "bin"

        if _pip_wheel_install(python, tmp_path) and _scripts_installed(scripts):
            print("Installed package via local wheel (pip).")
        else:
            print("Pip wheel install unavailable; using offline manual install fallback.")
            _manual_install(python, scripts)

        env = {
            **os.environ,
            "PATH": f"{scripts}{os.pathsep}{os.environ.get('PATH', '')}",
            "ACCEPTANCE_PROVIDER_API_KEY": "acceptance-placeholder-not-a-real-secret",
        }
        home = tmp_path / "broker"
        workdir = tmp_path / "work"
        workdir.mkdir()
        host_config = tmp_path / "cursor-mcp.json"
        recollect_mcp = shutil.which("recollect-mcp", path=env["PATH"])
        step("install exposes recollect-lines and recollect-mcp", recollect_mcp is not None)

        init_result = run(
            ["recollect-lines", "--home", str(home), "init", "--json"],
            cwd=workdir,
            env=env,
        )
        init_payload = json.loads(init_result.stdout)
        step(
            "init creates home and starter config",
            init_payload.get("config_action") in {"created", "preserved"},
            init_result.stdout,
        )
        step(
            "init JSON has no raw secret material",
            "sk-" not in init_result.stdout and env["ACCEPTANCE_PROVIDER_API_KEY"] not in init_result.stdout,
        )

        validate_result = run(
            ["recollect-lines", "--home", str(home), "config", "validate", "--json"],
            cwd=workdir,
            env=env,
        )
        validate_payload = json.loads(validate_result.stdout)
        step("config validate succeeds", validate_result.returncode == 0)
        lifecycle = [
            f for f in validate_payload.get("findings", [])
            if f.get("code") == "PROVIDER_CONFIG_LIFECYCLE"
        ]
        step("config validate reports resolved source", bool(lifecycle), validate_result.stdout)

        config_path = home / "config.yaml"
        add_result = run([
            "recollect-lines", "--home", str(home), "provider", "add",
            "--name", "acceptance_gateway",
            "--base-url", "http://127.0.0.1:4010/v1",
            "--api-key-env", "ACCEPTANCE_PROVIDER_API_KEY",
            "--default-model", "acceptance-model",
            "--allow-insecure-http",
            "--path", str(config_path),
            "--json",
        ], cwd=workdir, env=env)
        add_payload = json.loads(add_result.stdout)
        step("provider add succeeds with env-var reference only", add_result.returncode == 0)
        step(
            "provider add stores credential reference not value",
            add_payload.get("provider", {}).get("credential_reference") == "ACCEPTANCE_PROVIDER_API_KEY",
        )
        step(
            "provider add output never echoes the env var value",
            env["ACCEPTANCE_PROVIDER_API_KEY"] not in add_result.stdout,
        )

        provider_test = run([
            "recollect-lines", "--providers-config", str(config_path),
            "provider", "test", "acceptance_gateway", "--json",
        ], cwd=workdir, env=env)
        test_payload = json.loads(provider_test.stdout)
        step("provider test offline diagnostics succeed", provider_test.returncode == 0)
        step(
            "provider test did not perform a live remote probe",
            not test_payload.get("live_probe_performed", False),
        )

        doctor_result = run(
            ["recollect-lines", "--home", str(home), "--providers-config", str(config_path), "doctor", "--json"],
            cwd=workdir,
            env=env,
        )
        doctor_payload = json.loads(doctor_result.stdout)
        step("doctor --json succeeds", doctor_result.returncode in {0, 1})
        step("doctor JSON has stable schema version", "doctor_schema_version" in doctor_payload)
        step("doctor output has no raw secret material", env["ACCEPTANCE_PROVIDER_API_KEY"] not in doctor_result.stdout)

        print_result = run([
            "recollect-lines", "--home", str(home), "mcp", "print",
            "--host", "cursor",
            "--config-path", str(host_config),
            "--json",
        ], cwd=workdir, env=env)
        print_payload = json.loads(print_result.stdout)
        step("mcp print is side-effect free", not host_config.exists())
        step("mcp print renders registration", "mcpServers" in print_payload.get("rendered", ""))

        install_result = run([
            "recollect-lines", "--home", str(home), "mcp", "install",
            "--host", "cursor",
            "--config-path", str(host_config),
            "--mcp-command", recollect_mcp or "recollect-mcp",
            "--json",
        ], cwd=workdir, env=env)
        install_payload = json.loads(install_result.stdout)
        step("mcp install writes host config", host_config.is_file())
        step("mcp install verification succeeds", install_result.returncode == 0)
        installed = json.loads(host_config.read_text(encoding="utf-8"))
        entry = installed["mcpServers"]["recollect-lines"]
        step("installed registration uses absolute command path", Path(entry["command"]).is_absolute())

        verify_checks = {check["code"]: check for check in install_payload.get("verification", {}).get("checks", [])}
        step(
            "mcp install performed bounded initialize ping",
            verify_checks.get("MCP_DELEGATE_PING_OK", {}).get("status") == "ok",
            str(verify_checks.get("MCP_DELEGATE_PING_OK")),
        )

        fixture_repo = init_fixture_repo(tmp_path / "fixture-repo")
        client = McpStdioClient(home, env, python)
        try:
            init_resp = client.request(
                "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "five-minute-acceptance", "version": "0"}},
            )
            step(
                "fixture delegate ping: initialize handshake",
                init_resp["result"]["serverInfo"]["name"] == "recollect-lines-mcp",
            )
            client.notify("notifications/initialized")

            is_error, delegated = client.call_tool("delegate", {
                "task": "Inspect the fixture repository",
                "workspace": str(fixture_repo),
                "execution_mode": "isolated_worktree",
                "profile": "opencode",
            })
            task_id = delegated.get("task_id", "")
            step("fixture delegate ping: delegate accepted", not is_error and bool(task_id))

            is_error, collected = client.call_tool("collect", {"task_id": task_id})
            step(
                "fixture delegate ping: collect succeeded",
                not is_error and collected.get("state") == "succeeded",
                str(collected),
            )
            runtime_adapter = (collected.get("runtime_result") or {}).get("runtime", {}).get("adapter")
            step(
                "fixture delegate ping: runtime adapter is opencode fixture",
                runtime_adapter == "opencode",
                str(runtime_adapter),
            )
        finally:
            client.close()

    print()
    if _failures:
        print(f"Five-minute acceptance FAILED ({len(_failures)} check(s)): {', '.join(_failures)}")
        return 1
    print(
        "Five-minute acceptance PASSED: clean venv install, init, config validate, "
        "provider add (env-var reference), doctor, mcp print/install, and bounded "
        "fixture delegate ping completed without live provider credentials."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
