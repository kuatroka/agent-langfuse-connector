# Langfuse Token Accounting Fix — Factory Droid and Amp

Status: revision 2 — live-test findings incorporated
Date: 2026-10-20
Owner: coordination session `dv/deepseek-v4.1-flash-max`
Implementation: SWE-2-MAX via `droid exec`

## Revision 2 — live-test findings

The first implementation exposed two additional facts:

1. **Langfuse ignores `usage_details` on span observations.** A root span with
   usage is ingested with `totalTokens = 0`. The usage must be attached to a
   `GENERATION` observation. Attach it to the final generation of the turn,
   exactly once, and add `token_usage_scope: "turn"` metadata so the aggregate
   is not mistaken for that single call's usage.
2. **`droid exec` stores a per-turn `tokenUsage`.** In `droid exec` sessions the
   session settings contain `tags: [{"name": "exec"}]`, and `tokenUsage` is the
   current turn's usage, not a cumulative session counter. TUI sessions omit
   that tag and keep cumulative counters. For exec, use the current
   `tokenUsage` directly. For non-exec, keep the cumulative delta.

Revised Droid design:

- `read_token_usage` returns `(usage, is_exec)`; `is_exec` is true when the
  session settings `tags` contains an entry named `exec`.
- If `is_exec`: `turn_usage = current` (per-turn settings).
- Otherwise: `turn_usage = usage_delta(current, baseline)` (cumulative TUI
  settings) and update the state baseline as before.
- Attach `usage_details` only to the final generation (`index == len(assistants)`).
  The root span carries metadata but no usage.
- Set `token_usage_source` to `"exec-turn-settings"` for exec and
  `"session-cumulative-delta"` for non-exec.
- Keep the root/generation metadata `token_usage`, `token_usage_scope`,
  `token_usage_status`, and the multi-turn catch-up warning.

## Problem

The Langfuse session comparison showed Factory Droid at 1,035,298 total tokens
and Amp at 65,000 for the same one-turn prompt. Neither number is the native
turn total.

### Factory Droid

Session `bc8e766b-2ca0-4a66-8482-c2cd9d48c762`, trace
`177285398d640247f304dc4cc72508aa`:

- 32 observations: 1 root span, 20 tool spans, 11 generation spans.
- Every generation carries the same usage:
  `{input: 88531, output: 5587, total: 94118}`.
- Root span carries no usage.
- Langfuse total: `11 × 94,118 = 1,035,298`.

The hook computes one cumulative delta for the turn, then passes that same
`usage_details` to every generation in `emit_turn`. The turn total is correct
once (`94,118`), but it is counted 11 times.

### Amp

Session `T-01a15ac1-0a2e-715c-b71c-4c96ce1c38db`, trace
`4c7956583e82871664d93c259272dca9`:

- 1 generation with `output 5,827 + cache_creation 59,173 = 65,000`.
- Raw `amp threads export` has 9 assistant messages:
  `output 7,139 + cache_creation 62,663 = 69,802`.
- The final assistant message (`1,312` output + `3,490` cache creation =
  `4,802`) is missing from the trace.

`turnDetailsFromThreadExport` filters assistant messages to those that already
have `usage`, so a turn with 9 assistant messages and 8 usage records can look
"complete" if the expected count is also 8. The 20 × 250 ms poll loop then
returns `latest` even when `usageComplete` is false.

## Requirements

1. **One turn, one usage record.** A trace's total must equal the native turn
   total exactly once, regardless of how many model calls or tool spans the
   turn contains.
2. **Session totals.** Langfuse session total must equal the sum of the
   exported turn totals. Every turn must produce one trace, and each trace must
   contribute its turn total once.
3. **No silent partial usage.** An exporter must not attach an incomplete
   aggregate as if it were complete. Incomplete usage must be retried and, if
   still unavailable, explicitly marked.
4. **Preserve observability.** Keep the existing root span, generation spans,
   tool spans, prompts, outputs, metadata, tags, and model/effort attribution.
5. **No historical rewrite.** Do not modify existing Langfuse traces or
   retroactively repair old sessions.
