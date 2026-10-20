import type { Dirent } from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";

import {
  createTraceId,
  propagateAttributes,
  startObservation,
  type LangfuseGenerationAttributes,
  type LangfuseObservation,
} from "@langfuse/tracing";
import { TraceFlags, type SpanContext } from "@opentelemetry/api";

import type { Config } from "./config.js";
import { parseSession } from "./parse.js";
import { acquireRolloutLock, loadUploadedTurnIds, markTurnUploaded } from "./sidecar.js";
import type { ModelStep, RolloutLine, SessionMeta, TokenUsage, ToolCall, Turn } from "./types.js";
import { debugLog, toText, truncate } from "./utils.js";

async function loadSession(file: string): Promise<RolloutLine[]> {
  const data = await fs.readFile(file, "utf-8");
  const lines: RolloutLine[] = [];
  for (const raw of data.split("\n")) {
    const trimmed = raw.trim();
    if (!trimmed) continue;
    try {
      lines.push(JSON.parse(trimmed) as RolloutLine);
    } catch {
      // skip malformed lines rather than aborting the whole upload
    }
  }
  return lines;
}

/**
 * Resolve a subagent's rollout file from its thread id.
 *
 * Rollouts live at `<sessionsRoot>/YYYY/MM/DD/rollout-<ts>-<threadId>.jsonl`.
 * Starting from the parent rollout, we walk up to the sessions root and search
 * for a file whose name ends with the subagent's thread id.
 */
async function findSubagentRollout(
  parentFile: string,
  threadId: string,
): Promise<string | undefined> {
  const suffix = `-${threadId}.jsonl`;
  const root = path.resolve(path.dirname(parentFile), "../../..");

  async function walk(dir: string): Promise<string | undefined> {
    let entries: Dirent[];
    try {
      entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      return undefined;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        const found = await walk(full);
        if (found) return found;
      } else if (entry.isFile() && entry.name.endsWith(suffix)) {
        return full;
      }
    }
    return undefined;
  }

  return walk(root);
}

/**
 * Placeholder parent span id used to pin a deterministic trace id on a root
 * span (the pattern the Langfuse SDK documents for custom trace ids). The id
 * never exists as a real span, so Langfuse still renders the turn as the
 * trace root.
 */
const SEED_PARENT_SPAN_ID = "0123456789abcdef";

/**
 * Derive the deterministic trace id for a turn from `config.trace_seed`.
 *
 * Main-thread turn N (1-based, rollout order):  createTraceId(`${seed}:${N}`)
 * Subagent-thread turn N:                       createTraceId(`${seed}:${threadId}:${N}`)
 *
 * The main-thread form deliberately excludes the thread id so external systems
 * can precompute trace ids (hex(sha256(seed)).slice(0, 32)) before the Codex
 * thread exists. Returns `undefined` (auto-generated ids) when no seed is set
 * or derivation fails — the hook must never block an upload.
 */
async function seededTraceParent(
  config: Config,
  sessionMeta: SessionMeta,
  turnNumber: number,
): Promise<SpanContext | undefined> {
  if (!config.trace_seed) return undefined;
  try {
    const seed = sessionMeta.isSubagentThread
      ? `${config.trace_seed}:${sessionMeta.sessionId}:${turnNumber}`
      : `${config.trace_seed}:${turnNumber}`;
    return {
      traceId: await createTraceId(seed),
      spanId: SEED_PARENT_SPAN_ID,
      traceFlags: TraceFlags.SAMPLED,
      isRemote: true,
    };
  } catch (error) {
    debugLog("failed to derive seeded trace id; falling back to auto-generated:", error);
    if (config.fail_on_error) throw error;
    return undefined;
  }
}

function isTokenCount(value: number | undefined): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

