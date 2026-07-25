"""Runtime adaptor implementations for supervised CLI and fixture subprocess backends.

Adding a new adaptor
--------------------
1. Declare capabilities/contracts in ``contracts.py`` (reuse ``AdapterCapabilities``).
2. Implement concrete command construction, parsing, policy exceptions, error
   classification, and collection/recovery in a dedicated module under this
   package.
3. Use shared process primitives from ``process`` (and optional scaffolding from
   ``cli_base``) only when signal semantics and lifecycle match — never force
   runtime-specific behavior through a generic base class.
4. Add import/registration/contract/lifecycle tests; register the runtime in
   ``runtime_registry`` and wire the broker/CLI/MCP entry points.
"""

from __future__ import annotations

from .claude_code import (
    DEFAULT_COMMAND_PREFIX as CLAUDE_DEFAULT_COMMAND_PREFIX,
    DEFAULT_GRACE_PERIOD_SECONDS as CLAUDE_DEFAULT_GRACE_PERIOD_SECONDS,
    RUNTIME_DESCRIPTION as CLAUDE_RUNTIME_DESCRIPTION,
    ClaudeCodeAdapter,
    ClaudeCodeUnsupportedPolicy,
    redact_secrets as claude_redact_secrets,
)
from .cli_base import SubprocessCliAdapterBase, probe_cli_version
from .codex import (
    DEFAULT_COMMAND_PREFIX as CODEX_DEFAULT_COMMAND_PREFIX,
    DEFAULT_GRACE_PERIOD_SECONDS as CODEX_DEFAULT_GRACE_PERIOD_SECONDS,
    RUNTIME_DESCRIPTION as CODEX_RUNTIME_DESCRIPTION,
    CodexAdapter,
    CodexUnsupportedPolicy,
    redact_secrets as codex_redact_secrets,
)
from .contracts import AdapterCapabilities, LaunchSpec, RuntimeAdapter
from .cursor import (
    DEFAULT_COMMAND_PREFIX as CURSOR_DEFAULT_COMMAND_PREFIX,
    DEFAULT_GRACE_PERIOD_SECONDS as CURSOR_DEFAULT_GRACE_PERIOD_SECONDS,
    RUNTIME_DESCRIPTION as CURSOR_RUNTIME_DESCRIPTION,
    CursorAdapter,
    CursorUnsupportedPolicy,
    redact_secrets as cursor_redact_secrets,
)
from .fixture_durable import FixtureDurableAdapter
from .opencode import (
    DEFAULT_COMMAND_PREFIX as OPENCODE_DEFAULT_COMMAND_PREFIX,
    DEFAULT_GRACE_PERIOD_SECONDS as OPENCODE_DEFAULT_GRACE_PERIOD_SECONDS,
    RUNTIME_DESCRIPTION as OPENCODE_RUNTIME_DESCRIPTION,
    OpenCodeAdapter,
)
from .process import (
    group_alive,
    group_dead_within,
    redact_command,
)

__all__ = [
    "AdapterCapabilities",
    "LaunchSpec",
    "RuntimeAdapter",
    "SubprocessCliAdapterBase",
    "probe_cli_version",

    "group_alive",
    "group_dead_within",
    "redact_command",
    "OpenCodeAdapter",
    "OPENCODE_DEFAULT_COMMAND_PREFIX",
    "OPENCODE_DEFAULT_GRACE_PERIOD_SECONDS",
    "OPENCODE_RUNTIME_DESCRIPTION",
    "ClaudeCodeAdapter",
    "ClaudeCodeUnsupportedPolicy",
    "CLAUDE_DEFAULT_COMMAND_PREFIX",
    "CLAUDE_DEFAULT_GRACE_PERIOD_SECONDS",
    "CLAUDE_RUNTIME_DESCRIPTION",
    "claude_redact_secrets",
    "CodexAdapter",
    "CodexUnsupportedPolicy",
    "CODEX_DEFAULT_COMMAND_PREFIX",
    "CODEX_DEFAULT_GRACE_PERIOD_SECONDS",
    "CODEX_RUNTIME_DESCRIPTION",
    "codex_redact_secrets",
    "CursorAdapter",
    "CursorUnsupportedPolicy",
    "CURSOR_DEFAULT_COMMAND_PREFIX",
    "CURSOR_DEFAULT_GRACE_PERIOD_SECONDS",
    "CURSOR_RUNTIME_DESCRIPTION",
    "cursor_redact_secrets",
    "FixtureDurableAdapter",
]
