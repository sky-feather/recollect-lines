# CLI-only deployment example

Use this layout when you only need subprocess runtime adapters (mock, OpenCode,
Claude Code, Codex, Cursor) and **no** named direct-API providers.

## Quick start

```bash
pip install .
recollect-lines --home ~/.recollect doctor
recollect-lines --home ~/.recollect create \
  --task 'Investigate a flaky test' \
  --workspace /path/to/repo \
  --profile mock
```

## Expected doctor outcome

- `HOME_WRITABLE` — ok
- `PROVIDERS_CONFIG_NOT_SPECIFIED` — not checked (expected)
- `RUNTIME_CLI_UNAVAILABLE` — warning for CLIs not installed on PATH (expected on a fresh host)
- `ENDPOINT_CONNECTIVITY_NOT_CHECKED` — not checked (no providers config)

Warnings are normal on a minimal install. Blocking findings mean fix permissions or paths before delegating work.
