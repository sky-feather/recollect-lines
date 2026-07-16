# Phase 2 — Experimental OpenCode Runtime Adapter

## Scope

Phase 2 adds `OpenCodeAdapter`, a runtime adapter that launches the real
`opencode-ai` CLI as a supervised child process. It plugs into the Phase 1
broker via `profile="opencode"`; the existing mock lifecycle is untouched.

## Adapter boundary

- `adapters.py` defines `AdapterCapabilities` (a frozen dataclass) and a
  structural `RuntimeAdapter` protocol requiring `name` + `capabilities`.
  `MockAdapter` and `OpenCodeAdapter` both satisfy it. The two adapters'
  start/collect/cancel method shapes are intentionally not unified — mock
  execution is instantaneous and synchronous, OpenCode execution is a
  supervised subprocess — forcing one interface would just be indirection.
- `opencode_adapter.py` holds `OpenCodeAdapter` and `ProcessHandle`
  (pid, pgid, command, artifact paths, the live `Popen`).

## Verified behavior

1. **Command generation.** `build_command(workspace, prompt)` returns
   `<prefix> run --pure --format json --dir <workspace> <prompt>`, with
   `DEFAULT_COMMAND_PREFIX = ("npx", "--yes", "opencode-ai@1.17.18")`. The
   prefix is a constructor argument so tests inject a local Python fixture
   (`tests/fixtures/fake_opencode.py`) instead of the real CLI.
2. **Artifacts.** `start()` creates task-scoped `events.jsonl` (subprocess
   stdout) and `stderr.log` (subprocess stderr) under the task's artifact
   directory before spawning, and launches with `start_new_session=True` so
   the child becomes its own process group leader.
3. **Supervision metadata.** `start()` returns `{adapter, command, pid, pgid,
   events_artifact, stderr_artifact}`, which the broker stores as the
   `task.running` event's metadata — enough to identify and probe the group
   later, without adding new persisted columns.
4. **Cancellation.** `cancel()` sends `SIGTERM` to the process group, waits
   up to `grace_period_seconds`, and probes group liveness with
   `os.killpg(pgid, 0)` (not the runtime's own exit code or output) before
   deciding whether to escalate to `SIGKILL`. It returns `signals_sent` and
   `group_terminated` as observed facts. The broker only reaches `CANCELLED`
   if `group_terminated` is true; otherwise it transitions to `FAILED` with
   the same factual metadata attached.
5. **Collection.** `collect()` parses `events.jsonl` line by line, skipping
   blank lines and counting (not raising on) malformed JSON. It takes the
   last event with `type == "text"` as the candidate summary, reading the
   text from a top-level `text` field or, failing that, a nested `part.text`
   field — the real `opencode run --format json` CLI nests the text under
   `part`, while the test fixture uses a flatter shape; both are handled so
   the fixture-backed unit tests and a live run against the real CLI agree.
   The result always carries
   `verification: {tests_broker_verified: false, source: "runtime_reported"}`
   — Phase 2 never independently re-runs or checks anything the runtime
   claims, so it never claims to.
6. **Broker wiring.** `Broker.start`/`Broker.cancel` branch on
   `record.profile == "opencode"`; a new `Broker.collect(task_id)` drives the
   OpenCode-specific COLLECTING → SUCCEEDED/SUCCEEDED_WITH_WARNINGS/FAILED
   transition. Process handles live in an in-memory dict on the broker
   instance, never touched by mock-profile tasks. If a handle is missing
   (broker restart, or `collect()` called twice) the task fails immediately
   instead of getting stuck in COLLECTING with no valid outgoing transition.
7. **Cancellation liveness.** After sending a signal, `cancel()` polls
   process-group liveness for a short bounded window (1s after SIGTERM, 2s
   after SIGKILL) rather than checking once immediately — `SIGKILL` delivery
   is asynchronous, and a grandchild process (e.g. the real `opencode`
   binary under npm's `sh -c` wrapper) can outlive the top-level
   `popen.wait()` by a beat while the kernel tears it down.

## Real CLI smoke verification

All of the above was additionally exercised against the actual
`npx --yes opencode-ai@1.17.18` CLI (not just the local test fixture) in a
disposable git-initialized workspace, with real model access available in
that environment:

- A full `create` → `start` → `collect` run reached `SUCCEEDED` with a
  correct, model-generated summary of the fixture's `add.py`.
- A `start` → `cancel` run against a real multi-process tree (`npx` → `sh -c`
  → `opencode`) reached `CANCELLED` with `group_terminated: true`, confirmed
  independently via `os.killpg(pgid, 0)` after the call returned.
- This exercise is what surfaced the nested `part.text` summary shape and the
  liveness-check race documented above — both were bugs until this round of
  smoke testing against the real CLI, and no unit test alone would have
  caught either since the fixture script's shape and process tree don't
  match the real CLI's.
- No unit test starts a real OpenCode process; all subprocess *unit* tests
  still use the local fixture script for hermetic, fast, offline runs.

## Known limitations

- Process handles are **not durable**: they exist only in the `Broker`
  instance that called `start()`. Restarting the broker process loses the
  ability to `collect()` or `cancel()` an in-flight OpenCode task (the task
  row and events remain, but the PID/PGID recorded in the event log becomes
  the only trace). Re-attaching to abandoned processes is out of scope here.
- Cancellation is POSIX-only (`os.killpg`, `start_new_session`); there is no
  Windows process-group equivalent implemented.
- `collect()` blocks the calling thread until the process exits — there is
  no polling/streaming status API yet.
- No MCP surface, no worktree allocation/leases, no remote workers, no
  product daemon — all explicitly out of scope for Phase 2.

## Remaining Phase 3/4 responsibilities

- Phase 3: git worktree allocation and leases; likely also durable
  re-attachment to running OpenCode processes across broker restarts.
- Phase 4: MCP surface.
