"""MR 8.8: integrated side-agent fixture acceptance."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "side_agent_fixture_acceptance.py"
EVIDENCE = ROOT / "docs" / "demos" / "side-agent-fixture-evidence.json"


class SideAgentIntegratedFixtureTests(unittest.TestCase):
    def test_fixture_acceptance_script_passes_offline(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(ROOT / "src")},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Side-agent fixture acceptance PASSED", result.stdout)

    def test_recorded_fixture_evidence_documents_offline_proof(self):
        self.assertTrue(EVIDENCE.is_file())
        payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        self.assertFalse(payload["provider_calls"])
        self.assertEqual(payload["mode"], "fixture")
        self.assertEqual(len(payload["children"]), 3)
        runtimes = {child["runtime"] for child in payload["children"]}
        self.assertEqual(runtimes, {"codex", "claude_code", "mock"})
        self.assertIn("PYTHONPATH=src python3 scripts/side_agent_fixture_acceptance.py", payload["commands"])


if __name__ == "__main__":
    unittest.main()
