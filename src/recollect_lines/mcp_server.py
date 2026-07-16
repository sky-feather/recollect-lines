"""Local stdio MCP interface exposing the Recollect Lines broker to parent agents.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (the MCP stdio
transport) — every stdout line is exactly one complete JSON-RPC message, and
nothing else is ever written there. Diagnostics go to stderr only.

Error model:
- JSON-RPC protocol errors (top-level `error` object) cover things the
  protocol itself rejects before any broker call is made: malformed JSON,
  a malformed envelope, an unknown top-level method, an unknown tool name,
  or a `tools/call` whose `name`/`arguments` are the wrong JSON type.
- Tool-result `isError: true` (see `_tool_result`) covers everything a
  *known* tool rejects once it starts running: a malformed argument value
  (e.g. missing `task`), an unknown task id, an illegal state transition, or
  any other broker/policy error. This keeps a single bad `delegate_batch`
  item from turning the whole call into a protocol failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from .claude_code_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter
from .cursor_adapter import CursorAdapter
from .models import (
    VERIFICATION_POLICIES,
    TaskRequest,
    translate_delegate_fields,
    validate_verify_commands,
    verification_gate_label,
)
from .result_normalization import NORMALIZED_RESULT_ARTIFACT, concise_normalized_view
from .task_lineage import FORBIDDEN_CALLER_LINEAGE_KEYS, VALID_ORIGIN_KINDS, VALID_RELATIONSHIPS, concise_task_summary, reject_forbidden_lineage_keys
from .runtime_registry import DEFAULT_RUNTIME_REGISTRY
from .opencode_adapter import OpenCodeAdapter
from .operator_control import OperatorControlRefused
from .providers import resolve_providers_config_source
from .service import Broker

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "recollect-lines-mcp"
SERVER_VERSION = "0.1.0"
ENVELOPE_VERSION = 1

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

EXECUTION_MODES = ("read_only", "isolated_worktree")
RUNTIMES = DEFAULT_RUNTIME_REGISTRY.names()
PROFILES = RUNTIMES  # deprecated alias for schema/documentation continuity


class ProtocolError(Exception):
    """A JSON-RPC-level error: the message/request itself is invalid, before any tool runs."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- tool-result envelope -------------------------------------------------


def _envelope(tool: str, ok: bool, data: Any = None, error: dict | None = None) -> dict:
    body = {"envelope_version": ENVELOPE_VERSION, "tool": tool, "ok": ok}
    body["data" if ok else "error"] = data if ok else error
    return body


def _tool_result(tool: str, ok: bool, data: Any = None, error: dict | None = None) -> dict:
    body = _envelope(tool, ok, data=data, error=error)
    return {"content": [{"type": "text", "text": json.dumps(body, indent=2, sort_keys=True)}], "isError": not ok}


# --- shared argument helpers -----------------------------------------------


