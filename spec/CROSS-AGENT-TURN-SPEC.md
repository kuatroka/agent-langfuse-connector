# Cross-Agent Turn Naming and Token Accounting

Status: implementation spec
Date: 2026-10-20
Coordinator: `dv/deepseek-v4.1-flash-max`
Implementers: SWE-2-MAX headless sessions

## Goal

Make every Langfuse trace represent one user turn, with a consistent numbered
trace name and `turn_number` metadata, and make turn/session token totals exact.

Target naming:

- `Factory Droid - Turn N` (already implemented)
- `Amp - Turn N`
- `Codex - Turn N`
- `Devin - Turn N`
- `Claude Code - Turn N` (already implemented)
- `Pi - Turn N`
- `OMP - Turn N`

`turn_number` must be an integer metadata field on the trace. Numbering is
per user-authored prompt in the session, 1-based.

## Audit findings

| Harness | Current trace name | Granularity | Token status |
| --- | --- | --- | --- |
| Factory Droid | `Factory Droid - Turn N` | one per user turn | Fixed and verified |
| Amp | `Amp turn` | one per user prompt | Fixed and verified; needs number |
| Codex | `Codex Turn` | one per turn | Exact on usage-bearing traces; needs number; empty traces exist |
| Devin | `devin Turn` | one per turn | **Broken**: only last assistant metrics; needs sum and number |
| Claude Code | `Claude Code - Turn N` | one per turn | Exact on 6 audited turns; already numbered |
| Pi | `pi-agent` | one per agent run (user prompt) | Formula to validate; needs number/tag |
| OMP | `omp-agent` | one per agent run (user prompt) | Formula to validate; needs number |

Evidence:

- Codex native turn 49,346,380 matched Langfuse 49,346,380; native 308,603
  matched 308,603. Empty `Codex Turn` traces with `model:unknown` have zero
  generations and zero tokens.
- Claude native turns 1-6 matched Langfuse totals exactly; names already
  `Claude Code - Turn N`.
- Devin session `miniature-innovation`: last assistant 81,746 tokens, but the
  sum over 35 assistant messages is 2,276,353. The current trace exports only
  81,746.
- Amp/Droid were fixed in the previous pass.

## Requirements

1. One trace per user prompt. Do not add a second trace for the same prompt.
2. Trace name `<Display> - Turn N`; metadata `turn_number` as an integer.
3. Token total per trace equals the native turn total exactly once.
4. Session total equals the sum of turn traces.
5. Preserve existing tags, model/effort metadata, tool spans, and session IDs.
6. No historical trace rewrite. No commit or push from implementers; the
   coordinator reviews and pushes.

## Changes

### Amp

File: `/Users/yo_macbook/.cache/amp/repositories/ampcode.com-user-plugins/langfuse.ts`

- Add `turnNumber?: number` to `ExportedTurnDetails`.
- In `turnDetailsFromThreadExport`, count user-authored prompts up to and
  including the current prompt message (`startIndex`). A user prompt is a
  `role: "user"` message with a text block and no `tool_result` block.
  Set `latest.turnNumber`.
- Pass `turnNumber` to `makeTurnTrace`.
- Trace name: `Amp - Turn ${turnNumber}` when known, else `Amp turn`.
- Add `langfuse.trace.metadata.turn_number` and keep it on child spans.
- Keep the complete-usage polling and `usage_status` behavior.

After coordinator review, commit and push this file.

### Codex

Files:

- Installed:
  `/Users/yo_macbook/.codex/plugins/cache/personal-langfuse/tracing-normalized/1.0.0/src/trace.ts`
- Installed bundled hook:
  `/Users/yo_macbook/.codex/plugins/cache/personal-langfuse/tracing-normalized/1.0.0/dist/index.mjs`
- Build source:
  `/Users/yo_macbook/.codex/.tmp/marketplaces/codex-observability-plugin/plugins/tracing`

- Add a `turnNumber` argument to `emitTurn`.
- Main thread trace name: `Codex - Turn N`.
- Subagent thread trace name: `Codex Subagent - Turn N`.
- Add `turn_number` to trace metadata.
- Skip turns with no steps and no user input/final output. These are the empty
  `model:unknown` traces. Keep the native turn index for seeded trace IDs; use
  a separate emitted-turn counter for display numbering.
