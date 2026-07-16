"""Deterministic stand-in for the real `opencode-ai` CLI, used only in tests.

Behavior is selected by keywords in the trailing prompt argument so a single
fixture can cover the normal, malformed-output, and long-running scenarios
without any real model calls or network access.
"""
import json
import signal
import sys
import time


def emit(event):
    print(json.dumps(event), flush=True)


def main():
    args = sys.argv[1:]
    prompt = args[-1] if args else ""
    workspace = args[args.index("--dir") + 1] if "--dir" in args else None

    if "SLEEP_IGNORE_TERM" in prompt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        emit({"type": "started", "workspace": workspace})
        time.sleep(30)
        return 0

    if "SLEEP" in prompt:
        emit({"type": "started", "workspace": workspace})
        time.sleep(30)
        return 0

    if "MALFORMED" in prompt:
        print("{not valid json", flush=True)
        emit({"type": "text", "text": "partial result despite malformed line"})
        return 0

    if "NESTED_PART" in prompt:
        # Mirrors the real `opencode run --format json` event shape, where the
        # top-level event only carries type="text"; the actual text lives at
        # event["part"]["text"], not event["text"].
        emit({"type": "text", "part": {"type": "text", "text": f"nested summary for {workspace}"}})
        return 0

    if "NONZERO_EXIT" in prompt:
        emit({"type": "text", "text": "about to fail"})
        print("boom", file=sys.stderr, flush=True)
        return 1

    emit({"type": "started", "workspace": workspace})
    emit({"type": "tool_call", "name": "read_file"})
    emit({"type": "text", "text": f"All checks passed for {workspace}"})
    print("noise on stderr", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
