# Phase 5B — Durable runtime recovery and idempotent collection

## Scope

Phase 5B answers what happens to an in-flight `opencode`-profile task when the
`Broker` process that started it disappears and a new one takes its place. It
adds:

1. **Durable launch identity** (`store.runtime_launches`): the moment an
   adapter subprocess actually exists, its identity is persisted — adapter
   name/label, pid/pgid, launch timestamp, a redacted command, the effective
   workspace, artifact filenames, and a reconciliation marker. This is a real
   schema, not an ad-hoc log line; it's additive (`CREATE TABLE IF NOT
   EXISTS`), so it migrates an existing Phase 1–5A database with no data loss.
2. **Explicit reconciliation** (`Broker.reconcile()` /
   `Broker.reconcile_pending()`, CLI `reconcile` / `reconcile-all`, MCP
   `reconcile`): the operation a freshly constructed `Broker` — with no
   in-memory `ProcessHandle` — uses to inspect a durable launch record and
   decide, truthfully, what to do next.
3. **Idempotent `collect()`**: calling it again on an already-terminal task
   returns the same durable record with no re-transition, no duplicate
   cleanup, and (at the MCP layer) no re-run verification commands.
4. **Safer `cancel()`**: an `opencode` task with no in-memory handle no longer
   falls through to the mock-style "declare cancelled and delete the
   workspace" path. It consults the durable launch record and the process
   group's actual liveness first.

## What this is *not*

- **Not transparent re-attachment.** A new `Broker` process never regains the
  ability to stream/collect a still-running OpenCode process's output — that
  would require re-establishing a `Popen`/child relationship the OS doesn't
  let a new process obtain. This was true after Phase 2 and remains true
  here; see [phase-2.md](phase-2.md#known-limitations) and
  [../design/RFC-001.md](../../design/RFC-001.md) §8.
- **Not a recovered success.** Nothing in this phase upgrades an unobserved
  runtime outcome into `succeeded`. A dead-but-unconfirmed-successful process
  is reported `failed`, honestly, with a reason that says evidence was lost
  to a restart — never silently treated as if it had been collected normally.
- **Not a daemon.** Reconciliation is a broker operation callers invoke
  (CLI/MCP/directly); Phase 5B does not add a background scheduler, an HTTP
  service, or a second runtime adapter.

## State machine addition

One new state, `recovery_required` — non-terminal, actionable:

```
running ──(process group confirmed dead, or no durable launch record)──▶ failed
running ──(process group still alive / liveness unconfirmed)──▶ recovery_required
recovery_required ──(re-reconciled, now confirmed dead)──▶ failed
recovery_required ──(cancel requested)──▶ cancelling ──▶ cancelled | recovery_required
```

`recovery_required` never auto-resolves to a success state. The only ways out
are: reconciliation later confirms the process group is dead (→ `failed`), or
an explicit cancel via the persisted pgid confirms termination (→
`cancelled`).

## Reconciliation decision table

`Broker.reconcile(task_id)` (and the bulk `reconcile_pending()`) only acts on
`opencode`-profile tasks with no in-memory `ProcessHandle` that are in one of
four reconcilable states: `running` and `preparing` (an ordinary restart can
land either just before or just after the `running` transition —
`record_launch()` always happens first, so a crash on either side of it
leaves the same durable row behind), `cancelling` (a crash mid-signal), or
`recovery_required` (a previous reconciliation pass). Mock-profile tasks are
untouched — they never hold a subprocess, so a restart never puts them at
risk, even if one is legitimately still `running` (waiting on `complete()`).

| Task state | Durable launch record | `killpg(pgid, 0)` | Outcome | Workspace/lease |
|---|---|---|---|---|
| `running` / `preparing` | Missing entirely (`no_launch`) | n/a | `failed`, reason `missing_process_handle` | released (nothing to protect) |
| `running` / `preparing` | Present, pgid missing/invalid (`unknown`) | not attempted | `recovery_required`, reason `runtime_metadata_missing_or_invalid` | **untouched** (conservative: never treated as proof of death) |
| `running` / `preparing` | Present, valid pgid | `ProcessLookupError` (dead) | `failed`, reason `process_group_confirmed_dead` | released, diff/status artifacts captured |
| `running` / `preparing` | Present, valid pgid | succeeds / `PermissionError` (alive) | `recovery_required`, reason `process_group_alive_after_restart` | **untouched** |
| `cancelling` | Present, valid pgid | dead | `cancelled` — the in-progress cancellation is confirmed complete | released |
| `cancelling` | any not-confirmed-dead case | — | `recovery_required` | **untouched** |
| `recovery_required` | (re-checked) | dead | `failed` | released |
| `recovery_required` | (re-checked) | still alive/unknown | unchanged; audit event only | **untouched** |

Re-running `reconcile()` while still alive is a no-op state-wise: it appends
a `task.reconciliation_checked` audit event and returns the unchanged record.
It never asserts a result it didn't observe.

## Cancellation via a persisted pgid

`Broker.cancel()` on an `opencode` task with no in-memory handle follows the
same liveness classification. If the group is confirmed alive, it is
signalled directly by pgid (`SIGTERM`, then `SIGKILL` after the adapter's
configured grace period) and liveness is re-polled — mirroring the in-memory
cancellation path, just without a `Popen` to `wait()` on (so no exit code is
available). If termination can't be confirmed, the task moves to
`recovery_required` rather than being declared cancelled.

**Threat model note, stated honestly:** confirming a pgid is "alive"
immediately before signalling it does not eliminate PID/PGID reuse risk. The
OS can recycle a pgid number after the original process exits; a `killpg`
that finds *some* process group with that id does not prove it is *the one
this broker launched*. This codebase does not implement additional
verification (e.g. comparing `/proc/<pgid>/stat` start time) — that's a
stdlib-only Linux-specific check with its own edge cases, and the residual
window here is small (immediately-preceding liveness check, then signal).
Anyone deploying this in an environment with fast PID reuse and untrusted
co-tenants should treat persisted-pgid cancellation as a best-effort
convenience, not a hard security boundary. `killpg` is never called against a
bare, unverified pgid — only after `_process_group_status` reports "alive".

## Operator procedure after a broker restart

1. Construct a `Broker` against the existing `--home` directory as usual —
   this alone changes nothing; it does not automatically reconcile anything.
2. Run `recollect-lines reconcile-all` (or the MCP `reconcile` tool with no
   `task_id`) once, before resuming normal `collect`/`cancel` traffic. Every
   `opencode` task left `running` is reconciled: dead ones become `failed`
   with artifacts intact; still-alive ones become `recovery_required`.
3. For each `recovery_required` task: either wait and reconcile again once
   the process has actually exited, or call `cancel` — it will attempt a
   direct pgid-based termination and only report `cancelled` once liveness
   is actually re-confirmed dead.
4. `collect()` on a `recovery_required` task raises `RecoveryRequired`
   (a `ValueError`) rather than fabricating a result — surfaced as `isError`
   over MCP, exit code 2 over the CLI, same as any other business error.
5. `verify()` refuses outright on a `recovery_required` task: its worktree
   lease is still active by design, but an unconfirmed-dead process might
   still be writing to it.

## Non-goals carried forward

- Windows process groups, a network/HTTP transport, multi-tenant auth, a
  second real runtime adapter, and a mandatory verification gate remain out
  of scope here exactly as recorded in [PHASE-5.md](PHASE-5.md) and
  [RFC-001](../../design/RFC-001.md) — Phase 5B does not expand that boundary.
- `Broker.timeout()` still finalizes the workspace unconditionally on the
  caller's say-so, without a liveness check of its own. This is a known,
  pre-existing gap adjacent to the one this phase fixes for `collect()`/
  `cancel()`, left unaddressed here to keep this change scoped to the
  behavior actually specified for Phase 5B.

## Test evidence

`tests/test_lifecycle_recovery.py` covers: durable launch metadata surviving
a fresh `Broker`/`TaskStore`; dead-after-restart reconciliation reaching a
truthful `failed` with lease/workspace cleanup; live-after-restart
reconciliation reaching `recovery_required` with the workspace/lease left
untouched, and idempotent re-checks; conservative handling of a missing/
invalid persisted pgid (`reconcile()` and `cancel()` both refuse to treat it
as dead); idempotent `collect()` (repeated call returns the identical durable
record and artifact, no duplicate events) and idempotent MCP-level `collect`
(verification commands run at most once); pgid-based cancellation both when
the process honors `SIGTERM` and when it must be escalated to `SIGKILL`;
`reconcile_pending()` only touching `opencode` tasks that need it, leaving a
legitimately-still-`running` mock task alone; the crash window where a launch
was durably recorded but the task never reached `running` (still `preparing`)
or never finished being cancelled (stuck `cancelling`), for both a
dead-on-inspection and a still-alive process; and `redact_command()`'s
`--flag value` / `--flag=value` forms, including a value that itself contains
a marker word without cascading into the next unrelated argument. Every test
that spawns a real POSIX process group kills and reaps it in a `finally`
block.