- The usage formula is already exact; do not change it unless a test proves a
  mismatch.
- Rebuild `dist/index.mjs` from the personalized `src` using the upstream
  build workspace. If a full install/build is impossible, patch the bundle
  with the equivalent logic and run `node --check`.
- Do not push the public upstream repository.

### Devin

Files:

- `/Users/yo_macbook/Documents/dev/agent-langfuse-connector/packages/python/agent_langfuse_connector/common.py`
- `/Users/yo_macbook/Documents/dev/agent-langfuse-connector/packages/python/hooks/devin/export_devin.py`

- Add `turn_number: int | None` to `emit_turn`; use trace name
  `Devin - Turn N` and metadata `turn_number`.
- In `export_devin.py`, sum `metrics` across **all** assistant messages after
  the current user prompt, not just the last assistant:
  - `input_tokens`, `output_tokens`, `cache_read_tokens`,
    `cache_creation_tokens`; missing/None values count as 0.
  - `usage.total` = the sum of all four.
  - Keep the last assistant for output and model metadata.
- Set `turn_number = len(user_indexes)` for the latest user prompt.
- Pass `turn_number` to `emit_turn`.
- No tool-span changes.

### Pi

Files (installed package, edit in place):

- `/Users/yo_macbook/.pi/agent/npm/node_modules/pi-langfuse/index.ts`
- `/Users/yo_macbook/.pi/agent/npm/node_modules/pi-langfuse/src/state.ts`
- `/Users/yo_macbook/.pi/agent/npm/node_modules/pi-langfuse/src/handlers/agent.ts`

- Add `promptCount` to `SessionRunState`; initialize it from
  `ctx.sessionManager.getEntries()` at session start by counting user-authored
  prompt entries (exclude tool results if the entry shape allows it).
- Increment once per `startAgentRun` (one user prompt). Do not reset it in
  `finishAgentRun`/`resetRunState`.
- Root trace name: `Pi - Turn N`; add `turn_number` metadata.
- Add `harness:pi` to the root trace tags while preserving existing tags.
- Keep the root observation name as `pi-agent` if changing it is risky.
- Do not change usage extraction unless a test proves a mismatch.

### OMP

Files:

- Installed package:
  `/Users/yo_macbook/.omp/plugins/node_modules/pi-langfuse/index.ts`,
  `src/state.ts`, `src/handlers/agent.ts`
- Source fork:
  `/Users/yo_macbook/Documents/dev/agent-langfuse-connector/packages/typescript/omp/`
  (copy the final installed changes there so source and installed match)

- Same prompt-count/turn-number logic as Pi.
- Root trace name: `OMP - Turn N`; add `turn_number` metadata.
- Keep `harness:omp` tags.
- Do not change usage extraction unless a test proves a mismatch.

### Claude Code

No hook code change. The hook already emits `Claude Code - Turn N` with
`turn_number` and its token totals matched native usage on six audited turns.
The hook venv was upgraded to Python SDK 5.0.3 for the v4 ingestion header.
Include it in the final verification table.

## Tests

Use `/tmp/langfuse-e2e` or a fresh temp directory with a few Markdown files.
All prompts must force at least one shell/tool call.

For each harness, run two user prompts in the same session. Then verify:

- exactly two traces,
- names `... - Turn 1` and `... - Turn 2`,
- `turn_number` 1 and 2,
- at least one tool span per trace,
- per-trace token totals equal native per-turn totals,
- session total equals native session total.

Harness commands (confirm exact flags with `--help`):

- Codex: `codex exec ...` and resume the same session for turn 2.
- Devin: `devin ...` and resume the same session for turn 2.
- Pi: `pi -p --session-id <id> "..."` twice with the same id.
- OMP: `omp --mode=json ...` and continue/resume the same session.
- Claude: existing audit is sufficient; optionally rerun a two-turn session.

The implementers should run static checks only; the coordinator runs the live
Langfuse tests and pushes reviewed changes.

## Acceptance

- Every new trace for the seven harnesses has a numbered name and integer
  `turn_number`.
- Devin trace totals use all assistant calls in the turn.
- Codex/Claude token totals remain exact.
- Pi/OMP token totals are exact in live two-turn tests.
- No empty `Codex Turn` traces for turns with no model/tool activity.

## Verification results

