"""Deterministic stand-in for the real Claude Code CLI (`claude -p`), used only in tests.

Mirrors the real `--output-format json` shape confirmed against the installed
CLI during the Phase 6A compatibility spike (see docs/history/phases/phase-6a.md): exactly
one JSON object printed to stdout on exit, carrying `is_error`, `result`,
`api_error_status`, and `session_id`. Behavior is selected by keywords in the
prompt (the positional argument immediately after `-p`, per
ClaudeCodeAdapter.build_command) so a single fixture can cover the normal,
malformed-output, error, and long-running scenarios without any real model
calls, network access, or API cost.
"""
import json
import signal
import sys
import time


def emit(obj):
    print(json.dumps(obj), flush=True)


def result(text, session_id="ses_fake", is_error=False, api_error_status=None):
    emit({
        "type": "result",
        "subtype": "success" if not is_error else "error",
        "is_error": is_error,
        "api_error_status": api_error_status,
        "result": text,
        "session_id": session_id,
        "num_turns": 1,
        "permission_denials": [],
    })


def task_prompt_only(prompt: str) -> str:
    marker = "[recollect-lines result-schema contract"
    marker_at = prompt.find(marker)
    if marker_at >= 0:
        return prompt[:marker_at].rstrip()
    return prompt


def main():
    args = sys.argv[1:]
    prompt = args[args.index("-p") + 1] if "-p" in args else ""

    if "SLEEP_IGNORE_TERM" in prompt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("started", file=sys.stderr, flush=True)
        time.sleep(30)
        return 0

    if "SLEEP_BRIEF" in prompt:
        print("started", file=sys.stderr, flush=True)
        time.sleep(0.4)
        result("brief sleep complete")
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
        result("authentication failed", is_error=True, api_error_status=401)
        return 0

    if "NOT_LOGGED_IN" in prompt:
        # Missing/non-interactive auth is a launch-time failure, not an
        # in-band API error: the real CLI never reaches a model call, so it
        # never prints a `--output-format json` result object at all — just
        # a stderr hint and a nonzero exit.
        print("Invalid API key · Please run /login", file=sys.stderr, flush=True)
        return 1

    if "RATE_LIMIT" in prompt:
        result("rate limited", is_error=True, api_error_status=429)
        return 0

    if "NONZERO_EXIT" in prompt:
        print("boom", file=sys.stderr, flush=True)
        return 1

    if "KILLED_AFTER_RESULT" in prompt:
        # A clean, successful-looking is_error:false result was already
        # flushed when something external (timeout, OOM) killed the process
        # before it could exit 0 — the exit code is the only signal this
        # actually failed.
        result("looked fine right up until it wasn't")
        return 1

    if "EMPTY_OUTPUT" in prompt:
        return 0

    body = task_prompt_only(prompt)
    if "SCHEMA_" in body:
        schema_part = body[body.index("SCHEMA_"):]
        payload = schema_part.split(" ", 1)[1] if " " in schema_part else schema_part
        result(payload)
        return 0

    result(f"42 (from {prompt})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
