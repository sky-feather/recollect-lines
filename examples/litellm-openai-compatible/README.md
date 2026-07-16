# Local LiteLLM / OpenAI-compatible provider example

Routes `openai_compatible` profile tasks through a loopback gateway (e.g.
[LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy)) listening on
`127.0.0.1:4000`.

## TLS / HTTP policy

- `http://127.0.0.1` is permitted **only** with `"allow_insecure_http": true`.
- Remote endpoints must use `https://` with valid TLS (`tls_verify` defaults to true).

## Validate (offline)

```bash
recollect-lines --home ~/.recollect \
  --providers-config examples/litellm-openai-compatible/providers.json \
  doctor --json
```

## Expected doctor outcome

- `PROVIDERS_CONFIG_VALID` — ok (syntax and policy)
- `PROVIDER_SECRET_REFERENCE_MISSING` — **warning** for `LITELLM_MASTER_KEY` until you export it
- `ENDPOINT_CONNECTIVITY_NOT_CHECKED` — not checked (Phase 7A does not probe the network)

Export the placeholder reference before live tasks:

```bash
export LITELLM_MASTER_KEY='sk-placeholder-replace-me'
recollect-lines --home ~/.recollect \
  --providers-config examples/litellm-openai-compatible/providers.json \
  doctor --json
```

After export, `PROVIDER_SECRET_REFERENCE_PRESENT` should appear for `local_litellm`.

## Certification dry-run (Phase 7B)

Offline evidence only — no network probe:

```bash
recollect-lines --home ~/.recollect \
  --providers-config examples/litellm-openai-compatible/providers.json \
  certify --profile openai_compatible --provider local_litellm --json \
  --output /tmp/litellm-certify-evidence.json
```

`estimated_cost_usd_upper_bound` is declared for future live opt-in; dry-run does not call the gateway.
See [`docs/history/phases/phase-7b.md`](../../docs/history/phases/phase-7b.md) for live and fixture modes.
