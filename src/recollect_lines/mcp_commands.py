"""`recollect-lines mcp print|install` -- Wave 3 / PR 8.

Safe MCP host registration for parent tools this project actually supports
as runtimes (`cursor`, `claude_code`, `codex`). Does not claim integration
for hosts outside that set (e.g. Claude Desktop, VS Code, OpenCode).

Generated registrations use absolute executable paths, inherit only named
environment-variable references when needed, and never embed secret values.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .doctor import run_doctor
from .providers import existing_file_mode, write_atomic_text

MCP_COMMANDS_SCHEMA_VERSION = "1"
MCP_SERVER_NAME = "recollect-lines"

SupportedHost = Literal["cursor", "claude_code", "codex"]
SupportedScope = Literal["global", "project"]
ConfigFormat = Literal["json", "toml"]

SUPPORTED_HOSTS: frozenset[str] = frozenset({"cursor", "claude_code", "codex"})

_SECRET_MARKERS = ("sk-", "bearer ", "-----begin", "api_key:", "token:", "password:")

_CODEX_SECTION_RE = re.compile(
    rf"^\[mcp_servers\.{re.escape(MCP_SERVER_NAME)}\]\s*$.*?(?=^\[|\Z)",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class HostTarget:
    host: SupportedHost
    scope: SupportedScope
    config_path: Path
    config_format: ConfigFormat


class McpCommandError(RuntimeError):
    def __init__(self, code: str, message: str, *, remediation: str | None = None):
        self.code = code
        self.message = message
        self.remediation = remediation
        super().__init__(message)


def _package_payload() -> dict[str, str]:
    return {"name": "recollect-lines", "version": __version__}


def _error_report(schema_key: str, *, code: str, message: str, remediation: str | None = None, **extra: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        schema_key: MCP_COMMANDS_SCHEMA_VERSION,
        "package": _package_payload(),
        "error": {"code": code, "message": message},
    }
    if remediation is not None:
        report["error"]["remediation"] = remediation
    report.update(extra)
    return report


def _codex_home(environ: dict[str, str], user_home: Path) -> Path:
    override = environ.get("CODEX_HOME")
    return Path(override).expanduser() if override else user_home / ".codex"


def resolve_host_target(
    *,
    host: str,
    scope: SupportedScope,
    config_path: Path | None,
    repo_root: Path,
    user_home: Path,
    environ: dict[str, str],
) -> HostTarget:
    if host not in SUPPORTED_HOSTS:
        raise McpCommandError(
            "HostNotSupported",
            f"Unsupported MCP host {host!r}",
            remediation=(
                f"Choose one of: {', '.join(sorted(SUPPORTED_HOSTS))}. "
                "This project only installs into hosts it supports as runtimes."
            ),
        )
    if config_path is not None:
        path = config_path.expanduser().resolve()
        fmt: ConfigFormat = "toml" if path.suffix == ".toml" else "json"
        return HostTarget(host, scope, path, fmt)  # type: ignore[arg-type]

    if host == "cursor":
        path = (repo_root if scope == "project" else user_home) / ".cursor" / "mcp.json"
        return HostTarget("cursor", scope, path, "json")
    if host == "claude_code":
        path = (repo_root / ".mcp.json") if scope == "project" else (user_home / ".claude.json")
        return HostTarget("claude_code", scope, path, "json")
    path = (_codex_home(environ, user_home) if scope == "global" else repo_root / ".codex") / "config.toml"
    return HostTarget("codex", scope, path, "toml")


def resolve_mcp_invocation(*, mcp_command: str | None) -> tuple[str, list[str]]:
    """Return (command, args_prefix) where args_prefix excludes --home."""
    if mcp_command is not None:
        path = Path(mcp_command).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path), []
        if path.suffix == ".py" and path.is_file():
            return str(Path(sys.executable).resolve()), [str(path)]
        return str(path), []

    which = shutil.which("recollect-mcp")
    if which:
        return str(Path(which).resolve()), []

    return str(Path(sys.executable).resolve()), ["-m", "recollect_lines.mcp_server"]


def build_server_entry(
    *,
    home: Path,
    mcp_command: str | None,
    environ: dict[str, str],
    target: HostTarget | None = None,
) -> dict[str, Any]:
    command, prefix_args = resolve_mcp_invocation(mcp_command=mcp_command)
    home_abs = str(home.expanduser().resolve())
    args = [*prefix_args, "--home", home_abs]
    entry: dict[str, Any] = {"command": command, "args": args}
    if environ.get("RECOLLECT_CONFIG"):
        if target is not None and target.config_format == "toml":
            entry["env_vars"] = ["RECOLLECT_CONFIG"]
        else:
            entry["env"] = {"RECOLLECT_CONFIG": "${env:RECOLLECT_CONFIG}"}
    return entry


def _json_document_for_target(target: HostTarget, entry: dict[str, Any]) -> dict[str, Any]:
    if target.host == "claude_code" and target.scope == "global":
        return {"mcpServers": {MCP_SERVER_NAME: entry}}
    return {"mcpServers": {MCP_SERVER_NAME: entry}}


def _render_json_document(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _render_codex_section(entry: dict[str, Any]) -> str:
    command = entry["command"]
    args = entry.get("args", [])
    lines = [f"[mcp_servers.{MCP_SERVER_NAME}]", f'command = "{command}"']
    if args:
        rendered_args = ", ".join(json.dumps(arg) for arg in args)
        lines.append(f"args = [{rendered_args}]")
    env = entry.get("env")
    if env:
        lines.append("")
        lines.append(f"[mcp_servers.{MCP_SERVER_NAME}.env]")
        for key, value in sorted(env.items()):
            lines.append(f'{key} = "{value}"')
    env_vars = entry.get("env_vars")
    if env_vars:
        rendered = ", ".join(json.dumps(item) for item in env_vars)
        lines.append(f"env_vars = [{rendered}]")
    return "\n".join(lines) + "\n"


def _load_json_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise McpCommandError(
            "InvalidHostConfig",
            f"Could not parse JSON MCP config {path}: {error}",
            remediation="Fix the file manually or pass --config-path to target a fresh file.",
        ) from error
    if not isinstance(loaded, dict):
        raise McpCommandError(
            "InvalidHostConfig",
            f"MCP config {path} must be a JSON object",
            remediation="Restore a top-level object or pass --config-path to a new file.",
        )
    return loaded


def _load_codex_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise McpCommandError(
            "InvalidHostConfig",
            f"Could not parse Codex config {path}: {error}",
            remediation="Fix the TOML manually or pass --config-path to target a fresh file.",
        ) from error


def _existing_server_entry(target: HostTarget, raw_document: dict[str, Any]) -> dict[str, Any] | None:
    if target.config_format == "toml":
        servers = raw_document.get("mcp_servers")
        if not isinstance(servers, dict):
            return None
        entry = servers.get(MCP_SERVER_NAME)
        return entry if isinstance(entry, dict) else None
    servers = raw_document.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    entry = servers.get(MCP_SERVER_NAME)
    return entry if isinstance(entry, dict) else None


def _entries_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _scan_for_secrets(payload: Any) -> None:
    text = json.dumps(payload).lower()
    for marker in _SECRET_MARKERS:
        if marker in text:
            raise McpCommandError(
                "SecretMaterialRejected",
                "Generated MCP registration must not embed secret-shaped values",
                remediation="Use named environment-variable references only (e.g. ${env:NAME}).",
            )


def _validate_entry_structure(entry: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        checks.append({
            "code": "MCP_COMMAND_MISSING",
            "status": "error",
            "message": "registration.command must be a non-empty string",
        })
    elif not Path(command).is_absolute():
        checks.append({
            "code": "MCP_COMMAND_NOT_ABSOLUTE",
            "status": "error",
            "message": f"registration.command must be absolute, got {command!r}",
            "remediation": "Re-run `recollect-lines mcp install` so paths are resolved absolutely.",
        })
    else:
        checks.append({
            "code": "MCP_COMMAND_ABSOLUTE",
            "status": "ok",
            "message": f"registration.command is absolute: {command}",
        })

    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        checks.append({
            "code": "MCP_ARGS_INVALID",
            "status": "error",
            "message": "registration.args must be an array of strings",
        })
    else:
        checks.append({
            "code": "MCP_ARGS_PRESENT",
            "status": "ok",
            "message": f"registration.args has {len(args)} element(s)",
        })

    env = entry.get("env")
    if env is not None:
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            checks.append({
                "code": "MCP_ENV_INVALID",
                "status": "error",
                "message": "registration.env must be an object of string references",
            })
        elif any(not value.startswith("${") for value in env.values()):
            checks.append({
                "code": "MCP_ENV_LITERAL_REJECTED",
                "status": "error",
                "message": "registration.env values must be named references (e.g. ${env:NAME}), never literals",
            })
        else:
            checks.append({
                "code": "MCP_ENV_REFERENCES_ONLY",
                "status": "ok",
                "message": "registration.env uses named references only",
            })

    try:
        _scan_for_secrets(entry)
        checks.append({
            "code": "MCP_NO_EMBEDDED_SECRETS",
            "status": "ok",
            "message": "registration contains no secret-shaped literals",
        })
    except McpCommandError as error:
        checks.append({
            "code": error.code,
            "status": "error",
            "message": error.message,
            "remediation": error.remediation,
        })
    return checks


def _merge_json_document(target: HostTarget, existing: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    servers = merged.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise McpCommandError(
            "AmbiguousHostConfig",
            f"{target.config_path} has a non-object mcpServers key",
            remediation="Repair the file manually before installing.",
        )
    current = servers.get(MCP_SERVER_NAME)
    if current is not None and isinstance(current, dict) and not _entries_equivalent(current, entry):
        raise McpCommandError(
            "ConflictingMcpEntry",
            f"An existing {MCP_SERVER_NAME!r} entry in {target.config_path} differs from the generated registration",
            remediation=(
                "Remove or rename the conflicting entry manually, or pass --config-path to a dedicated file."
            ),
        )
    for name, other in servers.items():
        if name == MCP_SERVER_NAME or not isinstance(other, dict):
            continue
        if other.get("command") == entry.get("command") and other.get("args") == entry.get("args"):
            raise McpCommandError(
                "AmbiguousMcpEntry",
                f"Another MCP server {name!r} already registers the same recollect-mcp invocation",
                remediation="Remove the duplicate entry or choose a different MCP server name manually.",
            )
    merged["mcpServers"] = {**servers, MCP_SERVER_NAME: entry}
    return merged


def _merge_codex_document(existing_text: str, entry: dict[str, Any]) -> str:
    existing = tomllib.loads(existing_text) if existing_text.strip() else {}
    servers = existing.get("mcp_servers")
    if servers is not None and not isinstance(servers, dict):
        raise McpCommandError(
            "AmbiguousHostConfig",
            "Codex config mcp_servers must be a table collection",
            remediation="Repair ~/.codex/config.toml manually before installing.",
        )
    current = (servers or {}).get(MCP_SERVER_NAME)
    if isinstance(current, dict) and not _entries_equivalent(current, entry):
        raise McpCommandError(
            "ConflictingMcpEntry",
            f"An existing [mcp_servers.{MCP_SERVER_NAME}] block differs from the generated registration",
            remediation="Remove or rename the conflicting block manually, or pass --config-path.",
        )
    section = _render_codex_section(entry)
    if _CODEX_SECTION_RE.search(existing_text):
        merged_text = _CODEX_SECTION_RE.sub(section, existing_text)
    elif existing_text.strip():
        merged_text = existing_text.rstrip() + "\n\n" + section
    else:
        merged_text = section
    return merged_text if merged_text.endswith("\n") else merged_text + "\n"


def _backup_path_for(config_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return config_path.with_name(f"{config_path.name}.bak-recollect-lines-{stamp}")


def run_mcp_print(
    *,
    host: str,
    scope: SupportedScope,
    home: Path,
    config_path: Path | None,
    mcp_command: str | None,
    repo_root: Path,
    user_home: Path,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    env = dict(environ if environ is not None else os.environ)
    try:
        target = resolve_host_target(
            host=host,
            scope=scope,
            config_path=config_path,
            repo_root=repo_root,
            user_home=user_home,
            environ=env,
        )
        entry = build_server_entry(home=home, mcp_command=mcp_command, environ=env, target=target)
        _scan_for_secrets(entry)
        document = _json_document_for_target(target, entry)
        rendered = _render_json_document(document) if target.config_format == "json" else _render_codex_section(entry)
        report = {
            "mcp_print_schema_version": MCP_COMMANDS_SCHEMA_VERSION,
            "package": _package_payload(),
            "host": target.host,
            "scope": target.scope,
            "config_path": str(target.config_path),
            "config_format": target.config_format,
            "server_name": MCP_SERVER_NAME,
            "registration": entry,
            "document": document if target.config_format == "json" else None,
            "rendered": rendered,
        }
        return report, 0
    except McpCommandError as error:
        return _error_report(
            "mcp_print_schema_version",
            code=error.code,
            message=error.message,
            remediation=error.remediation,
            host=host,
            scope=scope,
        ), 2


def _write_host_config(target: HostTarget, entry: dict[str, Any]) -> tuple[str, Path | None]:
    target.config_path.parent.mkdir(parents=True, exist_ok=True)
    if target.config_format == "json":
        existing = _load_json_config(target.config_path)
        merged = _merge_json_document(target, existing, entry)
        new_text = _render_json_document(merged)
        old_text = target.config_path.read_text(encoding="utf-8") if target.config_path.is_file() else ""
        if old_text == new_text:
            return "unchanged", None
        backup_path = _backup_path_for(target.config_path) if old_text else None
        if backup_path is not None:
            backup_path.write_text(old_text, encoding="utf-8")
        mode = existing_file_mode(target.config_path, default=0o600)
        write_atomic_text(target.config_path, new_text, mode=mode)
        return ("updated" if old_text else "installed"), backup_path

    old_text = target.config_path.read_text(encoding="utf-8") if target.config_path.is_file() else ""
    _load_codex_config(target.config_path)
    new_text = _merge_codex_document(old_text, entry)
    if old_text == new_text:
        return "unchanged", None
    backup_path = _backup_path_for(target.config_path) if old_text else None
    if backup_path is not None:
        backup_path.write_text(old_text, encoding="utf-8")
    mode = existing_file_mode(target.config_path, default=0o600)
    write_atomic_text(target.config_path, new_text, mode=mode)
    return ("updated" if old_text else "installed"), backup_path


def _ping_mcp_delegate(*, entry: dict[str, Any], timeout_seconds: float = 10.0) -> dict[str, Any]:
    command = [entry["command"], *entry.get("args", [])]
    env = dict(os.environ)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    started = time.monotonic()
    try:
        assert proc.stdin and proc.stdout and proc.stderr
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "recollect-lines-mcp-install-verify", "version": "0"},
        }}
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            stderr = proc.stderr.read()
            return {
                "code": "MCP_DELEGATE_PING_FAILED",
                "status": "error",
                "message": f"MCP subprocess produced no initialize response (exit={proc.poll()})",
                "details": {"stderr": stderr[:500]},
            }
        response = json.loads(line)
        server_name = ((response.get("result") or {}).get("serverInfo") or {}).get("name")
        if server_name != "recollect-lines-mcp":
            return {
                "code": "MCP_DELEGATE_PING_UNEXPECTED",
                "status": "error",
                "message": f"Expected serverInfo.name recollect-lines-mcp, got {server_name!r}",
            }
        if time.monotonic() - started > timeout_seconds:
            return {
                "code": "MCP_DELEGATE_PING_TIMEOUT",
                "status": "error",
                "message": "MCP initialize handshake exceeded verification timeout",
            }
        return {
            "code": "MCP_DELEGATE_PING_OK",
            "status": "ok",
            "message": "initialize handshake succeeded against the installed registration",
            "details": {"server_name": server_name},
        }
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        return {
            "code": "MCP_DELEGATE_PING_FAILED",
            "status": "error",
            "message": f"MCP delegate ping failed: {error}",
        }
    finally:
        if proc.stdin:
            proc.stdin.close()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        try:
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def run_mcp_verify(
    *,
    target: HostTarget,
    entry: dict[str, Any],
    home: Path,
    skip_delegate_ping: bool = False,
) -> tuple[dict[str, Any], int]:
    checks = _validate_entry_structure(entry)
    if target.config_path.is_file():
        loaded = _load_codex_config(target.config_path) if target.config_format == "toml" else _load_json_config(target.config_path)
        persisted = _existing_server_entry(target, loaded)
        if persisted is None or not _entries_equivalent(persisted, entry):
            checks.append({
                "code": "MCP_REGISTRATION_NOT_PERSISTED",
                "status": "error",
                "message": f"Expected {MCP_SERVER_NAME!r} registration was not found in {target.config_path}",
            })
        else:
            checks.append({
                "code": "MCP_REGISTRATION_PERSISTED",
                "status": "ok",
                "message": f"registration for {MCP_SERVER_NAME!r} is present in {target.config_path}",
            })
    else:
        checks.append({
            "code": "MCP_CONFIG_MISSING",
            "status": "error",
            "message": f"Host config file {target.config_path} does not exist",
        })

    doctor_report, doctor_exit = run_doctor(home=home)
    doctor_summary = {
        "exit_code": doctor_exit,
        "finding_count": len(doctor_report.get("findings", [])),
        "error_findings": [f["code"] for f in doctor_report.get("findings", []) if f.get("status") == "error"],
    }
    checks.append({
        "code": "MCP_DOCTOR_SNAPSHOT",
        "status": "ok" if doctor_exit == 0 else "warning",
        "message": f"doctor exit_code={doctor_exit} ({doctor_summary['finding_count']} finding(s))",
        "details": doctor_summary,
    })

    if not skip_delegate_ping:
        checks.append(_ping_mcp_delegate(entry=entry))

    exit_code = 1 if any(check["status"] == "error" for check in checks) else 0
    return {"checks": checks, "doctor": doctor_summary}, exit_code


def run_mcp_install(
    *,
    host: str,
    scope: SupportedScope,
    home: Path,
    config_path: Path | None,
    mcp_command: str | None,
    repo_root: Path,
    user_home: Path,
    verify: bool = True,
    skip_delegate_ping: bool = False,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    env = dict(environ if environ is not None else os.environ)
    try:
        target = resolve_host_target(
            host=host,
            scope=scope,
            config_path=config_path,
            repo_root=repo_root,
            user_home=user_home,
            environ=env,
        )
        entry = build_server_entry(home=home, mcp_command=mcp_command, environ=env, target=target)
        _scan_for_secrets(entry)
        action, backup_path = _write_host_config(target, entry)
        report: dict[str, Any] = {
            "mcp_install_schema_version": MCP_COMMANDS_SCHEMA_VERSION,
            "package": _package_payload(),
            "host": target.host,
            "scope": target.scope,
            "config_path": str(target.config_path),
            "config_format": target.config_format,
            "server_name": MCP_SERVER_NAME,
            "registration": entry,
            "action": action,
            "backup_path": str(backup_path) if backup_path is not None else None,
        }
        if verify:
            verification, verify_exit = run_mcp_verify(
                target=target,
                entry=entry,
                home=home,
                skip_delegate_ping=skip_delegate_ping,
            )
            report["verification"] = verification
            exit_code = verify_exit if action != "unchanged" or verify_exit != 0 else 0
            return report, exit_code
        return report, 0
    except McpCommandError as error:
        return _error_report(
            "mcp_install_schema_version",
            code=error.code,
            message=error.message,
            remediation=error.remediation,
            host=host,
            scope=scope,
        ), 2


def format_human_report(report: dict[str, Any], *, command: str) -> str:
    lines = [f"recollect-lines {command}", f"package: {report['package']['name']} {report['package']['version']}", ""]
    if "error" in report:
        lines.append(f"[ERROR] {report['error']['code']}: {report['error']['message']}")
        if report["error"].get("remediation"):
            lines.append(f"  remediation: {report['error']['remediation']}")
        return "\n".join(lines)

    if command.endswith("print"):
        lines.extend([
            f"host: {report['host']} ({report['scope']})",
            f"config: {report['config_path']} ({report['config_format']})",
            f"server: {report['server_name']}",
            "",
            report["rendered"].rstrip(),
        ])
        return "\n".join(lines)

    lines.extend([
        f"host: {report['host']} ({report['scope']})",
        f"config: {report['config_path']}",
        f"action: {report['action']}",
    ])
    if report.get("backup_path"):
        lines.append(f"backup: {report['backup_path']}")
    verification = report.get("verification")
    if verification:
        lines.append("")
        for check in verification["checks"]:
            lines.append(f"[{check['status'].upper()}] {check['code']}: {check['message']}")
            if check.get("remediation"):
                lines.append(f"  remediation: {check['remediation']}")
    return "\n".join(lines)
