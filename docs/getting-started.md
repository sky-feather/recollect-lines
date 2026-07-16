# Getting started

Start with [operator-guide.md](operator-guide.md) for what Recollect Lines is (and is not), runtime vs parent-host boundaries, the security model, and troubleshooting from real dogfood failures.

## Product sentence

> Recollect Lines is a local-first delegation broker that lets a parent agent safely hand bounded work to existing AI coding runtimes and receive attributable, evidence-backed results.

## Requirements

- **Python 3.11+** (`requires-python` in `pyproject.toml`)
- **Git** (for `isolated_worktree` tasks and acceptance fixtures)
- A supported **runtime CLI** on `PATH` only if you delegate to that profile (e.g. `codex` for `--profile codex`)

Recollect Lines is **not published on PyPI** as of this writing. Install from a repository checkout.

## Install from source

```bash
git clone https://github.com/Umi4Life/recollect-lines.git
cd recollect-lines
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install .
```

Verify console entry points (there is **no** legacy `recollect` executable):

```bash
recollect-lines --help
recollect-mcp --help
which recollect && echo "unexpected legacy binary" || echo "ok: no recollect alias"
```

### Contributor editable install

```bash
pip install -e .
PYTHONPATH=src python3 -m recollect_lines --help   # equivalent during development
```

### Offline / CI-style clean install proof

```bash
python3 scripts/clean_install_acceptance.py
```

This builds a disposable venv, installs the package from local artifacts, and checks `recollect-lines` / `recollect-mcp` help output.

## Five-minute clean operator path

Hermetic acceptance for a fresh operator — no live provider, network login, or credentials.
CI runs the same script on every pull request.

```bash
python3 scripts/five_minute_acceptance.py
```

The script installs into a disposable venv, then runs:

| Step | Command (conceptual) | Evidence |
|------|----------------------|----------|
| Install | local wheel / fallback | `recollect-lines` + `recollect-mcp` on `PATH` |
| Init | `recollect-lines init --json` | `home_created`, starter `config.yaml` |
| Validate | `recollect-lines config validate --json` | resolved source, redacted diagnostics |
| Provider | `provider add --api-key-env …` | credential **reference** only, never the value |
| Doctor | `recollect-lines doctor --json` | stable schema, no secret echo |
| MCP | `mcp print` → `mcp install` (temp host file) | registration + initialize ping |
| Delegate ping | `recollect-mcp` + fixture OpenCode adapter | `delegate` → `collect` → `succeeded` |

