"""Direct OpenAI-compatible chat-completions runtime (Phase 6C).

Capability-limited: sends one chat-completions request per task through an
explicitly selected named provider configuration. Does not claim subprocess
supervision, worktree editing, tool loops, live steering, or process-group
cancellation — only best-effort HTTP abort via a cooperative cancel event.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import AdapterCapabilities
from .recovery_contract import DIRECT_API_RECOVERY_CONTROL
from .models import TaskRecord
from .providers import (
    MissingCredentialReference,
    ProviderConfig,
    ProviderConfigError,
    redact_provider_error,
    resolve_api_key,
)

RUNTIME_DESCRIPTION = "OpenAI-compatible direct HTTP chat-completions runtime"
DIRECT_API_PROFILE = "openai_compatible"

# Connection-level errors a subsequent attempt might plausibly clear within
# the same provider deadline (server mid-restart, transient reset, etc.).
# Everything else -- TLS/certificate failures, DNS/config errors, malformed
# URLs -- is deterministic and retrying it only burns the deadline while
# masking the real cause as a generic timeout.
_TRANSIENT_CONNECTION_ERRORS = (
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
)


class TlsVerificationError(Exception):
    """A provider's TLS certificate could not be verified against a trusted CA chain. Terminal: never retried."""


def _is_transient_url_error(error: urllib.error.URLError) -> bool:
    return isinstance(error.reason, _TRANSIENT_CONNECTION_ERRORS)


@dataclass
class DirectApiHandle:
    task_id: str
    provider_name: str
    model: str
    request_artifact: Path
    response_artifact: Path
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    result: dict[str, Any] | None = None
    error: Exception | None = None
    cancelled: bool = False


