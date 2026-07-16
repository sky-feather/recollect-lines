"""Versioned behavioral agent profiles (Phase 8.4).

Profiles are declarative behavior inputs — prompt prefix and default task fields.
They are not runtimes, credentials, permission escalations, or tool restrictions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROFILE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
EXECUTION_MODES = frozenset({"read_only", "isolated_worktree"})
RUNTIME_DEFAULT_EXECUTION_MODE = "read_only"
RUNTIME_DEFAULT_TIMEOUT_SECONDS = 1800

SOURCE_BROKER_CEILING = "broker_ceiling"
SOURCE_TASK_REQUEST = "task_request"
SOURCE_PROFILE_DEFAULT = "profile_default"
SOURCE_RUNTIME_DEFAULT = "runtime_default"


class AgentProfileError(ValueError):
    """Base error for invalid agent profile configuration or resolution."""


class UnknownAgentProfileError(AgentProfileError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Unknown agent_profile {name!r}")


@dataclass(frozen=True)
class AgentProfileConfig:
    name: str
    prompt_prefix: str
    default_result_schema: str | None = None
    default_execution_mode: str | None = None
    default_timeout_seconds: int | None = None
    recommended_runtime: str | None = None


@dataclass(frozen=True)
class ResolvedAgentProfile:
    name: str
    content_hash: str
    recommended_runtime: str | None
    execution_mode: str
    timeout_seconds: int
    result_schema: str | None
    sources: dict[str, str]
    task_overrides: dict[str, Any]
    prompt_prefix: str


def compose_task_prompt(prompt_prefix: str, task_text: str) -> str:
    """Deterministically join profile instructions and caller task text."""
    prefix = prompt_prefix.strip()
    task = task_text.strip()
    if not prefix:
        return task
    if not task:
        return prefix
    return f"{prefix}\n\n{task}"


def profile_content_hash(profile: AgentProfileConfig) -> str:
    payload = {
        "name": profile.name,
        "prompt_prefix": profile.prompt_prefix,
        "default_result_schema": profile.default_result_schema,
        "default_execution_mode": profile.default_execution_mode,
        "default_timeout_seconds": profile.default_timeout_seconds,
        "recommended_runtime": profile.recommended_runtime,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_profile_entry(name: str, raw: Any) -> AgentProfileConfig:
    if not PROFILE_NAME_PATTERN.match(name):
        raise AgentProfileError(
            f"Invalid agent profile name {name!r}: must match {PROFILE_NAME_PATTERN.pattern}"
        )
    if not isinstance(raw, dict):
        raise AgentProfileError(f"Agent profile {name!r} must be an object")
    prompt_prefix = raw.get("prompt_prefix")
    if not isinstance(prompt_prefix, str) or not prompt_prefix.strip():
        raise AgentProfileError(f"Agent profile {name!r}: prompt_prefix must be a non-empty string")
    default_result_schema = raw.get("default_result_schema")
    if default_result_schema is not None and (
        not isinstance(default_result_schema, str) or not default_result_schema.strip()
    ):
        raise AgentProfileError(f"Agent profile {name!r}: default_result_schema must be a non-empty string when set")
    default_execution_mode = raw.get("default_execution_mode")
    if default_execution_mode is not None:
        if default_execution_mode not in EXECUTION_MODES:
            raise AgentProfileError(
                f"Agent profile {name!r}: default_execution_mode must be one of {sorted(EXECUTION_MODES)}"
            )
    default_timeout_seconds = raw.get("default_timeout_seconds")
    if default_timeout_seconds is not None:
        if not isinstance(default_timeout_seconds, int) or isinstance(default_timeout_seconds, bool) or default_timeout_seconds < 1:
            raise AgentProfileError(f"Agent profile {name!r}: default_timeout_seconds must be a positive integer")
    recommended_runtime = raw.get("recommended_runtime")
    if recommended_runtime is not None and (not isinstance(recommended_runtime, str) or not recommended_runtime.strip()):
        raise AgentProfileError(f"Agent profile {name!r}: recommended_runtime must be a non-empty string when set")
    return AgentProfileConfig(
        name=name,
        prompt_prefix=prompt_prefix.strip(),
        default_result_schema=default_result_schema.strip() if isinstance(default_result_schema, str) else None,
        default_execution_mode=default_execution_mode,
        default_timeout_seconds=default_timeout_seconds,
        recommended_runtime=recommended_runtime.strip() if isinstance(recommended_runtime, str) else None,
    )


def validate_agent_profiles_document(data: Any) -> dict[str, AgentProfileConfig]:
    if not isinstance(data, dict):
        raise AgentProfileError("Agent profile configuration must be a top-level object")
    profiles_raw = data.get("agent_profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise AgentProfileError("'agent_profiles' must be a non-empty object")
    profiles: dict[str, AgentProfileConfig] = {}
    for name, entry in profiles_raw.items():
        if name in profiles:
            raise AgentProfileError(f"Duplicate agent profile name: {name!r}")
        profiles[name] = _parse_profile_entry(name, entry)
    return profiles


def load_agent_profiles_config(path: Path) -> dict[str, AgentProfileConfig]:
    try:
        raw_text = path.read_text()
    except OSError as error:
        raise AgentProfileError(f"Cannot read agent profile configuration {path}: {error}") from error
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise AgentProfileError(f"Agent profile configuration {path} is not valid JSON: {error}") from error
    return validate_agent_profiles_document(data)


BUILTIN_AGENT_PROFILES: dict[str, AgentProfileConfig] = {
    "repository-investigator": AgentProfileConfig(
        name="repository-investigator",
        prompt_prefix=(
            "You are a repository investigator. Trace code paths, cite file locations, "
            "and report findings without modifying the workspace."
        ),
        default_result_schema="evidence-report",
        default_execution_mode="read_only",
        default_timeout_seconds=2400,
        recommended_runtime="codex",
    ),
    "architecture-reviewer": AgentProfileConfig(
        name="architecture-reviewer",
        prompt_prefix=(
            "You are an architecture reviewer. Evaluate structure, boundaries, and risks. "
            "Prefer evidence-backed critique over speculative redesign."
        ),
        default_result_schema="review-findings",
        default_execution_mode="read_only",
        default_timeout_seconds=2400,
        recommended_runtime="claude_code",
    ),
    "implementation-worker": AgentProfileConfig(
        name="implementation-worker",
        prompt_prefix=(
            "You are an implementation worker. Make the smallest correct change that satisfies "
            "the task and preserve existing conventions."
        ),
        default_result_schema="implementation-report",
        default_execution_mode="isolated_worktree",
        default_timeout_seconds=3600,
        recommended_runtime="cursor",
    ),
    "test-planner": AgentProfileConfig(
        name="test-planner",
        prompt_prefix=(
            "You are a test planner. Propose focused, runnable verification steps and identify "
            "gaps without claiming execution you did not perform."
        ),
        default_result_schema="plain-summary",
        default_execution_mode="read_only",
        default_timeout_seconds=1800,
        recommended_runtime="mock",
    ),
}


def merge_agent_profile_registries(
    *registries: dict[str, AgentProfileConfig],
) -> dict[str, AgentProfileConfig]:
    merged = dict(BUILTIN_AGENT_PROFILES)
    for registry in registries:
        merged.update(registry)
    return merged


def get_agent_profile(name: str, registry: dict[str, AgentProfileConfig]) -> AgentProfileConfig:
    profile = registry.get(name)
    if profile is None:
        raise UnknownAgentProfileError(name)
    return profile


def _resolve_field(
    field: str,
    *,
    explicit_fields: frozenset[str],
    task_value: Any,
    profile_value: Any,
    runtime_default: Any,
) -> tuple[Any, str]:
    if field in explicit_fields:
        return task_value, SOURCE_TASK_REQUEST
    if profile_value is not None:
        return profile_value, SOURCE_PROFILE_DEFAULT
    return runtime_default, SOURCE_RUNTIME_DEFAULT


def resolve_agent_profile(
    *,
    profile: AgentProfileConfig,
    explicit_fields: frozenset[str],
    execution_mode: str,
    timeout_seconds: int,
    result_schema: str | None,
    allowed_modes: frozenset[str],
    max_timeout_seconds: int,
) -> ResolvedAgentProfile:
    resolved_mode, mode_source = _resolve_field(
        "execution_mode",
        explicit_fields=explicit_fields,
        task_value=execution_mode,
        profile_value=profile.default_execution_mode,
        runtime_default=RUNTIME_DEFAULT_EXECUTION_MODE,
    )
    resolved_timeout, timeout_source = _resolve_field(
        "timeout_seconds",
        explicit_fields=explicit_fields,
        task_value=timeout_seconds,
        profile_value=profile.default_timeout_seconds,
        runtime_default=RUNTIME_DEFAULT_TIMEOUT_SECONDS,
    )
    resolved_schema, schema_source = _resolve_field(
        "result_schema",
        explicit_fields=explicit_fields,
        task_value=result_schema,
        profile_value=profile.default_result_schema,
        runtime_default=None,
    )

    sources = {
        "execution_mode": mode_source,
        "timeout_seconds": timeout_source,
        "result_schema": schema_source,
    }
    task_overrides: dict[str, Any] = {}
    if "execution_mode" in explicit_fields:
        task_overrides["execution_mode"] = execution_mode
    if "timeout_seconds" in explicit_fields:
        task_overrides["timeout_seconds"] = timeout_seconds
    if "result_schema" in explicit_fields and result_schema is not None:
        task_overrides["result_schema"] = result_schema

    if resolved_mode not in allowed_modes:
        sources["execution_mode"] = SOURCE_BROKER_CEILING
        raise AgentProfileError(
            f"Resolved execution_mode {resolved_mode!r} is not permitted by runtime policy "
            f"(allowed: {sorted(allowed_modes)})"
        )
    if resolved_timeout > max_timeout_seconds:
        sources["timeout_seconds"] = SOURCE_BROKER_CEILING
        raise AgentProfileError(
            f"Resolved timeout_seconds {resolved_timeout} exceeds runtime maximum {max_timeout_seconds}"
        )

    return ResolvedAgentProfile(
        name=profile.name,
        content_hash=profile_content_hash(profile),
        recommended_runtime=profile.recommended_runtime,
        execution_mode=resolved_mode,
        timeout_seconds=resolved_timeout,
        result_schema=resolved_schema,
        sources=sources,
        task_overrides=task_overrides,
        prompt_prefix=profile.prompt_prefix,
    )


def resolution_artifact_payload(resolved: ResolvedAgentProfile) -> dict[str, Any]:
    return {
        "name": resolved.name,
        "content_hash": resolved.content_hash,
        "prompt_prefix": resolved.prompt_prefix,
        "recommended_runtime": resolved.recommended_runtime,
        "resolved": {
            "execution_mode": resolved.execution_mode,
            "timeout_seconds": resolved.timeout_seconds,
            "result_schema": resolved.result_schema,
        },
        "sources": resolved.sources,
        "task_overrides": resolved.task_overrides,
    }


def composed_prompt_artifact_payload(resolved: ResolvedAgentProfile, task_text: str, composed_prompt: str) -> dict[str, Any]:
    return {
        "profile_name": resolved.name,
        "profile_content_hash": resolved.content_hash,
        "task_text": task_text,
        "prompt_prefix": resolved.prompt_prefix,
        "composed_prompt": composed_prompt,
    }


def list_agent_profiles(registry: dict[str, AgentProfileConfig]) -> list[dict[str, Any]]:
    entries = []
    for name in sorted(registry):
        profile = registry[name]
        entries.append({
            "name": name,
            "content_hash": profile_content_hash(profile),
            "prompt_prefix": profile.prompt_prefix,
            "default_result_schema": profile.default_result_schema,
            "default_execution_mode": profile.default_execution_mode,
            "default_timeout_seconds": profile.default_timeout_seconds,
            "recommended_runtime": profile.recommended_runtime,
            "builtin": name in BUILTIN_AGENT_PROFILES,
        })
    return entries


def discovery_entry(profile: AgentProfileConfig) -> dict[str, Any]:
    return {
        "name": profile.name,
        "content_hash": profile_content_hash(profile),
        "default_result_schema": profile.default_result_schema,
        "default_execution_mode": profile.default_execution_mode,
        "default_timeout_seconds": profile.default_timeout_seconds,
        "recommended_runtime": profile.recommended_runtime,
        "limitations": [
            "recommended_runtime is advisory only and never overrides an explicit runtime",
            "prompt_prefix is composed with task text at launch; it does not enforce tools or permissions",
        ],
    }
