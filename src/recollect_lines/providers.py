"""Named OpenAI-compatible provider configuration (Phase 6C).

Provider entries describe model endpoints — base URL, credential references,
models, timeouts, declared capabilities, and TLS policy. They are not runtime
adapters and never grant subprocess supervision, worktree access, or process-
group cancellation by themselves.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from .cursor_adapter import redact_secrets

PROVIDER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
OPENAI_COMPATIBLE_KIND = "openai-compatible"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Strict schema surface (Wave 2 / PR 5): unknown keys are rejected rather than
# silently ignored, so a misspelled field or a misplaced literal secret is
# caught at load time instead of silently doing nothing.
ALLOWED_TOP_LEVEL_KEYS = frozenset({"providers"})
ALLOWED_PROVIDER_ENTRY_KEYS = frozenset({
    "kind", "base_url", "api_key_env", "default_model",
    "request_timeout_seconds", "tls_verify", "allow_insecure_http",
    "ca_bundle", "capabilities", "estimated_cost_usd_upper_bound",
})
# Key names that suggest an operator pasted a literal credential/secret value
# into the document instead of referencing it via api_key_env. These get a
# dedicated, more actionable error than a generic "unknown key".
SECRET_LIKE_KEY_HINTS = frozenset({
    "api_key", "apikey", "api_secret", "secret", "secrets", "token", "tokens",
    "password", "passwd", "auth", "authorization", "bearer", "bearer_token",
    "credential", "credentials", "access_token", "client_secret", "private_key",
})
_CA_BUNDLE_INLINE_MARKERS = ("BEGIN CERTIFICATE", "BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "BEGIN EC PRIVATE KEY")

# Configuration-resolution precedence (Wave 2 / PR 4). Highest first: an
# explicit --providers-config/constructor argument always wins; RECOLLECT_CONFIG
# is the next-highest "configured" source. Both are configured sources -- if
# either points at a missing/invalid file, that failure is reported with its
# own path rather than silently falling through to a lower-precedence source.
# The remaining tiers are discovery-based (skipped, not failed, when absent).
CONFIG_PATH_ENV_VAR = "RECOLLECT_CONFIG"
OPERATOR_CONFIG_DIRNAME = ".recollect"
OPERATOR_CONFIG_BASENAMES = ("config.yaml", "config.yml", "config.json")
LEGACY_DEFAULT_CONFIG_NAME = "providers.json"

ConfigSourceOrigin = Literal["explicit", "env", "repo_local", "user_level", "legacy_default", "not_configured"]


@dataclass(frozen=True)
class ResolvedProviderConfigSource:
    path: Path | None
    origin: ConfigSourceOrigin


def _first_existing_operator_config(directory: Path) -> Path | None:
    for name in OPERATOR_CONFIG_BASENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def resolve_providers_config_source(
    *,
    explicit: Path | None,
    environ: dict[str, str],
    repo_root: Path,
    user_home: Path,
) -> ResolvedProviderConfigSource:
    """Resolve which provider configuration file governs this process.

    Precedence, highest to lowest: explicit argument, `RECOLLECT_CONFIG` env
    var, repo-local operator config (`<repo_root>/.recollect/config.{yaml,yml,json}`),
    user-level operator config (`<user_home>/.recollect/config.{yaml,yml,json}`),
    then legacy default discovery (`<repo_root>/providers.json`). Does not
    read or validate file contents -- callers load the resolved path and let
    a missing/malformed configured (explicit or env) source fail with its own
    path rather than silently trying the next tier.
    """
    if explicit is not None:
        return ResolvedProviderConfigSource(explicit, "explicit")
    env_value = environ.get(CONFIG_PATH_ENV_VAR)
    if env_value:
        return ResolvedProviderConfigSource(Path(env_value), "env")
    repo_local = _first_existing_operator_config(repo_root / OPERATOR_CONFIG_DIRNAME)
    if repo_local is not None:
        return ResolvedProviderConfigSource(repo_local, "repo_local")
    user_level = _first_existing_operator_config(user_home / OPERATOR_CONFIG_DIRNAME)
    if user_level is not None:
        return ResolvedProviderConfigSource(user_level, "user_level")
    legacy = repo_root / LEGACY_DEFAULT_CONFIG_NAME
    if legacy.is_file():
        return ResolvedProviderConfigSource(legacy, "legacy_default")
    return ResolvedProviderConfigSource(None, "not_configured")

# Declarable provider capabilities — validation data only, not routing (Phase 6D).
ALLOWED_CAPABILITY_KEYS = frozenset({
    "chat_completions",
    "structured_output",
    "streaming",
    "tool_calls",
    "workspace_access",
    "process_cancellation",
})


@dataclass(frozen=True)
class ProviderCapabilities:
    chat_completions: bool = True
    structured_output: bool = False
    streaming: bool = False
    tool_calls: bool = False
    workspace_access: bool = False
    process_cancellation: bool = False

    @classmethod
    def from_mapping(cls, raw: Any) -> "ProviderCapabilities":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("capabilities must be an object")
        unknown = set(raw) - ALLOWED_CAPABILITY_KEYS
        if unknown:
            raise ValueError(f"Unknown capability keys: {', '.join(sorted(unknown))}")
        values: dict[str, bool] = {}
        for key in ALLOWED_CAPABILITY_KEYS:
            if key not in raw:
                continue
            value = raw[key]
            if not isinstance(value, bool):
                raise ValueError(f"capabilities.{key} must be a boolean")
            values[key] = value
        if values.get("chat_completions") is False:
            raise ValueError("capabilities.chat_completions must be true for openai-compatible providers")
        return cls(**values)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    base_url: str
    api_key_env: str
    default_model: str
    request_timeout_seconds: int
    tls_verify: bool
    allow_insecure_http: bool
    ca_bundle: str | None
    capabilities: ProviderCapabilities
    estimated_cost_usd_upper_bound: float | None = None

    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


class ProviderConfigError(ValueError):
    pass


class MissingCredentialReference(ProviderConfigError):
    pass


def _validate_provider_name(name: str) -> None:
    if not isinstance(name, str) or not PROVIDER_NAME_PATTERN.match(name):
        raise ProviderConfigError(
            f"Invalid provider name {name!r}: must match {PROVIDER_NAME_PATTERN.pattern}"
        )


def _validate_base_url(base_url: str, *, allow_insecure_http: bool) -> None:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ProviderConfigError("base_url must be a non-empty string")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ProviderConfigError(f"base_url must use http or https scheme, got {parsed.scheme!r}")
    if not parsed.netloc:
        raise ProviderConfigError("base_url must include a host")
    if parsed.scheme == "http":
        host = parsed.hostname or ""
        if host not in LOOPBACK_HOSTS:
            raise ProviderConfigError(
                "http base_url is only permitted for loopback hosts (127.0.0.1, localhost, ::1); "
                "use https for remote endpoints"
            )
        if not allow_insecure_http:
            raise ProviderConfigError(
                "http base_url requires allow_insecure_http: true (explicit opt-in for loopback only)"
            )


def _parse_provider_entry(name: str, raw: Any) -> ProviderConfig:
    _validate_provider_name(name)
    if not isinstance(raw, dict):
        raise ProviderConfigError(f"Provider {name!r} must be an object")
    unknown_keys = set(raw) - ALLOWED_PROVIDER_ENTRY_KEYS
    if unknown_keys:
        secret_like = sorted(k for k in unknown_keys if k.lower() in SECRET_LIKE_KEY_HINTS)
        if secret_like:
            raise ProviderConfigError(
                f"Provider {name!r}: field(s) {', '.join(secret_like)} look like a literal "
                "credential/secret value; provider entries must reference credentials via "
                "api_key_env (the name of an environment variable), never an inline secret"
            )
        raise ProviderConfigError(
            f"Provider {name!r}: unknown key(s) {', '.join(sorted(unknown_keys))}"
        )
    kind = raw.get("kind")
    if kind != OPENAI_COMPATIBLE_KIND:
        raise ProviderConfigError(f"Provider {name!r}: kind must be {OPENAI_COMPATIBLE_KIND!r}")
    base_url = raw.get("base_url")
    api_key_env = raw.get("api_key_env")
    default_model = raw.get("default_model")
    if not isinstance(api_key_env, str) or not api_key_env.strip():
        raise ProviderConfigError(f"Provider {name!r}: api_key_env must be a non-empty string")
    if not re.match(r"^[A-Z][A-Z0-9_]{0,126}$", api_key_env):
        raise ProviderConfigError(
            f"Provider {name!r}: api_key_env must be an uppercase environment-variable name"
        )
    if not isinstance(default_model, str) or not default_model.strip():
        raise ProviderConfigError(f"Provider {name!r}: default_model must be a non-empty string")
    timeout = raw.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise ProviderConfigError(f"Provider {name!r}: request_timeout_seconds must be a positive integer")
    tls_verify = raw.get("tls_verify", True)
    if not isinstance(tls_verify, bool):
        raise ProviderConfigError(f"Provider {name!r}: tls_verify must be a boolean")
    allow_insecure_http = raw.get("allow_insecure_http", False)
    if not isinstance(allow_insecure_http, bool):
        raise ProviderConfigError(f"Provider {name!r}: allow_insecure_http must be a boolean")
    ca_bundle = raw.get("ca_bundle")
    if ca_bundle is not None:
        if not isinstance(ca_bundle, str) or not ca_bundle.strip():
            raise ProviderConfigError(f"Provider {name!r}: ca_bundle must be a non-empty string when set")
        if any(marker in ca_bundle for marker in _CA_BUNDLE_INLINE_MARKERS):
            raise ProviderConfigError(
                f"Provider {name!r}: ca_bundle must be a filesystem path to a CA bundle file, "
                "not inline certificate/key content"
            )
    _validate_base_url(base_url, allow_insecure_http=allow_insecure_http)
    capabilities = ProviderCapabilities.from_mapping(raw.get("capabilities"))
    estimated_cost = raw.get("estimated_cost_usd_upper_bound")
    if estimated_cost is not None:
        if not isinstance(estimated_cost, (int, float)) or isinstance(estimated_cost, bool) or estimated_cost <= 0:
            raise ProviderConfigError(
                f"Provider {name!r}: estimated_cost_usd_upper_bound must be a positive number when set"
            )
        estimated_cost = float(estimated_cost)
    return ProviderConfig(
        name=name,
        kind=kind,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        default_model=default_model.strip(),
        request_timeout_seconds=timeout,
        tls_verify=tls_verify,
        allow_insecure_http=allow_insecure_http,
        ca_bundle=ca_bundle.strip() if isinstance(ca_bundle, str) else None,
        capabilities=capabilities,
        estimated_cost_usd_upper_bound=estimated_cost,
    )


def validate_providers_document(data: Any) -> dict[str, ProviderConfig]:
    if not isinstance(data, dict):
        raise ProviderConfigError("Provider configuration must be a top-level object")
    unknown_top_level = set(data) - ALLOWED_TOP_LEVEL_KEYS
    if unknown_top_level:
        raise ProviderConfigError(
            f"Unknown top-level key(s) {', '.join(sorted(unknown_top_level))}; "
            "only 'providers' is supported"
        )
    providers_raw = data.get("providers")
    if not isinstance(providers_raw, dict) or not providers_raw:
        raise ProviderConfigError("'providers' must be a non-empty object")
    providers: dict[str, ProviderConfig] = {}
    for name, entry in providers_raw.items():
        if name in providers:
            raise ProviderConfigError(f"Duplicate provider name: {name!r}")
        providers[name] = _parse_provider_entry(name, entry)
    return providers


def _sniff_config_format(path: Path, raw_text: str) -> Literal["json", "yaml"]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in (".yaml", ".yml"):
        return "yaml"
    stripped = raw_text.lstrip()
    return "json" if stripped.startswith(("{", "[")) else "yaml"


def provider_config_format(path: Path) -> Literal["json", "yaml"]:
    """Detect a provider configuration file's format without validating its schema."""
    try:
        raw_text = path.read_text()
    except OSError as error:
        raise ProviderConfigError(f"Cannot read provider configuration {path}: {error}") from error
    return _sniff_config_format(path, raw_text)


