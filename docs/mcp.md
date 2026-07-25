# MCP reference

Program: `recollect-mcp` (stdio JSON-RPC MCP server).

## Launch

```bash
recollect-mcp --home /path/to/.recollect
```

Adapter override flags match `recollect-lines` (`--codex-command`, etc.).

## Protocol

- Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout
- Supported protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`
- Server: `recollect-lines-mcp` v0.1.0
- Diagnostics: stderr only

## Tools (exact names)

| Tool | Purpose |
|------|---------|
| `delegate` | Create + start one task (returns `completion_cursor`) |
| `delegate_batch` | Create + start many tasks independently (returns `completion_cursor`) |
| `status` | Task state, events, artifacts |
| `collect` | Runtime result + broker verification |
| `cancel` | Cancellation with evidence |
| `control` | Operator recovery (`action`: `status`, `cancel`, `collect`, `message`) |
| `message` | Always returns explicit unsupported (no steering) |
| `reconcile` | Post-restart subprocess reconciliation |
| `discover_capabilities` | Runtime/provider inventory |
| `select_candidates` | Policy-aware filtering (parent chooses) |
| `council_validate` | Validate council plan |
| `council_execute` | Execute bounded council plan |
| `task_children` | Direct child task summaries for a parent |
| `task_tree` | Bounded tree for a `root_task_id`, or an audit lookup by `external_root_id` |
| `completion_events` | Poll durable completion signals from the global event cursor (see below) |

## `delegate` input (schema summary)

Required:

- `task` (string)
- `workspace` (string)

Optional:

| Field | Default | Values |
|-------|---------|--------|
| `execution_mode` | `read_only` | `read_only`, `isolated_worktree` |
| `runtime` | `mock` | `mock`, `opencode`, `claude_code`, `codex`, `cursor`, `openai_compatible` |
| `profile` | — | **Deprecated.** Legacy alias for `runtime`; accepted only for known runtime identifiers |
| `model` | — | Optional requested model identifier (persisted only in this release) |
| `agent_profile` | — | Optional behavioral role identifier (persisted only in this release) |
| `result_schema` | — | Optional normalized result schema (`plain-summary`, `evidence-report`, `review-findings`, `implementation-report`, `verified-investigation-report`, `review-report`); unknown values rejected at delegate. Structured schemas append a versioned prompt-level output contract at launch (not provider-native structured output). |
| `provider` | — | Required when `runtime` is `openai_compatible` |
| `timeout_seconds` | `1800` | positive integer |
| `verification_policy` | `none` | `none`, `advisory`, `required` |
| `verify_commands` | — | array of argv arrays |
| `parent_task_id` | — | optional existing broker parent |
| `external_root_id` | — | audit-only host/conversation grouping |
| `relationship` | — | `delegates`, `continues` (requires parent; `continues` is a new task, not resume) |
| `origin_kind` | `host` | `host` (external host via CLI/MCP, including parented tasks), `side_agent` (reserved for future explicit recursive callback path; audit only, not authorization) |
| `origin_ref` | — | audit-only caller reference |

`root_task_id` and `delegation_depth` are broker-derived and rejected if callers supply them.

`delegate` returns `task_id`, `state`, `workspace`, `runtime`, `profile` (bridge), `completion_cursor` (see [Completion-events polling contract](#completion-events-polling-contract) below), optional side-agent and lineage fields, `compatibility` when a legacy `profile` was translated, and `schema_conflict_warning` when the task prose looks incompatible with a requested structured `result_schema` — not a fabricated completion.

See [migration-runtime-profile.md](migration-runtime-profile.md) for translation rules.

## `task_tree`: `root_task_id` vs `external_root_id`

`task_tree` accepts exactly one of two mutually exclusive filters and returns the same shape (`truncated`, `tasks`) either way:

| Filter | Identity | Matches |
|--------|----------|---------|
| `root_task_id` | A broker task id that is itself a tree root (`root_task_id == task_id`) | Every task whose broker-derived `root_task_id` equals it — the full parent-directed delegation tree. Unknown id is an error. |
| `external_root_id` | A caller-supplied audit tag, not a task id | Every task that was explicitly created with that `external_root_id`, regardless of which broker tree(s) they belong to. Unlike `root_task_id`, this tag is **not inherited** by children automatically — each task only matches if its own `create`/`delegate` call supplied it. An unmatched key returns an empty `tasks` list, not an error. |

Use `root_task_id` to walk one delegation tree by its broker identity. Use `external_root_id` for audit lookups keyed on a caller-chosen grouping label (e.g. a host conversation or debate id) attached to some or all of the tasks the host created for that grouping — this is the query path the dogfooded `helloworld` debate was missing: it tagged every task with the same `external_root_id`, but there was no way to look them back up except by individually-known `tsk_…` ids. This is audit trail visibility only; it grants no additional authorization and adds no workflow automation.

## Result outcome dimensions: execution, parsing, contract

`status` and `collect` expose a task's outcome along three deliberately distinct, backward-compatible dimensions — none of them is ever inferred from another:

| Dimension | Field | Meaning |
|-----------|-------|---------|
| Execution | `state` | Did the child process/runtime actually run and exit successfully? Purely the runtime's exit code and process lifecycle; never downgraded because parsing or contract satisfaction failed. |
| Parsing | `normalized_result`/`normalized_summary.parse_status` | Could the broker extract a summary and, if structured JSON was expected, parse it? One of `ok`, `partial`, `fallback`, `failed`. |
| Contract | `normalized_result`/`normalized_summary.contract_status` | Did the *requested* `result_schema` contract actually get satisfied? One of `not_requested` (effective schema is `plain-summary`), `satisfied`, `unsatisfied_fallback` (structured schema requested, runtime returned plain prose — no JSON payload at all), `unsatisfied_malformed` (JSON/summary present but malformed or missing required fields), `unavailable` (the child did not reach a successful terminal state, so there is nothing to evaluate). |

This is what makes the Wave 0 dogfood incident un-repeatable: a `claude -p` run can exit 0 with a clean `is_error: false` result whose text is a meta-response asking which output format to use, rather than the requested JSON. `collect`/`status` then report `state: succeeded` (the process really did succeed) *and* `contract_status: unsatisfied_fallback` (the requested contract was not honored) as separate, equally authoritative fields — a caller must check `contract_status`, not just `state`, before trusting structured fields like `findings`.

## Durable lifecycle guidance

Every `status` and `collect` response now includes an `operator_control` object.
It is a read-only, fail-closed guide; it does **not** perform recovery or cleanup.
Use its `recovery_posture`, `permitted_actions`, and per-action refusal reasons
instead of inferring liveness from a PID, an old event, or a raw launch record.

| Task state / posture | Meaning | Safe next action |
|---|---|---|
| `running`, `collecting`, or `cancelling` / `observed` | The broker has an ordinary live lifecycle observation. | Poll `completion_events` or `status`; use only listed actions. `collect` is nonblocking and may return no terminal result yet. |
| `recovery_required` / `recovery_required` | Liveness or durable adoption is uncertain after a restart or contested evidence. This is **not** success and is not proof of death. | Read `operator_control`; do not release a workspace or signal an unowned process. Use `control(action="status")`/`reconcile` only when listed as permitted. |
| `uncollected` / `terminal` | The runtime exited but a legacy result was not recoverable. The outcome is unknown, not successful. | Inspect retained artifacts and verify the claimed side effect independently. Cleanup remains explicit. |
| terminal / `terminal` | A terminal result was recorded. | Call `collect` for the normalized runtime result and broker verification. |

`operator_control.distinction` describes the stable meanings of lifecycle state,
recovery posture, and permitted actions. It lets a caller distinguish **still
running**, **exited but not parsed**, and **unknown/recovery-required** without
reading internal launch fields.

## Durable rollout report

For a read-only rollout snapshot, run:

```bash
recollect-lines --home /path/to/.recollect durable-rollout-report --json
```

The report only summarizes persisted `durable_subprocess` launches. It reports
the count and min/max/average time from the persisted `task.running` event to
the first persisted terminal event, terminal outcomes by adapter, and durable
recovery/adoption outcomes. This is **event-observation latency**, not a claim
about provider execution time. It never starts, reconciles, cancels, or cleans
up a task, and it does not select a legacy path.

## Schema/prose conflict warning

`delegate`/`delegate_batch` run a deterministic, advisory check at create time: if the task text reads as an open-ended, unstructured request (matching a small fixed vocabulary — e.g. "debate", "essay", "story") while a structured `result_schema` (`evidence-report`, `review-findings`, `implementation-report`, `verified-investigation-report`, `review-report`) was requested, the response and later `status` calls include a `schema_conflict_warning` object:

```json
{
  "code": "prose_genre_vs_structured_schema",
  "requested_schema": "review-findings",
  "matched_signal": "debate",
  "message": "Task prose matches an open-ended prose signal ('debate') while result_schema='review-findings' requires a structured JSON contract; the runtime may return plain prose that cannot satisfy it."
}
```

This never blocks or rejects task creation, and ambiguous or unmatched task text is never flagged — it exists so a parent can decide to retry with a different `result_schema` *before* spending a runtime call, not to gate delegation. Only the matched keyword name is ever recorded; the task text itself is never inspected beyond that static match or stored in the warning.

## Completion-events polling contract

The dogfood problem this closes: a parent orchestrating several delegate
rounds used to sleep a guessed duration between dispatch and the next round
because there was no reliable way to know a batch had actually finished
without blocking on each task's `collect` one at a time. `completion_events`
is the primitive that replaces the guess with a real, cheap poll — and
`delegate`/`delegate_batch` now return a `completion_cursor` (the global
event high-water mark at dispatch time) so the parent never has to make a
separate round-trip to establish a baseline first:

```
delegate_batch(tasks) -> completion_cursor
loop:
  page = completion_events(after_event_id=completion_cursor, task_id=... )
  completion_cursor = page.next_cursor
  record any newly-seen task ids from page.events
  stop when every dispatched task id has been seen (or a bounded retry budget is spent)
