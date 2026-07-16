# Demos

Recorded end-to-end proofs that Recollect Lines delegates to runtime backends and returns evidence-backed results.

## Integrated side-agent fixture proof (offline, CI)

**Script:** `scripts/side_agent_fixture_acceptance.py`

**Command:**

```bash
PYTHONPATH=src python3 scripts/side_agent_fixture_acceptance.py
```

**Evidence:** [side-agent-fixture-evidence.json](side-agent-fixture-evidence.json)

### What the fixture proof demonstrates

1. A parent host delegates arbitrary bounded work under a shared `external_root_id` without inventing a fake broker parent for grouping.
2. The broker runs heterogeneous side agents (fixture Codex, fixture Claude Code, mock) concurrently.
3. `runtime`, `model`, and behavioral `agent_profile` are independent per child.
4. Parent-directed bounded task trees and external host roots are supported (`task_tree`, lineage fields).
5. The broker enforces isolation, evidence, and policy — not workflow topology.
6. Hosts poll durable compact `completion_events` and collect structured normalized results.
7. Raw runtime evidence remains separately inspectable (`events.jsonl`, `runtime_raw_output.txt`, manifests).
8. Unsupported in-flight steering becomes an explicit `continues` follow-up task; writer isolation still applies alongside read-only children.

### What it does not prove

- Live Codex, Claude Code, Cursor, OpenCode, or HTTP provider execution (no quota consumed).
- Certification of every installed provider build.
- Push notifications, session resume, or broker-verified runtime self-claims.

## Live two-runtime dogfood (opt-in only, not CI)

**Runbook:** [live-two-runtime-dogfood-runbook.md](live-two-runtime-dogfood-runbook.md)

Documents a future operator-initiated pass: live Codex `repository-investigator` plus Claude Code `architecture-reviewer` under one `external_root_id`, with cursor polling and a `continues` follow-up. **Not performed or evidenced by this repository milestone.**

## Codex marker identification (live)

**Script:** `scripts/run_codex_demo.py`

**Default:** dry-run plan only (no provider call).

**Live run:**

```bash
python3 scripts/run_codex_demo.py --execute-live --acknowledge-provider-call
```

**Evidence:** [codex-marker-evidence.json](codex-marker-evidence.json)

**Cancellation (offline, no provider call):**

```bash
python3 scripts/run_codex_demo.py --demo-cancel-fixture
```

### What the live Codex demo proves

- A parent-style MCP client can `delegate` bounded read-only work to the real **Codex CLI** through `recollect-mcp`
- The broker supervises the subprocess, records lifecycle transitions, and `collect` returns a runtime-reported summary (`alpha.txt` for the MARKER_ALPHA fixture)
- Artifacts (events JSONL, manifests) are stored under the broker home

### What it does not prove

- Separate one-shot `recollect-lines start` / `collect` shell invocations (process handle is per broker instance)
- Post-restart session resume
- USD cost accounting (Codex subscription quota path does not expose a dollar amount to this harness)

See [../user-flows.md](../user-flows.md) for role boundaries.
