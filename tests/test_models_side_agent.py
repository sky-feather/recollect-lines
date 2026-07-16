import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from recollect_lines import cli
from recollect_lines.models import (
    LegacyProfileConflictError,
    TaskRecord,
    TaskRequest,
    effective_runtime,
    legacy_profile_compatibility_metadata,
    request_artifact_payload,
    translate_delegate_fields,
)
from recollect_lines.service import Broker
from recollect_lines.store import TaskStore


class TranslateDelegateFieldsTests(unittest.TestCase):
    def test_runtime_only_not_deprecated(self):
        runtime, model, agent_profile, result_schema, compatibility = translate_delegate_fields(runtime="codex")
        self.assertEqual(runtime, "codex")
        self.assertIsNone(compatibility)
        self.assertIsNone(model)
        self.assertIsNone(agent_profile)
        self.assertIsNone(result_schema)

    def test_legacy_profile_codex_translated(self):
        runtime, _, _, _, compatibility = translate_delegate_fields(profile="codex")
        self.assertEqual(runtime, "codex")
        self.assertEqual(compatibility, legacy_profile_compatibility_metadata())

    def test_unknown_legacy_profile_rejected(self):
        with self.assertRaisesRegex(ValueError, "architecture-reviewer"):
            translate_delegate_fields(profile="architecture-reviewer")

    def test_runtime_and_agent_profile_separate(self):
        runtime, model, agent_profile, result_schema, compatibility = translate_delegate_fields(
            runtime="codex",
            agent_profile="architecture-reviewer",
            model="fixture-model",
            result_schema="evidence-report",
        )
        self.assertEqual(runtime, "codex")
        self.assertEqual(agent_profile, "architecture-reviewer")
        self.assertEqual(model, "fixture-model")
        self.assertEqual(result_schema, "evidence-report")
        self.assertIsNone(compatibility)

    def test_same_runtime_and_profile_deprecated(self):
        runtime, _, _, _, compatibility = translate_delegate_fields(runtime="codex", profile="codex")
        self.assertEqual(runtime, "codex")
        self.assertEqual(compatibility, legacy_profile_compatibility_metadata())

    def test_conflicting_runtime_and_profile_rejected(self):
        with self.assertRaises(LegacyProfileConflictError):
            translate_delegate_fields(runtime="codex", profile="claude_code")

    def test_implicit_default_mock_not_deprecated(self):
        runtime, _, _, _, compatibility = translate_delegate_fields()
        self.assertEqual(runtime, "mock")
        self.assertIsNone(compatibility)


class PersistenceAndMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_new_fields_round_trip_and_restart(self):
        broker = Broker(self.home)
        request = TaskRequest(
            "inspect",
            "/repo",
            runtime="codex",
            model="fixture-model",
            agent_profile="architecture-reviewer",
            result_schema="evidence-report",
            explicit_fields=frozenset({"result_schema", "model", "agent_profile"}),
        )
        created = broker.create(request)
        broker.close()

        reloaded = Broker(self.home)
        record = reloaded.store.get(created.id)
        self.assertEqual(record.runtime, "codex")
        self.assertEqual(record.profile, "codex")
        self.assertEqual(record.model, "fixture-model")
        self.assertEqual(record.agent_profile, "architecture-reviewer")
        self.assertEqual(record.result_schema, "evidence-report")
        request_artifact = json.loads((reloaded.store.artifacts / created.id / "request.json").read_text())
        self.assertEqual(request_artifact["runtime"], "codex")
        self.assertEqual(request_artifact["model"], "fixture-model")
        self.assertNotIn("compatibility", request_artifact)
        reloaded.close()

    def test_idempotent_migration_on_fresh_db(self):
        store = TaskStore(self.home)
        columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(tasks)")}
        self.assertIn("runtime", columns)
        self.assertIn("model", columns)
        self.assertIn("effective_model", columns)
        self.assertIn("agent_profile", columns)
        self.assertIn("result_schema", columns)
        store.close()
        store2 = TaskStore(self.home)
        columns2 = {row["name"] for row in store2.connection.execute("PRAGMA table_info(tasks)")}
        self.assertEqual(columns, columns2)
        store2.close()

    def test_backfill_legacy_profile_row(self):
        db_path = self.home / "recollectlines.db"
        self.home.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE tasks (
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
            INSERT INTO tasks VALUES (
                'tsk_legacy', 'old task', '/repo', 'read_only', 'codex', NULL,
                1800, 'none', 'queued', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00'
            );
            """
        )
        connection.commit()
        connection.close()

        store = TaskStore(self.home)
        record = store.get("tsk_legacy")
        self.assertEqual(record.runtime, "codex")
        self.assertEqual(record.profile, "codex")
        self.assertEqual(store.active_count("codex"), 1)
        store.close()

    def test_backfill_legacy_row_active_count_after_reopen(self):
        db_path = self.home / "recollectlines.db"
        self.home.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE tasks (
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
            INSERT INTO tasks VALUES (
                'tsk_legacy', 'old task', '/repo', 'read_only', 'codex', NULL,
                1800, 'none', 'queued', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00'
            );
            """
        )
        connection.commit()
        connection.close()

        store = TaskStore(self.home)
        store.close()
        store2 = TaskStore(self.home)
        self.assertEqual(store2.active_count("codex"), 1)
        store2.close()

    def test_legacy_request_artifact_has_compatibility_without_secrets(self):
        broker = Broker(self.home)
        runtime, model, agent_profile, result_schema, compatibility = translate_delegate_fields(profile="codex")
        request = TaskRequest(
            "inspect",
            "/repo",
            runtime=runtime,
            profile=runtime,
            model=model,
            agent_profile=agent_profile,
            result_schema=result_schema,
            compatibility=compatibility,
        )
        created = broker.create(request)
        payload = json.loads((broker.store.artifacts / created.id / "request.json").read_text())
        self.assertEqual(payload["compatibility"], legacy_profile_compatibility_metadata())
        self.assertNotIn("api_key", json.dumps(payload))
        broker.close()


class EffectiveRuntimeTests(unittest.TestCase):
    def test_internal_profile_still_works_for_tests(self):
        request = TaskRequest("task", "/repo", profile="codex")
        self.assertEqual(effective_runtime(request), "codex")

    def test_record_new_uses_effective_runtime(self):
        request = TaskRequest("task", "/repo", profile="codex", agent_profile="architecture-reviewer")
        record = TaskRecord.new(request)
        self.assertEqual(record.runtime, "codex")
        self.assertEqual(record.profile, "codex")
        self.assertEqual(record.agent_profile, "architecture-reviewer")


class CliRuntimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _create_stdout(self, *extra_args: str) -> dict:
        buf = io.StringIO()
        args = ["--home", str(self.home), "create", "--task", "cli task", "--workspace", str(self.workspace), *extra_args]
        with redirect_stdout(buf):
            exit_code = cli.main(args)
        self.assertEqual(exit_code, 0)
        return json.loads(buf.getvalue())

    def test_cli_runtime_and_side_agent_fields(self):
        output = self._create_stdout(
            "--runtime", "codex",
            "--model", "fixture-model",
            "--agent-profile", "architecture-reviewer",
            "--result-schema", "evidence-report",
        )
        self.assertEqual(output["runtime"], "codex")
        self.assertEqual(output["model"], "fixture-model")
        self.assertEqual(output["agent_profile"], "architecture-reviewer")
        self.assertEqual(output["result_schema"], "evidence-report")
        self.assertNotIn("compatibility", output)

    def test_cli_legacy_profile_echoes_compatibility(self):
        output = self._create_stdout("--profile", "codex")
        self.assertEqual(output["runtime"], "codex")
        self.assertEqual(output["compatibility"], legacy_profile_compatibility_metadata())

    def test_cli_root_create_defaults_origin_kind_host(self):
        output = self._create_stdout()
        self.assertEqual(output["origin_kind"], "host")

    def test_cli_parented_create_defaults_origin_kind_host(self):
        parent = self._create_stdout()
        child = self._create_stdout("--parent-task-id", parent["id"])
        self.assertEqual(child["origin_kind"], "host")
        self.assertEqual(child["parent_task_id"], parent["id"])


if __name__ == "__main__":
    unittest.main()