def _parse_yaml_document(path: Path, raw_text: str) -> Any:
    try:
        import yaml
    except ImportError as error:
        raise ProviderConfigError(
            f"Provider configuration {path} is YAML but the 'pyyaml' package is not installed; "
            "install pyyaml, or provide a JSON provider configuration instead."
        ) from error
    try:
        # yaml.safe_load only: no arbitrary Python object construction, tags,
        # or code execution -- unlike yaml.load with a non-Safe loader.
        return yaml.safe_load(raw_text)
    except yaml.YAMLError as error:
        raise ProviderConfigError(
            redact_provider_error(f"Provider configuration {path} is not valid YAML: {error}")
        ) from error


def load_providers_config(path: Path) -> dict[str, ProviderConfig]:
    try:
        raw_text = path.read_text()
    except OSError as error:
        raise ProviderConfigError(f"Cannot read provider configuration {path}: {error}") from error
    fmt = _sniff_config_format(path, raw_text)
    if fmt == "json":
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as error:
            raise ProviderConfigError(
                redact_provider_error(f"Provider configuration {path} is not valid JSON: {error}")
            ) from error
    else:
        data = _parse_yaml_document(path, raw_text)
        if data is None:
            raise ProviderConfigError(f"Provider configuration {path} is empty")
    try:
        return validate_providers_document(data)
    except ProviderConfigError as error:
        raise ProviderConfigError(f"Provider configuration {path}: {error}") from error


