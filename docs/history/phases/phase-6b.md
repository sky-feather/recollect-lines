# Phase 6B — Codex CLI runtime adapter

## Scope

Phase 6B adds a third heterogeneous runtime adapter — `CodexAdapter`, which
supervises the real `codex` CLI in non-interactive mode (`codex exec`) —
without changing any core broker lifecycle semantics. Phase 6B.5 (Cursor CLI) is
now implemented — see [phase-6b5.md](phase-6b5.md). Phase 6C (provider fabric),
and 6D (capability discovery/routing) remain planned, not implemented; see
[PHASE-5.md](PHASE-5.md) and [RFC-001.md](../../design/RFC-001.md) §10.

Runtime identity is reported honestly throughout: `"Codex via codex exec"`,
never a generic model API — see `CodexAdapter.RUNTIME_DESCRIPTION` and every
`result.json`/launch-record `adapter`/`runtime_description` field this phase
adds.

## Compatibility spike (before cementing flags)

Before writing `CodexAdapter`, this phase ran bounded invocations of the
installed CLI (`codex-cli` `0.144.4`, confirmed via `codex --version`) against
disposable directories to validate the noninteractive command contract, rather
than guessing from `--help` alone:

1. **`codex exec --json` streams NDJSON events to stdout** — not a single JSON
   object on exit. A real call emitted `thread.started`, `turn.started`,
   `item.completed`, and `turn.completed`. `collect()` parses stdout as
   JSONL, counts malformed lines, and takes the last `item.completed` with
   `item.type = "agent_message"` as the summary source.

2. **`--sandbox read-only` maps to broker `read_only`; `workspace-write` maps
   to `isolated_worktree`.** These are the narrowest sandbox modes confirmed
   for read-only inspection vs. in-worktree edits.

3. **`--skip-git-repo-check` is required outside a Git repository.** The
   adapter always passes it so disposable acceptance/smoke fixtures and
   non-git workspaces do not fail launch; git worktrees remain valid targets
   via `--cd`.

4. **`--output-schema` produces schema-shaped final agent messages** when used.
   The adapter does not pass `--output-schema` by default (generic broker
   tasks are not forced into one schema); `collect()` still accepts structured
   JSON in the final `agent_message.text` when the runtime returns it.

## `CodexAdapter`

`src/recollect_lines/codex_adapter.py` conforms to the same
`RuntimeAdapter`/`AdapterCapabilities` boundary as `OpenCodeAdapter` and
`ClaudeCodeAdapter` and reuses `opencode_adapter.cancel_process_group`:

- **Launch**: `subprocess.Popen(command, stdout=events.jsonl, stderr=stderr.log,
  start_new_session=True)` — process-group cancellation and durable pid/pgid
  launch records apply identically. Workspace isolation uses `--cd
  <effective_workspace>`; unlike OpenCode's `--dir`, Codex scopes via its own
  `--cd` flag.
- **Availability**: `check_availability()` runs `codex --version` — local,
  offline, no auth/quota spent.
- **Sandbox mapping**: `SANDBOX_BY_EXECUTION_MODE = {"read_only":
  "read-only", "isolated_worktree": "workspace-write"}`; unmapped modes raise
  `CodexUnsupportedPolicy` before spawn.
- **Structured-output parsing**: `collect()` walks the JSONL stream for
  `thread.started`/`turn.failed`/`turn.completed`/`item.completed`
  (`agent_message`), normalizes `error_category` from `turn.failed` messages,
  and preserves raw `events.jsonl` byte-for-byte as forensic evidence.
- **Capabilities declared**: `requires_subprocess=True`,
  `supports_process_group_cancellation=True`,
  `reports_broker_verified_tests=False`.

## Integration without hard-coded Codex branches

`Broker.subprocess_adapters` gained a `codex` entry via a new
`codex_adapter=` constructor parameter — the same generalized dispatch Phase 6A
introduced for `claude_code`. `models.DEFAULT_PROFILES`, `mcp_server.PROFILES`,
`cli.py`/`mcp_server.py` arg parsers, and `scripts/mcp_acceptance.py` gained
`codex` / `--codex-command` mirroring the existing adapter override pattern.

## Test evidence

`tests/test_codex_adapter.py` against `tests/fixtures/fake_codex.py` covers
command generation, NDJSON parsing (including structured agent_message JSON),
malformed-line tolerance, `turn.failed` error categories, cancellation/
timeout/reconciliation through the broker, and idempotent collection.

`tests/test_mcp_server.py` gained profile-enum and end-to-end MCP selection
tests for `profile="codex"`.

`scripts/mcp_acceptance.py` exercises a `codex` read_only delegate/collect
through the deterministic fixture.

## Real bounded smoke (completed separately from fixture-only CI)

A minimal real invocation exercised the actual `CodexAdapter` and `Broker` end to
end in a disposable non-git temp directory (`read_only`, `profile="codex"`):

- **Runtime/version**: `codex-cli` `0.144.4` (`Codex via codex exec`).
- **Command shape**: `codex exec --json --sandbox read-only --cd <workspace>
  --skip-git-repo-check --ephemeral "<prompt>"` with `stdin=DEVNULL` (required in
  headless subprocess contexts — otherwise the CLI blocks waiting for stdin).
- **Observed**: `state=succeeded`, `summary` matched fixture file content
  (`zebra-91`), `exit_code=0`, NDJSON events included `thread.started` and
  `turn.completed`, launch record `adapter=codex`, `sandbox=read-only`.
- A separate direct CLI probe with `--output-schema` returned structured
  `{"status":"codex-smoke-ok"}` in the final `agent_message.text` (not the
  default `build_command()` path, but compatible with `collect()` parsing).

No auth material is recorded here; this is narrow evidence (one CLI version,
one short read-only prompt), not continuous upstream compatibility testing.

## Unsupported / remaining constraints

- **Live mid-task `message` steering** remains unsupported for Codex, as for
  every other adapter.
- **Durable re-attachment after a broker restart** remains unsupported; `reconcile()`
  applies to `codex` tasks via the same `subprocess_adapters` dispatch.
- **Phase 6C and 6D remain planned, not implemented.** Phase 6B.5 (Cursor CLI)
  is implemented — see [phase-6b5.md](phase-6b5.md).