Live two-turn tool-calling sessions on 2026-10-20/21:

| Harness | Native turns | Trace names | Langfuse total | Native total | Tool spans |
| --- | --- | --- | ---: | ---: | --- |
| Codex | 37,233 / 37,764 | `Codex - Turn 1/2` | 74,997 | 74,997 | 1 per turn |
| Devin | 51,655 / 43,441 | `Devin - Turn 1/2` | 95,096 | 95,096 | 0 (hook has no tool export) |
| Pi | 10,688 / 10,826 | `Pi - Turn 1/2` | 21,514 | 21,514 | 1 per turn |
| OMP | 17,351 / 17,530 | `OMP - Turn 1/2` | 34,881 | 34,881 | 1 per turn |
| Amp | 16,311 / 16,781 | `Amp - Turn 1/2` | 33,092 | 33,092 | 1 per turn |
| Claude Code | audited 6 turns | `Claude Code - Turn N` | exact | exact | present |
| Claude Code (Python SDK 5) | 65,489 / 68,235 | `Claude Code - Turn 1/2` | 133,724 | 133,724 | 1 per turn |
| Droid (Python SDK 5) | 13,590 | `Factory Droid - Turn 1` | 13,590 | 13,590 | 1 |

Codex fixes found during verification:

- The Stop hook can run twice for the same turn. The original sidecar only
  marked completed turns, so the first upload was repeated by the second hook.
  The hook now marks every uploaded turn id and serializes per-rollout work
  with a lock file. A fresh two-turn test produced exactly two traces.
- Empty turns with no steps and no input/output are skipped.
- Duplicate Codex test traces created before the mark fix remain historical;
  they were not deleted.

Amp:

- Single-turn trace `T-01a15b25-3232-7280-b71d-feffcea5d38a` / Turn 1:
  4,678 native and Langfuse.
- Two-turn thread `T-01a15b36-24dc-759c-af85-e1e10f85717b`:
  `16,311` and `16,781` native and Langfuse, two traces, one tool each.
- Delayed two-message stream trace `T-01a15b29-2a1e-77bf-941a-891d49da800b`:
  53,556 native and Langfuse across both messages.
- `amp threads continue ... -x` starts a new process that replays the previous
  `agent.end`; it does not dispatch a fresh agent lifecycle for the continued
  prompt, so the second trace is missing in that noninteractive path. Normal
  interactive Amp turns dispatch per prompt. This is a CLI lifecycle limit,
  not a token-formula issue.
- The 15-second / 5-second usage poll avoids the 30-second hook timeout and
  Amp's full-read rate limit while still allowing the export to catch up.

Pi/OMP produced two traces with integer `turn_number` metadata, `harness:pi`
or `harness:omp` tags, and exact token totals. Pi/OMP `promptCount` is seeded
from prior user entries at session start and preserved across run resets.
After the v4 fallback removal, fresh sessions matched again: Pi `21,524`
native and Langfuse, OMP `34,895` native and Langfuse.

Amp was committed and pushed as `c389c1e` on `main`.

## Langfuse v4 read migration

The verification path no longer uses the deprecated trace, session, or v1
observation reads. New tooling:

- `tools/langfuse_v2.py` uses `GET /api/public/v2/observations`, cursor
  pagination, bounded time ranges, and the
  `core,basic,usage,trace_context,metadata` field groups.
- `tools/verify_agent_session.py` groups v2 observation rows by `traceId`,
  reads `traceName`, tags, and integer `turn_number` from the rows, and sums
  `totalUsage` on GENERATION observations.

One v2 behavior matters: a `sessionId` query can return only root observations
because child observations do not repeat the trace session ID. The client
resolves trace IDs from the session query and then fetches each `traceId`'s
complete observation tree.

The seven agent transports remain on OpenTelemetry ingestion with
`x-langfuse-ingestion-version: 4`. Droid and Claude run Python SDK 5.0.3,
which sets that header automatically; Pi/OMP send it explicitly. Pi/OMP's
legacy REST trace fallback has been removed and both passed fresh two-turn v2
verification afterward; score-create ingestion remains supported by Langfuse
after the November 16, 2026 cutover.

Run the migrated verifier with:

```sh
cd ~/Documents/dev/agent-langfuse-connector/tools
python3 verify_agent_session.py --harness pi --session-id <session-id>
```


