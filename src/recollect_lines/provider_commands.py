"""`recollect-lines provider list/add/show/test` -- Wave 3 / PR 7.

Safe provider identity management on top of the existing config contract
(precedence resolution, strict schema, atomic writer) from PR 4/5/6. Never
accepts or prints a raw credential value -- only environment-variable
*names* -- and never sends provider network traffic unless `provider test`
is explicitly run with `--live`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .direct_api_runtime import DIRECT_API_PROFILE, OpenAiCompatibleDirectRuntime
from .discovery import discover_providers
from .doctor import check_providers_config
from .models import TaskRecord, TaskRequest
from .providers import (
    MissingCredentialReference,
    OPENAI_COMPATIBLE_KIND,
    OPERATOR_CONFIG_DIRNAME,
    ProviderConfig,
    ProviderConfigError,
    existing_file_mode,
    load_providers_config,
    provider_config_format,
    redact_provider_error,
    render_providers_document,
    resolve_api_key,
    validate_providers_document,
    write_atomic_text,
)

PROVIDER_COMMANDS_SCHEMA_VERSION = "1"
PROVIDER_TEST_PROMPT = "recollect-lines provider test probe: respond with the single word OK"


def redact_report(report: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Defense-in-depth: scrub known secret-shaped patterns and any live env var
    value from a report before it is ever printed or returned. Every provider
    command report passes through this -- config never carries a raw secret in
    the first place, but this catches an endpoint accidentally echoing one back.
    """
    text = redact_provider_error(json.dumps(report))
    env = environ if environ is not None else os.environ
    for value in env.values():
        if isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, "***REDACTED***")
    return json.loads(text)


# Precedence tiers `provider add` will write to automatically when the caller
# does not pass --path. `legacy_default` (repo-root providers.json) is
# excluded on purpose: it predates the operator-config convention and is the
# tier most likely to be a tracked/shared file rather than local operator
# state, so a silent write there is refused with a remediation instead.
_AUTO_WRITABLE_ORIGINS = frozenset({"explicit", "env", "repo_local", "user_level"})


def _source_payload(providers_config: Path | None, providers_config_origin: str | None) -> dict[str, Any]:
    return {
        "origin": providers_config_origin or "not_configured",
        "path": str(providers_config) if providers_config is not None else None,
    }


def _package_payload() -> dict[str, str]:
    return {"name": "recollect-lines", "version": __version__}