def _build_task_request(item: Any) -> tuple[TaskRequest, list | None]:
    """Validate one delegate-shaped item, raising ValueError on any bad field.

    Shared by `delegate` (a single item) and `delegate_batch` (many items). A
    ValueError raised here is reported as a business/tool error, never a raw
    JSON-RPC protocol error, so one bad `delegate_batch` item never disturbs
    another item's already-started task.
    """
    if not isinstance(item, dict):
        raise ValueError("Each delegate item must be an object")
    reject_forbidden_lineage_keys(item)
    task = item.get("task")
    workspace = item.get("workspace")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("'task' must be a non-empty string")
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("'workspace' must be a non-empty string")
    explicit_fields: set[str] = set()
    if "execution_mode" in item:
        explicit_fields.add("execution_mode")
    execution_mode = item.get("execution_mode", "read_only")
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"execution_mode must be one of {EXECUTION_MODES}, got {execution_mode!r}")
    runtime = item.get("runtime")
    profile = item.get("profile") if "profile" in item else None
    if "model" in item:
        explicit_fields.add("model")
    if "agent_profile" in item:
        explicit_fields.add("agent_profile")
    if "result_schema" in item:
        explicit_fields.add("result_schema")
    if "task_category" in item:
        explicit_fields.add("task_category")
    if "claude_permission_mode" in item:
        explicit_fields.add("claude_permission_mode")
    effective_runtime, model, agent_profile, result_schema, compatibility = translate_delegate_fields(
        runtime=runtime,
        profile=profile,
        model=item.get("model"),
        agent_profile=item.get("agent_profile"),
        result_schema=item.get("result_schema"),
    )
    if "timeout_seconds" in item:
        explicit_fields.add("timeout_seconds")
    timeout_seconds = item.get("timeout_seconds", 1800)
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds < 1:
        raise ValueError("timeout_seconds must be a positive integer")
    verification_policy = item.get("verification_policy", "none")
    if verification_policy not in VERIFICATION_POLICIES:
        raise ValueError(f"verification_policy must be one of {VERIFICATION_POLICIES}, got {verification_policy!r}")
    provider = item.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise ValueError("'provider' must be a non-empty string when provided")
    task_category = item.get("task_category")
    if task_category is not None and (not isinstance(task_category, str) or not task_category.strip()):
        raise ValueError("'task_category' must be a non-empty string when provided")
    claude_permission_mode = item.get("claude_permission_mode")
    if claude_permission_mode is not None and (
        not isinstance(claude_permission_mode, str) or not claude_permission_mode.strip()
    ):
        raise ValueError("'claude_permission_mode' must be a non-empty string when provided")
    verify_commands = item.get("verify_commands")
    if verify_commands is not None:
        validate_verify_commands(verify_commands)
    parent_task_id = item.get("parent_task_id")
    external_root_id = item.get("external_root_id")
    relationship = item.get("relationship")
    origin_kind = item.get("origin_kind")
    origin_ref = item.get("origin_ref")
    for key in ("parent_task_id", "external_root_id", "relationship", "origin_kind", "origin_ref"):
        if key in item and item[key] is not None and not isinstance(item[key], str):
            raise ValueError(f"'{key}' must be a string when provided")
    return TaskRequest(
        task,
        workspace,
        execution_mode,
        effective_runtime,
        provider,
        timeout_seconds,
        verification_policy,
        runtime=effective_runtime,
        model=model,
        agent_profile=agent_profile,
        result_schema=result_schema,
        task_category=task_category,
        claude_permission_mode=claude_permission_mode,
        compatibility=compatibility,
        explicit_fields=frozenset(explicit_fields),
        parent_task_id=parent_task_id,
        external_root_id=external_root_id,
        relationship=relationship,
        origin_kind=origin_kind,
        origin_ref=origin_ref,
    ), verify_commands


def _require_task_id(args: dict) -> str:
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("'task_id' must be a non-empty string")
    return task_id


def _task_summary(record, broker: Broker | None = None) -> dict:
    summary = concise_task_summary(record)
    if broker is not None:
        compatibility = _read_json_artifact(broker, record.id, "request.json")
        if isinstance(compatibility, dict) and "compatibility" in compatibility:
            summary["compatibility"] = compatibility["compatibility"]
        detail = broker.reconcile_detail(record.id)
        if detail is not None:
            summary["reconciliation"] = detail
    return summary


def _read_json_artifact(broker: Broker, task_id: str, name: str) -> Any:
    path = broker.store.artifacts / task_id / name
    return json.loads(path.read_text()) if path.is_file() else None


# --- tool handlers -----------------------------------------------------


def _create_and_start(broker: Broker, item: Any) -> tuple[Any, Exception | None]:
    """Validate, create, and start one delegate item.

    Returns (record, None) for any normal outcome of start() — including a
    broker-returned FAILED record (e.g. a bad workspace) — since that's not
    an unexpected failure. Returns (record, error) only if start() itself
    raised after the task was already durably created: the record (and its
    task_id) must never be lost just because the caller now needs to know
    something went wrong with an already-persisted task.
    """
    request, verify_commands = _build_task_request(item)
    record = broker.create(request, verify_commands=verify_commands)
    try:
        record = broker.start(record.id)
    except Exception as error:
        print(f"recollect_lines.mcp_server: task {record.id} start() raised unexpectedly: {error!r}", file=sys.stderr)
        return record, error
    return record, None


def handle_delegate(broker: Broker, args: dict) -> dict:
    record, start_error = _create_and_start(broker, args)
    if start_error is not None:
        raise ValueError(f"Task {record.id} was created but start() raised unexpectedly: {start_error}")
    return _task_summary(record, broker)


