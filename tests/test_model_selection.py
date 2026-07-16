"""Per-task model selection gated by runtime registry capabilities (Phase 8.3)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.codex_adapter import CodexAdapter
from recollect_lines.direct_api_runtime import DIRECT_API_PROFILE
from recollect_lines.model_selection import (
    ModelSelectionRefusedError,
    resolve_effective_model,
    validate_requested_model,
)
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.runtime_registry import DEFAULT_RUNTIME_REGISTRY, ModelSelectionSupport
from recollect_lines.service import Broker

FIXTURE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"
FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "fake_openai_server.py"


def fake_codex_adapter(**kwargs):
    return CodexAdapter(command_prefix=(sys.executable, str(FIXTURE_CODEX)), grace_period_seconds=2.0, **kwargs)


class ModelSelectionPolicyTests(unittest.TestCase):
    def test_codex_runtime_allows_per_task_request(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("codex")
        self.assertEqual(descriptor.model_selection, ModelSelectionSupport.PER_TASK_REQUEST)

    def test_opencode_refuses_per_task_model(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("opencode")
        with self.assertRaises(ModelSelectionRefusedError):
            validate_requested_model(descriptor, "any-model")

    def test_mock_refuses_per_task_model(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("mock")
        with self.assertRaises(ModelSelectionRefusedError):
            validate_requested_model(descriptor, "any-model")

    def test_resolve_provider_default_without_task_override(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("openai_compatible")
        effective, source = resolve_effective_model(
            descriptor, requested_model=None, provider_default="provider-default",
        )
        self.assertEqual(effective, "provider-default")
        self.assertEqual(source, "provider_default")

    def test_resolve_task_override_for_direct_api(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("openai_compatible")
        effective, source = resolve_effective_model(
            descriptor, requested_model="override-model", provider_default="provider-default",
        )
        self.assertEqual(effective, "override-model")
        self.assertEqual(source, "task_request")


class CodexModelBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_task_model_reaches_fixture_command_without_mutating_shared_adapter(self):
        broker = Broker(self.home, codex_adapter=fake_codex_adapter(model="broker-default"))
        record_a = broker.create(TaskRequest("inspect A", str(self.workspace), runtime="codex", model="task-model-a"))
        record_b = broker.create(TaskRequest("inspect B", str(self.workspace), runtime="codex", model="task-model-b"))
        broker.start(record_a.id)
        broker.start(record_b.id)
        handle_a = broker._process_handles[record_a.id]
        handle_b = broker._process_handles[record_b.id]
        self.assertEqual(handle_a.command[handle_a.command.index("--model") + 1], "task-model-a")
        self.assertEqual(handle_b.command[handle_b.command.index("--model") + 1], "task-model-b")
        stored_a = broker.store.get(record_a.id)
        stored_b = broker.store.get(record_b.id)
        self.assertEqual(stored_a.model, "task-model-a")
        self.assertEqual(stored_a.effective_model, "task-model-a")
        self.assertEqual(stored_b.model, "task-model-b")
        self.assertEqual(stored_b.effective_model, "task-model-b")
        broker.collect(record_a.id)
        broker.collect(record_b.id)
        broker.close()

    def test_adapter_default_used_when_no_task_override(self):
        broker = Broker(self.home, codex_adapter=fake_codex_adapter(model="broker-default"))
        record = broker.create(TaskRequest("inspect", str(self.workspace), runtime="codex"))
        broker.start(record.id)
        handle = broker._process_handles[record.id]
        self.assertEqual(handle.command[handle.command.index("--model") + 1], "broker-default")
        self.assertIsNone(broker.store.get(record.id).model)
        self.assertEqual(broker.store.get(record.id).effective_model, "broker-default")
        broker.collect(record.id)
        broker.close()

    def test_model_evidence_in_running_event_without_provider_confirmation(self):
        broker = Broker(self.home, codex_adapter=fake_codex_adapter())
        record = broker.create(TaskRequest("inspect", str(self.workspace), runtime="codex", model="task-model"))
        broker.start(record.id)
        running = next(event for event in broker.store.events(record.id) if event["type"] == "task.running")
        evidence = running["metadata"]["model_selection"]
        self.assertEqual(evidence["requested_model"], "task-model")
        self.assertEqual(evidence["effective_model"], "task-model")
        self.assertTrue(evidence["invoked"])
        self.assertFalse(evidence["provider_confirmed"])
        broker.collect(record.id)
        broker.close()

    def test_effective_model_survives_broker_restart(self):
        broker = Broker(self.home, codex_adapter=fake_codex_adapter())
        created = broker.create(TaskRequest("inspect", str(self.workspace), runtime="codex", model="persist-model"))
        broker.start(created.id)
        broker.collect(created.id)
        broker.close()
        reloaded = Broker(self.home, codex_adapter=fake_codex_adapter())
        record = reloaded.store.get(created.id)
        self.assertEqual(record.model, "persist-model")
        self.assertEqual(record.effective_model, "persist-model")
        reloaded.close()

    def test_opencode_model_request_refused_at_create(self):
        broker = Broker(self.home)
        with self.assertRaises(ModelSelectionRefusedError):
            broker.create(TaskRequest("inspect", str(self.workspace), runtime="opencode", model="nope"))
        broker.close()

    def test_mock_model_request_refused_at_create(self):
        broker = Broker(self.home)
        with self.assertRaises(ModelSelectionRefusedError):
            broker.create(TaskRequest("inspect", str(self.workspace), runtime="mock", model="nope"))
        broker.close()


class DirectApiModelBrokerTests(unittest.TestCase):
    def setUp(self):
        import importlib.util

        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        spec = importlib.util.spec_from_file_location("fake_openai_server", FIXTURE_SERVER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.server = module.FakeOpenAiServer()
        self.server.start()
        self.env = {"TEST_OPENAI_API_KEY": "sk-fake-test-key-not-real"}
        config_path = Path(self.tempdir.name) / "providers.json"
        config_path.write_text(json.dumps(module.provider_document(self.server.base_url)) + "\n")
        self.broker = Broker(self.home, providers_config=config_path, environ=self.env)

    def tearDown(self):
        self.broker.close()
        self.server.stop()
        self.tempdir.cleanup()

    def test_task_model_override_reaches_request_payload(self):
        record = self.broker.create(TaskRequest(
            "hello", "/repo", runtime=DIRECT_API_PROFILE, provider="local", model="override-model",
        ))
        self.broker.start(record.id)
        payload = json.loads((self.home / "artifacts" / record.id / "request_payload.json").read_text())
        self.assertEqual(payload["payload"]["model"], "override-model")
        self.broker.collect(record.id)
        stored = self.broker.store.get(record.id)
        self.assertEqual(stored.model, "override-model")
        self.assertEqual(stored.effective_model, "override-model")

    def test_provider_default_without_task_override(self):
        record = self.broker.create(TaskRequest("hello", "/repo", runtime=DIRECT_API_PROFILE, provider="local"))
        self.broker.start(record.id)
        payload = json.loads((self.home / "artifacts" / record.id / "request_payload.json").read_text())
        self.assertEqual(payload["payload"]["model"], "fake-model")
        stored = self.broker.store.get(record.id)
        self.assertIsNone(stored.model)
        self.assertEqual(stored.effective_model, "fake-model")
        self.broker.collect(record.id)


if __name__ == "__main__":
    unittest.main()
