"""Provenance-aware structured result normalization (MR 8.6).

Parses runtime-reported output into a versioned envelope with explicit trust
zones. Unknown result_schema values are rejected before launch; they are never
silently reinterpreted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import TaskRecord, TaskState

NORMALIZED_RESULT_ARTIFACT = "normalized_result.json"
RAW_OUTPUT_ARTIFACT = "runtime_raw_output.txt"
ENVELOPE_VERSION = 1

SUPPORTED_RESULT_SCHEMAS = frozenset({
    "plain-summary",
    "evidence-report",
    "review-findings",
    "implementation-report",
})
DEFAULT_RESULT_SCHEMA = "plain-summary"

SCHEMA_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "plain-summary": frozenset({"summary"}),
    "evidence-report": frozenset({"summary"}),
    "review-findings": frozenset({"summary", "findings"}),
    "implementation-report": frozenset({"summary"}),
}


class UnknownResultSchemaError(ValueError):
    def __init__(self, schema: str):
        self.schema = schema
        super().__init__(
            f"Unknown result_schema {schema!r}: supported schemas are {sorted(SUPPORTED_RESULT_SCHEMAS)}"
        )


def effective_result_schema(record: TaskRecord) -> str:
    return record.result_schema or DEFAULT_RESULT_SCHEMA


def validate_result_schema(schema: str | None) -> None:
    if schema is not None and schema not in SUPPORTED_RESULT_SCHEMAS:
        raise UnknownResultSchemaError(schema)


def _artifact_refs(
    manifest: dict[str, Any],
    *,
    exclude: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    skip = exclude or frozenset()
    return [
        {"name": item["name"], "sha256": item["sha256"], "bytes": item["bytes"]}
        for item in manifest.get("files", [])
        if isinstance(item, dict) and "name" in item and item["name"] not in skip
    ]


def _try_parse_structured_text(text: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    if not isinstance(text, str) or not text.strip():
        return None, warnings
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None, warnings
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        warnings.append(f"summary looked like JSON but failed to parse: {error.msg}")
        return None, warnings
    if not isinstance(parsed, dict):
        warnings.append("structured summary JSON must be an object")
        return None, warnings
    return parsed, warnings


def _runtime_reported_from_structured(
    structured: dict[str, Any] | None,
    *,
    summary: str | None,
    collected: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": summary,
        "findings": [],
        "claimed_evidence": [],
        "claimed_commands": [],
        "claimed_tests": [],
        "unresolved_questions": [],
    }
    session_metadata: dict[str, Any] = {}
    for key in ("session_id", "thread_id", "num_turns", "usage", "permission_denials"):
        if collected.get(key) is not None:
            session_metadata[key] = collected[key]
    if session_metadata:
        payload["session_metadata"] = session_metadata
    if structured is None:
        return payload

    if isinstance(structured.get("summary"), str) and structured["summary"].strip():
        payload["summary"] = structured["summary"].strip()
    for key, target in (
        ("findings", "findings"),
        ("claimed_evidence", "claimed_evidence"),
        ("evidence", "claimed_evidence"),
        ("unresolved_questions", "unresolved_questions"),
    ):
        value = structured.get(key)
        if isinstance(value, list):
            payload[target] = value
    commands = structured.get("commands_executed")
    if commands is None:
        commands = structured.get("claimed_commands")
    if isinstance(commands, list):
        payload["claimed_commands"] = commands
    tests = structured.get("tests_reported")
    if tests is None:
        tests = structured.get("claimed_tests")
    if isinstance(tests, list):
        payload["claimed_tests"] = tests
    return payload


def _parse_status_and_warnings(
    schema: str,
    runtime_reported: dict[str, Any],
    *,
    structured: dict[str, Any] | None,
    parse_warnings: list[str],
    collected: dict[str, Any],
) -> tuple[str, list[str]]:
    warnings = list(parse_warnings)
    malformed = collected.get("malformed_output_lines", 0) or collected.get("malformed_event_lines", 0)
    if malformed:
        warnings.append(f"runtime emitted {malformed} malformed structured-output line(s)")
    summary = runtime_reported.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        if schema == "plain-summary":
            return "partial", warnings + ["no runtime summary text available"]
        return "failed", warnings + ["required summary missing"]

    if structured is None and schema != "plain-summary":
        warnings.append(f"no structured JSON payload for schema {schema!r}; using plain summary fallback")
        return "fallback", warnings

    required = SCHEMA_REQUIRED_FIELDS[schema]
    missing = [field for field in required if not _field_present(field, runtime_reported, structured)]
    if missing:
        warnings.append(f"missing required field(s) for {schema}: {', '.join(sorted(missing))}")
        if schema == "plain-summary":
            return "partial", warnings
        return "partial" if structured is not None else "fallback", warnings
    return "ok", warnings


def _field_present(field: str, runtime_reported: dict[str, Any], structured: dict[str, Any] | None) -> bool:
    if field == "summary":
        value = runtime_reported.get("summary")
        return isinstance(value, str) and bool(value.strip())
    if field == "findings":
        findings = runtime_reported.get("findings")
        return isinstance(findings, list) and len(findings) > 0
    return False


def resolve_raw_output_artifact(
    artifacts_dir: Path,
    launch: dict[str, Any] | None,
    collected: dict[str, Any],
) -> str | None:
    if launch is not None:
        for key in ("events_artifact", "stderr_artifact"):
            name = launch.get(key)
            if isinstance(name, str) and (artifacts_dir / name).is_file():
                return name
    for key in ("events_artifact", "stderr_artifact"):
        name = collected.get(key)
        if isinstance(name, str) and (artifacts_dir / name).is_file():
            return name
    if (artifacts_dir / RAW_OUTPUT_ARTIFACT).is_file():
        return RAW_OUTPUT_ARTIFACT
    return None


def read_raw_runtime_output(
    artifacts_dir: Path,
    launch: dict[str, Any] | None,
    collected: dict[str, Any],
) -> str:
    ref = resolve_raw_output_artifact(artifacts_dir, launch, collected)
    if ref is None:
        summary = collected.get("summary")
        return summary if isinstance(summary, str) else ""
    return (artifacts_dir / ref).read_text(errors="replace")


def persist_raw_runtime_output_if_needed(
    store: Any,
    task_id: str,
    *,
    launch: dict[str, Any] | None,
    collected: dict[str, Any],
) -> str | None:
    artifacts_dir = store.artifacts / task_id
    existing = resolve_raw_output_artifact(artifacts_dir, launch, collected)
    if existing is not None:
        return existing
    summary = collected.get("summary")
    if not isinstance(summary, str) or not summary:
        return None
    store.write_artifact(task_id, RAW_OUTPUT_ARTIFACT, summary if summary.endswith("\n") else summary + "\n")
    return RAW_OUTPUT_ARTIFACT


def build_normalized_envelope(
    *,
    record: TaskRecord,
    result: dict[str, Any],
    collected: dict[str, Any],
    gate: dict[str, Any],
    verification: dict[str, Any] | None,
    manifest: dict[str, Any],
    launch: dict[str, Any] | None,
    raw_output_artifact: str | None,
    final_state: TaskState,
) -> dict[str, Any]:
    schema = effective_result_schema(record)
    summary = collected.get("summary")
    if summary is None and isinstance(result.get("summary"), str):
        summary = result["summary"]
    structured, parse_warnings = _try_parse_structured_text(summary if isinstance(summary, str) else None)
    runtime_reported = _runtime_reported_from_structured(structured, summary=summary, collected=collected)
    parse_status, warnings = _parse_status_and_warnings(
        schema,
        runtime_reported,
        structured=structured,
        parse_warnings=parse_warnings,
        collected=collected,
    )

    broker_verification = None
    if verification is not None:
        broker_verification = {
            "broker_verified": True,
            "scope": verification.get("scope"),
            "commands": verification.get("commands"),
        }

    return {
        "envelope_version": ENVELOPE_VERSION,
        "task_id": record.id,
        "state": final_state.value,
        "runtime_reported": runtime_reported,
        "broker_observed": {
            "terminal_state": final_state.value,
            "exit_code": collected.get("exit_code"),
            "process_exit_code": collected.get("process_exit_code"),
            "artifact_manifest_ref": "manifest.json",
            "artifact_refs": _artifact_refs(manifest, exclude=frozenset({NORMALIZED_RESULT_ARTIFACT})),
            "verification": broker_verification,
            "verification_gate": {
                "policy": gate.get("policy"),
                "outcome": gate.get("outcome"),
            },
        },
        "parser": {
            "requested_schema": schema,
            "parse_status": parse_status,
            "warnings": warnings,
            "raw_output_artifact": raw_output_artifact,
            "malformed_output_lines": collected.get("malformed_output_lines", 0)
            or collected.get("malformed_event_lines", 0),
        },
    }


def concise_normalized_view(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    if envelope is None:
        return None
    runtime = envelope.get("runtime_reported") or {}
    parser = envelope.get("parser") or {}
    broker = envelope.get("broker_observed") or {}
    summary = runtime.get("summary")
    view: dict[str, Any] = {
        "envelope_version": envelope.get("envelope_version"),
        "state": envelope.get("state"),
        "requested_schema": parser.get("requested_schema"),
        "parse_status": parser.get("parse_status"),
        "summary": summary if isinstance(summary, str) else None,
        "warnings": parser.get("warnings") or [],
        "raw_output_artifact": parser.get("raw_output_artifact"),
        "artifact_manifest_ref": broker.get("artifact_manifest_ref"),
    }
    if parser.get("warnings"):
        view["has_parser_warnings"] = True
    verification = broker.get("verification")
    if verification is not None:
        view["broker_verification_present"] = True
    return view
