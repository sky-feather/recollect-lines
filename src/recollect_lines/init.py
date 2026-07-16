"""`recollect-lines init` -- one-shot local state/config bootstrap (Wave 3 / PR 6).

Establishes the operator home directory (`--home`, default `.recollect`) and
a minimal starter provider config, creating each only when absent, then runs
the same offline-safe diagnostic as `config validate` so the reported status
is truthful (no provider is ever claimed configured unless its file actually
validates). Provider credential capture (PR 7) and MCP host installation (PR 8) live in
`recollect-lines provider …` and `recollect-lines mcp …` respectively.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .doctor import format_human_report as _format_doctor_report, run_config_validate
from .providers import (
    OPERATOR_CONFIG_BASENAMES,
    ResolvedProviderConfigSource,
    resolve_providers_config_source,
    write_local_config_file,
)

INIT_SCHEMA_VERSION = "1"


class InitError(RuntimeError):
    """Raised when the home directory or config file cannot be created/read.

    Wraps filesystem errors (e.g. --home colliding with a plain file, or a
    permission-denied directory) with an actionable message instead of
    letting a raw OSError traceback reach the operator.
    """


NEXT_STEPS = (
    "Run `recollect-lines mcp install --host <cursor|claude_code|codex>` to register "
    "recollect-mcp with a supported parent host, or hand-edit the generated "
    "config.yaml with a real endpoint and re-run `recollect-lines config validate`."
)


def _existing_operator_config(home: Path) -> Path | None:
    for name in OPERATOR_CONFIG_BASENAMES:
        candidate = home / name
        if candidate.is_file():
            return candidate
    return None


def run_init(
    *,
    home: Path,
    force: bool,
    explicit_providers_config: Path | None,
    environ: dict[str, str],
    repo_root: Path,
    user_home: Path,
) -> tuple[dict[str, Any], int]:
    """Create the operator home directory and starter config if absent, then validate.

    Idempotent: a second call with the same arguments leaves an existing
    operator config file untouched (any of config.yaml/.yml/.json) unless
    `force` is set, in which case the existing file is deliberately
    overwritten with the safe starter content.
    """
    try:
        home_created = not home.exists()
        home.mkdir(parents=True, exist_ok=True)
        if home_created and os.name == "posix":
            os.chmod(home, 0o700)

        existing = _existing_operator_config(home)
        if existing is not None:
            config_path = existing
            if force:
                write_local_config_file(config_path, force=True)
                config_action = "overwritten"
            else:
                config_action = "preserved"
        else:
            config_path = home / "config.yaml"
            write_local_config_file(config_path, force=False)
            config_action = "created"
    except OSError as error:
        raise InitError(f"Could not initialize {home}: {error}") from error

    resolved: ResolvedProviderConfigSource = resolve_providers_config_source(
        explicit=explicit_providers_config,
        environ=environ,
        repo_root=repo_root,
        user_home=user_home,
    )
    diagnostics, exit_code = run_config_validate(
        providers_config=resolved.path,
        providers_config_origin=resolved.origin,
        environ=environ,
    )
    result = {
        "init_schema_version": INIT_SCHEMA_VERSION,
        "home": str(home),
        "home_created": home_created,
        "config_path": str(config_path),
        "config_action": config_action,
        "config_source": resolved.origin,
        "config_source_path": str(resolved.path) if resolved.path is not None else None,
        "diagnostics": diagnostics,
        "next_steps": NEXT_STEPS,
    }
    return result, exit_code


def format_human_report(result: dict[str, Any]) -> str:
    source_path = result["config_source_path"]
    header = [
        "recollect-lines init",
        f"home: {result['home']} ({'created' if result['home_created'] else 'already existed'})",
        f"config: {result['config_path']} ({result['config_action']})",
        "active config source: "
        + result["config_source"]
        + (f" -> {source_path}" if source_path else ""),
        "",
    ]
    body = _format_doctor_report(result["diagnostics"], command="init (config validate)")
    footer = ["", f"next steps: {result['next_steps']}"]
    return "\n".join(header) + body + "\n" + "\n".join(footer)
