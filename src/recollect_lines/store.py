from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import ALLOWED_TRANSITIONS, InvalidTransition, TaskRecord, TaskState, WorkspaceLeaseConflict, now
from .durable_reconciliation import DEFAULT_LEASE_TTL_SECONDS, LAUNCH_KIND_LEGACY


class TaskStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.artifacts = home / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.connection = sqlite3.connect(home / "recollectlines.db", timeout=5, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                workspace TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                profile TEXT NOT NULL,
                provider TEXT,
                timeout_seconds INTEGER NOT NULL,
                verification_policy TEXT NOT NULL DEFAULT 'none',
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                timestamp TEXT NOT NULL,
                type TEXT NOT NULL,
                state_before TEXT,
                state_after TEXT,
                message TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_task_id_id ON events(task_id, id);
            CREATE TABLE IF NOT EXISTS workspace_leases (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                canonical_source TEXT NOT NULL,
                worktree_path TEXT NOT NULL,
                branch TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                released_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS workspace_leases_active_source
                ON workspace_leases(canonical_source) WHERE status = 'active';
            CREATE TABLE IF NOT EXISTS runtime_launches (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                adapter TEXT NOT NULL,
                adapter_label TEXT NOT NULL,
                pid INTEGER,
                pgid INTEGER,
                launched_at TEXT NOT NULL,
                command_json TEXT NOT NULL,
                workspace TEXT NOT NULL,
                events_artifact TEXT,
                stderr_artifact TEXT,
                reconciliation_marker TEXT NOT NULL DEFAULT 'unreconciled'
            );
            """
        )
        # Additive migration for a pre-5C `tasks` table (CREATE TABLE IF NOT
        # EXISTS above only applies to a brand-new database): backfill the
        # verification_policy column so existing rows default to "none" —
        # exactly Phase 1-5B's evidence-only behavior — with no data loss.
        existing_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)")}
        if "verification_policy" not in existing_columns:
            self.connection.execute("ALTER TABLE tasks ADD COLUMN verification_policy TEXT NOT NULL DEFAULT 'none'")
        if "provider" not in existing_columns:
            self.connection.execute("ALTER TABLE tasks ADD COLUMN provider TEXT")
        launch_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runtime_launches)")}
        if "durable_launch_id" not in launch_columns:
            self.connection.execute("ALTER TABLE runtime_launches ADD COLUMN durable_launch_id TEXT")
        if "launch_kind" not in launch_columns:
            self.connection.execute(
                "ALTER TABLE runtime_launches ADD COLUMN launch_kind TEXT NOT NULL DEFAULT 'legacy_subprocess'"
            )
        side_agent_columns = {
            "runtime": "TEXT",
            "model": "TEXT",
            "agent_profile": "TEXT",
            "result_schema": "TEXT",
            "effective_model": "TEXT",
        }
        for column, column_type in side_agent_columns.items():
            if column not in existing_columns:
                self.connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {column_type}")
        lineage_columns = {
            "parent_task_id": "TEXT",
            "root_task_id": "TEXT",
            "external_root_id": "TEXT",
            "delegation_depth": "INTEGER NOT NULL DEFAULT 0",
            "relationship": "TEXT",
            "origin_kind": "TEXT",
            "origin_ref": "TEXT",
        }
        for column, column_type in lineage_columns.items():
            if column not in existing_columns:
                self.connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {column_type}")
        self.connection.execute("UPDATE tasks SET root_task_id = id WHERE root_task_id IS NULL")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS tasks_parent_task_id ON tasks(parent_task_id) WHERE parent_task_id IS NOT NULL"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS tasks_root_task_id ON tasks(root_task_id) WHERE root_task_id IS NOT NULL"
        )
        self.connection.execute("UPDATE tasks SET runtime = profile WHERE runtime IS NULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS durable_recovery_leases (
                durable_launch_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
                broker_id TEXT NOT NULL,
                broker_epoch INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        data = dict(row)
        runtime = data.get("runtime") or data["profile"]
        data["runtime"] = runtime
        data["profile"] = runtime
        data["state"] = TaskState(data["state"])
        if data.get("root_task_id") is None:
            data["root_task_id"] = data["id"]
        if data.get("delegation_depth") is None:
            data["delegation_depth"] = 0
        return TaskRecord(**data)

    def create(self, record: TaskRecord) -> TaskRecord:
        with self.connection:
            self.connection.execute(
                "INSERT INTO tasks (id, task, workspace, execution_mode, runtime, profile, provider, "
                "timeout_seconds, verification_policy, state, created_at, updated_at, model, agent_profile, "
                "result_schema, effective_model, parent_task_id, root_task_id, external_root_id, delegation_depth, "
                "relationship, origin_kind, origin_ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.task,
                    record.workspace,
                    record.execution_mode,
                    record.runtime,
                    record.profile,
                    record.provider,
                    record.timeout_seconds,
                    record.verification_policy,
                    record.state.value,
                    record.created_at,
                    record.updated_at,
                    record.model,
                    record.agent_profile,
                    record.result_schema,
                    record.effective_model,
                    record.parent_task_id,
                    record.root_task_id or record.id,
                    record.external_root_id,
                    record.delegation_depth,
                    record.relationship,
                    record.origin_kind,
                    record.origin_ref,
                ),
            )
            self.event(record.id, "task.created", None, record.state, "Task accepted", {})
        (self.artifacts / record.id).mkdir(exist_ok=True)
        return record

    def get(self, task_id: str) -> TaskRecord:
        row = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return self._row_to_record(row)

    def list(self) -> list[TaskRecord]:
        rows = self.connection.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        return [self._row_to_record(row) for row in rows]

    def active_count(self, runtime: str) -> int:
        active = tuple(state.value for state in (TaskState.QUEUED, TaskState.PREPARING, TaskState.RUNNING, TaskState.COLLECTING, TaskState.CANCELLING))
        return self.connection.execute(
            f"SELECT COUNT(*) FROM tasks WHERE runtime = ? AND state IN ({','.join('?' for _ in active)})",
            (runtime, *active),
        ).fetchone()[0]

    def total_active_count(self) -> int:
        active = tuple(
            state.value
            for state in (
                TaskState.QUEUED,
                TaskState.PREPARING,
                TaskState.RUNNING,
                TaskState.COLLECTING,
                TaskState.CANCELLING,
                TaskState.RECOVERY_REQUIRED,
            )
        )
        return self.connection.execute(
            f"SELECT COUNT(*) FROM tasks WHERE state IN ({','.join('?' for _ in active)})",
            active,
        ).fetchone()[0]

    def child_count(self, parent_task_id: str) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?",
            (parent_task_id,),
        ).fetchone()[0]

    def list_children(self, parent_task_id: str) -> list[TaskRecord]:
        rows = self.connection.execute(
            "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY created_at, id",
            (parent_task_id,),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_tree_tasks(self, root_task_id: str, *, limit: int) -> list[TaskRecord]:
        rows = self.connection.execute(
            "SELECT * FROM tasks WHERE root_task_id = ? ORDER BY delegation_depth, created_at, id LIMIT ?",
            (root_task_id, limit),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def set_effective_model(self, task_id: str, effective_model: str | None) -> TaskRecord:
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET effective_model = ? WHERE id = ?",
                (effective_model, task_id),
            )
        return self.get(task_id)

    def transition(self, task_id: str, target: TaskState, message: str, metadata: dict[str, Any] | None = None) -> TaskRecord:
        record = self.get(task_id)
        if target not in ALLOWED_TRANSITIONS.get(record.state, set()):
            raise InvalidTransition(f"Cannot transition {record.state.value} to {target.value}")
        timestamp = now()
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, timestamp, task_id),
            )
            self.event(task_id, f"task.{target.value}", record.state, target, message, metadata or {})
        return self.get(task_id)

    def event(self, task_id: str, event_type: str, before: TaskState | None, after: TaskState | None, message: str, metadata: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO events (task_id, timestamp, type, state_before, state_after, message, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, now(), event_type, before.value if before else None, after.value if after else None, message, json.dumps(metadata, sort_keys=True)),
        )

    def events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata_json"])} for row in rows]

    def event_high_water_mark(self) -> int:
        row = self.connection.execute("SELECT COALESCE(MAX(id), 0) AS high_water FROM events").fetchone()
        return int(row["high_water"])

    def events_since(
        self,
        after_event_id: int,
        *,
        limit: int,
        task_id: str | None = None,
        root_task_id: str | None = None,
        state_after_in: frozenset[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if after_event_id < 0:
            raise ValueError("after_event_id must be non-negative")
        clauses = ["e.id > ?"]
        params: list[Any] = [after_event_id]
        if task_id is not None:
            clauses.append("e.task_id = ?")
            params.append(task_id)
        if root_task_id is not None:
            clauses.append("t.root_task_id = ?")
            params.append(root_task_id)
        if state_after_in is not None:
            placeholders = ",".join("?" for _ in state_after_in)
            clauses.append(f"e.state_after IN ({placeholders})")
            params.extend(sorted(state_after_in))
        where = " AND ".join(clauses)
        query = (
            "SELECT e.id, e.task_id, e.timestamp, e.type, e.state_before, e.state_after, e.message, e.metadata_json, "
            "t.runtime, t.model, t.effective_model, t.agent_profile, t.parent_task_id, t.root_task_id, "
            "t.external_root_id, t.delegation_depth, t.relationship, t.origin_kind, t.origin_ref "
            f"FROM events e JOIN tasks t ON e.task_id = t.id WHERE {where} "
            "ORDER BY e.id ASC LIMIT ?"
        )
        params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [dict(row) for row in rows], self.event_high_water_mark()

    def write_artifact(self, task_id: str, name: str, content: str | bytes) -> Path:
        if "/" in name or name in {"", ".", "..", "manifest.json"}:
            raise ValueError("Artifact name must be a simple non-manifest filename")
        path = self.artifacts / task_id / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        self.refresh_manifest(task_id)
        return path

    def acquire_lease(self, task_id: str, canonical_source: str, worktree_path: str, branch: str, base_sha: str) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO workspace_leases "
                    "(task_id, canonical_source, worktree_path, branch, base_sha, status, created_at, released_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, NULL)",
                    (task_id, canonical_source, worktree_path, branch, base_sha, now()),
                )
        except sqlite3.IntegrityError as error:
            raise WorkspaceLeaseConflict(
                f"Source workspace already has an active writer lease: {canonical_source}"
            ) from error

    def release_lease(self, task_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE workspace_leases SET status = 'released', released_at = ? WHERE task_id = ? AND status = 'active'",
                (now(), task_id),
            )

    def get_lease(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM workspace_leases WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def record_launch(
        self, task_id: str, *, adapter: str, adapter_label: str, pid: int | None, pgid: int | None,
        command: list[str], workspace: str, events_artifact: str | None, stderr_artifact: str | None,
        durable_launch_id: str | None = None,
        launch_kind: str = LAUNCH_KIND_LEGACY,
    ) -> None:
        """Persist durable launch identity the moment an adapter process is actually spawned.

        `command` should already be redacted by the caller; this stores whatever it is given.
        """
        with self.connection:
            self.connection.execute(
                "INSERT INTO runtime_launches "
                "(task_id, adapter, adapter_label, pid, pgid, launched_at, command_json, workspace, "
                "events_artifact, stderr_artifact, durable_launch_id, launch_kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, adapter, adapter_label, pid, pgid, now(), json.dumps(command), workspace,
                    events_artifact, stderr_artifact, durable_launch_id, launch_kind,
                ),
            )

    def get_launch(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM runtime_launches WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["command"] = json.loads(data.pop("command_json"))
        return data

    def mark_launch_reconciled(self, task_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runtime_launches SET reconciliation_marker = 'reconciled' WHERE task_id = ?",
                (task_id,),
            )

    def try_acquire_recovery_lease(
        self,
        *,
        task_id: str,
        durable_launch_id: str,
        broker_id: str,
        broker_epoch: int,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> bool:
        """Atomic recovery lease: one active reconciler per durable launch."""
        acquired_at = now()
        expires_at = self._lease_expiry_iso(ttl_seconds)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT broker_id, broker_epoch, expires_at FROM durable_recovery_leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO durable_recovery_leases "
                    "(durable_launch_id, task_id, broker_id, broker_epoch, acquired_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (durable_launch_id, task_id, broker_id, broker_epoch, acquired_at, expires_at),
                )
                return True
            if self._lease_expired(row["expires_at"]):
                self.connection.execute("DELETE FROM durable_recovery_leases WHERE task_id = ?", (task_id,))
                self.connection.execute(
                    "INSERT INTO durable_recovery_leases "
                    "(durable_launch_id, task_id, broker_id, broker_epoch, acquired_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (durable_launch_id, task_id, broker_id, broker_epoch, acquired_at, expires_at),
                )
                return True
            if row["broker_id"] == broker_id and row["broker_epoch"] == broker_epoch:
                self.connection.execute(
                    "UPDATE durable_recovery_leases SET expires_at = ?, durable_launch_id = ? WHERE task_id = ?",
                    (expires_at, durable_launch_id, task_id),
                )
                return True
            return False

    def release_recovery_lease(self, task_id: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM durable_recovery_leases WHERE task_id = ?", (task_id,))

    def get_recovery_lease(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM durable_recovery_leases WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _lease_expiry_iso(ttl_seconds: float) -> str:
        from datetime import UTC, datetime, timedelta

        return (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()

    @staticmethod
    def _lease_expired(expires_at: str) -> bool:
        from datetime import UTC, datetime

        try:
            return datetime.now(UTC) >= datetime.fromisoformat(expires_at)
        except ValueError:
            return True

    def refresh_manifest(self, task_id: str) -> Path:
        directory = self.artifacts / task_id
        if not directory.is_dir():
            raise KeyError(f"No artifact directory for task: {task_id}")
        files = []
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name == "manifest.json":
                continue
            payload = path.read_bytes()
            files.append({"name": path.name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        manifest = {"task_id": task_id, "generated_at": now(), "retention": "manual_cleanup", "files": files}
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return path

    def artifact_manifest(self, task_id: str) -> dict[str, Any]:
        path = self.artifacts / task_id / "manifest.json"
        if not path.is_file():
            raise KeyError(f"No artifact manifest for task: {task_id}")
        return json.loads(path.read_text())
