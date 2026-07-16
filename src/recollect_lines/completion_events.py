"""Durable global completion-event cursor (MR 8.7).

Hosts poll append-only broker events by monotonic event id. Payloads are compact
and evidence-aware — never raw runtime logs.
"""

from __future__ import annotations

import json
from typing import Any

from .models import TERMINAL_STATES, TaskState, verification_gate_label
from .result_normalization import NORMALIZED_RESULT_ARTIFACT, concise_normalized_view

DEFAULT_COMPLETION_EVENTS_LIMIT = 64
MAX_COMPLETION_EVENTS_LIMIT = 256

# Terminal outcomes plus recovery_required, which hosts must observe even though
# it is actionable rather than strictly terminal.
COMPLETION_CURSOR_STATES = frozenset({state.value for state in TERMINAL_STATES} | {TaskState.RECOVERY_REQUIRED.value})


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > MAX_COMPLETION_EVENTS_LIMIT:
        raise ValueError(f"limit cannot exceed {MAX_COMPLETION_EVENTS_LIMIT}")
    return limit


def _validate_after_event_id(after_event_id: Any) -> int:
    if not isinstance(after_event_id, int) or isinstance(after_event_id, bool):
        raise ValueError("after_event_id must be a non-negative integer")
    if after_event_id < 0:
        raise ValueError("after_event_id must be non-negative")
    return after_event_id


def _compact_cancellation(metadata: dict[str, Any]) -> dict[str, Any] | None:
    cancellation = metadata.get("cancellation")
    if not isinstance(cancellation, dict):
        return None
    signals = cancellation.get("signals_sent")
    compact: dict[str, Any] = {}
    if "group_terminated" in cancellation:
        compact["group_terminated"] = bool(cancellation["group_terminated"])
    if isinstance(signals, list):
        compact["signals_sent_count"] = len(signals)
    return compact or None


def _compact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    gate = metadata.get("verification_gate")
    if isinstance(gate, dict):
        compact["verification_gate"] = {
            "policy": gate.get("policy"),
            "outcome": gate.get("outcome"),
            "label": verification_gate_label(gate),
        }
    if metadata.get("exit_code") is not None:
        compact["exit_code"] = metadata["exit_code"]
    reason = metadata.get("reason")
    if isinstance(reason, str) and reason.strip():
        compact["reason"] = reason.strip()
    cancellation = _compact_cancellation(metadata)
    if cancellation is not None:
        compact["cancellation"] = cancellation
    for key in ("result_artifact", "normalized_result_artifact"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            compact[key] = value
    return compact


def _artifact_count(store: Any, task_id: str) -> int | None:
    try:
        manifest = store.artifact_manifest(task_id)
    except KeyError:
        return None
    files = manifest.get("files")
    return len(files) if isinstance(files, list) else None


def _result_summary(store: Any, task_id: str) -> dict[str, Any] | None:
    normalized_path = store.artifacts / task_id / NORMALIZED_RESULT_ARTIFACT
    if normalized_path.is_file():
        view = concise_normalized_view(json.loads(normalized_path.read_text()))
        if view is not None:
            view.pop("raw_output_artifact", None)
        return view
    result_path = store.artifacts / task_id / "result.json"
    if not result_path.is_file():
        return None
    result = json.loads(result_path.read_text())
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    return {"summary": summary.strip()}


def build_completion_event(store: Any, row: dict[str, Any]) -> dict[str, Any]:
    metadata = json.loads(row["metadata_json"])
    state = row.get("state_after")
    payload: dict[str, Any] = {
        "event_id": row["id"],
        "task_id": row["task_id"],
        "event_type": row["type"],
        "timestamp": row["timestamp"],
        "state": state,
        "runtime": row["runtime"],
    }
    for field in (
        "model",
        "effective_model",
        "agent_profile",
        "parent_task_id",
        "root_task_id",
        "external_root_id",
        "delegation_depth",
        "relationship",
        "origin_kind",
        "origin_ref",
    ):
        value = row.get(field)
        if value is not None and value != "":
            payload[field] = value
    compact_meta = _compact_metadata(metadata)
    if compact_meta:
        payload["metadata"] = compact_meta
    artifact_count = _artifact_count(store, row["task_id"])
    if artifact_count is not None:
        payload["artifact_count"] = artifact_count
    summary = _result_summary(store, row["task_id"])
    if summary is not None:
        payload["result_summary"] = summary
    return payload


def completion_events_page(
    store: Any,
    *,
    after_event_id: int = 0,
    limit: int = DEFAULT_COMPLETION_EVENTS_LIMIT,
    task_id: str | None = None,
    root_task_id: str | None = None,
    completion_only: bool = True,
    states: frozenset[str] | None = None,
) -> dict[str, Any]:
    after_event_id = _validate_after_event_id(after_event_id)
    limit = _clamp_limit(limit)
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        raise ValueError("task_id must be a non-empty string when provided")
    if root_task_id is not None and (not isinstance(root_task_id, str) or not root_task_id.strip()):
        raise ValueError("root_task_id must be a non-empty string when provided")
    if states is not None:
        unknown = sorted(state for state in states if state not in COMPLETION_CURSOR_STATES)
        if unknown:
            raise ValueError(f"unsupported completion state filter(s): {', '.join(unknown)}")
        state_filter = states
    elif completion_only:
        state_filter = COMPLETION_CURSOR_STATES
    else:
        state_filter = None

    rows, high_water_mark = store.events_since(
        after_event_id,
        limit=limit + 1,
        task_id=task_id.strip() if task_id else None,
        root_task_id=root_task_id.strip() if root_task_id else None,
        state_after_in=state_filter,
    )
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    events = [build_completion_event(store, row) for row in rows]
    next_cursor = events[-1]["event_id"] if events else after_event_id
    return {
        "after_event_id": after_event_id,
        "next_cursor": next_cursor,
        "high_water_mark": high_water_mark,
        "events": events,
        "has_more": has_more,
    }
