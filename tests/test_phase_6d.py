import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recollect_lines.council import (
    CouncilCostBudgetError,
    CouncilValidationError,
    execute_council,
    parse_council_plan,
    validate_council_plan,
)
from recollect_lines.discovery import discover_providers, discover_runtimes, select_candidates
from recollect_lines.models import DEFAULT_PROFILES, TaskRequest
from recollect_lines.providers import validate_providers_document
from recollect_lines.service import Broker

FIXTURE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"
FIXTURE_OPENAI = Path(__file__).parent / "fixtures" / "fake_openai_server.py"


def _fake_opencode_broker(home: Path) -> Broker:
    command = [sys.executable, str(FIXTURE_OPENCODE)]
    return Broker(home, opencode_adapter=__import__("recollect_lines.opencode_adapter", fromlist=["OpenCodeAdapter"]).OpenCodeAdapter(command_prefix=tuple(command)))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.broker = Broker(self.home)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_discover_runtimes_includes_subprocess_and_mock_kinds(self):
        inventory = discover_runtimes(
            profiles=self.broker.profiles,
            subprocess_adapters=self.broker.subprocess_adapters,
            mock_adapter=self.broker.adapter,
            direct_api_runtime=self.broker.direct_api_runtime,
        )
        by_name = {entry["name"]: entry for entry in inventory}
        self.assertEqual(by_name["mock"]["kind"], "synthetic")
        self.assertEqual(by_name["opencode"]["kind"], "subprocess_cli")
        self.assertEqual(by_name["openai_compatible"]["kind"], "direct_api")
        self.assertFalse(by_name["openai_compatible"]["observed_availability"]["available"])

    def test_discover_providers_reports_missing_credential(self):
        config_path = Path(self.tempdir.name) / "providers.json"
        config_path.write_text(json.dumps({
            "providers": {
                "local": {
                    "kind": "openai-compatible",
                    "base_url": "http://127.0.0.1:8765/v1",
                    "api_key_env": "MISSING_PHASE6D_KEY",
                    "default_model": "m",
                    "allow_insecure_http": True,
                }
            }
        }) + "\n")
        broker = Broker(self.home, providers_config=config_path, environ={})
        try:
            providers = discover_providers(direct_api_runtime=broker.direct_api_runtime, environ={})
            self.assertEqual(len(providers), 1)
            entry = providers[0]
            self.assertNotIn("base_url", entry)
            self.assertEqual(entry["endpoint_summary"]["host_class"], "loopback")
            self.assertFalse(entry["observed_availability"]["available"])
            self.assertEqual(entry["observed_availability"]["reason"], "missing_credential_reference")
        finally:
            broker.close()

    def test_select_filters_by_runtime_capability(self):
        result = select_candidates(
            profiles=self.broker.profiles,
            subprocess_adapters=self.broker.subprocess_adapters,
            direct_api_runtime=None,
            environ={},
            execution_mode="read_only",
            allowed_runtimes=["mock", "opencode"],
            required_runtime_capabilities={"synthetic_runtime": True},
            require_available=True,
        )
        self.assertEqual(result["eligible_runtimes"], ["mock"])
        excluded = {item["candidate"]: item for item in result["excluded"]}
        self.assertIn("opencode", excluded)

    def test_select_fails_closed_when_no_candidate_matches(self):
        with self.assertRaisesRegex(ValueError, "No runtime candidates"):
            select_candidates(
                profiles=self.broker.profiles,
                subprocess_adapters=self.broker.subprocess_adapters,
                direct_api_runtime=None,
                environ={},
                execution_mode="read_only",
                allowed_runtimes=["openai_compatible"],
                require_available=True,
            )

    def test_broker_discover_cli_surface(self):
        output = self.broker.discover_capabilities()
        self.assertIn("runtimes", output)
        self.assertIn("providers", output)
        self.assertIn("provider_config", output)
        self.assertEqual(output["provider_config"]["source"], "not_configured")


