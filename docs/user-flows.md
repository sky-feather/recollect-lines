# User flows

Three roles appear in every delegation: **operator/parent** (human or parent agent), **Recollect Lines broker**, and **runtime backend** (Codex CLI, Cursor, Claude Code, OpenCode, or HTTP provider). The broker owns task state, artifacts, timeouts, cancellation evidence, and optional broker-verified checks. Runtimes are adapters — not plugins inside Recollect Lines.

```text
Operator / parent agent
        |
        |  CLI or MCP (stdio JSON-RPC)
        v
 Recollect Lines broker  ---- supervise ---->  Runtime backend
        |                                        (codex, claude, …)
        |  durable SQLite + artifact dir
        v
 Evidence-backed result (summary + artifacts, optional verification)
```

## Human / operator CLI flow

The CLI exposes discrete lifecycle commands; there is **no** single `submit` command.

### Typical sequence

```bash
BROKER=~/.recollect

# 1. Create (queues task)
recollect-lines --home "$BROKER" create \
  --task 'Inspect failing test output' \
  --workspace /path/to/repo \
  --profile mock \
  --mode read_only \
  --timeout 600

TASK_ID=tsk_...   # from JSON

# 2. Start runtime
recollect-lines --home "$BROKER" start "$TASK_ID"

# 3. Observe (any time)
recollect-lines --home "$BROKER" status "$TASK_ID"

# 4. Terminal collection
#    mock: use complete (see getting-started.md)
#    subprocess runtimes: collect (see limitation below)
recollect-lines --home "$BROKER" collect "$TASK_ID"

# 5. Cancel while active (optional)
recollect-lines --home "$BROKER" cancel "$TASK_ID" --reason 'operator abort'

# 6. Operator recovery/control (explicit actions)
recollect-lines --home "$BROKER" control "$TASK_ID" --action status
recollect-lines --home "$BROKER" control "$TASK_ID" --action cancel
```

### CLI limitation: subprocess collection

`recollect-lines` is a **short-lived process**. Subprocess-backed tasks (`opencode`, `claude_code`, `codex`, `cursor`) hold an in-memory process handle in the broker instance that called `start`. If that process exits before `collect`, a **new** CLI invocation cannot attach to the running runtime; `collect` may reconcile to `failed` with `missing_process_handle` even when the runtime already finished.

**Practical options for real runtimes:**

1. **Parent-agent MCP** — keep `recollect-mcp` running; use `delegate` (create+start) then `collect` on the same server ([mcp.md](mcp.md)).
2. **Orchestration script** — one process drives create → start → poll → collect ([scripts/run_codex_demo.py](../scripts/run_codex_demo.py)).
3. **Mock / certification** — `complete` for mock; `certify` for integration checks.

This is intentional restart-safety semantics, not session resume. See [design/RFC-001.md](design/RFC-001.md).

## Parent-agent MCP flow

Any MCP stdio host launches `recollect-mcp` and calls tools by name.

### Minimal configuration

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

Host-specific wrappers (Cursor, Claude Desktop, custom agents) use the same `command` / `args` shape.

### Lifecycle (exact tool names)

1. **`initialize`** / **`notifications/initialized`** — standard MCP handshake.
2. **`tools/list`** — discover tools (includes `delegate`, `status`, `collect`, `cancel`, `reconcile`, `discover_capabilities`, …).
3. **`delegate`** — create and start one task. Required: `task`, `workspace`. Common: `profile`, `execution_mode`, `timeout_seconds`, `verification_policy`, `verify_commands`.
4. **`status`** — durable state, events, artifact manifest (`task_id`).
5. **`collect`** — runtime result + broker verification artifacts (`task_id`). Idempotent on terminal tasks.
6. **`cancel`** — request cancellation with evidence (`task_id`, optional `reason`).
7. **`reconcile`** — after broker restart, classify orphaned subprocess state (optional `task_id`).

Example `delegate` arguments for Codex read-only inspection:

```json
{
  "task": "Read alpha.txt and beta.txt. Reply with only the filename containing MARKER_ALPHA.",
  "workspace": "/path/to/fixture-repo",
  "profile": "codex",
  "execution_mode": "read_only",
  "timeout_seconds": 120
}
```

Offline proof without provider calls: `python3 scripts/mcp_acceptance.py`.

