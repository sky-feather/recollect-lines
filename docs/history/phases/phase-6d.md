# Phase 6D — Capability discovery, policy-aware routing, bounded councils

## Scope

Phase 6D exposes **factual** runtime/provider capability and availability
inventory to hosts, adds **parent-directed** deterministic candidate filtering,
and executes **bounded** parent-specified council task graphs through existing
broker lifecycle primitives. The broker records evidence and enforces policy; it
does **not** choose a winner, merge outputs autonomously, or schedule recursive
councils.

Subprocess CLI adapters (`opencode`, `claude_code`, `codex`, `cursor`) and the
`openai_compatible` direct HTTP runtime remain distinct — discovery does not
erase that boundary.

## Capability discovery

`Broker.discover_capabilities()` (CLI `recollect-lines discover`, MCP
`discover_capabilities`) returns:

- **`runtimes`** — every registered profile (`mock`, subprocess adapters,
  `openai_compatible`) with:
  - `kind`: `synthetic` | `subprocess_cli` | `direct_api`
  - `runtime_label` (subprocess entries) — descriptive string resolved from the
    registered adapter instance at discovery time, or a static descriptor label
    for fixtures; never a raw Python class attribute or property descriptor
  - `execution_modes`, profile limits (`max_timeout_seconds`, `max_concurrency`)
  - **`declared_capabilities`** — broker-known facts (e.g. `isolated_worktree`,
    `process_group_cancellation`, `synthetic_runtime` for mock)
  - **`observed_availability`** — side-effect-free probes (`--version` for CLIs,
    credential resolution for providers, `providers_not_configured` when no
    `--providers-config` is loaded)
  - **`limitations`** — honest non-claims (no reattachment, no steering, etc.)
- **`providers`** — named entries from `--providers-config` with declared
  capabilities, `credential_reference` (env var name only), `default_model`,
  `endpoint_summary` (`scheme`, `host_class` — **no raw URL**), and observed
  availability (missing/empty secrets → `missing_credential_reference`).

Observed availability is **broker-probed evidence**, not a runtime self-report.

## Policy-aware selection

`Broker.select_candidates(...)` (CLI `recollect-lines select`, MCP `select_candidates`)
accepts parent requirements:

- `execution_mode`
- optional `allowed_runtimes` / `allowed_providers`
- optional `required_runtime_capabilities` / `required_provider_capabilities`
- `require_available` (default `true`)

Returns **`eligible_runtimes`**, **`eligible_providers`**, and **`excluded`**
entries with explicit reasons. Fails closed when no candidate meets declared
conditions. **Does not rank or pick a winner.**

## Bounded council graphs

Parents supply a JSON plan (CLI `recollect-lines council validate|execute`, MCP
`council_validate` / `council_execute`):

```json
{
  "workspace": "/path/to/repo",
  "execution_mode": "read_only",
  "acceptance_criteria": "Human/parent judges evidence — broker records only.",
  "bounds": {
    "max_rounds": 1,
    "max_concurrency": 2,
    "time_budget_seconds": 300,
    "cost_budget_usd": 1.0
  },
  "forbid_self_critique": true,
  "stages": [
    {"id": "plan_a", "role": "plan", "profile": "mock", "task": "Plan A"},
    {"id": "plan_b", "role": "plan", "profile": "mock", "task": "Plan B"},
    {
      "id": "critique_b",
      "role": "critique",
      "profile": "opencode",
      "task": "Critique plan B",
      "depends_on": ["plan_b"]
    }
  ]
}
```

Validation (fail closed):

- positive `max_rounds`, `max_concurrency`, `time_budget_seconds` within caps;
- when `cost_budget_usd` is set, every stage must have a configured
  `estimated_cost_usd_upper_bound` on its provider (direct API only today) and
  the summed upper bound must not exceed the budget;
- acyclic `depends_on`; unique stage ids; no self-critique when
  `forbid_self_critique` (same profile **and** provider as upstream);
- each stage candidate must pass `select_candidates` availability checks;
- `openai_compatible` stages require `provider`.

Execution:

- topological waves; batches respect `max_concurrency`;
- `time_budget_seconds` stops further dispatch with `time_budget_exhausted`
  skips;
- `cost_budget_usd` is **enforced or rejected before dispatch** — never
  recorded while allowing unmeasured execution:
  - **unset** → `cost_enforcement: not_configured` (no cost gate);
  - **set with configured estimates** → each stage's `openai_compatible`
    provider may declare `estimated_cost_usd_upper_bound` in
    `--providers-config`; the broker sums configured upper bounds and rejects
    when the total exceeds `cost_budget_usd` (`pre_dispatch_upper_bound`);
  - **set without estimates for every stage** → fail closed with
    `rejected_unmeasurable` (CLI/mock/subprocess stages have no broker-known
    cost unless explicitly configured on the provider entry). The broker does
    **not** invent token counts, vendor pricing, or runtime-reported spend;
- rejection is structured, redacted, and written to `council_evidence.json`
  before any stage task is created;
- stages use `create` → `start` → `complete` (mock) or `collect` (subprocess /
  direct API);
- durable `council_evidence.json` under `artifacts/<council_id>/`;
- **no winner**, **no recursive council**, **no mid-task steering**.

## Distinctions (required reading)

| Concept | Phase 6D behavior |
|---|---|
| Declared vs observed | Declared = config/profile facts; observed = broker probes |
| Selected vs eligible | Selection returns eligible sets + exclusions only |
| Broker evidence vs runtime self-report | Council artifacts cite task `result.json` / terminal states |
| Council limitations | No autonomous synthesis; parent applies `acceptance_criteria` |

## Integration surfaces

| Surface | Commands / tools |
|---|---|
| CLI | `discover`, `select`, `council validate`, `council execute` |
| MCP | `discover_capabilities`, `select_candidates`, `council_validate`, `council_execute` |

Existing delegate/status/collect tools are unchanged.

## Test evidence

| Area | Module |
|---|---|
| Discovery, redaction, selection, fail-closed | `tests/test_phase_6d.py` |
| Council validation (cycles, self-critique, bounds, cost budget) | `tests/test_phase_6d.py` |
| Council execution, time budget, evidence artifact | `tests/test_phase_6d.py` |
| Subprocess lifecycle via fake opencode | `tests/test_phase_6d.py` |
| MCP tool registration | `tests/test_phase_6d.py`, `tests/test_mcp_server.py` |

Commands (local):

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/mcp_acceptance.py
python3 -m compileall -q src tests scripts
git diff --check $(git hash-object -t tree /dev/null)
```

No real provider/CLI council smoke was run — no non-secret safe remote endpoint
was preconfigured in this environment. Deterministic fixtures are the acceptance
standard.

## Non-goals (unchanged)

- Phase 6E persistent orchestration engine
- Opaque scoring or autonomous model/candidate choice
- Recursive/unbounded council loops
- Durable reattachment or mid-task steering (still unsupported)
- Direct-API worktree/tool claims
- Production deploy