collect(task_id) for each -> advance to the next round
```

No `sleep(guessed_seconds)` appears anywhere in that loop; the caller decides
its own retry cadence (a short poll interval, exponential backoff, whatever
fits), and `completion_events` never blocks behind a task that is still
running.

Contract details:

- **Cursor is exclusive**: `after_event_id` is a strict lower bound — only
  events with `event_id > after_event_id` are returned. `after_event_id=0`
  (the default) returns from the beginning.
- **Ordering**: events are returned in strictly increasing global `event_id`
  order — one monotonic sequence shared by every task this broker instance
  has ever recorded a terminal/`recovery_required` transition for, not a
  per-task sequence.
- **Page limits**: `limit` defaults to 64 and is clamped to at most 256;
  `has_more: true` means call again with `next_cursor` to continue. `next_cursor`
  never regresses and equals `after_event_id` unchanged when the page is empty.
- **Filtering**: optional `task_id` or `root_task_id` narrow the same cursor
  sequence to one task or one lineage; `completion_only` (default `true`)
  restricts to terminal states plus `recovery_required` (which is actionable,
  not strictly terminal, but hosts must still observe it); an explicit `states`
  array overrides `completion_only` with an exact state set.
- **Idempotency**: polling the same `(after_event_id, filters)` twice without
  any new completions in between returns byte-identical pages. A task's
  terminal transition is recorded exactly once, ever — repolling never
  duplicates it and never invents a phantom completion for a task whose
  process this broker instance restarted without (see below).
- **Retention**: the broker never prunes the events table — it is
  append-only for the lifetime of the `.recollect` home, the same
  manual-cleanup posture as artifacts (see [operator-guide.md](operator-guide.md)).
  A cursor recorded at any point in the past remains valid indefinitely; there
  is no "cursor too old" failure mode.
- **Non-blocking, same-process pump**: every `completion_events` call also
  opportunistically finalizes (never blocks) any task *this exact broker/MCP
  server process* itself launched via `delegate`/`delegate_batch` and still
  holds a live process/request handle for, if that process has already
  exited (a plain non-blocking liveness check — `Popen.poll()` /
  `Thread.is_alive()` — never a bare wait). A task that is still genuinely
  running is left alone and simply does not appear yet. This is *not* a
  background watcher: it only ever runs as a direct side effect of a caller
  polling, and it only ever sees handles this process itself is holding — a
  task launched by a different broker/MCP process (e.g. before a restart)
  is untouched here and requires `reconcile`/`reconcile_pending` instead,
  which is a separate, already-documented restart-recovery path.
- **completion_events vs collect**: an event payload is a compact
  notification — `state`, lineage fields, `result_summary`, `artifact_count`,
  and `verification_gate.label` — never raw logs and never the full
  `runtime_result`/`normalized_result`. `collect` remains the only way to
  fetch the full, artifact-backed result; call it once a task's id has shown
  up here.

Non-goals: this is bounded, polling-only completion observation, not a
workflow engine or a general event bus. There is no push notification —
the caller always initiates every check — and no built-in retry/backoff
policy, round scheduler, or multi-round debate/synthesis orchestration; the
parent decides its own round structure and retry cadence using this and
`delegate_batch` as primitives (see [PRD.md §3.1](design/PRD.md#31-delegation-shape-dynamic-not-fixed)).

## Tool result envelope

Successful tool calls return MCP `content` with JSON:

```json
{
  "envelope_version": 1,
  "tool": "collect",
  "ok": true,
  "data": { }
}
```

Errors use `"ok": false` and `"error": { "code", "message" }` at the envelope level (business errors), distinct from JSON-RPC protocol errors.

## Provider configuration is a startup snapshot

`discover_capabilities` includes a `provider_config` object:

```json
{
  "source": "/path/to/.recollect/config.yaml",
  "source_origin": "repo_local",
  "loaded_at": "2026-07-16T13:59:41.475052+00:00",
  "restart_required_for_changes": true,
  "note": "Provider configuration is a startup snapshot: the resolved configuration file (if any) is read once when the broker/MCP process starts. Editing the file on disk afterward does not change the running process — restart the broker/MCP server to load changes."
}
```

`source` is `"not_configured"` when no provider configuration file was resolved from any tier. `source_origin` names which precedence tier selected `source`: `explicit` (`--providers-config`), `env` (`RECOLLECT_CONFIG`), `repo_local` (`./.recollect/config.{yaml,yml,json}`), `user_level` (`~/.recollect/config.{yaml,yml,json}`), `legacy_default` (`./providers.json`), or `not_configured`. See [cli.md](cli.md#provider-configuration-resolution-order) for the full precedence order and its fail-truthfully rule for configured (explicit/env) sources. `loaded_at` is when *this* process read the file — not when it was last modified on disk. There is no hot reload: if you edit the file, this MCP server will keep serving the old snapshot until it is restarted. Never contains credential values. Both JSON and YAML (safe-loaded only) are supported.

## Runtime capability contract

Each entry in `discover_capabilities`'s `runtimes` array carries a `capability_contract` object:

```json
{
  "output_kind": "text_synthesis",
  "mutates_workspace": false,
  "owns_worktree": false,
  "materialization_owner": "parent_applies_text",
  "parent_materialization_required": true,
  "materialization_note": "openai_compatible returns synthesized text only over HTTP; it never owns a git worktree or writes to any workspace. The parent that delegated this task must materialize (apply) and validate the returned text itself."
}
```

CLI runtimes (`mock`, `opencode`, `claude_code`, `codex`, `cursor`) report `output_kind: "workspace_mutation"` and `owns_worktree: true` — but `parent_materialization_required` is `true` for these too: the broker never merges an `isolated_worktree` branch back into the source workspace (see [operator-guide.md](operator-guide.md#materialize--validate--record-the-honest-parent-workflow)). Requesting an `execution_mode` a runtime does not permit is rejected at `delegate` time with a message naming supported alternative runtimes and this runtime's `materialization_note` — never a fabricated success.

## Host configuration example

```json
{
  "mcpServers": {
    "recollect-lines": {
      "command": "recollect-mcp",
      "args": ["--home", "/path/to/.recollect"]
    }
  }
}
```

Illustrative Hermes-style entry (optional, not required):

```json
{
  "mcpServers": {
    "recollect-lines": {
      "command": "recollect-mcp",
      "args": ["--home", "~/.recollect"]
    }
  }
}
```

## Offline acceptance

```bash
python3 scripts/mcp_acceptance.py
```

Uses deterministic fake CLIs — no network or credentials.

## Parent-agent flow

See [user-flows.md](user-flows.md#parent-agent-mcp-flow).