6. **No unrelated changes.** Preserve existing uncommitted work in the Amp
   plugin repository. Do not commit or push.

## Factory Droid design

Files:

- Source:
  `/Users/yo_macbook/.factory/local-langfuse-marketplace/factory-langfuse/hooks/langfuse_hook.py`
- Installed active copy:
  `/Users/yo_macbook/.factory/plugins/cache/local-langfuse-marketplace-e48b909f24c5/factory-langfuse-9ea73ac95aa6/mvh342ye/hooks/langfuse_hook.py`

The two files are currently identical. Patch both with identical content.

### Changes

1. In `emit_turn`, pass the turn usage to the **root span**:
   `start_backdated(..., "span", ..., usage_details=usage)`.
2. Remove `usage_details=usage` from every generation
   `start_backdated(..., "generation", ...)` call.
3. Add metadata that makes the attribution explicit:
   - `token_usage_scope: "turn"`
   - `token_usage_source: "session-cumulative-delta"`
   - `token_usage_status: "complete"` when a delta exists.
   - `token_usage_status: "unavailable"` when no delta exists (for a turn
     exported in a multi-turn catch-up).
4. When `len(ready) > 1`, log a warning and mark every turn except the last as
   `token_usage_status: "unavailable"`. Do not invent a split for earlier
   turns. The normal Stop-per-turn path gives every turn its own delta.
5. Keep the existing `token_usage` metadata JSON on the root span.

### Resulting semantics

- One trace per user turn.
- Root span usage = that turn's cumulative delta.
- Generation and tool spans carry no usage.
- Session total = sum of root-span usages = sum of turn deltas.

## Amp design

File:

- `/Users/yo_macbook/.cache/amp/repositories/ampcode.com-user-plugins/langfuse.ts`

This repository has uncommitted changes in `langfuse.ts` and `README.md`.
Preserve them; apply the token fix on top. Do not push. If Amp's global plugin
cache does not pick up the working file, commit `langfuse.ts` locally and
refresh the cache from that file (the coordinator did this for the live test).

### Changes

1. In `turnDetailsFromThreadExport`, stop filtering out assistant messages
   without usage before deciding completeness. Build:
   - `assistantMessages`: every assistant message in the turn.
   - `usageMessages`: assistant messages with a `usage` object.
   - `usageComplete`: `assistantMessages.length >= expectedAssistantMessages`
     **and** `usageMessages.length === assistantMessages.length`.
2. Compute usage from `usageMessages` (the existing sum formula is correct for
   this provider: `input + output + cacheRead + cacheCreation`).
3. Replace the fixed 20 × 250 ms loop with a bounded configurable poll:
   - default timeout 30,000 ms,
   - poll interval 500 ms,
   - optional `AMP_LANGFUSE_USAGE_TIMEOUT_MS` override,
   - return as soon as `usageComplete` is true.
4. Never return partial usage as complete. If the deadline expires:
   - log `Amp usage incomplete: counted X of Y assistant messages`,
   - return the partial usage with `usageComplete: false` and counts,
   - add trace metadata `usage_status: "incomplete"`,
     `usage_counted_messages`, `usage_expected_messages`, and a
     `usage:incomplete` tag.
5. On a complete turn, add `usage_status: "complete"` metadata.
6. Preserve the existing one-generation-per-turn design; it is correct once the
   aggregate is complete. Session totals then equal the sum of turn traces.

### Resulting semantics

- One trace per Amp turn, same Amp thread/session ID.
- One generation per trace with the complete turn aggregate.
- Incomplete snapshots are explicitly marked and never silently presented as
  complete.

## Test plan

Use a temporary directory containing a handful of Markdown files. All prompts
must force at least one tool call; no files may be modified by the agent.

### Factory Droid (2 turns)

1. `droid exec -m custom:devin-swe2-max -r max --auto low --cwd <tmp> -o json "..."`.
2. Parse the session ID from the first result.
3. `droid exec -s <session-id> -m custom:devin-swe2-max -r max --auto low -o json "..."`.
4. Prompts: count Markdown files with a shell command; then list the three
   largest Markdown files by line count with a fresh shell command.
