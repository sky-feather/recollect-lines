import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"
FAKE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"
FAKE_CURSOR = Path(__file__).parent / "fixtures" / "fake_cursor.py"


class McpStdioClient:
    """Drives a real `python -m recollect_lines.mcp_server` subprocess over its stdio transport."""

    def __init__(self, home: Path, claude_command: list | None = None, codex_command: list | None = None, cursor_command: list | None = None):
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
        args = [sys.executable, "-m", "recollect_lines.mcp_server", "--home", str(home)]
        if claude_command is not None:
            args += ["--claude-command", json.dumps(claude_command)]
        if codex_command is not None:
            args += ["--codex-command", json.dumps(codex_command)]
        if cursor_command is not None:
            args += ["--cursor-command", json.dumps(cursor_command)]
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._next_id = 1

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def send_raw(self, line: str) -> None:
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def recv(self, timeout: float = 10.0) -> dict:
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read()
            raise AssertionError(f"Subprocess produced no output (exit={self.process.poll()}); stderr:\n{stderr}")
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, **({"params": params} if params is not None else {})})
        response = self.recv()
        self.assertEqualId(response, request_id)
        return response

    def assertEqualId(self, response, request_id):
        if response.get("id") != request_id:
            raise AssertionError(f"Expected response id {request_id}, got {response!r}")

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.client = McpStdioClient(self.home)

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def initialize(self):
        return self.client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})

    def delegate(self, **overrides):
        arguments = {"task": "Inspect tests", "workspace": str(self.workspace)}
        arguments.update(overrides)
        return self.client.call_tool("delegate", arguments)

    # --- full lifecycle -----------------------------------------------

    def test_initialize_tools_list_tools_call_lifecycle(self):
        init_response = self.initialize()
        self.assertEqual(init_response["result"]["serverInfo"]["name"], "recollect-lines-mcp")
        self.assertIn("tools", init_response["result"]["capabilities"])

        self.client.notify("notifications/initialized")

        listed = self.client.request("tools/list")
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "delegate", "delegate_batch", "status", "collect", "cancel", "control", "message", "reconcile",
                "discover_capabilities", "select_candidates", "council_validate", "council_execute",
                "task_children", "task_tree", "completion_events",
            },
        )

        delegated = self.delegate()
        payload = json.loads(delegated["result"]["content"][0]["text"])
        self.assertFalse(delegated["result"]["isError"])
        self.assertEqual(payload["envelope_version"], 1)
        self.assertTrue(payload["ok"])
        task_id = payload["data"]["task_id"]
        self.assertEqual(payload["data"]["state"], "running")

        status = self.client.call_tool("status", {"task_id": task_id})
        status_payload = json.loads(status["result"]["content"][0]["text"])
        self.assertEqual(status_payload["data"]["state"], "running")
        self.assertEqual([event["type"] for event in status_payload["data"]["events"]], ["task.created", "task.queued", "task.preparing", "task.running"])

        # Mock profile never registers a process handle, so collect deterministically reaches
        # FAILED with a documented reason rather than fabricating a completion.
        collected = self.client.call_tool("collect", {"task_id": task_id})
        collected_payload = json.loads(collected["result"]["content"][0]["text"])
        self.assertFalse(collected["result"]["isError"])
        self.assertEqual(collected_payload["data"]["state"], "failed")
        self.assertIsNone(collected_payload["data"]["runtime_result"])

    def test_mcp_root_delegate_defaults_origin_kind_host(self):
        self.initialize()
        self.client.notify("notifications/initialized")
        delegated = self.delegate()
        payload = json.loads(delegated["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["origin_kind"], "host")

    def test_mcp_parented_delegate_defaults_origin_kind_host(self):
        self.initialize()
        self.client.notify("notifications/initialized")
        parent = json.loads(self.delegate()["result"]["content"][0]["text"])["data"]
        child = json.loads(
            self.delegate(parent_task_id=parent["task_id"])["result"]["content"][0]["text"]
        )["data"]
        self.assertEqual(child["origin_kind"], "host")
        self.assertEqual(child["parent_task_id"], parent["task_id"])

    def test_ping(self):
        self.initialize()
        response = self.client.request("ping")
        self.assertEqual(response["result"], {})

    # --- multi-message framing and notifications -----------------------

    def test_pipelined_requests_answered_in_order(self):
        self.initialize()
        self.client.send({"jsonrpc": "2.0", "id": "a", "method": "ping"})
        self.client.send({"jsonrpc": "2.0", "id": "b", "method": "ping"})
        first = self.client.recv()
        second = self.client.recv()
        self.assertEqual(first["id"], "a")
        self.assertEqual(second["id"], "b")

    def test_notification_produces_no_response(self):
        self.initialize()
        self.client.notify("notifications/initialized")
        # If the notification had produced a response, it would arrive before this ping's.
        response = self.client.request("ping")
        self.assertEqual(response["result"], {})

    def test_blank_lines_between_messages_are_ignored(self):
        self.initialize()
        self.client.send_raw("")
        self.client.send_raw("   ")
        response = self.client.request("ping")
        self.assertEqual(response["result"], {})

    # --- malformed request handling -------------------------------------

    def test_malformed_json_returns_parse_error(self):
        self.initialize()
        self.client.send_raw("{not valid json")
        response = self.client.recv()
        self.assertIsNone(response["id"])
        self.assertEqual(response["error"]["code"], -32700)

    def test_wrong_jsonrpc_version_is_invalid_request(self):
        self.initialize()
        self.client.send({"jsonrpc": "1.0", "id": 99, "method": "ping"})
        response = self.client.recv()
        self.assertEqual(response["id"], 99)
        self.assertEqual(response["error"]["code"], -32600)

    def test_missing_method_is_invalid_request(self):
        self.initialize()
        self.client.send({"jsonrpc": "2.0", "id": 42})
        response = self.client.recv()
        self.assertEqual(response["id"], 42)
        self.assertEqual(response["error"]["code"], -32600)

    def test_malformed_notification_produces_no_response(self):
        self.initialize()
        self.client.send({"jsonrpc": "1.0", "method": "bogus"})  # malformed AND a notification
        response = self.client.request("ping")  # proves nothing was queued for the line above
        self.assertEqual(response["result"], {})

    # --- unknown method / unknown tool -----------------------------------

    def test_unknown_method_is_method_not_found(self):
        self.initialize()
        response = self.client.request("resources/list")
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_tool_is_invalid_params(self):
        self.initialize()
        response = self.client.request("tools/call", {"name": "does_not_exist", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_non_object_arguments_is_invalid_params(self):
        self.initialize()
        response = self.client.request("tools/call", {"name": "status", "arguments": "nope"})
        self.assertEqual(response["error"]["code"], -32602)

    # --- unknown task id --------------------------------------------------

    def test_unknown_task_id_is_a_tool_error_not_a_protocol_error(self):
        self.initialize()
        for tool in ("status", "collect", "cancel", "message"):
            arguments = {"task_id": "tsk_does_not_exist"}
            if tool == "message":
                arguments["content"] = "hello"
            response = self.client.call_tool(tool, arguments)
            self.assertNotIn("error", response, f"{tool} should not raise a JSON-RPC error for an unknown task id")
            self.assertTrue(response["result"]["isError"], f"{tool} should report isError for an unknown task id")
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(payload["error"]["code"], "KeyError")

    # --- delegate_batch partial outcomes ----------------------------------

    def test_delegate_batch_partial_outcome(self):
        self.initialize()
        response = self.client.call_tool(
            "delegate_batch",
            {
                "tasks": [
                    {"task": "Good task", "workspace": str(self.workspace)},
                    {"task": "Bad task"},  # missing required workspace
                ]
            },
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])  # the batch call itself succeeded
        outcomes = payload["data"]["outcomes"]
        self.assertTrue(outcomes[0]["accepted"])
        self.assertEqual(outcomes[0]["state"], "running")
        self.assertFalse(outcomes[1]["accepted"])
        self.assertIn("workspace", outcomes[1]["error"]["message"])

        # The good item's task really was created and started, unaffected by the bad item.
        status = self.client.call_tool("status", {"task_id": outcomes[0]["task_id"]})
        status_payload = json.loads(status["result"]["content"][0]["text"])
        self.assertEqual(status_payload["data"]["state"], "running")

    def test_delegate_batch_rejects_non_array_or_empty_tasks(self):
        self.initialize()
        for bad_tasks in ("not a list", [], None):
            arguments = {} if bad_tasks is None else {"tasks": bad_tasks}
            response = self.client.call_tool("delegate_batch", arguments)
            self.assertNotIn("error", response, f"tasks={bad_tasks!r} should not be a protocol error")
            self.assertTrue(response["result"]["isError"], f"tasks={bad_tasks!r} should be rejected")
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertIn("tasks", payload["error"]["message"])

    # --- message unsupported ------------------------------------------------

    def test_message_returns_structured_unsupported(self):
        self.initialize()
        delegated = self.delegate()
        task_id = json.loads(delegated["result"]["content"][0]["text"])["data"]["task_id"]

        response = self.client.call_tool("message", {"task_id": task_id, "content": "steer me"})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(payload["data"]["status"], "unsupported")
        self.assertIn("OpenCode", payload["data"]["reason"])

    # --- cancel -------------------------------------------------------------

    def test_cancel_running_mock_task(self):
        self.initialize()
        delegated = self.delegate()
        task_id = json.loads(delegated["result"]["content"][0]["text"])["data"]["task_id"]

        response = self.client.call_tool("cancel", {"task_id": task_id, "reason": "no longer needed"})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(payload["data"]["state"], "cancelled")

    # --- verify_commands / collect broker-verified evidence ------------------

    def test_collect_surfaces_broker_verified_evidence_distinct_from_runtime_result(self):
        self.initialize()
        delegated = self.delegate(verify_commands=[[sys.executable, "-c", "print('ok')"]])
        task_id = json.loads(delegated["result"]["content"][0]["text"])["data"]["task_id"]

        collected = self.client.call_tool("collect", {"task_id": task_id})
        payload = json.loads(collected["result"]["content"][0]["text"])
        self.assertFalse(collected["result"]["isError"])
        # The mock adapter never registers a process handle, so the runtime-reported side
        # deterministically has no result...
        self.assertEqual(payload["data"]["state"], "failed")
        self.assertIsNone(payload["data"]["runtime_result"])
        # ...but the delegate-supplied verify_commands still ran for real, as broker-verified
        # evidence — distinct from (and unaffected by) that runtime-side failure.
        broker_verification = payload["data"]["broker_verification"]
        self.assertTrue(broker_verification["commands"][0]["broker_verified"])
        self.assertTrue(broker_verification["commands"][0]["passed"])

        status = self.client.call_tool("status", {"task_id": task_id})
        status_payload = json.loads(status["result"]["content"][0]["text"])
        verified_events = [event for event in status_payload["data"]["events"] if event["type"] == "task.verified"]
        self.assertEqual(len(verified_events), 1)
        self.assertTrue(verified_events[0]["metadata"]["all_passed"])

    # --- tool schema shape ----------------------------------------------

    def test_tool_schemas_declare_required_fields(self):
        self.initialize()
        listed = self.client.request("tools/list")
        schemas = {tool["name"]: tool["inputSchema"] for tool in listed["result"]["tools"]}
        self.assertEqual(schemas["delegate"]["required"], ["task", "workspace"])
        self.assertEqual(schemas["delegate_batch"]["required"], ["tasks"])
        self.assertEqual(schemas["status"]["required"], ["task_id"])
        self.assertEqual(schemas["collect"]["required"], ["task_id"])
        self.assertEqual(schemas["cancel"]["required"], ["task_id"])
        self.assertEqual(schemas["message"]["required"], ["task_id", "content"])
        self.assertNotIn("required", schemas["reconcile"])  # task_id is optional: omit to reconcile every pending task

    def test_profile_schema_lists_cursor_alongside_mock_opencode_claude_code_and_codex(self):
        self.initialize()
        listed = self.client.request("tools/list")
        schemas = {tool["name"]: tool["inputSchema"] for tool in listed["result"]["tools"]}
        self.assertEqual(
            set(schemas["delegate"]["properties"]["profile"]["enum"]),
            {"mock", "opencode", "claude_code", "codex", "cursor", "openai_compatible"},
        )


class ClaudeCodeMcpSelectionTests(unittest.TestCase):
    """Runtime discovery/selection contract: a profile="claude_code" delegate call is
    dispatched to ClaudeCodeAdapter (not silently treated as mock/opencode), driven
    end-to-end through the real MCP stdio surface against the deterministic fixture
    CLI — never the real network/quota-consuming `claude` binary.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.client = McpStdioClient(self.home, claude_command=[sys.executable, str(FAKE_CLAUDE)])
        self.initialize()

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def initialize(self):
        return self.client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})

    def test_delegate_with_claude_code_profile_runs_the_adapter_and_collects_a_result(self):
        delegated = self.client.call_tool("delegate", {
            "task": "what is the magic number",
            "workspace": str(self.workspace),
            "execution_mode": "read_only",
            "profile": "claude_code",
        })
        payload = json.loads(delegated["result"]["content"][0]["text"])
        self.assertFalse(delegated["result"]["isError"])
        task_id = payload["data"]["task_id"]
        self.assertEqual(payload["data"]["profile"], "claude_code")

        collected = self.client.call_tool("collect", {"task_id": task_id})
        collected_payload = json.loads(collected["result"]["content"][0]["text"])
        self.assertFalse(collected["result"]["isError"])
        self.assertEqual(collected_payload["data"]["state"], "succeeded")
        self.assertEqual(collected_payload["data"]["runtime_result"]["runtime"]["adapter"], "claude_code")
        self.assertIn("42", collected_payload["data"]["runtime_result"]["summary"])


class CodexMcpSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.client = McpStdioClient(self.home, codex_command=[sys.executable, str(FAKE_CODEX)])
        self.initialize()

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def initialize(self):
        return self.client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})

    def test_delegate_with_codex_profile_runs_the_adapter_and_collects_a_result(self):
        delegated = self.client.call_tool("delegate", {
            "task": "what is the magic number",
            "workspace": str(self.workspace),
            "execution_mode": "read_only",
            "profile": "codex",
        })
        payload = json.loads(delegated["result"]["content"][0]["text"])
        self.assertFalse(delegated["result"]["isError"])
        task_id = payload["data"]["task_id"]
        self.assertEqual(payload["data"]["profile"], "codex")

        collected = self.client.call_tool("collect", {"task_id": task_id})
        collected_payload = json.loads(collected["result"]["content"][0]["text"])
        self.assertFalse(collected["result"]["isError"])
        self.assertEqual(collected_payload["data"]["state"], "succeeded")
        self.assertEqual(collected_payload["data"]["runtime_result"]["runtime"]["adapter"], "codex")
        self.assertIn("42", collected_payload["data"]["runtime_result"]["summary"])


class RuntimeProfileCompatibilityMcpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.client = McpStdioClient(self.home)
        self.initialize()

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def initialize(self):
        return self.client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})

    def _delegate_payload(self, **overrides):
        arguments = {"task": "Inspect tests", "workspace": str(self.workspace)}
        arguments.update(overrides)
        response = self.client.call_tool("delegate", arguments)
        return json.loads(response["result"]["content"][0]["text"])

    def test_runtime_delegate_has_no_compatibility_marker(self):
        payload = self._delegate_payload(runtime="mock")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["runtime"], "mock")
        self.assertNotIn("compatibility", payload["data"])

    def test_legacy_profile_delegate_has_compatibility_marker(self):
        payload = self._delegate_payload(profile="mock")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["compatibility"], {
            "legacy_profile_translated": True,
            "deprecated_fields": ["profile"],
        })

    def test_runtime_with_agent_profile_stays_separate(self):
        payload = self._delegate_payload(runtime="mock", agent_profile="architecture-reviewer")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["runtime"], "mock")
        self.assertEqual(payload["data"]["agent_profile"], "architecture-reviewer")
        self.assertNotIn("compatibility", payload["data"])

    def test_delegate_batch_propagates_compatibility_per_item(self):
        response = self.client.call_tool("delegate_batch", {
            "tasks": [
                {"task": "one", "workspace": str(self.workspace), "runtime": "mock"},
                {"task": "two", "workspace": str(self.workspace), "profile": "mock"},
            ],
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        outcomes = payload["data"]["outcomes"]
        self.assertTrue(outcomes[0]["accepted"])
        self.assertNotIn("compatibility", outcomes[0])
        self.assertTrue(outcomes[1]["accepted"])
        self.assertEqual(outcomes[1]["compatibility"]["legacy_profile_translated"], True)

    def test_delegate_rejects_conflicting_runtime_and_profile(self):
        response = self.client.call_tool("delegate", {
            "task": "Inspect tests",
            "workspace": str(self.workspace),
            "runtime": "codex",
            "profile": "claude_code",
        })
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "LegacyProfileConflictError")
        self.assertIn("runtime", payload["error"]["message"])
        self.assertIn("profile", payload["error"]["message"])

    def test_delegate_rejects_unknown_legacy_profile(self):
        response = self.client.call_tool("delegate", {
            "task": "Inspect tests",
            "workspace": str(self.workspace),
            "profile": "architecture-reviewer",
        })
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "ValueError")
        self.assertIn("agent_profile", payload["error"]["message"])
        self.assertIn("architecture-reviewer", payload["error"]["message"])

    def test_delegate_batch_rejects_conflicting_runtime_and_profile_per_item(self):
        response = self.client.call_tool("delegate_batch", {
            "tasks": [
                {"task": "one", "workspace": str(self.workspace), "runtime": "mock"},
                {
                    "task": "two",
                    "workspace": str(self.workspace),
                    "runtime": "codex",
                    "profile": "claude_code",
                },
            ],
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        outcomes = payload["data"]["outcomes"]
        self.assertTrue(outcomes[0]["accepted"])
        self.assertFalse(outcomes[1]["accepted"])
        self.assertEqual(outcomes[1]["error"]["code"], "LegacyProfileConflictError")


class CursorMcpSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.client = McpStdioClient(self.home, cursor_command=[sys.executable, str(FAKE_CURSOR)])
        self.initialize()

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def initialize(self):
        return self.client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})

    def test_delegate_with_cursor_profile_runs_the_adapter_and_collects_a_result(self):
        delegated = self.client.call_tool("delegate", {
            "task": "what is the magic number",
            "workspace": str(self.workspace),
            "execution_mode": "read_only",
            "profile": "cursor",
        })
        payload = json.loads(delegated["result"]["content"][0]["text"])
        self.assertFalse(delegated["result"]["isError"])
        task_id = payload["data"]["task_id"]
        self.assertEqual(payload["data"]["profile"], "cursor")

        collected = self.client.call_tool("collect", {"task_id": task_id})
        collected_payload = json.loads(collected["result"]["content"][0]["text"])
        self.assertFalse(collected["result"]["isError"])
        self.assertEqual(collected_payload["data"]["state"], "succeeded")
        self.assertEqual(collected_payload["data"]["runtime_result"]["runtime"]["adapter"], "cursor")
        self.assertIn("42", collected_payload["data"]["runtime_result"]["summary"])


if __name__ == "__main__":
    unittest.main()
