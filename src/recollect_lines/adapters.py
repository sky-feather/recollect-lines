"""Runtime adapter boundary: shared types and capability reporting for task execution backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .recovery_contract import RecoveryControlContract


@dataclass(frozen=True)
class AdapterCapabilities:
    requires_subprocess: bool
    supports_process_group_cancellation: bool
    reports_broker_verified_tests: bool
    recovery_control: RecoveryControlContract
    uses_durable_subprocess_runner: bool = False


@runtime_checkable
class RuntimeAdapter(Protocol):
    name: str
    capabilities: AdapterCapabilities
