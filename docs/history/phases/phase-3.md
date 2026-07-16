# Phase 3 — Workspace Safety

## Scope

Phase 3 adds local Git workspace isolation and broker-side verification
evidence on top of the Phase 1 broker core and Phase 2 OpenCode adapter. It
introduces:

- `workspace.py`: `WorkspaceManager`, which validates a source workspace and
  creates/diffs/removes broker-owned Git worktrees.
- A durable `workspace_leases` table in SQLite enforcing one active writer
  per canonical source workspace.
- Broker integration for the `isolated_worktree` execution mode: worktree
  allocation before adapter launch, workspace-status/diff artifacts before
  cleanup, and idempotent release on every terminal lifecycle path.
- `Broker.verify()`: broker-executed verification commands (argv arrays
  only), with raw stdout/stderr/exit-code evidence persisted as an artifact.

No MCP surface, no daemon/restart recovery, no remote-worker execution, no
web UI, and no multi-user authorization are introduced here — see
[Known limitations](#known-limitations) and
[Phase 4 boundary](#phase-4-boundary).

## Data / safety model

### Workspace validation and allocation

`canonical_source(workspace)` resolves the caller-supplied `workspace` path
to its Git toplevel via `git -C <workspace> rev-parse --show-toplevel`,
raising `WorkspaceError` if the path isn't inside a Git repository or
worktree. This is the only thing ever read from the source; nothing is
written to it.

For a task with `execution_mode="isolated_worktree"`, `Broker.start()`:

1. Resolves and validates the canonical source.
2. Captures `base_sha` via `git rev-parse HEAD` in the source.
3. Acquires a durable lease (below) for `(task_id, canonical_source,
   worktree_path, branch, base_sha)` *before* touching the filesystem.
4. Only on a successful lease acquisition does it run
   `git -C <source> worktree add -b recollect/<task_id> <worktree_path>
   <base_sha>`, creating a broker-owned worktree under
   `<home>/worktrees/<task_id>` on a deterministic, task-specific branch.
5. Passes that worktree path — never the original `workspace` — as the
   adapter's runtime working directory (`MockAdapter.start_metadata` and
   `OpenCodeAdapter.start(..., workspace=...)` both take the *effective*
   workspace explicitly).

If validation, lease acquisition, or worktree creation fails, the task
transitions directly to `FAILED` with a machine-readable `reason` (
`workspace_invalid`, `workspace_lease_conflict`, or
`workspace_allocation_failed`) and the underlying error text — never left
hanging in `PREPARING`.

### Lease durability and concurrency

The `workspace_leases` table (in `store.py`) has one row per task that ever
allocated a worktree, keyed by `task_id`, and a **partial unique index** on
`canonical_source` filtered to `status = 'active'`. That index is what
enforces "one active writer per canonical source": a second concurrent
`INSERT` for the same source while a lease is still active raises a SQLite
`IntegrityError`, which the store surfaces as `WorkspaceLeaseConflict`. This
holds even across independent `Broker`/connection instances (the enforcement
lives in the database, not in an in-process lock), and it is exactly the
mechanism already used elsewhere in this codebase for durability (SQLite +
WAL, per Phase 1).

Read-only tasks (`execution_mode="read_only"`) never call `acquire_lease` at
all — they run directly against the source workspace and are therefore
unaffected by another task's writer lease, and can run fully concurrently
with each other and with an active writer.

Releasing a lease (`release_lease`) is an idempotent `UPDATE ... WHERE
status = 'active'`: calling it when no active lease exists (already
released, or never allocated) is a silent no-op, not an error.

### Result / artifact capture

Before a worktree is released, `Broker._finalize_workspace()`:

1. Stages everything in the worktree (`git add -A`, so untracked files are
   included) and diffs the staged tree against `base_sha`, producing:
   - `changed_paths`: parsed `git diff --cached --name-status` output.
   - `diff_status`: `"changed"` if any paths differ, `"clean"` otherwise —
     an empty diff is recorded explicitly, not omitted.
   - The raw patch bytes via `git diff --cached --binary`.
2. Writes `workspace_status.json` (source, worktree path, branch, base SHA,
   changed paths, diff status) and `diff.patch` (the raw patch, written with
   `Path.write_bytes` so binary diffs round-trip byte-for-byte) as task
   artifacts, going through the same `TaskStore.write_artifact` /
   `refresh_manifest` path Phase 1 already uses for `request.json` /
   `result.json`.
3. Only then calls `WorkspaceManager.release()` to remove the worktree and
   `release_lease()` to mark the lease released.

This runs on every terminal transition: `complete()` (mock success),
`collect()` (OpenCode success and failure, and the "no process handle"
fallback), `cancel()` (queued-cancel, and running-cancel whether it lands on
`CANCELLED` or `FAILED`), and `timeout()`. `_finalize_workspace` itself is
idempotent — it checks the lease is still `active` before doing any work, so
calling it again after a prior cleanup (e.g. a retried cleanup pass) is a
no-op. A `git` failure while capturing the diff is recorded (`diff_status:
"unknown"`, `capture_error`) rather than raised, so a broken diff capture can
never leave a lease stuck active and blocking every future writer.

Two paths deliberately *skip* cleanup rather than run it, because deleting
the worktree would mean deleting files a process might still be using:

- `collect()`'s "no process handle" fallback (a broker restart loses the
  in-memory `ProcessHandle`, but the real OpenCode child — started with
  `start_new_session=True` — can still be alive). Before cleaning up, it
  probes the last known process group from the durable `task.running` event
  metadata with `os.killpg(pgid, 0)` (the same technique the Phase 2
  adapter's own cancellation path uses) and only finalizes the workspace if
  that group is confirmed dead.
- `cancel()`'s running-task path, when `cancellation["group_terminated"]` is
  `False` (termination sent but not confirmed) — the task still reaches
  `FAILED`, but the lease stays active and the worktree is left in place
  until termination can be confirmed some other way.

Both cases leave the lease active, which means the source stays blocked for
new writers rather than risking data loss for an unconfirmed-dead process —
a deliberate "block-safe over delete-fast" default.

`start()` also releases a freshly acquired lease/worktree if anything after
allocation fails before reaching `RUNNING` — a non-`WorkspaceError` failure
creating the worktree (e.g. `git` itself missing), or the adapter failing to
launch — so a single failed launch cannot permanently block a source
workspace for every subsequent task.

### Cleanup safety

`WorkspaceManager.release(source, worktree_path)`:

- Refuses (raises `WorkspaceError`) to touch any path that doesn't resolve
  under its own `<home>/worktrees` root — a broker instance can never be
  asked to remove an arbitrary directory, including the source itself.
- Treats an already-absent worktree directory as success (`already_absent:
  true`), skipping the removal attempt entirely.
- Runs `git worktree remove --force`, falling back to a plain `shutil.rmtree`
  only if that fails, then always runs `git worktree prune` in the source to
  clear stale administrative metadata (this touches only `.git/worktrees/`
  bookkeeping in the source, never its tracked working-tree content).

### Broker-side verification

`Broker.verify(task_id, commands)` takes `commands` as a list of argv
arrays (e.g. `[["pytest", "-q"]]`) — never a shell string — and runs each
with `subprocess.run(command, cwd=working_dir, ...)`, which never sets
`shell=True`. Every command is validated as a non-empty list of strings
*before any of them run* — a malformed command later in the list raises
`ValueError` without side effects from commands earlier in the list. An
`isolated_worktree` task always runs verification in its worktree; if that
worktree's lease is not currently active (not yet allocated, or already
released after the task finished), `verify()` raises rather than silently
falling back to running the caller's commands against the real source
workspace. A `read_only` task (which never gets a worktree) runs directly
against its workspace, since there's no isolation contract to honor there.
For each command it captures `exit_code`, a derived `passed` boolean,
wall-clock `duration_seconds`, and raw `stdout`/`stderr` — persisted
verbatim as `verification.json` and echoed as a `task.verified` event.

