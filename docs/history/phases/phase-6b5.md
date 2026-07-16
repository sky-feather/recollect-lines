# Phase 6B.5 — Cursor Agent CLI runtime adapter

## Scope

Phase 6B.5 adds a fourth heterogeneous runtime adapter — `CursorAdapter`, which
supervises the real Cursor Agent CLI (`cursor-agent --print`) — without changing
any core broker lifecycle semantics. Phase 6C (provider fabric) and 6D
(capability discovery/routing) remain planned, not implemented; see
[PHASE-5.md](PHASE-5.md) and [RFC-001.md](../../design/RFC-001.md) §10.

Runtime identity is reported honestly throughout: `"Cursor Agent CLI via
cursor-agent --print"`, never a generic model API or another vendor's CLI — see
`CursorAdapter.RUNTIME_DESCRIPTION` and every `result.json`/launch-record
`adapter`/`runtime_description` field this phase adds.

## Compatibility spike (before cementing flags)

Before writing `CursorAdapter`, this phase ran bounded invocations of the
installed CLI (`cursor-agent` `2026.07.09-a3815c0`, confirmed via
`cursor-agent --version`) against disposable directories to validate the
noninteractive command contract:

1. **`--output-format json` prints exactly one JSON object to stdout on exit**
   (there is no `--json` flag). A real call emitted top-level fields `type`,
   `subtype`, `is_error`, `result`, `session_id`, `request_id`, `duration_ms`,
   `duration_api_ms`, and `usage`. `collect()` parses stdout as JSONL (tolerant
   of a single object), takes the last parsed dict, and uses `result` as the
   summary source.

2. **`--sandbox enabled` maps to broker `read_only`; `disabled` maps to
   `isolated_worktree`.** For `read_only`, `--mode plan` is also applied (CLI
   help: plan mode is read-only/planning). Cursor does not advertise a finer
   native read-only/workspace-write permission model than sandbox
   enabled/disabled plus plan mode — the adapter does not invent one.

3. **Headless invocation requires `--print --trust --force`.** The prompt is a
   trailing positional argument. `stdin=subprocess.DEVNULL` is used in
   subprocess launch (same discipline as `CodexAdapter`) so the CLI never blocks
   waiting for stdin in headless broker contexts.

4. **Workspace isolation uses `--workspace <effective_workspace>`** plus
   `cwd=effective_workspace` at launch. Unlike OpenCode's `--dir`, Cursor scopes
   via its own `--workspace` flag.

## `CursorAdapter`

`src/recollect_lines/cursor_adapter.py` conforms to the same
`RuntimeAdapter`/`AdapterCapabilities` boundary as the other subprocess
adapters and reuses `opencode_adapter.cancel_process_group`:

- **Launch**: `subprocess.Popen(command, stdin=DEVNULL, stdout=stdout.log,
  stderr=stderr.log, cwd=effective_workspace, start_new_session=True)` —
  process-group cancellation and durable pid/pgid launch records apply
  identically.
- **Availability**: `check_availability()` runs `cursor-agent --version` —
  local, offline, no auth/quota spent.
- **Sandbox mapping**: `SANDBOX_BY_EXECUTION_MODE = {"read_only": "enabled",
  "isolated_worktree": "disabled"}`; unmapped modes raise
  `CursorUnsupportedPolicy` before spawn.
- **Structured-output parsing**: `collect()` walks stdout for JSON result
  objects, normalizes `is_error` from `is_error`/`subtype`, classifies
  `error_category` from result text (no `api_error_status` field exists in the
  Cursor JSON shape), and preserves raw `stdout.log` byte-for-byte as forensic
  evidence.
- **Capabilities declared**: `requires_subprocess=True`,
  `supports_process_group_cancellation=True`,
  `reports_broker_verified_tests=False`.

## Integration without hard-coded Cursor branches

`Broker.subprocess_adapters` gained a `cursor` entry via a new `cursor_adapter=`
constructor parameter — the same generalized dispatch Phase 6A/6B introduced.
`models.DEFAULT_PROFILES`, `mcp_server.PROFILES`, `cli.py`/`mcp_server.py` arg
parsers, and `scripts/mcp_acceptance.py` gained `cursor` / `--cursor-command`
mirroring the existing adapter override pattern.

## Test evidence

`tests/test_cursor_adapter.py` against `tests/fixtures/fake_cursor.py` covers
command generation, JSON result parsing, malformed-line tolerance, auth/rate-
limit error categories, cancellation/timeout/reconciliation through the broker,
and idempotent collection.

`tests/test_mcp_server.py` gained profile-enum and end-to-end MCP selection
tests for `profile="cursor"`.

`scripts/mcp_acceptance.py` exercises a `cursor` read_only delegate/collect
through the deterministic fixture.

## Real bounded smoke (completed separately from fixture-only CI)

A minimal real invocation exercised the actual `CursorAdapter` and `Broker` end
to end in a disposable temp directory (`read_only`, `profile="cursor"`):

- **Runtime/version**: `cursor-agent` `2026.07.09-a3815c0` (`Cursor Agent CLI via
  cursor-agent --print`).
- **Command shape**: `cursor-agent --print --trust --force --output-format json
  --sandbox enabled --workspace <workspace> --mode plan --model composer-2.5
  "<prompt>"` with `stdin=DEVNULL`.
- **Observed**: `state=succeeded`, `summary` matched bounded prompt reply
  (`cursor-spike-ok`), `exit_code=0`, `subtype=success`, `is_error=false`,
  launch record `adapter=cursor`, `sandbox=enabled`.
- `--sandbox enabled` alone (without plan mode) also succeeded on the same
  bounded prompt during spike; plan mode is layered for read_only as defense in
  depth per CLI help.

No auth material is recorded here; this is narrow evidence (one CLI version,
one short read-only prompt), not continuous upstream compatibility testing.

## Unsupported / remaining constraints

- **Live mid-task `message` steering** remains unsupported for Cursor, as for
  every other adapter.
- **Durable re-attachment after a broker restart** remains unsupported;
  `reconcile()` applies to `cursor` tasks via the same `subprocess_adapters`
  dispatch.
- **Phase 6C and 6D remain planned, not implemented.**
