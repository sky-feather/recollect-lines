"""Versioned behavioral agent profiles (Phase 8.4)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.agent_profiles import (
    AgentProfileConfig,
    AgentProfileError,
    UnknownAgentProfileError,
    compose_task_prompt,
    profile_content_hash,
    resolve_agent_profile,
)
from recollect_lines.codex_adapter import CodexAdapter
from recollect_lines.models import ProfilePolicy, TaskRequest, TaskState
from recollect_lines.service import Broker

FIXTURE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"


def fake_codex_adapter(**kwargs):
    return CodexAdapter(command_prefix=(sys.executable, str(FIXTURE_CODEX)), grace_period_seconds=2.0, **kwargs)


class ComposePromptTests(unittest.TestCase):
    def test_compose_is_deterministic(self):
        prefix = "You are a reviewer."
        task = "Inspect module X."
        self.assertEqual(compose_task_prompt(prefix, task), "You are a reviewer.\n\nInspect module X.")
        self.assertEqual(compose_task_prompt(prefix, task), compose_task_prompt(prefix, task))

    def test_empty_prefix_returns_task_only(self):
        self.assertEqual(compose_task_prompt("", "only task"), "only task")


class ResolutionPrecedenceTests(unittest.TestCase):
    def test_profile_defaults_apply_when_task_fields_implicit(self):
        profile = AgentProfileConfig(
            name="architecture-reviewer",
            prompt_prefix="review",
            default_result_schema="review-findings",
            default_execution_mode="isolated_worktree",
            default_timeout_seconds=2400,
        )
        resolved = resolve_agent_profile(
            profile=profile,
            explicit_fields=frozenset(),
            execution_mode="read_only",
            timeout_seconds=1800,
            result_schema=None,
            allowed_modes=frozenset({"read_only", "isolated_worktree"}),
            max_timeout_seconds=3600,
        )
        self.assertEqual(resolved.execution_mode, "isolated_worktree")
        self.assertEqual(resolved.timeout_seconds, 2400)
        self.assertEqual(resolved.result_schema, "review-findings")
        self.assertEqual(resolved.sources["execution_mode"], "profile_default")

    def test_explicit_task_values_override_profile_defaults(self):
        profile = AgentProfileConfig(
            name="architecture-reviewer",
            prompt_prefix="review",
            default_execution_mode="isolated_worktree",
            default_timeout_seconds=2400,
            default_result_schema="review-findings",
        )
        resolved = resolve_agent_profile(
            profile=profile,
            explicit_fields=frozenset({"execution_mode", "timeout_seconds", "result_schema"}),
            execution_mode="read_only",
            timeout_seconds=900,
            result_schema="review-findings",
            allowed_modes=frozenset({"read_only", "isolated_worktree"}),
            max_timeout_seconds=3600,
        )
        self.assertEqual(resolved.execution_mode, "read_only")
        self.assertEqual(resolved.timeout_seconds, 900)
        self.assertEqual(resolved.result_schema, "review-findings")
        self.assertEqual(resolved.sources["timeout_seconds"], "task_request")

    def test_broker_ceiling_rejects_excessive_timeout(self):
        profile = AgentProfileConfig(
            name="architecture-reviewer",
            prompt_prefix="review",
            default_timeout_seconds=7200,
        )
        with self.assertRaises(AgentProfileError):
            resolve_agent_profile(
                profile=profile,
                explicit_fields=frozenset(),
                execution_mode="read_only",
                timeout_seconds=1800,
                result_schema=None,
                allowed_modes=frozenset({"read_only"}),
                max_timeout_seconds=3600,
            )


class BrokerAgentProfileTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _prompt_from_command(self, command: list[str]) -> str:
        return command[-1]

    def test_same_profile_works_with_two_runtimes(self):
        for runtime in ("mock", "codex"):
            broker = Broker(
                self.home / runtime,
                codex_adapter=fake_codex_adapter() if runtime == "codex" else None,
            )
            record = broker.create(TaskRequest(
                "inspect shared path",
                str(self.workspace),
                runtime=runtime,
                agent_profile="repository-investigator",
            ))
            resolution = json.loads((broker.store.artifacts / record.id / "agent_profile_resolution.json").read_text())
            self.assertEqual(resolution["name"], "repository-investigator")
            self.assertIn("content_hash", resolution)
            if runtime == "codex":
                broker.start(record.id)
                handle = broker._process_handles[record.id]
                self.assertIn("repository investigator", self._prompt_from_command(handle.command).lower())
                broker.collect(record.id)
            else:
                broker.start(record.id)
                broker.complete(record.id, "mock summary")
            broker.close()

    def test_same_runtime_accepts_two_distinct_profiles(self):
        profiles = ("repository-investigator", "architecture-reviewer")
        hashes = []
        for name in profiles:
            broker = Broker(self.home / name, codex_adapter=fake_codex_adapter())
            record = broker.create(TaskRequest("inspect", str(self.workspace), runtime="codex", agent_profile=name))
            resolution = json.loads((broker.store.artifacts / record.id / "agent_profile_resolution.json").read_text())
            hashes.append(resolution["content_hash"])
            broker.start(record.id)
            prompt = self._prompt_from_command(broker._process_handles[record.id].command)
            self.assertIn(resolution["prompt_prefix"], prompt)
            broker.collect(record.id)
            broker.close()
        self.assertNotEqual(hashes[0], hashes[1])

    def test_unknown_profile_fails_before_launch(self):
        broker = Broker(self.home)
        with self.assertRaises(UnknownAgentProfileError):
            broker.create(TaskRequest("inspect", str(self.workspace), runtime="mock", agent_profile="missing-profile"))
        broker.close()

    def test_resolution_snapshot_survives_profile_registry_change(self):
        broker = Broker(self.home, codex_adapter=fake_codex_adapter())
        record = broker.create(TaskRequest(
            "inspect", str(self.workspace), runtime="codex", agent_profile="test-planner",
        ))
        artifact_path = broker.store.artifacts / record.id / "agent_profile_resolution.json"
        original = json.loads(artifact_path.read_text())
        original_hash = original["content_hash"]
        broker.agent_profiles["test-planner"] = AgentProfileConfig(
            name="test-planner",
            prompt_prefix="CHANGED PREFIX",
            default_timeout_seconds=999,
        )
        broker.start(record.id)
        stored = json.loads(artifact_path.read_text())
        composed = json.loads((broker.store.artifacts / record.id / "composed_prompt.json").read_text())
        self.assertEqual(stored["content_hash"], original_hash)
        self.assertEqual(composed["prompt_prefix"], original["prompt_prefix"])
        self.assertNotIn("CHANGED PREFIX", composed["composed_prompt"])
        broker.collect(record.id)
        broker.close()

    def test_task_timeout_override_beats_profile_default(self):
        broker = Broker(self.home)
        request = TaskRequest(
            "inspect",
            str(self.workspace),
            runtime="mock",
            agent_profile="repository-investigator",
            timeout_seconds=900,
            explicit_fields=frozenset({"timeout_seconds"}),
        )
        record = broker.create(request)
        self.assertEqual(record.timeout_seconds, 900)
        resolution = json.loads((broker.store.artifacts / record.id / "agent_profile_resolution.json").read_text())
        self.assertEqual(resolution["resolved"]["timeout_seconds"], 900)
        self.assertEqual(resolution["sources"]["timeout_seconds"], "task_request")
        broker.close()

    def test_legacy_profile_codex_not_treated_as_agent_profile(self):
        broker = Broker(self.home)
        record = broker.create(TaskRequest("inspect", str(self.workspace), profile="codex"))
        self.assertEqual(record.runtime, "codex")
        self.assertIsNone(record.agent_profile)
        self.assertFalse((broker.store.artifacts / record.id / "agent_profile_resolution.json").exists())
        broker.close()

    def test_discover_lists_agent_profiles(self):
        broker = Broker(self.home)
        inventory = broker.discover_capabilities()
        names = {entry["name"] for entry in inventory["agent_profiles"]}
        self.assertIn("repository-investigator", names)
        self.assertIn("architecture-reviewer", names)
        broker.close()


class ProfileHashTests(unittest.TestCase):
    def test_content_hash_changes_when_profile_body_changes(self):
        first = AgentProfileConfig(name="x", prompt_prefix="a")
        second = AgentProfileConfig(name="x", prompt_prefix="b")
        self.assertNotEqual(profile_content_hash(first), profile_content_hash(second))


if __name__ == "__main__":
    unittest.main()
