"""Shared subprocess process-group lifecycle helpers used by multiple CLI adaptors."""

from __future__ import annotations

import os
import time

# ponytail: a flag-name heuristic, not exhaustive secret scanning. Nothing in
# typical command_prefix/build_command currently carries a secret, but any
# future argv-based flag whose name looks like one of these is redacted
# before the command is persisted durably (see Broker.start()/record_launch).
SECRET_FLAG_MARKERS = ("token", "key", "secret", "password", "credential")
REDACTED_VALUE = "***REDACTED***"


def redact_command(command: list) -> list:
    """Best-effort redaction of a suspicious flag's value, in either
    `--flag value` (two argv entries) or `--flag=value` (one entry) form.

    Only argv entries that look like a flag (leading `-`) are checked against
    SECRET_FLAG_MARKERS — otherwise an already-redacted or coincidentally
    marker-shaped *value* (e.g. a task prompt containing the word "secret")
    would itself be misread as a flag on the next iteration and cascade into
    redacting the following, unrelated argument.
    """
    redacted = list(command)
    for index, part in enumerate(redacted):
        if not part.startswith("-"):
            continue
        if "=" in part:
            flag_name, _, _value = part.partition("=")
            if any(marker in flag_name.lower().lstrip("-") for marker in SECRET_FLAG_MARKERS):
                redacted[index] = f"{flag_name}={REDACTED_VALUE}"
            continue
        if index + 1 >= len(redacted):
            continue
        flag = part.lower().lstrip("-")
        if any(marker in flag for marker in SECRET_FLAG_MARKERS):
            redacted[index + 1] = REDACTED_VALUE
    return redacted


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_dead_within(pgid: int, timeout: float, interval: float = 0.05) -> bool:
    # SIGKILL delivery is asynchronous: a grandchild (e.g. the real `opencode`
    # process under npm's `sh -c` wrapper) can outlive the top-level popen.wait()
    # by a beat while the kernel tears it down. A single instantaneous check right
    # after the signal races that teardown, so poll briefly instead.
    deadline = time.monotonic() + timeout
    while group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True