/** Send Codex's inclusive counts using Langfuse's strict OpenAI usage schema. */
function toUsageDetails(
  usage: TokenUsage | undefined,
): LangfuseGenerationAttributes["usageDetails"] {
  if (!usage) return undefined;
  const {
    input_tokens: input,
    output_tokens: output,
    total_tokens: total,
    cached_input_tokens: cached,
    reasoning_output_tokens: reasoning,
  } = usage;

  if (
    !isTokenCount(input) ||
    !isTokenCount(output) ||
    !isTokenCount(total) ||
    total !== input + output
  ) {
    debugLog("dropping usage: missing or inconsistent token counts", usage);
    return undefined;
  }
  if (
    (cached !== undefined && (!isTokenCount(cached) || cached > input)) ||
    (reasoning !== undefined && (!isTokenCount(reasoning) || reasoning > output))
  ) {
    debugLog("dropping usage: implausible cached/reasoning details", usage);
    return undefined;
  }

  // The runtime supports this documented shape, but the SDK type still
  // exposes only its legacy camelCase usage interface.
  return {
    prompt_tokens: input,
    completion_tokens: output,
    total_tokens: total,
    ...(cached !== undefined ? { prompt_tokens_details: { cached_tokens: cached } } : {}),
    ...(reasoning !== undefined
      ? { completion_tokens_details: { reasoning_tokens: reasoning } }
      : {}),
  } as unknown as LangfuseGenerationAttributes["usageDetails"];
}

type Clip = {
  (value: string): string;
  (value: unknown): unknown;
};

/** Build a clip() that truncates long strings to `maxChars`. */
function makeClip(maxChars: number): Clip {
  function clip(value: string): string;
  function clip(value: unknown): unknown;
  function clip(value: unknown): unknown {
    if (typeof value !== "string") return value;
    const { text, meta } = truncate(value, maxChars);
    return meta ? `${text}\n…[truncated ${meta.originalLength - text.length} chars]` : text;
  }
  return clip;
}

function buildGenerationOutput(step: ModelStep, clip: Clip): Record<string, unknown> | undefined {
  const output: Record<string, unknown> = {};
  if (step.text) output.content = clip(step.text);
  if (step.reasoning) output.reasoning = clip(step.reasoning);
  if (step.toolCalls.length > 0) {
    output.tool_calls = step.toolCalls.map((tc) => ({
      id: tc.callId,
      name: tc.name,
      arguments: tc.args,
    }));
  }
  return Object.keys(output).length > 0 ? output : undefined;
}

/**
 * Observation name for a tool call. MCP calls use the clean `server.tool`
 * split from the mcp_tool_call_* events instead of the mangled function name;
 * everything else uses the plain tool name. Call arguments (shell command,
 * search query, …) stay out of the name — they belong to the observation
 * input.
 */
function toolObservationName(tc: ToolCall): string {
  if (tc.mcp) return `${tc.mcp.server}.${tc.mcp.tool}`;
  return tc.name || "tool";
}

/**
 * A turn with no model steps carries no activity worth tracing. Codex writes
 * failed or placeholder turns with a user prompt but no model response; those
 * must not become empty `model:unknown` traces.
 */
function isEmptyTurn(turn: Turn): boolean {
  return turn.steps.length === 0;
}

