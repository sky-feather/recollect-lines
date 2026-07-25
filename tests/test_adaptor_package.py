"""Regression tests for recollect_lines.adaptor package layout and registry wiring."""

from __future__ import annotations

import importlib
import unittest
from pathlib import Path

import recollect_lines.adaptor as adaptor_pkg
from recollect_lines.adaptor import (
    AdapterCapabilities,
    ClaudeCodeAdapter,
    CodexAdapter,
    CursorAdapter,
    FixtureDurableAdapter,
    OpenCodeAdapter,
    RuntimeAdapter,
)
from recollect_lines.runtime_registry import DEFAULT_RUNTIME_REGISTRY

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "recollect_lines"
LEGACY_ROOT_MODULES = (
    "adapters.py",
    "claude_code_adapter.py",
    "codex_adapter.py",
    "cursor_adapter.py",
    "opencode_adapter.py",
    "fixture_durable_adapter.py",
)


class AdaptorPackageTests(unittest.TestCase):
    def test_legacy_root_adaptor_modules_are_absent(self):
        for module_name in LEGACY_ROOT_MODULES:
            with self.subTest(module=module_name):
                self.assertFalse((SRC_ROOT / module_name).exists())

    def test_registered_runtimes_resolve_to_expected_implementations(self):
        expected = {
            "opencode": OpenCodeAdapter,
            "claude_code": ClaudeCodeAdapter,
            "codex": CodexAdapter,
            "cursor": CursorAdapter,
        }
        for runtime_name, adapter_cls in expected.items():
            with self.subTest(runtime=runtime_name):
                descriptor = DEFAULT_RUNTIME_REGISTRY.get(runtime_name)
                self.assertIs(descriptor.adapter_capabilities, adapter_cls.capabilities)

    def test_no_duplicate_runtime_registrations(self):
        names = DEFAULT_RUNTIME_REGISTRY.names()
        self.assertEqual(len(names), len(set(names)))

    def test_adaptor_package_exports_stable_surface(self):
        expected = {
            "AdapterCapabilities",
            "RuntimeAdapter",
            "OpenCodeAdapter",
            "ClaudeCodeAdapter",
            "CodexAdapter",
            "CursorAdapter",
            "FixtureDurableAdapter",

            "group_alive",
            "redact_command",
        }
        self.assertTrue(expected.issubset(set(adaptor_pkg.__all__)))

    def test_adaptor_submodules_import_without_circular_dependency(self):
        for module_name in (
            "recollect_lines.adaptor.contracts",
            "recollect_lines.adaptor.process",
            "recollect_lines.adaptor.cli_base",
            "recollect_lines.adaptor.opencode",
            "recollect_lines.adaptor.claude_code",
            "recollect_lines.adaptor.codex",
            "recollect_lines.adaptor.cursor",
            "recollect_lines.adaptor.fixture_durable",
        ):
            with self.subTest(module=module_name):
                importlib.import_module(module_name)

    # test_process_helpers_exported_from_package retired (RFC-004
    # durable-opencode slice): it asserted that adaptor.opencode re-exported
    # generic process helpers it happened to import for its own pre-durable
    # Popen lifecycle. Runtime modules no longer expose that implementation
    # lifecycle. The durable OpenCodeAdapter no longer touches a process
    # directly (see adaptor/opencode.py's module docstring) and so no longer
    # imports them -- matching adaptor/codex.py and adaptor/claude_code.py,
    # which never re-exported them either. The package-level contract
    # (test_adaptor_package_exports_stable_surface above) is what actually
    # matters and is unaffected: those helpers are sourced directly from
    # `.process` in adaptor/__init__.py, independent of any single adapter
    # module.

if __name__ == "__main__":
    unittest.main()
