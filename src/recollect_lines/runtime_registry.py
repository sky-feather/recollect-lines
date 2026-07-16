"""Central runtime catalog: immutable descriptors and deterministic registration (Phase 8.2)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .adapters import AdapterCapabilities
from .claude_code_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter
from .cursor_adapter import CursorAdapter
from .direct_api_runtime import DIRECT_API_PROFILE, OpenAiCompatibleDirectRuntime
from .models import DEFAULT_PROFILES, ProfilePolicy
from .opencode_adapter import OpenCodeAdapter
from .recovery_contract import (
    SYNTHETIC_RECOVERY_CONTROL,
)

SUBPROCESS_LIMITATIONS = (
    "no_durable_session_reattachment_after_broker_restart",
    "no_live_mid_task_steering",
    "broker_observed_cancellation_not_runtime_self_report",
)
DIRECT_API_LIMITATIONS = (
    "read_only_execution_only",
    "no_subprocess_supervision",
    "no_isolated_worktree",
    "no_process_group_cancellation",
    "cooperative_http_abort_only",
    "no_live_mid_task_steering",
    "no_durable_session_reattachment_after_broker_restart",
    "no_agent_tool_loop",
)


class ExecutionStrategy(StrEnum):
    SUBPROCESS_CLI = "subprocess_cli"
    DIRECT_API = "direct_api"
    SYNTHETIC = "synthetic"
    FIXTURE = "fixture"


class ModelSelectionSupport(StrEnum):
    NOT_SUPPORTED = "not_supported"
    PERSISTED_NOT_INVOKED = "persisted_not_invoked"
    PER_TASK_REQUEST = "per_task_request"
    PROVIDER_CONFIG_DEFAULT = "provider_config_default"


class DuplicateRuntimeRegistrationError(ValueError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"runtime {name!r} is already registered")


class UnknownRuntimeError(ValueError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"unknown runtime {name!r}")


@dataclass(frozen=True)
class RuntimeDescriptor:
    name: str
    execution_strategy: ExecutionStrategy
    policy: ProfilePolicy
    adapter_capabilities: AdapterCapabilities
    limitations: tuple[str, ...]
    model_selection: ModelSelectionSupport
    requires_named_provider: bool = False
    runtime_label: str | None = None

    @property
    def discovery_kind(self) -> str:
        if self.execution_strategy is ExecutionStrategy.DIRECT_API:
            return "direct_api"
        if self.execution_strategy is ExecutionStrategy.SYNTHETIC:
            return "synthetic"
        return "subprocess_cli"


_MOCK_CAPABILITIES = AdapterCapabilities(
    requires_subprocess=False,
    supports_process_group_cancellation=False,
    reports_broker_verified_tests=False,
    recovery_control=SYNTHETIC_RECOVERY_CONTROL,
)


class RuntimeRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, RuntimeDescriptor] = {}

    def copy(self) -> RuntimeRegistry:
        other = RuntimeRegistry()
        other._entries = dict(self._entries)
        return other

    def register(self, descriptor: RuntimeDescriptor) -> None:
        if descriptor.name in self._entries:
            raise DuplicateRuntimeRegistrationError(descriptor.name)
        self._entries[descriptor.name] = descriptor

    def get(self, name: str) -> RuntimeDescriptor:
        try:
            return self._entries[name]
        except KeyError as error:
            raise UnknownRuntimeError(name) from error

    def contains(self, name: str) -> bool:
        return name in self._entries

    def known_runtimes(self) -> frozenset[str]:
        return frozenset(self._entries)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def policies(self) -> dict[str, ProfilePolicy]:
        return {name: descriptor.policy for name, descriptor in self._entries.items()}

    def descriptors(self) -> tuple[RuntimeDescriptor, ...]:
        return tuple(self._entries[name] for name in sorted(self._entries))

    def subprocess_runtime_names(self) -> frozenset[str]:
        return frozenset(
            descriptor.name
            for descriptor in self._entries.values()
            if descriptor.execution_strategy in {ExecutionStrategy.SUBPROCESS_CLI, ExecutionStrategy.FIXTURE}
        )

    def direct_api_runtime_names(self) -> frozenset[str]:
        return frozenset(
            descriptor.name
            for descriptor in self._entries.values()
            if descriptor.execution_strategy is ExecutionStrategy.DIRECT_API
        )


def resolve_runtime_label(descriptor: RuntimeDescriptor, adapter: object | None = None) -> str:
    """Return a JSON-safe descriptive label for discovery output."""
    static = descriptor.runtime_label
    if isinstance(static, str):
        return static
    if adapter is not None:
        dynamic = getattr(adapter, "runtime_label", descriptor.name)
        if isinstance(dynamic, str):
            return dynamic
    return descriptor.name


def _subprocess_descriptor(
    adapter_cls: type,
    *,
    policy_key: str,
    model_selection: ModelSelectionSupport,
) -> RuntimeDescriptor:
    return RuntimeDescriptor(
        name=adapter_cls.name,
        execution_strategy=ExecutionStrategy.SUBPROCESS_CLI,
        policy=DEFAULT_PROFILES[policy_key],
        adapter_capabilities=adapter_cls.capabilities,
        limitations=SUBPROCESS_LIMITATIONS,
        model_selection=model_selection,
    )


def build_default_runtime_registry() -> RuntimeRegistry:
    registry = RuntimeRegistry()
    registry.register(RuntimeDescriptor(
        name="mock",
        execution_strategy=ExecutionStrategy.SYNTHETIC,
        policy=DEFAULT_PROFILES["mock"],
        adapter_capabilities=_MOCK_CAPABILITIES,
        limitations=SUBPROCESS_LIMITATIONS,
        model_selection=ModelSelectionSupport.NOT_SUPPORTED,
    ))
    registry.register(_subprocess_descriptor(
        OpenCodeAdapter, policy_key=OpenCodeAdapter.name, model_selection=ModelSelectionSupport.PERSISTED_NOT_INVOKED,
    ))
    for adapter_cls in (ClaudeCodeAdapter, CodexAdapter, CursorAdapter):
        registry.register(_subprocess_descriptor(
            adapter_cls, policy_key=adapter_cls.name, model_selection=ModelSelectionSupport.PER_TASK_REQUEST,
        ))
    registry.register(RuntimeDescriptor(
        name=DIRECT_API_PROFILE,
        execution_strategy=ExecutionStrategy.DIRECT_API,
        policy=DEFAULT_PROFILES[DIRECT_API_PROFILE],
        adapter_capabilities=OpenAiCompatibleDirectRuntime.capabilities,
        limitations=DIRECT_API_LIMITATIONS,
        model_selection=ModelSelectionSupport.PROVIDER_CONFIG_DEFAULT,
        requires_named_provider=True,
        runtime_label=OpenAiCompatibleDirectRuntime.name,
    ))
    return registry


DEFAULT_RUNTIME_REGISTRY = build_default_runtime_registry()