class OpenAiCompatibleDirectRuntime:
    name = DIRECT_API_PROFILE
    capabilities = AdapterCapabilities(
        requires_subprocess=False,
        supports_process_group_cancellation=False,
        reports_broker_verified_tests=False,
        recovery_control=DIRECT_API_RECOVERY_CONTROL,
    )

    def __init__(
        self,
        providers: dict[str, ProviderConfig],
        environ: dict[str, str] | None = None,
        config_source: Path | None = None,
        config_source_origin: str | None = None,
    ):
        if not providers:
            raise ProviderConfigError("At least one provider configuration is required")
        self.providers = providers
        self.environ = environ
        # Startup snapshot: this object is built once when the broker/MCP process
        # starts. config_source/loaded_at exist so diagnostics can truthfully say
        # a later on-disk edit to that file has not (yet) reached this process.
        self.config_source = config_source
        # Which precedence tier selected config_source (explicit, env,
        # repo_local, user_level, legacy_default) -- see providers.py
        # resolve_providers_config_source(). None when the caller constructed
        # this runtime with a source but did not resolve it through that
        # precedence order (treated as "explicit" by discovery.py).
        self.config_source_origin = config_source_origin
        self.loaded_at = datetime.now(timezone.utc)

    @property
    def runtime_label(self) -> str:
        return "openai-compatible-direct-api"

    def get_provider(self, name: str) -> ProviderConfig:
        config = self.providers.get(name)
        if config is None:
            raise ProviderConfigError(f"Unknown provider: {name!r}")
        return config

    def _build_ssl_context(self, config: ProviderConfig) -> ssl.SSLContext | None:
        if config.base_url.startswith("http://"):
            return None
        if not config.tls_verify:
            raise ProviderConfigError(
                f"Provider {config.name!r}: disabling TLS verification is not supported; use ca_bundle for custom CAs"
            )
        context = ssl.create_default_context(cafile=config.ca_bundle) if config.ca_bundle else ssl.create_default_context()
        return context

    def _post_chat_completions(
        self,
        config: ProviderConfig,
        api_key: str,
        payload: dict[str, Any],
        cancel_event: threading.Event,
    ) -> tuple[int, dict[str, Any] | str, dict[str, str]]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            config.chat_completions_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        context = self._build_ssl_context(config)
        deadline = time.monotonic() + config.request_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                return 499, "cancelled", {}
            try:
                open_timeout = max(0.1, deadline - time.monotonic())
                if context is None:
                    response_ctx = urllib.request.urlopen(request, timeout=open_timeout)
                else:
                    response_ctx = urllib.request.urlopen(request, timeout=open_timeout, context=context)
                with response_ctx as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    try:
                        parsed = json.loads(raw) if raw.strip() else {}
                    except json.JSONDecodeError:
                        return response.status, raw, headers
                    return response.status, parsed, headers
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8", errors="replace")
                headers = {key.lower(): value for key, value in error.headers.items()} if error.headers else {}
                try:
                    parsed: dict[str, Any] | str = json.loads(raw) if raw.strip() else {"error": {"message": error.reason}}
                except json.JSONDecodeError:
                    parsed = raw
                return error.code, parsed, headers
            except urllib.error.URLError as error:
                if cancel_event.is_set():
                    return 499, "cancelled", {}
                if isinstance(error.reason, ssl.SSLError):
                    raise TlsVerificationError(
                        redact_provider_error(
                            f"Provider {config.name!r}: TLS certificate verification failed: {error.reason}",
                            api_key,
                        )
                    ) from error
                if not _is_transient_url_error(error):
                    raise
                last_error = error
                time.sleep(0.05)
            except TimeoutError as error:
                last_error = error
                if cancel_event.is_set():
                    return 499, "cancelled", {}
        if cancel_event.is_set():
            return 499, "cancelled", {}
        raise TimeoutError(
            redact_provider_error(
                f"Request to provider {config.name!r} timed out after {config.request_timeout_seconds}s",
                api_key,
            )
        ) from last_error

    def _worker(self, handle: DirectApiHandle, record: TaskRecord, config: ProviderConfig, api_key: str, *, payload: dict) -> None:
        try:
            status, body, headers = self._post_chat_completions(config, api_key, payload, handle.cancel_event)
            response_doc = {"status": status, "headers": headers, "body": body}
            handle.response_artifact.write_text(json.dumps(response_doc, indent=2) + "\n")
            if handle.cancel_event.is_set():
                handle.cancelled = True
                handle.result = {"exit_code": 130, "summary": None, "error_category": "cancelled"}
                return
            if status == 429:
                handle.result = self._error_result(config, api_key, "rate_limit_or_quota_error", body, status)
                return
            if status >= 400 or status == 499:
                handle.result = self._error_result(config, api_key, _classify_http_error(status, body), body, status)
                return
            summary = _extract_summary(body)
            if summary is None:
                handle.result = self._error_result(config, api_key, "malformed_response", body, status)
                return
            effective_model = payload["model"]
            handle.result = {
                "exit_code": 0,
                "summary": summary,
                "http_status": status,
                "provider": config.name,
                "model": effective_model,
                "requested_model": record.model,
                "runtime_description": RUNTIME_DESCRIPTION,
                "capabilities_declared": _capabilities_payload(config),
                "limitations": _direct_api_limitations(),
                "verification": {"tests_broker_verified": False, "source": "runtime_reported"},
            }
        except MissingCredentialReference as error:
            handle.error = error
            handle.result = {
                "exit_code": 1,
                "summary": None,
                "error_category": "missing_credential_reference",
                "error_message": redact_provider_error(str(error)),
                "runtime_description": RUNTIME_DESCRIPTION,
            }
        except TlsVerificationError as error:
            handle.error = error
            handle.result = {
                "exit_code": 1,
                "summary": None,
                "error_category": "tls_verification_error",
                "error_message": redact_provider_error(str(error), api_key),
                "runtime_description": RUNTIME_DESCRIPTION,
            }
        except Exception as error:
            handle.error = error
            handle.result = {
                "exit_code": 1,
                "summary": None,
                "error_category": "runtime_error",
                "error_message": redact_provider_error(str(error), api_key),
                "runtime_description": RUNTIME_DESCRIPTION,
            }

    def _error_result(self, config: ProviderConfig, api_key: str, category: str, body: Any, status: int) -> dict[str, Any]:
        message = _error_message_from_body(body)
        return {
            "exit_code": 1,
            "summary": None,
            "error_category": category,
            "error_message": redact_provider_error(message or f"HTTP {status}", api_key),
            "http_status": status,
            "provider": config.name,
            "runtime_description": RUNTIME_DESCRIPTION,
            "capabilities_declared": _capabilities_payload(config),
            "limitations": _direct_api_limitations(),
            "verification": {"tests_broker_verified": False, "source": "runtime_reported"},
        }

    def start(self, record: TaskRecord, artifacts_dir: Path, *, prompt: str | None = None) -> tuple[dict, DirectApiHandle]:
        if record.provider is None:
            raise ProviderConfigError(f"Profile {DIRECT_API_PROFILE!r} requires a named provider")
        config = self.get_provider(record.provider)
        if record.execution_mode != "read_only":
            raise ProviderConfigError(
                f"Direct API runtime only supports execution_mode='read_only'; "
                f"got {record.execution_mode!r} (no honest worktree/tool support)"
            )
        if not config.capabilities.chat_completions:
            raise ProviderConfigError(f"Provider {config.name!r} does not declare chat_completions capability")
        effective_model = record.effective_model or config.default_model
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        handle = DirectApiHandle(
            task_id=record.id,
            provider_name=config.name,
            model=effective_model,
            request_artifact=artifacts_dir / "request_payload.json",
            response_artifact=artifacts_dir / "response.json",
        )
        effective_model = record.effective_model or config.default_model
        launch_prompt = prompt or record.task
        payload = {
            "model": effective_model,
            "messages": [{"role": "user", "content": launch_prompt}],
        }
        handle.request_artifact.write_text(
            json.dumps({"provider": config.name, "url": config.chat_completions_url(), "payload": payload}, indent=2) + "\n",
        )
        api_key = resolve_api_key(config, self.environ)

        def run() -> None:
            self._worker(handle, record, config, api_key, payload=payload)

        thread = threading.Thread(target=run, name=f"direct-api-{record.id}", daemon=True)
        handle.thread = thread
        thread.start()
        metadata = {
            "adapter": self.name,
            "runtime_description": RUNTIME_DESCRIPTION,
            "provider": config.name,
            "model": effective_model,
            "requested_model": record.model,
            "base_url": config.base_url,
            "events_artifact": handle.response_artifact.name,
            "stderr_artifact": None,
            "workspace": record.workspace,
            "limitations": _direct_api_limitations(),
        }
        return metadata, handle

    def collect(self, handle: DirectApiHandle, wait_timeout: float | None = None) -> dict[str, Any]:
        if handle.thread is not None:
            handle.thread.join(timeout=wait_timeout)
        if handle.result is None:
            if handle.thread is not None and handle.thread.is_alive():
                return {
                    "exit_code": 1,
                    "summary": None,
                    "error_category": "still_running",
                    "runtime_description": RUNTIME_DESCRIPTION,
                }
            return {
                "exit_code": 1,
                "summary": None,
                "error_category": "no_result",
                "runtime_description": RUNTIME_DESCRIPTION,
            }
        return handle.result

    def cancel(self, handle: DirectApiHandle, grace_period_seconds: float = 2.0) -> dict:
        handle.cancel_event.set()
        if handle.thread is not None:
            handle.thread.join(timeout=grace_period_seconds)
        terminated = handle.thread is None or not handle.thread.is_alive()
        return {
            "signals_sent": ["cancel_event"],
            "group_terminated": terminated,
            "exit_code": None,
            "note": "Direct API cancellation is cooperative HTTP abort, not process-group termination",
        }


