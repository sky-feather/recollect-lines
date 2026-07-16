"""Deterministic stand-in for the real Codex CLI (`codex exec`), used only in tests.

Mirrors the `codex exec --json` NDJSON event shape confirmed against codex-cli
0.144.4 during the Phase 6B compatibility spike (see docs/history/phases/phase-6b.md):
thread/turn lifecycle plus `item.completed` agent_message events. Behavior is
selected by keywords in the trailing prompt argument so a single fixture can
cover normal, malformed-output, error, and long-running scenarios without any
real model calls, network access, or API cost.
"""
import json
import signal
import sys
import time


def emit(obj):
    print(json.dumps(obj), flush=True)


def prompt_from_argv(args):
    if not args:
        return ""
    if args[0] == "exec":
        args = args[1:]
    # CodexAdapter.build_command() always places the prompt as the final argv entry.
    last = args[-1]
    return last if not last.startswith("-") else ""


def agent_message(text):
    emit({"type": "item.completed", "item": {"id": "item_msg", "type": "agent_message", "text": text}})


def task_prompt_only(prompt: str) -> str:
    marker = "[recollect-lines result-schema contract"
    marker_at = prompt.find(marker)
    if marker_at >= 0:
        return prompt[:marker_at].rstrip()
    return prompt


def main():
    args = sys.argv[1:]
    prompt = prompt_from_argv(args)

    emit({"type": "thread.started", "thread_id": "thread_fake"})
    emit({"type": "turn.started"})

    if "SLEEP_IGNORE_TERM" in prompt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("started", file=sys.stderr, flush=True)
        time.sleep(30)
        agent_message("slept without being cancelled")
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
        return 0

    if "SLEEP_BRIEF" in prompt:
        print("started", file=sys.stderr, flush=True)
        time.sleep(0.4)
        agent_message("brief sleep complete")
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
        return 0

    if "SLEEP" in prompt:
        print("started", file=sys.stderr, flush=True)
        time.sleep(30)
        agent_message("slept without being cancelled")
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
        return 0

    if "MALFORMED" in prompt:
        print("{not valid json", flush=True)
        agent_message("partial result despite a malformed line")
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
        return 0

    if "AUTH_ERROR" in prompt:
        emit({"type": "turn.failed", "error": {"message": "authentication failed: HTTP 401 unauthorized"}})
        return 1

    if "RATE_LIMIT" in prompt:
        emit({"type": "turn.failed", "error": {"message": "rate limit exceeded (429)"}})
        return 1

    if "NONZERO_EXIT" in prompt:
        agent_message("about to fail")
        print("boom", file=sys.stderr, flush=True)
        return 1

    if "KILLED_AFTER_RESULT" in prompt:
        agent_message("looked fine right up until it wasn't")
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
        return 1

    if "EMPTY_OUTPUT" in prompt:
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
        return 0

    if "STRUCTURED" in prompt:
        agent_message(json.dumps({"status": "codex-fixture-ok", "answer": 42}))
        emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
        return 0

    body = task_prompt_only(prompt)
    if "SCHEMA_" in body:
        schema_part = body[body.index("SCHEMA_"):]
        payload = schema_part.split(" ", 1)[1] if " " in schema_part else schema_part
        agent_message(payload)
        emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
        return 0

    agent_message(f"42 (from {prompt})")
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