def _error_report(schema_key: str, *, code: str, message: str, remediation: str | None = None, **extra: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        schema_key: PROVIDER_COMMANDS_SCHEMA_VERSION,
        "package": _package_payload(),
        "error": {"code": code, "message": message},
    }
    if remediation is not None:
        report["error"]["remediation"] = remediation
    report.update(extra)
    return report


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def run_provider_list(
    *,
    providers_config: Path | None,
    providers_config_origin: str | None,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    env = dict(environ if environ is not None else os.environ)
    findings, runtime = check_providers_config(providers_config, env, providers_config_origin=providers_config_origin)
    providers = discover_providers(direct_api_runtime=runtime, environ=env) if runtime is not None else []
    exit_code = 1 if any(f.status == "error" for f in findings) else 0
    report = {
        "provider_list_schema_version": PROVIDER_COMMANDS_SCHEMA_VERSION,
        "package": _package_payload(),
        "source": _source_payload(providers_config, providers_config_origin),
        "findings": [f.to_dict() for f in findings],
        "providers": providers,
    }
    return redact_report(report, env), exit_code


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------

def run_provider_show(
    *,
    providers_config: Path | None,
    providers_config_origin: str | None,
    name: str,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    env = dict(environ if environ is not None else os.environ)
    findings, runtime = check_providers_config(providers_config, env, providers_config_origin=providers_config_origin)
    source = _source_payload(providers_config, providers_config_origin)
    if runtime is None or any(f.status == "error" for f in findings):
        code = "ProviderConfigNotConfigured" if providers_config is None else "ProviderConfigInvalid"
        message = (
            "No provider configuration is active for this process"
            if providers_config is None
            else f"Provider configuration {providers_config} did not pass validation; see findings"
        )
        report = _error_report(
            "provider_show_schema_version",
            code=code,
            message=message,
            remediation="Run `recollect-lines provider list` or `config validate` for details.",
            source=source,
            findings=[f.to_dict() for f in findings],
        )
        return redact_report(report, env), 2
    providers = discover_providers(direct_api_runtime=runtime, environ=env)
    match = next((entry for entry in providers if entry["name"] == name), None)
    if match is None:
        report = _error_report(
            "provider_show_schema_version",
            code="ProviderNotFound",
            message=f"Provider {name!r} not found in {providers_config}",
            remediation=f"Known providers: {', '.join(sorted(runtime.providers)) or '(none)'}",
            source=source,
        )
        return redact_report(report, env), 2
    report = {
        "provider_show_schema_version": PROVIDER_COMMANDS_SCHEMA_VERSION,
        "package": _package_payload(),
        "source": source,
        "redacted": True,
        "provider": match,
    }
    return redact_report(report, env), 0


# --------------------------------------------------------------------------
# add
# --------------------------------------------------------------------------

def _provider_config_to_raw(config: ProviderConfig) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "kind": config.kind,
        "base_url": config.base_url,
        "api_key_env": config.api_key_env,
        "default_model": config.default_model,
        "request_timeout_seconds": config.request_timeout_seconds,
        "tls_verify": config.tls_verify,
        "allow_insecure_http": config.allow_insecure_http,
        "capabilities": {
            "chat_completions": config.capabilities.chat_completions,
            "structured_output": config.capabilities.structured_output,
            "streaming": config.capabilities.streaming,
            "tool_calls": config.capabilities.tool_calls,
            "workspace_access": config.capabilities.workspace_access,
            "process_cancellation": config.capabilities.process_cancellation,
        },
    }
    if config.ca_bundle:
        raw["ca_bundle"] = config.ca_bundle
    if config.estimated_cost_usd_upper_bound is not None:
        raw["estimated_cost_usd_upper_bound"] = config.estimated_cost_usd_upper_bound
    return raw


def _format_for_new_path(path: Path) -> Literal["json", "yaml"]:
    return "json" if path.suffix.lower() == ".json" else "yaml"


def _safe_entry_summary(config: ProviderConfig) -> dict[str, Any]:
    return {
        "name": config.name,
        "kind": config.kind,
        "base_url": config.base_url,
        "credential_reference": config.api_key_env,
        "default_model": config.default_model,
        "request_timeout_seconds": config.request_timeout_seconds,
        "tls_verify": config.tls_verify,
        "allow_insecure_http": config.allow_insecure_http,
    }


def run_provider_add(
    *,
    name: str,
    base_url: str,
    api_key_env: str,
    default_model: str,
    request_timeout_seconds: int | None = None,
    allow_insecure_http: bool = False,
    ca_bundle: str | None = None,
    capabilities: dict[str, bool] | None = None,
    estimated_cost_usd_upper_bound: float | None = None,
    explicit_path: Path | None = None,
    resolved_source_path: Path | None,
    resolved_source_origin: str | None,
    force: bool = False,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Add a provider entry to a writable local/operator config, atomically.

    Never accepts a raw credential -- only `api_key_env`, the name of an
    environment variable resolved at use time. Refuses to write to the
    legacy repo-root `providers.json` default unless the caller passes an
    explicit `--path`, since that tier predates operator-config and is more
    likely to be a shared/tracked file.
    """
    if explicit_path is not None:
        target_path = explicit_path
    elif resolved_source_origin == "legacy_default":
        return redact_report(_error_report(
            "provider_add_schema_version",
            code="UnsafeConfigTarget",
            message=(
                f"The active provider configuration ({resolved_source_path}) is the legacy "
                "repo-root default, which predates operator-managed config and may be a "
                "shared/tracked file; refusing to mutate it automatically"
            ),
            remediation=(
                "Pass --path to target a specific file explicitly, or run "
                "`recollect-lines config init` to create a local operator config, then retry."
            ),
        ), environ), 2
    elif resolved_source_origin in _AUTO_WRITABLE_ORIGINS:
        assert resolved_source_path is not None
        target_path = resolved_source_path
    else:
        # not_configured: nothing resolved yet -- create the same default
        # location `config init`/`init` use, rather than requiring --path.
        target_path = Path.cwd() / OPERATOR_CONFIG_DIRNAME / "config.yaml"

    target_existed = target_path.exists()
    if target_existed:
        try:
            existing_validated = load_providers_config(target_path)
        except ProviderConfigError as error:
            return redact_report(_error_report(
                "provider_add_schema_version",
                code="ExistingConfigInvalid",
                message=f"Existing configuration {target_path} is invalid: {error}",
                remediation="Fix the existing file (see `provider list`), or pass --path to target a new file.",
            ), environ), 2
        existing_raw = {n: _provider_config_to_raw(c) for n, c in existing_validated.items()}
        fmt = provider_config_format(target_path)
    else:
        existing_raw = {}
        fmt = _format_for_new_path(target_path)

    if name in existing_raw and not force:
        return redact_report(_error_report(
            "provider_add_schema_version",
            code="ProviderAlreadyExists",
            message=f"Provider {name!r} already exists in {target_path}",
            remediation="Pass --force to overwrite the existing entry, or choose a different --name.",
        ), environ), 2

    new_entry: dict[str, Any] = {
        "kind": OPENAI_COMPATIBLE_KIND,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "default_model": default_model,
    }
    if request_timeout_seconds is not None:
        new_entry["request_timeout_seconds"] = request_timeout_seconds
    if allow_insecure_http:
        new_entry["allow_insecure_http"] = True
    if ca_bundle is not None:
        new_entry["ca_bundle"] = ca_bundle
    if capabilities:
        new_entry["capabilities"] = capabilities
    if estimated_cost_usd_upper_bound is not None:
        new_entry["estimated_cost_usd_upper_bound"] = estimated_cost_usd_upper_bound

    merged_raw = {**existing_raw, name: new_entry}
    try:
        validated = validate_providers_document({"providers": merged_raw})
    except ProviderConfigError as error:
        return redact_report(_error_report(
            "provider_add_schema_version",
            code="InvalidProviderEntry",
            message=str(error),
            remediation="Fix the provider fields and retry.",
        ), environ), 2

    text = render_providers_document(merged_raw, fmt)
    mode = existing_file_mode(target_path, default=0o600)
    write_atomic_text(target_path, text, mode=mode)

    action = "created" if not target_existed else ("overwritten" if name in existing_raw else "updated")
    report = {
        "provider_add_schema_version": PROVIDER_COMMANDS_SCHEMA_VERSION,
        "package": _package_payload(),
        "written": str(target_path),
        "action": action,
        "provider": _safe_entry_summary(validated[name]),
    }
    return redact_report(report, environ), 0


# --------------------------------------------------------------------------
# test
# --------------------------------------------------------------------------

def _check(
    code: str,
    *,
    status: Literal["ok", "warning", "error", "not_checked"],
    message: str,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "status": status, "message": message}
    if remediation is not None:
        payload["remediation"] = remediation
    if details is not None:
        payload["details"] = details
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    return payload


_LAYER_REMEDIATION = {
    "tls": "Verify the provider's TLS certificate chain, or set ca_bundle to a trusted CA bundle file.",
    "auth": "Check that the credential referenced by api_key_env is a valid, non-expired key for this provider.",
    "http": "Inspect the provider's HTTP response/status (rate limit, quota, or server error).",
    "deadline": "The provider did not respond within request_timeout_seconds; increase it or check provider health.",
    "connection": "Verify base_url is reachable from this host (DNS, firewall, port, TCP listener).",
}


def _classify_probe_result(result: dict[str, Any]) -> tuple[str, str]:
    category = result.get("error_category") or "unknown"
    if category == "tls_verification_error":
        return "tls", category
    if category == "authentication_error":
        return "auth", category
    if category == "cancelled":
        return "cancelled", category
    if category in ("rate_limit_or_quota_error", "malformed_response"):
        return "http", category
    if category == "missing_credential_reference":
        return "config", category
    if category == "runtime_error":
        message = str(result.get("error_message") or "").lower()
        if "timed out" in message:
            return "deadline", "timeout"
        if result.get("http_status") is not None:
            return "http", "http_error"
        return "connection", "connection_error"
    return "unknown", category


def _execute_remote_probe(
    *,
    providers_config: Path | None,
    providers_config_origin: str | None,
    config: ProviderConfig,
    env: dict[str, str],
    timeout_seconds: int | None,
    prompt: str,
) -> tuple[dict[str, Any], int]:
    effective_config = config if timeout_seconds is None else dataclasses.replace(config, request_timeout_seconds=timeout_seconds)
    runtime = OpenAiCompatibleDirectRuntime(
        {config.name: effective_config},
        environ=env,
        config_source=providers_config,
        config_source_origin=providers_config_origin,
    )
    record = TaskRecord.new(TaskRequest(
        prompt,
        "/provider-test-probe",
        execution_mode="read_only",
        profile=DIRECT_API_PROFILE,
        provider=config.name,
        timeout_seconds=effective_config.request_timeout_seconds,
        verification_policy="none",
    ))
    artifacts_dir = Path(tempfile.mkdtemp(prefix="recollect-lines-provider-test-"))
    started = time.monotonic()
    try:
        _metadata, handle = runtime.start(record, artifacts_dir)
        result = runtime.collect(handle, wait_timeout=effective_config.request_timeout_seconds + 5.0)
    finally:
        shutil.rmtree(artifacts_dir, ignore_errors=True)
    duration_ms = int((time.monotonic() - started) * 1000)
    return result, duration_ms


def run_provider_test(
    *,
    name: str,
    providers_config: Path | None,
    providers_config_origin: str | None,
    environ: dict[str, str] | None = None,
    live: bool = False,
    acknowledge_billed_remote_calls: bool = False,
    timeout_override: int | None = None,
    probe_prompt: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Layered provider diagnostics: config schema, env reference, declared capability,
    then an explicitly opt-in (`--live`) remote probe. A plain `provider test` call --
    the default, no `--live` -- never sends provider network traffic.
    """
    env = dict(environ if environ is not None else os.environ)
    checks: list[dict[str, Any]] = []
    source = _source_payload(providers_config, providers_config_origin)

    if providers_config is None:
        checks.append(_check(
            "CONFIG_SOURCE", status="error",
            message="No provider configuration is active for this process",
            remediation="Pass --providers-config, set RECOLLECT_CONFIG, or run `recollect-lines config init`.",
        ))
        return redact_report(_finish_test(name, source, live, checks), env), 1
    if not providers_config.exists():
        checks.append(_check(
            "CONFIG_SOURCE", status="error",
            message=f"Provider configuration file {providers_config} does not exist",
            remediation="Fix the resolved config path or create the file.",
        ))
        return redact_report(_finish_test(name, source, live, checks), env), 1
    try:
        providers = load_providers_config(providers_config)
    except ProviderConfigError as error:
        checks.append(_check(
            "CONFIG_SOURCE", status="error",
            message=f"Provider configuration is invalid: {error}",
            remediation="Fix the JSON/YAML syntax and provider fields; see `provider list`.",
        ))
        return redact_report(_finish_test(name, source, live, checks), env), 1
    checks.append(_check("CONFIG_SOURCE", status="ok", message=f"Provider configuration {providers_config} is valid"))

    config = providers.get(name)
    if config is None:
        checks.append(_check(
            "PROVIDER_UNKNOWN", status="error",
            message=f"Provider {name!r} not found in {providers_config}",
            remediation=f"Known providers: {', '.join(sorted(providers)) or '(none)'}",
        ))
        return redact_report(_finish_test(name, source, live, checks), env), 1

    credential_ok = True
    try:
        resolve_api_key(config, env)
        checks.append(_check(
            "CREDENTIAL_REFERENCE", status="ok",
            message=f"Credential reference {config.api_key_env!r} is set",
            details={"credential_reference": config.api_key_env},
        ))
    except MissingCredentialReference as error:
        credential_ok = False
        checks.append(_check(
            "CREDENTIAL_REFERENCE", status="error" if live else "warning",
            message=str(error),
            remediation=f"Export {config.api_key_env!r} before testing this provider.",
            details={"credential_reference": config.api_key_env},
        ))

    checks.append(_check(
        "CAPABILITY", status="ok",
        message=(
            f"Provider {name!r} declares chat_completions=True; the openai_compatible "
            "runtime supports read_only execution_mode only (declared, not observed)"
        ),
        details={
            "chat_completions": config.capabilities.chat_completions,
            "structured_output": config.capabilities.structured_output,
            "streaming": config.capabilities.streaming,
            "tool_calls": config.capabilities.tool_calls,
        },
    ))

    if not live:
        checks.append(_check(
            "REMOTE_PROBE", status="not_checked",
            message="Remote probe skipped (opt-in only): no provider network traffic was sent",
            remediation="Pass --live --i-accept-billed-remote-calls to send one real minimal request.",
        ))
    elif not credential_ok:
        checks.append(_check(
            "REMOTE_PROBE", status="not_checked",
            message="Remote probe skipped: credential reference is missing",
        ))
    elif not acknowledge_billed_remote_calls:
        checks.append(_check(
            "REMOTE_PROBE", status="error",
            message="--live requires --i-accept-billed-remote-calls",
            remediation="Re-run with --live --i-accept-billed-remote-calls to acknowledge a real, possibly billed request.",
        ))
    else:
        result, duration_ms = _execute_remote_probe(
            providers_config=providers_config,
            providers_config_origin=providers_config_origin,
            config=config,
            env=env,
            timeout_seconds=timeout_override,
            prompt=probe_prompt or PROVIDER_TEST_PROMPT,
        )
        if result.get("exit_code") == 0:
            checks.append(_check(
                "REMOTE_PROBE", status="ok",
                message=f"Remote probe succeeded (HTTP {result.get('http_status')})",
                details={"layer": "http", "http_status": result.get("http_status"), "response_received": True},
                duration_ms=duration_ms,
            ))
        else:
            layer, category = _classify_probe_result(result)
            message = result.get("error_message") or f"probe failed ({category})"
            checks.append(_check(
                "REMOTE_PROBE", status="error",
                message=f"Remote probe failed at the {layer} layer: {message}",
                remediation=_LAYER_REMEDIATION.get(layer),
                details={"layer": layer, "category": category, "http_status": result.get("http_status")},
                duration_ms=duration_ms,
            ))

    report = redact_report(_finish_test(name, source, live, checks), env)
    exit_code = 1 if any(check["status"] == "error" for check in checks) else 0
    return report, exit_code


def _finish_test(name: str, source: dict[str, Any], live: bool, checks: list[dict[str, Any]]) -> dict[str, Any]:
    if any(c["status"] == "error" for c in checks):
        outcome = "blocked"
    elif any(c["status"] == "warning" for c in checks):
        outcome = "degraded"
    else:
        outcome = "ok"
    return {
        "provider_test_schema_version": PROVIDER_COMMANDS_SCHEMA_VERSION,
        "package": _package_payload(),
        "provider": name,
        "source": source,
        "live_probe_requested": live,
        "outcome": outcome,
        "checks": checks,
    }


# --------------------------------------------------------------------------
# human-readable formatting
# --------------------------------------------------------------------------

def format_human_report(report: dict[str, Any], *, command: str) -> str:
    lines = [f"recollect-lines {command}", f"package: {report['package']['name']} {report['package']['version']}", ""]
    if "error" in report:
        lines.append(f"[ERROR] {report['error']['code']}: {report['error']['message']}")
        if report["error"].get("remediation"):
            lines.append(f"  remediation: {report['error']['remediation']}")
        return "\n".join(lines)
    if "checks" in report:
        lines.append(f"provider: {report['provider']!r} — outcome: {report['outcome']}")
        lines.append(f"live probe requested: {report['live_probe_requested']}")
        lines.append("")
        for check in report["checks"]:
            lines.append(f"[{check['status'].upper()}] {check['code']}: {check['message']}")
            if check.get("remediation"):
                lines.append(f"  remediation: {check['remediation']}")
        return "\n".join(lines)
    if "written" in report:
        lines.append(f"written: {report['written']} ({report['action']})")
        lines.append(f"provider: {report['provider']['name']!r} base_url_scheme={report['provider']['base_url'].split('://', 1)[0]!r}")
        return "\n".join(lines)
    if "provider" in report:  # show
        entry = report["provider"]
        lines.append(f"provider: {entry['name']!r} (redacted)")
        for key in ("kind", "runtime_profile", "default_model", "credential_reference", "endpoint_summary"):
            if key in entry:
                lines.append(f"  {key}: {entry[key]}")
        return "\n".join(lines)
    # list
    source = report["source"]
    lines.append(f"source: {source['origin']}" + (f" -> {source['path']}" if source["path"] else ""))
    lines.append("")
    for finding in report["findings"]:
        lines.append(f"[{finding['status'].upper()}] {finding['code']}: {finding['message']}")
    lines.append("")
    if report["providers"]:
        for entry in report["providers"]:
            lines.append(f"- {entry['name']} ({entry['kind']}) model={entry['default_model']} available={entry['observed_availability'].get('available')}")
    else:
        lines.append("(no providers configured)")
    return "\n".join(lines)
