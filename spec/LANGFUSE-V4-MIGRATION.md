# Langfuse v4 Migration

Date: 2026-10-21
Server target: Langfuse v4
Ingestion: OpenTelemetry `POST /api/public/otel/v1/traces` with
`x-langfuse-ingestion-version: 4`
Reads: Observations API v2 `GET /api/public/v2/observations`

## Verified API facts

- Langfuse Cloud switches fully to v4 on 2026-11-16.
- Legacy trace, observation, session, score, and metric reads are removed.
- Legacy trace/observation ingestion via `POST /api/public/ingestion` is removed
  for traces and observations.
- Python SDK 5 and JS/TS SDK 6 are the current majors; Python SDK 5 sets the
  v4 ingestion header on its OTLP exporter.
- `score-create` events sent by SDK score helpers to
  `/api/public/ingestion` remain supported after the cutover.
- Observations API v2 is cursor-paginated and requires bounded
  `fromStartTime`/`toStartTime`.
- A v2 session query can return only root observations because child
  observations do not repeat `sessionId`. Resolve trace ids from the session
  query, then query each `traceId` to reconstruct the tree.

## Already migrated

- Verification now uses `tools/langfuse_v2.py` and
  `tools/verify_agent_session.py`.
- Droid, Claude, Devin, Amp, Codex, Pi, and OMP ingest through OTel v4.
  Pi/OMP pass an explicit `x-langfuse-ingestion-version: 4` header.
- Codex, Devin, Pi, OMP, Amp, Droid, and Claude verification succeeded against
  Observations API v2 using the migrated tools.
- Droid and Claude hook environments were upgraded from Python SDK 4.17.0 to
  5.0.3. Python SDK 5 sets `x-langfuse-ingestion-version: 4` on the OTLP
  exporter automatically. Hook imports and the SDK v5 tracing APIs
  (`start_as_current_observation`, `propagate_attributes`, private
  `_otel_tracer`) pass smoke tests. A local capture server confirmed both
  environments post to `/api/public/otel/v1/traces` with
  `x-langfuse-ingestion-version: 4`. A live Claude two-turn tool session on
  SDK 5 produced two exact v2 traces (133,724 native and Langfuse). A live
  Droid custom-model tool turn produced 13,590 native and Langfuse exactly.
  The default hosted model returned HTTP 402 for this account; that is an
  account subscription issue, not the hook.

Live v2 verification after migration:

| Harness | Session | Native | Langfuse v2 | Traces |
| --- | --- | ---: | ---: | ---: |
| Codex | `01a15b34-dab7-7af0-8dcb-db21f2f9c1b8` | 74,997 | 74,997 | 2 |
| Devin | `triangular-equipment` | 95,096 | 95,096 | 2 |
| Pi | `01a15b31-0000-7000-8000-000000000001` | 21,514 | 21,514 | 2 |
| OMP | `01a15b2b-60ef-7212-a0ca-1fe976d2cd66` | 34,881 | 34,881 | 2 |
| Amp | `T-01a15b36-24dc-759c-af85-e1e10f85717b` | 33,092 | 33,092 | 2 |
| Pi (post-fallback removal) | `01a15b41-0000-7000-8000-000000000001` | 21,524 | 21,524 | 2 |
| OMP (post-fallback removal) | `01a15b43-8195-7298-8036-ea164a8fdcab` | 34,895 | 34,895 | 2 |
| Claude (Python SDK 5) | `876f00d4-2234-4c02-a806-b33b3757e760` | 133,724 | 133,724 | 2 |
| Droid (Python SDK 5) | `e0876562-29ba-4548-a5d0-c25c10a36e15` | 13,590 | 13,590 | 1 |

## Pi and OMP fallback removal (completed)

Files:

- `/Users/yo_macbook/.pi/agent/npm/node_modules/pi-langfuse/src/langfuse.ts`
- `/Users/yo_macbook/.omp/plugins/node_modules/pi-langfuse/src/langfuse.ts`
- OMP source fork, hardlinked to the installed package:
  `/Users/yo_macbook/Documents/dev/agent-langfuse-connector/packages/typescript/omp/src/langfuse.ts`

Applied changes:

1. Remove the legacy REST trace fallback:
   - `getLegacyTraceApiCapability`
   - `getTraceVisibility`
   - `waitForTraceVisibility`
   - `fallbackToRestIngestion`
   - the shutdown step that calls `fallbackToRestIngestion`
   - `/api/public/traces` and `/api/public/traces/{id}` calls.
2. Do not send `trace-create`, `span-create`, or `generation-create` events to
   `/api/public/ingestion`. OTel is the only trace/observation ingestion path.
3. Keep `ingestBatch` and `flushPendingScores` for `score-create` only. Add a
   short comment that score ingestion remains supported after the v4 cutover.
4. Keep the OTel span processor, tracer provider, and score client. Add
   `additionalHeaders: { "x-langfuse-ingestion-version": "4" }` to the
   `LangfuseSpanProcessor` construction so real-time v2 reads are explicit.
5. Simplify or remove the now-unused REST fallback store/wrapper only where it
   is safe. Child observation inheritance and `setTraceIO` behavior must remain.
6. Keep `GET /api/public/projects` for connectivity checks. It is not a
   deprecated trace read.

Validation:

- `rg '/api/public/(ingestion|traces|sessions|observations)'` in both packages
  must show only the score ingestion path, or no trace/observation path.
- `bun build index.ts --external @earendil-works/pi-coding-agent --target bun`
  for both packages. Without `--target bun`, Bun defaults to a browser target
  and fails on Node-only OpenTelemetry imports; that failure predates this
  migration.
- Confirm OMP source and installed files are identical.
- Do not commit or push.

Fresh Pi and OMP two-turn tool sessions against v2 passed after the fallback
removal. Both now send OTel with an explicit
`x-langfuse-ingestion-version: 4` header. The only remaining
`/api/public/ingestion` call is the supported `score-create` path.

The old `/tmp/langfuse_verify.py` and `/tmp/langfuse_sum2.py` ad hoc scripts
are obsolete: they still use v1 session and observation reads and must not be
used after the cutover. Use `tools/verify_agent_session.py` instead. Active
integrations no longer call v1 trace, session, or observation APIs.
