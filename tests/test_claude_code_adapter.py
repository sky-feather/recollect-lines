import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from recollect_lines.adapters import AdapterCapabilities
from recollect_lines.claude_code_adapter import (
    DEFAULT_COMMAND_PREFIX,
    ClaudeCodeAdapter,
    ClaudeCodeUnsupportedPolicy,
    redact_secrets,
)
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.service import Broker

FIXTURE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def command_only(adapter: ClaudeCodeAdapter, *args, **kwargs) -> list:
    command, _decision = adapter.build_command(*args, **kwargs)
    return command


def fake_adapter(grace_period_seconds=2.0, model=None):
    return ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE)), grace_period_seconds=grace_period_seconds, model=model)


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class ClaudeCodeAdapterUnitTests(unittest.TestCase):
    # --- command/argument generation -----------------------------------

    def test_default_command_prefix_is_the_bare_claude_binary(self):
        adapter = ClaudeCodeAdapter()
        self.assertEqual(adapter.command_prefix, DEFAULT_COMMAND_PREFIX)

    def test_build_command_places_prompt_immediately_after_dash_p(self):
        adapter = fake_adapter()
        command = command_only(adapter, "do the thing", "read_only")
        self.assertEqual(command[command.index("-p") + 1], "do the thing")

    def test_build_command_unknown_read_only_defaults_to_plan_with_tool_restrictions(self):
        adapter = fake_adapter()
        command = command_only(adapter, "inspect", "read_only")
        self.assertIn("--permission-mode", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertIn("--disallowedTools", command)
        disallowed = command[command.index("--disallowedTools") + 1]
        self.assertIn("Edit", disallowed)
        self.assertIn("Write", disallowed)
        self.assertIn("NotebookEdit", disallowed)

    def test_build_command_prose_debate_read_only_uses_dontask_not_plan(self):
        adapter = fake_adapter()
        command, decision = adapter.build_command(
            "Argue for approach A over B",
            "read_only",
            result_schema="plain-summary",
            task_category="prose",
        )
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(decision.task_category, "prose")

    def test_build_command_review_read_only_uses_dontask(self):
        adapter = fake_adapter()
        command = command_only(adapter, "review module X", "read_only", result_schema="review-findings")
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")

    def test_build_command_explicit_permission_mode_override(self):
        adapter = fake_adapter()
        command, decision = adapter.build_command(
            "summarize",
            "read_only",
            result_schema="plain-summary",
            claude_permission_mode="plan",
        )
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertEqual(decision.source, "caller_override")

    def test_build_command_read_only_restricts_tools_to_a_structural_allowlist_excluding_bash(self):
        # --disallowedTools alone leaves Bash nominally available (confirmed
        # against the real CLI during reconciliation, see docs/history/phases/phase-6a.md);
        # --tools is the actual structural guarantee for read_only, since it
        # narrows the tool *set* the model is given, not just a deny-list.
        adapter = fake_adapter()
        command = command_only(adapter, "inspect", "read_only")
        self.assertIn("--tools", command)
        allowed = command[command.index("--tools") + 1]
        self.assertIn("Read", allowed)
        self.assertIn("Grep", allowed)
        self.assertIn("Glob", allowed)
        self.assertNotIn("Bash", allowed)

    def test_build_command_isolated_worktree_maps_to_acceptedits_without_disallowed_tools(self):
        adapter = fake_adapter()
        command = command_only(adapter, "edit stuff", "isolated_worktree")
        self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")
        self.assertNotIn("--disallowedTools", command)
        self.assertNotIn("--tools", command)

    def test_disallowed_tools_flag_is_always_last_so_nothing_positional_trails_a_variadic_flag(self):
        adapter = fake_adapter()
        command = command_only(adapter, "inspect", "read_only")
        self.assertEqual(command[-2], "--disallowedTools")
        # --tools is also variadic; it must precede --disallowedTools (both
        # already argv-final) rather than trail it and risk swallowing it.
        self.assertLess(command.index("--tools"), command.index("--disallowedTools"))

    def test_build_command_fails_closed_for_an_unmapped_execution_mode(self):
        adapter = fake_adapter()
        with self.assertRaises(ClaudeCodeUnsupportedPolicy):
            adapter.build_command("do something", "shared_write")

    def test_build_command_includes_model_when_configured(self):
        adapter = fake_adapter(model="sonnet")
        command = command_only(adapter, "inspect", "read_only")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "sonnet")

    def test_build_command_omits_model_flag_by_default(self):
        adapter = fake_adapter()
        command = command_only(adapter, "inspect", "read_only")
        self.assertNotIn("--model", command)

    def test_output_format_json_and_no_session_persistence_are_always_present(self):
        adapter = fake_adapter()
        command = command_only(adapter, "inspect", "read_only")
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertIn("--no-session-persistence", command)

    # --- redaction --------------------------------------------------------

    def test_redact_secrets_scrubs_anthropic_api_keys(self):
        text = "call failed with key sk-ant-abcdefgh12345678 in the request"
        self.assertNotIn("sk-ant-abcdefgh12345678", redact_secrets(text))
        self.assertIn("***REDACTED***", redact_secrets(text))

    def test_redact_secrets_scrubs_bearer_tokens(self):
        text = "Authorization: Bearer abcdEFGH12345678"
        self.assertIn("***REDACTED***", redact_secrets(text))
        self.assertNotIn("abcdEFGH12345678", redact_secrets(text))

    def test_redact_secrets_leaves_ordinary_text_untouched(self):
        text = "42 is the magic number in fact.txt"
        self.assertEqual(redact_secrets(text), text)

    # --- capabilities -------------------------------------------------------

    def test_capabilities_declare_only_what_is_actually_implemented(self):
        self.assertIsInstance(ClaudeCodeAdapter.capabilities, AdapterCapabilities)
        self.assertTrue(ClaudeCodeAdapter.capabilities.requires_subprocess)
        self.assertTrue(ClaudeCodeAdapter.capabilities.supports_process_group_cancellation)
        self.assertFalse(ClaudeCodeAdapter.capabilities.reports_broker_verified_tests)

    # --- availability -------------------------------------------------------

    def test_check_availability_reports_missing_binary_without_raising(self):
        adapter = ClaudeCodeAdapter(command_prefix=("definitely-not-a-real-claude-binary-xyz",))
        probe = adapter.check_availability()
        self.assertFalse(probe["available"])
        self.assertEqual(probe["reason"], "cli_not_found")

    def test_check_availability_reports_installed_version(self):
        adapter = fake_adapter()
        # The fixture doesn't implement --version, so it falls through to the
        # default success path and prints a JSON result line — still exit 0,
        # which is all check_availability() asserts on.
        probe = adapter.check_availability()
        self.assertTrue(probe["available"])

    def test_check_availability_times_out_cleanly(self):
        adapter = ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE), "-p", "SLEEP"))
        probe = adapter.check_availability(timeout=0.2)
        self.assertFalse(probe["available"])
        self.assertEqual(probe["reason"], "version_check_timed_out")


class ClaudeCodeBrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        # Unlike OpenCodeAdapter (which passes the workspace as a --dir flag
        # value), ClaudeCodeAdapter has no --dir equivalent and launches with
        # cwd=workspace directly (see its module docstring) — so tests need a
        # real directory here, not a placeholder path.
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, claude_code_adapter=fake_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, task="Inspect fact.txt", **kwargs):
        return self.broker.create(TaskRequest(task, str(self.workspace), profile="claude_code", **kwargs))

    # --- process metadata / cancellation integration -----------------------

    def test_start_creates_stdout_and_stderr_artifacts_and_records_pid_pgid(self):
        record = self.create()
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        stdout_path = self.home / "artifacts" / record.id / "stdout.log"
        stderr_path = self.home / "artifacts" / record.id / "stderr.log"
        wait_until(lambda: stdout_path.exists() and stdout_path.stat().st_size > 0)
        self.assertTrue(stdout_path.exists())
        self.assertTrue(stderr_path.exists())
        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        self.assertIn("pid", run_event["metadata"])
        self.assertIn("pgid", run_event["metadata"])
        self.assertEqual(run_event["metadata"]["runtime_description"], "Claude Code via claude -p")
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["adapter"], "claude_code")
        self.broker.collect(record.id)

    def test_start_records_permission_mode_policy_for_prose_tasks(self):
        record = self.create(
            task="Debate whether tabs or spaces win",
            result_schema="plain-summary",
            task_category="prose",
        )
        self.broker.start(record.id)
        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        policy = run_event["metadata"]["permission_mode_policy"]
        self.assertEqual(policy["permission_mode"], "dontAsk")
        self.assertEqual(policy["task_category"], "prose")
        self.assertEqual(policy["signals"]["task_category_source"], "explicit")
        self.broker.collect(record.id)

    def test_cancel_terminates_process_group_read_only(self):
        record = self.broker.create(TaskRequest("SLEEP", str(self.workspace), profile="claude_code", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles[record.id]
        wait_until(lambda: handle.stderr_path.exists() and b"started" in handle.stderr_path.read_bytes())

        cancelled = self.broker.cancel(record.id, "no longer needed")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        cancel_event = self.broker.store.events(record.id)[-1]
        self.assertTrue(cancel_event["metadata"]["cancellation"]["group_terminated"])
        self.assertIn("SIGTERM", cancel_event["metadata"]["cancellation"]["signals_sent"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(handle.pgid, 0)

    def test_cancel_escalates_to_sigkill_when_process_ignores_sigterm(self):
        self.broker = Broker(self.home, claude_code_adapter=fake_adapter(grace_period_seconds=0.3))
        record = self.broker.create(TaskRequest("SLEEP_IGNORE_TERM", str(self.workspace), profile="claude_code", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles[record.id]
        wait_until(lambda: handle.stderr_path.exists() and b"started" in handle.stderr_path.read_bytes())

        cancelled = self.broker.cancel(record.id, "force stop")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        cancel_event = self.broker.store.events(record.id)[-1]
        signals_sent = cancel_event["metadata"]["cancellation"]["signals_sent"]
        self.assertEqual(signals_sent, ["SIGTERM", "SIGKILL"])

    # --- structured-result / raw-output collection -------------------------

    def test_collect_parses_single_json_result_object_as_summary(self):
        record = self.create(task="what is the magic number")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertIn("42", result["summary"])
        self.assertEqual(result["runtime"]["adapter"], "claude_code")
        self.assertFalse(result["runtime"]["verification"]["tests_broker_verified"])
        self.assertEqual(result["runtime"]["verification"]["source"], "runtime_reported")
        self.assertEqual(result["runtime"]["parsed_result_count"], 1)
        self.assertEqual(result["runtime"]["malformed_output_lines"], 0)

    def test_collect_is_defensive_against_a_malformed_leading_line(self):
        record = self.create(task="MALFORMED")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], "partial result despite a malformed line")
        self.assertGreaterEqual(result["runtime"]["malformed_output_lines"], 1)

    def test_collect_with_no_parseable_output_is_succeeded_with_warnings_not_a_fabricated_success(self):
        record = self.create(task="EMPTY_OUTPUT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED_WITH_WARNINGS)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertIsNone(result["runtime"]["summary"])

    def test_nonzero_exit_is_reported_as_failed_not_broker_verified(self):
        record = self.create(task="NONZERO_EXIT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["exit_code"], 1)
        self.assertEqual(result["runtime"]["error_category"], "unparseable_output")
        self.assertFalse(result["runtime"]["verification"]["tests_broker_verified"])

    def test_a_clean_is_error_false_result_followed_by_a_nonzero_exit_is_still_categorized(self):
        # A process can flush a well-formed, is_error:false JSON result and
        # still be killed (external timeout/OOM) before it exits 0. The task
        # must still fail with a non-null error_category, not silently look
        # like an uncategorized success-shaped failure.
        record = self.create(task="KILLED_AFTER_RESULT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["exit_code"], 1)
        self.assertFalse(result["runtime"]["is_error"])
        self.assertIsNotNone(result["runtime"]["error_category"])

    # --- availability/auth/launch error normalization -----------------------

    def test_auth_error_is_classified_without_leaking_the_key(self):
        record = self.create(task="AUTH_ERROR")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["error_category"], "authentication_error")
        self.assertTrue(result["runtime"]["is_error"])

    def test_missing_noninteractive_auth_currently_classified_as_unparseable_output(self):
        # Wave 0 dogfood finding (Cursor-as-parent MacBook run, 2026-07-16):
        # a claude CLI with no stored/non-interactive credentials fails
        # before it ever reaches a model call — it never prints an
        # `--output-format json` result object at all, just a stderr hint
        # and a nonzero exit (see fake_claude.py's NOT_LOGGED_IN branch).
        # collect() has no result_obj to read an api_error_status from, so
        # this currently falls through to the generic
        # process_exit_code-driven "unparseable_output" category rather
        # than "authentication_error" (contrast with AUTH_ERROR above, an
        # in-band 401 the CLI *does* get far enough to report structurally).
        # Asserting "authentication_error" here would assert a product
        # promise the runtime doesn't keep yet; this pins the actual,
        # observable failure path — including that the auth hint still
        # survives in stderr_tail as forensic evidence — as a baseline.
        record = self.create(task="NOT_LOGGED_IN")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["exit_code"], 1)
        self.assertEqual(result["runtime"]["error_category"], "unparseable_output")
        self.assertIsNone(result["runtime"]["summary"])
        self.assertIn("Please run /login", result["runtime"]["stderr_tail"])

    def test_rate_limit_error_is_classified_distinctly_from_auth_error(self):
        record = self.create(task="RATE_LIMIT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["error_category"], "rate_limit_or_quota_error")

    # --- result normalization / idempotent collection -----------------------

    def test_repeated_collect_on_a_terminal_task_is_idempotent(self):
        record = self.create()
        self.broker.start(record.id)
        first = self.broker.collect(record.id)
        second = self.broker.collect(record.id)
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.updated_at, second.updated_at)

    def test_collect_without_a_process_handle_confirms_dead_process_group_and_fails(self):
        record = self.create()
        self.broker.start(record.id)
        orphaned_handle = self.broker._process_handles.pop(record.id)
        orphaned_handle.popen.wait(timeout=5)

        completed = self.broker.collect(record.id)

        self.assertEqual(completed.state, TaskState.FAILED)
        self.assertEqual(self.broker.store.events(record.id)[-1]["metadata"]["reason"], "process_group_confirmed_dead")


class ClaudeCodeUnsupportedPolicyBrokerTests(unittest.TestCase):
    """Fail-closed permission-mode mapping surfaces through the broker, never
    silently broadening privilege for an execution_mode with no validated
    Claude Code permission-mode mapping.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.broker = Broker(self.home, claude_code_adapter=fake_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_broker_start_propagates_unsupported_policy_rather_than_launching(self):
        # profile policy only allows read_only/isolated_worktree today, so this
        # exercises the adapter's own defense-in-depth check directly.
        adapter = fake_adapter()
        with self.assertRaises(ClaudeCodeUnsupportedPolicy):
            adapter.build_command("do something", "shared_write")


if __name__ == "__main__":
    unittest.main()