class CouncilValidationTests(unittest.TestCase):
    def test_rejects_cycle_and_self_critique(self):
        plan = {
            "workspace": "/repo",
            "acceptance_criteria": "parent judges",
            "bounds": {"max_rounds": 1, "max_concurrency": 2, "time_budget_seconds": 60},
            "stages": [
                {"id": "a", "role": "plan", "profile": "mock", "task": "plan a", "depends_on": ["b"]},
                {"id": "b", "role": "plan", "profile": "mock", "task": "plan b", "depends_on": ["a"]},
            ],
        }
        with self.assertRaises(CouncilValidationError):
            parse_council_plan(plan)

        self_critique = {
            "workspace": "/repo",
            "acceptance_criteria": "parent judges",
            "bounds": {"max_rounds": 1, "max_concurrency": 1, "time_budget_seconds": 60},
            "stages": [
                {"id": "plan", "role": "plan", "profile": "mock", "task": "plan"},
                {"id": "crit", "role": "critique", "profile": "mock", "task": "critique plan", "depends_on": ["plan"]},
            ],
        }
        with self.assertRaisesRegex(CouncilValidationError, "self-critique forbidden"):
            parse_council_plan(self_critique)

    def test_rejects_non_positive_bounds(self):
        plan = {
            "workspace": "/repo",
            "acceptance_criteria": "parent judges",
            "bounds": {"max_rounds": 0, "max_concurrency": 1, "time_budget_seconds": 60},
            "stages": [{"id": "a", "role": "plan", "profile": "mock", "task": "x"}],
        }
        with self.assertRaises(CouncilValidationError):
            parse_council_plan(plan)


class CouncilExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.broker = Broker(self.home)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _plan(self, **overrides):
        plan = {
            "workspace": "/repo",
            "execution_mode": "read_only",
            "acceptance_criteria": "Parent compares independent plans and decides.",
            "bounds": {"max_rounds": 1, "max_concurrency": 2, "time_budget_seconds": 120},
            "stages": [
                {"id": "plan_a", "role": "plan", "profile": "mock", "task": "Independent plan A"},
                {"id": "plan_b", "role": "plan", "profile": "mock", "task": "Independent plan B"},
            ],
        }
        plan.update(overrides)
        return plan

    def test_rejects_nonzero_cost_budget_without_configured_estimates(self):
        plan = self._plan(bounds={"max_rounds": 1, "max_concurrency": 2, "time_budget_seconds": 120, "cost_budget_usd": 1.0})
        with self.assertRaises(CouncilCostBudgetError) as ctx:
            validate_council_plan(self.broker, plan)
        self.assertEqual(ctx.exception.rejection["code"], "cost_budget_unmeasurable")
        self.assertEqual(len(ctx.exception.rejection["unmeasurable_stages"]), 2)

        result = execute_council(self.broker, plan)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["rejection"]["code"], "cost_budget_unmeasurable")
        self.assertEqual(result["stage_outcomes"], [])
        self.assertEqual(list(self.broker.store.list()), [])
        evidence_path = self.home / "artifacts" / result["council_id"] / "council_evidence.json"
        self.assertTrue(evidence_path.is_file())
        stored = json.loads(evidence_path.read_text())
        self.assertEqual(stored["bounds"]["cost_enforcement"], "rejected_unmeasurable")

    def test_accepts_unset_cost_budget(self):
        validation = validate_council_plan(self.broker, self._plan())
        self.assertEqual(validation["bounds"]["cost_enforcement"], "not_configured")
        result = execute_council(self.broker, self._plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["bounds"]["cost_enforcement"], "not_configured")

    def test_enforces_cost_budget_with_configured_provider_estimates(self):
        config_path = Path(self.tempdir.name) / "providers.json"
        config_path.write_text(json.dumps({
            "providers": {
                "local": {
                    "kind": "openai-compatible",
                    "base_url": "http://127.0.0.1:8765/v1",
                    "api_key_env": "LOCAL_KEY",
                    "default_model": "m",
                    "allow_insecure_http": True,
                    "estimated_cost_usd_upper_bound": 0.25,
                }
            }
        }) + "\n")
        broker = Broker(self.home, providers_config=config_path, environ={"LOCAL_KEY": "secret"})
        try:
            plan = {
                "workspace": "/repo",
                "execution_mode": "read_only",
                "acceptance_criteria": "Parent judges",
                "bounds": {"max_rounds": 1, "max_concurrency": 2, "time_budget_seconds": 120, "cost_budget_usd": 1.0},
                "stages": [
                    {"id": "a", "role": "plan", "profile": "openai_compatible", "provider": "local", "task": "A"},
                    {"id": "b", "role": "plan", "profile": "openai_compatible", "provider": "local", "task": "B"},
                ],
            }
            validation = validate_council_plan(broker, plan)
            self.assertEqual(validation["bounds"]["cost_enforcement"], "pre_dispatch_upper_bound")
            self.assertEqual(validation["bounds"]["estimated_cost_usd_upper_bound_total"], 0.5)

            over_budget = dict(plan)
            over_budget["bounds"] = dict(plan["bounds"])
            over_budget["bounds"]["cost_budget_usd"] = 0.4
            with self.assertRaises(CouncilCostBudgetError) as ctx:
                validate_council_plan(broker, over_budget)
            self.assertEqual(ctx.exception.rejection["code"], "cost_budget_exceeded_pre_dispatch")
        finally:
            broker.close()

    def test_execute_records_stage_evidence_without_winner(self):
        result = execute_council(self.broker, self._plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["stage_outcomes"]), 2)
        self.assertNotIn("winner", result)
        self.assertIn("note", result)
        evidence_path = self.home / "artifacts" / result["council_id"] / "council_evidence.json"
        self.assertTrue(evidence_path.is_file())
        stored = json.loads(evidence_path.read_text())
        self.assertEqual(stored["acceptance_criteria"], "Parent compares independent plans and decides.")

    def test_time_budget_skips_remaining_stages(self):
        plan = self._plan(bounds={"max_rounds": 1, "max_concurrency": 1, "time_budget_seconds": 60})
        monotonic = mock.Mock(side_effect=[0.0, 0.0, 100.0, 100.0])
        with mock.patch("recollect_lines.council.time.monotonic", monotonic):
            result = execute_council(self.broker, plan)
        self.assertTrue(any(item.get("reason") == "time_budget_exhausted" for item in result["skipped_stages"]))

    def test_subprocess_stage_uses_broker_collect_lifecycle(self):
        broker = _fake_opencode_broker(self.home)
        try:
            plan = {
                "workspace": "/repo",
                "execution_mode": "read_only",
                "acceptance_criteria": "Parent reviews opencode evidence",
                "bounds": {"max_rounds": 1, "max_concurrency": 1, "time_budget_seconds": 120},
                "stages": [{"id": "plan", "role": "plan", "profile": "opencode", "task": "Summarize workspace"}],
            }
            validate_council_plan(broker, plan)
            result = execute_council(broker, plan)
            outcome = result["stage_outcomes"][0]
            self.assertTrue(outcome["terminal"])
            self.assertEqual(outcome["state"], "succeeded")
            status = broker.status(outcome["task_id"])
            self.assertIn("result.json", [item["name"] for item in status["artifacts"]["files"]])
        finally:
            broker.close()


class McpPhase6DTests(unittest.TestCase):
    def test_tools_list_includes_phase_6d_tools(self):
        from recollect_lines.mcp_server import TOOLS

        self.assertIn("discover_capabilities", TOOLS)
        self.assertIn("select_candidates", TOOLS)
        self.assertIn("council_validate", TOOLS)
        self.assertIn("council_execute", TOOLS)


if __name__ == "__main__":
    unittest.main()
