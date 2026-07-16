"""Deterministic stand-in for the real Cursor Agent CLI (`cursor-agent --print`), used only in tests.

Mirrors the `--output-format json` shape confirmed against cursor-agent
2026.07.09-a3815c0 during the Phase 6B.5 compatibility spike (see
docs/history/phases/phase-6b5.md): exactly one JSON object printed to stdout on exit,
carrying `type`, `subtype`, `is_error`, `result`, `session_id`, `duration_ms`,
and `usage`. Behavior is selected by keywords in the trailing prompt argument
so a single fixture can cover normal, malformed-output, error, and long-running
scenarios without any real model calls, network access, or API cost.
"""
import json
import signal
import sys
import time


def emit(obj):
    print(json.dumps(obj), flush=True)


def result(text, session_id="ses_fake", is_error=False, subtype=None):
    emit({
        "type": "result",
        "subtype": subtype or ("error" if is_error else "success"),
        "is_error": is_error,
        "result": text,
        "session_id": session_id,
        "request_id": "req_fake",
        "duration_ms": 100,
        "duration_api_ms": 100,
        "usage": {"inputTokens": 1, "outputTokens": 1, "cacheReadTokens": 0, "cacheWriteTokens": 0},
    })


def prompt_from_argv(args):
    if not args:
        return ""
    last = args[-1]
    return last if not last.startswith("-") else ""


def main():
    args = sys.argv[1:]
    prompt = prompt_from_argv(args)

    if "SLEEP_IGNORE_TERM" in prompt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("started", file=sys.stderr, flush=True)
        time.sleep(30)
        result("slept without being cancelled")
        return 0

    if "SLEEP" in prompt:
        print("started", file=sys.stderr, flush=True)
        time.sleep(30)
        result("slept without being cancelled")
        return 0

    if "MALFORMED" in prompt:
        print("{not valid json", flush=True)
        result("partial result despite a malformed line")
        return 0

    if "AUTH_ERROR" in prompt:
        result("authentication failed: HTTP 401 unauthorized", is_error=True)
        return 1

    if "RATE_LIMIT" in prompt:
        result("rate limit exceeded (429)", is_error=True)
        return 1

    if "NONZERO_EXIT" in prompt:
        print("boom", file=sys.stderr, flush=True)
        return 1

    if "KILLED_AFTER_RESULT" in prompt:
        result("looked fine right up until it wasn't")
        return 1

    if "EMPTY_OUTPUT" in prompt:
        return 0

    result(f"42 (from {prompt})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