def handle_delegate_batch(broker: Broker, args: dict) -> dict:
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("'tasks' must be a non-empty array")
    outcomes = []
    for index, item in enumerate(tasks):
        try:
            record, start_error = _create_and_start(broker, item)
        except Exception as error:
            # A bad item must never lose the outcomes already recorded for earlier,
            # independently-started items in this batch.
            outcomes.append({"index": index, "accepted": False, "error": {"code": type(error).__name__, "message": str(error)}})
            continue
        if start_error is not None:
            # The task was created (it has a real task_id the caller can still act on
            # via status/cancel) but start() itself raised unexpectedly.
            outcomes.append({
                "index": index,
                "accepted": False,
                "task_id": record.id,
                "error": {"code": type(start_error).__name__, "message": f"Task was created but start() raised unexpectedly: {start_error}"},
            })
        else:
            outcomes.append({"index": index, "accepted": True, **_task_summary(record, broker)})
    return {"outcomes": outcomes}


def handle_status(broker: Broker, args: dict) -> dict:
    return broker.status(_require_task_id(args))


def handle_collect(broker: Broker, args: dict) -> dict:
    """Collect a task's runtime-reported result, plus any broker-verified
    evidence. Verification (if verify_commands were declared at delegate
    time) and its effect on the terminal state are entirely Broker.collect()'s
    responsibility (Phase 5C) — this handler only reads back the artifacts a
    single `collect()` call already produced, which is what makes a repeated
    call on an already-terminal task idempotent (no re-run verification, see
    tests/test_verification_gate.py).
    """
    task_id = _require_task_id(args)
    broker.store.get(task_id)  # raises KeyError for an unknown task, same as every other tool
    record = broker.collect(task_id)
    gate = _read_json_artifact(broker, task_id, "verification_gate.json")
    normalized = _read_json_artifact(broker, task_id, NORMALIZED_RESULT_ARTIFACT)
    return {
        "task_id": record.id,
        "state": record.state.value,
        "runtime_result": _read_json_artifact(broker, task_id, "result.json"),
        "normalized_result": normalized,
        "normalized_summary": concise_normalized_view(normalized),
        "broker_verification": _read_json_artifact(broker, task_id, "verification.json"),
        "verification_gate": {**gate, "label": verification_gate_label(gate)} if gate else None,
    }


def handle_reconcile(broker: Broker, args: dict) -> dict:
    task_id = args.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        raise ValueError("'task_id' must be a non-empty string when provided")
    if task_id:
        record = broker.reconcile(task_id)
        return {"reconciled": [_task_summary(record, broker)]}
    return {"reconciled": [_task_summary(record, broker) for record in broker.reconcile_pending()]}


def handle_cancel(broker: Broker, args: dict) -> dict:
    task_id = _require_task_id(args)
    reason = args.get("reason", "Cancelled by MCP caller")
    if not isinstance(reason, str):
        raise ValueError("'reason' must be a string")
    return _task_summary(broker.cancel(task_id, reason))


def handle_control(broker: Broker, args: dict) -> dict:
    task_id = _require_task_id(args)
    action = args.get("action")
    if not isinstance(action, str) or not action.strip():
        raise ValueError("'action' must be a non-empty string")
    reason = args.get("reason", "Cancelled by operator control")
    if not isinstance(reason, str):
        raise ValueError("'reason' must be a string")
    content = args.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("'content' must be a string when provided")
    return broker.operator_control(
        task_id,
        action,
        reason=reason,
        message_content=content,
    )


def handle_message(broker: Broker, args: dict) -> dict:
    task_id = _require_task_id(args)
    content = args.get("content")
    if not isinstance(content, str):
        raise ValueError("'content' must be a string")
    record = broker.store.get(task_id)  # raises KeyError for an unknown task, same as every other tool
    return {
        "task_id": task_id,
        "status": "unsupported",
        "reason": (
            "Recollect Lines has no in-flight steering channel for any adapter: mock, "
            "OpenCode, and Claude Code tasks all run to completion (or are cancelled "
            "outright). Neither OpenCode nor Claude Code supports injecting a message "
            "into an already-running task."
        ),
        "profile": record.profile,
        "state": record.state.value,
    }


def handle_discover_capabilities(broker: Broker, args: dict) -> dict:
    return broker.discover_capabilities()


def _parse_capability_object(raw: Any, field_name: str) -> dict[str, bool] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{field_name} must be an object")
    parsed: dict[str, bool] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, bool):
            raise ValueError(f"{field_name} values must be boolean")
        parsed[key] = value
    return parsed


