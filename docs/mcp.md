# MCP reference

Program: `recollect-mcp` (stdio JSON-RPC MCP server).

## Launch

```bash
recollect-mcp --home /path/to/.recollect
```

Adapter override flags match `recollect-lines` (`--codex-command`, etc.).

## Protocol

- Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout
- Supported protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`
- Server: `recollect-lines-mcp` v0.1.0
- Diagnostics: stderr only

## Tools (exact names)

| Tool | Purpose |
|------|---------|
| `delegate` | Create + start one task |
| `delegate_batch` | Create + start many tasks independently |
| `status` | Task state, events, artifacts |
| `collect` | Runtime result + broker verification |
| `cancel` | Cancellation with evidence |
| `control` | Operator recovery (`action`: `status`, `cancel`, `collect`, `message`) |
| `message` | Always returns explicit unsupported (no steering) |
| `reconcile` | Post-restart subprocess reconciliation |
| `discover_capabilities` | Runtime/provider inventory |
| `select_candidates` | Policy-aware filtering (parent chooses) |
| `council_validate` | Validate council plan |
| `council_execute` | Execute bounded council plan |
| `task_children` | Direct child task summaries for a parent |
| `task_tree` | Bounded tree for a `root_task_id` |
| `completion_events` | Poll durable completion signals from the global event cursor |

## `delegate` input (schema summary)

Required:

- `task` (string)
- `workspace` (string)

Optional:

| Field | Default | Values |
|-------|---------|--------|
| `execution_mode` | `read_only` | `read_only`, `isolated_worktree` |
| `runtime` | `mock` | `mock`, `opencode`, `claude_code`, `codex`, `cursor`, `openai_compatible` |
| `profile` | — | **Deprecated.** Legacy alias for `runtime`; accepted only for known runtime identifiers |
| `model` | — | Optional requested model identifier (persisted only in this release) |
| `agent_profile` | — | Optional behavioral role identifier (persisted only in this release) |
| `result_schema` | — | Optional normalized result schema (`plain-summary`, `evidence-report`, `review-findings`, `implementation-report`); unknown values rejected at delegate. Structured schemas append a versioned prompt-level output contract at launch (not provider-native structured output). |
| `provider` | — | Required when `runtime` is `openai_compatible` |
| `timeout_seconds` | `1800` | positive integer |
| `verification_policy` | `none` | `none`, `advisory`, `required` |
| `verify_commands` | — | array of argv arrays |
| `parent_task_id` | — | optional existing broker parent |
| `external_root_id` | — | audit-only host/conversation grouping |
| `relationship` | — | `delegates`, `continues` (requires parent; `continues` is a new task, not resume) |
| `origin_kind` | `host` | `host` (external host via CLI/MCP, including parented tasks), `side_agent` (reserved for future explicit recursive callback path; audit only, not authorization) |
| `origin_ref` | — | audit-only caller reference |

`root_task_id` and `delegation_depth` are broker-derived and rejected if callers supply them.

`delegate` returns `task_id`, `state`, `workspace`, `runtime`, `profile` (bridge), optional side-agent and lineage fields, and `compatibility` when a legacy `profile` was translated — not a fabricated completion.

See [migration-runtime-profile.md](migration-runtime-profile.md) for translation rules.

## Tool result envelope

Successful tool calls return MCP `content` with JSON:

```json
{
  "envelope_version": 1,
  "tool": "collect",
  "ok": true,
  "data": { }
}
```

Errors use `"ok": false` and `"error": { "code", "message" }` at the envelope level (business errors), distinct from JSON-RPC protocol errors.

## Provider configuration is a startup snapshot

`discover_capabilities` includes a `provider_config` object:

```json
{
  "source": "/path/to/.recollect/config.yaml",
  "source_origin": "repo_local",
  "loaded_at": "2026-07-16T13:59:41.475052+00:00",
  "restart_required_for_changes": true,
  "note": "Provider configuration is a startup snapshot: the resolved configuration file (if any) is read once when the broker/MCP process starts. Editing the file on disk afterward does not change the running process — restart the broker/MCP server to load changes."
}
```

`source` is `"not_configured"` when no provider configuration file was resolved from any tier. `source_origin` names which precedence tier selected `source`: `explicit` (`--providers-config`), `env` (`RECOLLECT_CONFIG`), `repo_local` (`./.recollect/config.{yaml,yml,json}`), `user_level` (`~/.recollect/config.{yaml,yml,json}`), `legacy_default` (`./providers.json`), or `not_configured`. See [cli.md](cli.md#provider-configuration-resolution-order) for the full precedence order and its fail-truthfully rule for configured (explicit/env) sources. `loaded_at` is when *this* process read the file — not when it was last modified on disk. There is no hot reload: if you edit the file, this MCP server will keep serving the old snapshot until it is restarted. Never contains credential values. Both JSON and YAML (safe-loaded only) are supported.

## Host configuration example

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

Illustrative Hermes-style entry (optional, not required):

```json
{
  "mcpServers": {
    "recollect-lines": {
      "command": "recollect-mcp",
      "args": ["--home", "~/.recollect"]
    }
  }
}
```

## Offline acceptance

```bash
python3 scripts/mcp_acceptance.py
```

Uses deterministic fake CLIs — no network or credentials.

## Parent-agent flow

See [user-flows.md](user-flows.md#parent-agent-mcp-flow).