Every entry from this path carries `broker_verified: true` — not because
the command passed, but because the broker itself, not the adapter/agent,
executed the subprocess and observed its outcome directly. This is
deliberately separate from (and does not change) the OpenCode adapter's own
`runtime.verification.tests_broker_verified: false` on agent-reported
results (Phase 2) — an agent's self-report is never promoted to
broker-verified; only this explicit, broker-run path is.

## Verified behavior

Exercised in `tests/test_workspace_safety.py` against real local Git
fixtures (temporary repos created and torn down per test, no network/model
calls):

1. An isolated task's edits land in its worktree; the source repo's working
   tree, `git status`, and file contents are byte-identical before and
   after (`test_isolated_worktree_receives_changes_while_source_stays_untouched`).
2. Two writer tasks against the same canonical source: the first to `start()`
   gets `RUNNING`, the second gets `FAILED` with
   `reason: workspace_lease_conflict`; a concurrent read-only task against
   the same source still reaches `RUNNING`
   (`test_two_writers_contend_exactly_one_wins_read_only_stays_concurrent`).
3. A lease acquired by one `Broker` instance is visible, with matching
   fields, to a fresh `Broker` instance opened against the same home
   directory, and is marked `released` (with `released_at` set) after
   cleanup (`test_lease_persists_reloads_from_sqlite_and_releases_on_cleanup`).
