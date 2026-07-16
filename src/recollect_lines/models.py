from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class TaskState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    COLLECTING = "collecting"
    CANCELLING = "cancelling"
    # Non-terminal, actionable: a durable runtime launch record exists for this
    # task but the broker has no in-memory process handle (e.g. after a
    # restart) and cannot confirm the process group is dead. The broker never
    # fabricates a result from here; an operator must reconcile (which may
    # resolve it to failed, or attempt a persisted-pgid cancellation).
    RECOVERY_REQUIRED = "recovery_required"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"


TERMINAL_STATES = frozenset({
    TaskState.SUCCEEDED,
    TaskState.SUCCEEDED_WITH_WARNINGS,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.TIMED_OUT,
    TaskState.REJECTED,
})

ALLOWED_TRANSITIONS = {
    TaskState.CREATED: {TaskState.QUEUED, TaskState.REJECTED},
    TaskState.QUEUED: {TaskState.PREPARING, TaskState.CANCELLED, TaskState.REJECTED},
    # CANCELLING/RECOVERY_REQUIRED are reachable from PREPARING too: a broker
    # can crash between recording an opencode launch and the later RUNNING
    # transition, leaving a durable launch record for a task still at
    # PREPARING (see Broker.reconcile()).
    TaskState.PREPARING: {
        TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED, TaskState.CANCELLING, TaskState.RECOVERY_REQUIRED,
    },
    TaskState.RUNNING: {
        TaskState.COLLECTING, TaskState.CANCELLING, TaskState.FAILED, TaskState.TIMED_OUT, TaskState.RECOVERY_REQUIRED,
    },
    TaskState.CANCELLING: {TaskState.CANCELLED, TaskState.FAILED, TaskState.RECOVERY_REQUIRED},
    # RECOVERY_REQUIRED is reachable from COLLECTING too: a broker can crash
    # after popping the in-memory process handle (runtime already reaped, or
    # verification already in flight) but before the final terminal
    # transition — see Broker._RECONCILABLE_STATES / reconcile() and
    # docs/history/phases/phase-5c.md. Reconciliation from here never fabricates a success.
    TaskState.COLLECTING: {TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS, TaskState.FAILED, TaskState.RECOVERY_REQUIRED},
    TaskState.RECOVERY_REQUIRED: {TaskState.CANCELLING, TaskState.FAILED, TaskState.RUNNING},
}


@dataclass(frozen=True)
class ProfilePolicy:
    name: str
    allowed_modes: frozenset[str]
    max_timeout_seconds: int
    max_concurrency: int


DEFAULT_PROFILES = {
    "mock": ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 2),
    "opencode": ProfilePolicy("opencode", frozenset({"read_only", "isolated_worktree"}), 3600, 2),
    "claude_code": ProfilePolicy("claude_code", frozenset({"read_only", "isolated_worktree"}), 3600, 2),
    "codex": ProfilePolicy("codex", frozenset({"read_only", "isolated_worktree"}), 3600, 2),
    "cursor": ProfilePolicy("cursor", frozenset({"read_only", "isolated_worktree"}), 3600, 2),
    # Phase 6C: direct OpenAI-compatible HTTP runtime — read_only only; requires provider name.
    "openai_compatible": ProfilePolicy("openai_compatible", frozenset({"read_only"}), 3600, 2),
}

# Execution-backend identifiers accepted from legacy `profile=` at API boundaries.
# Kept aligned with DEFAULT_RUNTIME_REGISTRY (see runtime_registry.py).
KNOWN_RUNTIME_IDENTIFIERS = frozenset(DEFAULT_PROFILES)

# Verification-gate policy (Phase 5C): distinguishes evidence-only from
# gating verification without inventing a new terminal state per outcome —
# see Broker._apply_verification_gate and docs/history/phases/phase-5c.md.
#   "none"     — no automatic gating; any declared verify_commands still run
#                as evidence (unchanged from Phase 3/5B), but never affect
#                the task's terminal state. Default; fully backward
#                compatible with every pre-5C caller.
#   "advisory" — a verification failure downgrades a runtime success to
#                succeeded_with_warnings; it never blocks a success outright.
#   "required" — a verification failure (or missing/blocked verification)
#                forces a would-be success to failed. A runtime failure is
#                never "rescued" by passing verification either way.
VERIFICATION_POLICIES = ("none", "advisory", "required")


def now() -> str:
    return datetime.now(UTC).isoformat()


def legacy_profile_compatibility_metadata() -> dict[str, Any]:
    return {"legacy_profile_translated": True, "deprecated_fields": ["profile"]}


