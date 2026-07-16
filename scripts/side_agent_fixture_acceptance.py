#!/usr/bin/env python3
"""Integrated side-agent fixture acceptance (MR 8.8).

Deterministic, no-network proof that one host session can delegate heterogeneous
bounded children, poll durable completion-event cursors, collect provenance-aware
normalized results, display a task tree, refuse in-flight steering, and retain
writer isolation — using fixture CLIs only.

Usage:
    python3 scripts/side_agent_fixture_acceptance.py

Exit 0 when every check passes, 1 otherwise. Safe for CI without provider credentials.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURE_CODEX = ROOT / "tests" / "fixtures" / "fake_codex.py"
FIXTURE_CLAUDE = ROOT / "tests" / "fixtures" / "fake_claude.py"
EVIDENCE_PATH = ROOT / "docs" / "demos" / "side-agent-fixture-evidence.json"

sys.path.insert(0, str(SRC))

from recollect_lines.claude_code_adapter import ClaudeCodeAdapter  # noqa: E402
from recollect_lines.codex_adapter import CodexAdapter  # noqa: E402
from recollect_lines.models import ProfilePolicy, TaskRequest, TaskState  # noqa: E402
from recollect_lines.result_normalization import NORMALIZED_RESULT_ARTIFACT  # noqa: E402
from recollect_lines.service import Broker  # noqa: E402

EXTERNAL_ROOT = "integrated-fixture-session"
REVIEW_FINDINGS_PAYLOAD = json.dumps({
    "summary": "architecture review complete",
    "findings": [{"severity": "medium", "topic": "coupling"}],
})
EVIDENCE_REPORT_PAYLOAD = json.dumps({
    "summary": "evidence gathered",
    "findings": [{"id": "f1", "detail": "handler path traced"}],
    "claimed_evidence": ["src/handler.py"],
    "commands_executed": ["grep -R handler"],
    "unresolved_questions": [],
})

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" — {detail}" if detail and not condition else ""
    print(f"[{status}] {label}{suffix}")
    if not condition:
        _failures.append(label)
    return condition


def fake_codex_adapter(**kwargs):
    return CodexAdapter(command_prefix=(sys.executable, str(FIXTURE_CODEX)), grace_period_seconds=2.0, **kwargs)


def fake_claude_adapter(**kwargs):
    return ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE_CLAUDE)), grace_period_seconds=2.0, **kwargs)


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result


def init_fixture_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], cwd=path)
    run_git(["config", "user.email", "fixture@example.com"], cwd=path)
    run_git(["config", "user.name", "Side Agent Fixture"], cwd=path)
    (path / "README.md").write_text("Disposable repository for integrated side-agent fixture acceptance.\n")
    run_git(["add", "-A"], cwd=path)
    run_git(["commit", "-q", "-m", "initial"], cwd=path)
    return path


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def main() -> int:
    evidence: dict = {
        "mode": "fixture",
        "provider_calls": False,
        "external_root_id": EXTERNAL_ROOT,
        "children": [],
        "completion_events_observed": 0,
        "task_tree_node_count": 0,
        "steering_refused": False,
        "follow_up_relationship": "continues",
        "writer_isolation_enforced": False,
    }

    with tempfile.TemporaryDirectory(prefix="recollect-side-agent-fixture-") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "broker"
        source = init_fixture_repo(tmp_path / "source")
        head_before = run_git(["rev-parse", "HEAD"], cwd=source).stdout.strip()

        broker = Broker(
            home,
            profiles={"mock": ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)},
            codex_adapter=fake_codex_adapter(),
            claude_code_adapter=fake_claude_adapter(),
        )
        try:
            # 1. external_root_id without inventing a broker parent for grouping
            host_anchor = broker.create(TaskRequest(
                "host coordination anchor",
                str(source),
                runtime="mock",
                external_root_id=EXTERNAL_ROOT,
                origin_kind="host",
            ))
            check(
                "external_root_id groups host work without a broker parent",
                host_anchor.parent_task_id is None and host_anchor.external_root_id == EXTERNAL_ROOT,
                str(host_anchor),
            )
            check("host anchor is its own lineage root", host_anchor.root_task_id == host_anchor.id)

            # 2. concurrent heterogeneous bounded children
            codex_child = broker.create(TaskRequest(
                f"SLEEP_BRIEF SCHEMA_evidence-report {EVIDENCE_REPORT_PAYLOAD}",
                str(source),
                runtime="codex",
                model="fixture-codex-model",
                agent_profile="repository-investigator",
                result_schema="evidence-report",
                parent_task_id=host_anchor.id,
                external_root_id=EXTERNAL_ROOT,
                relationship="delegates",
                explicit_fields=frozenset({"model", "agent_profile", "result_schema"}),
            ))
            claude_child = broker.create(TaskRequest(
                f"SLEEP_BRIEF SCHEMA_review-findings {REVIEW_FINDINGS_PAYLOAD}",
                str(source),
                runtime="claude_code",
                model="fixture-claude-model",
                agent_profile="architecture-reviewer",
                result_schema="review-findings",
                parent_task_id=host_anchor.id,
                external_root_id=EXTERNAL_ROOT,
                relationship="delegates",
                explicit_fields=frozenset({"model", "agent_profile", "result_schema"}),
            ))
            mock_child = broker.create(TaskRequest(
                "Summarize verification scope",
                str(source),
                runtime="mock",
                agent_profile="test-planner",
                result_schema="plain-summary",
                parent_task_id=host_anchor.id,
                external_root_id=EXTERNAL_ROOT,
                relationship="delegates",
                explicit_fields=frozenset({"agent_profile", "result_schema"}),
            ))

            distinct_runtimes = {codex_child.runtime, claude_child.runtime, mock_child.runtime}
            check("children use distinct runtimes", distinct_runtimes == {"codex", "claude_code", "mock"}, str(distinct_runtimes))
            check(
                "children carry distinct agent profiles",
                {codex_child.agent_profile, claude_child.agent_profile, mock_child.agent_profile}
                == {"repository-investigator", "architecture-reviewer", "test-planner"},
            )
            check(
                "children carry distinct result schemas",
                {codex_child.result_schema, claude_child.result_schema, mock_child.result_schema}
                == {"evidence-report", "review-findings", "plain-summary"},
            )
            check("codex child persists requested and effective model", codex_child.model == "fixture-codex-model")
            broker.start(codex_child.id)
            check(
                "codex effective_model resolved at launch",
                broker.store.get(codex_child.id).effective_model == "fixture-codex-model",
            )
            broker.start(claude_child.id)
            check(
                "claude effective_model resolved at launch",
                broker.store.get(claude_child.id).effective_model == "fixture-claude-model",
            )
            broker.start(mock_child.id)

            running = [
                broker.store.get(codex_child.id).state,
                broker.store.get(claude_child.id).state,
                broker.store.get(mock_child.id).state,
            ]
            check("subprocess children enter running before collection", TaskState.RUNNING in running, str(running))

            # 3. host-side work while children run
            host_side = broker.create(TaskRequest(
                "host-side progress note",
                str(source),
                runtime="mock",
                external_root_id=EXTERNAL_ROOT,
                origin_kind="host",
            ))
            broker.start(host_side.id)
            broker.complete(host_side.id, "host continued while side agents ran")
            check(
                "host-side mock task completes while subprocess children are active",
                broker.store.get(host_side.id).state == TaskState.SUCCEEDED
                and any(broker.store.get(tid).state == TaskState.RUNNING for tid in (codex_child.id, claude_child.id)),
            )

            # 7. steering refusal then continues follow-up (on a brief sleeper)
            steer_target = broker.create(TaskRequest(
                "SLEEP_BRIEF steering probe",
                str(source),
                runtime="codex",
                parent_task_id=host_anchor.id,
                external_root_id=EXTERNAL_ROOT,
            ))
            broker.start(steer_target.id)
            wait_until(lambda: broker.store.get(steer_target.id).state == TaskState.RUNNING, timeout=2.0)
            refusal = broker.operator_control(steer_target.id, "message", message_content="please pivot")
            check(
                "in-flight steering is explicitly refused",
                refusal.get("code") == "unsupported_message_steering" and refusal.get("refused") is True,
                str(refusal),
            )
            evidence["steering_refused"] = refusal.get("code") == "unsupported_message_steering"
            follow_up = broker.create(TaskRequest(
                "follow-up after steering refusal",
                str(source),
                runtime="mock",
                parent_task_id=steer_target.id,
                external_root_id=EXTERNAL_ROOT,
                relationship="continues",
            ))
            check("continues spawns a new queued task", follow_up.relationship == "continues" and follow_up.state == TaskState.QUEUED)
            check("continues is not session resume", follow_up.id != steer_target.id)
            evidence["follow_up_task_id"] = follow_up.id

            broker.collect(codex_child.id)
            broker.collect(claude_child.id)
            broker.complete(mock_child.id, "verification scope: unit and integration smoke")
            broker.collect(steer_target.id)
            broker.start(follow_up.id)
            broker.complete(follow_up.id, "follow-up handled steering gap")

            # 4. durable completion-event cursor polling
            cursor = 0
            polled_events: list[dict] = []
            while True:
                page = broker.completion_events_since(cursor, limit=8, root_task_id=host_anchor.id)
                polled_events.extend(page["events"])
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    break
            terminal_ids = {codex_child.id, claude_child.id, mock_child.id, steer_target.id, follow_up.id}
            observed_ids = {event["task_id"] for event in polled_events}
            check(
                "completion_events cursor returns compact terminal signals for tree tasks",
                terminal_ids <= observed_ids,
                f"missing={terminal_ids - observed_ids}",
            )
            for event in polled_events:
                blob = json.dumps(event)
                check("completion event payload stays compact", "events.jsonl" not in blob and "malformed_event_lines" not in blob)
                check("completion event carries result_summary", "result_summary" in event)
            evidence["completion_events_observed"] = len(polled_events)

            # 5. provenance-aware collection with separate raw artifacts
            for child_id, schema in (
                (codex_child.id, "evidence-report"),
                (claude_child.id, "review-findings"),
                (mock_child.id, "plain-summary"),
            ):
                record = broker.store.get(child_id)
                check(f"{child_id} reached succeeded", record.state == TaskState.SUCCEEDED)
                artifacts_dir = broker.store.artifacts / child_id
                norm_path = artifacts_dir / NORMALIZED_RESULT_ARTIFACT
                check(f"{child_id} writes normalized_result.json", norm_path.is_file())
                envelope = json.loads(norm_path.read_text())
                raw_ref = envelope["parser"]["raw_output_artifact"]
                check(
                    f"{child_id} retains separate raw evidence artifact",
                    isinstance(raw_ref, str) and (artifacts_dir / raw_ref).is_file(),
                    str(raw_ref),
                )
                check(
                    f"{child_id} normalized artifact is distinct from raw evidence",
                    norm_path.name != raw_ref,
                )
                check(f"{child_id} normalized envelope schema", envelope["parser"]["requested_schema"] == schema)
                status = broker.status(child_id)
                check(f"{child_id} status exposes concise normalized view", "normalized_result" in status)
                evidence["children"].append({
                    "task_id": child_id,
                    "runtime": record.runtime,
                    "agent_profile": record.agent_profile,
                    "result_schema": record.result_schema,
                    "effective_model": record.effective_model,
                    "parse_status": envelope["parser"]["parse_status"],
                })

            # 6. deterministic task tree
            tree = broker.task_tree(host_anchor.id)
            tree_ids = {node["task_id"] for node in tree["tasks"]}
            expected_tree = {
                host_anchor.id,
                codex_child.id,
                claude_child.id,
                mock_child.id,
                steer_target.id,
                follow_up.id,
            }
            check("task_tree lists bounded descendants", expected_tree <= tree_ids, f"missing={expected_tree - tree_ids}")
            check("task_tree is not truncated", tree.get("truncated") is False)
            evidence["task_tree_node_count"] = len(tree["tasks"])

            # 8. writer isolation alongside read-only children
            writer = broker.create(TaskRequest(
                "isolated writer",
                str(source),
                runtime="mock",
                execution_mode="isolated_worktree",
                parent_task_id=host_anchor.id,
                external_root_id=EXTERNAL_ROOT,
            ))
            blocked = broker.create(TaskRequest(
                "second writer must fail",
                str(source),
                runtime="mock",
                execution_mode="isolated_worktree",
                parent_task_id=host_anchor.id,
                external_root_id=EXTERNAL_ROOT,
            ))
            broker.start(writer.id)
            blocked_started = broker.start(blocked.id)
            check("second isolated writer is rejected", blocked_started.state == TaskState.FAILED)
            events = broker.store.events(blocked.id)
            check(
                "writer lease conflict is recorded",
                any(event.get("metadata", {}).get("reason") == "workspace_lease_conflict" for event in events),
            )
            evidence["writer_isolation_enforced"] = blocked_started.state == TaskState.FAILED
            broker.complete(writer.id, "writer finished")

            head_after = run_git(["rev-parse", "HEAD"], cwd=source).stdout.strip()
            porcelain = run_git(["status", "--porcelain"], cwd=source).stdout
            check("read-only children leave source HEAD unchanged", head_before == head_after)
            check("read-only children leave source workspace clean", porcelain.strip() == "")
        finally:
            broker.close()

    if EVIDENCE_PATH.is_file():
        expected = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
        check("recorded evidence marks fixture-only mode", expected.get("provider_calls") is False)
        check("recorded evidence lists three heterogeneous children", len(expected.get("children", [])) == 3)
        check(
            "recorded evidence documents the acceptance command",
            any("side_agent_fixture_acceptance.py" in cmd for cmd in expected.get("commands", [])),
        )

    print()
    if _failures:
        print(f"Side-agent fixture acceptance FAILED ({len(_failures)} check(s)): {', '.join(_failures)}")
        return 1

    print(
        "Side-agent fixture acceptance PASSED: heterogeneous bounded children, completion-event "
        "cursor polling, provenance-aware results, task tree, steering refusal with continues "
        "follow-up, and writer isolation — all without network or provider credentials."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
