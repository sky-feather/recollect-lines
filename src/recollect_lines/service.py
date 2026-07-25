from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .adaptor.contracts import AdapterCapabilities
from .capability_contract import describe_unsupported_execution_mode, materialization_prompt_notice
from .capability_contract_result import STATUS_UNSATISFIED, evaluate_capability_contract
from .durable_reconciliation import (
    AdoptedDurableHandle,
    LAUNCH_KIND_DURABLE,
    ReconcileDetail,
    ReconcileOutcome,
    adopt_durable_handle,
    adopted_cancel,
    adopted_collect,
    adopted_status,
    evaluate_durable_reconciliation,
    is_durable_launch_row,
    load_broker_identity,
    make_reconcile_detail,
)
from .durable_cli_launch import is_durable_launch_terminal, wait_for_durable_launch_terminal
from .durable_runner import DurableSubprocessRunner, classify_process_identity
from .adaptor.fixture_durable import FixtureDurableAdapter
from .recovery_contract import SYNTHETIC_RECOVERY_CONTROL
from .adaptor.claude_code import ClaudeCodeAdapter
from .adaptor.codex import CodexAdapter
from .adaptor.cursor import CursorAdapter
from .direct_api_runtime import DIRECT_API_PROFILE, OpenAiCompatibleDirectRuntime
from .agent_profiles import (
    get_agent_profile,
    list_agent_profiles,
    load_agent_profiles_config,
    merge_agent_profile_registries,
    resolution_artifact_payload,
    resolve_agent_profile,
)
from .model_selection import (
    ModelSelectionRefusedError,
    model_selection_metadata,
    resolve_effective_model,
    validate_requested_model,
)
from .models import (
    TERMINAL_STATES,
    VERIFICATION_POLICIES,
    ProfilePolicy,
    RecoveryRequired,
    TaskRecord,
    TaskRequest,
    TaskState,
    WorkspaceLeaseConflict,
    effective_runtime,
    now,
    request_artifact_payload,
    validate_result,
    validate_verify_commands,
)
from .adaptor.opencode import OpenCodeAdapter
from .adaptor.process import group_alive, group_dead_within, redact_command
from .providers import ProviderConfigError, load_providers_config
from .runtime_registry import DEFAULT_RUNTIME_REGISTRY, RuntimeDescriptor, RuntimeRegistry, ExecutionStrategy, ModelSelectionSupport, SUBPROCESS_LIMITATIONS
from .result_schema_capabilities import evaluate_result_schema_preflight
from .required_capabilities import (
    CapabilityPreflightContext,
    RequiredCapabilityValidationError,
    evaluate_capability_preflight,
    normalize_required_capabilities,
)
from .tool_access_profile import (
    ToolAccessProfileValidationError,
    build_tool_access_profile_registry,
    evaluate_tool_access_profile_preflight,
    load_tool_access_profiles_config,
    normalize_tool_access_profile,
    resolve_tool_access_profile,
    tool_access_profile_audit_payload,
)
from .model_profile import (
    ModelProfileValidationError,
    build_model_profile_registry,
    evaluate_model_profile_preflight,
    load_model_profiles_config,
    model_profile_public_projection,
    normalize_model_profile,
    resolve_model_profile_snapshot,
    unconfigured_model_profile_snapshot,
)
from .cost_rework_policy import (
    CostReworkPolicyValidationError,
    build_cost_rework_policy_registry,
    build_cost_policy_snapshot,
    compute_workflow_usage,
    cost_policy_public_projection,
    evaluate_cost_rework_preflight,
    load_cost_rework_policies_config,
    normalize_cost_rework_policy,
    normalize_rework_metadata,
    unconfigured_cost_policy_snapshot,
)
from .result_normalization import (
    NORMALIZED_RESULT_ARTIFACT,
    build_normalized_envelope,
    concise_normalized_view,
    effective_result_schema,
    normalize_permission_denials,
    persist_raw_runtime_output_if_needed,
    validate_result_schema,
)
from .result_schema_prompt import RESULT_SCHEMA_PROMPT_VERSION, compose_launch_prompt
from .completion_events import completion_events_page
from .contract_conflict import detect_schema_prose_conflict
from .store import TaskStore
from .task_lineage import (
    DEFAULT_LINEAGE_POLICY,
    MAX_TREE_NODES,
    LineagePolicy,
    concise_task_summary,
    reject_forbidden_lineage_keys,
    resolve_lineage,
)
from .workspace import WorkspaceError, WorkspaceManager, canonical_source, capture_head

ISOLATED_WORKTREE = "isolated_worktree"

# ponytail: bounded, not event-driven -- a full async/notify collect redesign
# is out of scope for this P0 containment slice (see RFC-004's durable
# supervisor migration). This only stops collect() from blocking the single
# synchronous MCP/CLI call site indefinitely on a still-running legacy
# subprocess; a caller that hits the bound just gets an explicit nonterminal
# result back and can retry once the runtime actually finishes.
LEGACY_PROCESS_COLLECT_GRACE_SECONDS = 5.0


class MockAdapter:
    name = "mock"
    capabilities = AdapterCapabilities(
        requires_subprocess=False,
        supports_process_group_cancellation=False,
        reports_broker_verified_tests=False,
        recovery_control=SYNTHETIC_RECOVERY_CONTROL,
    )

    def start_metadata(self, record: TaskRecord, workspace: str) -> dict[str, str]:
        return {"adapter": self.name, "mode": record.execution_mode, "workspace": workspace}


