"""Explicit integration-certification harness (Phase 7B).

Produces structured, redacted evidence for operator-approved targets. Default
mode is offline dry-run (no remote HTTP, no model CLI invocation). Live
execution is strongly opt-in; local fixture execution proves the executed path
without claiming external provider certification.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .claude_code_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter
from .cursor_adapter import CursorAdapter
from .direct_api_runtime import DIRECT_API_PROFILE, OpenAiCompatibleDirectRuntime
from .discovery import probe_cli_version
from .models import TaskRecord, TaskRequest
from .runtime_registry import DEFAULT_RUNTIME_REGISTRY, ExecutionStrategy
from .opencode_adapter import OpenCodeAdapter
from .providers import (
    MissingCredentialReference,
    ProviderConfig,
    ProviderConfigError,
    load_providers_config,
    redact_provider_error,
    resolve_api_key,
)

CERTIFY_SCHEMA_VERSION = "1"
CERTIFICATION_PROMPT = "recollect-lines certification probe: respond with the single word OK"
FIXTURE_EVIDENCE_CLASS = "local_fixture"
LIVE_EVIDENCE_CLASS = "live_remote"
DRY_RUN_EVIDENCE_CLASS = "local_dry_run"

ExecutionOutcome = Literal["dry_run", "blocked", "executed"]
TargetKind = Literal["cli_adapter", "direct_api", "synthetic"]


def _target_kind(profile: str, *, registry=DEFAULT_RUNTIME_REGISTRY) -> TargetKind:
    if not registry.contains(profile):
        return "synthetic"
    strategy = registry.get(profile).execution_strategy
    if strategy is ExecutionStrategy.SYNTHETIC:
        return "synthetic"
    if strategy is ExecutionStrategy.DIRECT_API:
        return "direct_api"
    return "cli_adapter"


@dataclass(frozen=True)
class CertifyRequest:
    home: Path
    profile: str
    provider: str | None
    providers_config: Path | None
    output: Path | None
    max_cost_usd: float | None
    execute_live: bool
    acknowledge_billed_remote_calls: bool
    fixture_execute: bool
    opencode_adapter: OpenCodeAdapter | None = None
    claude_code_adapter: ClaudeCodeAdapter | None = None
    codex_adapter: CodexAdapter | None = None
    cursor_adapter: CursorAdapter | None = None
    environ: dict[str, str] | None = None
    fixture_runtime: OpenAiCompatibleDirectRuntime | None = None


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _check(
    code: str,
    *,
    status: str,
    message: str,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "status": status,
        "message": message,
    }
    if remediation is not None:
        payload["remediation"] = remediation
    if details is not None:
        payload["details"] = details
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    return payload


def _declared_capabilities(profile: str, provider_config: ProviderConfig | None, *, registry=DEFAULT_RUNTIME_REGISTRY) -> dict[str, Any]:
    if provider_config is not None:
        caps = provider_config.capabilities
        return {
            "chat_completions": caps.chat_completions,
            "structured_output": caps.structured_output,
            "streaming": caps.streaming,
            "tool_calls": caps.tool_calls,
            "workspace_access": caps.workspace_access,
            "process_cancellation": caps.process_cancellation,
        }
    if not registry.contains(profile):
        return {}
    descriptor = registry.get(profile)
    policy = descriptor.policy
    modes = policy.allowed_modes
    caps = descriptor.adapter_capabilities
    return {
        "read_only_execution": "read_only" in modes,
        "isolated_worktree": "isolated_worktree" in modes,
        "subprocess_supervision": caps.requires_subprocess,
        "model_selection": descriptor.model_selection.value,
    }


def _adapter_for_profile(request: CertifyRequest) -> Any | None:
    mapping = {
        "opencode": request.opencode_adapter or OpenCodeAdapter(),
        "claude_code": request.claude_code_adapter or ClaudeCodeAdapter(),
        "codex": request.codex_adapter or CodexAdapter(),
        "cursor": request.cursor_adapter or CursorAdapter(),
    }
    return mapping.get(request.profile)


def _safe_provider_identity(config: ProviderConfig) -> dict[str, Any]:
    scheme = config.base_url.split("://", 1)[0]
    return {
        "provider": config.name,
        "kind": config.kind,
        "base_url_scheme": scheme,
        "default_model": config.default_model,
        "credential_reference": config.api_key_env,
        "tls_verify": config.tls_verify,
        "allow_insecure_http": config.allow_insecure_http,
        "request_timeout_seconds": config.request_timeout_seconds,
        "estimated_cost_usd_upper_bound": config.estimated_cost_usd_upper_bound,
    }


def _config_fingerprint(providers_config: Path | None, provider_name: str | None) -> dict[str, Any]:
    if providers_config is None:
        return {"providers_config": None, "provider": provider_name}
    try:
        data = json.loads(providers_config.read_text())
    except (OSError, json.JSONDecodeError):
        return {"providers_config": str(providers_config), "provider": provider_name, "parseable": False}
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict) or provider_name not in providers:
        digest = hashlib.sha256(providers_config.read_bytes()).hexdigest()[:16]
        return {"providers_config": str(providers_config.resolve()), "sha256_prefix": digest, "provider": provider_name}
    entry = dict(providers[provider_name])
    entry.pop("api_key_env", None)
    canonical = json.dumps({"providers": {provider_name: entry}}, sort_keys=True)
    return {
        "providers_config": str(providers_config.resolve()),
        "provider": provider_name,
        "sha256_prefix": hashlib.sha256(canonical.encode()).hexdigest()[:16],
    }


def _resolve_mode(request: CertifyRequest) -> tuple[str, list[dict[str, Any]], ExecutionOutcome | None]:
    checks: list[dict[str, Any]] = []
    if request.execute_live and request.fixture_execute:
        checks.append(_check(
            "MUTUALLY_EXCLUSIVE_EXECUTION_FLAGS",
            status="error",
            message="--execute-live and --fixture-execute cannot be used together",
            remediation="Choose dry-run (default), --fixture-execute, or --execute-live.",
        ))
        return "blocked", checks, "blocked"
    if request.execute_live:
        return "live_execute", checks, None
    if request.fixture_execute:
        return "fixture_execute", checks, None
    return "dry_run", checks, None


def _validate_selection(request: CertifyRequest, checks: list[dict[str, Any]]) -> ExecutionOutcome | None:
    if not request.profile or not str(request.profile).strip():
        checks.append(_check(
            "TARGET_NOT_SELECTED",
            status="error",
            message="Explicit --profile is required (no default target)",
            remediation="Pass --profile <name> and --provider <name> when profile is openai_compatible.",
        ))
        return "blocked"
    if not DEFAULT_RUNTIME_REGISTRY.contains(request.profile):
        checks.append(_check(
            "PROFILE_UNKNOWN",
            status="error",
            message=f"Unknown profile {request.profile!r}",
            remediation=f"Choose one of: {', '.join(DEFAULT_RUNTIME_REGISTRY.names())}.",
        ))
        return "blocked"
    profile_descriptor = DEFAULT_RUNTIME_REGISTRY.get(request.profile)
    if profile_descriptor.execution_strategy is ExecutionStrategy.DIRECT_API:
        if not request.provider:
            checks.append(_check(
                "PROVIDER_NOT_SELECTED",
                status="error",
                message=f"Profile {request.profile!r} requires explicit --provider",
                remediation="Pass --provider <name> matching an entry in --providers-config.",
            ))
            return "blocked"
        if request.providers_config is None:
            checks.append(_check(
                "PROVIDERS_CONFIG_REQUIRED",
                status="error",
                message="--providers-config is required for openai_compatible certification",
                remediation="Pass --providers-config /path/to/providers.json.",
            ))
            return "blocked"
    checks.append(_check(
        "TARGET_SELECTED",
        status="ok",
        message=f"Certification target profile={request.profile!r}"
        + (f" provider={request.provider!r}" if request.provider else ""),
        details={"profile": request.profile, "provider": request.provider, "kind": _target_kind(request.profile)},
    ))
    return None


def _load_provider(
    request: CertifyRequest,
    checks: list[dict[str, Any]],
    env: dict[str, str],
) -> tuple[ProviderConfig | None, OpenAiCompatibleDirectRuntime | None, ExecutionOutcome | None]:
    if DEFAULT_RUNTIME_REGISTRY.get(request.profile).execution_strategy is not ExecutionStrategy.DIRECT_API:
        return None, request.fixture_runtime, None
    assert request.providers_config is not None
    assert request.provider is not None
    if not request.providers_config.exists():
        checks.append(_check(
            "PROVIDERS_CONFIG_MISSING",
            status="error",
            message=f"Provider configuration file {request.providers_config} does not exist",
            remediation="Create the file or fix --providers-config.",
        ))
        return None, None, "blocked"
    try:
        providers = load_providers_config(request.providers_config)
    except ProviderConfigError as error:
        checks.append(_check(
            "PROVIDERS_CONFIG_INVALID",
            status="error",
            message=f"Provider configuration is invalid: {error}",
            remediation="Fix JSON syntax and provider fields.",
        ))
        return None, None, "blocked"
    config = providers.get(request.provider)
    if config is None:
        checks.append(_check(
            "PROVIDER_UNKNOWN",
            status="error",
            message=f"Provider {request.provider!r} not found in configuration",
            remediation="Use a provider name declared in --providers-config.",
        ))
        return None, None, "blocked"
    runtime = request.fixture_runtime or OpenAiCompatibleDirectRuntime(
        providers, environ=env, config_source=request.providers_config,
    )
    checks.append(_check(
        "PROVIDER_CONFIG_VALID",
        status="ok",
        message=f"Provider {config.name!r} passed local configuration validation",
        details=_safe_provider_identity(config),
    ))
    checks.append(_check(
        "ENDPOINT_TLS_POLICY_VALID",
        status="ok",
        message="Endpoint/TLS policy passed local validation (remote reachability not observed)",
        details={
            "connectivity_checked": False,
            "declared_not_observed": True,
            "scheme": config.base_url.split("://", 1)[0],
            "tls_verify": config.tls_verify,
            "allow_insecure_http": config.allow_insecure_http,
        },
    ))
    try:
        resolve_api_key(config, env)
        checks.append(_check(
            "CREDENTIAL_REFERENCE_PRESENT",
            status="ok",
            message=f"Credential reference {config.api_key_env!r} is set",
            details={"credential_reference": config.api_key_env},
        ))
    except MissingCredentialReference as error:
        severity = "error" if request.fixture_execute or request.execute_live else "warning"
        checks.append(_check(
            "CREDENTIAL_REFERENCE_MISSING",
            status=severity,
            message=str(error),
            remediation=f"Export {config.api_key_env!r} before live or fixture execution.",
        ))
        if request.fixture_execute or request.execute_live:
            return config, runtime, "blocked"
    return config, runtime, None


def _validate_live_gates(
    request: CertifyRequest,
    provider_config: ProviderConfig | None,
    checks: list[dict[str, Any]],
) -> ExecutionOutcome | None:
    if not request.execute_live:
        return None
    if not request.acknowledge_billed_remote_calls:
        checks.append(_check(
            "LIVE_ACKNOWLEDGEMENT_REQUIRED",
            status="error",
            message="Live execution requires --i-accept-billed-remote-calls",
            remediation=(
                "Re-run with --execute-live --i-accept-billed-remote-calls only when you "
                "intend paid/billed remote model calls under an approved non-production profile."
            ),
        ))
        return "blocked"
    if request.max_cost_usd is None or request.max_cost_usd <= 0:
        checks.append(_check(
            "LIVE_BUDGET_REQUIRED",
            status="error",
            message="Live execution requires a positive --max-cost-usd budget",
            remediation="Pass --max-cost-usd <positive_number> within your approved spend limit.",
        ))
        return "blocked"
    if DEFAULT_RUNTIME_REGISTRY.get(request.profile).execution_strategy is ExecutionStrategy.SUBPROCESS_CLI:
        checks.append(_check(
            "LIVE_CLI_NOT_SUPPORTED",
            status="error",
            message="Live CLI adapter certification is not supported in Phase 7B",
            remediation=(
                "Use --profile openai_compatible with a named provider for live checks, "
                "or --fixture-execute for deterministic local proof."
            ),
        ))
        return "blocked"
    if provider_config is None:
        return "blocked"
    bound = provider_config.estimated_cost_usd_upper_bound
    if bound is None or bound <= 0:
        checks.append(_check(
            "PROVIDER_COST_BOUND_MISSING",
            status="error",
            message=(
                f"Provider {provider_config.name!r} lacks factual estimated_cost_usd_upper_bound metadata"
            ),
            remediation=(
                "Add estimated_cost_usd_upper_bound to the provider entry before live certification."
            ),
            details={"provider": provider_config.name},
        ))
        return "blocked"
    if request.max_cost_usd < bound:
        checks.append(_check(
            "LIVE_BUDGET_BELOW_PROVIDER_BOUND",
            status="error",
            message=(
                f"--max-cost-usd {request.max_cost_usd} is below provider bound {bound}"
            ),
            remediation="Increase --max-cost-usd to at least the provider's configured upper bound.",
            details={"max_cost_usd": request.max_cost_usd, "provider_bound": bound},
        ))
        return "blocked"
    checks.append(_check(
        "LIVE_BUDGET_ACCEPTED",
        status="ok",
        message="Operator budget and provider cost bound accepted",
        details={"max_cost_usd": request.max_cost_usd, "provider_bound": bound},
    ))
    return None


def _dry_run_checks(
    request: CertifyRequest,
    provider_config: ProviderConfig | None,
    checks: list[dict[str, Any]],
) -> None:
    profile_descriptor = DEFAULT_RUNTIME_REGISTRY.get(request.profile)
    checks.append(_check(
        "DECLARED_CAPABILITIES",
        status="ok",
        message="Declared capabilities recorded (not remote availability)",
        details={"capabilities": _declared_capabilities(request.profile, provider_config)},
    ))
    if profile_descriptor.execution_strategy is ExecutionStrategy.SUBPROCESS_CLI:
        checks.append(_check(
            "CLI_INVOCATION_SKIPPED",
            status="ok",
            message="Dry-run did not invoke model CLI binaries",
            details={"profile": request.profile},
        ))
    if profile_descriptor.execution_strategy is ExecutionStrategy.DIRECT_API:
        checks.append(_check(
            "REMOTE_REQUEST_SKIPPED",
            status="ok",
            message="Dry-run did not send HTTP requests to provider endpoints",
            details={"declared_not_observed": True},
        ))
    checks.append(_check(
        "REMOTE_AVAILABILITY_NOT_CHECKED",
        status="not_checked",
        message="Configured/declared configuration is not observed remote availability",
        remediation="Use --fixture-execute for local proof or --execute-live with explicit opt-in later.",
    ))


def _execute_fixture_direct_api(
    runtime: OpenAiCompatibleDirectRuntime,
    provider_config: ProviderConfig,
    env: dict[str, str],
    checks: list[dict[str, Any]],
) -> tuple[ExecutionOutcome, list[dict[str, Any]]]:
    started = time.monotonic()
    record = TaskRecord.new(TaskRequest(
        CERTIFICATION_PROMPT,
        "/certification-fixture",
        execution_mode="read_only",
        profile=DIRECT_API_PROFILE,
        provider=provider_config.name,
        timeout_seconds=provider_config.request_timeout_seconds,
        verification_policy="none",
    ))
    artifacts_dir = Path("/tmp/recollect-lines-certify-fixture") / record.id
    try:
        api_key = resolve_api_key(provider_config, env)
        _metadata, handle = runtime.start(record, artifacts_dir)
        result = runtime.collect(handle, wait_timeout=provider_config.request_timeout_seconds + 5.0)
        duration_ms = int((time.monotonic() - started) * 1000)
        if result.get("exit_code") == 0 and result.get("summary"):
            checks.append(_check(
                "FIXTURE_DIRECT_API_EXECUTED",
                status="ok",
                message="Local fixture direct-API certification completed",
                details={
                    "http_status": result.get("http_status"),
                    "provider": provider_config.name,
                    "model": provider_config.default_model,
                    "summary_redacted": True,
                    "not_external_provider_certification": True,
                },
                duration_ms=duration_ms,
            ))
            return "executed", checks
        checks.append(_check(
            "FIXTURE_DIRECT_API_FAILED",
            status="error",
            message=redact_provider_error(
                str(result.get("error_message") or result.get("error_category") or "fixture execution failed"),
                api_key,
            ),
            details={"error_category": result.get("error_category"), "http_status": result.get("http_status")},
            duration_ms=duration_ms,
        ))
        return "blocked", checks
    except Exception as error:
        duration_ms = int((time.monotonic() - started) * 1000)
        checks.append(_check(
            "FIXTURE_DIRECT_API_ERROR",
            status="error",
            message=redact_provider_error(str(error)),
            duration_ms=duration_ms,
        ))
        return "blocked", checks
    finally:
        if artifacts_dir.exists():
            for child in sorted(artifacts_dir.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
            for child in sorted(artifacts_dir.rglob("*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
            artifacts_dir.rmdir()
            parent = artifacts_dir.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()


def _execute_fixture_cli(
    request: CertifyRequest,
    checks: list[dict[str, Any]],
) -> tuple[ExecutionOutcome, list[dict[str, Any]]]:
    adapter = _adapter_for_profile(request)
    if adapter is None:
        checks.append(_check(
            "FIXTURE_CLI_UNSUPPORTED",
            status="error",
            message=f"Fixture execution is not supported for profile {request.profile!r}",
        ))
        return "blocked", checks
    started = time.monotonic()
    command_prefix = getattr(adapter, "command_prefix", ())
    probe = probe_cli_version(tuple(command_prefix))
    duration_ms = int((time.monotonic() - started) * 1000)
    if probe.get("available"):
        checks.append(_check(
            "FIXTURE_CLI_EXECUTED",
            status="ok",
            message="Local fixture CLI availability probe completed (not external provider certification)",
            details={
                "profile": request.profile,
                "command_prefix": list(command_prefix),
                "observed": {k: v for k, v in probe.items() if k != "detail"},
                "not_external_provider_certification": True,
            },
            duration_ms=duration_ms,
        ))
        return "executed", checks
    checks.append(_check(
        "FIXTURE_CLI_UNAVAILABLE",
        status="error",
        message=f"Fixture CLI probe failed: {probe.get('reason', 'unavailable')}",
        duration_ms=duration_ms,
    ))
    return "blocked", checks


def _execute_live_direct_api(
    runtime: OpenAiCompatibleDirectRuntime,
    provider_config: ProviderConfig,
    env: dict[str, str],
    checks: list[dict[str, Any]],
) -> tuple[ExecutionOutcome, list[dict[str, Any]]]:
    return _execute_fixture_direct_api(runtime, provider_config, env, checks)


def _redact_report(report: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    text = json.dumps(report)
    for value in env.values():
        if isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, "***REDACTED***")
    return json.loads(text)


def _write_evidence_atomic(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _exit_code(outcome: ExecutionOutcome, checks: list[dict[str, Any]]) -> int:
    if outcome == "blocked":
        return 1
    if any(check.get("status") == "error" for check in checks):
        return 1
    return 0


def run_certify(request: CertifyRequest) -> tuple[dict[str, Any], int]:
    """Run certification and return (report_dict, exit_code). Evidence artifact is written only when complete."""
    env = dict(request.environ if request.environ is not None else os.environ)
    started_at = _utc_now()
    t0 = time.monotonic()
    mode_requested, checks, early_outcome = _resolve_mode(request)
    outcome: ExecutionOutcome = early_outcome or "dry_run"
    evidence_class = DRY_RUN_EVIDENCE_CLASS

    blocked = _validate_selection(request, checks)
    if blocked:
        outcome = blocked

    provider_config: ProviderConfig | None = None
    runtime: OpenAiCompatibleDirectRuntime | None = None
    if outcome != "blocked":
        provider_config, runtime, provider_blocked = _load_provider(request, checks, env)
        if provider_blocked:
            outcome = provider_blocked

    if outcome != "blocked":
        live_blocked = _validate_live_gates(request, provider_config, checks)
        if live_blocked:
            outcome = live_blocked

    if outcome != "blocked":
        if mode_requested == "dry_run":
            _dry_run_checks(request, provider_config, checks)
            outcome = "dry_run"
            evidence_class = DRY_RUN_EVIDENCE_CLASS
        elif mode_requested == "fixture_execute":
            evidence_class = FIXTURE_EVIDENCE_CLASS
            strategy = DEFAULT_RUNTIME_REGISTRY.get(request.profile).execution_strategy
            if strategy is ExecutionStrategy.DIRECT_API:
                assert runtime is not None and provider_config is not None
                outcome, checks = _execute_fixture_direct_api(runtime, provider_config, env, checks)
            elif strategy is ExecutionStrategy.SUBPROCESS_CLI:
                outcome, checks = _execute_fixture_cli(request, checks)
            else:
                checks.append(_check(
                    "FIXTURE_UNSUPPORTED_PROFILE",
                    status="error",
                    message=f"Fixture execution is not supported for profile {request.profile!r}",
                ))
                outcome = "blocked"
        elif mode_requested == "live_execute":
            evidence_class = LIVE_EVIDENCE_CLASS
            assert runtime is not None and provider_config is not None
            outcome, checks = _execute_live_direct_api(runtime, provider_config, env, checks)

    completed_at = _utc_now()
    duration_ms = int((time.monotonic() - t0) * 1000)
    report = {
        "certification_schema_version": CERTIFY_SCHEMA_VERSION,
        "package": {"name": "recollect-lines", "version": __version__},
        "run_id": str(uuid.uuid4()),
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
        "execution": {
            "mode_requested": mode_requested,
            "outcome": outcome,
            "evidence_class": evidence_class,
            "declared_not_observed_remote_availability": outcome in {"dry_run", "blocked"}
            or evidence_class == FIXTURE_EVIDENCE_CLASS,
        },
        "target": {
            "kind": _target_kind(request.profile),
            "profile": request.profile,
            "provider": request.provider,
        },
        "config_fingerprint": _config_fingerprint(request.providers_config, request.provider),
        "checks": checks,
        "limitations": [
            "No automatic live provider discovery",
            "No provider-selection winner or council/delegate execution",
            "Direct API certification is read-only with a fixed innocuous prompt",
            "Fixture executed evidence is local proof, not external provider certification",
            "No durable task reattachment or mid-task steering",
        ],
    }
    if request.max_cost_usd is not None:
        report["operator_budget"] = {"max_cost_usd": request.max_cost_usd}

    report = _redact_report(report, env)
    exit_code = _exit_code(outcome, checks)

    if request.output is not None:
        _write_evidence_atomic(request.output, report)

    return report, exit_code


def format_human_report(report: dict[str, Any]) -> str:
    execution = report["execution"]
    lines = [
        f"recollect-lines certify — outcome: {execution['outcome']} ({execution['evidence_class']})",
        f"package: {report['package']['name']} {report['package']['version']}",
        f"target: {report['target']['kind']} profile={report['target']['profile']!r}"
        + (f" provider={report['target']['provider']!r}" if report['target'].get('provider') else ""),
        f"mode_requested: {execution['mode_requested']}",
        "",
    ]
    if execution.get("declared_not_observed_remote_availability"):
        lines.append(
            "note: configured/declared settings are not proof of observed remote availability."
        )
        lines.append("")
    for check in report["checks"]:
        lines.append(f"[{check['status'].upper()}] {check['code']}: {check['message']}")
        if check.get("remediation"):
            lines.append(f"  remediation: {check['remediation']}")
    lines.append("")
    lines.append(f"duration_ms: {report['duration_ms']}")
    return "\n".join(lines)
