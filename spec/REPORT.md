# Unified coding-agent Langfuse integration report

Verified on 2026-09-13 against the Langfuse project `ai coding assistants` in the US region.

## Contract

Every certified trace has exactly one tag with each prefix `harness:`, `model:`, and `reasoning-effort:`. It has all six comparison metadata fields:

`harness`, `model`, `model_base`, `model_source`, `reasoning_effort`, and `reasoning_effort_source`.

Every certified trace has at least one `GENERATION` observation. Its Langfuse `model` equals the trace's `model_base`; its metadata repeats the same effort and effort source.

The complete contract is in `CONTRACT.md`.

## Certified live traces

| Harness | Model | Effort | Effort source | Trace |
| --- | --- | --- | --- | --- |
| Codex | `gpt-5.6-luna` | `low` | `codex-rollout` | `23c26c1f8a6c09c048da4c53c1df2017` |
| Amp | `gpt-6-astra` | `low` | `thread-export` | `16da4c255fef0c9cf25edefb42a1ce73` |
| Factory Droid | `gpt-6-astra` | `low` | `factory-transcript` | `c46e6d3b063cf78ff72bf21fbe32db24` |
| Claude Code | `claude-fable-5-1` | `low` | `claude-transcript` | `b6523884c02104bc89fba7250da8b793` |
| Pi | `gpt-5.6-luna` | `low` | `pi-runtime` | `7e28b3e7485a442d28c31b42a2f76998` |
| OMP | `glm-5-2` | `high` | `omp-runtime` | `24e4a4a2106ecbd67cc3e0f49524d0b6` |
| Devin | `gpt-5.6-luna` | `low` | `devin-model-id` | `f6988688ad866149edd4a85421cd9870` |
| Antigravity | `gemini-3.8-flash` | `high` | `antigravity-model-id` | `b3a4a2d3013f52e84bdbac76e4006802` |

The requested OMP `low` setting resolved to `high` for `devin/glm-5-2`; the trace records the applied runtime value.

The first Droid recheck used Factory's built-in `gpt-5.6-luna` route and failed before generation with HTTP 402 because no Factory subscription was active. The successful certification used the configured `custom:cx/gpt-6-astra-low` route and verified the resulting native transcript and Langfuse trace.

## Pi and OMP

Langfuse publishes an official experimental `@langfuse/pi-observability-plugin` (version 0.1.2 inspected). It traces Pi turns, model calls, tools, usage, cost, images, and subagents. Its current source does not expose the six-field comparison contract or Pi's selected thinking level in trace metadata.

This Mac already used `pi-langfuse` 1.5.15. A live Pi test showed that it captures model, effort, generations, tools, and session identity. It was retained and normalized instead of adding a second exporter and producing duplicate traces.

OMP explicitly accepts legacy `pi.extensions` manifests. `pi-langfuse` was copied into `omp-pi-langfuse`, configured with OMP-specific paths and harness identity, and linked into OMP. OMP 18.1.6 omits Pi's `agent_end` event in print mode; the compatibility code closes and flushes on a terminal `turn_end`. TypeScript checks pass and a live trace certifies the result.

## Devin

Devin CLI 3000.10.21 has an official global plugin and Stop-hook mechanism. The installed local `langfuse-unified` plugin reads the exact native session and main-chain turn from `~/.local/share/devin/cli/sessions.db`, including `generation_model` and per-response usage. It sends deterministic Langfuse trace IDs and preserves the raw model alongside the normalized comparison model.

## Antigravity

Antigravity 2.12.2's bundled customization documentation supports globally discovered plugins under `~/.gemini/config/plugins/` and Stop hooks carrying `conversationId`, `transcriptPath`, and `modelName`. The `langfuse-unified` global plugin and exporter are installed.

The parser and Langfuse exporter were first validated using an existing native Antigravity transcript. The final certification then used a new Antigravity desktop conversation and its automatically invoked global Stop hook. It produced:

- harness `antigravity`;
- model `gemini-3.8-flash(high)`;
- model source `antigravity-hook`;
- effort `high` with matching provenance;
- session `258ed7ff-97cd-464d-b35f-7558bbb4ff4e`;
- trace `b3a4a2d3013f52e84bdbac76e4006802`.

The desktop visibly returned the unique test marker. The hook log recorded the same native conversation ID and deterministic trace ID, and an authenticated Langfuse API readback verified the complete metadata, tag, session, and generation contract. A prior standalone headless attempt authenticated and started but could not create a project-backed conversation without the desktop host's project store; that process was stopped cleanly.

## Installed files

- `hooks/agent_langfuse_common.py`: shared stdlib OTLP exporter and model normalization.
- `hooks/export_devin.py`: exact Devin Stop-hook adapter.
- `devin-plugin/`: locally installed Devin plugin.
- `omp-pi-langfuse/`: OMP-compatible normalized Pi extension.
- `hooks/export_antigravity.py`: Antigravity transcript adapter.
- `~/.gemini/config/plugins/langfuse-unified/`: globally discovered Antigravity plugin.

Existing unrelated dirty files in the Amp and Codex source repositories were preserved.
