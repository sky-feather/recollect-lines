# Product Requirements Document — Recollect Lines

Status: canonical, living document. This PRD defines the product contract.
It is provider/runtime neutral: no runtime, language, protocol, or storage
choice named here is a permanent requirement. Where the current
implementation makes a specific choice (OpenCode, Python, MCP over stdio,
SQLite), that is documented as an implementation decision in
[RFC-001](RFC-001.md), not as a product requirement.

## 1. Problem statement

An agent ("parent agent") working on a task often needs to delegate a
bounded, well-scoped piece of work — investigate a failing test, draft a
patch, summarize a subsystem — to a separate agent process, without losing
track of what happened, without polluting its own context with the
delegate's full transcript, and without risking the delegate's actions
corrupting the parent's own workspace.

Today that delegation is ad hoc: manual copy-paste, informally supervised
subprocesses, or trusting a sub-agent's self-report at face value. There is
no durable record of what was asked, what ran, what changed, or whether
claimed results are actually true.

## 2. Target users

- **Parent agents** — the primary caller. An LLM-driven coding agent (or an
  operator's tooling standing in for one) that wants to delegate bounded work
  and get back a concise, trustworthy result instead of a raw transcript.
- **Operators** — humans running or configuring the parent agent's
  environment, who need delegated work to be safe by default (no source
  workspace corruption, no secret leakage) and inspectable after the fact.

Out of scope as a user: end users of a downstream product. This is
infrastructure for agents and the people operating them, not a
consumer-facing tool.

## 3. Product / job-to-be-done

When a parent agent has a bounded task that doesn't need to happen in its
own context, it should be able to:

1. **Delegate** the task with a clear instruction, target workspace, and
   bounds (timeout, isolation mode) — and get back a stable handle, not a
   blocking call.
2. **Observe** the task's status and event history at any time without
   affecting it.
3. **Collect** a concise result: a summary plus evidence, not a raw
   transcript — and know whether that evidence is the delegate's own claim
   or something independently checked.
4. **Cancel** the task and know, factually, whether the underlying work
   actually stopped.
5. Optionally let the task **write to a workspace** without risking the
   parent's own checkout — isolated by default, never touching the source
   until the parent asks for the result.

This is "recollect the signal": the parent gets back durable, evidence-backed
results, not a firehose of agent output it has to babysit or re-parse.

### 3.1 Delegation shape: dynamic, not fixed

The five flows above compose into whatever task graph a parent agent
decides it needs, created and extended at runtime — not a single
predefined pipeline. A requirements-to-implementation sequence is one
valid usage pattern; so is running several independent investigations in
parallel, having one delegate critique another delegate's plan (a
bounded "model council" comparison — the parent decides when the
comparison is sufficient; the broker never runs an unlimited or automatic
debate loop on its own, and this stays a usage pattern the parent directs,
never a provider-specific feature; see [RFC-001](RFC-001.md) §10.5 for the
planned, not-yet-implemented discovery/routing work this pattern will
eventually build on), or delegating narrow, unrelated lookups on an ad
hoc basis. The broker enforces deterministic bounds (timeout, concurrency,
one writer per workspace) on whatever graph the parent constructs; it does
not prescribe the graph's shape, and it is not a requirements-to-PR
workflow engine.

The broker is also meant to be reusable across more than one parent-agent
host and more than one delegate runtime: nothing in its interface (CLI or
MCP) or its policy layer is specific to one calling host or one runtime
adapter. See [RFC-001](RFC-001.md) §1 for the adapter boundary that is
meant to keep adding a runtime from requiring broker changes, and §8/§9
for the current, honest gap between that design goal and how many
adapters are actually implemented today.

## 4. Goals and MVP success criteria

Goals:

- A parent agent can delegate bounded work and later retrieve a truthful,
  evidence-backed result without polling a live process itself.
- Delegated work that writes files never corrupts or blocks the parent's own
  workspace.
- Every claim of "this passed" is traceable to either the delegate's own
  report or an independently broker-run check — and the two are never
  conflated.
- The system runs entirely on the operator's own machine, with no required
  external service.

Measurable MVP success criteria:

- **Durability**: a task's state, event history, and artifacts survive a
  restart of the broker process (verified by test, not just by design).