def handle_select_candidates(broker: Broker, args: dict) -> dict:
    execution_mode = args.get("execution_mode")
    if not isinstance(execution_mode, str) or not execution_mode.strip():
        raise ValueError("'execution_mode' must be a non-empty string")
    allowed_runtimes = args.get("allowed_runtimes")
    allowed_providers = args.get("allowed_providers")
    if allowed_runtimes is not None and (not isinstance(allowed_runtimes, list) or not all(isinstance(item, str) for item in allowed_runtimes)):
        raise ValueError("'allowed_runtimes' must be an array of strings when provided")
    if allowed_providers is not None and (not isinstance(allowed_providers, list) or not all(isinstance(item, str) for item in allowed_providers)):
        raise ValueError("'allowed_providers' must be an array of strings when provided")
    require_available = args.get("require_available", True)
    if not isinstance(require_available, bool):
        raise ValueError("'require_available' must be a boolean")
    return broker.select_candidates(
        execution_mode=execution_mode,
        required_runtime_capabilities=_parse_capability_object(args.get("required_runtime_capabilities"), "required_runtime_capabilities"),
        required_provider_capabilities=_parse_capability_object(args.get("required_provider_capabilities"), "required_provider_capabilities"),
        allowed_runtimes=allowed_runtimes,
        allowed_providers=allowed_providers,
        require_available=require_available,
    )


def _require_council_plan(args: dict) -> dict:
    plan = args.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("'plan' must be an object")
    return plan


def handle_council_validate(broker: Broker, args: dict) -> dict:
    return broker.validate_council(_require_council_plan(args))


def handle_council_execute(broker: Broker, args: dict) -> dict:
    return broker.execute_council(_require_council_plan(args))


def handle_task_children(broker: Broker, args: dict) -> dict:
    task_id = _require_task_id(args)
    return {"parent_task_id": task_id, "children": broker.children(task_id)}


def handle_task_tree(broker: Broker, args: dict) -> dict:
    root_task_id = args.get("root_task_id")
    if not isinstance(root_task_id, str) or not root_task_id.strip():
        raise ValueError("'root_task_id' must be a non-empty string")
    return broker.task_tree(root_task_id.strip())


def handle_completion_events(broker: Broker, args: dict) -> dict:
    after_event_id = args.get("after_event_id", 0)
    if not isinstance(after_event_id, int) or isinstance(after_event_id, bool):
        raise ValueError("'after_event_id' must be a non-negative integer")
    limit = args.get("limit", 64)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("'limit' must be a positive integer")
    task_id = args.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        raise ValueError("'task_id' must be a non-empty string when provided")
    root_task_id = args.get("root_task_id")
    if root_task_id is not None and (not isinstance(root_task_id, str) or not root_task_id.strip()):
        raise ValueError("'root_task_id' must be a non-empty string when provided")
    completion_only = args.get("completion_only", True)
    if not isinstance(completion_only, bool):
        raise ValueError("'completion_only' must be a boolean")
    states = args.get("states")
    if states is not None:
        if not isinstance(states, list) or not all(isinstance(item, str) for item in states):
            raise ValueError("'states' must be an array of strings when provided")
        states = frozenset(states)
    return broker.completion_events_since(
        after_event_id,
        limit=limit,
        task_id=task_id.strip() if isinstance(task_id, str) else None,
        root_task_id=root_task_id.strip() if isinstance(root_task_id, str) else None,
        completion_only=completion_only,
        states=states,
    )


# --- tool schemas and registry ----------------------------------------------

DELEGATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "Natural-language description of the work to delegate."},
        "workspace": {
            "type": "string",
            "description": "Path to the source workspace. Must be a Git repository or worktree when execution_mode is isolated_worktree.",
        },
        "execution_mode": {
            "type": "string",
            "enum": list(EXECUTION_MODES),
            "default": "read_only",
            "description": "read_only runs directly against workspace; isolated_worktree runs in a broker-owned Git worktree branched from workspace's current HEAD.",
        },
        "runtime": {
            "type": "string",
            "enum": list(RUNTIMES),
            "default": "mock",
            "description": (
                "Execution backend identifier. mock is a deterministic no-op adapter for testing; "
                "opencode runs the real OpenCode CLI; claude_code runs Claude Code (`claude -p`); "
                "codex runs Codex (`codex exec`); cursor runs Cursor Agent; "
                "openai_compatible sends a chat-completions request through a named provider."
            ),
        },
        "profile": {
            "type": "string",
            "enum": list(PROFILES),
            "deprecated": True,
            "description": (
                "Deprecated alias for runtime. Accepted only when its value is a known runtime "
                "identifier; use runtime instead. Unknown values that look like behavioral roles "
                "must be passed as agent_profile with an explicit runtime."
            ),
        },
        "model": {
            "type": "string",
            "description": "Optional requested model identifier (persisted; adapter wiring is future work).",
        },
        "agent_profile": {
            "type": "string",
            "description": (
                "Optional behavioral agent profile name. Resolves prompt prefix and default task fields "
                "at create time; recommended_runtime is advisory only."
            ),
        },
        "result_schema": {
            "type": "string",
            "enum": ["plain-summary", "evidence-report", "review-findings", "implementation-report"],
            "description": (
                "Requested normalized result schema. Unknown values are rejected at delegate time; "
                "profile defaults apply when omitted unless an explicit task value wins."
            ),
        },
        "task_category": {
            "type": "string",
            "enum": ["prose", "review", "investigation", "implementation", "unknown"],
            "description": (
                "Optional Claude Code task category for permission-mode policy. When omitted, inferred "
                "from execution_mode, result_schema, and agent_profile."
            ),
        },
        "claude_permission_mode": {
            "type": "string",
            "enum": ["acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"],
            "description": (
                "Optional explicit Claude Code --permission-mode override. Validated per execution_mode; "
                "read_only may not broaden to acceptEdits or bypassPermissions."
            ),
        },
        "provider": {
            "type": "string",
            "description": "Named provider entry (required when runtime is openai_compatible).",
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "default": 1800,
            "description": "Maximum seconds this task may run under the selected profile's policy.",
        },
        "verify_commands": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "description": "Optional argv-array commands (never shell strings) run as broker-verified evidence when this task is collected.",
        },
        "verification_policy": {
            "type": "string",
            "enum": list(VERIFICATION_POLICIES),
            "default": "none",
            "description": (
                "Controls whether verify_commands can affect this task's terminal outcome. 'none' (default) "
                "runs any declared verify_commands as evidence only, with no effect on the task's terminal "
                "state — fully backward compatible with delegate calls that predate this field. 'advisory' "
                "downgrades a runtime success to succeeded_with_warnings if verification fails, but never "
                "blocks it. 'required' blocks a runtime success into failed if any declared command fails, "
                "or if verification_policy is 'required' but no verify_commands were declared at all."
            ),
        },
        "parent_task_id": {
            "type": "string",
            "description": "Optional existing broker task parent for parent-directed side-agent composition.",
        },
        "external_root_id": {
            "type": "string",
            "description": "Audit-only host/conversation/operation grouping. Does not require a broker parent.",
        },
        "relationship": {
            "type": "string",
            "enum": list(VALID_RELATIONSHIPS),
            "description": "Descriptive child relationship when parent_task_id is set. continues marks a follow-up new task, not session resume.",
        },
        "origin_kind": {
            "type": "string",
            "enum": list(VALID_ORIGIN_KINDS),
            "description": (
                "Audit-only provenance class (not used for authorization). Defaults to host for "
                "CLI/MCP delegation, including when parent_task_id is set. side_agent is reserved "
                "for a future explicit recursive callback path."
            ),
        },
        "origin_ref": {
            "type": "string",
            "description": "Audit-only caller/reference string.",
        },
    },
    "required": ["task", "workspace"],
}

DELEGATE_BATCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": DELEGATE_INPUT_SCHEMA,
            "minItems": 1,
            "description": "Independent delegate requests. Each is validated and started independently; one invalid item never affects another.",
        },
    },
    "required": ["tasks"],
}

_TASK_ID_PROPERTY = {"task_id": {"type": "string", "description": "Task id returned by delegate or delegate_batch."}}

