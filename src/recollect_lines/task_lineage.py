"""Bounded parent-directed task lineage (MR 8.5).

Immutable provenance fields are derived at create time; callers cannot forge
root_task_id or delegation_depth. external_root_id groups host-side work
without inventing a broker parent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import TaskRecord, TaskRequest, TaskState

VALID_RELATIONSHIPS = frozenset({"delegates", "continues"})
VALID_ORIGIN_KINDS = frozenset({"host", "side_agent"})
FORBIDDEN_CALLER_LINEAGE_KEYS = frozenset({"root_task_id", "delegation_depth"})

ACTIVE_STATES = frozenset({
    TaskState.QUEUED,
    TaskState.PREPARING,
    TaskState.RUNNING,
    TaskState.COLLECTING,
    TaskState.CANCELLING,
    TaskState.RECOVERY_REQUIRED,
})

MAX_TREE_NODES = 256


@dataclass(frozen=True)
class LineagePolicy:
    max_active_agents: int = 32
    max_children_per_parent: int = 16
    max_delegation_depth: int = 8


DEFAULT_LINEAGE_POLICY = LineagePolicy()


@dataclass(frozen=True)
class ResolvedLineage:
    parent_task_id: str | None
    root_task_id: str
    external_root_id: str | None
    delegation_depth: int
    relationship: str | None
    origin_kind: str
    origin_ref: str | None


def _optional_audit_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field}' must be a non-empty string when provided")
    return value.strip()


def reject_forbidden_lineage_keys(raw: dict[str, Any]) -> None:
    for key in FORBIDDEN_CALLER_LINEAGE_KEYS:
        if key in raw:
            raise ValueError(f"'{key}' is derived by the broker and cannot be supplied by callers")


def validate_lineage_inputs(
    *,
    parent_task_id: str | None,
    external_root_id: str | None,
    relationship: str | None,
    origin_kind: str | None,
    origin_ref: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    parent_task_id = _optional_audit_string(parent_task_id, "parent_task_id")
    external_root_id = _optional_audit_string(external_root_id, "external_root_id")
    origin_ref = _optional_audit_string(origin_ref, "origin_ref")
    if relationship is not None:
        if relationship not in VALID_RELATIONSHIPS:
            raise ValueError(
                f"relationship must be one of {sorted(VALID_RELATIONSHIPS)}, got {relationship!r}"
            )
    if origin_kind is not None and origin_kind not in VALID_ORIGIN_KINDS:
        raise ValueError(
            f"origin_kind must be one of {sorted(VALID_ORIGIN_KINDS)}, got {origin_kind!r}"
        )
    if parent_task_id is None:
        if relationship is not None:
            raise ValueError("relationship requires parent_task_id")
        return None, external_root_id, None, origin_kind, origin_ref
    if relationship is None:
        relationship = "delegates"
    return parent_task_id, external_root_id, relationship, origin_kind, origin_ref


def resolve_lineage(
    *,
    task_id: str,
    parent_task_id: str | None,
    external_root_id: str | None,
    relationship: str | None,
    origin_kind: str | None,
    origin_ref: str | None,
    get_parent: Any,
    child_count: Any,
    active_agent_count: Any,
    policy: LineagePolicy,
) -> ResolvedLineage:
    parent_task_id, external_root_id, relationship, origin_kind, origin_ref = validate_lineage_inputs(
        parent_task_id=parent_task_id,
        external_root_id=external_root_id,
        relationship=relationship,
        origin_kind=origin_kind,
        origin_ref=origin_ref,
    )
    if parent_task_id is not None:
        if parent_task_id == task_id:
            raise ValueError("parent_task_id cannot equal the task being created")
        try:
            parent = get_parent(parent_task_id)
        except KeyError as error:
            raise ValueError(f"Unknown parent task: {parent_task_id}") from error
        if child_count(parent_task_id) >= policy.max_children_per_parent:
            raise ValueError(
                f"Parent {parent_task_id} already has the maximum number of child tasks "
                f"({policy.max_children_per_parent})"
            )
        delegation_depth = parent.delegation_depth + 1
        if delegation_depth > policy.max_delegation_depth:
            raise ValueError(
                f"delegation_depth {delegation_depth} exceeds policy maximum {policy.max_delegation_depth}"
            )
        root_task_id = parent.root_task_id
    else:
        delegation_depth = 0
        root_task_id = task_id
    resolved_origin = origin_kind or "host"
    if active_agent_count() >= policy.max_active_agents:
        raise ValueError(f"Broker active-agent limit reached ({policy.max_active_agents})")
    return ResolvedLineage(
        parent_task_id=parent_task_id,
        root_task_id=root_task_id,
        external_root_id=external_root_id,
        delegation_depth=delegation_depth,
        relationship=relationship,
        origin_kind=resolved_origin,
        origin_ref=origin_ref,
    )


def lineage_from_request(request: TaskRequest) -> dict[str, Any]:
    return {
        "parent_task_id": request.parent_task_id,
        "external_root_id": request.external_root_id,
        "relationship": request.relationship,
        "origin_kind": request.origin_kind,
        "origin_ref": request.origin_ref,
    }


def concise_task_summary(record: TaskRecord) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "task_id": record.id,
        "state": record.state.value,
        "workspace": record.workspace,
        "execution_mode": record.execution_mode,
        "runtime": record.runtime,
        "profile": record.profile,
        "provider": record.provider,
        "verification_policy": record.verification_policy,
        "root_task_id": record.root_task_id,
        "delegation_depth": record.delegation_depth,
    }
    if record.parent_task_id is not None:
        summary["parent_task_id"] = record.parent_task_id
    if record.external_root_id is not None:
        summary["external_root_id"] = record.external_root_id
    if record.relationship is not None:
        summary["relationship"] = record.relationship
    if record.origin_kind is not None:
        summary["origin_kind"] = record.origin_kind
    if record.origin_ref is not None:
        summary["origin_ref"] = record.origin_ref
    if record.model is not None:
        summary["model"] = record.model
    if record.effective_model is not None:
        summary["effective_model"] = record.effective_model
    if record.agent_profile is not None:
        summary["agent_profile"] = record.agent_profile
    if record.result_schema is not None:
        summary["result_schema"] = record.result_schema
    return summary
