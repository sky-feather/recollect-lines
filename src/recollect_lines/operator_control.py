"""Bounded operator recovery/control surface (Phase 7C.4).

Exposes a machine-readable, secret-safe view of what status/cancel/collect/message
may do for a task, and executes only explicitly requested actions without bypassing
7C.3 proof/lease/identity gates.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any

from .durable_reconciliation import (
    LAUNCH_KIND_DURABLE,
    adopted_status,
    is_durable_launch_row,
)
from .models import TERMINAL_STATES, RecoveryRequired, TaskRecord, TaskState
from .recovery_contract import ControlAction, parse_control_action

OPERATOR_CONTROL_SCHEMA_VERSION = "1"
_CONTROL_ACTIONS = tuple(action.value for action in ControlAction)
_REDACT_RE = re.compile(r"sk-[A-Za-z0-9_-]{4,}|rl_secret_sentinel\w*|RL_SECRET_SENTINEL", re.IGNORECASE)

_DISTINCTION = {
    "process_recovery": (
        "Broker-owned durable subprocess adoption after restart (status, owned-group cancel, "
        "terminal collect) — distinct from provider session resume."
    ),
    "provider_session_resume": "not_implemented",
    "continuation_task": "out_of_scope",
    "free_form_steering": "unsupported",
}


class RecoveryPosture(StrEnum):
    OBSERVED = "observed"
    RECOVERY_REQUIRED = "recovery_required"
    SAFELY_ADOPTED = "safely_adopted"
    TERMINAL = "terminal"
    REFUSED = "refused"


class OperatorControlRefused(ValueError):
    """Raised when an operator requests an action the recovery contract refuses."""

    def __init__(self, *, code: str, message: str, control: dict[str, Any]):
        self.code = code
        self.message = message
        self.control = control
        super().__init__(message)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _REDACT_RE.sub("<redacted>", value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _adapter_for_profile(broker: Any, profile: str) -> Any:
    if profile in broker.subprocess_adapters:
        return broker.subprocess_adapters[profile]
    if profile == broker.adapter.name:
        return broker.adapter
    from .direct_api_runtime import DIRECT_API_PROFILE

    if profile == DIRECT_API_PROFILE and broker.direct_api_runtime is not None:
        return broker.direct_api_runtime
    return None


def _recovery_level(broker: Any, record: TaskRecord) -> str:
    adapter = _adapter_for_profile(broker, record.runtime)
    if adapter is None:
        return "none"
    contract = getattr(getattr(adapter, "capabilities", None), "recovery_control", None)
    return contract.recovery_level.value if contract is not None else "none"


def _durable_without_adoption(broker: Any, task_id: str, launch: dict[str, Any] | None) -> bool:
    return (
        is_durable_launch_row(launch)
        and task_id not in broker._adopted_durable_handles
        and task_id not in broker._process_handles
    )


def _message_action() -> dict[str, Any]:
    return {
        "permitted": False,
        "reason": (
            "in-flight message steering is explicitly unsupported for all runtimes; "
            "this is not provider session resume or continuation"
        ),
    }


def _assess_actions(broker: Any, record: TaskRecord, launch: dict[str, Any] | None) -> tuple[RecoveryPosture, dict[str, dict[str, Any]]]:
    task_id = record.id
    adopted = broker._adopted_durable_handles.get(task_id)
    has_memory = task_id in broker._process_handles or task_id in broker._direct_api_handles

    if record.state in TERMINAL_STATES:
        posture = RecoveryPosture.TERMINAL
        actions = {
            "status": {"permitted": True, "reason": "read-only observation of terminal task state"},
            "cancel": {"permitted": False, "reason": "task already terminal"},
            "collect": {"permitted": True, "reason": "idempotent read of terminal artifacts"},
            "message": _message_action(),
        }
        return posture, actions

    if adopted is not None:
        posture = RecoveryPosture.SAFELY_ADOPTED
        adopted_info = adopted_status(adopted)
        collect_ok = adopted.terminal or adopted_info.get("lifecycle_state") not in {"running", "launching"}
        actions = {
            "status": {"permitted": True, "reason": "read-only observation of adopted durable launch"},
            "cancel": {
                "permitted": not adopted.terminal,
                "reason": (
                    "owned process group cancel via adopted durable handle"
                    if not adopted.terminal else
                    "adopted launch already terminal"
                ),
            },
            "collect": {
                "permitted": collect_ok,
                "reason": (
                    "bounded terminal durable evidence collection after adoption"
                    if collect_ok else
                    "durable payload still running; collect refused until terminal"
                ),
            },
            "message": _message_action(),
        }
        return posture, actions

    if record.state is TaskState.RECOVERY_REQUIRED:
        durable_unadopted = _durable_without_adoption(broker, task_id, launch)
        detail = broker.reconcile_detail(task_id)
        refused_proof = detail is not None and detail.get("outcome", "").startswith("refused_")
        posture = RecoveryPosture.REFUSED if refused_proof else RecoveryPosture.RECOVERY_REQUIRED
        legacy_cancel = not durable_unadopted and launch is not None and broker._process_group_status(task_id) == "alive"
        actions = {
            "status": {"permitted": True, "reason": "read-only observation; task requires reconciliation or adoption"},
            "cancel": {
                "permitted": legacy_cancel,
                "reason": (
                    "legacy observe_and_cancel pgid cancellation after restart"
                    if legacy_cancel else
                    "durable launch requires proof-gated adoption before owned-group cancel"
                ),
            },
            "collect": {
                "permitted": False,
                "reason": (
                    "durable evidence corrupt or contested; collect refused"
                    if refused_proof else
                    "collect requires durable adoption or in-memory handle; recovery_required is fail-closed"
                ),
            },
            "message": _message_action(),
        }
        return posture, actions

    if has_memory:
        posture = RecoveryPosture.OBSERVED
        actions = {
            "status": {"permitted": True, "reason": "read-only observation of in-memory runtime handle"},
            "cancel": {"permitted": True, "reason": "in-memory runtime handle supports cancellation"},
            "collect": {
                "permitted": False,
                "reason": "runtime not terminal; collect refused until completion",
            },
            "message": _message_action(),
        }
        return posture, actions

    durable_unadopted = _durable_without_adoption(broker, task_id, launch)
    if durable_unadopted:
        posture = RecoveryPosture.RECOVERY_REQUIRED
        actions = {
            "status": {"permitted": True, "reason": "read-only observation; durable launch not adopted in this broker"},
            "cancel": {"permitted": False, "reason": "durable launch requires proof-gated adoption before owned-group cancel"},
            "collect": {"permitted": False, "reason": "collect requires proof-gated durable adoption after restart"},
            "message": _message_action(),
        }
        return posture, actions

    posture = RecoveryPosture.OBSERVED
    actions = {
        "status": {"permitted": True, "reason": "read-only observation"},
        "cancel": {"permitted": record.state not in TERMINAL_STATES, "reason": "cancellation available for non-terminal task"},
        "collect": {
            "permitted": False,
            "reason": "runtime not terminal; collect refused until completion",
        },
        "message": _message_action(),
    }
    return posture, actions


def build_operator_control_view(broker: Any, task_id: str) -> dict[str, Any]:
    record = broker.store.get(task_id)
    launch = broker.store.get_launch(task_id)
    posture, actions = _assess_actions(broker, record, launch)
    permitted_actions = sorted(name for name, detail in actions.items() if detail["permitted"])
    payload: dict[str, Any] = {
        "schema_version": OPERATOR_CONTROL_SCHEMA_VERSION,
        "task_id": task_id,
        "task_state": record.state.value,
        "profile": record.profile,
        "launch": None,
        "recovery_posture": posture.value,
        "recovery_level": _recovery_level(broker, record),
        "permitted_actions": permitted_actions,
        "actions": actions,
        "distinction": dict(_DISTINCTION),
    }
    if launch is not None:
        payload["launch"] = {
            "adapter": launch.get("adapter"),
            "launch_kind": launch.get("launch_kind"),
            "durable_launch_id": launch.get("durable_launch_id"),
            "pgid": launch.get("pgid"),
        }
    detail = broker.reconcile_detail(task_id)
    if detail is not None:
        payload["reconciliation"] = detail
    adopted = broker._adopted_durable_handles.get(task_id)
    if adopted is not None:
        payload["adopted_durable"] = adopted_status(adopted)
    return _redact(payload)


def _parse_action(action: str) -> str:
    try:
        return parse_control_action(action).value
    except ValueError as error:
        raise ValueError(str(error)) from error


def execute_operator_control(
    broker: Any,
    task_id: str,
    action: str,
    *,
    reason: str = "Cancelled by operator control",
    message_content: str | None = None,
) -> dict[str, Any]:
    parsed = _parse_action(action)
    if parsed not in _CONTROL_ACTIONS:
        raise ValueError(f"control action must be one of: {', '.join(_CONTROL_ACTIONS)}")
    view = build_operator_control_view(broker, task_id)

    if parsed == ControlAction.MESSAGE.value:
        if message_content is None:
            raise ValueError("'content' is required when action is message")
        return {
            **view,
            "action": parsed,
            "ok": False,
            "refused": True,
            "code": "unsupported_message_steering",
            "result": {
                "status": "unsupported",
                "reason": view["actions"]["message"]["reason"],
            },
        }

    action_detail = view["actions"][parsed]
    if not action_detail["permitted"]:
        raise OperatorControlRefused(
            code=f"refused_{parsed}",
            message=action_detail["reason"],
            control=view,
        )

    if parsed == ControlAction.STATUS.value:
        result = broker.status(task_id)
        return {**view, "action": parsed, "ok": True, "result": _redact(result)}

    if parsed == ControlAction.CANCEL.value:
        record = broker.cancel(task_id, reason)
        return {
            **view,
            "action": parsed,
            "ok": True,
            "result": _redact({"task_id": record.id, "state": record.state.value}),
        }

    if parsed == ControlAction.COLLECT.value:
        try:
            record = broker.collect(task_id)
        except RecoveryRequired as error:
            raise OperatorControlRefused(
                code="refused_collect",
                message=str(error),
                control=view,
            ) from error
        collect_result: dict[str, Any] = {"task_id": record.id, "state": record.state.value}
        result_path = broker.store.artifacts / task_id / "result.json"
        if result_path.is_file():
            collect_result["runtime_result"] = json.loads(result_path.read_text())
        return {**view, "action": parsed, "ok": True, "result": _redact(collect_result)}

    raise ValueError(f"unsupported control action: {parsed}")
