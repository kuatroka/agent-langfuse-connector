import { state, resetRunState, computeEvaluationScores } from "../state.js";
import { getRuntime, sendScore } from "../langfuse.js";
import { ensureConfig } from "../config.js";
import { shapePayload, truncate, extractFinalAssistant, extractAssistantOutput, getCapturePolicy, getLimits } from "../utils.js";
import { closeDanglingObservations } from "./tool.js";
import { applyCapturePolicy } from "../capture-policy.js";
import { collectSourceMetadata } from "../source-metadata.js";
import { normalizedTraceMetadata, normalizedTraceTags, resolveModelInfo } from "../model.js";

function stringMetadata(metadata: Record<string, unknown> | undefined): Record<string, string> | undefined {
  if (!metadata) {
    return undefined;
  }

  const output: Record<string, string> = {};
  for (const [key, value] of Object.entries(metadata)) {
    // turn_number must stay an integer on the trace; do not stringify it into
    // the propagated langfuse.trace.metadata.* attributes.
    if (key === "turn_number") {
      continue;
    }
    if (typeof value === "string") {
      output[key] = value;
    } else if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
      output[key] = String(value);
    }
  }
  return Object.keys(output).length > 0 ? output : undefined;
}

export function updateTraceIO(input?: unknown, output?: unknown) {
  const root = state.agentState?.root;
  if (!root?.setTraceIO) {
    return;
  }

  try {
    root.setTraceIO({ input, output });
  } catch {
    // Older SDKs may omit setTraceIO; root IO still mirrors trace IO in current Langfuse.
  }
}

/**
 * Count user-authored prompt entries in the session history.
 *
 * A user prompt is a `type: "message"` session entry whose message has
 * `role: "user"`. This excludes tool results (`role: "toolResult"`), assistant
 * messages, and extension-injected `custom_message`/`custom` entries, which all
 * use different entry/message shapes. Falls back to the current promptCount
 * when the entry shape is not inspectable.
 */
function countUserPromptEntries(ctx: any, fallback: number): number {
  try {
    const entries = ctx?.sessionManager?.getEntries?.();
    if (!Array.isArray(entries)) {
      return fallback;
    }
    return entries.filter((entry) => {
      if (!entry || entry.type !== "message") {
        return false;
      }
      const message = (entry as { message?: { role?: unknown } }).message;
      return message?.role === "user";
    }).length;
  } catch {
    return fallback;
  }
}

export async function startAgentRun(event: Record<string, unknown>, ctx: any) {
  if (!(await ensureConfig(ctx))) {
    state.isTracingDisabled = true;
    return;
  }

  // One agent run == one user prompt. Seed the count from session history once
  // (e.g. after a resume), then advance it for this run.
  if (state.promptCount === 0) {
    state.promptCount = countUserPromptEntries(ctx, 0);
  }
  state.promptCount += 1;
  const turnNumber = state.promptCount;

  try {
    const rt = await getRuntime();
    const cwd = String(
      (event.systemPromptOptions && typeof event.systemPromptOptions === "object"
        ? (event.systemPromptOptions as Record<string, unknown>).cwd
        : undefined) ?? process.cwd(),
    );

    if (!state.currentModel && ctx.model) {
      state.currentModel = ctx.model.id || "";
      state.currentProvider = ctx.model.provider || "";
    }
    const modelInfo = resolveModelInfo(ctx.model, event, state.currentModel, {
      reasoningEffort: state.currentReasoningEffort,
    });
    if (modelInfo.raw) {
      state.currentModel = modelInfo.raw;
    }
    if (modelInfo.reasoningEffort) {
      state.currentReasoningEffort = modelInfo.reasoningEffort;
    }

    const rawPromptInput = shapePayload({
      prompt: event.prompt,
      images: event.images,
      context: event.context ?? event.attachments,
    });
    const capturePolicy = getCapturePolicy();
    const sourceMetadata = collectSourceMetadata(cwd, capturePolicy.captureSourceMetadata);
    const captured = applyCapturePolicy(
      {
        input: rawPromptInput,
        metadata: {
          cwd,
          ...sourceMetadata,
          ...normalizedTraceMetadata(modelInfo),
          ...(state.currentProvider ? { provider: state.currentProvider } : {}),
          sessionId: state.currentSessionId || undefined,
          turn_number: turnNumber,
        },
      },
      capturePolicy,
    );

    state.agentState = {
      cwd,
      promptInput: captured.input,
      generationSeq: 0,
      activeGenerations: new Map(),
      generationOrder: [],
      activeTools: new Map(),
      sourceMetadata,
      providerMetadataByRequest: new Map(),
    };

    const root = rt.propagateAttributes(
      {
        sessionId: state.currentSessionId ? truncate(state.currentSessionId, 200) : undefined,
        traceName: `OMP - Turn ${turnNumber}`,
        metadata: stringMetadata(captured.metadata),
        tags: normalizedTraceTags(modelInfo),
      },
      () =>
        rt.startObservation(
          "omp-agent",
            {
              input: captured.input,
              metadata: captured.metadata ?? {},
            },
          { asType: "agent" },
        ),
    );

    state.agentState.root = root;
    state.agentState.traceId = root.traceId;
    updateTraceIO(captured.input, undefined);
  } catch (e) {
    console.warn("📊 Langfuse: Failed to create agent observation", e);
    state.isTracingDisabled = true;
  }
}