STATUS_INPUT_SCHEMA = {"type": "object", "properties": dict(_TASK_ID_PROPERTY), "required": ["task_id"]}
COLLECT_INPUT_SCHEMA = {"type": "object", "properties": dict(_TASK_ID_PROPERTY), "required": ["task_id"]}
CANCEL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        **_TASK_ID_PROPERTY,
        "reason": {"type": "string", "default": "Cancelled by MCP caller", "description": "Human-readable reason recorded on the task."},
    },
    "required": ["task_id"],
}
CONTROL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        **_TASK_ID_PROPERTY,
        "action": {
            "type": "string",
            "enum": ["status", "cancel", "collect", "message"],
            "description": "Explicit operator control action. message is always an explicit unsupported refusal.",
        },
        "reason": {
            "type": "string",
            "default": "Cancelled by operator control",
            "description": "Human-readable reason when action is cancel.",
        },
        "content": {
            "type": "string",
            "description": "Required when action is message (always refused; no in-flight steering).",
        },
    },
    "required": ["task_id", "action"],
}
MESSAGE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        **_TASK_ID_PROPERTY,
        "content": {"type": "string", "description": "Message that would have been steered into the running task."},
    },
    "required": ["task_id", "content"],
}
RECONCILE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "Reconcile only this task. Omit to reconcile every running/recovery_required subprocess-backed (opencode, claude_code, or codex) task this broker instance can see without an in-memory process handle (e.g. right after a restart).",
        },
    },
}
DISCOVER_CAPABILITIES_INPUT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
SELECT_CANDIDATES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "execution_mode": {"type": "string", "enum": list(EXECUTION_MODES)},
        "allowed_runtimes": {"type": "array", "items": {"type": "string"}},
        "allowed_providers": {"type": "array", "items": {"type": "string"}},
        "required_runtime_capabilities": {"type": "object", "additionalProperties": {"type": "boolean"}},
        "required_provider_capabilities": {"type": "object", "additionalProperties": {"type": "boolean"}},
        "require_available": {"type": "boolean", "default": True},
    },
    "required": ["execution_mode"],
}
COUNCIL_PLAN_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "object",
            "description": "Parent-directed bounded council plan with stages, bounds, and acceptance_criteria.",
        },
    },
    "required": ["plan"],
}
TASK_CHILDREN_INPUT_SCHEMA = {"type": "object", "properties": dict(_TASK_ID_PROPERTY), "required": ["task_id"]}
TASK_TREE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "root_task_id": {
            "type": "string",
            "description": "Broker-tree root task id (must match the task's persisted root_task_id).",
        },
    },
    "required": ["root_task_id"],
}
COMPLETION_EVENTS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "after_event_id": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "Exclusive lower bound on durable global event id.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 256,
            "default": 64,
            "description": "Maximum completion events to return in chronological order.",
        },
        "task_id": {"type": "string", "description": "Optional filter to one task id."},
        "root_task_id": {"type": "string", "description": "Optional filter to one broker root_task_id lineage."},
        "completion_only": {
            "type": "boolean",
            "default": True,
            "description": "When true (default), return terminal and recovery_required completion signals only.",
        },
        "states": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional explicit completion-state filter (repeatable values in one array).",
        },
    },
}

