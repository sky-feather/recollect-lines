# Phase 4 — Local stdio MCP interface

## Scope

Phase 4 exposes the Phase 1–3 broker to parent agents over the Model Context
Protocol (MCP), so a parent agent can delegate, poll, collect, and cancel
broker tasks as MCP tools instead of shelling out to the `recollect` CLI. It
adds one new module, `src/recollect_lines/mcp_server.py`, and does not change
`models.py`, `service.py`, `store.py`, `workspace.py`, `adapters.py`,
`opencode_adapter.py`, or `cli.py` — every Phase 1–3 API, CLI behavior, and
test keeps working unchanged (`tests/test_broker.py`,
`tests/test_opencode_adapter.py`, `tests/test_workspace_safety.py` all pass
as-is).

Not in scope, per the Phase 1–3 boundary docs and unchanged here: a web
server or HTTP transport, authentication, remote workers, multi-user
support, daemon/restart recovery, new model providers, or automatic
background scheduling. This server is a thin, local, single-parent-agent
front door onto the existing broker — nothing more.

## Transport

`python -m recollect_lines.mcp_server --home <path>` (or the `recollect-mcp`
console script installed by this package) runs a stdio MCP server:
newline-delimited JSON-RPC 2.0 messages on stdin/stdout, one complete message
per line, exactly as the MCP stdio transport expects. `--home` matches the
existing `recollect-lines --home` flag — point both at the same directory to
operate on the same durable broker state.

**All protocol output is JSON-RPC on stdout. Nothing else is ever written
there.** Diagnostics (unexpected internal errors, tracebacks) go to stderr
only, so a client reading stdout as strict JSON-RPC framing never sees a
stray line. Every unexpected exception is caught before it can reach stdout
or crash the process — the worst case is a `-32603 Internal error` JSON-RPC
response or an `isError: true` tool result, never a dropped connection.

Supported JSON-RPC methods: `initialize`, `notifications/initialized`,
`ping`, `tools/list`, `tools/call`. `notifications/initialized` and any
other message received without an `id` field are notifications: they are
processed but never produce a response, per JSON-RPC 2.0 semantics. Blank
lines between messages are ignored.

## Tool-result envelope (versioned)

