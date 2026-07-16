#!/usr/bin/env python3
"""Host-side polling example for durable completion-event cursors (MR 8.7).

Poll the broker's global append-only event cursor and print compact completion
signals. This is a pull contract — no daemon, webhook, or push transport.

Usage:
    python3 examples/completion-event-polling/poll_completions.py --home .recollect

Environment:
    Runs offline against an existing broker home directory. It never makes
    provider calls or spawns side-agent runtimes on its own.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from recollect_lines.service import Broker  # noqa: E402


def format_completion(event: dict) -> str:
    summary = (event.get("result_summary") or {}).get("summary")
    gate = (event.get("metadata") or {}).get("verification_gate") or {}
    gate_label = gate.get("label")
    parts = [
        f"event={event['event_id']}",
        f"task={event['task_id']}",
        f"state={event['state']}",
        f"type={event['event_type']}",
    ]
    if event.get("root_task_id"):
        parts.append(f"root={event['root_task_id']}")
    if summary:
        parts.append(f"summary={summary!r}")
    if gate_label:
        parts.append(f"verification={gate_label}")
    if event.get("artifact_count") is not None:
        parts.append(f"artifacts={event['artifact_count']}")
    return " ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll durable broker completion events")
    parser.add_argument("--home", type=Path, default=Path(".recollect"))
    parser.add_argument("--after-event-id", type=int, default=0)
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--once", action="store_true", help="Poll one page and exit")
    parser.add_argument("--json", action="store_true", help="Emit the raw broker page as JSON")
    args = parser.parse_args()

    broker = Broker(args.home)
    cursor = args.after_event_id
    try:
        while True:
            page = broker.completion_events_since(cursor, limit=args.limit)
            if args.json:
                print(json.dumps(page, indent=2, sort_keys=True))
            else:
                if not page["events"]:
                    print(f"no new completion events (cursor={cursor}, high_water={page['high_water_mark']})")
                for event in page["events"]:
                    print(format_completion(event))
            cursor = page["next_cursor"]
            if args.once or not page["has_more"]:
                break
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
