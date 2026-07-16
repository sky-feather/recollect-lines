"""Task-aware Claude Code ``--permission-mode`` selection (Wave 4 / PR 10).

``plan`` structurally refuses workspace writes but also steers the model toward
planning/meta-refusal on prose, debate, review, and summarization tasks. Read-only
safety for those categories is enforced structurally via ``--tools`` /
``--disallowedTools``; they use ``dontAsk`` instead of ``plan``.

Unknown categories and code-investigation read-only tasks keep ``plan`` as the
conservative default — no extra workspace authority without validated evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# CLI choices from ``claude --help`` (claude 2.1.x spike baseline).
VALID_CLAUDE_PERMISSION_MODES = frozenset({
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
})

TASK_CATEGORIES = frozenset({
    "prose",
    "review",
    "investigation",
    "implementation",
    "unknown",
})

# Read-only overrides may not broaden to file-edit or bypass modes.
READ_ONLY_ALLOWED_PERMISSION_MODES = frozenset({"plan", "dontAsk", "default", "auto"})
WORKTREE_ALLOWED_PERMISSION_MODES = frozenset({"acceptEdits"})

_SCHEMA_TO_CATEGORY: dict[str, str] = {
    "plain-summary": "prose",
    "review-findings": "review",
    "evidence-report": "investigation",
    "implementation-report": "implementation",
}

_AGENT_PROFILE_TO_CATEGORY: dict[str, str] = {
    "architecture-reviewer": "review",
    "repository-investigator": "investigation",
    "test-planner": "prose",
    "implementation-worker": "implementation",
}

_READ_ONLY_MODE_BY_CATEGORY: dict[str, str] = {
    "prose": "dontAsk",
    "review": "dontAsk",
    "investigation": "plan",
    "implementation": "plan",
    "unknown": "plan",
}


class ClaudePermissionModePolicyError(ValueError):
    """Invalid task category or permission-mode override."""


@dataclass(frozen=True)
class ClaudePermissionModeDecision:
    permission_mode: str
    task_category: str
    source: str
    signals: dict[str, Any]


def infer_task_category(
    *,
    execution_mode: str,
    result_schema: str | None = None,
    agent_profile: str | None = None,
    explicit_task_category: str | None = None,
) -> tuple[str, str]:
    """Return ``(category, source)`` where source is ``explicit`` or ``inferred``."""
    if explicit_task_category is not None:
        category = explicit_task_category.strip()
        if category not in TASK_CATEGORIES:
            raise ClaudePermissionModePolicyError(
                f"task_category must be one of {sorted(TASK_CATEGORIES)}, got {category!r}"
            )
        return category, "explicit"
    if execution_mode == "isolated_worktree":
        return "implementation", "inferred"
    if result_schema is not None:
        schema = result_schema.strip()
        if schema in _SCHEMA_TO_CATEGORY:
            return _SCHEMA_TO_CATEGORY[schema], "inferred"
    if agent_profile is not None and agent_profile in _AGENT_PROFILE_TO_CATEGORY:
        return _AGENT_PROFILE_TO_CATEGORY[agent_profile], "inferred"
    return "unknown", "inferred"


def _validate_override(execution_mode: str, permission_mode: str) -> None:
    if permission_mode not in VALID_CLAUDE_PERMISSION_MODES:
        raise ClaudePermissionModePolicyError(
            f"claude_permission_mode must be one of {sorted(VALID_CLAUDE_PERMISSION_MODES)}, "
            f"got {permission_mode!r}"
        )
    allowed = (
        WORKTREE_ALLOWED_PERMISSION_MODES
        if execution_mode == "isolated_worktree"
        else READ_ONLY_ALLOWED_PERMISSION_MODES
    )
    if permission_mode not in allowed:
        raise ClaudePermissionModePolicyError(
            f"claude_permission_mode {permission_mode!r} is not permitted for "
            f"execution_mode={execution_mode!r} (allowed: {sorted(allowed)})"
        )


def resolve_claude_permission_mode(
    *,
    execution_mode: str,
    result_schema: str | None = None,
    agent_profile: str | None = None,
    task_category: str | None = None,
    claude_permission_mode: str | None = None,
) -> ClaudePermissionModeDecision:
    """Choose a validated ``--permission-mode`` for a Claude Code launch."""
    if execution_mode not in ("read_only", "isolated_worktree"):
        raise ClaudePermissionModePolicyError(
            f"No validated Claude Code permission-mode mapping for execution_mode={execution_mode!r}; "
            "refusing to launch rather than silently broadening privilege"
        )
    category, category_source = infer_task_category(
        execution_mode=execution_mode,
        result_schema=result_schema,
        agent_profile=agent_profile,
        explicit_task_category=task_category,
    )
    signals: dict[str, Any] = {
        "execution_mode": execution_mode,
        "result_schema": result_schema,
        "agent_profile": agent_profile,
        "task_category_source": category_source,
    }
    if claude_permission_mode is not None:
        override = claude_permission_mode.strip()
        _validate_override(execution_mode, override)
        return ClaudePermissionModeDecision(
            permission_mode=override,
            task_category=category,
            source="caller_override",
            signals=signals,
        )
    if execution_mode == "isolated_worktree":
        return ClaudePermissionModeDecision(
            permission_mode="acceptEdits",
            task_category=category,
            source="policy",
            signals=signals,
        )
    return ClaudePermissionModeDecision(
        permission_mode=_READ_ONLY_MODE_BY_CATEGORY[category],
        task_category=category,
        source="policy",
        signals=signals,
    )


def permission_mode_policy_artifact(decision: ClaudePermissionModeDecision) -> dict[str, Any]:
    """Secret-safe diagnostics payload for launch metadata / artifacts."""
    return {
        "permission_mode": decision.permission_mode,
        "task_category": decision.task_category,
        "source": decision.source,
        "signals": decision.signals,
    }
