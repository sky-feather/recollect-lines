"""Parent-directed bounded council task graphs (Phase 6D).

Validates explicit bounds (rounds, concurrency, time/cost budget), executes a
parent-specified DAG through existing broker lifecycle primitives, and records
broker-observed evidence for parent/human synthesis. Never declares a winner,
merges results autonomously, or schedules recursive councils.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .discovery import select_candidates
from .runtime_registry import DEFAULT_RUNTIME_REGISTRY, ExecutionStrategy
from .models import TaskRequest, TaskState, TERMINAL_STATES

MAX_MAX_ROUNDS = 20
MAX_MAX_CONCURRENCY = 32
MAX_TIME_BUDGET_SECONDS = 86400
VALID_ROLES = frozenset({"plan", "critique", "analysis", "implement", "custom"})


@dataclass(frozen=True)
class CouncilBounds:
    max_rounds: int
    max_concurrency: int
    time_budget_seconds: int
    cost_budget_usd: float | None


@dataclass(frozen=True)
class CouncilStage:
    id: str
    role: str
    profile: str
    task: str
    provider: str | None
    depends_on: tuple[str, ...]
    round: int


class CouncilValidationError(ValueError):
    pass


class CouncilCostBudgetError(CouncilValidationError):
    """Nonzero cost budget cannot be measured or would be exceeded pre-dispatch."""

    def __init__(self, rejection: dict[str, Any]):
        self.rejection = rejection
        super().__init__(rejection["message"])


def _parse_bounds(raw: Any) -> CouncilBounds:
    if not isinstance(raw, dict):
        raise CouncilValidationError("bounds must be an object")
    max_rounds = raw.get("max_rounds")
    max_concurrency = raw.get("max_concurrency")
    time_budget_seconds = raw.get("time_budget_seconds")
    cost_budget_usd = raw.get("cost_budget_usd")
    for field_name, value in (("max_rounds", max_rounds), ("max_concurrency", max_concurrency), ("time_budget_seconds", time_budget_seconds)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise CouncilValidationError(f"bounds.{field_name} must be a positive integer")
    if max_rounds > MAX_MAX_ROUNDS:
        raise CouncilValidationError(f"bounds.max_rounds must be <= {MAX_MAX_ROUNDS}")
    if max_concurrency > MAX_MAX_CONCURRENCY:
        raise CouncilValidationError(f"bounds.max_concurrency must be <= {MAX_MAX_CONCURRENCY}")
    if time_budget_seconds > MAX_TIME_BUDGET_SECONDS:
        raise CouncilValidationError(f"bounds.time_budget_seconds must be <= {MAX_TIME_BUDGET_SECONDS}")
    if cost_budget_usd is not None:
        if not isinstance(cost_budget_usd, (int, float)) or isinstance(cost_budget_usd, bool) or cost_budget_usd <= 0:
            raise CouncilValidationError("bounds.cost_budget_usd must be a positive number when set")
        cost_budget_usd = float(cost_budget_usd)
    return CouncilBounds(max_rounds, max_concurrency, time_budget_seconds, cost_budget_usd)


def _parse_stage(raw: Any) -> CouncilStage:
    if not isinstance(raw, dict):
        raise CouncilValidationError("each stage must be an object")
    stage_id = raw.get("id")
    role = raw.get("role", "custom")
    profile = raw.get("profile")
    task = raw.get("task")
    provider = raw.get("provider")
    depends_on = raw.get("depends_on", [])
    stage_round = raw.get("round", 1)
    if not isinstance(stage_id, str) or not stage_id.strip():
        raise CouncilValidationError("stage.id must be a non-empty string")
    if role not in VALID_ROLES:
        raise CouncilValidationError(f"stage.role must be one of {sorted(VALID_ROLES)}")
    if not isinstance(profile, str) or not profile.strip():
        raise CouncilValidationError("stage.profile must be a non-empty string")
    profile = profile.strip()
    if not DEFAULT_RUNTIME_REGISTRY.contains(profile):
        raise CouncilValidationError(
            f"stage.profile must be a registered runtime, got {profile!r}"
        )
    descriptor = DEFAULT_RUNTIME_REGISTRY.get(profile)
    if descriptor.requires_named_provider and not provider:
        raise CouncilValidationError(f"stage.provider is required when profile is {profile!r}")
    if not descriptor.requires_named_provider and provider is not None:
        raise CouncilValidationError("stage.provider is only valid with openai_compatible profile")
    if not isinstance(task, str) or not task.strip():
        raise CouncilValidationError("stage.task must be a non-empty string")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise CouncilValidationError("stage.provider must be a non-empty string when set")
    if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
        raise CouncilValidationError("stage.depends_on must be an array of strings")
    if not isinstance(stage_round, int) or isinstance(stage_round, bool) or stage_round <= 0:
        raise CouncilValidationError("stage.round must be a positive integer")
    return CouncilStage(stage_id.strip(), role, profile.strip(), task.strip(), provider.strip() if provider else None, tuple(depends_on), stage_round)


def parse_council_plan(raw: dict[str, Any]) -> tuple[dict[str, Any], list[CouncilStage], CouncilBounds]:
    if not isinstance(raw, dict):
        raise CouncilValidationError("council plan must be an object")
    workspace = raw.get("workspace")
    execution_mode = raw.get("execution_mode", "read_only")
    acceptance_criteria = raw.get("acceptance_criteria")
    forbid_self_critique = raw.get("forbid_self_critique", True)
    stages_raw = raw.get("stages")
    if not isinstance(workspace, str) or not workspace.strip():
        raise CouncilValidationError("workspace must be a non-empty string")
    if execution_mode not in ("read_only", "isolated_worktree"):
        raise CouncilValidationError("execution_mode must be read_only or isolated_worktree")
    if not isinstance(acceptance_criteria, str) or not acceptance_criteria.strip():
        raise CouncilValidationError("acceptance_criteria must be a non-empty string (parent/human judges; broker records only)")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise CouncilValidationError("stages must be a non-empty array")
    if not isinstance(forbid_self_critique, bool):
        raise CouncilValidationError("forbid_self_critique must be a boolean")
    bounds = _parse_bounds(raw.get("bounds"))
    stages = [_parse_stage(item) for item in stages_raw]
    ids = [stage.id for stage in stages]
    if len(set(ids)) != len(ids):
        raise CouncilValidationError("stage ids must be unique")
    id_set = set(ids)
    for stage in stages:
        if stage.round > bounds.max_rounds:
            raise CouncilValidationError(f"stage {stage.id!r} round {stage.round} exceeds bounds.max_rounds {bounds.max_rounds}")
        for dep in stage.depends_on:
            if dep not in id_set:
                raise CouncilValidationError(f"stage {stage.id!r} depends_on unknown stage {dep!r}")
            if dep == stage.id:
                raise CouncilValidationError(f"stage {stage.id!r} cannot depend on itself")
    _assert_acyclic(stages)
    if forbid_self_critique:
        by_id = {stage.id: stage for stage in stages}
        for stage in stages:
            if stage.role != "critique":
                continue
            for dep in stage.depends_on:
                upstream = by_id[dep]
                if upstream.profile == stage.profile and upstream.provider == stage.provider:
                    raise CouncilValidationError(
                        f"self-critique forbidden: stage {stage.id!r} critiques {dep!r} with the same profile/provider"
                    )
    header = {
        "council_id": raw.get("council_id") or f"csl_{uuid4().hex}",
        "workspace": workspace.strip(),
        "execution_mode": execution_mode,
        "acceptance_criteria": acceptance_criteria.strip(),
        "forbid_self_critique": forbid_self_critique,
        "parent_task": raw.get("parent_task", "").strip() if isinstance(raw.get("parent_task"), str) else "",
    }
    return header, stages, bounds


def _assert_acyclic(stages: list[CouncilStage]) -> None:
    by_id = {stage.id: stage for stage in stages}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(stage_id: str) -> None:
        if stage_id in visited:
            return
        if stage_id in visiting:
            raise CouncilValidationError(f"cycle detected involving stage {stage_id!r}")
        visiting.add(stage_id)
        for dep in by_id[stage_id].depends_on:
            visit(dep)
        visiting.remove(stage_id)
        visited.add(stage_id)

    for stage in stages:
        visit(stage.id)


def _topological_waves(stages: list[CouncilStage]) -> list[list[CouncilStage]]:
    remaining = {stage.id: stage for stage in stages}
    completed: set[str] = set()
    waves: list[list[CouncilStage]] = []
    while remaining:
        ready = [stage for stage in remaining.values() if set(stage.depends_on).issubset(completed)]
        if not ready:
            raise CouncilValidationError("stage graph has a cycle or unresolved dependencies")
        ready.sort(key=lambda stage: stage.id)
        waves.append(ready)
        for stage in ready:
            completed.add(stage.id)
            del remaining[stage.id]
    return waves


def _stage_cost_upper_bound(broker: object, stage: CouncilStage) -> float | None:
    if DEFAULT_RUNTIME_REGISTRY.get(stage.profile).execution_strategy is not ExecutionStrategy.DIRECT_API:
        return None
    runtime = broker.direct_api_runtime
    if runtime is None or stage.provider is None:
        return None
    try:
        config = runtime.get_provider(stage.provider)
    except Exception:
        return None
    return config.estimated_cost_usd_upper_bound


def _resolve_cost_budget_enforcement(
    broker: object,
    stages: list[CouncilStage],
    bounds: CouncilBounds,
) -> dict[str, Any]:
    if bounds.cost_budget_usd is None:
        return {
            "cost_budget_usd": None,
            "cost_enforcement": "not_configured",
            "estimated_cost_usd_upper_bound_total": None,
            "stage_cost_estimates": [],
        }

    stage_estimates: list[dict[str, Any]] = []
    unmeasurable: list[dict[str, Any]] = []
    total_upper = 0.0
    for stage in stages:
        upper = _stage_cost_upper_bound(broker, stage)
        if upper is None:
            unmeasurable.append({
                "stage_id": stage.id,
                "profile": stage.profile,
                "provider": stage.provider,
                "reason": "no_configured_cost_upper_bound",
            })
            continue
        stage_estimates.append({
            "stage_id": stage.id,
            "profile": stage.profile,
            "provider": stage.provider,
            "estimated_cost_usd_upper_bound": upper,
        })
        total_upper += upper

    if unmeasurable:
        raise CouncilCostBudgetError({
            "code": "cost_budget_unmeasurable",
            "message": (
                "bounds.cost_budget_usd requires a configured estimated_cost_usd_upper_bound "
                "for every stage candidate; broker does not invent runtime costs"
            ),
            "cost_budget_usd": bounds.cost_budget_usd,
            "cost_enforcement": "rejected_unmeasurable",
            "unmeasurable_stages": unmeasurable,
            "stage_cost_estimates": stage_estimates,
        })

    if total_upper > bounds.cost_budget_usd:
        raise CouncilCostBudgetError({
            "code": "cost_budget_exceeded_pre_dispatch",
            "message": (
                f"configured stage cost upper bounds total {total_upper} exceeds "
                f"bounds.cost_budget_usd {bounds.cost_budget_usd}"
            ),
            "cost_budget_usd": bounds.cost_budget_usd,
            "cost_enforcement": "rejected_exceeds_budget",
            "estimated_cost_usd_upper_bound_total": total_upper,
            "stage_cost_estimates": stage_estimates,
        })

    return {
        "cost_budget_usd": bounds.cost_budget_usd,
        "cost_enforcement": "pre_dispatch_upper_bound",
        "estimated_cost_usd_upper_bound_total": total_upper,
        "stage_cost_estimates": stage_estimates,
    }


def _bounds_payload(bounds: CouncilBounds, cost_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_rounds": bounds.max_rounds,
        "max_concurrency": bounds.max_concurrency,
        "time_budget_seconds": bounds.time_budget_seconds,
        **cost_meta,
    }


def _record_council_rejection(
    broker: object,
    header: dict[str, Any],
    stages: list[CouncilStage],
    bounds: CouncilBounds,
    rejection: dict[str, Any],
) -> dict[str, Any]:
    cost_meta = {
        "cost_budget_usd": bounds.cost_budget_usd,
        "cost_enforcement": rejection["cost_enforcement"],
        "estimated_cost_usd_upper_bound_total": rejection.get("estimated_cost_usd_upper_bound_total"),
        "stage_cost_estimates": rejection.get("stage_cost_estimates", []),
    }
    result = {
        **header,
        "bounds": _bounds_payload(bounds, cost_meta),
        "stage_count": len(stages),
        "stages": [stage.id for stage in stages],
        "valid": False,
        "status": "rejected",
        "rejection": {
            "code": rejection["code"],
            "message": rejection["message"],
            "unmeasurable_stages": rejection.get("unmeasurable_stages", []),
        },
        "stage_outcomes": [],
        "skipped_stages": [],
        "note": "Council rejected before dispatch; no stage tasks were started.",
    }
    artifact_dir = broker.store.artifacts / header["council_id"]
    artifact_dir.mkdir(parents=True, exist_ok=True)
    broker.store.write_artifact(
        header["council_id"],
        "council_evidence.json",
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    return result


def _validate_stage_candidates(broker: object, header: dict[str, Any], stage: CouncilStage) -> None:
    select_candidates(
        registry=broker.runtime_registry,
        subprocess_adapters=broker.subprocess_adapters,
        direct_api_runtime=broker.direct_api_runtime,
        environ=broker._environ if broker._environ is not None else __import__("os").environ,
        execution_mode=header["execution_mode"],
        allowed_runtimes=[stage.profile],
        allowed_providers=[stage.provider] if stage.provider else None,
        require_available=True,
    )


def _validation_payload(
    header: dict[str, Any],
    stages: list[CouncilStage],
    bounds: CouncilBounds,
    cost_meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        **header,
        "bounds": _bounds_payload(bounds, cost_meta),
        "stage_count": len(stages),
        "stages": [stage.id for stage in stages],
        "valid": True,
        "limitations": [
            "no_autonomous_winner_selection",
            "no_recursive_council_scheduling",
            "no_durable_reattachment",
            "runtime_costs_not_measured_without_configured_upper_bounds",
        ],
    }


def validate_council_plan(broker: object, raw: dict[str, Any]) -> dict[str, Any]:
    header, stages, bounds = parse_council_plan(raw)
    for stage in stages:
        _validate_stage_candidates(broker, header, stage)
    cost_meta = _resolve_cost_budget_enforcement(broker, stages, bounds)
    return _validation_payload(header, stages, bounds, cost_meta)


def _collect_stage_evidence(broker: object, task_id: str) -> dict[str, Any]:
    status = broker.status(task_id)
    result = None
    result_path = broker.store.artifacts / task_id / "result.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text())
    return {
        "task_id": task_id,
        "state": status["state"],
        "profile": status["profile"],
        "provider": status.get("provider"),
        "runtime_result_summary": (result or {}).get("summary"),
        "terminal": status["state"] in {state.value for state in TERMINAL_STATES},
    }


def _run_mock_stage(broker: object, header: dict[str, Any], stage: CouncilStage, timeout_seconds: int) -> dict[str, Any]:
    request = TaskRequest(
        task=stage.task,
        workspace=header["workspace"],
        execution_mode=header["execution_mode"],
        profile=stage.profile,
        provider=stage.provider,
        timeout_seconds=timeout_seconds,
    )
    record = broker.create(request)
    broker.start(record.id)
    completed = broker.complete(record.id, f"[council:{stage.role}] {stage.task}")
    return _collect_stage_evidence(broker, completed.id)


def _run_subprocess_stage(broker: object, header: dict[str, Any], stage: CouncilStage, timeout_seconds: int) -> dict[str, Any]:
    request = TaskRequest(
        task=stage.task,
        workspace=header["workspace"],
        execution_mode=header["execution_mode"],
        profile=stage.profile,
        provider=stage.provider,
        timeout_seconds=timeout_seconds,
    )
    record = broker.create(request)
    broker.start(record.id)
    collected = broker.collect(record.id)
    return _collect_stage_evidence(broker, collected.id)


def _run_direct_api_stage(broker: object, header: dict[str, Any], stage: CouncilStage, timeout_seconds: int) -> dict[str, Any]:
    request = TaskRequest(
        task=stage.task,
        workspace=header["workspace"],
        execution_mode=header["execution_mode"],
        profile=stage.profile,
        provider=stage.provider,
        timeout_seconds=timeout_seconds,
    )
    record = broker.create(request)
    broker.start(record.id)
    collected = broker.collect(record.id)
    return _collect_stage_evidence(broker, collected.id)


def execute_council(broker: object, raw: dict[str, Any]) -> dict[str, Any]:
    header, stages, bounds = parse_council_plan(raw)
    for stage in stages:
        _validate_stage_candidates(broker, header, stage)
    try:
        cost_meta = _resolve_cost_budget_enforcement(broker, stages, bounds)
    except CouncilCostBudgetError as error:
        return _record_council_rejection(broker, header, stages, bounds, error.rejection)
    validation = _validation_payload(header, stages, bounds, cost_meta)
    started = time.monotonic()
    stage_outcomes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    budget_exhausted = False
    per_task_timeout = min(bounds.time_budget_seconds, broker.profiles[stages[0].profile].max_timeout_seconds)

    for wave in _topological_waves(stages):
        if budget_exhausted:
            for stage in wave:
                skipped.append({"stage_id": stage.id, "reason": "time_budget_exhausted"})
            continue
        for batch_start in range(0, len(wave), bounds.max_concurrency):
            batch = wave[batch_start:batch_start + bounds.max_concurrency]
            for stage in batch:
                elapsed = time.monotonic() - started
                if elapsed >= bounds.time_budget_seconds:
                    budget_exhausted = True
                    skipped.append({"stage_id": stage.id, "reason": "time_budget_exhausted", "elapsed_seconds": round(elapsed, 3)})
                    continue
                try:
                    if stage.profile == "mock":
                        evidence = _run_mock_stage(broker, header, stage, per_task_timeout)
                    elif DEFAULT_RUNTIME_REGISTRY.get(stage.profile).execution_strategy is ExecutionStrategy.DIRECT_API:
                        evidence = _run_direct_api_stage(broker, header, stage, per_task_timeout)
                    elif stage.profile in broker.subprocess_adapters:
                        evidence = _run_subprocess_stage(broker, header, stage, per_task_timeout)
                    else:
                        raise CouncilValidationError(f"unsupported runtime profile {stage.profile!r}")
                except Exception as error:
                    evidence = {
                        "stage_id": stage.id,
                        "role": stage.role,
                        "error": {"code": type(error).__name__, "message": str(error)},
                        "terminal": False,
                    }
                else:
                    evidence = {"stage_id": stage.id, "role": stage.role, "round": stage.round, **evidence}
                stage_outcomes.append(evidence)

    elapsed_total = round(time.monotonic() - started, 3)
    result = {
        **validation,
        "status": "completed_with_skips" if skipped else "completed",
        "rounds_executed": 1,
        "bounds_observed": {
            "max_rounds": bounds.max_rounds,
            "max_concurrency": bounds.max_concurrency,
            "time_budget_seconds": bounds.time_budget_seconds,
            "elapsed_seconds": elapsed_total,
            "time_budget_exhausted": budget_exhausted,
            "cost_budget_usd": cost_meta["cost_budget_usd"],
            "cost_observed_usd": None,
            "cost_enforcement": cost_meta["cost_enforcement"],
            "estimated_cost_usd_upper_bound_total": cost_meta["estimated_cost_usd_upper_bound_total"],
        },
        "stage_outcomes": stage_outcomes,
        "skipped_stages": skipped,
        "note": "Broker recorded stage evidence only; parent/human applies acceptance_criteria — no winner declared.",
    }
    artifact_dir = broker.store.artifacts / header["council_id"]
    artifact_dir.mkdir(parents=True, exist_ok=True)
    broker.store.write_artifact(
        header["council_id"],
        "council_evidence.json",
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    return result
