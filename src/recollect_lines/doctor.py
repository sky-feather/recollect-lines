"""Offline-safe operational diagnostics (Phase 7A).

Runs local-only checks: package metadata, filesystem usability, CLI adapter
probes, provider configuration validity, credential-reference presence (never
values), and capability inventory consistency. Does not perform remote HTTP
connectivity probes or paid API calls.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .claude_code_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter
from .cursor_adapter import CursorAdapter
from .discovery import discover_providers, discover_runtimes, probe_cli_version, provider_config_lifecycle
from .direct_api_runtime import DIRECT_API_PROFILE, OpenAiCompatibleDirectRuntime
from .runtime_registry import DEFAULT_RUNTIME_REGISTRY
from .opencode_adapter import OpenCodeAdapter
from .providers import (
    MissingCredentialReference,
    ProviderConfigError,
    load_providers_config,
    provider_config_format,
    resolve_api_key,
)
from .recovery_contract import ControlAction, RecoveryLevel
from .service import Broker

_ADAPTER_COMMAND_FLAGS = {
    "opencode": "--opencode-command",
    "claude_code": "--claude-command",
    "codex": "--codex-command",
    "cursor": "--cursor-command",
}

FindingSeverity = Literal["info", "warning", "error"]
FindingStatus = Literal["ok", "warning", "error", "not_checked"]

DOCTOR_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: FindingSeverity
    status: FindingStatus
    message: str
    remediation: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "status": self.status,
            "message": self.message,
        }
        if self.remediation is not None:
            payload["remediation"] = self.remediation
        if self.details is not None:
            payload["details"] = self.details
        return payload


def _finding(
    code: str,
    *,
    severity: FindingSeverity,
    status: FindingStatus,
    message: str,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
) -> Finding:
    return Finding(code, severity, status, message, remediation, details)


def _check_home_directory(home: Path) -> list[Finding]:
    findings: list[Finding] = []
    if home.exists() and not home.is_dir():
        findings.append(_finding(
            "HOME_NOT_DIRECTORY",
            severity="error",
            status="error",
            message=f"Broker home {home} exists but is not a directory",
            remediation="Remove or rename the conflicting path, or choose a different --home directory.",
        ))
        return findings
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor_write_probe"
        probe.write_text("ok\n")
        probe.unlink()
        findings.append(_finding(
            "HOME_WRITABLE",
            severity="info",
            status="ok",
            message=f"Broker home {home} is writable",
            details={"path": str(home.resolve())},
        ))
    except OSError as error:
        findings.append(_finding(
            "HOME_NOT_WRITABLE",
            severity="error",
            status="error",
            message=f"Broker home {home} is not writable: {error}",
            remediation="Choose a writable --home directory or fix filesystem permissions.",
            details={"path": str(home)},
        ))
    return findings


def _check_workspace(workspace: Path | None) -> list[Finding]:
    if workspace is None:
        return [_finding(
            "WORKSPACE_NOT_SPECIFIED",
            severity="info",
            status="not_checked",
            message="No --workspace was provided; workspace usability was not checked",
            remediation="Pass --workspace /path/to/repo when validating a deployment workspace.",
        )]
    if not workspace.exists():
        return [_finding(
            "WORKSPACE_MISSING",
            severity="error",
            status="error",
            message=f"Workspace {workspace} does not exist",
            remediation="Create the workspace directory or pass an existing repository path.",
            details={"path": str(workspace)},
        )]
    if not workspace.is_dir():
        return [_finding(
            "WORKSPACE_NOT_DIRECTORY",
            severity="error",
            status="error",
            message=f"Workspace {workspace} is not a directory",
            remediation="Pass a directory path for --workspace.",
            details={"path": str(workspace)},
        )]
    if not os.access(workspace, os.R_OK | os.X_OK):
        return [_finding(
            "WORKSPACE_NOT_ACCESSIBLE",
            severity="error",
            status="error",
            message=f"Workspace {workspace} is not readable/executable by this user",
            remediation="Fix workspace permissions or choose a different path.",
            details={"path": str(workspace.resolve())},
        )]
    return [_finding(
        "WORKSPACE_ACCESSIBLE",
        severity="info",
        status="ok",
        message=f"Workspace {workspace} is accessible",
        details={"path": str(workspace.resolve())},
    )]


def _check_runtime_adapters(broker: Broker) -> list[Finding]:
    findings: list[Finding] = []
    for profile_name, adapter in broker.subprocess_adapters.items():
        probe = getattr(adapter, "check_availability", None)
        if callable(probe):
            result = probe()
        else:
            command_prefix = getattr(adapter, "command_prefix", ())
            result = probe_cli_version(tuple(command_prefix)) if command_prefix else {
                "available": None, "reason": "not_probed",
            }
        available = result.get("available")
        command = getattr(adapter, "command_prefix", ())
        binary = command[0] if command else profile_name
        if available is True:
            findings.append(_finding(
                "RUNTIME_CLI_AVAILABLE",
                severity="info",
                status="ok",
                message=f"Runtime adapter {profile_name!r} CLI is available locally",
                details={
                    "profile": profile_name,
                    "command_prefix": list(command),
                    "observed": {k: v for k, v in result.items() if k != "detail" or len(str(v)) <= 200},
                },
            ))
        elif available is False:
            reason = result.get("reason", "unavailable")
            findings.append(_finding(
                "RUNTIME_CLI_UNAVAILABLE",
                severity="warning",
                status="warning",
                message=f"Runtime adapter {profile_name!r} CLI is not available ({reason})",
                remediation=(
                    f"Install {binary!r} on PATH or override with "
                    f"{_ADAPTER_COMMAND_FLAGS.get(profile_name, '--<adapter>-command')} "
                    "when starting the broker."
                ),
                details={
                    "profile": profile_name,
                    "command_prefix": list(command),
                    "observed_reason": reason,
                },
            ))
        else:
            findings.append(_finding(
                "RUNTIME_CLI_NOT_PROBED",
                severity="info",
                status="not_checked",
                message=f"Runtime adapter {profile_name!r} has no local availability probe",
                details={"profile": profile_name},
            ))
    return findings


def check_providers_config(
    providers_config: Path | None,
    environ: dict[str, str],
    *,
    providers_config_origin: str | None = None,
) -> tuple[list[Finding], OpenAiCompatibleDirectRuntime | None]:
    if providers_config is None:
        return [_finding(
            "PROVIDERS_CONFIG_NOT_SPECIFIED",
            severity="info",
            status="not_checked",
            message=(
                "No provider configuration was found (--providers-config, RECOLLECT_CONFIG, "
                "repo-local/user-level operator config, and the legacy providers.json default "
                "were all absent); named provider checks were skipped"
            ),
            remediation="Pass --providers-config /path/to/providers.json (or .yaml) to validate direct-API providers.",
        )], None

    if not providers_config.exists():
        return [_finding(
            "PROVIDERS_CONFIG_MISSING",
            severity="error",
            status="error",
            message=f"Provider configuration file {providers_config} does not exist",
            remediation="Create the file from examples/ or fix the --providers-config path.",
            details={"path": str(providers_config)},
        )], None

    try:
        providers = load_providers_config(providers_config)
    except ProviderConfigError as error:
        return [_finding(
            "PROVIDERS_CONFIG_INVALID",
            severity="error",
            status="error",
            message=f"Provider configuration is invalid: {error}",
            remediation="Fix the JSON/YAML syntax and provider fields; run recollect-lines doctor again.",
            details={"path": str(providers_config)},
        )], None

    runtime = OpenAiCompatibleDirectRuntime(
        providers, environ=environ, config_source=providers_config,
        config_source_origin=providers_config_origin,
    )
    findings: list[Finding] = [
        _finding(
            "PROVIDERS_CONFIG_VALID",
            severity="info",
            status="ok",
            message=f"Provider configuration {providers_config} parsed successfully",
            details={"path": str(providers_config.resolve()), "provider_count": len(providers)},
        ),
    ]
    if provider_config_format(providers_config) == "json":
        findings.append(_finding(
            "PROVIDERS_CONFIG_LEGACY_JSON_FORMAT",
            severity="info",
            status="ok",
            message=f"Provider configuration {providers_config} uses the legacy JSON format",
            remediation=(
                "JSON remains fully supported. Optional: rewrite as YAML (same schema) for "
                "easier hand-editing."
            ),
            details={"path": str(providers_config)},
        ))

    for name, config in sorted(providers.items()):
        try:
            resolve_api_key(config, environ)
            findings.append(_finding(
                "PROVIDER_SECRET_REFERENCE_PRESENT",
                severity="info",
                status="ok",
                message=f"Provider {name!r} credential reference {config.api_key_env!r} is set",
                details={"provider": name, "credential_reference": config.api_key_env},
            ))
        except MissingCredentialReference as error:
            findings.append(_finding(
                "PROVIDER_SECRET_REFERENCE_MISSING",
                severity="warning",
                status="warning",
                message=f"Provider {name!r}: {error}",
                remediation=(
                    f"Export {config.api_key_env!r} in the broker/MCP environment before using "
                    f"profile {DIRECT_API_PROFILE!r} with provider {name!r}."
                ),
                details={"provider": name, "credential_reference": config.api_key_env},
            ))

        findings.append(_finding(
            "PROVIDER_ENDPOINT_POLICY_VALID",
            severity="info",
            status="ok",
            message=(
                f"Provider {name!r} endpoint/TLS policy passed local validation "
                f"(connectivity not probed)"
            ),
            details={
                "provider": name,
                "scheme": config.base_url.split("://", 1)[0],
                "tls_verify": config.tls_verify,
                "allow_insecure_http": config.allow_insecure_http,
                "connectivity_checked": False,
            },
        ))

        declared = config.capabilities
        if not declared.chat_completions:
            findings.append(_finding(
                "PROVIDER_CAPABILITY_INCONSISTENT",
                severity="error",
                status="error",
                message=f"Provider {name!r} declares chat_completions=false (invalid for openai-compatible)",
                remediation="Set capabilities.chat_completions to true or remove the provider.",
                details={"provider": name},
            ))

    return findings, runtime


def _check_provider_config_lifecycle(runtime: OpenAiCompatibleDirectRuntime | None) -> Finding:
    lifecycle = provider_config_lifecycle(runtime)
    configured = lifecycle["source"] != "not_configured"
    message = (
        f"Active provider configuration loaded from {lifecycle['source']} at {lifecycle['loaded_at']}"
        if configured
        else "No provider configuration file is active for this process (source: not_configured)"
    )
    return _finding(
        "PROVIDER_CONFIG_LIFECYCLE",
        severity="info",
        status="ok" if configured else "not_checked",
        message=message,
        remediation=lifecycle["note"],
        details=lifecycle,
    )


def _check_inventory_consistency(broker: Broker, environ: dict[str, str]) -> list[Finding]:
    runtimes = discover_runtimes(
        registry=broker.runtime_registry,
        subprocess_adapters=broker.subprocess_adapters,
        mock_adapter=broker.adapter,
        direct_api_runtime=broker.direct_api_runtime,
        include_compatibility_evidence=False,
    )
    providers = discover_providers(
        direct_api_runtime=broker.direct_api_runtime,
        environ=environ,
    )
    findings: list[Finding] = [
        _finding(
            "CAPABILITY_INVENTORY_AVAILABLE",
            severity="info",
            status="ok",
            message="Runtime and provider capability inventory is consistent",
            details={
                "runtime_count": len(runtimes),
                "provider_count": len(providers),
                "note": "Declared capabilities are distinct from observed remote availability.",
            },
        ),
    ]
    profile_names = {entry["name"] for entry in runtimes}
    for expected in DEFAULT_RUNTIME_REGISTRY.names():
        if expected not in profile_names:
            findings.append(_finding(
                "INVENTORY_PROFILE_MISSING",
                severity="error",
                status="error",
                message=f"Expected runtime {expected!r} missing from capability inventory",
                remediation="Report as a packaging bug; runtime registry and discovery are out of sync.",
                details={"profile": expected},
            ))
    for entry in runtimes:
        recovery = entry.get("recovery_control")
        if not isinstance(recovery, dict) or "declared" not in recovery:
            findings.append(_finding(
                "RECOVERY_CONTRACT_MISSING",
                severity="error",
                status="error",
                message=f"Runtime {entry['name']!r} is missing recovery_control contract",
                remediation="Report as a packaging bug; discovery must expose declared recovery/control.",
                details={"profile": entry["name"]},
            ))
            continue
        declared = recovery["declared"]
        level = declared.get("recovery_level")
        if level not in {item.value for item in RecoveryLevel}:
            findings.append(_finding(
                "RECOVERY_CONTRACT_INVALID",
                severity="error",
                status="error",
                message=f"Runtime {entry['name']!r} has invalid recovery_level {level!r}",
                details={"profile": entry["name"], "recovery_level": level},
            ))
        if ControlAction.MESSAGE.value not in declared.get("unsupported_control_actions", []):
            findings.append(_finding(
                "RECOVERY_CONTRACT_MESSAGE_NOT_EXCLUDED",
                severity="error",
                status="error",
                message=f"Runtime {entry['name']!r} must declare message control unsupported",
                details={"profile": entry["name"]},
            ))
        if recovery.get("provider_native_session_resume") != "unproven":
            findings.append(_finding(
                "RECOVERY_CONTRACT_OVERCLAIM",
                severity="error",
                status="error",
                message=f"Runtime {entry['name']!r} must not claim provider-native session resume without proof",
                details={"profile": entry["name"]},
            ))
        availability = entry.get("observed_availability", {})
        if availability.get("available") is False:
            findings.append(_finding(
                "RECOVERY_OBSERVED_LOCAL_UNAVAILABLE",
                severity="warning",
                status="warning",
                message=(
                    f"Runtime {entry['name']!r} CLI is not available on this host "
                    f"({availability.get('reason', 'unavailable')}) — local observation only"
                ),
                remediation=(
                    "Install the CLI on PATH or override the adapter command flag. "
                    "This is not a global capability verdict."
                ),
                details={
                    "profile": entry["name"],
                    "observed_reason": availability.get("reason"),
                    "declared_recovery_level": level,
                },
            ))
    findings.append(_finding(
        "RECOVERY_CONTRACT_INVENTORY",
        severity="info",
        status="ok",
        message="Declared recovery/control contracts surfaced for all runtimes",
        details={
            "runtime_contracts": {
                entry["name"]: entry.get("recovery_control", {}).get("declared")
                for entry in runtimes
            },
            "note": (
                "Declared recovery/control differs from observed local executable availability "
                "and from unproven provider-native resume behavior."
            ),
        },
    ))
    return findings


def _check_mcp_prerequisites() -> list[Finding]:
    findings: list[Finding] = []
    if shutil.which("recollect-mcp") is not None:
        findings.append(_finding(
            "MCP_CONSOLE_SCRIPT_AVAILABLE",
            severity="info",
            status="ok",
            message="recollect-mcp console script is available on PATH",
        ))
    else:
        findings.append(_finding(
            "MCP_CONSOLE_SCRIPT_NOT_ON_PATH",
            severity="info",
            status="not_checked",
            message="recollect-mcp console script is not on PATH in this environment",
            remediation="After pip install ., recollect-mcp should be on PATH for MCP hosts.",
        ))
    findings.append(_finding(
        "MCP_STDIO_TRANSPORT",
        severity="info",
        status="ok",
        message="MCP server uses local stdio JSON-RPC (no network listener required)",
        details={"server_name": "recollect-lines-mcp"},
    ))
    return findings


def _aggregate_status(findings: list[Finding]) -> tuple[str, int, dict[str, int]]:
    counts = {"blocking": 0, "warning": 0, "info": 0, "not_checked": 0}
    for finding in findings:
        if finding.status == "error":
            counts["blocking"] += 1
        elif finding.status == "warning":
            counts["warning"] += 1
        elif finding.status == "not_checked":
            counts["not_checked"] += 1
        else:
            counts["info"] += 1
    if counts["blocking"]:
        overall = "blocking"
        exit_code = 1
    elif counts["warning"]:
        overall = "degraded"
        exit_code = 0
    else:
        overall = "ok"
        exit_code = 0
    return overall, exit_code, counts


def run_doctor(
    *,
    home: Path,
    workspace: Path | None = None,
    providers_config: Path | None = None,
    providers_config_origin: str | None = None,
    opencode_adapter: OpenCodeAdapter | None = None,
    claude_code_adapter: ClaudeCodeAdapter | None = None,
    codex_adapter: CodexAdapter | None = None,
    cursor_adapter: CursorAdapter | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Run offline diagnostics and return (report_dict, exit_code)."""
    env = dict(environ if environ is not None else os.environ)
    findings: list[Finding] = [
        _finding(
            "PACKAGE_VERSION",
            severity="info",
            status="ok",
            message=f"recollect-lines {__version__}",
            details={"name": "recollect-lines", "version": __version__, "python": sys.version.split()[0]},
        ),
    ]
    findings.extend(_check_home_directory(home))
    findings.extend(_check_workspace(workspace))

    provider_findings, direct_runtime = check_providers_config(
        providers_config, env, providers_config_origin=providers_config_origin,
    )
    findings.extend(provider_findings)
    findings.append(_check_provider_config_lifecycle(direct_runtime))

    broker = Broker(
        home,
        opencode_adapter=opencode_adapter,
        claude_code_adapter=claude_code_adapter,
        codex_adapter=codex_adapter,
        cursor_adapter=cursor_adapter,
        direct_api_runtime=direct_runtime,
        environ=env,
    )
    try:
        findings.extend(_check_runtime_adapters(broker))
        findings.extend(_check_inventory_consistency(broker, env))
    finally:
        broker.close()

    findings.extend(_check_mcp_prerequisites())
    findings.append(_finding(
        "ENDPOINT_CONNECTIVITY_NOT_CHECKED",
        severity="info",
        status="not_checked",
        message="Remote provider endpoint reachability was not checked (offline-safe default)",
        remediation=(
            "Phase 7A does not probe network connectivity. Confirm endpoints manually "
            "or wait for a later phase with explicit opt-in connectivity checks."
        ),
        details={"providers_configured": providers_config is not None},
    ))
    overall, exit_code, counts = _aggregate_status(findings)
    report = {
        "doctor_schema_version": DOCTOR_SCHEMA_VERSION,
        "package": {"name": "recollect-lines", "version": __version__},
        "status": overall,
        "summary": counts,
        "findings": [finding.to_dict() for finding in findings],
    }
    return report, exit_code