/** Emit a single turn (and its subagents) as a Langfuse observation tree. */
async function emitTurn(
  turn: Turn,
  sessionMeta: SessionMeta,
  ctx: {
    config: Config;
    rolloutFile: string;
    parentObservation?: LangfuseObservation;
    /** Pre-derived trace id for top-level turns (see seededTraceParent). */
    seededParent?: SpanContext;
    /** 1-based number of this emitted turn within its thread, for naming. */
    turnNumber?: number;
  },
): Promise<void> {
  const clip = makeClip(ctx.config.max_chars);
  const reasoningEffort = turn.reasoningEffort ?? sessionMeta.reasoningEffort;
  const modelBase = turn.model ?? "unknown";
  const model = reasoningEffort ? `${modelBase}(${reasoningEffort})` : modelBase;

  // A turn belongs to a subagent when its rollout is marked as a subagent
  // thread or when it is being nested under a spawning turn.
  const isSubagent = sessionMeta.isSubagentThread === true || ctx.parentObservation != null;

  const rootName =
    ctx.turnNumber !== undefined
      ? isSubagent
        ? `Codex Subagent - Turn ${ctx.turnNumber}`
        : `Codex - Turn ${ctx.turnNumber}`
      : isSubagent
        ? "Codex Subagent Turn"
        : "Codex Turn";

  const root = startObservation(
    rootName,
    {
      input: turn.userInput != null ? clip(turn.userInput) : undefined,
      output: turn.finalOutput != null ? clip(turn.finalOutput) : undefined,
      level: turn.aborted ? "WARNING" : undefined,
      statusMessage: turn.aborted ? "Turn interrupted by user" : undefined,
      metadata: {
        harness: "codex",
        model,
        model_base: modelBase,
        model_source: "codex-rollout",
        ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
        reasoning_effort_source: "codex-rollout",
        "codex.turn_id": turn.turnId,
        "codex.thread_id": sessionMeta.sessionId,
        "codex.model": turn.model,
        "codex.model_provider": sessionMeta.modelProvider,
        "codex.cli_version": sessionMeta.cliVersion,
        "codex.aborted": turn.aborted,
        "codex.tool_call_count": turn.steps.reduce((n, s) => n + s.toolCalls.length, 0),
        ...(ctx.turnNumber !== undefined ? { turn_number: ctx.turnNumber } : {}),
      },
    },
    {
      asType: "agent",
      startTime: new Date(turn.startTime),
      parentSpanContext: ctx.parentObservation?.otelSpan.spanContext() ?? ctx.seededParent,
    },
  );

  let previousToolResults: unknown = undefined;

  for (let i = 0; i < turn.steps.length; i++) {
    const step = turn.steps[i];
    const generation = startObservation(
      isSubagent ? "LLM Subagent" : "LLM",
      {
        input:
          i === 0
            ? turn.userInput != null
              ? clip(turn.userInput)
              : undefined
            : previousToolResults,
        output: buildGenerationOutput(step, clip),
        model: turn.model,
        usageDetails: toUsageDetails(step.usage),
        metadata: {
          "codex.step_index": i,
          reasoning_effort: reasoningEffort ?? "unknown",
          reasoning_effort_source: "codex-rollout",
        },
      },
      {
        asType: "generation",
        startTime: new Date(step.startTime),
        parentSpanContext: root.otelSpan.spanContext(),
      },
    );

    for (const tc of step.toolCalls) {
      emitToolCall(tc, generation, clip, step.endTime);
    }

    generation.end(new Date(step.endTime));

    previousToolResults =
      step.toolCalls.length > 0
        ? step.toolCalls.map((tc) => ({
            name: tc.name,
            output: tc.output != null ? clip(toText(tc.output)) : undefined,
            ...(tc.error ? { error: clip(tc.error) } : {}),
          }))
        : undefined;
  }

  // Subagent threads spawned by this turn are nested under the turn root.
  for (const threadId of turn.subagentThreadIds) {
    const subFile = await findSubagentRollout(ctx.rolloutFile, threadId);
    if (!subFile) {
      debugLog(`subagent rollout not found for thread ${threadId}`);
      continue;
    }
    await convertRollout(subFile, { config: ctx.config, parentObservation: root });
  }

  root.end(new Date(turn.endTime));
  if (!process.env.VITEST && !isSubagent && turn.finalOutput != null) {
    try {
      execFileSync("/Users/yo_macbook/.local/bin/agent-eval", ["ingest"], {
        input: JSON.stringify({id: root.otelSpan.spanContext().traceId,
          session_id: sessionMeta.sessionId, harness: "codex", model_base: modelBase,
          model_source: "codex-rollout", reasoning_effort: reasoningEffort,
          reasoning_effort_source: "codex-rollout", prompt: turn.userInput,
          response: turn.finalOutput, occurred_at: new Date(turn.startTime).toISOString()}),
        timeout: 3000, stdio: ["pipe", "pipe", "pipe"],
      });
    } catch { debugLog("AgentEval local capture failed; remote backfill remains enabled"); }
  }
}

function emitToolCall(
  tc: ToolCall,
  parent: LangfuseObservation,
  clip: Clip,
  fallbackEnd: number,
): void {
  const tool = startObservation(
    toolObservationName(tc),
    {
      input: tc.args,
      output: tc.output != null ? clip(toText(tc.output)) : undefined,
      level: tc.error ? "ERROR" : undefined,
      statusMessage: tc.error ? clip(tc.error) : undefined,
      metadata: { "codex.call_id": tc.callId, "codex.tool_name": tc.name || "tool" },
    },
    {
      asType: "tool",
      startTime: new Date(tc.startTime),
      parentSpanContext: parent.otelSpan.spanContext(),
    },
  );
  tool.end(new Date(tc.endTime ?? fallbackEnd));
}

/**
 * Convert a Codex rollout file into Langfuse traces.
 *
 * Top-level turns each become their own trace (grouped into a Langfuse session
 * via the Codex thread id). Subagent rollouts are nested under the spawning
 * turn via `parentObservation`.
 */
