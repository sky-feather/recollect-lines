"""Result-schema launch prompt contracts (Dogfood Readiness D3)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.claude_code_adapter import ClaudeCodeAdapter
from recollect_lines.codex_adapter import CodexAdapter
from recollect_lines.models import TaskRequest
from recollect_lines.result_normalization import SUPPORTED_RESULT_SCHEMAS, DEFAULT_RESULT_SCHEMA
from recollect_lines.result_schema_prompt import (
    RESULT_SCHEMA_PROMPT_VERSION,
    compose_launch_prompt,
    result_schema_prompt,
)
from recollect_lines.service import Broker

FIXTURE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"
FIXTURE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"

STRUCTURED_SCHEMAS = tuple(
    sorted(schema for schema in SUPPORTED_RESULT_SCHEMAS if schema != DEFAULT_RESULT_SCHEMA)
)


def fake_codex_adapter(**kwargs):
    return CodexAdapter(command_prefix=(sys.executable, str(FIXTURE_CODEX)), grace_period_seconds=2.0, **kwargs)


def fake_claude_adapter(**kwargs):
    return ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE_CLAUDE)), grace_period_seconds=2.0, **kwargs)


class ResultSchemaPromptContractTests(unittest.TestCase):
    def test_plain_summary_has_no_forced_json_contract(self):
        self.assertEqual(result_schema_prompt("plain-summary"), "")

    def test_review_findings_includes_versioned_required_fields(self):
        contract = result_schema_prompt("review-findings")
        self.assertIn(f"result-schema contract v{RESULT_SCHEMA_PROMPT_VERSION}", contract)
        self.assertIn("review-findings", contract)
        self.assertIn("summary (string)", contract)
        self.assertIn("findings (array of objects)", contract)
        self.assertIn("exactly one JSON object", contract)
        self.assertNotIn("```", contract)

    def test_structured_schema_contracts_share_central_source(self):
        contracts = {schema: result_schema_prompt(schema) for schema in STRUCTURED_SCHEMAS}
        marker = f"recollect-lines result-schema contract v{RESULT_SCHEMA_PROMPT_VERSION}"
        for schema, contract in contracts.items():
            with self.subTest(schema=schema):
                self.assertTrue(contract.startswith(f"[{marker}: {schema}]"))
                self.assertIn("exactly one JSON object", contract)
        self.assertEqual(len({id(contract) for contract in contracts.values()}), len(STRUCTURED_SCHEMAS))


class BrokerLaunchPromptContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, codex_adapter=fake_codex_adapter(), claude_code_adapter=fake_claude_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _prompt_from_command(self, runtime: str, command: list[str]) -> str:
        if runtime == "claude_code":
            return command[command.index("-p") + 1]
        return command[-1]

    def test_profile_selected_schema_appends_contract(self):
        record = self.broker.create(TaskRequest(
            "inspect module boundaries",
            str(self.workspace),
            runtime="mock",
            agent_profile="architecture-reviewer",
        ))
        self.assertEqual(record.result_schema, "review-findings")
        contract = result_schema_prompt("review-findings")
        self.broker.start(record.id)
        composed = json.loads((self.broker.store.artifacts / record.id / "composed_prompt.json").read_text())
        self.assertEqual(composed["result_schema"], "review-findings")
        self.assertEqual(composed["result_schema_source"], "profile_default")
        self.assertEqual(composed["result_schema_contract"], contract)
        self.assertEqual(composed["composed_prompt"], compose_launch_prompt(
            prompt_prefix=composed["prompt_prefix"],
            task_text=record.task,
            result_schema="review-findings",
        )[0])

    def test_explicit_task_schema_override_appends_selected_contract(self):
        record = self.broker.create(TaskRequest(
            "inspect handler",
            str(self.workspace),
            runtime="mock",
            agent_profile="repository-investigator",
            result_schema="implementation-report",
            explicit_fields=frozenset({"result_schema"}),
        ))
        contract = result_schema_prompt("implementation-report")
        self.broker.start(record.id)
        composed = json.loads((self.broker.store.artifacts / record.id / "composed_prompt.json").read_text())
        self.assertEqual(composed["result_schema"], "implementation-report")
        self.assertEqual(composed["result_schema_source"], "task_request")
        self.assertEqual(composed["result_schema_contract"], contract)
        self.assertIn(contract, composed["composed_prompt"])

    def test_plain_summary_profile_writes_composed_prompt_without_contract(self):
        record = self.broker.create(TaskRequest(
            "plan tests",
            str(self.workspace),
            runtime="mock",
            agent_profile="test-planner",
        ))
        self.broker.start(record.id)
        composed = json.loads((self.broker.store.artifacts / record.id / "composed_prompt.json").read_text())
        self.assertEqual(composed["result_schema"], "plain-summary")
        self.assertNotIn("result_schema_contract", composed)
        self.assertNotIn("exactly one JSON object", composed["composed_prompt"])

    def test_codex_fixture_command_receives_schema_contract(self):
        record = self.broker.create(TaskRequest(
            "trace handler path",
            str(self.workspace),
            runtime="codex",
            agent_profile="repository-investigator",
        ))
        contract = result_schema_prompt("evidence-report")
        self.broker.start(record.id)
        prompt = self._prompt_from_command("codex", self.broker._process_handles[record.id].command)
        self.assertIn(contract, prompt)
        self.broker.collect(record.id)

    def test_claude_fixture_command_receives_schema_contract(self):
        record = self.broker.create(TaskRequest(
            "review coupling",
            str(self.workspace),
            runtime="claude_code",
            agent_profile="architecture-reviewer",
        ))
        contract = result_schema_prompt("review-findings")
        self.broker.start(record.id)
        prompt = self._prompt_from_command("claude_code", self.broker._process_handles[record.id].command)
        self.assertIn(contract, prompt)
        self.broker.collect(record.id)

    def test_malformed_schema_response_retains_parser_warnings(self):
        record = self.broker.create(TaskRequest(
            "MALFORMED",
            str(self.workspace),
            runtime="codex",
            result_schema="evidence-report",
            explicit_fields=frozenset({"result_schema"}),
        ))
        contract = result_schema_prompt("evidence-report")
        self.broker.start(record.id)
        prompt = self._prompt_from_command("codex", self.broker._process_handles[record.id].command)
        self.assertIn(contract, prompt)
        self.broker.collect(record.id)
        envelope = json.loads((self.broker.store.artifacts / record.id / "normalized_result.json").read_text())
        self.assertIn(envelope["parser"]["parse_status"], {"fallback", "partial"})
        self.assertTrue(envelope["parser"]["warnings"])
        self.assertGreater(envelope["parser"]["malformed_output_lines"], 0)


if __name__ == "__main__":
    unittest.main()
