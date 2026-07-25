"""Small typed subclass template for subprocess CLI adaptors.

Concrete adaptors retain their own build_command, runtime-specific parsing,
policy exceptions, error classification, and collection/recovery behavior.
Use shared process primitives from ``process`` only when signal semantics match.
"""

from __future__ import annotations

import subprocess
from abc import ABC
from collections.abc import Callable


class SubprocessCliAdapterBase(ABC):
    """Optional base scaffolding for CLI adaptors with a command prefix and grace period."""

    name: str
    command_prefix: tuple[str, ...]
    grace_period_seconds: float

    @property
    def runtime_label(self) -> str:
        """A human-readable adapter/version label for durable launch records."""
        return self.command_prefix[-1] if self.command_prefix else self.name


def probe_cli_version(
    command_prefix: tuple[str, ...],
    *,
    timeout: float,
    redact_secrets: Callable[[str], str],
    version_from_stdout_only: bool = False,
) -> dict:
    """Best-effort, side-effect-free probe of whether a CLI is installed and runnable."""
    try:
        completed = subprocess.run(
            [*command_prefix, "--version"], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return {
            "available": False,
            "reason": "cli_not_found",
            "detail": f"{command_prefix[0]!r} was not found on PATH",
        }
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "reason": "version_check_timed_out",
            "detail": f"--version did not return within {timeout}s",
        }
    if completed.returncode != 0:
        detail = redact_secrets((completed.stderr or completed.stdout or "").strip()[:500])
        return {"available": False, "reason": "version_check_failed", "detail": detail}
    if version_from_stdout_only:
        version = completed.stdout.strip()
    else:
        version = (completed.stdout or completed.stderr).strip()
    return {"available": True, "version": version}
