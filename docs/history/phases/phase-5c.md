# Phase 5C — Verification gate, timeout liveness safety, and generic MCP-host acceptance

## Scope

Phase 5C closes two lifecycle gaps left open at the end of Phase 5B
(RFC-001 §9, [phase-5b.md](phase-5b.md) "Non-goals carried forward") and
adds one new piece of acceptance evidence:

1. **Verification-gate policy**: an opt-in, per-task `verification_policy`
   (`none` / `advisory` / `required`) folded into the real lifecycle
   (`complete()`/`collect()`), not just the pre-existing caller-invoked
   `Broker.verify()`.
2. **`Broker.timeout()` liveness classification**: timing out a task no
   longer finalizes (and potentially deletes) its workspace on the
   caller's say-so alone; it now classifies the same restart-safety
   process-group liveness `reconcile()`/`cancel()` already used before
   touching anything.
3. **A generic MCP-host acceptance harness** (`scripts/mcp_acceptance.py`):
   a real stdio JSON-RPC client driving a real `recollect-mcp` subprocess
   against a disposable local Git fixture, proving the documented
   delegate → observe → collect → cancel lifecycle end to end using the
   standard MCP stdio protocol any compliant host could speak.

## Documentation correction this phase makes

An earlier draft of this phase's planning entry in [PHASE-5.md](PHASE-5.md)
described its acceptance work as "a real field acceptance run driven by
Hermes (or an equivalent operator)." That was a documentation error, not a
product decision: Recollect Lines is a provider- and host-neutral
delegation broker (see [PRD.md](../../design/PRD.md) §1, §3.1); Hermes is one possible
operator/host environment among many, never a required dependency or an
acceptance criterion. This phase's actual acceptance harness requires no
Hermes installation, CLI, account, or configuration to run — it is a
plain-stdlib JSON-RPC client. See [README.md](../../README.md) for a generic
MCP client configuration example, with one clearly labeled, optional
Hermes-specific example alongside it.

## Verification-gate policy

`TaskRequest.verification_policy` (CLI `--verification-policy`, MCP
`delegate`/`delegate_batch` field `verification_policy`) accepts three
values, all backward compatible with every pre-5C caller by default
(`none`):

- **`none`** (default): any `verify_commands` declared at delegate time
  still run as broker-verified evidence when the task is collected —
  unchanged from Phase 3/5B — but the outcome never affects the task's
  terminal state.
- **`advisory`**: a verification failure downgrades a runtime success to
  `succeeded_with_warnings`. It never blocks a success outright, and never
  touches an already-failed candidate outcome.
- **`required`**: a verification failure — or a `required` policy with no
  `verify_commands` declared at all, or a command that could not even be
  run — forces a would-be success to `failed`. A runtime failure is never
  "rescued" into a success by a passing verification pass, under any
  policy.

The runtime-reported result (`result.json`) is never rewritten or erased
by a gate outcome; only the task's terminal `TaskState` and a new
`verification_gate.json` artifact (skipped entirely when policy is `none`
and nothing was declared, to leave a plain evidence-only task's artifact
manifest identical to pre-5C behavior) record what the gate decided and
why. `models.verification_gate_label()` collapses that artifact into one
of four caller-facing labels surfaced by MCP `collect` —
`runtime_reported`, `advisory_verified` / `advisory_verification_failed`,
`required_verified`, or `blocked_failed_verification` — so a caller never
has to re-derive the policy/outcome cross product itself.

The gate is applied by `Broker._apply_verification_gate()` at every place
a candidate terminal state is produced — the mock adapter's `complete()`,
and both the success and failure branches of the real OpenCode
`collect()` — always *before* `_finalize_workspace()` releases the
worktree lease, so verification still runs against the exact workspace
the runtime task wrote to. It is skipped, not silently passed, on an
already-terminal task: `collect()`'s existing Phase 5B idempotency
(`state in TERMINAL_STATES` short-circuits before any gate logic runs)
means a repeated `collect()` call never re-executes verification commands
a second time.

A broker crash between the runtime finishing and the gate/terminal
transition being durably written is handled the same honest way Phase 5B
handles a crash mid-`collect()`: `COLLECTING` is now a reconcilable state
(`Broker._RECONCILABLE_STATES`), and a fresh broker reconciling a task
stuck there reaches `failed` (if the process group is confirmed dead) or
`recovery_required` (if it might still be alive) — never a fabricated
success, whatever the interrupted verification would have decided.

## Timeout liveness safety

Phase 5B's `reconcile()`/`cancel()` already refused to treat an
unconfirmed-alive process group as proof of anything; `Broker.timeout()`
did not follow the same discipline — it finalized (and could delete) a
workspace purely because a caller said the clock had run out, without
checking the runtime process group at all. Phase 5C brings `timeout()`
under the same classification `_process_group_status()` already provides:

| Situation | Outcome | Workspace/lease |
|---|---|---|
| In-memory process handle present, group terminates on signal | `timed_out` | released |
| In-memory process handle present, termination not confirmed | `recovery_required` | **untouched** |
| Mock profile (never holds a subprocess) | `timed_out` immediately | released (nothing to protect) |
| No in-memory handle, durable launch record confirms `dead`/absent | `timed_out` | released |
| No in-memory handle, durable launch record confirms `alive` | signalled via persisted pgid; `timed_out` if confirmed terminated, else `recovery_required` | released only if confirmed terminated |
| No in-memory handle, pgid missing/invalid in persisted metadata | `recovery_required` | **untouched** — invalid metadata is never treated as proof of death |

