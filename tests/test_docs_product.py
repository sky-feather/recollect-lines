"""Documentation product contract and five-minute acceptance (Wave 3 / PR 9)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
OPERATOR_GUIDE = ROOT / "docs" / "operator-guide.md"
GETTING_STARTED = ROOT / "docs" / "getting-started.md"
FIVE_MINUTE_SCRIPT = ROOT / "scripts" / "five_minute_acceptance.py"

# Product claims the operator guide must state plainly.
OPERATOR_GUIDE_REQUIRED = (
    "local-first delegation broker",
    "What it is not",
    "openai_compatible",
    "workspace-mutating",
    "api_key_env",
    "restart_required_for_changes",
    "five_minute_acceptance.py",
    "missing_process_handle",
    "mcp install",
    "cursor",
    "claude_code",
    "codex",
)

# Stale guidance we refuse to ship in user-facing docs.
STALE_DOC_PATTERNS = (
    re.compile(r"\bgpt-4o\b(?!-)"),  # gpt-4o without -mini suffix
    re.compile(r"planned for a later PR", re.I),
    re.compile(r"providers\.json-only", re.I),
)


def markdown_files() -> list[Path]:
    return sorted(ROOT.rglob("*.md"))


def resolve_link(source: Path, target: str) -> Path | None:
    if target.startswith(("http://", "https://", "mailto:")):
        return None
    if target.startswith("#"):
        return source
    path_part, _, _ = target.partition("#")
    if not path_part:
        return source
    return (source.parent / path_part).resolve()


class DocsLinkTests(unittest.TestCase):
    def test_relative_markdown_links_resolve(self):
        missing: list[str] = []
        for md in markdown_files():
            text = md.read_text(encoding="utf-8")
            for match in LINK_RE.finditer(text):
                target = match.group(1)
                resolved = resolve_link(md, target)
                if resolved is None:
                    continue
                if not resolved.exists():
                    missing.append(f"{md.relative_to(ROOT)} -> {target}")
        self.assertEqual(missing, [], "broken markdown links:\n" + "\n".join(missing))


class OperatorGuideTests(unittest.TestCase):
    def test_operator_guide_exists_with_product_claims(self):
        self.assertTrue(OPERATOR_GUIDE.is_file(), "docs/operator-guide.md is required")
        text = OPERATOR_GUIDE.read_text(encoding="utf-8")
        missing = [phrase for phrase in OPERATOR_GUIDE_REQUIRED if phrase not in text]
        self.assertEqual(missing, [], f"operator-guide.md missing required phrases: {missing}")

    def test_getting_started_documents_five_minute_script(self):
        text = GETTING_STARTED.read_text(encoding="utf-8")
        self.assertIn("five_minute_acceptance.py", text)
        self.assertIn("Five-minute clean operator path", text)

    def test_user_facing_docs_avoid_stale_guidance(self):
        user_docs = [
            ROOT / "README.md",
            OPERATOR_GUIDE,
            GETTING_STARTED,
            ROOT / "docs" / "README.md",
            ROOT / "docs" / "cli.md",
            ROOT / "docs" / "mcp.md",
            ROOT / "docs" / "user-flows.md",
        ]
        violations: list[str] = []
        for path in user_docs:
            text = path.read_text(encoding="utf-8")
            for pattern in STALE_DOC_PATTERNS:
                if pattern.search(text):
                    violations.append(f"{path.relative_to(ROOT)} matches {pattern.pattern}")
        self.assertEqual(violations, [], "stale user-facing guidance:\n" + "\n".join(violations))


class FiveMinuteAcceptanceTests(unittest.TestCase):
    def test_five_minute_acceptance_script_passes(self):
        result = subprocess.run(
            [sys.executable, str(FIVE_MINUTE_SCRIPT)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=300,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"five_minute_acceptance failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("Five-minute acceptance PASSED", result.stdout)


class DemoScriptTests(unittest.TestCase):
    def test_codex_demo_defaults_to_dry_run(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_codex_demo.py")],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertIn("No provider call", result.stderr)

    def test_codex_demo_refuses_live_without_ack(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_codex_demo.py"), "--execute-live"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 2)

    def test_recorded_codex_evidence_present(self):
        evidence = ROOT / "docs" / "demos" / "codex-marker-evidence.json"
        self.assertTrue(evidence.is_file())
        payload = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertTrue(payload["provider_call_occurred"])
        self.assertEqual(payload["result"]["runtime_result"]["summary"], "alpha.txt")


if __name__ == "__main__":
    unittest.main()
