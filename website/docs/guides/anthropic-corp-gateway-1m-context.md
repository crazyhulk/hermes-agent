---
sidebar_position: 18
title: "Self-hosted Anthropic gateways: 1M context"
description: "Enable Claude's 1M-token context window on corporate / self-hosted Anthropic-Messages gateways via the HERMES_ANTHROPIC_CONTEXT_1M env switch."
---

# Self-hosted Anthropic gateways: 1M context

Hermes Agent talks the Anthropic Messages protocol to a number of endpoints
beyond `api.anthropic.com` — AWS Bedrock, Microsoft Foundry, MiniMax, Kimi
`/coding`, DeepSeek `/anthropic`, and arbitrary self-hosted / corporate
gateways that front Claude with their own auth scheme. Most of those paths
opt in or out of Anthropic's `context-1m-2025-08-07` beta on a per-host
basis, but **arbitrary corporate gateways are not on the host allow-list**,
so by default they only get the 200K-token short-context behaviour.

The `HERMES_ANTHROPIC_CONTEXT_1M` env switch lets you opt those gateways in
without per-host wiring.

## When to use this

You need this if your gateway:

- Speaks the Anthropic Messages protocol at `POST /v1/messages`
- Authenticates via `x-api-key` or `Authorization: Bearer` with a non-Anthropic key
- Gates 1M context behind the standard `anthropic-beta: context-1m-2025-08-07`
  header (the same beta Anthropic's own API uses)
- Is **not** Anthropic, AWS Bedrock, Microsoft Foundry, or MiniMax — those
  paths are already auto-detected

A typical example is a corporate LLM proxy that fronts Claude behind a
non-Anthropic key prefix (`bsk-…`, `corp-…`, etc.) and re-exposes the
Anthropic Messages route. Confirm with your gateway operator that the
underlying Claude entitlement includes 1M context for the model you plan
to use (Opus 4.6/4.7 and Sonnet 4.6 support 1M; older Claude tiers do not).

## Quick start

```bash
export HERMES_ANTHROPIC_CONTEXT_1M=1
export ANTHROPIC_BASE_URL="https://llmapi.your-corp.example"
export ANTHROPIC_AUTH_TOKEN="<your-corp-api-key>"
hermes
```

Truthy values: `1`, `true`, `yes`, `on` (case-insensitive). Anything else
— including unset — leaves the gateway on the 200K default.

Once set, every Anthropic-client build attaches
`anthropic-beta: context-1m-2025-08-07` to the wire. Combined with a model
slug whose `context_length` resolves to 1M (see below), Hermes' context
compressor will use the full 1M window before deciding to compact.

## Why this is opt-in instead of always-on

Anthropic's own subscriptions reject `context-1m-2025-08-07` for accounts
that don't have the long-context beta enabled — every Messages call,
including short auxiliary ones like title generation, returns HTTP 400
"long context beta is not yet available for this subscription". Hermes
therefore only auto-attaches the beta on hosts that are known to accept
or require it (Bedrock, Microsoft Foundry, the upcoming GA path).
Corporate gateways are too varied to allow-list by hostname, so they
opt in by env.

The reactive recovery path in `run_agent.py` still wins: if the upstream
returns a 400 telling Hermes to drop the beta, it will rebuild the client
with `drop_context_1m_beta=True` and retry, regardless of the env switch.
This means the env var is safe to leave on globally — a gateway that
later loses the entitlement degrades gracefully.

## Model name and context_length resolution

Hermes ships defaults for the canonical Anthropic model slugs (`claude-sonnet-4-6`,
`claude-opus-4-7`, etc.) and for a few common corporate-gateway variants
that name the model with the version before the family
(`claude-4.6-sonnet` / `claude-4.6-opus` / `claude-4.7-opus` —
e.g. bilibili's internal `llmapi` gateway).

If your gateway exposes a model under a slug that isn't in
`agent/model_metadata.py::DEFAULT_CONTEXT_LENGTHS`, the lookup falls back
to the `claude` catch-all at 200K and the agent will compact early even
when the gateway accepts 1M-context requests. Two ways to fix this:

1. **One-off override** — set the model's context length explicitly via
   the per-model `context_length` field in your config (see the model
   metadata docs).
2. **Permanent fix** — open a PR adding the slug to
   `DEFAULT_CONTEXT_LENGTHS`. The dict matches by substring so adding
   both the dot-form and dash-form (`claude-4.6-sonnet` and
   `claude-4-6-sonnet`) covers callers that bypass `normalize_model_name`.

## Verifying the gateway accepts 1M

Before turning the env switch on for real workloads, sanity-check the
gateway with a payload that exceeds 200K tokens. Anthropic's tokeniser
treats 4 chars/token as a rough lower bound, so a ~5MB English text body
exceeds the 200K threshold:

```bash
# 1. Without the beta header — gateway should reject the size
curl -sS -X POST "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "x-api-key: $ANTHROPIC_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @huge_payload.json
# → {"error":{"type":"invalid_request_error","message":"Input is too long."}}

# 2. With the beta header — gateway should accept the size
curl -sS -X POST "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "x-api-key: $ANTHROPIC_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -H "anthropic-beta: context-1m-2025-08-07" \
  --data-binary @huge_payload.json
# → 200 OK with usage.input_tokens > 200_000
```

If step 2 also returns "Input is too long", the gateway gates 1M behind
something other than the standard Anthropic beta header. Open an issue
with the gateway operator first, then with Hermes if it turns out to be
a host family Hermes should learn.

## Interaction with other beta headers

`HERMES_ANTHROPIC_CONTEXT_1M` only flips `context-1m-2025-08-07`. The
other betas Hermes attaches by default (`interleaved-thinking-2025-05-14`,
`fine-grained-tool-streaming-2025-05-14`) are unaffected — their gating
is host-family-specific and independent of the long-context decision.

The fast-mode beta (`fast-mode-2026-02-01`, only for native Anthropic
Opus 4.6) is also unaffected: the env switch never adds it because
fast-mode is gated explicitly on the model name.

## See also

- `agent/anthropic_adapter.py::_force_context_1m_beta_via_env` — the
  implementation hook the env switch flips.
- `agent/anthropic_adapter.py::_common_betas_for_base_url` — the central
  beta-header builder. The env switch composes with `drop_context_1m_beta`
  so reactive recovery after a 400 still works.
- `tests/agent/test_anthropic_adapter.py` — `test_env_switch_*` cases
  pin down truthy/falsy parsing and the drop-beta interaction.
- [Microsoft Foundry guide](./azure-foundry.md) — the canonical example
  of a host-family that Hermes auto-opts in (Foundry's Anthropic-style
  endpoint always gets 1M).