`timeout()` is idempotent on an already-terminal task (returns the record
unchanged, no duplicate event) and, like `cancel()`, escalates
`SIGTERM` → `SIGKILL` after the adapter's configured grace period rather
than declaring termination on the first signal alone.

## Generic MCP-host acceptance harness

`scripts/mcp_acceptance.py` is a standalone script (no `unittest`
dependency, no import of the `tests` package) that:

1. Creates a disposable local Git repository as its fixture workspace —
   never the actual project checkout.
2. Spawns a real `recollect-mcp` subprocess (the same entry point
   `pyproject.toml` installs as the `recollect-mcp` console script),
   pointed at the repo's deterministic `tests/fixtures/fake_opencode.py`
   stand-in via `--opencode-command` so the harness needs no network
   access, no real `opencode-ai` package, and no model credentials — while
   still exercising a real subprocess-of-a-subprocess process tree end to
   end.
3. Performs the `initialize` → `notifications/initialized` → `tools/list`
   handshake any MCP client performs, then drives `delegate` → `status` →
   `collect` → `cancel` → `reconcile` exactly as documented, including a
   `required` verification-gate pass and a live-process cancellation.
4. Asserts the fixture repository's `HEAD` and working tree are byte-for-
   byte unchanged afterward — the same workspace-safety guarantee
   `tests/test_workspace_safety.py` verifies at the unit level, checked
   here end to end through the real MCP wire protocol.

Run it directly:

```bash
python3 scripts/mcp_acceptance.py
```

It prints one `PASS`/`FAIL` line per check and exits `0` only if every
check passed. CI (`.github/workflows/ci.yml`) runs it on every matrix leg,
so this evidence is continuously re-verified, not a one-time manual claim.

This harness is deliberately generic: nothing in it assumes Hermes,
OpenCode-as-a-host, or any other specific parent-agent product. Any host
that can spawn a subprocess and speak newline-delimited JSON-RPC 2.0 over
its stdio can integrate the same way.

## Non-goals carried forward

- **No daemon, no background watchdog.** `timeout()` and `reconcile()`
  remain caller/operator-invoked operations; Phase 5C adds no scheduler,
  no HTTP listener, and no automatic timer that calls them for you. This
  matches the product's explicit "no daemon" non-goal (PRD.md §8).
- **No new runtime adapter.** OpenCode remains the only implemented
  adapter, and RFC-001 still marks it "experimental." Phase 5C's
  verification gate and timeout-liveness work apply equally to any future
  adapter (they live in broker-owned lifecycle code, not adapter code),
  but adding a second adapter itself is out of scope here.
- **No remote workers, multi-user functionality, or additional MCP
  transport.** Only local stdio, exactly as before.

## Honest gap against the original product PRD

The original product PRD's MVP boundary (§8.2) names **at least two
heterogeneous runtime adapters** as an MVP requirement, with **Claude
Code CLI** and **Codex CLI** as the preferred initial pair — precisely
because a single adapter cannot demonstrate the product's core
provider-independence claim. This codebase still implements exactly one
adapter (OpenCode, experimental). Phase 5C does not attempt to close that
gap; it is the single largest piece of unimplemented MVP scope and should
be treated as the next priority after this phase, not as something this
PR's acceptance evidence should be read as having satisfied. See
[PRD.md](../../design/PRD.md) §9 and [RFC-001.md](../../design/RFC-001.md) §8 for the full,
continuously-honest capability accounting.

**Addendum (post-5C):** a roadmap decision made after this phase's own
implementation work landed has since sequenced that gap as Phase 6A
(Claude Code CLI adapter), Phase 6B (Codex CLI adapter), and Phase 6B.5
(Cursor CLI adapter), alongside a separately scheduled Phase 6C (plural,
configurable OpenAI-compatible provider fabric and a capability-limited
direct-API runtime foundation) and Phase 6D (capability discovery,
policy-aware routing, and bounded model-council patterns). See
[PHASE-5.md](PHASE-5.md) and [RFC-001.md](../../design/RFC-001.md) §10 for the full
sequence and design constraints. This addendum is documentation only — it
schedules Phase 6, it does not implement any of it.

## Test evidence

- `tests/test_verification_gate.py`: all three policy outcomes against
  the mock adapter and the real (fixture) OpenCode adapter; the
  no-commands-declared and unrunnable-command `required` cases; idempotent
  repeated `collect()` never re-runs verification; a broker crash after
  the runtime finished but before the gate/result were durably written
  never reconciles to a fabricated success; a broker crash while the
  process group is still alive correctly enters `recovery_required`; CLI
  wiring of `--verification-policy`/`--verify-command`; MCP `collect`
  surfaces `verification_gate.label`.
- `tests/test_timeout_liveness.py`: mock-task timeout regression
  (unchanged, immediate finalize); idempotent repeated `timeout()`; a live
  in-memory process group is actually terminated (not just declared
  timed out) before finalizing, including `SIGTERM` → `SIGKILL`
  escalation; post-restart timeout against a confirmed-dead durable
  launch record; post-restart timeout against a confirmed-alive group
  (terminates it, still reaches `timed_out`, the exact gap this phase
  closes); post-restart timeout with unconfirmable (missing/invalid)
  persisted liveness metadata never finalizes; no leaked process groups
  after any `timeout()` path. Every test that spawns a real POSIX process
  group kills and reaps it in a `finally` block.
- `scripts/mcp_acceptance.py`: real end-to-end evidence described above,
  run in CI on every push/PR.