- **Truthful verification**: any success result distinguishes
  runtime-reported evidence from broker-verified evidence in its returned
  data, with no code path that upgrades the former into the latter.
- **Workspace safety**: for an isolated task, the source workspace's working
  tree is provably unchanged (byte-identical before/after) whether the task
  succeeds, fails, times out, or is cancelled.
- **Cancellation truthfulness**: cancelling a task reports whether the
  underlying work was actually confirmed stopped, not merely "a signal was
  sent."
- **Bounded execution**: every task has an enforced timeout; no task can run
  unbounded by default.
- **Reviewable interfaces**: both the parent-facing interface (currently
  MCP) and a local operator-facing interface (currently a CLI) expose the
  same lifecycle without duplicating policy logic.

## 5. Core user flows

### 5.1 Delegate

Parent agent submits a task description, a target workspace, an execution
mode (read-only or workspace-writable), and a bound (timeout). The system
validates the request against policy (allowed modes, timeout ceiling,
concurrency limits) and either accepts it (returning a stable task handle)
or rejects it with a specific, machine-readable reason. Delegation is
non-blocking: the caller gets a handle back immediately, not a completed
result.

### 5.2 Observe / status

At any point, the parent (or an operator) can retrieve a task's current
state, its full append-only event history, and its artifact manifest. This
never mutates the task and never requires the task to be finished.

### 5.3 Collect evidence

Once a task reaches a terminal or collectible state, the parent retrieves
its result: a concise summary plus structured evidence. The result always
distinguishes what the delegate itself reported from what was independently
checked by the system running real commands and observing their raw
output/exit codes. A result is never fabricated on the caller's behalf —
absence of real evidence is reported as absence, not papered over.

### 5.4 Cancel

The parent can request cancellation of a running task. The system attempts
to stop the underlying work, observes whether it actually stopped (not just
whether a stop signal was sent), and reports that observation as the
outcome. A task that could not be confirmed stopped is reported as such
rather than being marked cleanly cancelled.

### 5.5 Workspace-safe writable task

When a task is allowed to write to a workspace, the system isolates that
writing from the parent's actual source checkout by default — the source is
never mutated by a delegated task, regardless of how that task ends
(success, failure, timeout, cancellation). The parent can later retrieve
what changed (a diff/status artifact) without the isolated copy being
merged back automatically. Concurrent writable tasks against the same
source are serialized or rejected, never silently interleaved.

### 5.6 Parent-directed side agents and bounded trees

Beyond single tasks, a parent host may construct a **bounded task tree** at runtime:

- Delegate arbitrary bounded work to one or more side agents, optionally grouped by `external_root_id` for host-side audit (without inventing a fake broker parent for grouping alone).
- Choose **runtime**, **model**, and **behavioral agent profile** independently per child; request a **result schema** for normalized collection.
- Continue host-side work while children run; poll durable **completion events** by cursor; collect concise provenance-aware results while raw evidence remains separately inspectable.
- When in-flight steering is unsupported, create an explicit **`continues`** follow-up task rather than resuming a session.

The broker enforces isolation, evidence, timeouts, and concurrency — not workflow topology. Fixture proof: [../demos/side-agent-fixture-evidence.json](../demos/side-agent-fixture-evidence.json). Opt-in live two-runtime dogfood: [../demos/live-two-runtime-dogfood-runbook.md](../demos/live-two-runtime-dogfood-runbook.md).

## 6. Functional requirements

- **Durable tasks and events**: every task has a stable identifier, a
  validated state machine, and an append-only event log, all surviving
  process restarts.
- **Artifacts**: task-scoped output (requests, results, logs, diffs,
  verification output) is stored under a task-specific location with an
  integrity manifest (at minimum, size and content hash per file).
- **Truthful verification**: the system must be able to distinguish, in its
  returned data, between a delegate's self-reported outcome and an outcome
  the system itself independently executed and observed. Neither is ever
  silently promoted into the other.
- **Worktree/workspace isolation**: a workspace-writable task must not be
  able to mutate the parent-visible source workspace directly; isolation
  must hold across every task outcome, including crashes and restarts of
  the broker itself, without leaking or double-allocating a writable
  isolation slot for the same source.