Every `tools/call` response's single `content` block is `{"type": "text",
"text": "<JSON>"}`, and that JSON is always this envelope:

```json
{
  "envelope_version": 1,
  "tool": "<tool name>",
  "ok": true,
  "data": { "...": "tool-specific payload" }
}
```

or, on failure:

```json
{
  "envelope_version": 1,
  "tool": "<tool name>",
  "ok": false,
  "error": { "code": "KeyError", "message": "Unknown task: tsk_..." }
}
```

`isError` on the MCP tool result always matches `!ok`. `envelope_version`
lets a client detect a future incompatible reshaping of `data`/`error`
without guessing from field presence.

## Error model

Two distinct channels, deliberately kept separate:

1. **JSON-RPC protocol errors** (`error` at the top level of the JSON-RPC
   response) — the message or request itself is invalid, before any broker
   call is made:
   - `-32700` Parse error: a line isn't valid JSON.
   - `-32600` Invalid Request: not a JSON object, wrong/missing `jsonrpc`,
     or missing/non-string `method`.
   - `-32601` Method not found: an unknown top-level method (anything other
     than the five supported above).
   - `-32602` Invalid params: `tools/call` with a missing/non-string `name`,
     an unknown tool `name`, or non-object `arguments`.
   - `-32603` Internal error: a genuinely unexpected server-side exception
     while dispatching (should not happen in normal operation; logged to
     stderr with a traceback).
2. **Tool-result `isError: true`** — a *known* tool ran and rejected the
   call for a business/broker reason: a malformed argument value (e.g. a
   missing `task`, a non-integer `timeout_seconds`), an unknown `task_id`,
   an illegal state transition, a workspace/lease/policy rejection from the
   broker, or any other exception raised while executing the tool. This
   keeps one bad item in `delegate_batch` from turning the *entire call*
   into a protocol failure — see below.

## Tool contracts

All six tools reuse the existing `Broker` API directly (`create`, `start`,
`status`, `collect`, `cancel`, `verify`, and the public `store.artifacts`
path for artifact reads) — no lifecycle, policy, or workspace logic is
duplicated in the MCP layer.

### `delegate`

Creates and starts one task: `broker.create(TaskRequest(...))` followed by
`broker.start(record.id)`. Input: `task`, `workspace` (required strings);
`execution_mode` (`read_only` default, or `isolated_worktree`); `profile`
(`mock` default, or `opencode`); `timeout_seconds` (positive int, default
1800); optional `verify_commands` (array of non-empty argv arrays of
strings — never shell strings), persisted as the task's
`verify_commands.json` artifact for `collect` to use later. Returns
`{task_id, state, workspace, execution_mode, profile}` reflecting whatever
state `start()` actually reached — including a `failed` state with its
reason, for e.g. `workspace_invalid` or `workspace_lease_conflict` — never a
fabricated success.

### `delegate_batch`

Input: `tasks`, a non-empty array of `delegate`-shaped items. Each item is
validated and `create`/`start`-ed **independently**, in a per-item
`try/except` — an exception for item *N* (bad shape, policy rejection,
anything) is recorded as that item's outcome and never affects items already
started earlier in the same call, or later items in the same call. Returns
`{"outcomes": [{"index", "accepted": true, task_id, state, ...} | {"index",
"accepted": false, "task_id"?, "error": {code, message}}, ...]}`, one entry
per input item in order. `task_id` is present on a rejected outcome too if
the task was actually created before something (unexpectedly) failed to
start it — the caller can still `status`/`cancel` it. `tasks` missing,
non-array, or empty is itself a rejected tool call (`isError: true`), same
as every other per-item validation failure — never a JSON-RPC protocol
error, so a malformed batch request never masks results already computed
for other tools in the same session.

### `status`

Input: `task_id`. Directly returns `broker.status(task_id)`: the task
record, its full event history, and its artifact manifest (filenames, byte
counts, and sha256 — never a raw host filesystem path outside the task's own
declared `workspace`).

### `collect`

Input: `task_id`. If a `verify_commands.json` artifact exists for this task
(from `delegate`), runs `broker.verify(task_id, commands)` **first** — while
an `isolated_worktree` task's worktree/lease is still active — and folds any
verify-side failure (e.g. the worktree was already released) into
`broker_verification` as a best-effort result rather than blocking the
collection below. Then calls `broker.collect(task_id)` and reads back
`result.json` if the runtime produced one. Returns:

```json
{
  "task_id": "...",
  "state": "succeeded | succeeded_with_warnings | failed | ...",
  "runtime_result": { "...": "the adapter's own result.json, or null" },
  "broker_verification": { "...": "broker.verify()'s payload, or null if no verify_commands were supplied" }
}
```

`runtime_result.runtime.verification` (when present) is the adapter's own
self-report (`tests_broker_verified: false, source: "runtime_reported"`, per
Phase 2/3) — always kept distinct from `broker_verification`, whose entries
are `broker_verified: true` because the broker itself, not the adapter, ran
those commands as a subprocess and observed the result directly. For the
deterministic `mock` profile — which never registers a runtime process
handle — `collect` always (correctly) reaches `state: "failed"` with
`runtime_result: null`, since there is no real subprocess to join; this is
existing, unchanged `Broker.collect()` behavior, not an MCP-layer
simplification.

### `cancel`

Input: `task_id`, optional `reason` (default `"Cancelled by MCP caller"`).
Directly returns `broker.cancel(task_id, reason)`'s resulting
`{task_id, state, workspace, execution_mode, profile}` — the real,
adapter-confirmed (for OpenCode, process-group-probed) outcome, per Phase 2.

### `message`

Input: `task_id`, `content`. Validates the task exists, then **always**
returns a structured, non-error response:

```json
{
  "task_id": "...",
  "status": "unsupported",
  "reason": "Recollect Lines has no in-flight steering channel for any adapter: mock and OpenCode tasks both run to completion (or are cancelled outright). OpenCode itself does not support injecting a message into an already-running task.",
  "profile": "mock | opencode",
  "state": "..."
}
```

This is explicit and factual rather than a silently dropped call or a false
claim that OpenCode supports mid-run steering — it doesn't.

## Sample configuration / invocation

A generic MCP-client config entry (e.g. Claude Desktop-style `mcpServers`):

```json
{
  "mcpServers": {
    "recollect-lines": {
      "command": "recollect-mcp",
      "args": ["--home", "/absolute/path/to/.recollect"]
    }
  }
}
```

Manual smoke test over stdio (each line is one JSON-RPC message):

```
$ python -m recollect_lines.mcp_server --home .recollect
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"recollect-lines-mcp","version":"0.1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"delegate","arguments":{"task":"Inspect tests","workspace":"/repo"}}}
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{...envelope with task_id/state...}"}],"isError":false}}
```

## Security / privacy constraints

- The server never executes a shell: `verify_commands` are argv arrays run
  via `subprocess.run(command, cwd=..., ...)` with no `shell=True`, exactly
  as `Broker.verify()` already enforces (Phase 3).
- `status`/`collect` never expose more than the requesting task's own
  artifact manifest (filenames + sha256 + byte counts) and event metadata —
  there is no tool that lists or reads another task's artifacts, or any path
  outside the broker's own `--home` directory and the caller's own supplied
  `workspace`.
- No credentials, tokens, or environment variables are read, stored, or
  echoed by this module.
- The transport is local stdio only — no network listener is opened by this
  phase, and none is planned; that would be a new phase's decision.

## Tested behavior

`tests/test_mcp_server.py` drives a real `python -m recollect_lines.mcp_server`
subprocess over its actual stdio pipes (not direct calls into private
handler functions), covering:

1. Full `initialize` → `notifications/initialized` → `tools/list` →
   `tools/call` lifecycle, including a real `delegate`/`status`/`collect`
   round trip against the deterministic `mock` profile (no network/model
   call).
2. `ping`.
3. Multi-message framing: two pipelined requests sent before either
   response is read still come back in order, each matched to its own id.
4. A notification (`notifications/initialized`, and a malformed
   notification) produces no response, proven by a following `ping`'s
   response arriving as the very next line.
5. Blank lines between messages are ignored.
6. Malformed request handling: invalid JSON (parse error), wrong
   `jsonrpc` version, and a missing `method` all produce the documented
   JSON-RPC error codes.
7. An unknown top-level method, and an unknown `tools/call` tool name, and
   non-object `tools/call` arguments, each produce `-32601`/`-32602` as
   documented — never an isError tool result.
8. An unknown `task_id` for `status`/`collect`/`cancel`/`message` is an
   `isError: true` tool result (`KeyError`), not a JSON-RPC protocol error.
9. `delegate_batch` with one valid and one invalid item: the valid item is
   really created and started (confirmed via a follow-up `status` call),
   the invalid item is reported rejected, and the call itself is not an
   error.
10. `message` always returns the structured `unsupported` response.
11. `cancel` on a running mock task reaches `cancelled`.
12. `collect` with delegate-supplied `verify_commands` surfaces real,
    broker-verified command evidence (`broker_verified: true`, exit code 0)
    distinct from the (deterministically absent) mock runtime result.
13. `tools/list` advertises exactly the six documented tools, each with its
    documented required-field schema.

## Known limitations

- `verify_commands` supplied to `delegate` are persisted as a
  `verify_commands.json` artifact and read back by `collect` — durable
  across an MCP server restart, unlike the in-memory `_process_handles`
  table Phase 2 already documents as a known limitation. `collect` is *not*
  safely repeatable on the same task: `Broker.collect()` always calls
  `store.transition()` toward a terminal state, and no terminal state has
  any allowed outgoing transition (`models.ALLOWED_TRANSITIONS`), so a
  second `collect` call on an already-terminal task always raises
  `InvalidTransition` (surfaced as `isError: true`) rather than idempotently
  re-returning the prior result — this is unchanged `Broker` behavior, not
  an MCP-layer guarantee. A caller that wants a task's result again should
  use `status`, not call `collect` twice.
- For an `isolated_worktree` task, `collect`'s verification step must run
  before `broker.collect()` finalizes and releases the worktree. For the
  `opencode` profile this means: if `collect` is invoked while the
  underlying OpenCode subprocess is still writing to the worktree, the
  verification commands can race an in-progress edit. This mirrors the
  existing CLI's own `verify`/`collect` sequencing, which has always left
  timing to the caller (Phase 3) — Phase 4 does not add a new race, it
  automates the same two calls a human previously had to sequence by hand.
- `collect` on a `mock`-profile task always reaches `failed` with reason
  `missing_process_handle`; this is unchanged `Broker.collect()` behavior
  (mock never registers a process handle) and is not something the MCP
  layer special-cases or "fixes." There is currently no MCP tool that
  drives the CLI's `complete --summary` path for a mock task — mock is
  positioned here purely as a deterministic profile for exercising
  lifecycle/protocol behavior, not as an MCP-drivable adapter.
- One broker process (and therefore one MCP server process) per running
  task, as already documented in Phase 2/3 — an MCP server restart loses
  the in-memory OpenCode process handle exactly as the CLI does; the same
  process-group-alive safety net (Phase 3) still applies.
- No resources/prompts MCP capabilities, no partial/streaming tool results,
  and no batched JSON-RPC arrays (removed from the current MCP spec
  revision and not implemented here).
- `message` is a fixed, factual "unsupported" response — it does not queue,
  buffer, or forward its `content` anywhere; there is no mechanism, for any
  adapter, to steer a task after it has started.

## Future boundary

Phase 4 stops at a local, single-caller stdio MCP front door onto the
existing broker. Explicitly out of scope, deferred to a future phase if
ever needed: a network/HTTP MCP transport, authentication/authorization,
multi-caller/multi-tenant support, daemon/restart recovery for in-flight
processes, remote workers, and any mechanism for live task steering.
