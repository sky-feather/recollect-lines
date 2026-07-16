# Migration: runtime vs profile

## Summary

Recollect Lines previously overloaded `profile` as the execution-backend selector
(`codex`, `claude_code`, `cursor`, …). Side-agent semantics split that meaning:

| Field | Meaning |
|-------|---------|
| `runtime` | Execution backend identifier |
| `agent_profile` | Optional behavioral agent profile name (resolves prompt prefix and defaults at create; composed at launch) |
| `model` | Optional requested model identifier (passed to adapters when the runtime's `model_selection` supports it) |
| `effective_model` | Model resolved at launch (adapter/provider default or task override); persisted after `start()` |
| `result_schema` | Optional requested normalized result schema (`plain-summary`, `evidence-report`, `review-findings`, `implementation-report`); unknown values fail at create |

The SQLite `profile` column remains as a compatibility bridge (`profile = runtime`).

## Callers

### Preferred (new)

```json
{
  "task": "Trace cancellation path",
  "workspace": "/path/to/repo",
  "runtime": "codex",
  "agent_profile": "architecture-reviewer"
}
```

CLI equivalent:

```bash
recollect-lines create --task "..." --workspace "$PWD" --runtime codex --agent-profile architecture-reviewer
```

### Deprecated (legacy)

```json
{
  "task": "Trace cancellation path",
  "workspace": "/path/to/repo",
  "profile": "codex"
}
```

Rules:

- `profile` is accepted **only** when its value is a known runtime identifier.
- Unknown values (for example `architecture-reviewer`) are rejected with guidance to use `agent_profile` plus an explicit `runtime`.
- If both `runtime` and `profile` are supplied and differ, the request fails with a stable conflict error.
- If both agree, the request is accepted and carries deprecation metadata (see below).
- `agent_profile` is never inferred from legacy `profile`.

## Deprecation metadata

Translated legacy requests persist secret-safe metadata:

```json
{
  "compatibility": {
    "legacy_profile_translated": true,
    "deprecated_fields": ["profile"]
  }
}
```

This appears in `request.json` and in MCP `delegate` / `delegate_batch` summaries when translation occurred. New `runtime=` requests are not marked deprecated.

## Runtime registry (Phase 8.2)

Execution backends are registered centrally in `RuntimeRegistry` as immutable
`RuntimeDescriptor` records. Each descriptor declares:

| Field | Meaning |
|-------|---------|
| `execution_strategy` | `subprocess_cli`, `direct_api`, `synthetic`, or `fixture` |
| `model_selection` | Honest model behavior: `not_supported`, `persisted_not_invoked`, `per_task_request`, or `provider_config_default` |
| `adapter_capabilities` | Reuses `AdapterCapabilities` / recovery contracts from adapters |
| `policy` | Timeout, concurrency, and allowed execution modes |

CLI, MCP, certification, council validation, and discovery read runtime names
from the registry instead of separately maintained lists. Test-only fixture
runtimes can register into a copied registry without editing broker lifecycle
code or interface constants.

## Database

Additive migration on the existing `tasks` table:

- `runtime` — backfilled from historical `profile`
- `model`, `agent_profile`, `result_schema` — nullable; `agent_profile` resolves through built-in or configured profiles (see below)

## Behavioral agent profiles (Phase 8.4)

Built-in profiles (`repository-investigator`, `architecture-reviewer`, `implementation-worker`, `test-planner`) declare:

| Field | Meaning |
|-------|---------|
| `prompt_prefix` | Instructions prepended to task text at launch (deterministic composition) |
| `default_result_schema` | Default normalized result schema when the task omits one (must be a supported schema) |
| `default_execution_mode` | Default mode when the task omits `execution_mode` |
| `default_timeout_seconds` | Default timeout when the task omits `timeout_seconds` |
| `recommended_runtime` | Advisory hint only — never overrides an explicit `runtime` |

Resolution precedence:

```text
broker safety ceiling (policy max timeout, allowed modes)
> explicit permitted task value
> profile default
> runtime default
```

At create time the broker persists `agent_profile_resolution.json` (name, content hash, resolved fields, sources, task overrides). At launch it writes `composed_prompt.json` from that snapshot — changing profile configuration later cannot alter historic records.

Optional JSON configuration extends built-ins (`--agent-profiles-config` / broker constructor). List profiles via `discover` / `discover_capabilities` or `list-agent-profiles`.

No destructive schema replacement. Re-running migration is idempotent.

## Normalized results (MR 8.6)

Supported `result_schema` values:

| Schema | Purpose |
|--------|---------|
| `plain-summary` | Default when unset; summary text only |
| `evidence-report` | Investigation output with optional findings/evidence/commands (runtime-reported) |
| `review-findings` | Review output with findings list |
| `implementation-report` | Change summary with runtime-reported commands/tests |

Unknown schemas are rejected at task create (including profile-resolved defaults). Profile defaults follow the precedence in §Behavioral agent profiles; explicit permitted task values win.

On collect/complete the broker writes:

- `result.json` — legacy-compatible runtime result (unchanged role)
- `normalized_result.json` — versioned envelope with `runtime_reported`, `broker_observed`, and `parser` zones
- `broker_observed.artifact_manifest_ref` points at `manifest.json`; embedded `artifact_refs` list sibling artifacts only (never `normalized_result.json` itself, since a file cannot embed a stable hash of its own final bytes)
- raw runtime output remains in the adapter events artifact or `runtime_raw_output.txt` (mock path); parser references rather than duplicating it

At launch, when a structured `result_schema` is selected (profile default or explicit task override), the broker appends a versioned, provider-neutral output contract to the composed launch prompt (`composed_prompt.json`). These contracts guide CLI runtime output shape; they are prompt-level instructions only and do not enable provider-native structured-output enforcement.

Limitations: structured parsing is heuristic over runtime summary text (JSON object when present); provider-native structured output is not assumed unless the runtime adapter supplies parseable text. Model-reported `commands_executed` / tests are never broker-verified.

## Integrated fixture proof (MR 8.8)

Offline acceptance tying lineage, heterogeneous runtimes/profiles/schemas, completion-event cursor polling, normalized collection, task trees, steering refusal with `continues` follow-up, and writer isolation:

```bash
PYTHONPATH=src python3 scripts/side_agent_fixture_acceptance.py
```

Recorded evidence: [demos/side-agent-fixture-evidence.json](demos/side-agent-fixture-evidence.json). Live two-runtime dogfood is documented separately and is opt-in only: [demos/live-two-runtime-dogfood-runbook.md](demos/live-two-runtime-dogfood-runbook.md).