- **Interfaces**: the system must expose its lifecycle (delegate, status,
  collect, cancel) both to a parent agent programmatically and to a human
  operator locally. The specific protocol and CLI shape are implementation
  choices (RFC-001), not requirements — but duplication of policy logic
  across interfaces is disallowed; interfaces are thin front doors onto one
  lifecycle implementation.

## 7. Nonfunctional requirements

- **Local-first**: the system must be fully operable on a single operator
  machine, with no required external network service, hosted database, or
  third-party account. Any given runtime adapter may itself require network
  access (e.g. to reach a model provider) — that is a property of the
  adapter, not of the broker.
- **Evidence-first**: every user-visible claim about a task's outcome must
  be traceable to a stored artifact or event, not held only in an
  in-process variable that disappears on restart.
- **Secret and output hygiene**: the system must not read, store, log, or
  echo credentials/tokens on the caller's behalf, and must not expose any
  filesystem path or artifact outside a task's own declared workspace and
  artifact directory.
- **Bounded execution**: every task must have an enforced upper bound on
  runtime; there is no supported "run forever" mode.
- **No shell injection surface**: any command the system itself executes on
  the caller's behalf (e.g. verification commands) must be an explicit
  argument list, never a caller-supplied shell string.

## 8. Explicitly out of scope

The following are deliberate non-goals for the product as currently
scoped, not oversights:

- **Remote workers** — all delegated execution happens as a local process
  under the broker's direct supervision; no distributed/remote execution
  fabric.
- **Multi-user authorization** — single local operator/parent agent per
  broker instance; no user accounts, roles, or permission model.
- **Web UI** — no browser-based dashboard or control surface.
- **Daemon / HTTP service** — no long-running network listener; interfaces
  are local (stdio/CLI), invoked per-call.
- **Provider lock-in as a requirement** — the product must not be defined in
  terms of one specific model runtime, language runtime, or storage engine;
  today's choices are implementation, not contract (see RFC-001).
- **A fixed requirements-to-PR pipeline as the only supported shape** —
  see §3.1; dynamic, runtime-constructed task graphs are the default
  assumption, not an extension bolted onto a rigid workflow.
- **Live mid-task steering** — no requirement that a parent be able to send
  a delegate agent additional input after it has started; a delegate runs to
  completion, timeout, or cancellation.

## 9. Risks and open questions

- **Runtime process durability**: resolved for Phase 5B as "safely fail
  closed on restart," not durable re-attachment — a fresh broker instance
  reconciles a durable launch record against the process group's actual
  liveness, reaching a truthful `failed` when it's confirmed dead or an
  explicit `recovery_required` state when it isn't, never a fabricated
  success (see [RFC-001](RFC-001.md) §8 and [phase-5b.md](../history/phases/phase-5b.md)).
  Whether *transparent* re-attachment is ever required for MVP remains open;
  nothing in Phase 5B attempts it.
- **Verification is opt-in per task, by design**: Phase 5C added a
  `verification_policy` lifecycle option (`none`/`advisory`/`required`) a
  caller sets per task; the broker never silently applies a stricter
  policy than the caller asked for. Nothing globally forces every
  workspace-writable task through `required` — that remains a caller/host
  policy decision, not a broker-enforced default. See
  [phase-5c.md](../history/phases/phase-5c.md).
- **Heterogeneous adapter coverage was the largest MVP gap; Phase 6A closes
  it to "at least two"**: the product's MVP boundary calls for at least two
  heterogeneous runtime adapters, with Claude Code CLI and Codex CLI named
  as the preferred initial pair. Phase 6A implemented `ClaudeCodeAdapter`
  (supervising the real `claude` CLI in `-p` mode), so this codebase now has
  two adapters — OpenCode and Claude Code, both still marked experimental
  (RFC-001 §2, §8, [phase-6a.md](../history/phases/phase-6a.md)). Truthful-verification and
  cancellation evidence has been exercised against both real runtimes;
  confidence generalizing further (a third, differently-shaped runtime) is
  still not established, and neither adapter has continuous re-verification
  against upstream CLI releases. A post-Phase-5C roadmap decision sequenced
  the remaining gap as Phase 6B (Codex CLI adapter) and Phase 6B.5 (Cursor
  CLI adapter) — see [RFC-001](RFC-001.md) §10 for the full sequence and
  design constraints. Phase 6B implemented `CodexAdapter` (supervising the real
  `codex exec` CLI); Phase 6B.5 implemented `CursorAdapter` (supervising the real
  `cursor-agent --print` CLI) — see [phase-6b5.md](../history/phases/phase-6b5.md). Phase 6C
  implemented the plural OpenAI-compatible provider configuration layer and a
  capability-limited direct HTTP runtime (`openai_compatible` profile) —
  see [phase-6c.md](../history/phases/phase-6c.md). Phase 6D implemented capability discovery,
  parent-directed selection, and bounded council graphs — see
  [phase-6d.md](../history/phases/phase-6d.md) and [RFC-001](RFC-001.md) §10.5.