def resolve_api_key(config: ProviderConfig, environ: dict[str, str] | None = None) -> str:
    """Resolve a provider's credential from an environment-variable reference.

    Fail closed when the reference is missing, empty, or malformed.
    """
    env = environ if environ is not None else os.environ
    value = env.get(config.api_key_env)
    if value is None:
        raise MissingCredentialReference(
            f"Credential reference {config.api_key_env!r} is not set in the environment"
        )
    if not isinstance(value, str) or not value.strip():
        raise MissingCredentialReference(
            f"Credential reference {config.api_key_env!r} is set but empty"
        )
    return value.strip()


def redact_provider_error(message: str, secret: str | None = None) -> str:
    redacted = redact_secrets(message)
    if secret:
        redacted = redacted.replace(secret, "***REDACTED***")
    return redacted


# A minimal, immediately-valid starter config: loopback-only, a placeholder
# credential-reference name, no real secret. Written non-interactively by
# `recollect-lines config init`; see config/providers.example.yaml for a
# fuller annotated reference.
STARTER_CONFIG_YAML = """\
# Local provider configuration written by `recollect-lines config init`.
# Safe to edit. Never put a real API key/token/secret in this file --
# api_key_env names an environment variable to read the credential from.
# Full schema: config/providers.example.yaml, config/providers.schema.json,
# docs/cli.md. CA bundle guidance (incl. macOS): docs/getting-started.md.
providers:
  local:
    kind: openai-compatible
    base_url: http://127.0.0.1:8000/v1
    api_key_env: LOCAL_PROVIDER_API_KEY
    default_model: local-model
    allow_insecure_http: true
"""