export async function convertRollout(
  rolloutFile: string,
  options: { config: Config; parentObservation?: LangfuseObservation },
): Promise<void> {
  const release = await acquireRolloutLock(rolloutFile);
  try {
    await convertRolloutUnlocked(rolloutFile, options);
  } finally {
    await release();
  }
}

async function convertRolloutUnlocked(
  rolloutFile: string,
  options: { config: Config; parentObservation?: LangfuseObservation },
): Promise<void> {
  const { sessionMeta, turns } = parseSession(await loadSession(rolloutFile));
  debugLog(`parsed ${turns.length} turn(s) from ${path.basename(rolloutFile)}`);

  // Subagent rollout: nest everything under the parent turn, no dedup/session wrapping.
  if (options.parentObservation) {
    let subagentTurnNumber = 0;
    for (const turn of turns) {
      // Skip empty turns (no model steps and no input/output) — these are the
      // `model:unknown` placeholders Codex writes for turns with no activity.
      if (isEmptyTurn(turn)) continue;
      subagentTurnNumber += 1;
      await emitTurn(turn, sessionMeta, {
        config: options.config,
        rolloutFile,
        parentObservation: options.parentObservation,
        turnNumber: subagentTurnNumber,
      });
    }
    return;
  }

  const uploaded = await loadUploadedTurnIds(rolloutFile);
  // Display numbering counts only emitted turns; seeded trace ids keep the
  // native 1-based rollout index so external id derivation stays stable.
  let emittedTurnNumber = 0;

  for (let turnIndex = 0; turnIndex < turns.length; turnIndex++) {
    const turn = turns[turnIndex];
    // Skip empty turns (no model steps and no user input/final output) — the
    // empty `Codex Turn` traces with `model:unknown` and zero generations.
    if (isEmptyTurn(turn)) {
      debugLog(`skipping empty turn at rollout index ${turnIndex + 1}`);
      continue;
    }
    // Display numbering counts every non-empty turn in the rollout, including
    // turns uploaded by an earlier Stop hook. Otherwise resuming a thread would
    // renumber turn 2 as turn 1.
    emittedTurnNumber += 1;
    if (turn.turnId && uploaded.has(turn.turnId)) {
      continue; // already uploaded in a previous hook invocation
    }

    // Turn numbering stays 1-based over the full rollout (including turns
    // skipped by dedup above) so the derived id is stable across hook runs.
    const seededParent = await seededTraceParent(options.config, sessionMeta, turnIndex + 1);
    const reasoningEffort = turn.reasoningEffort ?? sessionMeta.reasoningEffort;
    const modelBase = turn.model ?? "unknown";
    const model = reasoningEffort ? `${modelBase}(${reasoningEffort})` : modelBase;

    const traceName = sessionMeta.isSubagentThread
      ? `Codex Subagent - Turn ${emittedTurnNumber}`
      : `Codex - Turn ${emittedTurnNumber}`;

    await propagateAttributes(
      {
        sessionId: sessionMeta.sessionId,
        traceName,
        ...(options.config.user_id ? { userId: options.config.user_id } : {}),
        tags: [
          ...new Set([
            ...(options.config.tags ?? []),
            "harness:codex",
            `model:${model}`,
            ...(reasoningEffort ? [`reasoning-effort:${reasoningEffort}`] : []),
          ]),
        ],
        metadata: {
          ...(options.config.metadata ?? {}),
          harness: "codex",
          model,
          model_base: modelBase,
          model_source: "codex-rollout",
          ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
          reasoning_effort_source: "codex-rollout",
          turn_number: emittedTurnNumber,
        },
      },
      async () => {
        await emitTurn(turn, sessionMeta, {
          config: options.config,
          rolloutFile,
          seededParent,
          turnNumber: emittedTurnNumber,
        });
      },
    );

    // Mark every uploaded turn, including a trailing turn that Codex has not
    // flagged completed yet. Codex can fire the Stop hook twice for one turn;
    // leaving the first upload unmarked makes the second invocation upload the
    // same usage again. The Stop hook already runs at turn end, so the rollout
    // contains the turn's token usage.
    if (turn.turnId) {
      uploaded.add(turn.turnId);
      await markTurnUploaded(rolloutFile, turn.turnId);
    }
  }
}
import { execFileSync } from "node:child_process";