5. Sum the per-turn `usage` fields from the two `droid exec` JSON results.
   `droid exec` session settings are per-turn; do not expect the final
   `tokenUsage` to be cumulative. For a TUI session, use the cumulative
   `tokenUsage` from the session settings.
6. Langfuse assertions:
   - exactly 2 traces for the session,
   - each trace has exactly one usage-bearing observation (the final
     generation),
   - no other generation carries usage,
   - each trace has at least one tool span,
   - sum of trace totals equals the sum of the native per-turn usages (or the
     native cumulative TUI total).

### Amp (2 turns)

1. Create/continue a thread in the same temp directory with
   `amp threads continue <thread> --execute "<prompt>"` (or the equivalent
   new-thread execute form).
2. Second turn continues the same thread.
3. Prompts: same tool-forcing questions, phrased to require fresh shell calls.
4. `amp threads export <thread-id>` to compute native total.
5. Langfuse assertions:
   - exactly 2 traces for the thread/session,
   - each trace has one generation with complete usage,
   - each trace has at least one tool span,
   - sum of trace totals equals the native thread total,
   - no `usage:incomplete` tag or incomplete metadata.

### Acceptance

- No trace total is a multiple of a single turn total (the Droid 11× artifact).
- No usage-bearing observation is duplicated across generations.
- Session totals match native cumulative usage within exact integer equality.
- Incomplete Amp usage is flagged, never silently understated.

## Verification results (2026-10-20)

**Pre-fix control (Droid headless, 2-call turn).** Session
`2dc20ad4-e396-4066-a133-5b7f7747e8e9`: native turn usage 17,783; Langfuse
showed 35,566 across two generations (`2 × 17,783`). This is the duplication
artifact.

**Post-fix Droid, two turns with tools.** Session
`26484b56-01e3-43d0-a931-37d4517ab3c6`:

- Native per-turn totals: 13,757 and 28,443; native sum 42,200.
- Langfuse: 2 traces, 13,757 and 28,443; sum 42,200.
- Each trace has exactly one usage-bearing observation, the final generation
  (`Factory Generation 2`); the root span is 0; each trace has one tool span.
- Trace metadata: `token_usage_source=exec-turn-settings`,
  `token_usage_status=complete`.

**Post-fix Amp, two turns with tools.** Thread
`T-01a15af5-bf47-7221-b512-af9f3e406cb4`:

- Native thread total: 21,254.
- Langfuse: 2 traces, 16,385 and 4,869; sum 21,254.
- Each trace has one generation with usage and one tool span.
- Both traces report `usage_status=complete`; no `usage:incomplete` tags.

**Static checks.** Both Droid hook copies pass `py_compile` and are
byte-identical. The Amp plugin transpiles with `bun build`; `git diff --check`
is clean.

**Activation note.** The Amp user-plugin repository is committed and pushed as
`c389c1e` on `main`. Amp's global plugin cache was refreshed from that file so
the live test ran the new wait logic; `usage_status=complete` on the traces
confirms the new code is active. `README.md` remains uncommitted user work.

**TUI path.** The non-exec cumulative-delta path is unit-tested
(`read_token_usage` returns `is_exec=False`, `usage_delta` computes the turn
delta) and shares the same final-generation usage placement exercised by the
live exec test. It was not re-run as a live TUI session in this pass.

## Implementation constraints

- Use Python 3 syntax and keep the hook dependency-free beyond the existing
  Langfuse SDK.
- Keep TypeScript compatible with the Amp plugin runtime.
- Run `python3 -m py_compile` on both Droid hook copies.
- Run a TypeScript syntax/transpile check on the Amp plugin if the repo has
  tooling; otherwise report the available check.
- Report changed files, exact diffs, checks, and any remaining risk.

## Langfuse v4 API migration

Read verification now uses `GET /api/public/v2/observations` through
`tools/langfuse_v2.py`; the legacy trace, session, and v1 observation reads
were removed from the verification path. Trace ingestion remains OpenTelemetry
with `x-langfuse-ingestion-version: 4`. Droid and Claude use Python SDK 5.0.3,
which sets the header automatically. See `LANGFUSE-V4-MIGRATION.md` for details
and live checks.
