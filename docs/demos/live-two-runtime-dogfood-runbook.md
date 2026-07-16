# Live two-runtime dogfood acceptance (opt-in only)

This runbook describes a **future, operator-initiated** acceptance pass. It is **not**
executed by CI and **not** completed by the integrated fixture proof in MR 8.8.
Do not treat fixture acceptance as certification of every installed provider.

## Goal

Prove one host session can delegate heterogeneous **live** runtimes under a shared
`external_root_id` and bounded task tree:

| Child | Runtime | Behavioral role |
|-------|---------|-----------------|
| Repository investigation | Codex CLI | `repository-investigator` |
| Architecture review | Claude Code CLI | `architecture-reviewer` |

The host polls durable completion-event cursors, collects provenance-aware normalized
results, and creates a `relationship=continues` follow-up when in-flight steering is
refused.

## Prerequisites

- Recollect Lines installed from source (`pip install .`)
- Authenticated **Codex CLI** on `PATH` with quota for a short read-only task
- Authenticated **Claude Code CLI** on `PATH` with quota for a short read-only task
- A disposable Git repository (not your production checkout)
- Long-lived `recollect-mcp` process (subprocess `collect` requires the same broker instance that started each child)
- Operator acknowledgement that this consumes provider quota

## Exact commands (not run in CI)

```bash
export RECOLLECT_HOME="$PWD/.recollect-live-dogfood"
export FIXTURE_REPO="$PWD/tmp/live-dogfood-repo"
rm -rf "$RECOLLECT_HOME" "$FIXTURE_REPO"
mkdir -p "$FIXTURE_REPO" && git -C "$FIXTURE_REPO" init -q && \
  git -C "$FIXTURE_REPO" config user.email dogfood@example.com && \
  git -C "$FIXTURE_REPO" config user.name "Live Dogfood" && \
  echo "# probe" > "$FIXTURE_REPO/README.md" && \
  git -C "$FIXTURE_REPO" add README.md && git -C "$FIXTURE_REPO" commit -q -m init

# Terminal A — keep this running
recollect-mcp --home "$RECOLLECT_HOME"
```

In an MCP host (or `examples/` client), with `external_root_id` set to a stable host
session id:

1. `delegate` Codex read-only child — `runtime=codex`, `agent_profile=repository-investigator`, `result_schema=evidence-report`, bounded `timeout_seconds`
2. `delegate` Claude Code read-only child — `runtime=claude_code`, `agent_profile=architecture-reviewer`, `result_schema=review-findings`, same `external_root_id`
3. While children run, delegate a short host-side mock or read-only task sharing the same `external_root_id`
4. Poll `completion_events` with `after_event_id` cursor advancement until both children terminal
5. `collect` each child; confirm `normalized_result` plus separate raw evidence artifacts (`events.jsonl` / runtime output files)
6. `task_tree` for the broker root; confirm both children appear under the host tree
7. Attempt `message` / `control --action message` on a running child — expect explicit `unsupported_message_steering`
8. `delegate` a `relationship=continues` follow-up child from the refused task

CLI polling helper (offline against an existing home; does not start providers):

```bash
python3 examples/completion-event-polling/poll_completions.py --home "$RECOLLECT_HOME" --once
```

## Expected evidence (live)

- Durable SQLite tasks with shared `external_root_id` and distinct `runtime` / `agent_profile` / `result_schema`
- `completion_events` pages with monotonic `event_id`, compact `result_summary`, no raw log bodies
- `collect` payloads with `normalized_result` and hash-backed artifact refs
- Raw runtime evidence inspectable under `$RECOLLECT_HOME/artifacts/<task_id>/`
- `task_tree` JSON listing both runtime children
- Steering refusal JSON with `unsupported_message_steering`
- A new queued task with `relationship=continues` (not session resume)

## Failure handling

| Symptom | Action |
|---------|--------|
| `delegate` rejected at policy | Reduce concurrency, fix workspace path, or lower `timeout_seconds` |
| Child stuck `running` | `status` events; `cancel` with reason; inspect adapter stderr artifacts |
| `collect` unavailable after CLI exit | Use the same long-lived `recollect-mcp` session that called `delegate` |
| Auth / quota errors | Stop; do not retry in CI; record adapter stderr and `turn.failed` / `api_error_status` honestly |
| Steering attempted | Expect refusal; use `continues` follow-up instead of retrying `message` |

## Status

**Not performed** by the fixture proof or CI. Completing this runbook is an explicit
operator opt-in after MR 8.8 ships. No synthesized live-run evidence is committed here.
