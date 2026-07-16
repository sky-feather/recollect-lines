# Phase 7B — Explicit integration-certification harness

## Scope

Phase 7B adds **`recollect-lines certify`**: a safe, explicit path to produce
**truthful evidence** for operator-approved integration targets. Default
behavior is **offline dry-run** — no remote HTTP, no model CLI invocation.

This phase implements the certification **contract** and local fixture proof.
It does **not** perform external provider certification in CI or claim remote
availability from a green fixture test.

## `recollect-lines certify`

Human-readable default output; stable redacted JSON with `--json`. Optional
atomic evidence artifact with `--output /path/to/evidence.json`.

### Required explicit target selection

- `--profile <name>` is **required** (no “certify everything” default).
- `--provider <name>` is **required** when `--profile openai_compatible`.
- `--providers-config` is required for direct-API targets.

Reuses existing provider validation, redaction, doctor-style local checks, and
direct-API runtime contracts — no parallel configuration model.

### Execution modes

| Mode | Flags | Network / CLI |
|------|-------|----------------|
| **Dry-run** (default) | *(none)* | None |
| **Fixture execute** | `--fixture-execute` | Local deterministic fixture only |
| **Live execute** | `--execute-live --i-accept-billed-remote-calls --max-cost-usd <n>` | Remote HTTP (operator opt-in) |

`--execute-live` and `--fixture-execute` are mutually exclusive.

### Dry-run command (safe default)

```bash
recollect-lines --home ~/.recollect \
  --providers-config examples/litellm-openai-compatible/providers.json \
  certify --profile openai_compatible --provider local_litellm --json
```

This records declared configuration, local policy validation, and credential
**reference** presence — not observed remote availability.

### Live opt-in (operator only — billed calls possible)

```bash
# WARNING: can create billed/paid remote model API calls.
# Use only with user-approved non-production profiles and spend limits.

recollect-lines --home ~/.recollect \
  --providers-config /path/to/providers-with-cost-bound.json \
  certify --profile openai_compatible --provider local_litellm \
  --execute-live --i-accept-billed-remote-calls --max-cost-usd 0.05 \
  --output /tmp/certify-live-evidence.json
```

Live execution requires:

1. `--execute-live`
2. `--i-accept-billed-remote-calls` (wording makes billed calls explicit)
3. Positive `--max-cost-usd`
4. Provider entry with factual `estimated_cost_usd_upper_bound`
5. Operator budget ≥ provider bound

Live CLI adapter certification is **not supported** in Phase 7B (fail closed).
Direct API live uses a fixed innocuous read-only prompt; no tools, worktrees,
or council spawning.

### Deterministic local fixture certification

```bash
# Direct API against a local test fixture (tests/CI pattern — not external certification)
recollect-lines --home /tmp/recollect-certify \
  --providers-config /path/to/fixture-providers.json \
  certify --profile openai_compatible --provider local \
  --fixture-execute --output /tmp/fixture-evidence.json --json
```

Fixture evidence uses `evidence_class: local_fixture` and
`declared_not_observed_remote_availability: true`. It proves the executed path
locally; it is **not** external provider certification.

### Reading evidence statuses

| `execution.outcome` | Meaning |
|---------------------|---------|
| `dry_run` | Validation only; no remote/CLI execution |
| `blocked` | Fail-closed before or instead of execution |
| `executed` | Fixture or live path ran (see `evidence_class`) |

| `execution.evidence_class` | Meaning |
|----------------------------|---------|
| `local_dry_run` | Offline validation evidence |
| `local_fixture` | Deterministic local fixture execution |
| `live_remote` | Operator opt-in live remote execution |

Stable check `code` values (e.g. `LIVE_BUDGET_REQUIRED`, `PROVIDER_COST_BOUND_MISSING`,
`REMOTE_AVAILABILITY_NOT_CHECKED`) include remediation where applicable.

### Evidence artifact

- Written **atomically** only after the full report is assembled (no partial files on blocked validation).
- Redacted by default — safe to inspect/share; no raw secrets, prompts, or response bodies in normal output.
- Includes `config_fingerprint` (non-secret identity) and package version.

### Sample redacted JSON (truncated)

```json
{
  "certification_schema_version": "1",
  "package": {"name": "recollect-lines", "version": "0.1.0"},
  "execution": {
    "mode_requested": "dry_run",
    "outcome": "dry_run",
    "evidence_class": "local_dry_run",
    "declared_not_observed_remote_availability": true
  },
  "target": {
    "kind": "direct_api",
    "profile": "openai_compatible",
    "provider": "local_litellm"
  },
  "checks": [
    {
      "code": "REMOTE_AVAILABILITY_NOT_CHECKED",
      "status": "not_checked",
      "message": "Configured/declared configuration is not observed remote availability"
    }
  ]
}
```

## Fixture vs later approved integration

| This PR (fixture) | Later operator-approved live run |
|-------------------|----------------------------------|
| `evidence_class: local_fixture` | `evidence_class: live_remote` |
| Loopback fake server or fake CLI | Real endpoint / approved profile |
| Explicitly **not** external certification | Truthful integration evidence when operator opts in |

## Known limitations

- No automatic live discovery or bulk provider scanning
- No provider-selection winner or council/delegate execution
- No real provider run in CI
- Direct API certification stays read-only / no tools
- No durable task reattachment or mid-task steering
- Live CLI adapters fail closed in Phase 7B

## Verification commands

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/mcp_acceptance.py
python3 scripts/clean_install_acceptance.py
python3 -m compileall -q src tests scripts
git diff --check $(git hash-object -t tree /dev/null)
```

## Related

- [`phase-7a.md`](phase-7a.md) — doctor, examples, clean-install proof
- [`examples/litellm-openai-compatible/`](../../../examples/litellm-openai-compatible/) — secret-free provider example
