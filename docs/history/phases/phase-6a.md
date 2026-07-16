# Phase 6A — Claude Code CLI runtime adapter

## Scope

Phase 6A closes one instance of the largest remaining MVP gap named in
[PRD.md](../../design/PRD.md) §9 and [RFC-001.md](../../design/RFC-001.md) §8/§10: Recollect Lines
implemented exactly one runtime adapter (OpenCode, experimental) before this
phase. This phase adds a second, heterogeneous runtime adapter —
`ClaudeCodeAdapter`, which supervises the real `claude` CLI in non-interactive
mode (`claude -p`) — without changing any core broker lifecycle semantics to
accommodate it. Phase 6B (Codex CLI), 6B.5 (Cursor CLI), 6C (provider fabric),
and 6D (capability discovery/routing) remain planned, not implemented; see
[PHASE-5.md](PHASE-5.md) and [RFC-001.md](../../design/RFC-001.md) §10.

Runtime identity is reported honestly throughout: `"Claude Code via
claude -p"`, never "Cursor" or a generic model API — see
`ClaudeCodeAdapter.RUNTIME_DESCRIPTION` and every `result.json`/launch-record
`adapter`/`runtime_description` field this phase adds.

## Compatibility spike (before cementing flags)

Before writing `ClaudeCodeAdapter`, this phase ran a small number of real,
bounded invocations of the installed CLI (`claude` `2.1.201`, confirmed via
`claude --version`) against a disposable local Git fixture
(`/tmp/rl6a-spike`, never the project checkout) to validate the noninteractive
command contract the adapter is built on, rather than guessing from `--help`
alone. Raw output was preserved for each call; findings:

1. **`--output-format json` prints exactly one JSON object to stdout on
   exit** — not an incremental event stream. A real call
   (`claude -p "Read fact.txt and state only the number it contains, nothing
   else." --output-format json --permission-mode plan --disallowedTools
   Edit,Write,NotebookEdit --model haiku --no-session-persistence`) returned:

   ```json
   {"type":"result","subtype":"success","is_error":false,"api_error_status":null,
    "result":"42","session_id":"5c2ef85d-...","permission_denials":[], ...}
   ```

   `--output-format stream-json` (incremental events) was available per
   `--help` but was **not** adopted: the adapter has no need for partial
   output, and adopting an unverified event-stream shape would have meant
   guessing its parse contract instead of testing it. `collect()` therefore
   parses stdout as (at most) one JSON object per line and tolerates any
   number of malformed/extra lines — see "Structured-output parsing" below.

   Incidental finding, recorded honestly rather than silently worked around:
   passing `--model haiku` did **not** error, but the response's own
   `modelUsage` showed `claude-sonnet-5` was actually used — the alias
   silently fell back rather than being rejected. `ClaudeCodeAdapter` never
   passes `--model` unless the caller explicitly configures one (see
   `PERMISSION_MODE_BY_EXECUTION_MODE`/`self.model` below); this finding is
   documented, not further investigated, since dedicated model-alias
   compatibility testing is outside this phase's read-only-adapter-contract
   scope.

2. **`--permission-mode plan` structurally refuses file writes.** A real
   call asking the CLI to create `spike_probe.txt` under `--permission-mode
   plan --disallowedTools Edit,Write,NotebookEdit` returned `is_error:
   false` with `result` explaining plan mode was active and no file was
   created — confirmed independently via `ls`/`git status`, not just the
   CLI's own claim. A parallel call with `--permission-mode acceptEdits`
   (no `--disallowedTools`) did create the file with the requested content.
   This is why `read_only` maps to `plan` (plus `--disallowedTools` as
   defense in depth) and `isolated_worktree` maps to `acceptEdits` — the
   narrowest mode this spike actually confirmed writes files — rather than
   `bypassPermissions`/`dontAsk`/`auto`/`manual`, none of which had
   equivalent non-interactive safety evidence behind them.