class Broker:
    def __init__(
        self,
        home: Path,
        profiles: dict[str, ProfilePolicy] | None = None,
        runtime_registry: RuntimeRegistry | None = None,
        opencode_adapter: OpenCodeAdapter | None = None,
        claude_code_adapter: ClaudeCodeAdapter | None = None,
        codex_adapter: CodexAdapter | None = None,
        cursor_adapter: CursorAdapter | None = None,
        fixture_durable_adapter: FixtureDurableAdapter | None = None,
        providers_config: Path | None = None,
        providers_config_origin: str | None = None,
        agent_profiles_config: Path | None = None,
        agent_profiles: dict | None = None,
        tool_access_profiles: dict | None = None,
        tool_access_profile_registry=None,
        model_profiles: dict | None = None,
        model_profile_registry=None,
        cost_rework_policies: dict | None = None,
        cost_rework_policy_registry=None,
        direct_api_runtime: OpenAiCompatibleDirectRuntime | None = None,
        lineage_policy: LineagePolicy | None = None,
        environ: dict[str, str] | None = None,
    ):
        self.store = TaskStore(home)
        self.adapter = MockAdapter()
        self.opencode_adapter = opencode_adapter or OpenCodeAdapter()
        configured_profiles = tool_access_profiles
        if configured_profiles is None and tool_access_profile_registry is None and providers_config is not None:
            try:
                configured_profiles = load_tool_access_profiles_config(providers_config)
            except Exception:
                configured_profiles = {}
        if tool_access_profile_registry is not None:
            self.tool_access_profile_registry = tool_access_profile_registry
        else:
            self.tool_access_profile_registry = build_tool_access_profile_registry(
                configured=configured_profiles or {},
            )
        configured_model_profiles = model_profiles
        if configured_model_profiles is None and model_profile_registry is None and providers_config is not None:
            try:
                configured_model_profiles = load_model_profiles_config(providers_config)
            except Exception:
                configured_model_profiles = {}
        if model_profile_registry is not None:
            self.model_profile_registry = model_profile_registry
        else:
            self.model_profile_registry = build_model_profile_registry(
                configured=configured_model_profiles or {},
            )
        configured_cost_policies = cost_rework_policies
        if (
            configured_cost_policies is None
            and cost_rework_policy_registry is None
            and providers_config is not None
        ):
            try:
                configured_cost_policies = load_cost_rework_policies_config(providers_config)
            except Exception:
                configured_cost_policies = {}
        if cost_rework_policy_registry is not None:
            self.cost_rework_policy_registry = cost_rework_policy_registry
        else:
            self.cost_rework_policy_registry = build_cost_rework_policy_registry(
                configured=configured_cost_policies or {},
            )
        self.claude_code_adapter = claude_code_adapter or ClaudeCodeAdapter(
            tool_access_profile_registry=self.tool_access_profile_registry,
        )
        if claude_code_adapter is not None:
            self.claude_code_adapter.tool_access_profile_registry = self.tool_access_profile_registry
        self.codex_adapter = codex_adapter or CodexAdapter()
        self.cursor_adapter = cursor_adapter or CursorAdapter()
        # Broker owns the one durable-subprocess supervisor and injects it into
        # every LaunchSpec-based adapter (Cursor, Claude Code, Codex, and
        # OpenCode, RFC-004) -- an adapter never constructs or configures its
        # own DurableSubprocessRunner.
        self.durable_runner = DurableSubprocessRunner(self.store.home)
        self.cursor_adapter.durable_runner = self.durable_runner
        self.claude_code_adapter.durable_runner = self.durable_runner
        self.codex_adapter.durable_runner = self.durable_runner
        self.opencode_adapter.durable_runner = self.durable_runner
        self.fixture_durable_adapter = fixture_durable_adapter
        # Every subprocess-backed adapter, keyed by the profile name that selects
        # it — the one place profile-to-adapter dispatch lives, so start()/
        # collect()/cancel()/timeout()/reconcile() never hard-code a specific
        # adapter's name to decide whether a task has a supervised process.
        self.subprocess_adapters = {
            self.opencode_adapter.name: self.opencode_adapter,
            self.claude_code_adapter.name: self.claude_code_adapter,
            self.codex_adapter.name: self.codex_adapter,
            self.cursor_adapter.name: self.cursor_adapter,
        }
        if self.fixture_durable_adapter is not None:
            self.subprocess_adapters[self.fixture_durable_adapter.name] = self.fixture_durable_adapter
        self.runtime_registry = (runtime_registry or DEFAULT_RUNTIME_REGISTRY).copy()
        seed_profiles = profiles or self.runtime_registry.policies()
        for name, policy in seed_profiles.items():
            if self.runtime_registry.contains(name):
                continue
            adapter = self.subprocess_adapters.get(name)
            caps = adapter.capabilities if adapter is not None else self.adapter.capabilities
            self.runtime_registry.register(RuntimeDescriptor(
                name=name,
                execution_strategy=ExecutionStrategy.FIXTURE,
                policy=policy,
                adapter_capabilities=caps,
                limitations=SUBPROCESS_LIMITATIONS,
                model_selection=ModelSelectionSupport.PERSISTED_NOT_INVOKED,
                runtime_label=name if adapter is None else None,
            ))
        if profiles is not None:
            self.profiles = {**self.runtime_registry.policies(), **profiles}
        else:
            self.profiles = self.runtime_registry.policies()
        self.workspaces = WorkspaceManager(self.store.home)
        self._environ = environ
        if direct_api_runtime is not None:
            self.direct_api_runtime = direct_api_runtime
        elif providers_config is not None:
            self.direct_api_runtime = OpenAiCompatibleDirectRuntime(
                load_providers_config(providers_config), environ=environ, config_source=providers_config,
                config_source_origin=providers_config_origin,
            )
        else:
            self.direct_api_runtime = None
        custom_profiles = {}
        if agent_profiles is not None:
            custom_profiles = agent_profiles
        elif agent_profiles_config is not None:
            custom_profiles = load_agent_profiles_config(agent_profiles_config)
        self.agent_profiles = merge_agent_profile_registries(custom_profiles)
        self.lineage_policy = lineage_policy or DEFAULT_LINEAGE_POLICY
        # In-memory only, one broker process per running task; a restart loses
        # this dict. That's fine: `store.runtime_launches` is the durable record
        # a fresh Broker reconciles against (see reconcile()/reconcile_pending()
        # and docs/history/phases/phase-5b.md). Transparent re-attachment remains out of scope.
        self._process_handles: dict[str, object] = {}
        self._direct_api_handles: dict[str, object] = {}
        self._adopted_durable_handles: dict[str, AdoptedDurableHandle] = {}
        self._broker_identity = load_broker_identity(self.store.home)
        self._last_reconcile_details: dict[str, ReconcileDetail] = {}

    def close(self) -> None:
        for task_id in list(self._adopted_durable_handles):
            self.store.release_recovery_lease(task_id)
        self._adopted_durable_handles.clear()
        self.store.close()

    def reconcile_detail(self, task_id: str) -> dict | None:
        detail = self._last_reconcile_details.get(task_id)
        return detail.to_dict() if detail else None

    def _adapter_default_model(self, runtime: str) -> str | None:
        adapter = self.subprocess_adapters.get(runtime)
        if adapter is None:
            return None
        return getattr(adapter, "model", None)

    def _resolve_launch_model(self, record: TaskRecord) -> tuple[TaskRecord, dict[str, object]]:
        descriptor = self.runtime_registry.get(record.runtime)
        validate_requested_model(descriptor, record.model)
        provider_default = None
        if record.runtime == DIRECT_API_PROFILE:
            assert self.direct_api_runtime is not None and record.provider is not None
            provider_default = self.direct_api_runtime.get_provider(record.provider).default_model
        effective_model, source = resolve_effective_model(
            descriptor,
            requested_model=record.model,
            adapter_default=self._adapter_default_model(record.runtime),
            provider_default=provider_default,
        )
        invoked = descriptor.model_selection in {
            ModelSelectionSupport.PER_TASK_REQUEST,
            ModelSelectionSupport.PROVIDER_CONFIG_DEFAULT,
        } and effective_model is not None
        evidence = model_selection_metadata(
            requested_model=record.model,
            effective_model=effective_model,
            source=source,
            invoked=invoked,
        )
        if record.effective_model != effective_model:
            record = self.store.set_effective_model(record.id, effective_model)
        return record, evidence

    def _validate_request(self, request: TaskRequest, *, policy: ProfilePolicy | None = None) -> ProfilePolicy:
        if not request.task.strip():
            raise ValueError("Task must not be empty")
        if not request.workspace.strip():
            raise ValueError("Workspace must not be empty")
        if request.timeout_seconds <= 0:
            raise ValueError("Timeout must be positive")
        runtime = effective_runtime(request)
        if runtime not in self.profiles:
            raise ValueError(f"Unknown runtime: {runtime}")
        resolved_policy = policy or self.profiles[runtime]
        if request.execution_mode not in resolved_policy.allowed_modes:
            raise ValueError(describe_unsupported_execution_mode(
                self.runtime_registry, runtime, request.execution_mode,
            ))
        if request.timeout_seconds > resolved_policy.max_timeout_seconds:
            raise ValueError(
                f"Profile {resolved_policy.name} maximum timeout is {resolved_policy.max_timeout_seconds} seconds"
            )
        if request.verification_policy not in VERIFICATION_POLICIES:
            raise ValueError(f"Unknown verification_policy: {request.verification_policy}")
        try:
            normalize_required_capabilities(list(request.required_capabilities) if request.required_capabilities else None)
        except RequiredCapabilityValidationError as error:
            raise ValueError(str(error)) from error
        try:
            normalize_tool_access_profile(
                request.tool_access_profile,
                registry=self.tool_access_profile_registry,
            )
        except ToolAccessProfileValidationError as error:
            raise ValueError(str(error)) from error
        try:
            normalize_model_profile(
                request.model_profile,
                registry=self.model_profile_registry,
            )
        except ModelProfileValidationError as error:
            raise ValueError(str(error)) from error
        try:
            normalize_cost_rework_policy(
                request.cost_rework_policy,
                registry=self.cost_rework_policy_registry,
            )
        except CostReworkPolicyValidationError as error:
            raise ValueError(str(error)) from error
        if request.cost_rework_policy is not None:
            try:
                normalize_rework_metadata(
                    prior_task_id=request.rework_prior_task_id,
                    scope=request.rework_scope,
                    escalation_reason=request.escalation_reason,
                )
            except CostReworkPolicyValidationError as error:
                raise ValueError(str(error)) from error
        if runtime == DIRECT_API_PROFILE:
            if self.direct_api_runtime is None:
                raise ValueError(
                    f"Profile {DIRECT_API_PROFILE!r} requires a provider configuration (--providers-config)"
                )
            if not request.provider:
                raise ValueError(f"Profile {DIRECT_API_PROFILE!r} requires a named provider (--provider)")
            self.direct_api_runtime.get_provider(request.provider)
        elif request.provider is not None:
            raise ValueError(f"provider is only valid with profile {DIRECT_API_PROFILE!r}")
        if self.store.active_count(runtime) >= resolved_policy.max_concurrency:
            raise ValueError(f"Profile {resolved_policy.name} concurrency limit reached")
        validate_requested_model(self.runtime_registry.get(runtime), request.model)
        return resolved_policy

    def _resolve_agent_profile_request(
        self, request: TaskRequest,
    ) -> tuple[TaskRequest, object | None]:
        if request.agent_profile is None:
            return request, None
        runtime = effective_runtime(request)
        policy = self.profiles[runtime]
        profile = get_agent_profile(request.agent_profile, self.agent_profiles)
        resolved = resolve_agent_profile(
            profile=profile,
            explicit_fields=request.explicit_fields,
            execution_mode=request.execution_mode,
            timeout_seconds=request.timeout_seconds,
            result_schema=request.result_schema,
            allowed_modes=policy.allowed_modes,
            max_timeout_seconds=policy.max_timeout_seconds,
            runtime=runtime,
            runtime_registry=self.runtime_registry,
        )
        effective = TaskRequest(
            request.task,
            request.workspace,
            resolved.execution_mode,
            request.profile,
            request.provider,
            resolved.timeout_seconds,
            request.verification_policy,
            runtime=request.runtime,
            model=request.model,
            agent_profile=request.agent_profile,
            result_schema=resolved.result_schema,
            task_category=request.task_category,
            claude_permission_mode=request.claude_permission_mode,
            compatibility=request.compatibility,
            explicit_fields=request.explicit_fields,
            parent_task_id=request.parent_task_id,
            external_root_id=request.external_root_id,
            relationship=request.relationship,
            origin_kind=request.origin_kind,
            origin_ref=request.origin_ref,
            required_capabilities=request.required_capabilities,
            tool_access_profile=request.tool_access_profile,
            model_profile=request.model_profile,
            cost_rework_policy=request.cost_rework_policy,
            rework_prior_task_id=request.rework_prior_task_id,
            rework_scope=request.rework_scope,
            escalation_reason=request.escalation_reason,
        )
        return effective, resolved

    def _request_payload_for_record(self, record: TaskRecord) -> dict[str, Any]:
        request_path = self.store.artifacts / record.id / "request.json"
        if not request_path.is_file():
            return {}
        try:
            return json.loads(request_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _capability_preflight_context(self, record: TaskRecord) -> CapabilityPreflightContext:
        payload = self._request_payload_for_record(record)
        task_category = payload.get("task_category")
        claude_permission_mode = payload.get("claude_permission_mode")
        tool_access_profile = payload.get("tool_access_profile")
        if task_category is not None and not isinstance(task_category, str):
            task_category = None
        if claude_permission_mode is not None and not isinstance(claude_permission_mode, str):
            claude_permission_mode = None
        if tool_access_profile is not None and not isinstance(tool_access_profile, str):
            tool_access_profile = None
        return CapabilityPreflightContext(
            runtime=record.runtime,
            execution_mode=record.execution_mode,
            result_schema=record.result_schema,
            agent_profile=record.agent_profile,
            task_category=task_category,
            claude_permission_mode=claude_permission_mode,
            tool_access_profile=tool_access_profile,
            tool_access_profile_registry=self.tool_access_profile_registry,
        )

    def _required_capabilities_for_record(self, record: TaskRecord) -> tuple[str, ...]:
        return normalize_required_capabilities(self._request_payload_for_record(record).get("required_capabilities"))

    def _tool_access_profile_for_record(self, record: TaskRecord) -> str | None:
        value = self._request_payload_for_record(record).get("tool_access_profile")
        return value if isinstance(value, str) else None

    def _model_profile_for_record(self, record: TaskRecord) -> str | None:
        value = self._request_payload_for_record(record).get("model_profile")
        return value if isinstance(value, str) else None

    def _cost_rework_policy_for_record(self, record: TaskRecord) -> str | None:
        value = self._request_payload_for_record(record).get("cost_rework_policy")
        return value if isinstance(value, str) else None

    def _rework_metadata_for_record(self, record: TaskRecord):
        payload = self._request_payload_for_record(record)
        if self._cost_rework_policy_for_record(record) is None:
            return None
        try:
            return normalize_rework_metadata(
                prior_task_id=payload.get("rework_prior_task_id"),
                scope=payload.get("rework_scope"),
                escalation_reason=payload.get("escalation_reason"),
            )
        except CostReworkPolicyValidationError:
            return None

    def _read_model_profile_snapshot(self, task_id: str) -> dict[str, Any]:
        path = self.store.artifacts / task_id / "model_profile_resolution.json"
        if path.is_file():
            try:
                snapshot = json.loads(path.read_text())
                if isinstance(snapshot, dict):
                    return snapshot
            except (OSError, json.JSONDecodeError):
                pass
        return unconfigured_model_profile_snapshot()

    def _read_cost_policy_projection(self, task_id: str) -> dict[str, Any] | None:
        path = self.store.artifacts / task_id / "cost_rework_policy_resolution.json"
        if path.is_file():
            try:
                snapshot = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                snapshot = None
            if isinstance(snapshot, dict):
                return cost_policy_public_projection(snapshot)
        requested = self._request_payload_for_record(self.store.get(task_id))
        if isinstance(requested.get("cost_rework_policy"), str) and requested["cost_rework_policy"]:
            return {
                "resolution": "pending",
                "policy_id": requested["cost_rework_policy"],
                "preflight_status": "pending",
            }
        return cost_policy_public_projection(unconfigured_cost_policy_snapshot())

    def _reject_cost_rework_policy(self, record: TaskRecord) -> TaskRecord | None:
        policy_id = self._cost_rework_policy_for_record(record)
        if policy_id is None:
            return None
        policy = self.cost_rework_policy_registry.policies.get(policy_id)
        if policy is None:
            return self.store.transition(
                record.id,
                TaskState.REJECTED,
                "Requested cost_rework_policy is not configured",
                {"reason": "unknown_cost_rework_policy", "cost_rework_policy": policy_id},
            )
        snapshot_path = self.store.artifacts / record.id / "model_profile_resolution.json"
        if snapshot_path.is_file():
            try:
                model_snapshot = json.loads(snapshot_path.read_text())
            except (OSError, json.JSONDecodeError):
                model_snapshot = unconfigured_model_profile_snapshot()
        else:
            requested = self._model_profile_for_record(record)
            try:
                model_snapshot = resolve_model_profile_snapshot(
                    runtime=record.runtime,
                    provider=record.provider,
                    effective_model=record.effective_model,
                    requested_profile=requested,
                    registry=self.model_profile_registry,
                )
            except ModelProfileValidationError:
                model_snapshot = unconfigured_model_profile_snapshot()
        rework = self._rework_metadata_for_record(record)
        rejection = evaluate_cost_rework_preflight(
            record=record,
            policy=policy,
            rework=rework,
            model_profile_snapshot=model_snapshot,
            get_task=self.store.get,
            list_tree_tasks=lambda root, limit: self.store.list_tree_tasks(root, limit=limit),
            artifacts_dir=self.store.artifacts,
            tree_limit=MAX_TREE_NODES,
        )
        if rejection is None:
            root_task_id = record.root_task_id or record.id
            usage = compute_workflow_usage(
                root_task_id=root_task_id,
                tasks=self.store.list_tree_tasks(root_task_id, limit=MAX_TREE_NODES),
                artifacts_dir=self.store.artifacts,
                exclude_task_id=record.id,
            )
            cost_class = model_snapshot.get("cost_class", "unknown")
            escalation_decision = "not_applicable"
            if rework is not None and rework.scope == "full":
                escalation_decision = "allowed"
            snapshot = build_cost_policy_snapshot(
                policy=policy,
                rework=rework,
                model_profile_snapshot=model_snapshot,
                usage=usage,
                cost_class=str(cost_class),
                escalation_decision=escalation_decision,
            )
            self.store.write_artifact(
                record.id,
                "cost_rework_policy_resolution.json",
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            )
            return None
        return self.store.transition(
            record.id,
            TaskState.REJECTED,
            "Cost/rework policy preflight rejected this launch",
            rejection,
        )

    def _reject_invalid_model_profile(self, record: TaskRecord) -> TaskRecord | None:
        requested = self._model_profile_for_record(record)
        rejection = evaluate_model_profile_preflight(
            runtime=record.runtime,
            provider=record.provider,
            effective_model=record.effective_model,
            requested_profile=requested,
            registry=self.model_profile_registry,
        )
        if rejection is None:
            return None
        return self.store.transition(
            record.id,
            TaskState.REJECTED,
            "Requested model_profile is not compatible with this task runtime/model binding",
            rejection,
        )

    def _record_model_profile_resolution(self, record: TaskRecord) -> dict[str, Any] | None:
        requested = self._model_profile_for_record(record)
        try:
            snapshot = resolve_model_profile_snapshot(
                runtime=record.runtime,
                provider=record.provider,
                effective_model=record.effective_model,
                requested_profile=requested,
                registry=self.model_profile_registry,
            )
        except ModelProfileValidationError:
            return None
        public = model_profile_public_projection(snapshot)
        if public is None:
            return None
        self.store.write_artifact(
            record.id,
            "model_profile_resolution.json",
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        )
        return public

    def _read_model_profile_projection(self, task_id: str) -> dict[str, Any] | None:
        path = self.store.artifacts / task_id / "model_profile_resolution.json"
        if not path.is_file():
            requested_path = self.store.artifacts / task_id / "request.json"
            if requested_path.is_file():
                try:
                    payload = json.loads(requested_path.read_text())
                except (OSError, json.JSONDecodeError):
                    payload = {}
                requested = payload.get("model_profile")
                if isinstance(requested, str) and requested:
                    return {"resolution": "pending", "model_profile": requested, "cost_class": "unknown"}
            return model_profile_public_projection(unconfigured_model_profile_snapshot())
        try:
            snapshot = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return model_profile_public_projection(snapshot) if isinstance(snapshot, dict) else None

    def _record_tool_access_profile_resolution(self, record: TaskRecord) -> dict[str, Any] | None:
        requested = self._tool_access_profile_for_record(record)
        try:
            profile = resolve_tool_access_profile(
                runtime=record.runtime,
                execution_mode=record.execution_mode,
                requested_profile=requested,
                registry=self.tool_access_profile_registry,
            )
        except ToolAccessProfileValidationError:
            return None
        audit = tool_access_profile_audit_payload(profile)
        if audit is None:
            return None
        self.store.write_artifact(
            record.id,
            "tool_access_profile_resolution.json",
            json.dumps(audit, indent=2, sort_keys=True) + "\n",
        )
        return audit

    def _reject_invalid_tool_access_profile(self, record: TaskRecord) -> TaskRecord | None:
        requested = self._tool_access_profile_for_record(record)
        rejection = evaluate_tool_access_profile_preflight(
            runtime=record.runtime,
            execution_mode=record.execution_mode,
            requested_profile=requested,
            registry=self.tool_access_profile_registry,
        )
        if rejection is None:
            return None
        return self.store.transition(
            record.id,
            TaskState.REJECTED,
            "Requested tool_access_profile is not available for this runtime/execution_mode",
            rejection,
        )

    def _reject_unsatisfied_capabilities(self, record: TaskRecord) -> TaskRecord | None:
        required = self._required_capabilities_for_record(record)
        rejection = evaluate_capability_preflight(required, self._capability_preflight_context(record))
        if rejection is None:
            return None
        return self.store.transition(
            record.id,
            TaskState.REJECTED,
            "Required capabilities are not satisfied by the selected runtime policy",
            rejection,
        )

    def _reject_unsupported_result_schema(self, record: TaskRecord) -> TaskRecord | None:
        rejection = evaluate_result_schema_preflight(
            runtime=record.runtime,
            result_schema=effective_result_schema(record),
            capabilities=self.runtime_registry.get(record.runtime).adapter_capabilities,
        )
        if rejection is None:
            return None
        return self.store.transition(
            record.id,
            TaskState.REJECTED,
            "Requested result schema is not supported by the selected runtime policy",
            rejection,
        )


    def _composed_launch_prompt(self, task_id: str, record: TaskRecord) -> tuple[str, dict[str, object] | None]:
        schema = effective_result_schema(record)
        notice = materialization_prompt_notice(self.runtime_registry.get(record.runtime).capability_contract)
        resolution_path = self.store.artifacts / task_id / "agent_profile_resolution.json"
        if resolution_path.is_file():
            resolution = json.loads(resolution_path.read_text())
            composed, contract = compose_launch_prompt(
                prompt_prefix=resolution["prompt_prefix"],
                task_text=record.task,
                result_schema=schema,
                materialization_notice=notice,
            )
            evidence: dict[str, object] = {
                "profile_name": resolution["name"],
                "profile_content_hash": resolution["content_hash"],
                "task_text": record.task,
                "prompt_prefix": resolution["prompt_prefix"],
                "result_schema": schema,
                "result_schema_source": resolution.get("sources", {}).get("result_schema", "runtime_default"),
                "result_schema_prompt_version": RESULT_SCHEMA_PROMPT_VERSION,
                "composed_prompt": composed,
            }
            if contract is not None:
                evidence["result_schema_contract"] = contract
            if notice is not None:
                evidence["materialization_notice"] = notice
            return composed, evidence

        composed, contract = compose_launch_prompt(
            prompt_prefix=None,
            task_text=record.task,
            result_schema=schema,
            materialization_notice=notice,
        )
        if contract is None and notice is None:
            return record.task, None
        source = "task_request" if record.result_schema is not None else "runtime_default"
        evidence = {
            "task_text": record.task,
            "result_schema": schema,
            "result_schema_source": source,
            "result_schema_prompt_version": RESULT_SCHEMA_PROMPT_VERSION,
            "composed_prompt": composed,
        }
        if contract is not None:
            evidence["result_schema_contract"] = contract
        if notice is not None:
            evidence["materialization_notice"] = notice
        return composed, evidence

    def _apply_resolved_lineage(self, record: TaskRecord, resolved) -> TaskRecord:
        return dataclasses.replace(
            record,
            parent_task_id=resolved.parent_task_id,
            root_task_id=resolved.root_task_id,
            external_root_id=resolved.external_root_id,
            delegation_depth=resolved.delegation_depth,
            relationship=resolved.relationship,
            origin_kind=resolved.origin_kind,
            origin_ref=resolved.origin_ref,
        )

    def _resolve_record_lineage(self, record: TaskRecord, request: TaskRequest) -> TaskRecord:
        resolved = resolve_lineage(
            task_id=record.id,
            parent_task_id=request.parent_task_id,
            external_root_id=request.external_root_id,
            relationship=request.relationship,
            origin_kind=request.origin_kind,
            origin_ref=request.origin_ref,
            get_parent=self.store.get,
            child_count=self.store.child_count,
            active_agent_count=self.store.total_active_count,
            policy=self.lineage_policy,
        )
        return self._apply_resolved_lineage(record, resolved)

    def children(self, task_id: str) -> list[dict]:
        self.store.get(task_id)
        return [
            concise_task_summary(
                child,
                model_profile_resource=self._read_model_profile_projection(child.id),
                cost_policy_status=self._read_cost_policy_projection(child.id),
            )
            for child in self.store.list_children(task_id)
        ]

    def task_tree(self, root_task_id: str) -> dict:
        root = self.store.get(root_task_id)
        if root.root_task_id != root_task_id:
            raise ValueError(f"Task {root_task_id!r} is not a tree root (root_task_id={root.root_task_id!r})")
        tasks = self.store.list_tree_tasks(root_task_id, limit=MAX_TREE_NODES)
        truncated = len(tasks) >= MAX_TREE_NODES
        return {
            "root_task_id": root_task_id,
            "truncated": truncated,
            "tasks": [
                concise_task_summary(
                    task,
                    model_profile_resource=self._read_model_profile_projection(task.id),
                    cost_policy_status=self._read_cost_policy_projection(task.id),
                )
                for task in tasks
            ],
        }

    def _pump_finished_handles(self) -> None:
        """Opportunistically finalize any locally-held runtime handle whose
        process/request has already finished, without ever blocking on one
        that is still in flight.

        `collect()` is the only thing that ever records a terminal completion
        event, and `collect()` blocks until the runtime actually finishes --
        fine for a caller that wants exactly one task's result, useless for a
        parent that wants to observe *which* of several in-flight tasks just
        finished without stalling behind whichever one is slowest. This is
        the one place that gap is closed: a cheap, non-blocking liveness
        check (`Popen.poll()` / `Thread.is_alive()`, never `.wait()`/`.join()`
        with no timeout) on every handle this exact broker instance still
        holds, and `collect()` only for the ones that already finished --
        `collect()` on an already-exited process/thread returns immediately,
        it does not introduce a second wait.

        This only ever sees handles this same live broker process launched
        and still holds in memory. It never reaches into another process's
        or another broker instance's launches (that is reconcile()'s job,
        after a restart) and it never runs on its own -- it only executes as
        a side effect of a caller explicitly polling (completion_events_since()),
        matching the documented polling-only, no-push-notification contract.
        """
        for task_id, handle in list(self._process_handles.items()):
            popen = getattr(handle, "popen", None)
            if popen is not None:
                if popen.poll() is not None:
                    self._pump_collect(task_id)
                continue
            if hasattr(handle, "durable") and is_durable_launch_terminal(handle):
                self._pump_collect(task_id)
        for task_id, handle in list(self._direct_api_handles.items()):
            thread = getattr(handle, "thread", None)
            if thread is None or not thread.is_alive():
                self._pump_collect(task_id)

    def _pump_collect(self, task_id: str) -> None:
        try:
            self.collect(task_id)
        except Exception as error:
            print(f"recollect_lines.service: pump collect failed for {task_id}: {error!r}", file=sys.stderr)

    def task_tree_by_external_root(self, external_root_id: str) -> dict:
        # Unlike task_tree, this key need not belong to any task; no match is an empty result, not an error.
        if not isinstance(external_root_id, str) or not external_root_id.strip():
            raise ValueError("'external_root_id' must be a non-empty string")
        external_root_id = external_root_id.strip()
        tasks = self.store.list_tasks_by_external_root(external_root_id, limit=MAX_TREE_NODES)
        truncated = len(tasks) >= MAX_TREE_NODES
        return {
            "external_root_id": external_root_id,
            "truncated": truncated,
            "tasks": [
                concise_task_summary(
                    task,
                    model_profile_resource=self._read_model_profile_projection(task.id),
                    cost_policy_status=self._read_cost_policy_projection(task.id),
                )
                for task in tasks
            ],
        }

    def completion_events_since(
        self,
        after_event_id: int = 0,
        *,
        limit: int = 64,
        task_id: str | None = None,
        root_task_id: str | None = None,
        completion_only: bool = True,
        states: frozenset[str] | None = None,
    ) -> dict:
        """Poll durable completion signals in global event-id order.

        Before reading, this opportunistically finalizes (non-blocking) any
        runtime this broker instance itself launched and still holds a
        handle for that has already finished -- see _pump_finished_handles().
        A task still genuinely running is left alone; this call never blocks
        waiting for one to finish and never observes a task launched by a
        different broker instance/process.
        """
        self._pump_finished_handles()
        return completion_events_page(
            self.store,
            after_event_id=after_event_id,
            limit=limit,
            task_id=task_id,
            root_task_id=root_task_id,
            completion_only=completion_only,
            states=states,
        )

    def create(self, request: TaskRequest, verify_commands: list[list[str]] | None = None) -> TaskRecord:
        """Create a task, optionally declaring the broker-verified commands its
        verification_policy will gate on. Shared by the CLI and MCP surfaces so
        neither duplicates this policy check (PRD §6).
        """
        effective_request, resolved_profile = self._resolve_agent_profile_request(request)
        validate_result_schema(effective_request.result_schema)
        self._validate_request(effective_request)
        if verify_commands is not None:
            validate_verify_commands(verify_commands)
        record = TaskRecord.new(effective_request)
        record = self._resolve_record_lineage(record, effective_request)
        self.store.create(record)
        request_payload = request_artifact_payload(effective_request)
        request_payload["root_task_id"] = record.root_task_id
        request_payload["delegation_depth"] = record.delegation_depth
        if record.origin_kind is not None:
            request_payload["origin_kind"] = record.origin_kind
        self.store.write_artifact(record.id, "request.json", json.dumps(request_payload, indent=2) + "\n")
        if resolved_profile is not None:
            self.store.write_artifact(
                record.id,
                "agent_profile_resolution.json",
                json.dumps(resolution_artifact_payload(resolved_profile), indent=2, sort_keys=True) + "\n",
            )
        if verify_commands is not None:
            self.store.write_artifact(record.id, "verify_commands.json", json.dumps(verify_commands, indent=2) + "\n")
        conflict = detect_schema_prose_conflict(record.task, effective_result_schema(record))
        if conflict is not None:
            self.store.write_artifact(
                record.id, "schema_conflict_warning.json", json.dumps(conflict, indent=2, sort_keys=True) + "\n",
            )
            self.store.event(
                record.id, "task.schema_conflict_warning", record.state, record.state,
                "Task prose may not satisfy the requested result_schema contract", conflict,
            )
        return self.store.transition(record.id, TaskState.QUEUED, "Task queued", {})

    def schema_conflict_warning(self, task_id: str) -> dict[str, str] | None:
        """Read back the advisory, deterministic pre-delegate signal `create()` may
        have recorded (see contract_conflict.py): never blocks task creation,
        so this is purely informational for a caller deciding whether to
        retry with a different result_schema.
        """
        path = self.store.artifacts / task_id / "schema_conflict_warning.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def start(self, task_id: str) -> TaskRecord:
        record = self.store.get(task_id)
        rejected = self._reject_unsupported_result_schema(record)
        if rejected is not None:
            return rejected
        rejected = self._reject_invalid_tool_access_profile(record)
        if rejected is not None:
            return rejected
        rejected = self._reject_unsatisfied_capabilities(record)
        if rejected is not None:
            return rejected
        record, model_evidence = self._resolve_launch_model(record)
        rejected = self._reject_invalid_model_profile(record)
        if rejected is not None:
            return rejected
        self._record_model_profile_resolution(record)
        rejected = self._reject_cost_rework_policy(record)
        if rejected is not None:
            return rejected
        profile_audit = self._record_tool_access_profile_resolution(record)
        record = self.store.transition(task_id, TaskState.PREPARING, "Preparing execution", {})
        launch_prompt, prompt_evidence = self._composed_launch_prompt(task_id, record)
        if prompt_evidence is not None:
            self.store.write_artifact(
                task_id,
                "composed_prompt.json",
                json.dumps(prompt_evidence, indent=2, sort_keys=True) + "\n",
            )
        effective_workspace = record.workspace
        if record.execution_mode == ISOLATED_WORKTREE:
            try:
                source = canonical_source(record.workspace)
            except WorkspaceError as error:
                return self.store.transition(
                    record.id, TaskState.FAILED, f"Workspace validation failed: {error}",
                    {"reason": "workspace_invalid", "error": str(error)},
                )
            branch = self.workspaces.branch_name(record.id)
            worktree_path = str(self.workspaces.worktree_path(record.id))
            base_sha = capture_head(source)
            try:
                # Lease acquisition (durable, atomic via a partial unique index)
                # gates worktree creation: a losing writer never touches the
                # filesystem at all.
                self.store.acquire_lease(record.id, source, worktree_path, branch, base_sha)
            except WorkspaceLeaseConflict as error:
                return self.store.transition(
                    record.id, TaskState.FAILED, str(error),
                    {"reason": "workspace_lease_conflict", "canonical_source": source},
                )
            try:
                self.workspaces.create_worktree(source, record.id, base_sha)
            except Exception as error:
                self.store.release_lease(record.id)
                return self.store.transition(
                    record.id, TaskState.FAILED, f"Workspace allocation failed: {error}",
                    {"reason": "workspace_allocation_failed", "error": str(error)},
                )
            effective_workspace = worktree_path
        if record.runtime == DIRECT_API_PROFILE:
            runtime = self.direct_api_runtime
            assert runtime is not None
            try:
                metadata, handle = runtime.start(record, self.store.artifacts / record.id, prompt=launch_prompt)
            except Exception as error:
                if record.execution_mode == ISOLATED_WORKTREE:
                    lease = self.store.get_lease(record.id)
                    if lease is not None and lease["status"] == "active":
                        self.workspaces.release(lease["canonical_source"], lease["worktree_path"])
                        self.store.release_lease(record.id)
                return self.store.transition(
                    record.id, TaskState.FAILED, f"Direct API runtime failed to start: {error}",
                    {"reason": "direct_api_start_failed", "error": str(error)},
                )
            self._direct_api_handles[record.id] = handle
            self.store.record_launch(
                record.id,
                adapter=runtime.name,
                adapter_label=runtime.runtime_label,
                pid=None,
                pgid=None,
                command=[f"provider={metadata['provider']}", f"model={metadata['model']}", f"base_url={metadata['base_url']}"],
                workspace=metadata["workspace"],
                events_artifact=metadata["events_artifact"],
                stderr_artifact=metadata["stderr_artifact"],
            )
            self.store.refresh_manifest(record.id)
            metadata = {**metadata, "model_selection": model_evidence}
            if prompt_evidence is not None:
                metadata = {**metadata, "agent_profile_prompt": prompt_evidence}
            return self.store.transition(record.id, TaskState.RUNNING, f"{runtime.name} direct API request started", metadata)
        adapter = self.subprocess_adapters.get(record.runtime)
        if adapter is not None:
            try:
                metadata, handle = adapter.start(
                    record, self.store.artifacts / record.id, workspace=effective_workspace, prompt=launch_prompt,
                )
            except Exception:
                # A losing writer never allocates, but a *successful* allocation
                # whose adapter then fails to launch must still give up its lease —
                # otherwise this source stays blocked for every future writer.
                if record.execution_mode == ISOLATED_WORKTREE:
                    self.workspaces.release(source, worktree_path)
                    self.store.release_lease(record.id)
                raise
            self._process_handles[record.id] = handle
            # Durable launch identity is recorded as soon as the process actually
            # exists, before the task even reaches RUNNING — a fresh Broker must
            # be able to reconcile against this row even if this process crashes
            # on the very next line.
            stored_command = (
                ["<durable-subprocess>", adapter.name]
                if metadata.get("launch_kind") == LAUNCH_KIND_DURABLE
                else redact_command(metadata["command"])
            )
            self.store.record_launch(
                record.id,
                adapter=adapter.name,
                adapter_label=adapter.runtime_label,
                pid=handle.pid,
                pgid=handle.pgid,
                command=stored_command,
                workspace=metadata["workspace"],
                events_artifact=metadata["events_artifact"],
                stderr_artifact=metadata["stderr_artifact"],
                durable_launch_id=metadata.get("durable_launch_id"),
                launch_kind=metadata.get("launch_kind", "legacy_subprocess"),
                leader_start_identity=metadata.get("leader_start_identity"),
            )
            self.store.refresh_manifest(record.id)
            event_metadata = metadata
            if metadata.get("launch_kind") == LAUNCH_KIND_DURABLE:
                event_metadata = {**metadata, "command": stored_command}
            event_metadata = {**event_metadata, "model_selection": model_evidence}
            if prompt_evidence is not None:
                event_metadata = {**event_metadata, "agent_profile_prompt": prompt_evidence}
            if profile_audit is not None:
                event_metadata = {**event_metadata, "tool_access_profile_audit": profile_audit}
            return self.store.transition(record.id, TaskState.RUNNING, f"{adapter.name} adapter started", event_metadata)
        mock_metadata = {**self.adapter.start_metadata(record, effective_workspace), "model_selection": model_evidence}
        if prompt_evidence is not None:
            mock_metadata = {**mock_metadata, "agent_profile_prompt": prompt_evidence}
        return self.store.transition(
            record.id, TaskState.RUNNING, "Mock adapter started",
            mock_metadata,
        )

    def _read_verification_artifact(self, task_id: str) -> dict | None:
        path = self.store.artifacts / task_id / "verification.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def _read_tool_access_profile_audit(self, task_id: str) -> dict[str, Any] | None:
        path = self.store.artifacts / task_id / "tool_access_profile_resolution.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _capability_contract_for_collection(
        self, record: TaskRecord, collected: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Post-run capability-contract verdict, or None when nothing was declared.

        Reuses `normalize_permission_denials` so this never reasons
        about raw `permission_denials`/`tool_input` directly -- see
        `capability_contract_result.evaluate_capability_contract`.
        """
        required = self._required_capabilities_for_record(record)
        if not required:
            return None
        adapter = collected.get("adapter")
        observations, _warning, denial_warnings = normalize_permission_denials(
            collected.get("permission_denials"),
            adapter=adapter if isinstance(adapter, str) else "",
        )
        return evaluate_capability_contract(
            required,
            adapter=adapter if isinstance(adapter, str) else None,
            capability_observations=observations,
            denial_metadata_malformed=bool(denial_warnings),
        )

    def _apply_capability_contract_gate(
        self,
        record: TaskRecord,
        collected: dict[str, Any],
        final_state: TaskState,
        gate: dict[str, Any],
    ) -> tuple[TaskState, dict[str, Any]]:
        """Fold an unsatisfied declared capability contract into `final_state`,
        but only when the task explicitly opted into `verification_policy="required"`.
        Default/advisory policy leaves state untouched -- the contract stays
        visible (via the normalized envelope/concise view) rather than
        silently failing the task. Never introduces a new lifecycle state:
        an unsatisfied contract under "required" policy resolves to the
        existing `TaskState.FAILED`, exactly like a failed/missing
        verify_commands gate under the same policy.
        """
        if record.verification_policy != "required":
            return final_state, gate
        if final_state not in (TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS):
            return final_state, gate
        contract = self._capability_contract_for_collection(record, collected)
        if contract is None or contract["status"] != STATUS_UNSATISFIED:
            return final_state, gate
        gate = {
            **gate,
            "outcome": "blocked_unsatisfied_capability",
            "unsatisfied_capabilities": contract["unsatisfied_capabilities"],
        }
        return TaskState.FAILED, gate

    def _persist_normalized_result(
        self,
        record: TaskRecord,
        result: dict[str, Any],
        collected: dict[str, Any],
        gate: dict[str, Any],
        final_state: TaskState,
    ) -> str | None:
        launch = self.store.get_launch(record.id)
        raw_ref = persist_raw_runtime_output_if_needed(
            self.store, record.id, launch=launch, collected=collected,
        )
        self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
        manifest = self.store.artifact_manifest(record.id)
        normalized = build_normalized_envelope(
            record=record,
            result=result,
            collected=collected,
            gate=gate,
            verification=self._read_verification_artifact(record.id),
            manifest=manifest,
            launch=launch,
            raw_output_artifact=raw_ref,
            final_state=final_state,
            required_capabilities=self._required_capabilities_for_record(record),
            tool_access_profile_audit=self._read_tool_access_profile_audit(record.id),
            model_profile_resource=self._read_model_profile_projection(record.id),
            cost_policy_status=self._read_cost_policy_projection(record.id),
        )
        self.store.write_artifact(
            record.id,
            NORMALIZED_RESULT_ARTIFACT,
            json.dumps(normalized, indent=2, sort_keys=True) + "\n",
        )
        return raw_ref

    def _finalize_runtime_collection(
        self,
        record: TaskRecord,
        result: dict[str, Any],
        collected: dict[str, Any],
        candidate_state: TaskState,
        *,
        success_message: str,
        blocked_message: str,
        transition_metadata: dict,
    ) -> TaskRecord:
        if candidate_state in (TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS):
            validate_result(result, record.id)
        final_state, gate = self._apply_verification_gate(record.id, record, candidate_state)
        final_state, gate = self._apply_capability_contract_gate(record, collected, final_state, gate)
        self._write_gate_artifact(record.id, gate)
        self._persist_normalized_result(record, result, collected, gate, final_state)
        self._finalize_workspace(record.id)
        message = success_message if final_state is candidate_state else blocked_message.format(outcome=gate["outcome"])
        return self.store.transition(
            record.id,
            final_state,
            message,
            {
                **transition_metadata,
                "result_artifact": "result.json",
                "normalized_result_artifact": NORMALIZED_RESULT_ARTIFACT,
                "verification_gate": gate,
            },
        )

    def complete(self, task_id: str, summary: str) -> TaskRecord:
        record = self.store.transition(task_id, TaskState.COLLECTING, "Collecting mock result", {})
        result = {"task_id": record.id, "state": "succeeded", "summary": summary, "runtime": {"adapter": "mock"}}
        collected = {"summary": summary, "exit_code": 0, "adapter": "mock"}
        return self._finalize_runtime_collection(
            record,
            result,
            collected,
            TaskState.SUCCEEDED,
            success_message="Mock task completed",
            blocked_message="Mock task blocked by verification gate ({outcome})",
            transition_metadata={},
        )

    def collect(self, task_id: str) -> TaskRecord:
        """Collect a task's runtime-reported result.

        Idempotent: calling this again on an already-terminal task returns the
        same durable record without re-running verification, re-finalizing the
        workspace, or emitting a duplicate terminal event. A task that requires
        reconciliation (state recovery_required, or a fresh restart discovering
        a still-alive process group) raises RecoveryRequired rather than
        fabricating a result — see reconcile().

        A legacy subprocess-backed task whose in-memory handle is still
        genuinely running gets one bounded grace wait
        (`LEGACY_PROCESS_COLLECT_GRACE_SECONDS`), then an explicit nonterminal
        result: the unchanged, still-`running` record, handle retained for a
        later collect() call. This never calls an unbounded `Popen.wait()` —
        collect() is the single synchronous call site behind the CLI/MCP
        `collect` command, and a long-lived real CLI run must not be able to
        block it (or the rest of that MCP process) indefinitely.
        """
        record = self.store.get(task_id)
        if record.state in TERMINAL_STATES:
            return record
        if task_id in self._direct_api_handles:
            handle = self._direct_api_handles.pop(task_id)
            runtime = self.direct_api_runtime
            assert runtime is not None
            record = self.store.transition(task_id, TaskState.COLLECTING, f"Collecting {runtime.name} result", {})
            collected = runtime.collect(handle)
            self.store.refresh_manifest(record.id)
            runtime_payload = {"adapter": runtime.name, **collected}
            if collected.get("exit_code") != 0:
                result = {
                    "task_id": record.id,
                    "state": TaskState.FAILED.value,
                    "summary": collected.get("summary") or collected.get("error_message") or f"{runtime.name} request failed",
                    "runtime": runtime_payload,
                }
                return self._finalize_runtime_collection(
                    record, result, runtime_payload, TaskState.FAILED,
                    success_message=f"{runtime.name} task failed",
                    blocked_message=f"{runtime.name} task blocked by verification gate ({{outcome}})",
                    transition_metadata={"exit_code": collected.get("exit_code")},
                )
            state = TaskState.SUCCEEDED if collected.get("summary") else TaskState.SUCCEEDED_WITH_WARNINGS
            result = {
                "task_id": record.id,
                "state": state.value,
                "summary": collected.get("summary") or f"{runtime.name} run produced no text result",
                "runtime": runtime_payload,
            }
            return self._finalize_runtime_collection(
                record, result, runtime_payload, state,
                success_message=f"{runtime.name} task completed",
                blocked_message=f"{runtime.name} task blocked by verification gate ({{outcome}})",
                transition_metadata={"exit_code": collected.get("exit_code")},
            )
        if task_id in self._process_handles:
            handle = self._process_handles[task_id]
            if hasattr(handle, "durable"):
                # Durable-subprocess handle (RFC-004): the manifest lifecycle
                # state is a nonblocking, read-only liveness check, so this
                # bounded grace wait never touches DurableSubprocessRunner.wait()
                # (which cancels a still-live payload on timeout -- exactly
                # what a collect() grace wait must never do to a live task).
                if not wait_for_durable_launch_terminal(handle, timeout=LEGACY_PROCESS_COLLECT_GRACE_SECONDS):
                    return record
            elif handle.popen.poll() is None:
                # Still genuinely running: never let collect() (and the single
                # synchronous MCP/CLI call site behind it) block indefinitely on
                # a long-lived CLI run. A short bounded grace window absorbs the
                # ordinary start()->collect() race (fixtures/fast runtimes that
                # are already exiting); anything still alive after it gets an
                # explicit nonterminal result (the record, unchanged, still
                # `running`) instead of a hang. The handle stays in place so a
                # later collect() call can retry.
                try:
                    handle.popen.wait(timeout=LEGACY_PROCESS_COLLECT_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    return record
            del self._process_handles[task_id]
            adapter = self.subprocess_adapters[record.runtime]
            record = self.store.transition(task_id, TaskState.COLLECTING, f"Collecting {adapter.name} result", {})
            collected = adapter.collect(handle)
            self.store.refresh_manifest(record.id)
            runtime = {"adapter": adapter.name, **collected}
            if collected["exit_code"] != 0:
                result = {"task_id": record.id, "state": TaskState.FAILED.value, "summary": collected["summary"] or f"{adapter.name} exited with a non-zero status", "runtime": runtime}
                return self._finalize_runtime_collection(
                    record, result, runtime, TaskState.FAILED,
                    success_message=f"{adapter.name} task failed",
                    blocked_message=f"{adapter.name} task blocked by verification gate ({{outcome}})",
                    transition_metadata={"exit_code": collected["exit_code"]},
                )
            state = TaskState.SUCCEEDED if collected["summary"] else TaskState.SUCCEEDED_WITH_WARNINGS
            result = {"task_id": record.id, "state": state.value, "summary": collected["summary"] or f"{adapter.name} run produced no text result", "runtime": runtime}
            return self._finalize_runtime_collection(
                record, result, runtime, state,
                success_message=f"{adapter.name} task completed",
                blocked_message=f"{adapter.name} task blocked by verification gate ({{outcome}})",
                transition_metadata={"exit_code": collected["exit_code"]},
            )
        if task_id in self._adopted_durable_handles:
            return self._collect_adopted_durable(task_id, record)
        if record.runtime == DIRECT_API_PROFILE:
            reconciled = self.reconcile(task_id)
            if reconciled.state is not TaskState.FAILED:
                raise RecoveryRequired(task_id, reconciled.state)
            return reconciled
        if record.runtime not in self.subprocess_adapters:
            # No subprocess was ever involved for this profile (mock tasks are
            # collected via complete(), not collect()); this is a caller/protocol
            # error, not a restart, and there is nothing to reconcile. Any
            # declared verify_commands still run here as evidence (matching
            # Phase 3/5B's MCP-level behavior) — they just can never rescue this
            # protocol error into a success, whatever the policy.
            _, gate = self._apply_verification_gate(task_id, record, TaskState.FAILED)
            self._write_gate_artifact(task_id, gate)
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id,
                TaskState.FAILED,
                "No running process handle for task (broker restart or already collected)",
                {"reason": "missing_process_handle", "verification_gate": gate},
            )
        reconciled = self.reconcile(task_id)
        # Not just `is not TaskState.FAILED`: reconcile() can also resolve a
        # legacy launch to another terminal state (e.g. Cursor's `uncollected`,
        # or `cancelled` for a crash mid-cancellation) — any of those is a real
        # terminal outcome collect() should just hand back, not refuse.
        if reconciled.state not in TERMINAL_STATES:
            raise RecoveryRequired(task_id, reconciled.state)
        return reconciled

    def _collect_adopted_durable(self, task_id: str, record: TaskRecord) -> TaskRecord:
        adopted = self._adopted_durable_handles.pop(task_id)
        adapter_name = adopted.adapter_id
        record = self.store.transition(task_id, TaskState.COLLECTING, f"Collecting adopted {adapter_name} durable result", {})
        collected = adopted_collect(adopted)
        self.store.release_recovery_lease(task_id)
        self.store.refresh_manifest(record.id)
        runtime = {"adapter": adapter_name, **collected}
        if collected["exit_code"] != 0:
            result = {
                "task_id": record.id,
                "state": TaskState.FAILED.value,
                "summary": collected.get("summary") or f"{adapter_name} durable payload exited with a non-zero status",
                "runtime": runtime,
            }
            return self._finalize_runtime_collection(
                record, result, runtime, TaskState.FAILED,
                success_message=f"{adapter_name} task failed",
                blocked_message=f"{adapter_name} task blocked by verification gate ({{outcome}})",
                transition_metadata={"exit_code": collected["exit_code"], "adopted_durable": True},
            )
        state = TaskState.SUCCEEDED if collected.get("summary") else TaskState.SUCCEEDED_WITH_WARNINGS
        result = {
            "task_id": record.id,
            "state": state.value,
            "summary": collected.get("summary") or f"{adapter_name} durable run produced no text result",
            "runtime": runtime,
        }
        return self._finalize_runtime_collection(
            record, result, runtime, state,
            success_message=f"{adapter_name} task completed from adopted durable artifacts",
            blocked_message=f"{adapter_name} task blocked by verification gate ({{outcome}})",
            transition_metadata={"exit_code": collected["exit_code"], "adopted_durable": True},
        )

    def _process_group_status(self, task_id: str) -> str:
        """Classify the durably-persisted process group for `task_id`.

        Returns "no_launch" (no durable runtime_launches row at all),
        "unknown" (a row exists but pid/pgid metadata is missing/invalid —
        handled conservatively, i.e. never treated as dead), "dead", or
        "alive". "alive" also covers PermissionError from killpg (a process
        group that exists but this broker doesn't own — still alive from our
        point of view).
        """
        launch = self.store.get_launch(task_id)
        if launch is None:
            return "no_launch"
        pgid = launch["pgid"]
        if not isinstance(pgid, int) or pgid <= 0:
            return "unknown"
        return "alive" if group_alive(pgid) else "dead"

    # States a durable launch record might be sitting under when no in-memory
    # handle exists for it: RUNNING/PREPARING from an ordinary restart (the
    # crash can land either just before or just after the RUNNING transition
    # — record_launch() happens first either way), CANCELLING from a crash
    # mid-signal, COLLECTING from a crash after the in-memory handle was
    # popped (runtime already reaped, or a verification gate already in
    # flight) but before the terminal transition (Phase 5C — see
    # docs/history/phases/phase-5c.md), and RECOVERY_REQUIRED from a previous reconciliation
    # pass.
    _RECONCILABLE_STATES = (
        TaskState.PREPARING, TaskState.RUNNING, TaskState.COLLECTING, TaskState.CANCELLING, TaskState.RECOVERY_REQUIRED,
    )

    def _reap_exited_process_handle(self, task_id: str) -> TaskRecord | None:
        """Nonblocking check for an in-memory legacy subprocess handle that has
        already exited without ever being collected.

        Holding a `ProcessHandle` in `self._process_handles` is not proof a
        task is still running -- only `Popen.poll()` (nonblocking) is. Without
        this, `status()`/`reconcile()` could report `running` forever for a
        task whose process exited seconds or minutes ago, simply because
        nothing had called `collect()` yet. Returns the freshly collected
        record if a handle existed and had already exited, or None if there is
        no in-memory handle for this task or it is still genuinely running --
        callers should keep using their own already-loaded record in that
        case, exactly as if this check hadn't run.
        """
        handle = self._process_handles.get(task_id)
        if handle is None:
            return None
        if hasattr(handle, "durable"):
            if not is_durable_launch_terminal(handle):
                return None
        elif handle.popen.poll() is None:
            return None
        return self.collect(task_id)

    def reconcile(self, task_id: str) -> TaskRecord:
        """Reconcile a task's durable runtime-launch record against reality.

        The one operation a freshly constructed Broker (no in-memory
        ProcessHandle) can use to inspect and act on a task whose last known
        state predates a previous broker process disappearing. Idempotent:
        re-running it while the process group is still alive just logs an
        audit event and makes no state change; it never asserts success and
        never touches a workspace/lease it cannot prove is safe to release.

        Durable subprocess launches (Phase 7C.3) may be adopted after proof
        and recovery-lease acquisition; legacy subprocess/direct paths remain
        fail-closed as before.

        An in-memory legacy subprocess handle that has already exited (see
        `_reap_exited_process_handle`) is collected here rather than left
        reporting `running` forever -- this is not a restart scenario, so no
        liveness inference is involved: `Popen.poll()` returning non-None is
        direct, nonblocking proof the process is gone.
        """
        reaped = self._reap_exited_process_handle(task_id)
        if reaped is not None:
            return reaped
        record = self.store.get(task_id)
        if record.state in TERMINAL_STATES or task_id in self._process_handles or task_id in self._direct_api_handles:
            return record
        if record.state not in self._RECONCILABLE_STATES:
            return record
        if record.runtime not in self.subprocess_adapters and record.runtime != DIRECT_API_PROFILE:
            return record  # mock tasks never hold a subprocess; nothing to reconcile
        launch = self.store.get_launch(task_id)
        if is_durable_launch_row(launch) and record.runtime in self.subprocess_adapters:
            adapter = self.subprocess_adapters[record.runtime]
            if getattr(adapter.capabilities, "uses_durable_subprocess_runner", False):
                return self._reconcile_durable_subprocess(task_id, record, launch, adapter.name)
        if record.runtime == DIRECT_API_PROFILE:
            launch = self.store.get_launch(task_id)
            if launch is not None:
                self.store.mark_launch_reconciled(task_id)
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.FAILED,
                "Direct API request was in flight when the broker restarted; outcome could not be observed",
                {"reason": "direct_api_restart_no_reattachment", "provider": launch.get("command", [None])[0] if launch else None},
            )
        adapter_name = self.subprocess_adapters[record.runtime].name
        # Cursor only (Wave 3 field evidence, docs/history/phases/phase-7c5-cursor-uncollected.md):
        # a reparented same-PGID Cursor helper can survive the supervised leader by
        # minutes, so process-group liveness alone cannot prove the leader is dead.
        # CANCELLING is excluded here and falls through to the unchanged pgid-based
        # path below — explicit cancellation authority is out of scope for this fix.
        if adapter_name == "cursor" and record.state is not TaskState.CANCELLING:
            launch = self.store.get_launch(task_id)
            if launch is not None and isinstance(launch.get("pid"), int):
                return self._reconcile_cursor_legacy_subprocess(task_id, record, launch)
        status = self._process_group_status(task_id)
        launch = self.store.get_launch(task_id)
        if launch is not None:
            self.store.mark_launch_reconciled(task_id)
        was_cancelling = record.state is TaskState.CANCELLING
        if status in ("dead", "no_launch"):
            self._finalize_workspace(task_id)
            reason = "process_group_confirmed_dead" if status == "dead" else "missing_process_handle"
            target = TaskState.CANCELLED if was_cancelling else TaskState.FAILED
            if status == "dead":
                message = (
                    f"{adapter_name} process group is no longer present after a broker restart; the in-progress "
                    "cancellation is confirmed complete" if was_cancelling else
                    f"{adapter_name} process group is no longer present after a broker restart; the runtime outcome "
                    "could not be observed and is recorded as failed"
                )
            else:
                message = "No running process handle or durable launch record for this task"
            return self.store.transition(task_id, target, message, {"reason": reason, "pgid": launch["pgid"] if launch else None})
        reason = "process_group_alive_after_restart" if status == "alive" else "runtime_metadata_missing_or_invalid"
        message = (
            "Process group still appears active after a broker restart; task requires "
            "explicit operator reconciliation (reconcile again once it exits, or cancel it)"
            if status == "alive" else
            "Runtime launch metadata is missing or invalid; treating conservatively as possibly still active"
        )
        if record.state is not TaskState.RECOVERY_REQUIRED:
            return self.store.transition(task_id, TaskState.RECOVERY_REQUIRED, message, {"reason": reason, "pgid": launch["pgid"] if launch else None})
        self.store.event(task_id, "task.reconciliation_checked", record.state, record.state, message, {"reason": reason})
        return record

    def _reconcile_cursor_legacy_subprocess(self, task_id: str, record: TaskRecord, launch: dict) -> TaskRecord:
        """Cursor-only restart reconciliation using leader PID+start-identity proof.

        Process-group liveness (`_process_group_status`) is recorded here only as
        compact diagnostic metadata — never as proof of the leader's death. Only
        `classify_process_identity` returning "dead" (the persisted pid no longer
        exists, or now belongs to a different process per anti-reuse start_identity)
        is accepted as proof the supervised Cursor leader exited. "alive" and
        "unknown" (missing/unverifiable identity) both retain the existing
        recovery_required behavior — death is never inferred from a live or
        unconfirmed leader. See docs/history/phases/phase-7c5-cursor-uncollected.md.
        """
        pid = launch.get("pid")
        expected_identity = launch.get("leader_start_identity")
        leader_state = classify_process_identity(pid, expected_identity)
        group_state = self._process_group_status(task_id)
        self.store.mark_launch_reconciled(task_id)
        leader_meta = {
            "pid": pid,
            "identity_captured_at_launch": bool(expected_identity),
            "state": leader_state,
        }
        process_group_meta = {
            "pgid": launch.get("pgid"),
            "state": group_state,
            "helpers_may_linger": leader_state == "dead" and group_state == "alive",
        }
        metadata = {"leader": leader_meta, "process_group": process_group_meta}

        if leader_state != "dead":
            reason = "cursor_leader_alive_after_restart" if leader_state == "alive" else "cursor_leader_identity_unverifiable"
            message = (
                "Cursor leader process is still alive after a broker restart; task requires "
                "explicit operator reconciliation (reconcile again once it exits, or cancel it)"
                if leader_state == "alive" else
                "Cursor leader identity could not be safely verified after a broker restart; "
                "treating conservatively as possibly still active"
            )
            metadata = {**metadata, "reason": reason}
            if record.state is not TaskState.RECOVERY_REQUIRED:
                return self.store.transition(task_id, TaskState.RECOVERY_REQUIRED, message, metadata)
            self.store.event(task_id, "task.reconciliation_checked", record.state, record.state, message, metadata)
            return record

        # leader_state == "dead": positive proof the leader exited. The broker
        # never collected a terminal result from it (no in-memory handle survived
        # the restart) — the outcome cannot be asserted either way.
        self._finalize_workspace(task_id)
        message = (
            "Cursor leader process is proven exited after a broker restart, but no terminal result "
            "was ever collected from it; the task outcome cannot be asserted and is recorded as uncollected"
        )
        metadata = {**metadata, "reason": "leader_exited_uncollected", "outcome": "unknown"}
        return self.store.transition(task_id, TaskState.UNCOLLECTED, message, metadata)

    def _reconcile_durable_subprocess(
        self,
        task_id: str,
        record: TaskRecord,
        launch: dict,
        adapter_name: str,
    ) -> TaskRecord:
        durable_launch_id = launch["durable_launch_id"]
        if task_id in self._adopted_durable_handles:
            detail = make_reconcile_detail(
                ReconcileOutcome.ALREADY_ADOPTED,
                "task already holds an adopted durable handle in this broker instance",
                launch_id=durable_launch_id,
                adapter_id=adapter_name,
            )
            self._last_reconcile_details[task_id] = detail
            self._emit_reconcile_event(task_id, record, detail)
            return record

        proof_outcome, inspection, reason = evaluate_durable_reconciliation(
            self.store.home,
            task_id=task_id,
            expected_adapter_id=adapter_name,
            durable_launch_id=durable_launch_id,
            launch_row_adapter=launch["adapter"],
        )
        if proof_outcome not in {ReconcileOutcome.ADOPTED_RUNNING, ReconcileOutcome.ADOPTED_TERMINAL_COLLECTABLE}:
            detail = make_reconcile_detail(
                proof_outcome,
                reason,
                launch_id=durable_launch_id,
                adapter_id=adapter_name,
                inspection=inspection,
            )
            self._last_reconcile_details[task_id] = detail
            self.store.mark_launch_reconciled(task_id)
            message = f"Durable reconciliation refused: {reason}"
            metadata = detail.to_dict()
            if record.state is not TaskState.RECOVERY_REQUIRED:
                return self.store.transition(task_id, TaskState.RECOVERY_REQUIRED, message, metadata)
            self.store.event(task_id, "task.reconciliation_checked", record.state, record.state, message, metadata)
            return record

        acquired = self.store.try_acquire_recovery_lease(
            task_id=task_id,
            durable_launch_id=durable_launch_id,
            broker_id=self._broker_identity.broker_id,
            broker_epoch=self._broker_identity.epoch,
        )
        if not acquired:
            detail = make_reconcile_detail(
                ReconcileOutcome.REFUSED_LEASE_CONTENDED,
                "another broker holds an active recovery lease for this launch",
                launch_id=durable_launch_id,
                adapter_id=adapter_name,
                inspection=inspection,
            )
            self._last_reconcile_details[task_id] = detail
            message = detail.reason
            metadata = detail.to_dict()
            if record.state is not TaskState.RECOVERY_REQUIRED:
                return self.store.transition(task_id, TaskState.RECOVERY_REQUIRED, message, metadata)
            self.store.event(task_id, "task.reconciliation_checked", record.state, record.state, message, metadata)
            return record

        terminal = proof_outcome is ReconcileOutcome.ADOPTED_TERMINAL_COLLECTABLE
        adopted = adopt_durable_handle(
            self.store.home,
            task_id=task_id,
            launch_id=durable_launch_id,
            adapter_id=adapter_name,
            terminal=terminal,
        )
        self._adopted_durable_handles[task_id] = adopted
        detail = make_reconcile_detail(
            proof_outcome,
            reason,
            launch_id=durable_launch_id,
            adapter_id=adapter_name,
            inspection=inspection,
        )
        self._last_reconcile_details[task_id] = detail
        self.store.mark_launch_reconciled(task_id)
        message = (
            "Adopted terminal durable launch; collect bounded artifacts when ready"
            if terminal else
            "Adopted running durable launch after broker-restart proof"
        )
        metadata = detail.to_dict()
        if record.state is TaskState.RECOVERY_REQUIRED or record.state is TaskState.PREPARING:
            return self.store.transition(task_id, TaskState.RUNNING, message, metadata)
        self.store.event(task_id, "task.durable_adopted", record.state, record.state, message, metadata)
        return record

    def _emit_reconcile_event(self, task_id: str, record: TaskRecord, detail: ReconcileDetail) -> None:
        self.store.event(
            task_id,
            "task.reconciliation_checked",
            record.state,
            record.state,
            detail.reason,
            detail.to_dict(),
        )

    def reconcile_pending(self) -> list[TaskRecord]:
        """Reconcile every subprocess-backed task (any profile in `subprocess_adapters`)
        this Broker instance can see is in a reconcilable non-terminal state
        without an in-memory handle — the operation a freshly started broker
        uses to inspect durable active runtime records after a restart,
        without waiting for a caller to happen to call collect()/cancel() on
        each task individually.
        """
        return [
            self.reconcile(record.id)
            for record in self.store.list()
            if (record.runtime in self.subprocess_adapters or record.runtime == DIRECT_API_PROFILE)
            and record.state in self._RECONCILABLE_STATES
            and record.id not in self._process_handles
            and record.id not in self._direct_api_handles
        ]

    def _cancel_by_pgid(self, pgid: int, grace_period_seconds: float = 10.0) -> dict:
        """Signal a process group known only by its durably persisted pgid — there is
        no live Popen/child relationship after a broker restart, so this cannot
        reap or read an exit code, only observe group liveness via killpg.

        ponytail: this is only ever called once `_process_group_status` has
        confirmed the group is alive via killpg(pgid, 0) immediately beforehand,
        never on bare unverified metadata. PID/PGID reuse is still a real
        residual risk (a killpg "alive" only proves *some* process group with
        this id exists right now, not that it's still the one we launched) —
        see docs/history/phases/phase-5b.md for the accepted threat-model tradeoff; there is no
        further escalation path (e.g. /proc start-time comparison) here.
        """
        signals_sent = []
        try:
            os.killpg(pgid, signal.SIGTERM)
            signals_sent.append("SIGTERM")
        except ProcessLookupError:
            pass
        terminated = group_dead_within(pgid, timeout=grace_period_seconds)
        if not terminated:
            try:
                os.killpg(pgid, signal.SIGKILL)
                signals_sent.append("SIGKILL")
            except ProcessLookupError:
                pass
            terminated = group_dead_within(pgid, timeout=grace_period_seconds)
        return {"signals_sent": signals_sent, "group_terminated": terminated, "exit_code": None}

    def timeout(self, task_id: str, reason: str = "Task exceeded configured timeout") -> TaskRecord:
        """Time out a task, but only after classifying whether its runtime process
        group (if any) is actually still alive — the same restart-safety
        classification `reconcile()`/`cancel()` use (Phase 5B), applied here to
        close the gap where a timeout clock alone used to finalize (and delete)
        a workspace a still-running process might still be writing to.

        Idempotent: an already-terminal task is returned unchanged. A
        confirmed-alive (or liveness-unconfirmed) process group is never
        treated as evidence the workspace is safe to finalize — it's driven
        through the same in-memory or pgid-based cancellation `cancel()` uses,
        and only a confirmed termination allows the workspace to be released.
        An unconfirmed group lands in `recovery_required` with the
        workspace/lease untouched, never `timed_out` on the caller's say-so
        alone.
        """
        record = self.store.get(task_id)
        if record.state in TERMINAL_STATES:
            return record

        direct_handle = self._direct_api_handles.get(task_id)
        if direct_handle is not None:
            self._direct_api_handles.pop(task_id, None)
            runtime = self.direct_api_runtime
            assert runtime is not None
            cancellation = runtime.cancel(direct_handle)
            if cancellation["group_terminated"]:
                self._finalize_workspace(task_id)
                return self.store.transition(task_id, TaskState.TIMED_OUT, reason, {"reason": reason, "cancellation": cancellation})
            return self.store.transition(
                task_id, TaskState.RECOVERY_REQUIRED,
                "Timeout fired but the direct API request could not be confirmed aborted",
                {"reason": reason, "cancellation": cancellation},
            )

        handle = self._process_handles.get(task_id)
        if handle is not None:
            self._process_handles.pop(task_id, None)
            cancellation = self.subprocess_adapters[record.runtime].cancel(handle)
            if cancellation["group_terminated"]:
                self._finalize_workspace(task_id)
                return self.store.transition(task_id, TaskState.TIMED_OUT, reason, {"reason": reason, "cancellation": cancellation})
            return self.store.transition(
                task_id, TaskState.RECOVERY_REQUIRED,
                "Timeout fired but the runtime process group could not be confirmed terminated; workspace retained",
                {"reason": reason, "cancellation": cancellation},
            )

        if record.runtime not in self.subprocess_adapters and record.runtime != DIRECT_API_PROFILE:
            # Mock tasks never hold a subprocess; nothing to protect.
            self._finalize_workspace(task_id)
            return self.store.transition(task_id, TaskState.TIMED_OUT, reason, {"reason": reason})

        if record.runtime == DIRECT_API_PROFILE:
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.TIMED_OUT, reason,
                {"reason": reason, "liveness": "direct_api_no_in_memory_handle"},
            )

        status = self._process_group_status(task_id)
        launch = self.store.get_launch(task_id)
        if launch is not None:
            self.store.mark_launch_reconciled(task_id)

        if status in ("dead", "no_launch"):
            self._finalize_workspace(task_id)
            detail = "process_group_confirmed_dead" if status == "dead" else "missing_process_handle"
            return self.store.transition(
                task_id, TaskState.TIMED_OUT, reason,
                {"reason": reason, "liveness": detail, "pgid": launch["pgid"] if launch else None},
            )

        if status == "alive":
            cancellation = self._cancel_by_pgid(launch["pgid"], grace_period_seconds=self.subprocess_adapters[record.runtime].grace_period_seconds)
            if cancellation["group_terminated"]:
                self._finalize_workspace(task_id)
                return self.store.transition(
                    task_id, TaskState.TIMED_OUT, reason,
                    {"reason": reason, "liveness": "process_group_alive_then_terminated", "cancellation": cancellation},
                )
            return self.store.transition(
                task_id, TaskState.RECOVERY_REQUIRED,
                "Timeout fired while the persisted process group was still alive and could not be confirmed terminated; workspace retained",
                {"reason": reason, "liveness": "process_group_alive_after_restart", "cancellation": cancellation},
            )

        # status == "unknown": pgid missing/invalid — never treated as proof of death.
        return self.store.transition(
            task_id, TaskState.RECOVERY_REQUIRED,
            "Timeout fired but process group liveness could not be confirmed from persisted metadata; refusing to finalize",
            {"reason": reason, "liveness": "runtime_metadata_missing_or_invalid"},
        )

    def cancel(self, task_id: str, reason: str) -> TaskRecord:
        """Cancel a task, observing (never assuming) whether the work actually stopped.

        Idempotent for an already-terminal task. When an in-memory process
        handle exists this is the original same-process cancellation path. When
        it doesn't (mock task, or a subprocess-backed task whose handle was lost
        to a broker restart), this consults the durable launch record: a mock
        task (or a subprocess-backed task with no launch record at all — an
        anomaly with nothing to protect) is cancelled immediately; a
        subprocess-backed task with a confirmed-alive persisted process group is
        signalled via its pgid directly (never blindly declared cancelled);
        anything the broker can't confirm is dead is never assumed safe to
        clean up.
        """
        record = self.store.get(task_id)
        if record.state in TERMINAL_STATES:
            return record
        if record.state is TaskState.QUEUED:
            self._finalize_workspace(task_id)
            return self.store.transition(task_id, TaskState.CANCELLED, reason, {"reason": reason})
        direct_handle = self._direct_api_handles.get(task_id)
        if direct_handle is not None:
            runtime = self.direct_api_runtime
            assert runtime is not None
            record = self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            self._direct_api_handles.pop(record.id, None)
            cancellation = runtime.cancel(direct_handle)
            target = TaskState.CANCELLED if cancellation["group_terminated"] else TaskState.FAILED
            message = (
                "Direct API request aborted" if cancellation["group_terminated"]
                else "Direct API request abort unconfirmed"
            )
            if cancellation["group_terminated"]:
                self._finalize_workspace(record.id)
            return self.store.transition(record.id, target, message, {"reason": reason, "cancellation": cancellation})
        handle = self._process_handles.get(task_id)
        if handle is not None:
            adapter_name = self.subprocess_adapters[record.runtime].name
            record = self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            self._process_handles.pop(record.id, None)
            cancellation = self.subprocess_adapters[record.runtime].cancel(handle)
            target = TaskState.CANCELLED if cancellation["group_terminated"] else TaskState.FAILED
            message = f"{adapter_name} process group terminated" if cancellation["group_terminated"] else f"{adapter_name} process group termination unconfirmed"
            if cancellation["group_terminated"]:
                self._finalize_workspace(record.id)
            return self.store.transition(record.id, target, message, {"reason": reason, "cancellation": cancellation})

        adopted = self._adopted_durable_handles.get(task_id)
        if adopted is not None:
            adapter_name = adopted.adapter_id
            record = self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            cancellation = adopted_cancel(adopted)
            self._adopted_durable_handles.pop(task_id, None)
            self.store.release_recovery_lease(task_id)
            target = TaskState.CANCELLED if cancellation["group_terminated"] else TaskState.FAILED
            message = (
                f"{adapter_name} adopted durable process group terminated"
                if cancellation["group_terminated"] else
                f"{adapter_name} adopted durable cancellation unconfirmed"
            )
            if cancellation["group_terminated"]:
                self._finalize_workspace(record.id)
            return self.store.transition(record.id, target, message, {"reason": reason, "cancellation": cancellation, "adopted_durable": True})

        if record.runtime not in self.subprocess_adapters and record.runtime != DIRECT_API_PROFILE:
            self._finalize_workspace(task_id)
            if record.state is not TaskState.CANCELLING:
                self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            return self.store.transition(task_id, TaskState.CANCELLED, "Mock cancellation confirmed", {"reason": reason})

        if record.runtime == DIRECT_API_PROFILE:
            if record.state is not TaskState.CANCELLING:
                self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.CANCELLED,
                "Direct API request marked cancelled; in-flight HTTP cannot be aborted after broker restart",
                {"reason": reason, "note": "no_session_reattachment"},
            )

        adapter_name = self.subprocess_adapters[record.runtime].name
        status = self._process_group_status(task_id)
        launch = self.store.get_launch(task_id)
        if launch is not None:
            self.store.mark_launch_reconciled(task_id)

        if status == "no_launch":
            # Anomalous for a subprocess-backed task (start() always records a
            # launch before RUNNING) — nothing durable to signal or protect either way.
            self._finalize_workspace(task_id)
            if record.state is not TaskState.CANCELLING:
                self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            return self.store.transition(task_id, TaskState.CANCELLED, "Mock cancellation confirmed", {"reason": reason})

        if status == "unknown":
            # Never signal a pgid we can't confirm liveness for.
            if record.state in (TaskState.PREPARING, TaskState.RUNNING):
                return self.store.transition(
                    task_id, TaskState.RECOVERY_REQUIRED,
                    "Cannot confirm process group liveness from persisted metadata; refusing to signal an unverified pgid",
                    {"reason": "runtime_metadata_missing_or_invalid"},
                )
            self.store.event(
                task_id, "task.reconciliation_checked", record.state, record.state,
                "Cancellation requested but process group liveness still cannot be confirmed",
                {"reason": "runtime_metadata_missing_or_invalid"},
            )
            return record

        if record.state is not TaskState.CANCELLING:
            self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})

        if status == "dead":
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.CANCELLED, f"{adapter_name} process group already terminated",
                {"reason": reason, "cancellation": {"signals_sent": [], "group_terminated": True, "note": "confirmed dead via persisted pgid before any signal was sent"}},
            )

        cancellation = self._cancel_by_pgid(launch["pgid"], grace_period_seconds=self.subprocess_adapters[record.runtime].grace_period_seconds)
        if cancellation["group_terminated"]:
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.CANCELLED, f"{adapter_name} process group terminated via persisted pgid after broker restart",
                {"reason": reason, "cancellation": cancellation},
            )
        return self.store.transition(
            task_id, TaskState.RECOVERY_REQUIRED, "Cancellation signalled via persisted pgid but termination could not be confirmed",
            {"reason": reason, "cancellation": cancellation},
        )

    def operator_control_view(self, task_id: str) -> dict:
        """Return action-safe lifecycle guidance without performing an operation."""
        from .operator_control import build_operator_control_view

        return build_operator_control_view(self, task_id)

    def operator_control(
        self,
        task_id: str,
        action: str,
        *,
        reason: str = "Cancelled by operator control",
        message_content: str | None = None,
    ) -> dict:
        """Bounded operator recovery/control: explicit action, fail-closed gates (Phase 7C.4)."""
        from .operator_control import execute_operator_control

        return execute_operator_control(
            self,
            task_id,
            action,
            reason=reason,
            message_content=message_content,
        )

    def operator_control_view(self, task_id: str) -> dict:
        from .operator_control import build_operator_control_view

        return build_operator_control_view(self, task_id)

    def status(self, task_id: str) -> dict:
        # A stale in-memory handle for an already-exited legacy subprocess
        # must never keep reporting `running` (see
        # `_reap_exited_process_handle`); this is a nonblocking check
        # (Popen.poll()) so it stays a prompt, side-effect-visible-only-when-
        # actually-finished read from the caller's perspective.
        record = self._reap_exited_process_handle(task_id) or self.store.get(task_id)
        if record.state not in TERMINAL_STATES:
            # The artifact manifest is otherwise only refreshed when an
            # artifact is written or a task is collected -- for a still-active
            # task that leaves it frozen at whatever it looked like when the
            # runtime launched (often a zero-byte placeholder), which reads as
            # stale/dead evidence about a task that may in fact be actively
            # writing output right now. Cheap (local file stat/hash, no
            # process signaling) enough to just always do for active tasks.
            self.store.refresh_manifest(task_id)
        payload = {
            **record.json(),
            "events": self.store.events(task_id),
            "artifacts": self.store.artifact_manifest(task_id),
            "runtime_launch": self.store.get_launch(task_id),
        }
        detail = self.reconcile_detail(task_id)
        if detail is not None:
            payload["reconciliation"] = detail
        adopted = self._adopted_durable_handles.get(task_id)
        if adopted is not None:
            payload["adopted_durable"] = adopted_status(adopted)
        normalized_path = self.store.artifacts / task_id / NORMALIZED_RESULT_ARTIFACT
        if normalized_path.is_file():
            envelope = json.loads(normalized_path.read_text())
            payload["normalized_result"] = concise_normalized_view(envelope)
        warning = self.schema_conflict_warning(task_id)
        if warning is not None:
            payload["schema_conflict_warning"] = warning
        return payload

    def _finalize_workspace(self, task_id: str) -> None:
        """Capture diff/status evidence and release a task's worktree lease, if any.

        Idempotent: a lease that is missing or already released means a prior
        cleanup already ran (or none was ever allocated), so this is a no-op.
        Diff capture failures are recorded, not raised — a broken git command
        must never block the lease/worktree release that would otherwise
        block every future writer to that source.
        """
        lease = self.store.get_lease(task_id)
        if lease is None or lease["status"] != "active":
            return
        payload = {
            "task_id": task_id,
            "source": lease["canonical_source"],
            "worktree_path": lease["worktree_path"],
            "branch": lease["branch"],
            "base_sha": lease["base_sha"],
            "captured_at": now(),
        }
        try:
            status = self.workspaces.capture_status(lease["worktree_path"], lease["base_sha"])
            payload.update({
                "changed_paths": status["changed_paths"],
                "diff_status": status["diff_status"],
                "diff_artifact": "diff.patch",
            })
            diff_bytes = status["diff_bytes"]
        except WorkspaceError as error:
            payload.update({"changed_paths": [], "diff_status": "unknown", "diff_artifact": "diff.patch", "capture_error": str(error)})
            diff_bytes = b""
        self.store.write_artifact(task_id, "workspace_status.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        self.store.write_artifact(task_id, "diff.patch", diff_bytes)
        release_result = self.workspaces.release(lease["canonical_source"], lease["worktree_path"])
        self.store.release_lease(task_id)
        self.store.event(
            task_id, "task.workspace_released", None, None,
            "Broker released the isolated worktree", {"release": release_result},
        )

    def _load_verify_commands(self, task_id: str) -> list[list[str]] | None:
        path = self.store.artifacts / task_id / "verify_commands.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def _apply_verification_gate(self, task_id: str, record: TaskRecord, candidate_state: TaskState) -> tuple[TaskState, dict]:
        """Fold any declared broker-run verification into `candidate_state` per the
        task's verification_policy. Always called after the runtime has
        definitely finished (a runtime result/candidate_state already exists)
        and before `_finalize_workspace` releases the worktree lease, so
        verification still sees the same workspace the runtime task wrote to.

        Declared verify_commands always run (as broker-verified evidence)
        whenever present, independent of the runtime outcome — unconditional
        evidence collection, matching Phase 3/5B's existing behavior and the
        PRD's evidence-first pillar. Whether the *outcome* changes
        `candidate_state` depends on policy:
          - "none": never — evidence-only, the default, fully backward
            compatible with every pre-5C caller.
          - "advisory": a failure downgrades a runtime success to
            succeeded_with_warnings; it never blocks a success outright, and
            never touches an already-failed candidate.
          - "required": a failure (or missing/blocked verification) forces a
            runtime success to failed. A runtime failure is never "rescued"
            by passing verification either way.
        """
        policy = record.verification_policy
        commands = self._load_verify_commands(task_id)
        gate = {"policy": policy, "commands_declared": bool(commands)}
        is_candidate_success = candidate_state in (TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS)

        if not commands:
            gate["outcome"] = "not_configured"
            if policy == "required" and is_candidate_success:
                gate["outcome"] = "blocked_no_commands_declared"
                return TaskState.FAILED, gate
            return candidate_state, gate

        try:
            verification = self.verify(task_id, commands)
        except Exception as error:
            gate["outcome"] = "blocked_verification_error"
            gate["error"] = str(error)
            if policy == "required" and is_candidate_success:
                return TaskState.FAILED, gate
            return candidate_state, gate

        gate["verification_artifact"] = "verification.json"
        passed = bool(verification["commands"]) and all(command["passed"] for command in verification["commands"])
        gate["outcome"] = "passed" if passed else "failed"

        if passed or not is_candidate_success:
            return candidate_state, gate
        if policy == "required":
            return TaskState.FAILED, gate
        if policy == "advisory":
            return TaskState.SUCCEEDED_WITH_WARNINGS, gate
        return candidate_state, gate  # policy == "none": informational only

    def _write_gate_artifact(self, task_id: str, gate: dict) -> None:
        """Skip writing when nothing meaningful happened (policy=none, no commands
        declared) so a plain evidence-only task's artifact manifest is unchanged
        from pre-5C behavior.
        """
        if not gate["commands_declared"] and gate["policy"] != "required":
            return
        self.store.write_artifact(task_id, "verification_gate.json", json.dumps(gate, indent=2, sort_keys=True) + "\n")

    def verify(self, task_id: str, commands: list[list[str]]) -> dict:
        """Run broker-declared verification commands as argv arrays (never shell=True).

        An isolated_worktree task always runs verification in its worktree,
        never in the source it was allocated from — if that worktree has
        already been released (task finalized, or allocation never
        succeeded), verification is refused outright rather than silently
        falling back to the caller's real workspace. Only a read_only task
        (which never gets a worktree) runs directly against its workspace.
        `broker_verified` is always true here because this is, by
        construction, the broker's own subprocess execution — as opposed to
        whatever an adapter/agent merely reports about itself.

        Refuses outright on a recovery_required task: its worktree lease is
        still active (by design — reconciliation never releases a lease it
        can't prove is safe), but a persisted-alive process group may still be
        writing to it, so running commands there would race an unobserved
        process rather than verify a settled result.
        """
        record = self.store.get(task_id)
        if record.state is TaskState.RECOVERY_REQUIRED:
            raise ValueError(
                "Cannot run verification: task requires reconciliation before further action "
                "(state=recovery_required; its process group may still be alive)"
            )
        lease = self.store.get_lease(task_id)
        if record.execution_mode == ISOLATED_WORKTREE:
            if lease is None or lease["status"] != "active":
                raise ValueError(
                    "Cannot run verification: this task's isolated worktree is not currently active "
                    "(not yet allocated, or already released)"
                )
            working_dir, scope = lease["worktree_path"], "isolated_worktree"
        else:
            working_dir, scope = record.workspace, "source_workspace"
        for command in commands:
            if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
                raise ValueError("Verification commands must be non-empty argv arrays of strings")
        results = []
        for command in commands:
            started = time.monotonic()
            completed = subprocess.run(command, cwd=working_dir, capture_output=True, text=True)
            duration = time.monotonic() - started
            results.append({
                "command": command,
                "cwd": working_dir,
                "exit_code": completed.returncode,
                "passed": completed.returncode == 0,
                "duration_seconds": round(duration, 6),
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "broker_verified": True,
            })
        payload = {"task_id": task_id, "scope": scope, "commands": results, "captured_at": now()}
        self.store.write_artifact(task_id, "verification.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        self.store.event(
            task_id, "task.verified", record.state, record.state,
            "Broker executed verification commands",
            {"scope": scope, "count": len(results), "all_passed": all(r["passed"] for r in results)},
        )
        return payload

    def discover_capabilities(self) -> dict:
        from .discovery import discover_providers, discover_runtimes, provider_config_lifecycle
        from .recovery_contract import RECOVERY_CONTRACT_SCHEMA_VERSION

        environ = self._environ if self._environ is not None else os.environ
        return {
            "recovery_contract_schema_version": RECOVERY_CONTRACT_SCHEMA_VERSION,
            "runtimes": discover_runtimes(
                registry=self.runtime_registry,
                subprocess_adapters=self.subprocess_adapters,
                mock_adapter=self.adapter,
                direct_api_runtime=self.direct_api_runtime,
            ),
            "providers": discover_providers(
                direct_api_runtime=self.direct_api_runtime,
                environ=environ,
            ),
            "provider_config": provider_config_lifecycle(self.direct_api_runtime),
            "agent_profiles": list_agent_profiles(self.agent_profiles),
        }

    def list_agent_profiles(self) -> list[dict]:
        return list_agent_profiles(self.agent_profiles)

    def select_candidates(
        self,
        *,
        execution_mode: str,
        required_runtime_capabilities: dict[str, bool] | None = None,
        required_provider_capabilities: dict[str, bool] | None = None,
        allowed_runtimes: list[str] | None = None,
        allowed_providers: list[str] | None = None,
        require_available: bool = True,
    ) -> dict:
        from .discovery import select_candidates as _select_candidates

        environ = self._environ if self._environ is not None else os.environ
        return _select_candidates(
            registry=self.runtime_registry,
            subprocess_adapters=self.subprocess_adapters,
            direct_api_runtime=self.direct_api_runtime,
            environ=environ,
            execution_mode=execution_mode,
            required_runtime_capabilities=required_runtime_capabilities,
            required_provider_capabilities=required_provider_capabilities,
            allowed_runtimes=allowed_runtimes,
            allowed_providers=allowed_providers,
            require_available=require_available,
        )

    def validate_council(self, plan: dict) -> dict:
        from .council import validate_council_plan

        return validate_council_plan(self, plan)

    def execute_council(self, plan: dict) -> dict:
        from .council import execute_council as _execute_council

        return _execute_council(self, plan)
