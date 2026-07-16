# Phase 1 — Broker Core

## Scope

Phase 1 implements provider-neutral lifecycle mechanics. It intentionally does not invoke OpenCode, expose MCP, or manage real subprocesses.

## Acceptance criteria

1. A task receives a stable ID and durable SQLite record.
2. Every state transition is validated and written as an append-only event.
3. Task artifacts are stored under a task-specific directory.
4. Cancellation reaches a terminal `cancelled` state through the lifecycle graph.
5. Data survives reconstruction of a service instance.
6. The local CLI can create, start, complete, cancel, inspect, and list mock tasks.

## Non-goals

- Real runtime adapters and process groups (Phase 2).
- Git worktree allocation and leases (Phase 3).
- MCP (Phase 4).
