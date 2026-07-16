# Phase 7A — Field readiness, doctor, examples, clean-install proof

## Scope

Phase 7A builds an **operational adoption runway**: offline diagnostics, versioned
deployment examples, a documented CLI naming correction, and deterministic
clean-install acceptance. It does **not** add autonomous orchestration, network
probes, or paid API calls.

## Breaking change: `recollect` → `recollect-lines`

The primary product console script is now **`recollect-lines`**, not `recollect`.

| Before | After |
|--------|-------|
| `recollect` | `recollect-lines` |
| `recollect-mcp` | `recollect-mcp` (unchanged) |

There is **no** undocumented `recollect` alias. Update shell scripts, MCP host
wrappers, CI jobs, and documentation to call `recollect-lines`.

`python -m recollect_lines` remains available for module invocation.

### Migration

```bash
# Old (removed)
recollect --help

# New
recollect-lines --help
pip install --upgrade .
```

## `recollect-lines doctor`

Offline-safe diagnostics with human-readable default output and stable JSON
(`--json`). Never prints credential values, authorization headers, or raw
secret material.

### What it checks (local only)

| Area | Checked | Not checked (Phase 7A) |
|------|---------|-------------------------|
| Package version | yes | — |
| `--home` writable | yes | — |
| `--workspace` accessible (when passed) | yes | — |
| Subprocess CLI adapters (`--version` probe) | yes | remote runtime health |
| `--providers-config` parse + TLS/HTTP policy | yes | HTTP connectivity to endpoints |
| Credential **references** present/absent | yes (names only) | secret values |
| Capability inventory consistency | yes | remote model availability |

### Exit codes

- `0` — no blocking findings (warnings may be present)
- `1` — blocking findings (invalid config, inaccessible paths, etc.)

### Sample redacted JSON (truncated)

```json
{
  "doctor_schema_version": "1",
  "package": {"name": "recollect-lines", "version": "0.1.0"},
  "status": "degraded",
  "summary": {"blocking": 0, "warning": 2, "info": 8, "not_checked": 2},
  "findings": [
    {
      "code": "PACKAGE_VERSION",
      "severity": "info",
      "status": "ok",
      "message": "recollect-lines 0.1.0"
    },
    {
      "code": "PROVIDER_SECRET_REFERENCE_MISSING",
      "severity": "warning",
      "status": "warning",
      "message": "Provider 'local_litellm': Credential reference 'LITELLM_MASTER_KEY' is not set in the environment",
      "remediation": "Export 'LITELLM_MASTER_KEY' in the broker/MCP environment before using profile 'openai_compatible' with provider 'local_litellm'.",
      "details": {"provider": "local_litellm", "credential_reference": "LITELLM_MASTER_KEY"}
    },
    {
      "code": "ENDPOINT_CONNECTIVITY_NOT_CHECKED",
      "severity": "info",
      "status": "not_checked",
      "message": "Remote provider endpoint reachability was not checked (offline-safe default)"
    }
  ]
}
```

### Remediation quick reference

| Code | Severity | Action |
|------|----------|--------|
| `HOME_NOT_WRITABLE` | blocking | Fix `--home` permissions |
| `WORKSPACE_MISSING` | blocking | Pass a valid `--workspace` |
| `PROVIDERS_CONFIG_INVALID` | blocking | Fix JSON / provider fields |
| `PROVIDER_SECRET_REFERENCE_MISSING` | warning | Export the named env var |
| `RUNTIME_CLI_UNAVAILABLE` | warning | Install CLI or override command prefix |
| `ENDPOINT_CONNECTIVITY_NOT_CHECKED` | not checked | Confirm endpoints manually |

## Deployment examples

Secret-free, commented examples under `examples/`:

1. **`examples/cli-only/`** — mock/subprocess adapters only
2. **`examples/litellm-openai-compatible/`** — loopback OpenAI-compatible gateway
3. **`examples/mixed-cli-and-providers/`** — CLI inventory + named provider

Each README states expected doctor outcomes (including intentional placeholder-secret warnings).

## Clean-install acceptance

Deterministic offline proof that packaging installs correctly:

```bash
python3 scripts/clean_install_acceptance.py
```

Creates a fresh venv, bootstraps `setuptools`/`wheel` into it (one network
fetch in CI — no product credentials), builds a local wheel, installs it with
`--no-index`, then verifies:

- `recollect-lines --help`
- `recollect-lines doctor --json`
- `recollect-mcp --help`
- `recollect` is **absent** from PATH

Integrated into CI on Python 3.11.

## Operational limitations (unchanged, documented)

- **Broker restart** fails closed — no durable session reattachment to in-flight subprocesses.
- **No mid-task steering** — parents cannot redirect a running side agent.
- **Direct API** (`openai_compatible`) is read-only HTTP — no tools, no worktrees, no process supervision.
- **Council selection** is parent-directed — the broker records evidence but does not autonomously pick a winner.
- **Capability declarations** are distinct from observed remote availability; doctor does not prove endpoints are reachable.

## Release-readiness checklist

- [ ] `pip install .` from tag / wheel
- [ ] `recollect-lines doctor --json` — no blocking findings for your config
- [ ] `recollect-mcp` registered in MCP host config with matching `--home`
- [ ] Placeholder secrets exported in deployment environment (never committed)
- [ ] `recollect-lines certify --profile …` — dry-run evidence for your target (see [phase-7b.md](phase-7b.md))
- [ ] `PYTHONPATH=src python3 -m unittest discover -s tests -v`
- [ ] `python3 scripts/mcp_acceptance.py`

## Verification commands

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/mcp_acceptance.py
python3 scripts/clean_install_acceptance.py
python3 -m compileall -q src tests scripts
git diff --check $(git hash-object -t tree /dev/null)
```
