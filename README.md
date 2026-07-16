# Recollect Lines

> Recollect Lines is a local-first delegation broker that lets a parent agent safely hand bounded work to existing AI coding runtimes and receive attributable, evidence-backed results.

It is **not** a new editor, not a coding agent, not an OpenCode plugin, and not merely an MCP server. **MCP** and **CLI** are interfaces to the broker; **Codex, Cursor, Claude Code, OpenCode**, and HTTP providers are **runtime backends** the broker supervises.

## What is this?

A small, local-first **delegation broker**: queue bounded work, supervise an external runtime, store durable evidence, and return a concise result. Parent agents (or human operators) stay in control of scope, timeouts, cancellation, and optional verification.

## Who uses it?

| User | Role |
|------|------|
| **Parent agents** | Delegate bounded tasks via MCP or CLI; collect evidence-backed summaries |
| **Operators** | Install, configure runtimes, diagnose (`doctor`), certify integrations |

## What problem does it solve?

Ad-hoc subprocess delegation loses track of what was asked, what ran, and whether a self-reported success is true. Recollect Lines adds durable task state, artifact manifests, bounded execution, cancellation evidence, and optional broker-verified checks.

## Is it an MCP server, plugin, or coding program?

| | |
|-|-|
| **Is** | A broker with **CLI** (`recollect-lines`) and **stdio MCP** (`recollect-mcp`) interfaces |
| **Is** | A supervisor for **existing** runtime CLIs and HTTP provider endpoints |
| **Is not** | A replacement IDE, agent host, or OpenCode/Codex plugin |
| **Is not** | “Just MCP” — MCP is one entry point; the product is the broker + evidence model |

```text
Operator / parent agent
        |
        v
 Recollect Lines  ------>  Runtime backend (codex, claude, cursor, …)
   (broker)                    existing CLI / API
        |
        v
 Evidence-backed result (summary + artifacts)
```

## What works today

- Task lifecycle: create, start, status, collect/cancel, timeout with process-group checks
- Profiles: `mock`, `opencode`, `claude_code`, `codex`, `cursor`, `openai_compatible` (experimental CLI adapters)
- Durable SQLite storage, artifact directories, optional verification gate
- Post-restart reconciliation (truthful `failed` / `recovery_required`, not fabricated success)
- Capability discovery, routing, bounded councils; `doctor` and `certify` harnesses
- Parent-directed bounded task trees: `external_root_id`, lineage, `task_tree`, heterogeneous `runtime` / `agent_profile` / `model` / `result_schema` per child
- Durable completion-event cursor polling and provenance-aware normalized results (raw evidence artifacts kept separate)

**Honest limits:** no in-flight steering (use `relationship=continues` for follow-up work); no session resume after broker restart; subprocess `collect` needs the same long-lived broker instance that started the task (use MCP or a script for real runtimes). Details in [docs/user-flows.md](docs/user-flows.md).

## Fastest way to try it

**Python 3.11+**, install from source (not on PyPI yet):

```bash
git clone https://github.com/Umi4Life/recollect-lines.git
cd recollect-lines
python3 -m venv .venv && source .venv/bin/activate
pip install .
recollect-lines --help
recollect-mcp --help
```

**Offline five-minute operator path (no provider):** [docs/operator-guide.md](docs/operator-guide.md#five-minute-success-path-clean-environment)

```bash
python3 scripts/five_minute_acceptance.py
```

**Offline mock (CLI only):** [docs/getting-started.md](docs/getting-started.md#manual-mock-quick-start-cli-only)

**Integrated side-agent fixture proof (no provider):** [docs/demos/side-agent-fixture-evidence.json](docs/demos/side-agent-fixture-evidence.json) — reproduce with:

```bash
PYTHONPATH=src python3 scripts/side_agent_fixture_acceptance.py
```

**Recorded live Codex demo:** [docs/demos/codex-marker-evidence.json](docs/demos/codex-marker-evidence.json) — reproduce with:

```bash
python3 scripts/run_codex_demo.py --execute-live --acknowledge-provider-call
```

## Entry points

| Interface | Command | Doc |
|-----------|---------|-----|
| CLI | `recollect-lines` | [docs/cli.md](docs/cli.md) |
| MCP | `recollect-mcp` | [docs/mcp.md](docs/mcp.md) |

Generic MCP host config:

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

## Documentation

- [docs/README.md](docs/README.md) — documentation index
- [docs/operator-guide.md](docs/operator-guide.md) — product orientation for fresh operators
- [docs/getting-started.md](docs/getting-started.md) — install and quick start
- [docs/user-flows.md](docs/user-flows.md) — operator, parent-agent, and runtime flows
- [docs/demos/](docs/demos/) — end-to-end proofs

## Supported capabilities and limitations

| Area | Status |
|------|--------|
| Local broker + artifacts | Supported |
| MCP + CLI interfaces | Supported |
| Multiple runtime adapters | Supported, experimental |
| `isolated_worktree` workspace safety | Supported |
| Broker-verified verification gate | Supported, opt-in per task |
| Post-restart reconciliation | Supported; no full result recovery |
| In-flight message steering | Not supported (explicit refusal) |
| PyPI package | Not published yet |

Canonical design: [docs/design/PRD.md](docs/design/PRD.md), [docs/design/RFC-001.md](docs/design/RFC-001.md).

## Tests and CI

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/five_minute_acceptance.py
python3 scripts/mcp_acceptance.py
python3 scripts/side_agent_fixture_acceptance.py
python3 scripts/clean_install_acceptance.py
python3 -m compileall -q src tests scripts
```

## Contributing / design history

- Product requirements: [docs/design/PRD.md](docs/design/PRD.md)
- Implementation RFC: [docs/design/RFC-001.md](docs/design/RFC-001.md)
- Phase implementation records: [docs/history/phases/](docs/history/phases/) (not the user guide)
