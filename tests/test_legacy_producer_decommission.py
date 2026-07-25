import ast
import inspect
import unittest
from pathlib import Path

import recollect_lines.adaptor as adaptor_package
from recollect_lines.adaptor.claude_code import ClaudeCodeAdapter
from recollect_lines.adaptor.codex import CodexAdapter
from recollect_lines.adaptor.cursor import CursorAdapter
from recollect_lines.adaptor.opencode import OpenCodeAdapter


PRODUCTION_CLI_ADAPTERS = (CursorAdapter, ClaudeCodeAdapter, CodexAdapter, OpenCodeAdapter)


class LegacyProducerDecommissionTests(unittest.TestCase):
    def test_no_production_adapter_constructor_accepts_legacy_launch_opt_in(self):
        for adapter in PRODUCTION_CLI_ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                self.assertNotIn("legacy_popen_launch", inspect.signature(adapter).parameters)

    def test_no_production_cli_adapter_calls_subprocess_popen(self):
        for adapter in PRODUCTION_CLI_ADAPTERS:
            path = Path(inspect.getsourcefile(adapter))
            tree = ast.parse(path.read_text())
            popen_calls = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                if isinstance(target, ast.Attribute) and target.attr == "Popen":
                    popen_calls.append(node.lineno)
                elif isinstance(target, ast.Name) and target.id == "Popen":
                    popen_calls.append(node.lineno)
            with self.subTest(adapter=adapter.__name__):
                self.assertEqual(popen_calls, [])

    def test_cursor_process_handle_is_not_publicly_exported(self):
        self.assertFalse(hasattr(adaptor_package, "CursorProcessHandle"))

    def test_historical_legacy_reader_remains_broker_owned_during_sunset(self):
        from recollect_lines.service import Broker

        self.assertTrue(hasattr(Broker, "_reconcile_cursor_legacy_subprocess"))


if __name__ == "__main__":
    unittest.main()