def _extract_summary(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return redact_provider_error(content.strip())
    return None


def _error_message_from_body(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(body.get("message"), str):
            return body["message"]
    if isinstance(body, str):
        return body
    return ""


def _classify_http_error(status: int, body: Any) -> str:
    if status in (401, 403):
        return "authentication_error"
    if status == 429:
        return "rate_limit_or_quota_error"
    message = _error_message_from_body(body).lower()
    if any(token in message for token in ("401", "403", "unauthorized", "authentication")):
        return "authentication_error"
    if any(token in message for token in ("429", "rate limit", "quota")):
        return "rate_limit_or_quota_error"
    if status == 499:
        return "cancelled"
    return "runtime_error"


def _capabilities_payload(config: ProviderConfig) -> dict[str, bool]:
    caps = config.capabilities
    return {
        "chat_completions": caps.chat_completions,
        "structured_output": caps.structured_output,
        "streaming": caps.streaming,
        "tool_calls": caps.tool_calls,
        "workspace_access": caps.workspace_access,
        "process_cancellation": caps.process_cancellation,
    }


def _direct_api_limitations() -> list[str]:
    return [
        "No subprocess supervision or process-group cancellation",
        "No agent tool loop or repository/worktree mutation",
        "No live mid-task steering or session reattachment after broker restart",
        "Cancellation is cooperative HTTP abort only",
        "read_only execution_mode only",
    ]
