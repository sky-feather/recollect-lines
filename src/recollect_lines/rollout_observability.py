"""Read-only durable-subprocess rollout observability.

This module deliberately projects persisted task/event history only.  It never
chooses an adapter, changes a launch mode, reconciles a process, or removes a
workspace.  That makes the report safe to run during a staged rollout.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from .models import TERMINAL_STATES

SCHEMA_VERSION = 1


def _parse_timestamp(value: str) -> datetime:
    """Parse the repository's ISO-8601 event timestamps, including ``Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration_ms(start: str, end: str) -> int | None:
    try:
        return max(0, int((_parse_timestamp(end) - _parse_timestamp(start)).total_seconds() * 1000))
    except (TypeError, ValueError):
        return None


def durable_rollout_report(store: Any) -> dict[str, Any]:
    """Summarize durable launch outcomes from the public ``TaskStore`` API.

    ``terminal_event_latency_ms`` measures persisted `task.running` to the
    first terminal task event.  It is an event-observation latency, not a
    provider-reported duration and not a claim that the child ran for exactly
    that interval.
    """
    by_adapter: dict[str, dict[str, Any]] = {}
    recovery_outcomes: Counter[str] = Counter()
    terminal_latencies: list[int] = []
    durable_tasks = 0

    for record in store.list():
        launch = store.get_launch(record.id) or {}
        if launch.get("launch_kind") != "durable_subprocess":
            continue
        durable_tasks += 1
        adapter = str(launch.get("adapter") or record.profile or "unknown")
        bucket = by_adapter.setdefault(adapter, {
            "durable_tasks": 0,
            "terminal_outcomes": {},
            "recovery_outcomes": {},
            "terminal_event_latency_ms": [],
        })
        bucket["durable_tasks"] += 1
        events = store.events(record.id)
        running = next((event for event in events if event["type"] == "task.running"), None)
        terminal = next((event for event in events if event["state_after"] in {state.value for state in TERMINAL_STATES}), None)
        if terminal is not None:
            outcome = str(terminal["state_after"])
            bucket["terminal_outcomes"][outcome] = bucket["terminal_outcomes"].get(outcome, 0) + 1
            if running is not None:
                latency = _duration_ms(running["timestamp"], terminal["timestamp"])
                if latency is not None:
                    terminal_latencies.append(latency)
                    bucket["terminal_event_latency_ms"].append(latency)
        for event in events:
            if event["type"] in {"task.durable_adopted", "task.recovery_required", "task.uncollected"}:
                recovery_outcomes[event["type"]] += 1
                bucket["recovery_outcomes"][event["type"]] = bucket["recovery_outcomes"].get(event["type"], 0) + 1

    for bucket in by_adapter.values():
        values = bucket["terminal_event_latency_ms"]
        bucket["terminal_event_latency_ms"] = {
            "count": len(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "average": (sum(values) // len(values)) if values else None,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "persisted_durable_subprocess_tasks",
        "durable_task_count": durable_tasks,
        "terminal_event_latency_ms": {
            "count": len(terminal_latencies),
            "min": min(terminal_latencies) if terminal_latencies else None,
            "max": max(terminal_latencies) if terminal_latencies else None,
            "average": (sum(terminal_latencies) // len(terminal_latencies)) if terminal_latencies else None,
        },
        "recovery_outcomes": dict(sorted(recovery_outcomes.items())),
        "by_adapter": dict(sorted(by_adapter.items())),
    }
