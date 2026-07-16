"""Versioned, provider-neutral result-schema output contracts for launch prompts.

Contracts are prompt-level guidance only. They do not enable provider-native
structured output APIs; the broker still parses runtime-reported text heuristically.
"""

from __future__ import annotations

from .agent_profiles import compose_task_prompt
from .result_normalization import DEFAULT_RESULT_SCHEMA, UnknownResultSchemaError, validate_result_schema

RESULT_SCHEMA_PROMPT_VERSION = 1
_CONTRACT_MARKER = f"recollect-lines result-schema contract v{RESULT_SCHEMA_PROMPT_VERSION}"

_SCHEMA_FIELD_GUIDANCE: dict[str, str] = {
    "evidence-report": (
        "Required JSON fields:\n"
        "- summary (string): concise investigation outcome.\n"
        "Optional JSON fields:\n"
        "- findings (array of objects): structured observations.\n"
        "- claimed_evidence (array of strings) or evidence (array): cited paths or artifacts.\n"
        "- commands_executed (array) or claimed_commands (array): commands you report running.\n"
        "- unresolved_questions (array of strings): open questions."
    ),
    "review-findings": (
        "Required JSON fields:\n"
        "- summary (string): concise review outcome.\n"
        "- findings (array of objects): each item should describe one review finding."
    ),
    "implementation-report": (
        "Required JSON fields:\n"
        "- summary (string): concise change outcome.\n"
        "Optional JSON fields:\n"
        "- commands_executed (array) or claimed_commands (array): commands you report running.\n"
        "- tests_reported (array of objects) or claimed_tests (array): tests you report running."
    ),
}


def result_schema_prompt(schema: str) -> str:
    """Return prompt-level output contract text for *schema*, or '' for plain-summary."""
    validate_result_schema(schema)
    if schema == DEFAULT_RESULT_SCHEMA:
        return ""
    guidance = _SCHEMA_FIELD_GUIDANCE.get(schema)
    if guidance is None:
        raise UnknownResultSchemaError(schema)
    return (
        f"[{_CONTRACT_MARKER}: {schema}]\n"
        "Your final response must be exactly one JSON object and nothing else.\n"
        "Do not wrap the JSON in Markdown code fences or add prose before or after it.\n"
        f"{guidance}"
    )


def compose_launch_prompt(
    *,
    prompt_prefix: str | None,
    task_text: str,
    result_schema: str,
) -> tuple[str, str | None]:
    """Join profile prefix, task text, and optional schema contract."""
    base = compose_task_prompt(prompt_prefix or "", task_text)
    contract = result_schema_prompt(result_schema)
    if not contract:
        return base, None
    return f"{base}\n\n{contract}", contract