class LegacyProfileConflictError(ValueError):
    """Raised when legacy `profile` and `runtime` disagree on the execution backend."""

    def __init__(self, runtime: str, profile: str):
        self.runtime = runtime
        self.profile = profile
        super().__init__(
            f"runtime and legacy profile conflict: runtime={runtime!r}, profile={profile!r}"
        )


def _optional_non_empty_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field}' must be a non-empty string when provided")
    return value


def translate_delegate_fields(
    *,
    runtime: str | None = None,
    profile: str | None = None,
    model: str | None = None,
    agent_profile: str | None = None,
    result_schema: str | None = None,
    runtime_registry: object | None = None,
) -> tuple[str, str | None, str | None, str | None, dict[str, Any] | None]:
    """Centralized runtime/profile translation for CLI and MCP delegate inputs.

    Returns the effective execution-backend identifier, optional persisted side-agent
    fields, and optional compatibility metadata when legacy `profile` was translated.
    """
    from .runtime_registry import DEFAULT_RUNTIME_REGISTRY

    registry = runtime_registry or DEFAULT_RUNTIME_REGISTRY
    known_runtimes = registry.known_runtimes()
    model = _optional_non_empty_string(model, "model")
    agent_profile = _optional_non_empty_string(agent_profile, "agent_profile")
    result_schema = _optional_non_empty_string(result_schema, "result_schema")
    has_runtime = runtime is not None
    has_profile = profile is not None
    if has_runtime:
        runtime = _optional_non_empty_string(runtime, "runtime")
        assert runtime is not None
        if runtime not in known_runtimes:
            raise ValueError(
                f"runtime must be one of {sorted(known_runtimes)}, got {runtime!r}"
            )
    if has_profile:
        profile = _optional_non_empty_string(profile, "profile")
        assert profile is not None
    if has_runtime and has_profile:
        assert runtime is not None and profile is not None
        if runtime != profile:
            raise LegacyProfileConflictError(runtime, profile)
        return runtime, model, agent_profile, result_schema, legacy_profile_compatibility_metadata()
    if has_runtime:
        assert runtime is not None
        return runtime, model, agent_profile, result_schema, None
    if has_profile:
        assert profile is not None
        if profile not in known_runtimes:
            raise ValueError(
                f"Unknown profile {profile!r}: legacy profile is only accepted for known "
                f"runtime identifiers ({sorted(known_runtimes)}). "
                f"For behavioral roles use agent_profile={profile!r} with an explicit runtime=..."
            )
        return profile, model, agent_profile, result_schema, legacy_profile_compatibility_metadata()
    return "mock", model, agent_profile, result_schema, None


def effective_runtime(request: "TaskRequest") -> str:
    """Resolve the execution backend for broker policy and adapter dispatch."""
    if request.runtime is not None and request.profile is not None:
        if request.runtime != request.profile:
            raise LegacyProfileConflictError(request.runtime, request.profile)
        return request.runtime
    if request.runtime is not None:
        return request.runtime
    if request.profile is not None:
        return request.profile
    return "mock"


@dataclass(frozen=True)
class TaskRequest:
    task: str
    workspace: str
    execution_mode: str = "read_only"
    profile: str | None = None
    provider: str | None = None
    timeout_seconds: int = 1800
    verification_policy: str = "none"
    runtime: str | None = None
    model: str | None = None
    agent_profile: str | None = None
    result_schema: str | None = None
    task_category: str | None = None
    claude_permission_mode: str | None = None
    compatibility: dict[str, Any] | None = None
    explicit_fields: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)
    parent_task_id: str | None = None
    external_root_id: str | None = None
    relationship: str | None = None
    origin_kind: str | None = None
    origin_ref: str | None = None


