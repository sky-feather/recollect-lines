# Phase 6C — Configurable OpenAI-compatible provider fabric

## Scope

Phase 6C adds a **plural, configuration-driven provider layer** and a
**deliberately capability-limited direct HTTP runtime** for OpenAI-compatible
chat-completions endpoints. It does not change subprocess adapter semantics
(OpenCode, Claude Code, Codex, Cursor). Phase 6D (discovery, routing, bounded
councils) is documented separately in [phase-6d.md](phase-6d.md).

Provider configuration entries describe model endpoints. They are **not**
runtime adapters: they never grant agent tools, worktree access, process-group
cancellation, or live steering on their own.

## Provider configuration

Operators supply a JSON document (schema-equivalent to [RFC-001](../../design/RFC-001.md)
§10.4's illustrative YAML) via `--providers-config`:

```json
{
  "providers": {
    "alpha": {
      "kind": "openai-compatible",
      "base_url": "https://api.example.com/v1",
      "api_key_env": "ALPHA_API_KEY",
      "default_model": "alpha-model",
      "request_timeout_seconds": 120,
      "tls_verify": true,
      "capabilities": {
        "chat_completions": true
      }
    },
    "local": {
      "kind": "openai-compatible",
      "base_url": "http://127.0.0.1:8765/v1",
      "api_key_env": "LOCAL_MODEL_API_KEY",
      "default_model": "local-coder",
      "allow_insecure_http": true,
      "request_timeout_seconds": 30
    }
  }
}
```

Validation (fail closed):

- provider names are lowercase identifiers (`^[a-z][a-z0-9_-]{0,62}$`), unique;
- `kind` must be `openai-compatible`;
- `base_url` must be `http`/`https` with a host;
- remote `http` is rejected — loopback `http` requires explicit
  `allow_insecure_http: true`;
- HTTPS verifies TLS by default; optional `ca_bundle` for custom CAs — no blanket
  insecure-TLS bypass;
- `api_key_env` must name an environment variable (reference only — never stored
  in SQLite, artifacts, or logs);
- `default_model`, timeouts, and declared `capabilities` are validated strictly.

## Credential references

Secrets are resolved at request time from `api_key_env` only. Missing or empty
environment values raise `MissingCredentialReference` and fail the task without
persisting the secret. Error text and summaries pass through
`redact_provider_error()` (shared secret-shape heuristics with CLI adapters).

## Direct API runtime (`openai_compatible` profile)

Tasks select the runtime via `profile: "openai_compatible"` and a **named**
`provider` field (CLI `--provider`, MCP `delegate.provider`).

`OpenAiCompatibleDirectRuntime` (`src/recollect_lines/direct_api_runtime.py`):

- sends one `POST {base_url}/chat/completions` request per task;
- supports **`read_only` execution_mode only** — `isolated_worktree` is rejected
  at profile validation (no honest workspace mutation claim);
- runs the HTTP call on a background thread with cooperative cancel via
  `threading.Event` (not process-group termination);
- normalizes success/error evidence into the existing broker `result.json`
  lifecycle shape with explicit `limitations` and declared provider capabilities;
- on broker restart, in-flight direct API tasks reconcile to **failed** with
  `direct_api_restart_no_reattachment` — no session reattachment is claimed.

Honest limitations recorded on every result:

- no subprocess supervision or process-group cancellation;
- no agent tool loop or repository/worktree mutation;
- no live mid-task steering or session reattachment after restart;
- cancellation is cooperative HTTP abort only.

## Integration surfaces

- `Broker(..., providers_config=Path)` loads named providers once at construction.
- `recollect-lines --providers-config … create --profile openai_compatible --provider …`
- `recollect-mcp --providers-config …` — MCP `delegate`/`delegate_batch` accept
  optional `provider` when `profile` is `openai_compatible`.
- Existing CLI/MCP adapter overrides (`--opencode-command`, etc.) unchanged.

Generic MCP acceptance (`scripts/mcp_acceptance.py`) remains adapter-focused;
Phase 6C evidence is deterministic unittest coverage against
`tests/fixtures/fake_openai_server.py` (local loopback HTTP only).

## Test evidence

| Area | Test module | Fixture |
|---|---|---|
| Config validation, TLS/http policy, credential fail-closed | `tests/test_providers.py`, `tests/test_direct_api_runtime.py` | inline JSON |
| Success, malformed body, 429, timeout, cancel, redaction, multi-provider, restart reconcile | `tests/test_direct_api_runtime.py` | `tests/fixtures/fake_openai_server.py` |

No real provider smoke was run in CI — no preconfigured non-secret remote
endpoint was available in this environment. Deterministic local fixture evidence
is the acceptance standard for this phase.

## Non-goals (at Phase 6C delivery time)

- Phase 6D routing, capability discovery, policy-aware provider selection, or
  bounded council orchestration (since delivered in [phase-6d.md](phase-6d.md)).
- Vendor-specific branching (DeepSeek/Qwen/OpenAI/etc.) in source — only named
  configuration entries.
- Claiming CLI-runtime capabilities (worktrees, tools, streaming events) for a
  plain chat-completions endpoint.
