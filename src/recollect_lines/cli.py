from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .claude_code_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter
from .cursor_adapter import CursorAdapter
from .models import VERIFICATION_POLICIES, InvalidTransition, TaskRequest, translate_delegate_fields
from .opencode_adapter import OpenCodeAdapter
from .doctor import format_human_report as format_doctor_report, run_config_validate, run_doctor
from .certify import format_human_report as format_certify_report, run_certify, CertifyRequest
from .init import InitError, format_human_report as format_init_report, run_init
from .operator_control import OperatorControlRefused
from .mcp_commands import (
    McpCommandError,
    format_human_report as format_mcp_report,
    run_mcp_install,
    run_mcp_print,
)
from .provider_commands import (
    format_human_report as format_provider_report,
    run_provider_add,
    run_provider_list,
    run_provider_show,
    run_provider_test,
)
from .providers import OPERATOR_CONFIG_DIRNAME, ProviderConfigError, resolve_providers_config_source, write_local_config_file
from .service import Broker


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recollect-lines")
    p.add_argument("--home", type=Path, default=Path(".recollect"))
    p.add_argument(
        "--opencode-command", default=None,
        help=(
            "Advanced: override the opencode adapter's command prefix as a JSON array "
            "(e.g. to pin a specific opencode-ai version, or point at a deterministic "
            "stand-in binary for testing). Defaults to the built-in npx opencode-ai invocation."
        ),
    )
    p.add_argument(
        "--claude-command", default=None,
        help=(
            "Advanced: override the Claude Code adapter's command prefix as a JSON array "
            "(e.g. to point at a deterministic stand-in binary for testing). "
            "Defaults to the built-in `claude` CLI invocation."
        ),
    )
    p.add_argument(
        "--codex-command", default=None,
        help=(
            "Advanced: override the Codex adapter's command prefix as a JSON array "
            "(e.g. to point at a deterministic stand-in binary for testing). "
            "Defaults to the built-in `codex` CLI invocation."
        ),
    )
    p.add_argument(
        "--cursor-command", default=None,
        help=(
            "Advanced: override the Cursor adapter's command prefix as a JSON array "
            "(e.g. to point at a deterministic stand-in binary for testing). "
            "Defaults to the built-in `cursor-agent` CLI invocation."
        ),
    )
    p.add_argument(
        "--providers-config", type=Path, default=None,
        help=(
            "Path to a JSON or YAML provider configuration file (required for openai_compatible "
            "profile tasks). Highest-precedence source; see docs/cli.md for the full resolution "
            "order (RECOLLECT_CONFIG env var, repo-local/user-level operator config, then the "
            "legacy providers.json default)."
        ),
    )
    p.add_argument(
        "--agent-profiles-config", type=Path, default=None,
        help="Path to a JSON agent profile configuration file (extends built-in profiles).",
    )
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--task", required=True)
    create.add_argument("--workspace", required=True)
    create.add_argument("--mode", default=None, help="Execution mode (defaults to read_only or agent profile default).")
    create.add_argument("--runtime", default=None, help="Execution backend identifier (preferred over --profile).")
    create.add_argument("--profile", default=None, help="Deprecated alias for --runtime.")
    create.add_argument("--model", default=None, help="Optional per-task model override (capability-gated by runtime).")
    create.add_argument("--agent-profile", dest="agent_profile", default=None, help="Optional behavioral agent profile name.")
    create.add_argument("--result-schema", dest="result_schema", default=None, help="Normalized result schema (plain-summary, evidence-report, review-findings, implementation-report).")
    create.add_argument(
        "--task-category",
        dest="task_category",
        default=None,
        choices=("prose", "review", "investigation", "implementation", "unknown"),
        help="Claude Code task category for permission-mode policy (optional; inferred when omitted).",
    )
    create.add_argument(
        "--claude-permission-mode",
        dest="claude_permission_mode",
        default=None,
        help="Explicit Claude Code --permission-mode override (validated per execution mode).",
    )
    create.add_argument(
        "--provider", default=None,
        help="Named provider from --providers-config (required when --runtime openai_compatible).",
    )
    create.add_argument("--timeout", type=int, default=None)
    create.add_argument("--verification-policy", default="none", choices=VERIFICATION_POLICIES)
    create.add_argument(
        "--verify-command", dest="verify_commands", action="append", default=None,
        help="JSON-encoded argv array run as broker-verified evidence when this task is collected; may be repeated",
    )
    create.add_argument("--parent-task-id", dest="parent_task_id", default=None, help="Optional broker parent task id.")
    create.add_argument("--external-root-id", dest="external_root_id", default=None, help="Audit-only host grouping id.")
    create.add_argument("--relationship", default=None, choices=("delegates", "continues"), help="Child relationship when parent is set.")
    create.add_argument("--origin-kind", dest="origin_kind", default=None, choices=("host", "side_agent"))
    create.add_argument("--origin-ref", dest="origin_ref", default=None, help="Audit-only caller reference.")
    for name in ("start", "status", "complete", "collect", "cancel", "timeout", "reconcile"):
        cmd = sub.add_parser(name)
        cmd.add_argument("task_id")
        if name == "complete":
            cmd.add_argument("--summary", required=True)
        if name in {"cancel", "timeout"}:
            cmd.add_argument("--reason", default="Cancelled or timed out by caller")
    control = sub.add_parser(
        "control",
        help="Operator recovery/control with an explicit action (status, cancel, collect, message)",
    )
    control.add_argument("task_id")
    control.add_argument(
        "--action",
        required=True,
        choices=("status", "cancel", "collect", "message"),
        help="Explicit control action; message is always an explicit unsupported refusal",
    )
    control.add_argument("--reason", default="Cancelled by operator control")
    control.add_argument(
        "--content",
        default=None,
        help="Required when --action message (always refused; no steering occurs)",
    )
    verify = sub.add_parser("verify")
    verify.add_argument("task_id")
    verify.add_argument(
        "--command", dest="commands", action="append", required=True,
        help="JSON-encoded argv array, e.g. '[\"pytest\", \"-q\"]'; may be repeated",
    )
    sub.add_parser("list")
    children = sub.add_parser("children", help="List direct child task summaries for a parent task")
    children.add_argument("task_id")
    task_tree = sub.add_parser("task-tree", help="Show bounded task tree for a root task id")
    task_tree.add_argument("root_task_id")
    completion_events = sub.add_parser(
        "completion-events",
        help="Poll durable completion signals from the global event cursor",
    )
    completion_events.add_argument(
        "--after-event-id",
        dest="after_event_id",
        type=int,
        default=0,
        help="Exclusive lower bound on durable event id (default 0)",
    )
    completion_events.add_argument("--limit", type=int, default=64)
    completion_events.add_argument("--task-id", dest="task_id", default=None)
    completion_events.add_argument("--root-task-id", dest="root_task_id", default=None)
    completion_events.add_argument(
        "--include-non-terminal",
        action="store_true",
        help="Include non-completion events (default returns terminal/recovery completion signals only)",
    )
    completion_events.add_argument(
        "--state",
        dest="states",
        action="append",
        default=None,
        help="Restrict to specific completion state(s); repeatable",
    )
    sub.add_parser("reconcile-all")
    sub.add_parser("discover")
    sub.add_parser("list-agent-profiles", help="List built-in and configured behavioral agent profiles")
    select = sub.add_parser("select")
    select.add_argument("--mode", dest="execution_mode", required=True)
    select.add_argument("--allowed-runtime", dest="allowed_runtimes", action="append", default=None)
    select.add_argument("--allowed-provider", dest="allowed_providers", action="append", default=None)
    select.add_argument("--require-runtime-capability", dest="runtime_capabilities", action="append", default=None,
                        help='JSON object fragment like \'{"isolated_worktree": true}\' (repeatable, merged)')
    select.add_argument("--require-provider-capability", dest="provider_capabilities", action="append", default=None,
                        help='JSON object fragment like \'{"chat_completions": true}\' (repeatable, merged)')
    select.add_argument("--include-unavailable", action="store_true", help="Do not exclude unavailable candidates")
    council = sub.add_parser("council")
    council_sub = council.add_subparsers(dest="council_command", required=True)
    council_validate = council_sub.add_parser("validate")
    council_validate.add_argument("--plan", required=True, help="JSON council plan")
    council_execute = council_sub.add_parser("execute")
    council_execute.add_argument("--plan", required=True, help="JSON council plan")
    init_cmd = sub.add_parser(
        "init",
        help=(
            "One-shot local setup: create the --home directory and a starter provider config "
            "only if absent (mode 0600 on POSIX), then run config validate"
        ),
    )
    init_cmd.add_argument("--force", action="store_true", help="Overwrite an existing operator config file")
    init_cmd.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
    doctor = sub.add_parser("doctor", help="Offline-safe operational diagnostics")
    doctor.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
    doctor.add_argument("--workspace", type=Path, default=None, help="Optional workspace path to validate")
    config_cmd = sub.add_parser("config", help="Provider configuration validation and local file generation")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_validate = config_sub.add_parser(
        "validate",
        help="Validate the resolved provider configuration (secrets redacted; values are never printed)",
    )
    config_validate.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
    config_init = config_sub.add_parser(
        "init",
        help="Write a minimal starter provider config (mode 0600 on POSIX); non-interactive, no real secrets",
    )
    config_init.add_argument(
        "--path", type=Path, default=None,
        help=f"Destination file (default: ./{OPERATOR_CONFIG_DIRNAME}/config.yaml)",
    )
    config_init.add_argument("--force", action="store_true", help="Overwrite an existing file")
    provider_cmd = sub.add_parser("provider", help="Provider identity management: list, add, show, test (secrets never captured/printed)")
    provider_sub = provider_cmd.add_subparsers(dest="provider_command", required=True)
    provider_list = provider_sub.add_parser("list", help="List configured providers (redacted; no secrets)")
    provider_list.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
    provider_show = provider_sub.add_parser("show", help="Show one configured provider (always fully redacted)")
    provider_show.add_argument("name")
    provider_show.add_argument(
        "--redacted", action="store_true",
        help="Explicit acknowledgement that output is always redacted; no raw secret is ever stored or shown",
    )
    provider_show.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
    provider_add = provider_sub.add_parser(
        "add",
        help="Add a provider entry to a writable local/operator config (never accepts a raw secret value)",
    )
    provider_add.add_argument("--name", required=True, help="Provider name (lowercase, matches an existing entry only with --force)")
    provider_add.add_argument("--base-url", required=True, help="OpenAI-compatible base URL (https, or http for loopback with --allow-insecure-http)")
    provider_add.add_argument(
        "--api-key-env", required=True,
        help="Name of an environment variable holding the credential -- never a raw secret value",
    )
    provider_add.add_argument("--default-model", required=True)
    provider_add.add_argument("--request-timeout-seconds", type=int, default=None)
    provider_add.add_argument("--allow-insecure-http", action="store_true", help="Required to use an http:// loopback base_url")
    provider_add.add_argument("--ca-bundle", default=None, help="Filesystem path to a custom CA bundle (never inline cert/key content)")
    provider_add.add_argument(
        "--capability", dest="capabilities", action="append", default=None,
        help="KEY=true|false, repeatable (e.g. --capability streaming=true)",
    )
    provider_add.add_argument("--estimated-cost-usd-upper-bound", type=float, default=None)
    provider_add.add_argument(
        "--path", type=Path, default=None,
        help="Explicit destination config file, bypassing precedence resolution",
    )
    provider_add.add_argument("--force", action="store_true", help="Overwrite an existing provider entry with the same name")
    provider_add.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
    provider_test = provider_sub.add_parser(
        "test",
        help="Layered provider diagnostics (config, credential reference, capability); remote probe is opt-in only",
    )
    provider_test.add_argument("name")
    provider_test.add_argument("--live", action="store_true", help="Opt in to sending one real minimal chat-completions request")
    provider_test.add_argument(
        "--i-accept-billed-remote-calls", action="store_true",
        help="Required with --live: acknowledge a real, possibly billed remote model call",
    )
    provider_test.add_argument("--timeout", type=int, default=None, help="Override request_timeout_seconds for the remote probe only")
    provider_test.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
    mcp_cmd = sub.add_parser(
        "mcp",
        help="Print or install MCP host registration for supported parent tools (cursor, claude_code, codex)",
    )
    mcp_sub = mcp_cmd.add_subparsers(dest="mcp_action", required=True)
    for name, help_text in (
        ("print", "Side-effect-free preview of the MCP registration for a supported host"),
        ("install", "Idempotently install the MCP registration into a supported host config file"),
    ):
        parser = mcp_sub.add_parser(name, help=help_text)
        parser.add_argument(
            "--host",
            required=True,
            choices=("cursor", "claude_code", "codex"),
            help="Parent host to target (only hosts supported as runtimes in this project)",
        )
        parser.add_argument(
            "--scope",
            default="global",
            choices=("global", "project"),
            help="global (user-level) or project-scoped config location",
        )
        parser.add_argument(
            "--config-path",
            type=Path,
            default=None,
            help="Override the host config file path (for hermetic tests or non-default layouts)",
        )
        parser.add_argument(
            "--mcp-command",
            default=None,
            help="Override the recollect-mcp executable or module entrypoint (absolute path recommended)",
        )
        parser.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
        if name == "install":
            parser.add_argument(
                "--no-verify",
                action="store_true",
                help="Skip post-install structural/doctor/delegate verification",
            )
    certify = sub.add_parser("certify", help="Integration certification with explicit target selection")
    certify.add_argument("--profile", required=True, help="Target profile (required; no default)")
    certify.add_argument("--provider", default=None, help="Named provider (required when --profile openai_compatible)")
    certify.add_argument("--json", action="store_true", help="Emit stable redacted machine-readable JSON")
    certify.add_argument("--output", type=Path, default=None, help="Write redacted evidence JSON atomically to this path")
    certify.add_argument("--max-cost-usd", type=float, default=None, help="Operator budget ceiling for --execute-live")
    certify.add_argument("--fixture-execute", action="store_true", help="Run deterministic local fixture certification")
    certify.add_argument("--execute-live", action="store_true", help="Opt in to live remote execution (billed calls possible)")
    certify.add_argument(
        "--i-accept-billed-remote-calls",
        action="store_true",
        help="Required with --execute-live: acknowledge paid/billed remote model calls",
    )
    return p