def write_atomic_text(path: Path, text: str, *, mode: int = 0o600) -> Path:
    """Write `text` to `path` atomically (temp file + rename) at the given mode.

    Shared primitive for every command that mutates a provider config file --
    a reader never observes a partially written document, and a crash between
    write and rename leaves the original file (or nothing) rather than a
    truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    finally:
        if tmp_path.exists() and not path.exists():
            tmp_path.unlink(missing_ok=True)
    return path


def existing_file_mode(path: Path, *, default: int = 0o600) -> int:
    """The current owner/group/other permission bits of `path`, or `default` if absent."""
    if not path.exists():
        return default
    return stat.S_IMODE(path.stat().st_mode)


def write_local_config_file(path: Path, *, force: bool = False, content: str | None = None) -> Path:
    """Write a minimal, safe starter provider configuration.

    Non-interactive (no prompts) and contains no real credentials -- only a
    placeholder `api_key_env` name. Owner-private (mode 0600) on POSIX;
    written atomically via a temp file + rename in the destination directory.
    """
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass force=True (--force) to overwrite")
    text = content if content is not None else STARTER_CONFIG_YAML
    return write_atomic_text(path, text, mode=0o600)


def render_providers_document(providers_raw: dict[str, dict[str, Any]], fmt: Literal["json", "yaml"]) -> str:
    """Render a raw (unvalidated-shape) `{name: entry}` mapping back to config file text."""
    document = {"providers": providers_raw}
    if fmt == "json":
        return json.dumps(document, indent=2, sort_keys=True) + "\n"
    import yaml
    return yaml.safe_dump(document, sort_keys=True, default_flow_style=False)
