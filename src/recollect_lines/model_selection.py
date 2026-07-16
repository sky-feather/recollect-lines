"""Capability-gated per-task model resolution (Phase 8.3)."""

from __future__ import annotations

from typing import Any

from .runtime_registry import ModelSelectionSupport, RuntimeDescriptor


class ModelSelectionRefusedError(ValueError):
    """Per-task model was requested but the runtime cannot honor it."""

    def __init__(self, runtime: str, model: str, support: ModelSelectionSupport):
        self.runtime = runtime
        self.model = model
        self.support = support
        super().__init__(
            f"runtime {runtime!r} does not accept per-task model selection "
            f"(model_selection={support.value}); refused model={model!r}"
        )


def refuses_per_task_model(support: ModelSelectionSupport) -> bool:
    return support in {
        ModelSelectionSupport.NOT_SUPPORTED,
        ModelSelectionSupport.PERSISTED_NOT_INVOKED,
    }


def validate_requested_model(descriptor: RuntimeDescriptor, requested_model: str | None) -> None:
    if requested_model is None:
        return
    if refuses_per_task_model(descriptor.model_selection):
        raise ModelSelectionRefusedError(descriptor.name, requested_model, descriptor.model_selection)


def resolve_effective_model(
    descriptor: RuntimeDescriptor,
    *,
    requested_model: str | None,
    adapter_default: str | None = None,
    provider_default: str | None = None,
) -> tuple[str | None, str]:
    """Return (effective_model, source) for launch metadata.

    source is one of: none, task_request, adapter_default, provider_default, runtime_default.
    """
    if descriptor.model_selection is ModelSelectionSupport.NOT_SUPPORTED:
        return None, "none"
    if descriptor.model_selection is ModelSelectionSupport.PROVIDER_CONFIG_DEFAULT:
        if requested_model is not None:
            return requested_model, "task_request"
        if provider_default is None:
            raise ValueError(f"runtime {descriptor.name!r} requires a provider default_model")
        return provider_default, "provider_default"
    if descriptor.model_selection is ModelSelectionSupport.PER_TASK_REQUEST:
        if requested_model is not None:
            return requested_model, "task_request"
        if adapter_default is not None:
            return adapter_default, "adapter_default"
        return None, "runtime_default"
    # PERSISTED_NOT_INVOKED: validate_requested_model should have refused a request.
    if adapter_default is not None:
        return adapter_default, "adapter_default"
    return None, "runtime_default"


def model_selection_metadata(
    *,
    requested_model: str | None,
    effective_model: str | None,
    source: str,
    invoked: bool,
) -> dict[str, Any]:
    return {
        "requested_model": requested_model,
        "effective_model": effective_model,
        "source": source,
        "invoked": invoked,
        "provider_confirmed": False,
    }