Full schemas: [mcp.md](mcp.md).

## Runtime / backend flow

Recollect Lines does **not** embed Codex, Cursor, or Claude. Each **profile** selects an adapter that supervises an external CLI or HTTP endpoint.

| Profile | Backend | How broker supervises | Notes |
|---------|---------|----------------------|-------|
| `mock` | In-process stub | Synchronous | Tests, quickstart |
| `opencode` | OpenCode CLI | `npx opencode-ai` subprocess | Experimental |
| `claude_code` | Claude Code CLI | `claude -p` subprocess | Experimental |
| `codex` | Codex CLI | `codex exec --json` subprocess | Experimental; ChatGPT subscription quota |
| `cursor` | Cursor CLI | `cursor-agent` subprocess | Experimental |
| `openai_compatible` | HTTP chat API | Direct HTTP runtime | Requires `--providers-config` |

### What works today

- Delegate with bounds (timeout, `read_only` / `isolated_worktree`)
- Durable task/event storage and artifact manifests
- Process-group cancellation with evidence
- Optional per-task verification gate (`none` / `advisory` / `required`)
- Post-restart **reconciliation** (truthful `failed` / `recovery_required`, not fabricated success)
- Capability discovery and parent-directed routing (`discover`, `select`, `council`)
- Bounded task trees with `external_root_id`, `parent_task_id`, `relationship`, and `task_tree`
- Per-child `runtime`, `agent_profile`, `model`, and `result_schema` (independent dimensions)
- Durable `completion_events` cursor polling and provenance-aware `normalized_result` on `collect`
- Fixture proof of heterogeneous concurrent children: [demos/side-agent-fixture-evidence.json](demos/side-agent-fixture-evidence.json)

### What does not work / is not claimed

- **Session resume** — no re-attachment to a running runtime after broker restart with full result recovery
- **In-flight steering** — `message` / `control --action message` always refuse; use `relationship=continues` for explicit follow-up tasks
- **Live multi-runtime dogfood in CI** — see [demos/live-two-runtime-dogfood-runbook.md](demos/live-two-runtime-dogfood-runbook.md) (opt-in operator runbook, not performed here)
- **Continuous upstream CLI certification** — adapters are spike-tested, not pinned to every upstream release
- **PyPI install** — source install only until published

## Recorded demos

- Integrated side-agent fixture proof (offline, CI): [demos/README.md](demos/README.md#integrated-side-agent-fixture-proof-offline-ci)
- Live Codex marker identification through MCP: [demos/README.md](demos/README.md#codex-marker-identification-live)
- Opt-in live two-runtime dogfood runbook (not performed in repo): [demos/live-two-runtime-dogfood-runbook.md](demos/live-two-runtime-dogfood-runbook.md)

## Parent-directed side-agent tree (host flow)

A parent host (human or agent) can delegate multiple bounded children under one operation:

1. **Group host work** with `external_root_id` — audit-only; does not invent a broker parent.
2. **Delegate children** with explicit `runtime`, optional `model`, optional `agent_profile`, optional `result_schema`, and `parent_task_id` when the child belongs to a host-directed tree. Absent `origin_kind`, host-facing CLI/MCP calls default to `origin_kind=host` whether or not `parent_task_id` is set; parenthood alone does not imply `side_agent`. Reserve `origin_kind=side_agent` for a future explicit authenticated recursive callback path. Provenance is audit metadata only — it does not change authorization, permissions, or runtime behavior.
3. **Continue host-side work** while children run (separate tasks sharing the same `external_root_id`).
4. **Poll** `completion_events` / `completion-events` by monotonic cursor for compact terminal signals.
5. **Collect** concise `normalized_result` views; inspect raw runtime artifacts separately under the task artifact directory.
6. **Inspect** `task_tree` / `task-tree` for a bounded descendant list.
7. When mid-task steering is required, expect an explicit refusal — create a new child with `relationship=continues` instead of session resume.

Offline proof (fixture adapters only, no provider credentials):

```bash
PYTHONPATH=src python3 scripts/side_agent_fixture_acceptance.py
```

Live two-runtime proof is documented separately and requires operator opt-in: [demos/live-two-runtime-dogfood-runbook.md](demos/live-two-runtime-dogfood-runbook.md).
