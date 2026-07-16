import unittest

from recollect_lines.claude_permission_mode_policy import (
    ClaudePermissionModePolicyError,
    infer_task_category,
    resolve_claude_permission_mode,
)


class ClaudePermissionModePolicyTests(unittest.TestCase):
    def test_prose_debate_read_only_uses_dontask_not_plan(self):
        decision = resolve_claude_permission_mode(
            execution_mode="read_only",
            result_schema="plain-summary",
            task_category="prose",
        )
        self.assertEqual(decision.permission_mode, "dontAsk")
        self.assertEqual(decision.task_category, "prose")
        self.assertEqual(decision.signals["task_category_source"], "explicit")

    def test_review_read_only_uses_dontask(self):
        decision = resolve_claude_permission_mode(
            execution_mode="read_only",
            result_schema="review-findings",
        )
        self.assertEqual(decision.permission_mode, "dontAsk")
        self.assertEqual(decision.task_category, "review")

    def test_worktree_implementation_uses_acceptedits(self):
        decision = resolve_claude_permission_mode(
            execution_mode="isolated_worktree",
            result_schema="implementation-report",
        )
        self.assertEqual(decision.permission_mode, "acceptEdits")
        self.assertEqual(decision.task_category, "implementation")

    def test_explicit_override_is_validated_and_honored(self):
        decision = resolve_claude_permission_mode(
            execution_mode="read_only",
            result_schema="plain-summary",
            claude_permission_mode="plan",
        )
        self.assertEqual(decision.permission_mode, "plan")
        self.assertEqual(decision.source, "caller_override")

    def test_unknown_category_defaults_to_plan_conservatively(self):
        decision = resolve_claude_permission_mode(execution_mode="read_only")
        self.assertEqual(decision.task_category, "unknown")
        self.assertEqual(decision.permission_mode, "plan")
        self.assertEqual(decision.source, "policy")

    def test_investigation_read_only_keeps_plan(self):
        decision = resolve_claude_permission_mode(
            execution_mode="read_only",
            result_schema="evidence-report",
        )
        self.assertEqual(decision.task_category, "investigation")
        self.assertEqual(decision.permission_mode, "plan")

    def test_read_only_override_rejects_acceptedits(self):
        with self.assertRaises(ClaudePermissionModePolicyError):
            resolve_claude_permission_mode(
                execution_mode="read_only",
                claude_permission_mode="acceptEdits",
            )

    def test_worktree_override_rejects_dontask(self):
        with self.assertRaises(ClaudePermissionModePolicyError):
            resolve_claude_permission_mode(
                execution_mode="isolated_worktree",
                claude_permission_mode="dontAsk",
            )

    def test_unmapped_execution_mode_fails_closed(self):
        with self.assertRaises(ClaudePermissionModePolicyError):
            resolve_claude_permission_mode(execution_mode="shared_write")

    def test_infer_task_category_from_agent_profile(self):
        category, source = infer_task_category(
            execution_mode="read_only",
            agent_profile="architecture-reviewer",
        )
        self.assertEqual(category, "review")
        self.assertEqual(source, "inferred")


if __name__ == "__main__":
    unittest.main()
