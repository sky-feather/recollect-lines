# Phase 7C RFC — Recovery, control contract, and compatibility evidence

Status: Phase **7C.4** (this document) adds bounded operator recovery/control
surfaces (CLI `control`, MCP `control`) atop the 7C.3 adoption contract —
explicit `status`/`cancel`/`collect`/`message` actions with a secret-safe
recovery/control view and fail-closed gates. Phase **7C.3** added safe
broker-restart reconciliation for **eligible durable subprocess launches only**.
Phase **7C.1** defined the typed recovery/control contract. Phase **7C.2** added
the durable subprocess-runner primitive and read-only launch inspection.
This document does **not** implement provider-native session resume or mid-task
message injection.

Related: [RFC-001.md](../../design/RFC-001.md), [phase-5b.md](phase-5b.md),
[phase-6d.md](phase-6d.md), [PRD.md](../../design/PRD.md).

## 1. Problem statement

Operators and parent agents need honest answers to:

- What can the broker do after it restarts while a task was in flight?
- Can we resume a provider-native session or steer a running CLI?
- What is declared product capability vs what was observed on one host?

Phase 7C.1 answers these with a **small vocabulary**, **fail-closed validation**,
and **redacted compatibility evidence**. Phase 7C.3 adds **durable process
adoption** for a narrowly eligible path only — distinct from provider session
resume.

## 2. Terminology (do not conflate)

| Concept | Meaning today (7C.1–7C.3) |
|---|---|
| **Durable process adoption** | After broker restart, a fresh broker re-acquires an **adopted handle** for a surviving durable subprocess launch only after manifest proof, PID+start-identity verification, adapter binding, and recovery-lease acquisition. Supports `status`, owned-group `cancel`, and terminal `collect` — **not** redispatch. **Implemented for the proven `fixture_durable` test runtime (7C.3).** |
| **Process recovery (legacy)** | Phase 5B PGID reconciliation for in-memory `ProcessHandle` subprocess CLIs (`observe_and_cancel`). Still **fail-closed** to `recovery_required` when alive after restart — unchanged. |
| **Post-restart output collection** | Collecting bounded stdout/stderr from durable artifacts after adoption. **Proven on `fixture_durable` only** via `collect_after_restart`. |
| **Provider-native session resume** | CLI/provider re-opening its own persisted session (e.g. `resume` in help text). **Unproven** — help keywords are not adoption proof. **Not implemented.** |
| **Continuation task** | Starting a *new* broker task that references prior context. Out of 7C scope; not claimed. |
| **Cancellation** | Broker-initiated stop via in-memory handle, adopted durable handle, or persisted PGID reconciliation (legacy). |
| **Free-form mid-task steering** | `recollect.message` / stdin injection while running. **Explicitly unsupported** for all runtimes. |

## 3. Capability vocabulary

### 3.1 Recovery levels (`recovery_level`)

| Level | Semantics |
|---|---|
| `none` | No subprocess/durable launch recovery affordance (mock, direct API). |
| `observe_and_cancel` | Broker may observe task state and attempt cancellation via persisted launch metadata after restart; **cannot** adopt or collect in-flight output. **All production subprocess CLIs (OpenCode, Claude Code, Codex, Cursor).** |
| `collect_after_restart` | Broker may adopt eligible durable subprocess launches after restart and collect bounded terminal artifacts. **Declared only on the proven `fixture_durable` test runtime (7C.3).** |
| `session_resume` | Provider-native session resume integrated with broker safety proof. **Not declared. Not implemented.** |

There is **no** global `recoverable: true` flag. Differences between runtime kinds must remain visible.

### 3.2 Control actions

| Action | Subprocess CLI (legacy) | Durable subprocess (`fixture_durable`) | Mock | Direct API |
|---|---|---|---|---|
| `status` | supported | supported (incl. adopted) | supported | supported |
| `cancel` | supported | supported (owned group; identity proof) | supported | supported (HTTP abort) |
| `collect` | in-memory only; fail-closed after restart | supported after adoption when terminal | supported | supported |
| `message` | **unsupported** | **unsupported** | **unsupported** | **unsupported** |

After broker restart, `collect` on **legacy** subprocess-backed tasks without
reconciliation remains **fail-closed** (`RecoveryRequired`) — unchanged from Phase 5B.

## 4. State / control matrix (broker truth)

| Task state | `status` | `cancel` | `collect` | `message` | After broker restart |
|---|---|---|---|---|---|
| running (in-memory handle) | yes | yes | no (not terminal) | unsupported | N/A |
| running (adopted durable handle) | yes | yes (owned PG) | only if terminal | unsupported | 7C.3 adoption path |
| running (no handle, legacy launch) | yes | via reconcile | fail-closed | unsupported | → `recovery_required` |
| recovery_required (legacy) | yes | via reconcile | fail-closed | unsupported | no fabricated success |
| recovery_required (durable eligible) | yes | after reconcile adoption | after adoption + terminal | unsupported | reconcile may adopt → `running` |
| terminal | yes | no-op | yes (if succeeded) | unsupported | unchanged |

## 5. Safety proof required before adoption

Before any runtime may declare `collect_after_restart` or `session_resume`, **all**
of the following must be present and consistent (fail-closed on missing/corrupt/conflict):

