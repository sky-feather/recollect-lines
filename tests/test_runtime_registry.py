"""Tests for the central runtime registry (Phase 8.2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recollect_lines.adapters import AdapterCapabilities
from recollect_lines.discovery import discover_runtimes
from recollect_lines.models import ProfilePolicy, translate_delegate_fields
from recollect_lines.mcp_server import handle_discover_capabilities
from recollect_lines.recovery_contract import SYNTHETIC_RECOVERY_CONTROL
from recollect_lines.runtime_registry import (
    DEFAULT_RUNTIME_REGISTRY,
    DuplicateRuntimeRegistrationError,
    ExecutionStrategy,
    ModelSelectionSupport,
    RuntimeDescriptor,
    RuntimeRegistry,
    SUBPROCESS_LIMITATIONS,
    UnknownRuntimeError,
    resolve_runtime_label,
)
from recollect_lines.service import Broker


FIXTURE_TEST_PROFILE = ProfilePolicy(
    "fixture_test_runtime",
    frozenset({"read_only"}),
    3600,
    2,
)


class RuntimeRegistryTests(unittest.TestCase):
    def test_default_registry_lists_standard_runtimes(self):
        names = DEFAULT_RUNTIME_REGISTRY.names()
        self.assertIn("mock", names)
        self.assertIn("opencode", names)
        self.assertIn("claude_code", names)
        self.assertIn("codex", names)
        self.assertIn("cursor", names)
        self.assertIn("openai_compatible", names)

    def test_duplicate_registration_is_explicit(self):
        registry = RuntimeRegistry()
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("mock")
        registry.register(descriptor)
        with self.assertRaises(DuplicateRuntimeRegistrationError):
            registry.register(descriptor)

    def test_unknown_runtime_lookup_is_explicit(self):
        with self.assertRaises(UnknownRuntimeError):
            DEFAULT_RUNTIME_REGISTRY.get("not-a-runtime")

    def test_fixture_runtime_registers_and_discovers_without_interface_constants(self):
        registry = DEFAULT_RUNTIME_REGISTRY.copy()
        registry.register(RuntimeDescriptor(
            name=FIXTURE_TEST_PROFILE.name,
            execution_strategy=ExecutionStrategy.FIXTURE,
            policy=FIXTURE_TEST_PROFILE,
            adapter_capabilities=AdapterCapabilities(
                requires_subprocess=True,
                supports_process_group_cancellation=True,
                reports_broker_verified_tests=False,
                recovery_control=SYNTHETIC_RECOVERY_CONTROL,
            ),
            limitations=SUBPROCESS_LIMITATIONS,
            model_selection=ModelSelectionSupport.PERSISTED_NOT_INVOKED,
            runtime_label="fixture_test_runtime",
        ))
        runtime, _, _, _, _ = translate_delegate_fields(
            runtime=FIXTURE_TEST_PROFILE.name,
            runtime_registry=registry,
        )
        self.assertEqual(runtime, FIXTURE_TEST_PROFILE.name)
        class _FakeAdapter:
            name = FIXTURE_TEST_PROFILE.name
            capabilities = AdapterCapabilities(
                requires_subprocess=True,
                supports_process_group_cancellation=True,
                reports_broker_verified_tests=False,
                recovery_control=SYNTHETIC_RECOVERY_CONTROL,
            )

        inventory = {
            entry["name"]: entry
            for entry in discover_runtimes(
                registry=registry,
                subprocess_adapters={FIXTURE_TEST_PROFILE.name: _FakeAdapter()},
                direct_api_runtime=None,
            )
        }
        self.assertIn(FIXTURE_TEST_PROFILE.name, inventory)
        self.assertEqual(inventory[FIXTURE_TEST_PROFILE.name]["execution_strategy"], "fixture")

    def test_direct_api_execution_strategy_is_distinct(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("openai_compatible")
        self.assertEqual(descriptor.execution_strategy, ExecutionStrategy.DIRECT_API)
        self.assertFalse(descriptor.adapter_capabilities.requires_subprocess)

    def test_subprocess_execution_strategy_is_distinct(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("codex")
        self.assertEqual(descriptor.execution_strategy, ExecutionStrategy.SUBPROCESS_CLI)
        self.assertTrue(descriptor.adapter_capabilities.requires_subprocess)
        self.assertEqual(descriptor.model_selection, ModelSelectionSupport.PER_TASK_REQUEST)

    def test_opencode_model_selection_is_persisted_not_invoked(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("opencode")
        self.assertEqual(descriptor.model_selection, ModelSelectionSupport.PERSISTED_NOT_INVOKED)


class RuntimeDiscoveryLabelSerializationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.broker = Broker(self.home)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_default_subprocess_descriptors_carry_no_property_descriptors(self):
        for descriptor in DEFAULT_RUNTIME_REGISTRY.descriptors():
            if descriptor.execution_strategy is not ExecutionStrategy.SUBPROCESS_CLI:
                continue
            label = descriptor.runtime_label
            self.assertTrue(label is None or isinstance(label, str), descriptor.name)

    def test_discover_runtimes_entries_are_json_serializable(self):
        entries = discover_runtimes(
            subprocess_adapters=self.broker.subprocess_adapters,
            direct_api_runtime=self.broker.direct_api_runtime,
        )
        serialized = json.dumps(entries)
        self.assertIsInstance(serialized, str)
        self.assertGreater(len(entries), 0)

    def test_mcp_discover_capabilities_is_json_serializable(self):
        payload = handle_discover_capabilities(self.broker, {})
        json.dumps(payload)

    def test_builtin_runtime_labels_are_strings(self):
        inventory = {
            entry["name"]: entry
            for entry in discover_runtimes(
                subprocess_adapters=self.broker.subprocess_adapters,
                direct_api_runtime=self.broker.direct_api_runtime,
            )
        }
        for runtime_name in ("codex", "claude_code", "cursor", "opencode"):
            self.assertIn(runtime_name, inventory)
            label = inventory[runtime_name]["runtime_label"]
            self.assertIsInstance(label, str, runtime_name)
            self.assertTrue(label, runtime_name)

    def test_custom_fixture_descriptor_label_resolves_from_adapter(self):
        registry = DEFAULT_RUNTIME_REGISTRY.copy()
        registry.register(RuntimeDescriptor(
            name=FIXTURE_TEST_PROFILE.name,
            execution_strategy=ExecutionStrategy.FIXTURE,
            policy=FIXTURE_TEST_PROFILE,
            adapter_capabilities=AdapterCapabilities(
                requires_subprocess=True,
                supports_process_group_cancellation=True,
                reports_broker_verified_tests=False,
                recovery_control=SYNTHETIC_RECOVERY_CONTROL,
            ),
            limitations=SUBPROCESS_LIMITATIONS,
            model_selection=ModelSelectionSupport.PERSISTED_NOT_INVOKED,
            runtime_label="fixture_test_runtime",
        ))

        class _FakeAdapter:
            name = FIXTURE_TEST_PROFILE.name
            capabilities = AdapterCapabilities(
                requires_subprocess=True,
                supports_process_group_cancellation=True,
                reports_broker_verified_tests=False,
                recovery_control=SYNTHETIC_RECOVERY_CONTROL,
            )

        inventory = {
            entry["name"]: entry
            for entry in discover_runtimes(
                registry=registry,
                subprocess_adapters={FIXTURE_TEST_PROFILE.name: _FakeAdapter()},
                direct_api_runtime=None,
            )
        }
        self.assertEqual(
            inventory[FIXTURE_TEST_PROFILE.name]["runtime_label"],
            "fixture_test_runtime",
        )

    def test_non_string_dynamic_label_falls_back_to_runtime_name(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("codex")

        class _BrokenAdapter:
            name = "codex"

            @property
            def runtime_label(self):
                return 42

        self.assertEqual(resolve_runtime_label(descriptor, _BrokenAdapter()), "codex")
        entry = next(
            item
            for item in discover_runtimes(
                subprocess_adapters={"codex": _BrokenAdapter()},
                direct_api_runtime=None,
            )
            if item["name"] == "codex"
        )
        self.assertEqual(entry["runtime_label"], "codex")


if __name__ == "__main__":
    unittest.main()
