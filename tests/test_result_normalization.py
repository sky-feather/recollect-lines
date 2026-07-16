"""MR 8.6: provenance-aware structured result normalization."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.claude_code_adapter import ClaudeCodeAdapter
from recollect_lines.codex_adapter import CodexAdapter
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.result_normalization import (
    NORMALIZED_RESULT_ARTIFACT,
    RAW_OUTPUT_ARTIFACT,
    SUPPORTED_RESULT_SCHEMAS,
    UnknownResultSchemaError,
    build_normalized_envelope,
    validate_result_schema,
)
from recollect_lines.service import Broker

FIXTURE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"
FIXTURE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def fake_codex_adapter(**kwargs):
    return CodexAdapter(command_prefix=(sys.executable, str(FIXTURE_CODEX)), grace_period_seconds=2.0, **kwargs)


def fake_claude_adapter(**kwargs):
    return ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE_CLAUDE)), grace_period_seconds=2.0, **kwargs)


SCHEMA_FIXTURES = {
    "plain-summary": "PLAIN_SUMMARY ok",
    "evidence-report": json.dumps({
        "summary": "evidence gathered",
        "findings": [{"id": "f1", "detail": "race in handler"}],
        "claimed_evidence": ["logs/trace.txt"],
        "commands_executed": ["pytest -q"],
        "unresolved_questions": ["is retry bounded?"],
    }),
    "review-findings": json.dumps({
        "summary": "architecture review complete",
        "findings": [{"severity": "medium", "topic": "coupling"}],
    }),
    "implementation-report": json.dumps({
        "summary": "implemented fix",
        "commands_executed": ["make test"],
        "tests_reported": [{"name": "unit", "passed": True}],
    }),
}


class ResultSchemaPolicyTests(unittest.TestCase):
    def test_supported_schemas_are_explicit(self):
        self.assertEqual(
            SUPPORTED_RESULT_SCHEMAS,
            frozenset({"plain-summary", "evidence-report", "review-findings", "implementation-report"}),
        )

    def test_unknown_schema_is_rejected(self):
        with self.assertRaises(UnknownResultSchemaError):
            validate_result_schema("investigation-report")

    def test_none_schema_is_allowed_at_validation(self):
        validate_result_schema(None)


class BrokerNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, codex_adapter=fake_codex_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _collect_mock(self, summary: str, *, result_schema: str | None = None):
        kwargs = {}
        if result_schema is not None:
            kwargs["result_schema"] = result_schema
            kwargs["explicit_fields"] = frozenset({"result_schema"})
        record = self.broker.create(TaskRequest("task", str(self.workspace), runtime="mock", **kwargs))
        self.broker.start(record.id)
        self.broker.complete(record.id, summary)
        return record

    def _collect_codex(self, prompt: str, *, result_schema: str):
        record = self.broker.create(TaskRequest(
            prompt,
            str(self.workspace),
            runtime="codex",
            result_schema=result_schema,
            explicit_fields=frozenset({"result_schema"}),
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        return record

    def _normalized(self, task_id: str) -> dict:
        path = self.broker.store.artifacts / task_id / NORMALIZED_RESULT_ARTIFACT
        self.assertTrue(path.is_file(), "expected normalized_result.json artifact")
        return json.loads(path.read_text())

    def test_unknown_schema_fails_before_launch(self):
        with self.assertRaises(UnknownResultSchemaError):
            self.broker.create(TaskRequest(
                "task",
                str(self.workspace),
                runtime="mock",
                result_schema="not-a-schema",
                explicit_fields=frozenset({"result_schema"}),
            ))

    def test_plain_summary_mock_remains_compatible(self):
        record = self._collect_mock("Found no failures")
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["envelope_version"], 1)
        self.assertEqual(envelope["parser"]["requested_schema"], "plain-summary")
        self.assertEqual(envelope["parser"]["parse_status"], "ok")
        self.assertEqual(envelope["runtime_reported"]["summary"], "Found no failures")
        self.assertIsNone(envelope["broker_observed"]["verification"])
        raw_path = self.broker.store.artifacts / record.id / RAW_OUTPUT_ARTIFACT
        self.assertTrue(raw_path.is_file())
        self.assertEqual(raw_path.read_text(), "Found no failures\n")

    def test_each_supported_schema_fixture(self):
        for schema, payload in SCHEMA_FIXTURES.items():
            with self.subTest(schema=schema):
                if schema == "plain-summary":
                    record = self._collect_mock(payload, result_schema=schema)
                else:
                    record = self._collect_codex(f"SCHEMA_{schema} {payload}", result_schema=schema)
                envelope = self._normalized(record.id)
                self.assertEqual(envelope["parser"]["requested_schema"], schema)
                self.assertIn(envelope["parser"]["parse_status"], {"ok", "partial", "fallback"})
                self.assertIn("summary", envelope["runtime_reported"])
                self.assertTrue(envelope["parser"]["raw_output_artifact"])

    def test_malformed_json_is_evidence_not_fabricated_success(self):
        record = self._collect_codex("MALFORMED", result_schema="evidence-report")
        envelope = self._normalized(record.id)
        self.assertIn(envelope["parser"]["parse_status"], {"fallback", "partial"})
        self.assertTrue(envelope["parser"]["warnings"])
        self.assertGreater(envelope["parser"]["malformed_output_lines"], 0)
        self.assertEqual(envelope["runtime_reported"]["summary"], "partial result despite a malformed line")

    def test_claude_code_exit_zero_plain_text_is_fallback_for_a_requested_structured_schema(self):
        # Wave 0 dogfood finding: a claude -p run that exits 0 with a clean,
        # well-formed JSON result line (process succeeds, is_error is False)
        # can still report plain prose in `result` rather than the JSON
        # object a structured result_schema expects — fake_claude.py's
        # default branch (no SCHEMA_ prefix) does exactly this. Execution
        # status and schema-parse status are deliberately asserted
        # separately here: SUCCEEDED/exit_code 0 is what the runtime
        # observed; "fallback" parse_status is what the normalizer made of
        # the *content*, and the two must not be conflated.
        broker = Broker(self.home / "claude", claude_code_adapter=fake_claude_adapter())
        try:
            record = broker.create(TaskRequest(
                "summarize the incident",
                str(self.workspace),
                runtime="claude_code",
                result_schema="evidence-report",
                explicit_fields=frozenset({"result_schema"}),
            ))
            broker.start(record.id)
            completed = broker.collect(record.id)
            self.assertEqual(completed.state, TaskState.SUCCEEDED)

            path = broker.store.artifacts / record.id / NORMALIZED_RESULT_ARTIFACT
            envelope = json.loads(path.read_text())
            self.assertEqual(envelope["broker_observed"]["exit_code"], 0)
            self.assertEqual(envelope["broker_observed"]["terminal_state"], TaskState.SUCCEEDED.value)
            self.assertEqual(envelope["parser"]["requested_schema"], "evidence-report")
            self.assertEqual(envelope["parser"]["parse_status"], "fallback")
            self.assertIn("summarize the incident", envelope["runtime_reported"]["summary"])
        finally:
            broker.close()

    def test_runtime_commands_are_not_broker_verified(self):
        payload = SCHEMA_FIXTURES["implementation-report"]
        record = self._collect_codex(f"SCHEMA_implementation-report {payload}", result_schema="implementation-report")
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["runtime_reported"]["claimed_commands"], ["make test"])
        self.assertIsNone(envelope["broker_observed"]["verification"])

    def test_broker_verification_only_when_broker_ran_commands(self):
        record = self.broker.create(
            TaskRequest("verify me", str(self.workspace), runtime="mock"),
            verify_commands=[[sys.executable, "-c", "print('ok')"]],
        )
        self.broker.start(record.id)
        self.broker.complete(record.id, "done")
        envelope = self._normalized(record.id)
        verification = envelope["broker_observed"]["verification"]
        self.assertIsNotNone(verification)
        self.assertTrue(verification["broker_verified"])
        self.assertTrue(all(cmd["broker_verified"] for cmd in verification["commands"]))

    def test_artifact_refs_are_hash_backed(self):
        record = self._collect_mock("summary", result_schema="plain-summary")
        envelope = self._normalized(record.id)
        refs = envelope["broker_observed"]["artifact_refs"]
        names = {item["name"] for item in refs}
        self.assertNotIn(NORMALIZED_RESULT_ARTIFACT, names)
        self.assertIn("result.json", names)
        for item in refs:
            self.assertRegex(item["sha256"], r"^[a-f0-9]{64}$")
            self.assertGreater(item["bytes"], 0)

    def test_normalized_result_excludes_self_and_manifest_matches_final_bytes(self):
        record = self._collect_mock("integrity check", result_schema="plain-summary")
        task_dir = self.broker.store.artifacts / record.id
        envelope = self._normalized(record.id)
        ref_names = {item["name"] for item in envelope["broker_observed"]["artifact_refs"]}
        self.assertNotIn(NORMALIZED_RESULT_ARTIFACT, ref_names)
        self.assertEqual(envelope["broker_observed"]["artifact_manifest_ref"], "manifest.json")

        manifest = json.loads((task_dir / "manifest.json").read_text())
        manifest_entry = next(
            (item for item in manifest["files"] if item["name"] == NORMALIZED_RESULT_ARTIFACT),
            None,
        )
        self.assertIsNotNone(manifest_entry, "manifest.json must list normalized_result.json")

        normalized_path = task_dir / NORMALIZED_RESULT_ARTIFACT
        on_disk = normalized_path.read_bytes()
        recomputed = hashlib.sha256(on_disk).hexdigest()
        self.assertEqual(recomputed, manifest_entry["sha256"])
        self.assertEqual(len(on_disk), manifest_entry["bytes"])

    def test_status_exposes_concise_normalized_view(self):
        record = self._collect_mock("summary", result_schema="plain-summary")
        status = self.broker.status(record.id)
        self.assertIn("normalized_result", status)
        self.assertEqual(status["normalized_result"]["requested_schema"], "plain-summary")
        self.assertNotIn("runtime_reported", status["normalized_result"])

    def test_profile_default_schema_is_validated(self):
        record = self.broker.create(TaskRequest(
            "inspect",
            str(self.workspace),
            runtime="mock",
            agent_profile="repository-investigator",
        ))
        self.assertEqual(record.result_schema, "evidence-report")

    def test_explicit_task_schema_overrides_profile_default(self):
        record = self.broker.create(TaskRequest(
            "inspect",
            str(self.workspace),
            runtime="mock",
            agent_profile="repository-investigator",
            result_schema="review-findings",
            explicit_fields=frozenset({"result_schema"}),
        ))
        self.assertEqual(record.result_schema, "review-findings")


if __name__ == "__main__":
    unittest.main()