@dataclass(frozen=True)
class TaskRecord:
    id: str
    task: str
    workspace: str
    execution_mode: str
    runtime: str
    profile: str
    provider: str | None
    timeout_seconds: int
    verification_policy: str
    state: TaskState
    created_at: str
    updated_at: str
    model: str | None = None
    agent_profile: str | None = None
    result_schema: str | None = None
    effective_model: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    external_root_id: str | None = None
    delegation_depth: int = 0
    relationship: str | None = None
    origin_kind: str | None = None
    origin_ref: str | None = None

    @classmethod
    def new(cls, request: TaskRequest) -> "TaskRecord":
        timestamp = now()
        runtime = effective_runtime(request)
        return cls(
            id=f"tsk_{uuid4().hex}",
            task=request.task,
            workspace=request.workspace,
            execution_mode=request.execution_mode,
            runtime=runtime,
            profile=runtime,
            provider=request.provider,
            timeout_seconds=request.timeout_seconds,
            verification_policy=request.verification_policy,
            state=TaskState.CREATED,
            created_at=timestamp,
            updated_at=timestamp,
            model=request.model,
            agent_profile=request.agent_profile,
            result_schema=request.result_schema,
            parent_task_id=request.parent_task_id,
            external_root_id=request.external_root_id,
            relationship=request.relationship,
            origin_kind=request.origin_kind,
            origin_ref=request.origin_ref,
        )

    def json(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


def request_artifact_payload(request: TaskRequest) -> dict[str, Any]:
    """Secret-safe persisted request view including optional compatibility metadata."""
    runtime = effective_runtime(request)
    payload: dict[str, Any] = {
        "task": request.task,
        "workspace": request.workspace,
        "execution_mode": request.execution_mode,
        "runtime": runtime,
        "profile": runtime,
        "provider": request.provider,
        "timeout_seconds": request.timeout_seconds,
        "verification_policy": request.verification_policy,
    }
    if request.model is not None:
        payload["model"] = request.model
    if request.agent_profile is not None:
        payload["agent_profile"] = request.agent_profile
    if request.result_schema is not None:
        payload["result_schema"] = request.result_schema
    if request.task_category is not None:
        payload["task_category"] = request.task_category
    if request.claude_permission_mode is not None:
        payload["claude_permission_mode"] = request.claude_permission_mode
    if request.compatibility is not None:
        payload["compatibility"] = request.compatibility
    if request.parent_task_id is not None:
        payload["parent_task_id"] = request.parent_task_id
    if request.external_root_id is not None:
        payload["external_root_id"] = request.external_root_id
    if request.relationship is not None:
        payload["relationship"] = request.relationship
    if request.origin_kind is not None:
        payload["origin_kind"] = request.origin_kind
    if request.origin_ref is not None:
        payload["origin_ref"] = request.origin_ref
    return payload


class InvalidTransition(ValueError):
    pass


class WorkspaceLeaseConflict(ValueError):
    pass


class RecoveryRequired(ValueError):
    """Raised by `Broker.collect()` when a task needs explicit reconciliation.

    Subclasses ValueError so existing CLI/MCP error handling (which already
    treats ValueError as a reportable business error, not an internal fault)
    surfaces it without any new plumbing.
    """

    def __init__(self, task_id: str, state: TaskState):
        self.task_id = task_id
        self.state = state
        super().__init__(
            f"Task {task_id} is {state.value} and requires reconciliation "
            "(call Broker.reconcile()/`recollect-lines reconcile`) before it can be collected"
        )


def validate_result(result: dict[str, Any], task_id: str) -> None:
    required = {"task_id", "state", "summary", "runtime"}
    missing = required - result.keys()
    if missing:
        raise ValueError(f"Result missing required fields: {', '.join(sorted(missing))}")
    if result["task_id"] != task_id:
        raise ValueError("Result task_id does not match the task")
    if result["state"] not in {TaskState.SUCCEEDED.value, TaskState.SUCCEEDED_WITH_WARNINGS.value}:
        raise ValueError("Result state must be a successful terminal state")
    if not isinstance(result["summary"], str) or not result["summary"].strip():
        raise ValueError("Result summary must be a non-empty string")
    if not isinstance(result["runtime"], dict) or not isinstance(result["runtime"].get("adapter"), str):
        raise ValueError("Result runtime.adapter must be a string")


def validate_verify_commands(commands: Any) -> None:
    """Shared by CLI, MCP, and Broker.create() so no interface duplicates this policy check."""
    valid = isinstance(commands, list) and all(
        isinstance(command, list) and command and all(isinstance(part, str) for part in command)
        for command in commands
    )
    if not valid:
        raise ValueError("verify_commands must be an array of non-empty argv arrays of strings")


def verification_gate_label(gate: dict[str, Any]) -> str:
    """Collapse a stored verification-gate outcome into one of four caller-facing labels:
    runtime_reported (policy=none, or advisory/required with nothing to check),
    advisory_verified / advisory_verification_failed, required_verified, or
    blocked_failed_verification (required policy, missing/blocked/failed verification).
    """
    policy = gate.get("policy", "none")
    outcome = gate.get("outcome", "not_configured")
    if policy == "required":
        return "required_verified" if outcome == "passed" else "blocked_failed_verification"
    if policy == "advisory":
        return "advisory_verification_failed" if outcome == "failed" else "advisory_verified"
    return "runtime_reported"