See [operator-guide.md](operator-guide.md#five-minute-success-path-clean-environment) for the product context.

### Manual mock quick start (CLI only)

The acceptance script above is the canonical five-minute path. For a minimal
hands-on mock flow without the venv installer:

Uses the deterministic **mock** profile — no external model or CLI.

```bash
export RECOLLECT_HOME=/tmp/recollect-quickstart
mkdir -p "$RECOLLECT_HOME"

# 1. Create and queue a task
recollect-lines --home "$RECOLLECT_HOME" create \
  --task 'Summarize the README' \
  --workspace "$(pwd)" \
  --profile mock

# Note the task id from JSON output, then:
TASK_ID=tsk_xxxxxxxx   # replace with your id

# 2. Start (mock runs synchronously in-process)
recollect-lines --home "$RECOLLECT_HOME" start "$TASK_ID"

# 3. Complete mock work (real runtimes use collect instead)
recollect-lines --home "$RECOLLECT_HOME" complete "$TASK_ID" \
  --summary 'Mock summary for quickstart'

# 4. Inspect
recollect-lines --home "$RECOLLECT_HOME" status "$TASK_ID"
recollect-lines --home "$RECOLLECT_HOME" list
```

For **real subprocess runtimes** (Codex, Claude Code, OpenCode, Cursor), use a **long-lived** `recollect-mcp` session or the [Codex demo script](../scripts/run_codex_demo.py) — see [user-flows.md](user-flows.md#cli-limitation-subprocess-collection).

### Integrated side-agent fixture (offline)

Proves heterogeneous concurrent children, completion-event polling, normalized results, task trees, steering refusal with `continues` follow-up, and writer isolation — without provider credentials:

```bash
PYTHONPATH=src python3 scripts/side_agent_fixture_acceptance.py
```

Evidence: [demos/side-agent-fixture-evidence.json](demos/side-agent-fixture-evidence.json). Live multi-runtime dogfood is a separate opt-in runbook: [demos/live-two-runtime-dogfood-runbook.md](demos/live-two-runtime-dogfood-runbook.md).

## MCP quick start

Add to your MCP host (shape is generic; adjust paths):

```json
{
  "mcpServers": {
    "recollect-lines": {
      "command": "recollect-mcp",
      "args": ["--home", "/path/to/.recollect"]
    }
  }
}
```

Then call `delegate` → poll `status` → `collect`. Details: [mcp.md](mcp.md).

## Diagnostics

```bash
recollect-lines --home ~/.recollect doctor
recollect-lines --home ~/.recollect doctor --json
```

## First-time setup

A fresh checkout has no local state yet. `init` is the one safe,
non-interactive, idempotent command that creates it:

```bash
recollect-lines init        # creates ./.recollect/ + a starter config.yaml (mode 0600),
                             # only if either is absent, then runs config validate
recollect-lines init        # run again any time: no-op if already initialized
```

It never captures or writes a real credential — the starter config
references a placeholder environment-variable name only — and it never
touches an existing config file unless you explicitly pass `--force`. The
printed report tells you exactly what was created vs. already present, the
active config source/path, and next steps: `provider add` for a named HTTP
gateway, `mcp install` for a supported parent host, then `config validate`
and `doctor`.

## Provider configuration

`openai_compatible` is a **text/synthesis** runtime over HTTP — it does not
supervise workspace-mutating CLI adapters. CLI profiles (`codex`, `claude_code`,
`cursor`, `opencode`) own subprocess supervision and worktree policy; keep a
long-lived `recollect-mcp` session when collecting their results.

Preferred operator flow (YAML operator config, not legacy repo-root JSON):

```bash
recollect-lines init                   # or: recollect-lines config init
recollect-lines provider add --name my_gateway \
  --base-url https://gateway.example.invalid/v1 \
  --api-key-env MY_GATEWAY_API_KEY --default-model my-model \
  --path ./.recollect/config.yaml
export MY_GATEWAY_API_KEY=...          # secret stays in the environment
recollect-lines config validate --json # resolved source + validation; secrets redacted
recollect-lines provider test my_gateway --json
```

Start from [`config/providers.example.yaml`](../config/providers.example.yaml)
(schema: [`config/providers.schema.json`](../config/providers.schema.json)).
Legacy `./providers.json` still resolves at the lowest precedence tier for
backward compatibility, but new operators should use `./.recollect/config.yaml`.

See [cli.md](cli.md#provider-configuration-resolution-order) for the full
resolution order and strict-validation rules.

### CA bundles / custom certificates

A provider's `ca_bundle` field, when set, must be a filesystem path to a CA
bundle file — never inline certificate/key content. When unset, the system
default trust store is used. That default differs by platform:

- **Linux**: the distribution's system CA bundle is used automatically
  (commonly `/etc/ssl/certs/ca-certificates.crt` or
  `/etc/pki/tls/certs/ca-bundle.crt` depending on distro — you normally don't
  need to set `ca_bundle` at all).
- **macOS with the python.org installer**: this Python build does **not**
  ship with the system trust store wired up. Either run the installer's
  `Install Certificates.command` (in `/Applications/Python <version>/`) once,
  or install [`certifi`](https://pypi.org/project/certifi/) and point
  `ca_bundle` at it: `python3 -c "import certifi; print(certifi.where())"`.
- **Any platform, explicit override**: install `certifi` and set
  `ca_bundle` to `certifi.where()`, or point it at any other CA bundle file
  your organization provides (e.g. a corporate proxy's root CA).

Do not hard-code `/etc/ssl/cert.pem` or any single path as a universal
default — it does not exist on most Linux distributions and is not the
right answer on macOS/Windows.

## Next steps

- [user-flows.md](user-flows.md) — roles, boundaries, runtime matrix
- [demos/side-agent-fixture-evidence.json](demos/side-agent-fixture-evidence.json) — offline integrated side-agent proof
- [demos/codex-marker-evidence.json](demos/codex-marker-evidence.json) — recorded live Codex-through-broker run
- [design/PRD.md](design/PRD.md) — full product contract