TOOLS = {
    "delegate": {
        "description": "Create and start one bounded delegated task. Returns the task id, resulting state, and workspace context — never a fabricated completion.",
        "inputSchema": DELEGATE_INPUT_SCHEMA,
        "handler": handle_delegate,
    },
    "delegate_batch": {
        "description": "Create and start a batch of delegated tasks independently. Each item is validated and started on its own; one rejected item does not affect the others.",
        "inputSchema": DELEGATE_BATCH_INPUT_SCHEMA,
        "handler": handle_delegate_batch,
    },
    "status": {
        "description": "Return a task's durable state, its event history, and its artifact manifest.",
        "inputSchema": STATUS_INPUT_SCHEMA,
        "handler": handle_status,
    },
    "collect": {
        "description": (
            "Collect a completed task's runtime-reported result plus a provenance-aware normalized envelope "
            "(runtime_reported vs broker_observed vs parser). Returns artifact references, not full raw logs. "
            "Broker-verified command evidence appears in broker_verification when verify_commands were declared. "
            "verification_gate.label distinguishes runtime_reported vs advisory/required verification outcomes."
        ),
        "inputSchema": COLLECT_INPUT_SCHEMA,
        "handler": handle_collect,
    },
    "cancel": {
        "description": "Request cancellation of a task and return the factual resulting state and cancellation evidence.",
        "inputSchema": CANCEL_INPUT_SCHEMA,
        "handler": handle_cancel,
    },
    "control": {
        "description": (
            "Bounded operator recovery/control with an explicit action. Returns a secret-safe "
            "recovery/control view (task/launch identity, posture, permitted actions, refusal "
            "reasons) and executes status/cancel/collect only when the 7C.3 gates permit. "
            "message is always an explicit unsupported refusal."
        ),
        "inputSchema": CONTROL_INPUT_SCHEMA,
        "handler": handle_control,
    },
    "message": {
        "description": "Always returns an explicit unsupported response: Recollect Lines has no in-flight steering channel for any adapter.",
        "inputSchema": MESSAGE_INPUT_SCHEMA,
        "handler": handle_message,
    },
    "reconcile": {
        "description": (
            "Reconcile one (or, if task_id is omitted, every) subprocess-backed (opencode, claude_code, or codex) task's durable runtime-launch record "
            "against its actual process-group liveness. Use this after a broker restart, before collect/cancel, "
            "to safely discover whether an in-flight task's process is confirmed dead (moves to failed) or still "
            "alive (moves to recovery_required — never a fabricated success, never an unsafe workspace deletion)."
        ),
        "inputSchema": RECONCILE_INPUT_SCHEMA,
        "handler": handle_reconcile,
    },
    "discover_capabilities": {
        "description": (
            "Return a machine-readable inventory of registered runtime profiles and named provider "
            "configurations with declared/observed capabilities and availability (no credentials or "
            "raw endpoints). Includes provider_config: the active provider configuration source path "
            "(or not_configured), which precedence tier selected it (source_origin), and when this "
            "process loaded it — a startup snapshot; edits to the file on disk require restarting "
            "the broker/MCP server to take effect."
        ),
        "inputSchema": DISCOVER_CAPABILITIES_INPUT_SCHEMA,
        "handler": handle_discover_capabilities,
    },
    "select_candidates": {
        "description": "Parent-directed capability filtering: return eligible runtimes/providers plus exclusion evidence. Does not choose a winner.",
        "inputSchema": SELECT_CANDIDATES_INPUT_SCHEMA,
        "handler": handle_select_candidates,
    },
    "council_validate": {
        "description": "Validate a parent-specified bounded council plan (graph, bounds, candidate availability) without executing it.",
        "inputSchema": COUNCIL_PLAN_INPUT_SCHEMA,
        "handler": handle_council_validate,
    },
    "council_execute": {
        "description": "Execute a validated bounded council plan through broker lifecycle primitives and record stage evidence for parent synthesis (no autonomous winner).",
        "inputSchema": COUNCIL_PLAN_INPUT_SCHEMA,
        "handler": handle_council_execute,
    },
    "task_children": {
        "description": "List concise summaries of direct child tasks for a parent task id.",
        "inputSchema": TASK_CHILDREN_INPUT_SCHEMA,
        "handler": handle_task_children,
    },
    "task_tree": {
        "description": "Return a deterministic bounded task tree for a broker root_task_id (concise summaries only).",
        "inputSchema": TASK_TREE_INPUT_SCHEMA,
        "handler": handle_task_tree,
    },
    "completion_events": {
        "description": (
            "Poll durable completion signals from the global append-only event cursor. "
            "Returns compact terminal/recovery summaries with lineage and normalized result hints — never raw logs."
        ),
        "inputSchema": COMPLETION_EVENTS_INPUT_SCHEMA,
        "handler": handle_completion_events,
    },
}