def _merge_capability_flags(fragments: list[str] | None) -> dict[str, bool] | None:
    if not fragments:
        return None
    merged: dict[str, bool] = {}
    for fragment in fragments:
        parsed = json.loads(fragment)
        if not isinstance(parsed, dict):
            raise ValueError("capability requirements must be JSON objects")
        merged.update(parsed)
    return merged


def _parse_provider_capability_flags(fragments: list[str] | None) -> dict[str, bool] | None:
    if not fragments:
        return None
    parsed: dict[str, bool] = {}
    for fragment in fragments:
        key, sep, raw_value = fragment.partition("=")
        if not sep or raw_value.strip().lower() not in ("true", "false"):
            raise ValueError(f"--capability must be KEY=true|false, got {fragment!r}")
        parsed[key.strip()] = raw_value.strip().lower() == "true"
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    opencode_adapter = OpenCodeAdapter(command_prefix=tuple(json.loads(args.opencode_command))) if args.opencode_command else None
    claude_code_adapter = ClaudeCodeAdapter(command_prefix=tuple(json.loads(args.claude_command))) if args.claude_command else None
    codex_adapter = CodexAdapter(command_prefix=tuple(json.loads(args.codex_command))) if args.codex_command else None
    cursor_adapter = CursorAdapter(command_prefix=tuple(json.loads(args.cursor_command))) if args.cursor_command else None
    resolved_config = resolve_providers_config_source(
        explicit=args.providers_config,
        environ=os.environ,
        repo_root=Path.cwd(),
        user_home=Path.home(),
    )
    if args.command == "init":
        try:
            result, exit_code = run_init(
                home=args.home,
                force=args.force,
                explicit_providers_config=args.providers_config,
                environ=os.environ,
                repo_root=Path.cwd(),
                user_home=Path.home(),
            )
        except InitError as error:
            print(json.dumps({"error": {"code": "InitError", "message": str(error)}}, sort_keys=True))
            return 2
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(format_init_report(result))
        return exit_code
    if args.command == "doctor":
        report, exit_code = run_doctor(
            home=args.home,
            workspace=args.workspace,
            providers_config=resolved_config.path,
            providers_config_origin=resolved_config.origin,
            opencode_adapter=opencode_adapter,
            claude_code_adapter=claude_code_adapter,
            codex_adapter=codex_adapter,
            cursor_adapter=cursor_adapter,
        )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_doctor_report(report))
        return exit_code
    if args.command == "config":
        if args.config_command == "init":
            dest = args.path if args.path is not None else Path.cwd() / OPERATOR_CONFIG_DIRNAME / "config.yaml"
            try:
                written = write_local_config_file(dest, force=args.force)
            except FileExistsError as error:
                print(json.dumps({"error": {"code": "FileExistsError", "message": str(error)}}, sort_keys=True))
                return 2
            print(json.dumps({"written": str(written)}, sort_keys=True))
            return 0
        report, exit_code = run_config_validate(
            providers_config=resolved_config.path,
            providers_config_origin=resolved_config.origin,
        )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_doctor_report(report, command="config validate"))
        return exit_code
    if args.command == "mcp":
        try:
            common = {
                "host": args.host,
                "scope": args.scope,
                "home": args.home,
                "config_path": args.config_path,
                "mcp_command": args.mcp_command,
                "repo_root": Path.cwd(),
                "user_home": Path.home(),
            }
            if args.mcp_action == "print":
                report, exit_code = run_mcp_print(**common)
                command_label = "mcp print"
            else:
                report, exit_code = run_mcp_install(
                    **common,
                    verify=not args.no_verify,
                )
                command_label = "mcp install"
        except McpCommandError as error:
            print(json.dumps({
                "error": {"code": error.code, "message": error.message, **(
                    {"remediation": error.remediation} if error.remediation else {}
                )},
            }, sort_keys=True))
            return 2
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            if args.mcp_action == "print":
                print(report["rendered"].rstrip())
            else:
                print(format_mcp_report(report, command=command_label))
        return exit_code
    if args.command == "provider":
        try:
            if args.provider_command == "list":
                report, exit_code = run_provider_list(
                    providers_config=resolved_config.path,
                    providers_config_origin=resolved_config.origin,
                )
            elif args.provider_command == "show":
                report, exit_code = run_provider_show(
                    providers_config=resolved_config.path,
                    providers_config_origin=resolved_config.origin,
                    name=args.name,
                )
            elif args.provider_command == "add":
                report, exit_code = run_provider_add(
                    name=args.name,
                    base_url=args.base_url,
                    api_key_env=args.api_key_env,
                    default_model=args.default_model,
                    request_timeout_seconds=args.request_timeout_seconds,
                    allow_insecure_http=args.allow_insecure_http,
                    ca_bundle=args.ca_bundle,
                    capabilities=_parse_provider_capability_flags(args.capabilities),
                    estimated_cost_usd_upper_bound=args.estimated_cost_usd_upper_bound,
                    explicit_path=args.path,
                    resolved_source_path=resolved_config.path,
                    resolved_source_origin=resolved_config.origin,
                    force=args.force,
                )
            else:
                report, exit_code = run_provider_test(
                    name=args.name,
                    providers_config=resolved_config.path,
                    providers_config_origin=resolved_config.origin,
                    live=args.live,
                    acknowledge_billed_remote_calls=args.i_accept_billed_remote_calls,
                    timeout_override=args.timeout,
                )
        except (ProviderConfigError, ValueError, OSError) as error:
            print(json.dumps({"error": {"code": type(error).__name__, "message": str(error)}}, sort_keys=True))
            return 2
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_provider_report(report, command=f"provider {args.provider_command}"))
        return exit_code
    if args.command == "certify":
        report, exit_code = run_certify(CertifyRequest(
            home=args.home,
            profile=args.profile,
            provider=args.provider,
            providers_config=resolved_config.path,
            output=args.output,
            max_cost_usd=args.max_cost_usd,
            execute_live=args.execute_live,
            acknowledge_billed_remote_calls=args.i_accept_billed_remote_calls,
            fixture_execute=args.fixture_execute,
            opencode_adapter=opencode_adapter,
            claude_code_adapter=claude_code_adapter,
            codex_adapter=codex_adapter,
            cursor_adapter=cursor_adapter,
        ))
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_certify_report(report))
        return exit_code
    broker = Broker(
        args.home,
        opencode_adapter=opencode_adapter,
        claude_code_adapter=claude_code_adapter,
        codex_adapter=codex_adapter,
        cursor_adapter=cursor_adapter,
        providers_config=resolved_config.path,
        providers_config_origin=resolved_config.origin,
        agent_profiles_config=args.agent_profiles_config,
    )
    try:
        if args.command == "create":
            explicit_fields: set[str] = set()
            execution_mode = args.mode if args.mode is not None else "read_only"
            if args.mode is not None:
                explicit_fields.add("execution_mode")
            timeout_seconds = args.timeout if args.timeout is not None else 1800
            if args.timeout is not None:
                explicit_fields.add("timeout_seconds")
            if args.model is not None:
                explicit_fields.add("model")
            if args.agent_profile is not None:
                explicit_fields.add("agent_profile")
            if args.result_schema is not None:
                explicit_fields.add("result_schema")
            if args.task_category is not None:
                explicit_fields.add("task_category")
            if args.claude_permission_mode is not None:
                explicit_fields.add("claude_permission_mode")
            runtime, model, agent_profile, result_schema, compatibility = translate_delegate_fields(
                runtime=args.runtime,
                profile=args.profile,
                model=args.model,
                agent_profile=args.agent_profile,
                result_schema=args.result_schema,
            )
            request = TaskRequest(
                args.task,
                args.workspace,
                execution_mode,
                runtime,
                args.provider,
                timeout_seconds,
                args.verification_policy,
                runtime=runtime,
                model=model,
                agent_profile=agent_profile,
                result_schema=result_schema,
                task_category=args.task_category,
                claude_permission_mode=args.claude_permission_mode,
                compatibility=compatibility,
                explicit_fields=frozenset(explicit_fields),
                parent_task_id=args.parent_task_id,
                external_root_id=args.external_root_id,
                relationship=args.relationship,
                origin_kind=args.origin_kind,
                origin_ref=args.origin_ref,
            )
            verify_commands = [json.loads(command) for command in args.verify_commands] if args.verify_commands else None
            record = broker.create(request, verify_commands=verify_commands)
            output = record.json()
            if compatibility is not None:
                output = {**output, "compatibility": compatibility}
        elif args.command == "start":
            output = broker.start(args.task_id).json()
        elif args.command == "complete":
            output = broker.complete(args.task_id, args.summary).json()
        elif args.command == "collect":
            output = broker.collect(args.task_id).json()
        elif args.command == "cancel":
            output = broker.cancel(args.task_id, args.reason).json()
        elif args.command == "timeout":
            output = broker.timeout(args.task_id, args.reason).json()
        elif args.command == "verify":
            output = broker.verify(args.task_id, [json.loads(command) for command in args.commands])
        elif args.command == "status":
            output = broker.status(args.task_id)
        elif args.command == "control":
            output = broker.operator_control(
                args.task_id,
                args.action,
                reason=args.reason,
                message_content=args.content,
            )
        elif args.command == "reconcile":
            record = broker.reconcile(args.task_id)
            output = {**record.json(), "reconciliation": broker.reconcile_detail(args.task_id)}
        elif args.command == "reconcile-all":
            output = [
                {**record.json(), "reconciliation": broker.reconcile_detail(record.id)}
                for record in broker.reconcile_pending()
            ]
        elif args.command == "discover":
            output = broker.discover_capabilities()
        elif args.command == "list-agent-profiles":
            output = broker.list_agent_profiles()
        elif args.command == "select":
            output = broker.select_candidates(
                execution_mode=args.execution_mode,
                required_runtime_capabilities=_merge_capability_flags(args.runtime_capabilities),
                required_provider_capabilities=_merge_capability_flags(args.provider_capabilities),
                allowed_runtimes=args.allowed_runtimes,
                allowed_providers=args.allowed_providers,
                require_available=not args.include_unavailable,
            )
        elif args.command == "children":
            output = {"parent_task_id": args.task_id, "children": broker.children(args.task_id)}
        elif args.command == "task-tree":
            output = broker.task_tree(args.root_task_id)
        elif args.command == "completion-events":
            states = frozenset(args.states) if args.states else None
            output = broker.completion_events_since(
                args.after_event_id,
                limit=args.limit,
                task_id=args.task_id,
                root_task_id=args.root_task_id,
                completion_only=not args.include_non_terminal,
                states=states,
            )
        elif args.command == "council":
            plan = json.loads(args.plan)
            if args.council_command == "validate":
                output = broker.validate_council(plan)
            else:
                output = broker.execute_council(plan)
        else:
            output = [record.json() for record in broker.store.list()]
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except OperatorControlRefused as error:
        print(json.dumps({
            "error": {"code": error.code, "message": error.message},
            "control": error.control,
        }, sort_keys=True))
        return 3
    except (KeyError, ValueError, InvalidTransition) as error:
        print(json.dumps({"error": {"code": type(error).__name__, "message": str(error)}}, sort_keys=True))
        return 2
    finally:
        broker.close()