3. **Commander (the CLI's arg parser) treats `--disallowedTools`/
   `--allowedTools` as variadic** (`--help` shows `<tools...>`): any bare,
   non-flag token following one of these flags on the argv is swallowed as
   an additional tool name, including a prompt positional argument. This was
   confirmed from the CLI's own `--help` grammar, not by deliberately
   triggering the bug against the live API (which would have added cost
   without adding evidence beyond what the grammar itself already proves).
   `ClaudeCodeAdapter.build_command()` always places the prompt immediately
   after `-p`, before any other flag, and puts `--disallowedTools` last — so
   nothing positional ever trails a variadic flag. See
   `test_disallowed_tools_flag_is_always_last_so_nothing_positional_trails_a_variadic_flag`
   in `tests/test_claude_code_adapter.py`.

## `ClaudeCodeAdapter`

`src/recollect_lines/claude_code_adapter.py` conforms to the same
`RuntimeAdapter`/`AdapterCapabilities` boundary `OpenCodeAdapter` does
([RFC-001.md](../../design/RFC-001.md) §1) and reuses its process-group cancellation
implementation directly (`opencode_adapter.cancel_process_group`) rather than
reinventing SIGTERM→SIGKILL escalation per adapter:

- **Launch**: `subprocess.Popen(command, cwd=effective_workspace,
  start_new_session=True)` — the same process-group-leader pattern
  `OpenCodeAdapter` uses, so `os.killpg` cancellation and durable
  pid/pgid launch records apply identically. Unlike OpenCode, Claude Code
  has no `--dir`/`--workspace` flag; isolation is entirely `cwd`-driven.
- **Availability**: `check_availability()` runs `claude --version` — local,
  offline, no auth/quota spent — and normalizes `cli_not_found` /
  `version_check_timed_out` / `version_check_failed`, with any stderr/stdout
  detail passed through `redact_secrets()` before being returned.
- **Command/permission mapping**: `build_command()` maps only the two
  `execution_mode`s the broker currently defines
  (`PERMISSION_MODE_BY_EXECUTION_MODE = {"read_only": "plan",
  "isolated_worktree": "acceptEdits"}`) and raises
  `ClaudeCodeUnsupportedPolicy` (fail-closed, before any subprocess is
  spawned) for anything else — a future `execution_mode` never silently
  inherits write access. `read_only` passes `--tools Read,Grep,Glob`
  (a structural allowlist — the tool *set* the CLI gives the model, not a
  deny-list) as the actual guarantee, plus `--disallowedTools
  Edit,Write,NotebookEdit` layered on top as defense in depth. See
  "Reconciliation addendum" below for why the allowlist is required and not
  merely additional defense in depth.
- **Structured-output parsing**: `collect()` splits stdout into lines,
  parses each independently as JSON, counts (but does not fail on) any
  line that doesn't parse, and takes the last successfully-parsed JSON
  object as the result — tolerant of a truncated/malformed final line (a
  killed process) without losing an otherwise-valid single-object result.
  Raw stdout/stderr are preserved byte-for-byte as artifacts regardless of
  parse outcome.
- **Result normalization**: `is_error`/`api_error_status` from the parsed
  result map to `error_category` (`authentication_error` for 401/403,
  `rate_limit_or_quota_error` for 429, `runtime_error` otherwise;
  `unparseable_output` when the process exited non-zero with nothing
  parseable at all) — normalized without leaking credentials, since
  `redact_secrets()` scrubs the concise `summary`/`stderr_tail` fields
  folded into `result.json` (never the raw artifact files, which stay
  forensic evidence). Classification also covers the case where a
  well-formed `is_error: false` result was already flushed but the process
  was then killed before exiting `0` (an external timeout/OOM after the
  result line) — `error_category` is derived whenever the *task* failed
  (`is_error` or a non-zero process exit), not only when the parsed JSON's
  own `is_error` flag said so; see
  `test_a_clean_is_error_false_result_followed_by_a_nonzero_exit_is_still_categorized`.
  The 401/403/429 classification logic itself is documented as **inferred
  from the API's own status-code semantics, not spike-observed** —
  deliberately triggering a real auth/quota failure was out of scope for a
  bounded, cost-conscious spike.
- **Capabilities declared**: `requires_subprocess=True`,
  `supports_process_group_cancellation=True`,
  `reports_broker_verified_tests=False` — the same three-field
  `AdapterCapabilities` shape `OpenCodeAdapter` declares. Live `message`
  steering is not claimed anywhere: `mcp_server.handle_message` already
  returns a structured `unsupported` response for every profile, and this
  phase extended its wording to name Claude Code explicitly rather than
  adding an adapter-specific steering claim.

## Integration without hard-coded Claude branches

`Broker` (`service.py`) previously special-cased exactly one subprocess
adapter (`if record.profile == "opencode": ... self.opencode_adapter...`)
throughout `start()`/`collect()`/`cancel()`/`timeout()`/`reconcile()`/
`reconcile_pending()`. This phase generalizes that dispatch to a
`self.subprocess_adapters: dict[str, adapter]` keyed by profile name
(`{"opencode": ..., "claude_code": ...}`), built once in `__init__` from the
existing `opencode_adapter=`/new `claude_code_adapter=` constructor
parameters. Every one of those methods now looks the adapter up by
`record.profile` instead of naming `"opencode"` directly, so:

- Adding `claude_code` required **no new adapter-specific branch** anywhere
  in `service.py` — the exact same code path that already handled OpenCode
  (start → durable launch record → collect/cancel/timeout → reconcile) now
  handles any profile present in `subprocess_adapters`.
- `self.opencode_adapter` remains a directly accessible attribute (existing
  tests reference it directly), unchanged in behavior.
- `models.DEFAULT_PROFILES` gained a `claude_code` entry with the same
  `{read_only, isolated_worktree}` mode set, 3600s max timeout, and
  concurrency 2 as `opencode` — no new policy shape was invented.
- `mcp_server.PROFILES`/`DELEGATE_INPUT_SCHEMA`'s profile enum and `cli.py`/
  `mcp_server.py`'s arg parsers gained `claude_code` and a `--claude-command`
  override (mirroring the existing `--opencode-command`, for pointing at a
  deterministic stand-in binary in tests/acceptance) — the minimal
  "runtime discovery/selection surface" update the task required, not a new
  capabilities-discovery endpoint.

Isolated-worktree write safety is preserved by construction, not by any
Claude-specific code: `Broker.start()` still resolves the canonical source,
acquires the durable lease, and creates the worktree *before* calling
`adapter.start()`, for any profile in `subprocess_adapters` — this phase
added no new branch to that sequencing.

## Permission safety

- A `read_only` task is never labeled read-only while actually able to edit
  files: `plan` mode was independently confirmed (not just documented) to
  refuse file creation (see spike finding 2), and `--disallowedTools
  Edit,Write,NotebookEdit` is layered on top as defense in depth.
- A `read_only` task's tool set is structurally narrowed to `Read,Grep,Glob`
  via `--tools` — Bash (and every other built-in tool) does not exist for
  the model to call at all in this mode, confirmed against the real CLI: it
  reports having no Bash tool available, rather than merely declining to use
  one. See "Reconciliation addendum" below for the gap this closed.
- Execution modes with no validated Claude Code permission-mode mapping fail
  closed (`ClaudeCodeUnsupportedPolicy`, raised inside `build_command()`
  before any subprocess exists) rather than defaulting to a broader mode.
- `isolated_worktree` maps to `acceptEdits`, the narrowest mode this spike
  confirmed actually writes files — not `bypassPermissions`, which was
  available per `--help` but has no non-interactive safety evidence behind
  it in this phase's testing.

## Test evidence

`tests/test_claude_code_adapter.py` (30 tests) against a deterministic
fixture CLI (`tests/fixtures/fake_claude.py`, selected by prompt keyword,
mirroring `fake_opencode.py`'s existing pattern) covers:

- Command/argument generation: prompt placement immediately after `-p`;
  `read_only` → `plan` + `--tools Read,Grep,Glob` (excluding Bash) +
  `--disallowedTools`; `isolated_worktree` → `acceptEdits` with no
  `--tools`/`--disallowedTools`; both variadic flags always argv-final with
  `--tools` before `--disallowedTools`; fail-closed
  `ClaudeCodeUnsupportedPolicy` for an unmapped mode; optional `--model`;
  `--output-format json`/`--no-session-persistence` always present.
- Redaction: `redact_secrets()` scrubs an Anthropic-shaped API key and a
  bearer token, leaves ordinary text untouched.
- Structured-output/raw-output collection: a single JSON result object
  parsed as the summary; a malformed leading line tolerated and counted
  without losing the valid result; a process that produces no parseable
  output reaches `succeeded_with_warnings`, never a fabricated `succeeded`.
- Availability/auth/launch error normalization: missing binary
  (`cli_not_found`), version-check timeout, `is_error`+`api_error_status`
  401 → `authentication_error`, 429 → `rate_limit_or_quota_error` — distinct
  from a generic non-zero exit with unparseable output
  (`unparseable_output`); and a well-formed `is_error: false` result
  followed by a non-zero process exit still reaches a non-null
  `error_category`, not a silently-uncategorized failure.
- Permission-mode mapping/fail-closed: covered above and in
  `ClaudeCodeUnsupportedPolicyBrokerTests`.
- Process metadata/cancellation integration through the existing broker
  contract: `task.running` event carries `pid`/`pgid`/`runtime_description`;
  the durable launch record's `adapter` is `"claude_code"`; cancellation
  reaches confirmed `group_terminated: true` via `SIGTERM`, and escalates to
  `SIGKILL` when the fixture ignores `SIGTERM` — reusing
  `cancel_process_group`, not a reimplementation.
- Result normalization/idempotent collection: `runtime.adapter ==
  "claude_code"`; a repeated `collect()` on an already-terminal task returns
  the identical record (same `updated_at`, no re-collection); a lost
  in-memory handle with a confirmed-dead process group reaches `failed` with
  `reason: process_group_confirmed_dead` — the same restart-safety
  contract Phase 5B built for OpenCode, now exercised for Claude Code.

`tests/test_mcp_server.py` gained two tests: the `delegate` tool's `profile`
enum lists `mock`/`opencode`/`claude_code` (the discovery-surface contract),
and an end-to-end `ClaudeCodeMcpSelectionTests` case drives a real
`recollect-mcp` subprocess with `--claude-command` pointed at the
deterministic fixture, delegating a `profile="claude_code"` task and
confirming `collect` returns `runtime_result.runtime.adapter ==
"claude_code"` — proof the MCP surface actually dispatches to
`ClaudeCodeAdapter`, not silently to mock/opencode.

Full suite: **128 tests pass** (96 pre-existing Phase 1–5C tests, unchanged
and still green, plus 32 new: 30 in `test_claude_code_adapter.py` and 2 in
`test_mcp_server.py`). `python3 -m compileall -q src tests scripts` passes.
`scripts/mcp_acceptance.py` now also exercises a `claude_code` read_only
task (delegate/collect through the deterministic fixture) alongside its
original OpenCode coverage — see "Reconciliation addendum" below — and
still passes in full (19/19 checks), confirming the `service.py`
generalization introduced no OpenCode regression either.

## Real bounded smoke (completed, distinct from the fixture-only evidence above)

A separate, minimal real invocation exercised the actual `ClaudeCodeAdapter`
and `Broker` — not the deterministic fixture — end to end, against a second
disposable local Git fixture (`/tmp/rl6a-smoke`, never the project
checkout), immediately after the compatibility spike above (four total real
`claude` invocations across this phase: three spike calls, one smoke call).

Setup: `/tmp/rl6a-smoke/fact.txt` contained `"the secret ingredient is
basil"`, committed at a known `HEAD`.

Task: a narrow read-only task — *"Read fact.txt in this repository and state
only the secret ingredient it names, nothing else. Do not create, edit, or
delete any files."* — delegated with `execution_mode="read_only",
profile="claude_code"`, `timeout_seconds=90`, through the real (default,
un-overridden) `ClaudeCodeAdapter` and `Broker.start()`/`collect()`.

Observed:

- **Runtime/version**: `claude` `2.1.201` (`Claude Code via claude -p`).
- **Command shape (secrets redacted — there were none to redact)**:
  ```
  claude -p "Read fact.txt in this repository and state only the secret
  ingredient it names, nothing else. Do not create, edit, or delete any
  files." --output-format json --permission-mode plan
  --no-session-persistence --disallowedTools Edit,Write,NotebookEdit
  ```
- **Exit behavior**: `process_exit_code=0`, `is_error=false`,
  `parsed_result_count=1`, `malformed_output_lines=0`.
- **Result**: `summary="Basil."` — correctly read from the fixture, task
  reached `succeeded`.
- **Artifact facts**: `stdout.log` held exactly the one raw JSON result
  object (`session_id`, `num_turns=2`, `permission_denials=[]`,
  `total_cost_usd` present in the raw artifact); `stderr.log` was empty;
  the durable launch record's `adapter="claude_code"`,
  `adapter_label="claude"`, real `pid`/`pgid` recorded.
- **Independent zero-change filesystem verification** (checked by the smoke
  driver itself, not derived from anything the adapter/CLI reported):
  `git rev-parse HEAD` identical before/after
  (`a4373966c5c29de24cc688a5cb4ba41f9e511f3c`); `git status --porcelain`
  empty both before and after; a SHA-256 over every tracked file's bytes
  (excluding `.git/`) identical before/after
  (`1c42ac6c6c505e7b5ef1316d2e4f65d6c1a3a39457b343bda97903eca76d315a`).

This is real evidence, and it is narrow: one CLI version, one short
read-only prompt, one disposable fixture, not continuous compatibility
testing against future `claude` CLI releases — the same honesty standard
[RFC-001.md](../../design/RFC-001.md) §4 already applies to the OpenCode smoke evidence.
No broad implementation work was run against the live API; this phase's
subscription-quota usage was limited to the three spike calls above and
this one smoke call.

## Reconciliation addendum (2026-07-14)

Two independent implementations of this phase existed briefly: this one
(published first, as PR #8) and a separately-developed local commit built on
the same Phase 5C base, never pushed or reported. Both were compared
directly — requirements coverage, tests, and (where the docs disagreed) the
underlying factual claims re-verified against the real, installed `claude`
2.1.201 CLI, not just read off either document.

Both implementations were sound overall, and most differences between them
(`--output-format json` vs `stream-json`, `claude_code`/`claude_adapter.py`
naming, doc structure) were equally-valid style choices — re-verified
against the real CLI, both output-format claims held up empirically. Neither
side of those was "wrong," so this phase kept its own (already published,
already green) choices rather than merging mechanically.

One finding was not a style difference: the local commit's `read_only`
mapping used `--tools Read,Grep,Glob` (a structural tool-set allowlist),
while this phase's original `read_only` mapping used only
`--disallowedTools Edit,Write,NotebookEdit`. Re-verifying both directly
against the real CLI:

- Under `--permission-mode plan --disallowedTools Edit,Write,NotebookEdit`
  alone (this phase's original mapping), Bash remained available to the
  model, and a `whoami` call through it **actually executed** and returned
  real output — `plan` mode's own file-write refusal does not extend to
  arbitrary non-write Bash execution, and `--disallowedTools` never named
  Bash. A second, more sensitive-sounding request (env-var dump piped into
  `curl`) was declined, but by the model's own judgment, not by any
  structural mechanism — the same request could plausibly succeed on a
  different phrasing or a different model turn.
- Under `--permission-mode plan --tools Read,Grep,Glob` (the local commit's
  mapping), the same `whoami` request failed structurally: the model
  reported it has no Bash tool available at all, not merely that it
  declined to use one.

`read_only` is this phase's explicitly critical-scope guarantee, and a
guarantee that depends on the model's own judgment call about a given
request is not the structural guarantee the product constraints require
("do not overclaim... permission safety"). This was therefore carried over
as a necessary fix, not a style preference: `build_command()` now emits
`--tools Read,Grep,Glob` for `read_only` (see `READ_ONLY_TOOLS` in
`claude_code_adapter.py`), on top of the original `--disallowedTools`
defense-in-depth, which stays. Nothing else from the local commit was
merged. The local commit's own branch is preserved at
`backup/local-phase-6a-7d2ebc0` for reference; it was not merged into this
history.

This fix was re-verified three ways: a new unit test asserting `--tools`
excludes Bash for `read_only`
(`test_build_command_read_only_restricts_tools_to_a_structural_allowlist_excluding_bash`);
`scripts/mcp_acceptance.py` extended to exercise a `claude_code` `read_only`
task end-to-end through the fixture CLI (closing the coverage gap that let
this ship in the first place — the original acceptance harness never
touched the `claude_code` profile at all); and one additional real,
bounded smoke call — a fresh `read_only` task through the real broker + real
MCP stdio surface + real `claude` CLI, disposable fixture
(`/tmp/rl6a-smoke-recon`, never the project checkout). Observed:

- Command actually launched: `["claude", "-p", "<prompt>",
  "--output-format", "json", "--permission-mode", "plan",
  "--no-session-persistence", "--tools", "Read,Grep,Glob",
  "--disallowedTools", "Edit,Write,NotebookEdit"]` (read from the broker's
  own durable launch record, not merely `build_command()`'s return value).
- Result: `summary="zebra-91"`, the exact fact value; `exit_code=0`,
  `is_error=false`.
- Fixture repository `git rev-parse HEAD` identical before/after
  (`1746ecdafd328ec3850d390ac6e2a14966edb834`); `git status --porcelain`
  empty before and after.

This addendum's own Bash-availability probes (the `whoami` calls above) were
run directly against the bare `claude` CLI from a shell, outside the broker,
specifically to compare the two candidate flag mappings — they are not part
of the adapter's own smoke evidence and are not repeated by CI.

## Unsupported / remaining constraints

- **Live mid-task `message` steering remains unsupported** for Claude Code,
  exactly as for OpenCode and mock — `mcp_server.handle_message` always
  returns a structured `unsupported` response. This phase did not add any
  steering channel; `AdapterCapabilities` has no field for it because
  nothing in the broker's lifecycle graph queues, buffers, or forwards
  message content to any adapter.
- **Durable re-attachment after a broker restart** is unsupported for
  Claude Code for the same reason it's unsupported for OpenCode (Phase 2/5B
  known limitation): a new OS process cannot regain a `Popen`/child
  relationship with an orphaned subprocess. `reconcile()`/
  `reconcile_pending()` apply to `claude_code` tasks identically to
  `opencode` tasks via the same generalized `subprocess_adapters` dispatch.
- **Model-alias fallback behavior** (`--model haiku` silently using
  `claude-sonnet-5`) is documented, not fixed or worked around — it's a CLI
  behavior outside this adapter's control, and the adapter never passes
  `--model` unless a caller explicitly configures one.
- **401/403/429 → `error_category` classification is inferred, not
  spike-observed** (see above) — a real auth/quota failure was not
  deliberately triggered.
- **Phase 6B (Codex CLI) and 6B.5 (Cursor CLI) are now implemented** — see
  [phase-6b.md](phase-6b.md) and [phase-6b5.md](phase-6b5.md). Phase 6C
  (provider fabric) and 6D (capability discovery/routing) remain planned, not
  implemented — see [PHASE-5.md](PHASE-5.md) and [RFC-001.md](../../design/RFC-001.md) §10.
- **Windows is still unsupported** — POSIX process groups only, unchanged
  from every prior phase.