4. `workspace_status.json`/`diff.patch` distinguish a task that made no
   changes (`diff_status: "clean"`, empty patch) from one that did
   (`diff_status: "changed"`, non-empty patch naming the changed paths)
   (`test_diff_and_status_distinguish_clean_from_changed`).
5. `verify()` captures matching raw stdout/stderr/exit-code evidence for
   both a passing and a non-zero-exit command in the same call, both marked
   `broker_verified: true`
   (`test_verify_captures_raw_output_and_broker_verified_truth`); a
   shell-string command is rejected outright, before any command in the
   batch runs (`test_verify_rejects_non_argv_commands`,
   `test_verify_validates_all_commands_before_running_any`); and it refuses
   to fall back to the real source workspace once an isolated task's
   worktree has been released (`test_verify_refuses_to_fall_back_to_source_after_worktree_release`).
6. Cancellation and timeout cleanup remove the worktree, never the source,
   and a repeated cleanup call afterwards is a safe no-op
   (`test_cancel_cleanup_is_idempotent_and_never_touches_source`,
   `test_timeout_cleanup_never_removes_source_and_is_idempotent`).
7. `WorkspaceManager.release()` refuses to remove a path outside its own
   worktrees root, including the source repo itself
   (`test_release_refuses_to_remove_path_outside_broker_worktrees_root`).
8. A workspace that isn't a Git repository fails `start()` cleanly with
   `reason: workspace_invalid`
   (`test_start_fails_clearly_when_workspace_is_not_a_git_repo`).
9. An `isolated_worktree` OpenCode task uses the worktree (not the source) as
   its runtime working directory and cleans up on a successful `collect()`
   (`test_isolated_worktree_is_the_effective_workspace_and_cleans_up_on_success`);
   when a broker restart loses the in-memory process handle but the real
   process group is confirmed still alive, cleanup is skipped (worktree and
   active lease both preserved) until the group is confirmed dead
   (`test_collect_skips_cleanup_when_process_group_is_still_alive_after_a_simulated_restart`).
10. All Phase 1 (`tests/test_broker.py`) and Phase 2
    (`tests/test_opencode_adapter.py`) tests pass unchanged.

## Known limitations

- POSIX/Git-CLI only: worktree operations shell out to the `git` binary;
  there is no libgit2/dulwich fallback and no Windows-specific handling
  beyond what Git itself provides.
- A lease and its worktree are allocated once per task at `start()` and
  never re-validated mid-run; if something outside the broker deletes the
  worktree directory while the task is `RUNNING`, that's only discovered
  (and safely handled) at the next cleanup attempt, not immediately.
- `verify()` is a broker-callable capability, not part of the task's own
  automatic lifecycle — nothing currently forces a verification pass before
  a task can reach a successful terminal state. Wiring a mandatory
  verification gate into the lifecycle graph is future work if needed.
- `git add -A` before diffing respects `.gitignore`; changes to ignored
  paths are (by design) never captured in `diff.patch` or `changed_paths`.
- No cross-process file lock on the worktrees directory itself — concurrency
  safety comes entirely from the SQLite lease table, consistent with how
  Phase 1 already relies on SQLite + WAL for durable concurrent access.
- Process handles for OpenCode tasks remain non-durable across broker
  restarts (a Phase 2 limitation, unchanged here): a restart mid-`RUNNING`
  still loses the ability to `collect()`/`cancel()` that specific process.
  Phase 3 adds one safety net on top of that limitation — cleanup checks the
  last known process group (`os.killpg(pgid, 0)`) before deleting anything,
  so a still-alive orphaned process is never deleted out from under, at the
  cost of that source staying leased (blocked for new writers) until the
  process is confirmed dead by some other means. There is still no
  mechanism to re-attach to or reap that orphaned process itself.
- Similarly, if `cancel()` cannot confirm the OpenCode process group actually
  terminated (`group_terminated: false`), the task still reaches `FAILED`
  but the worktree and lease are deliberately left in place rather than
  risking deletion under a possibly-still-running process.

## Phase 4 boundary

Phase 3 stops at local workspace safety and broker-side verification
evidence. Explicitly out of scope, deferred to Phase 4 or later:

- MCP surface.
- Daemon/restart recovery (durable re-attachment to in-flight processes).
- Remote-worker product execution.
- Web UI.
- Multi-user authorization.