- **Plural model-provider support is a distinct, separately scheduled
  gap**: today nothing in this codebase talks to a model provider
  directly — adapters supervise a CLI, which itself owns provider/auth
  concerns. A configurable, plural OpenAI-compatible provider
  configuration layer (named entries such as DeepSeek, Qwen, or a local
  endpoint) is scheduled as Phase 6C, with capability discovery,
  policy-aware routing, and bounded parent-directed model-council usage
  patterns (§3.1) scheduled as Phase 6D — see [RFC-001](RFC-001.md) §10.
  Phase 6C implemented named provider configuration and the direct
  `openai_compatible` runtime ([phase-6c.md](../history/phases/phase-6c.md)). Phase 6D implemented
  discovery, policy-aware routing, and bounded councils ([phase-6d.md](../history/phases/phase-6d.md)).

## 10. Acceptance checklist

- [ ] A parent agent can delegate, observe, collect, and cancel a task using
      only the documented interfaces, with no direct access to internal
      storage.
- [ ] A workspace-writable task's source workspace is unchanged after every
      possible task outcome (success, failure, timeout, cancellation).
- [ ] A collected result clearly labels each piece of evidence as
      runtime-reported or broker-verified.
- [ ] A cancelled task's report reflects an observed outcome, not merely a
      sent signal.
- [ ] All of the above hold after a restart of the broker process between
      steps, for at least the durable (non-in-memory) parts of task state.
- [ ] No required external network service exists for the core lifecycle.
- [x] At least two heterogeneous runtime adapters exist and can run
      concurrently under the same broker (**met** — OpenCode, Claude Code, Codex,
      and Cursor are implemented and dispatch through the same generic broker
      lifecycle; all remain marked experimental; see §9, [phase-6a.md](../history/phases/phase-6a.md),
      [phase-6b.md](../history/phases/phase-6b.md), and [phase-6b5.md](../history/phases/phase-6b5.md)).

## 11. Terminology

- **Parent agent**: the caller delegating work; consumer of the product.
- **Broker**: the local component owning task lifecycle, persistence, and
  policy enforcement (implementation name; see RFC-001).
- **Adapter** (runtime adapter): the component translating the broker's
  generic "run this task" into a specific runtime's invocation, including
  its process/session lifecycle, output parsing, and cancellation
  semantics — e.g. OpenCode, Claude Code, Codex, or Cursor (all implemented), or a
  future runtime sequenced in [RFC-001](RFC-001.md) §10. An adapter
  is what actually supervises a coding-agent runtime.
- **Provider configuration**: a named, configured description of a model
  endpoint — base URL, credentials reference, model aliases, and declared
  API capabilities — as distinct from an adapter. A provider configuration
  entry (e.g. a DeepSeek or Qwen endpoint per [RFC-001](RFC-001.md) §10)
  does not by itself grant agent tools, workspace/worktree access,
  cancellation, or streaming; those must be separately and explicitly
  declared by whatever runtime speaks to that endpoint. Provider selection
  is orthogonal to adapter/runtime selection.
- **Runtime-reported evidence**: a claim (e.g. "tests passed") made by the
  delegate/runtime itself, taken at face value and labeled as such — never
  silently treated as independently confirmed.
- **Broker-verified evidence**: a claim backed by a command the broker
  itself executed and whose raw stdout/stderr/exit code it directly
  observed. Only this evidence is labeled verified.
- **Workspace isolation**: running a writable task against a private copy
  of a source workspace such that the source is never mutated directly by
  the task.
