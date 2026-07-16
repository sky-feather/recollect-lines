# Phase 5 — Contract, CI, and the road to a lifecycle/verification gate

## Phase 5A (this PR)

Scope: documentation and CI only, no runtime/product behavior change.

- [`../../design/PRD.md`](../../design/PRD.md): canonical, provider-neutral product requirements.
- [`../../design/RFC-001.md`](../../design/RFC-001.md): the current implementation RFC, consolidating
  Phase 1–4 evidence and known limitations.
- `.github/workflows/ci.yml`: automated enforcement of the test suite,
  compileall, and whitespace hygiene already used as manual quality evidence
  in Phase 1–4.

This is a plan for what comes next, not an assertion that 5B/5C are
implemented. Both remain open work.

## Phase 5B (this PR) — Lifecycle recovery / idempotent collection

Problem: a broker restart mid-task loses the in-memory process handle for a
running OpenCode task (RFC-001 §8). The Phase 2–5A mitigation was fail-safe
cleanup only (never delete a possibly-live process's worktree/lease, but
otherwise fail immediately on any lost handle); there was no durable launch
identity to reconcile against, and a second `collect()` call on an
already-terminal task raised `InvalidTransition` rather than idempotently
returning the prior result (phase-4.md known limitations).

Implemented in this PR — see [phase-5b.md](phase-5b.md) for the full design,
state table, and operator procedure:

- Durable launch identity (`store.runtime_launches`), persisted the moment an
  adapter subprocess actually exists.
- Explicit reconciliation (`Broker.reconcile()` / `reconcile_pending()`, CLI
  `reconcile`/`reconcile-all`, MCP `reconcile`): a fresh `Broker` instance can
  inspect and act on a durable launch record with no in-memory
  `ProcessHandle`, reaching a truthful `failed` when the process group is
  confirmed dead, or an explicit non-terminal `recovery_required` state when
  it's still alive or liveness can't be confirmed — never a fabricated
  success.
- Idempotent `collect()`: a second call on an already-terminal task returns
  the stored result with no re-transition, no duplicate cleanup, and (at the
  MCP layer) no re-run verification.
- Safer `cancel()`: an OpenCode task with a lost handle is no longer treated
  like a mock task; it's reconciled against its durable launch record first,
  and a confirmed-alive process group can be cancelled directly via its
  persisted pgid.

Explicitly not in scope (see phase-5b.md "What this is not"): transparent
re-attachment to a still-running OpenCode process's output stream. That
remains impossible for the same reason Phase 2/3 named it out of scope — a
new OS process cannot regain a `Popen`/child relationship with an orphaned
subprocess.

## Phase 5C (this PR) — Verification gate, timeout liveness safety, and generic MCP-host acceptance

Problem: verification was caller-invoked, not enforceable (PRD §9,
RFC-001 §9), and `Broker.timeout()` finalized (and could delete) a
workspace without ever checking whether the process group it was timing
out was actually still alive ([phase-5b.md](phase-5b.md), "Non-goals
carried forward").

Implemented in this PR — see [phase-5c.md](phase-5c.md) for the full
design, decision tables, and test evidence:

- An opt-in, per-task `verification_policy` (`none` / `advisory` /
  `required`) folded into the real lifecycle (`complete()`/`collect()`),
  not just the pre-existing caller-invoked `verify()`.
- `Broker.timeout()` now classifies process-group liveness — the same
  restart-safety classification `reconcile()`/`cancel()` already used —
  before finalizing a workspace, closing the gap named in phase-5b.md.
- A generic MCP-host acceptance harness (`scripts/mcp_acceptance.py`): a
  real stdio JSON-RPC client driving a real `recollect-mcp` subprocess
  against a disposable local Git fixture, proving the delegate → observe
  → collect → cancel lifecycle end to end using the standard MCP stdio
  protocol any compliant host could speak.

**Documentation correction:** this section previously described the
acceptance work above as "driven by Hermes." That was an error — Recollect
Lines is provider- and host-neutral (see [PRD.md](../../design/PRD.md) §1, §3.1);
Hermes is one possible operator environment among many, never a required
dependency. The harness this phase actually delivers assumes no specific
host and requires no Hermes installation to run; see
[README.md](../../README.md) for a generic MCP client configuration example
alongside one clearly labeled, optional Hermes example.

This phase does **not** close the codebase's largest remaining gap against
the original product PRD: at least two heterogeneous runtime adapters
(Claude Code CLI and Codex CLI are the PRD's preferred initial pair). Only
one experimental adapter (OpenCode) is implemented. See
[PRD.md](../../design/PRD.md) §9 and [RFC-001.md](../../design/RFC-001.md) §8 for the full, honest
capability accounting — that gap remained open and unscheduled at the end
of this PR's original scope; see Phase 6 below for the roadmap decision
made after.

## Phase 6 — Adapter and provider expansion (6A–6D implemented)

A post-Phase-5C roadmap decision sequenced the next phases:

- **Phase 6A** — Claude Code CLI adapter. **Implemented** — see
  [phase-6a.md](phase-6a.md) for the full compatibility-spike evidence,
  `ClaudeCodeAdapter` design, permission-mode mapping, real bounded smoke,
  and test evidence. `service.py`'s adapter dispatch was generalized to a
  profile-keyed `subprocess_adapters` lookup so this required no
  Claude-specific branching in broker core.
- **Phase 6B** — Codex CLI adapter. **Implemented** — see [phase-6b.md](phase-6b.md).
- **Phase 6B.5** — Cursor CLI adapter (a real runtime adapter, not an
  OpenAI-compatible-provider alias). **Implemented** — see [phase-6b5.md](phase-6b5.md).
- **Phase 6C** — configurable, plural OpenAI-compatible provider fabric
  (named entries such as DeepSeek, Qwen, or a local endpoint) and a
  capability-limited direct-API runtime foundation. **Implemented** — see
  [phase-6c.md](phase-6c.md).
- **Phase 6D** — runtime/provider capability discovery, policy-aware
  routing, and bounded parent-directed model-council usage patterns.
  **Implemented** — see [phase-6d.md](phase-6d.md).

Recollect Lines remains provider- and host-neutral throughout: a runtime
adapter (supervises a concrete CLI) and a provider configuration (names a
model endpoint and its declared capabilities) stay distinct concepts, and
no phase above is a hard-coded vendor branch. See
[RFC-001.md](../../design/RFC-001.md) §10 for the full design constraints and
[PRD.md](../../design/PRD.md) §9 for the product-level framing.