/**
 * Records the effective system prompt on the root agent observation.
 *
 * Deliberately called from `agent_start` rather than `before_agent_start`:
 * during `before_agent_start` the extension runner hands each handler the
 * prompt as it stands mid-chain, so extensions registered after this one
 * (e.g. inline factories that rewrite the system prompt) are not reflected
 * yet. By `agent_start` the session has applied the final override, and
 * `ctx.getSystemPrompt()` returns the prompt actually sent to the model.
 */
export async function recordSystemPrompt(ctx: any) {
  const root = state.agentState?.root;
  if (state.isTracingDisabled || !root) {
    return;
  }

  let systemPrompt = undefined;
  try {
    if (ctx.getSystemPrompt) {
      systemPrompt = await ctx.getSystemPrompt();
    }
  } catch {
    // Ignore if getSystemPrompt is not available or fails
  }
  if (!systemPrompt) {
    return;
  }

  const captured = applyCapturePolicy(
    { systemPrompt: truncate(String(systemPrompt), getLimits().maxString) },
    getCapturePolicy(),
  );
  if (!captured.systemPrompt) {
    return;
  }

  try {
    root.update({ metadata: { systemPrompt: captured.systemPrompt } });
  } catch (e) {
    console.warn("\u{1F4CA} Langfuse: Failed to record system prompt", e);
  }
}

export async function finishAgentRun(event: Record<string, unknown> = {}) {
  if (!state.agentState?.root) {
    resetRunState();
    return;
  }

  const lastAssistant = extractFinalAssistant(event.messages);
  const rawOutput = lastAssistant ? extractAssistantOutput(lastAssistant) : state.agentState.latestAssistantOutput;
  const captured = applyCapturePolicy(
    {
      output: rawOutput,
      metadata: {
        cwd: state.agentState.cwd,
        ...(state.agentState.sourceMetadata ?? {}),
        completed: true,
        ...normalizedTraceMetadata(resolveModelInfo(state.currentModel, {
          reasoningEffort: state.currentReasoningEffort,
        })),
        provider: state.currentProvider || undefined,
        totalTools: state.toolCallCount,
        ...computeEvaluationScores(),
      },
    },
    getCapturePolicy(),
  );
  const scores = computeEvaluationScores();

  closeDanglingObservations("Agent run ended before observation finalized");

  try {
    state.agentState.root
      .update({
        output: captured.output,
        metadata: captured.metadata,
      })
      .end();
    updateTraceIO(state.agentState.promptInput, captured.output);
    try {
      execFileSync("/Users/yo_macbook/.local/bin/agent-eval", ["ingest"], {
        input: JSON.stringify({id: state.agentState.traceId, session_id: state.currentSessionId,
          metadata: captured.metadata, prompt: state.agentState.promptInput, response: captured.output,
          project: state.agentState.cwd}), timeout: 3000, stdio: ["pipe", "pipe", "pipe"],
      });
    } catch { console.warn("AgentEval local capture failed; remote backfill remains enabled"); }

    await sendScore("tool_call_count", scores.tool_call_count, { traceId: state.agentState.traceId });
    await sendScore("turn_count", scores.turn_count, { traceId: state.agentState.traceId });
    await sendScore("total_tool_errors", scores.total_tool_errors, { traceId: state.agentState.traceId });
    await sendScore("tool_success_rate", scores.tool_success_rate, { traceId: state.agentState.traceId });
    await sendScore("session_had_errors", scores.session_had_errors, { traceId: state.agentState.traceId });
  } catch (e) {
    console.warn("📊 Langfuse: Failed to finish agent observation", e);
  } finally {
    resetRunState();
  }
}
import { execFileSync } from "node:child_process";