1. **Launch ID** tied to broker home and task ID.
2. **Task/workspace ownership** — lease and worktree policy still enforced.
3. **Adapter kind** — must match persisted launch record and manifest `adapter_id`.
4. **PID/PGID plus anti-reuse identity** — detect PID reuse; never signal on identity mismatch.
5. **Durable runner/artifact proof** — file-backed stdout/stderr metadata in manifest.
6. **Broker recovery lease** — single writer / fencing token (`durable_recovery_leases` table).
7. **Sanitized audit evidence** — no credentials, raw argv, or secrets in DB events, reconcile responses, or collected broker-facing summaries.

Missing or conflicting proof → remain at `observe_and_cancel` or `none`; never optimistic upgrade.

## 6. Compatibility evidence (no-model-call)

Unchanged from 7C.1 — help keywords alone **must not** elevate conclusions.
Production subprocess CLIs remain `observe_and_cancel`. Only `fixture_durable`
(test runtime) declares `collect_after_restart` after 7C.3 proof tests.

## 7. Phased roadmap

| Phase | Deliverable |
|---|---|
| **7C.1** | Typed contract, discovery/doctor/MCP visibility, compatibility evidence model, RFC/matrix |
| **7C.2** | Durable subprocess runner (`durable_runner.py`), bounded owner-private artifacts, read-only `inspect_durable_launch()` |
| **7C.3** | `durable_reconciliation.py`, recovery lease, broker adoption in `reconcile()`/`collect()`/`cancel()`/`status`, structured reconcile diagnostics |
| **7C.4** (this PR) | Operator surfaces: CLI/MCP `control` with explicit actions, recovery/control view, refusal codes |

### 7.1 What 7C.3 proves

`recollect_lines.durable_reconciliation` wires broker restart reconciliation for
**eligible durable subprocess launches only**:

1. **Recovery lease** — atomic SQLite lease binding `task_id`, `durable_launch_id`,
   `broker_id`, `broker_epoch`, and `expires_at`; competing reconcilers are refused.
2. **Proof-gated `reconcile()`** — reads manifest safely; verifies task/launch/adapter
   binding; verifies PID **and** start-identity; refuses on corrupt/traversal/mismatch;
   adopts only when proof succeeds.
3. **Adopted handle truth** — `status`, owned-group `cancel`, terminal `collect`
   only; no redispatch, no stdin/PTY, no provider session resume, no `message`.
4. **Legacy fail-closed** — direct API, legacy subprocess CLIs, and mock tasks
   retain `recovery_required` behavior without adoption.
5. **Redacted diagnostics** — `reconcile` / MCP reconciliation output includes
   `outcome`, `reason`, and `remediation` without command text, env values, or
   raw sensitive output.

**Test runtime:** `FixtureDurableAdapter` (`fixture_durable` profile) is the only
runtime elevated to `collect_after_restart`. Production CLI adapters are unchanged.

### 7.2 What 7C.4 adds

`recollect_lines.operator_control` and broker `operator_control()` expose a
machine-readable recovery/control view for operators and automation:

1. **Explicit actions only** — `status`, `cancel`, `collect`, `message`; no
   dangerous defaults; `message` is always an explicit unsupported refusal.
2. **Recovery/control view** — task/launch identity, `recovery_posture`
   (`observed`, `recovery_required`, `safely_adopted`, `terminal`, `refused`),
   `permitted_actions`, per-action refusal reasons, and hard distinction:
   `process_recovery != provider_session_resume != continuation_task !=
   free_form_steering`.
3. **Fail-closed execution** — post-restart `cancel`/`collect` on durable
   launches require 7C.3 proof-gated adoption; legacy/direct paths retain
   Phase 5B/6C behavior; corrupt/contested evidence refuses control.
4. **Secret-safe output** — redacted diagnostics in CLI/MCP-visible responses.

**Interfaces:** `recollect-lines control <task_id> --action <action>` (exit 3 on
refused action); MCP tool `control` with the same contract.

### 7.3 What remains future work

- Optional elevation of production CLI adapters only after per-runtime safety proof.

## 8. Interfaces

- `recollect.message` / `control --action message` — remains structured
  `unsupported`; no side effects.
- `recollect-lines control` / MCP `control` — explicit operator recovery/control
  with secret-safe view; refuses when 7C.3 gates are not satisfied.
- `Broker.reconcile()` / `reconcile_pending()` — durable adoption path for eligible
  launches; legacy paths unchanged; returns structured `reconciliation` metadata
  via events / `reconcile_detail()`.
- `recollect-lines reconcile` / MCP `reconcile` — include `reconciliation` object
  with adoption/refusal outcome (no secrets).
- Production subprocess dispatch — still uses Phase 5B in-memory `ProcessHandle` path.

## 9. Known limitations (7C.1–7C.4)

- Compatibility evidence is host-local; not a universal compatibility promise.
- `collect_after_restart` is declared only on `fixture_durable` (test runtime).
- Production subprocess CLIs remain `observe_and_cancel`; PGID-only reconciliation
  retains accepted PID-reuse residual risk (phase-5b.md).
- Owner-private stdout/stderr under `durable_launches/` are not redacted at rest;
  broker-facing collect summaries and reconcile diagnostics are redacted.
- `session_resume` and `message` remain unavailable.
- Durable adoption does not replace provider-native session resume.