def _dispatch_tool_call(broker: Broker, name: str, arguments: dict) -> dict:
    try:
        data = TOOLS[name]["handler"](broker, arguments)
    except OperatorControlRefused as error:
        return _tool_result(
            name,
            False,
            error={
                "code": error.code,
                "message": error.message,
                "control": error.control,
            },
        )
    except (ValueError, KeyError) as error:
        return _tool_result(name, False, error={"code": type(error).__name__, "message": str(error)})
    except Exception as error:
        print(f"recollect_lines.mcp_server: unexpected error in tool {name!r}: {error!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return _tool_result(name, False, error={"code": "InternalError", "message": "Unexpected server error while executing this tool"})
    return _tool_result(name, True, data=data)


# --- JSON-RPC methods --------------------------------------------------


def _handle_initialize(params: Any) -> dict:
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _handle_tools_list() -> dict:
    return {"tools": [{"name": name, "description": tool["description"], "inputSchema": tool["inputSchema"]} for name, tool in TOOLS.items()]}


def _handle_tools_call(broker: Broker, params: Any) -> dict:
    if not isinstance(params, dict):
        raise ProtocolError(INVALID_PARAMS, "tools/call params must be an object")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise ProtocolError(INVALID_PARAMS, "tools/call requires a non-empty string 'name'")
    if name not in TOOLS:
        raise ProtocolError(INVALID_PARAMS, f"Unknown tool: {name}")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ProtocolError(INVALID_PARAMS, "tools/call 'arguments' must be an object")
    return _dispatch_tool_call(broker, name, arguments)


def _error_response(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _success_response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _process_message(broker: Broker, message: Any) -> dict | None:
    """Return a JSON-RPC response, or None if none should be sent (a notification)."""
    if not isinstance(message, dict):
        return _error_response(None, INVALID_REQUEST, "Each message must be a JSON object")
    is_notification = "id" not in message
    request_id = message.get("id")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return None if is_notification else _error_response(request_id, INVALID_REQUEST, "Message must be a JSON-RPC 2.0 request with a string 'method'")
    method = message["method"]
    params = message.get("params", {})
    try:
        if method == "initialize":
            result = _handle_initialize(params)
        elif method == "notifications/initialized":
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = _handle_tools_list()
        elif method == "tools/call":
            result = _handle_tools_call(broker, params)
        else:
            return None if is_notification else _error_response(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
    except ProtocolError as error:
        return None if is_notification else _error_response(request_id, error.code, error.message)
    return None if is_notification else _success_response(request_id, result)


def _write_message(outstream, message: dict) -> None:
    outstream.write(json.dumps(message) + "\n")
    outstream.flush()


def serve(broker: Broker, instream, outstream) -> None:
    for raw_line in instream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            _write_message(outstream, _error_response(None, PARSE_ERROR, f"Invalid JSON on a request line: {error}"))
            continue
        try:
            response = _process_message(broker, message)
        except Exception as error:
            print(f"recollect_lines.mcp_server: internal error handling message: {error!r}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            response = _error_response(message.get("id") if isinstance(message, dict) else None, INTERNAL_ERROR, "Unexpected internal server error")
        if response is not None:
            _write_message(outstream, response)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recollect-mcp", description="Local stdio MCP interface for the Recollect Lines broker.")
    parser.add_argument("--home", type=Path, default=Path(".recollect"), help="Broker home directory (matches `recollect-lines --home`).")
    parser.add_argument(
        "--opencode-command", default=None,
        help=(
            "Advanced: override the opencode adapter's command prefix as a JSON array "
            "(e.g. to pin a specific opencode-ai version, or point at a deterministic "
            "stand-in binary for testing/acceptance). Defaults to the built-in npx opencode-ai invocation."
        ),
    )
    parser.add_argument(
        "--claude-command", default=None,
        help=(
            "Advanced: override the Claude Code adapter's command prefix as a JSON array "
            "(e.g. to point at a deterministic stand-in binary for testing/acceptance). "
            "Defaults to the built-in `claude` CLI invocation."
        ),
    )
    parser.add_argument(
        "--codex-command", default=None,
        help=(
            "Advanced: override the Codex adapter's command prefix as a JSON array "
            "(e.g. to point at a deterministic stand-in binary for testing/acceptance). "
            "Defaults to the built-in `codex` CLI invocation."
        ),
    )
    parser.add_argument(
        "--cursor-command", default=None,
        help=(
            "Advanced: override the Cursor adapter's command prefix as a JSON array "
            "(e.g. to point at a deterministic stand-in binary for testing/acceptance). "
            "Defaults to the built-in `cursor-agent` CLI invocation."
        ),
    )
    parser.add_argument(
        "--providers-config", type=Path, default=None,
        help=(
            "Path to a JSON or YAML provider configuration file (required for openai_compatible "
            "profile tasks). Highest-precedence source; see docs/mcp.md for the full resolution "
            "order (RECOLLECT_CONFIG env var, repo-local/user-level operator config, then the "
            "legacy providers.json default)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    opencode_adapter = OpenCodeAdapter(command_prefix=tuple(json.loads(args.opencode_command))) if args.opencode_command else None
    claude_code_adapter = ClaudeCodeAdapter(command_prefix=tuple(json.loads(args.claude_command))) if args.claude_command else None
    codex_adapter = CodexAdapter(command_prefix=tuple(json.loads(args.codex_command))) if args.codex_command else None
    cursor_adapter = CursorAdapter(command_prefix=tuple(json.loads(args.cursor_command))) if args.cursor_command else None
    resolved_config = resolve_providers_config_source(
        explicit=args.providers_config,
        environ=os.environ,
        repo_root=Path.cwd(),
        user_home=Path.home(),
    )
    broker = Broker(
        args.home,
        opencode_adapter=opencode_adapter,
        claude_code_adapter=claude_code_adapter,
        codex_adapter=codex_adapter,
        cursor_adapter=cursor_adapter,
        providers_config=resolved_config.path,
        providers_config_origin=resolved_config.origin,
    )
    try:
        serve(broker, sys.stdin, sys.stdout)
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
