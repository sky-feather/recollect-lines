# Mixed CLI + named provider inventory example

Combines subprocess runtime adapters (selected per task via `--profile`) with a
named `openai_compatible` provider from `--providers-config`.

## Validate workspace + providers

```bash
recollect-lines --home ~/.recollect \
  --providers-config examples/mixed-cli-and-providers/providers.json \
  --workspace /path/to/repo \
  doctor
```

## Routing discovery (no winner selection)

```bash
recollect-lines --home ~/.recollect \
  --providers-config examples/mixed-cli-and-providers/providers.json \
  discover
recollect-lines --home ~/.recollect \
  --providers-config examples/mixed-cli-and-providers/providers.json \
  select --mode read_only --allowed-runtime mock --allowed-provider docs_gateway
```

Council selection remains parent-directed; the broker never autonomously picks a winner.

## Expected doctor outcome

- Missing `DOCS_EXAMPLE_API_KEY` → `PROVIDER_SECRET_REFERENCE_MISSING` (warning)
- Uninstalled CLI adapters → `RUNTIME_CLI_UNAVAILABLE` (warning)
- `https://api.example.invalid` passes **local** TLS/HTTP policy validation; reachability is **not** checked