def run_config_validate(
    *,
    providers_config: Path | None,
    providers_config_origin: str | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Focused, offline-safe provider-config validation/inspection.

    Reports the resolved source and validation result only (no runtime
    adapter probing). Never prints credential values -- only whether each
    provider's credential reference is present in the environment.
    """
    env = dict(environ if environ is not None else os.environ)
    provider_findings, direct_runtime = check_providers_config(
        providers_config, env, providers_config_origin=providers_config_origin,
    )
    findings = list(provider_findings)
    findings.append(_check_provider_config_lifecycle(direct_runtime))
    overall, exit_code, counts = _aggregate_status(findings)
    report = {
        "doctor_schema_version": DOCTOR_SCHEMA_VERSION,
        "package": {"name": "recollect-lines", "version": __version__},
        "status": overall,
        "summary": counts,
        "findings": [finding.to_dict() for finding in findings],
    }
    return report, exit_code


def format_human_report(report: dict[str, Any], *, command: str = "doctor") -> str:
    lines = [
        f"recollect-lines {command} — status: {report['status']}",
        f"package: {report['package']['name']} {report['package']['version']}",
        "",
    ]
    for finding in report["findings"]:
        prefix = finding["status"].upper()
        lines.append(f"[{prefix}] {finding['code']}: {finding['message']}")
        if finding.get("remediation"):
            lines.append(f"  remediation: {finding['remediation']}")
    lines.append("")
    summary = report["summary"]
    lines.append(
        "summary: "
        f"{summary['blocking']} blocking, {summary['warning']} warning, "
        f"{summary['info']} ok/info, {summary['not_checked']} not checked"
    )
    return "\n".join(lines)
