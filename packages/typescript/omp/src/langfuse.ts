import type { LangfuseRuntime, LangfuseScoreClient, PendingScore } from "./types.js";
import { state } from "./state.js";
import { randomUUID } from "node:crypto";

let runtime: LangfuseRuntime | null = null;
let registeredContextManager: OtelContextManager | null = null;
const activeSessions = new Set<string>();
let lastRuntimeError: { scope: string; message: string; timestamp: Date } | null = null;

interface OtelContextManager {
  enable(): OtelContextManager;
  disable(): void;
}

interface OtelContextApi {
  setGlobalContextManager(contextManager: OtelContextManager): boolean;
}

type AsyncHooksContextManagerCtor = new () => OtelContextManager;

const DEFAULT_SHUTDOWN_STEP_TIMEOUT_MS = 2_000;
const DEFAULT_SCORE_SHUTDOWN_TIMEOUT_MS = 2_000;
const DEFAULT_LANGFUSE_REQUEST_TIMEOUT_SECONDS = 5;
const DEFAULT_SCORE_FLUSH_AT = 10;
const DEFAULT_SCORE_FLUSH_INTERVAL_MS = 1_000;
const MAX_SCORE_QUEUE_SIZE = 100_000;
const MAX_SCORE_BATCH_SIZE = 100;

let shutdownStepTimeoutMs = DEFAULT_SHUTDOWN_STEP_TIMEOUT_MS;

function nowIso() {
  return new Date().toISOString();
}

function resolvePositiveEnvNumber(name: string, fallback: number, integer = false): number {
  const parsed = Number(process.env[name]);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return integer ? Math.floor(parsed) : parsed;
}

function getScoreShutdownTimeoutMs(): number {
  return resolvePositiveEnvNumber(
    "PI_LANGFUSE_SCORE_SHUTDOWN_TIMEOUT",
    DEFAULT_SCORE_SHUTDOWN_TIMEOUT_MS / 1_000,
  ) * 1_000;
}

function debugLog(message: string) {
  if (process.env.PI_LANGFUSE_DEBUG === "1" || process.env.PI_LANGFUSE_DEBUG === "true") {
    console.log(message);
  }
}

export function ensureOtelContextManager(
  contextApi: OtelContextApi,
  AsyncHooksContextManager: AsyncHooksContextManagerCtor,
): boolean {
  if (registeredContextManager) {
    return true;
  }

  const contextManager = new AsyncHooksContextManager().enable();
  if (contextApi.setGlobalContextManager(contextManager)) {
    registeredContextManager = contextManager;
    return true;
  }

  contextManager.disable();
  return false;
}

function rememberRuntimeError(scope: string, error: unknown) {
  lastRuntimeError = {
    scope,
    message: error instanceof Error ? error.message : String(error),
    timestamp: new Date(),
  };
}

export function getLastRuntimeError(): { scope: string; message: string; timestamp: Date } | null {
  return lastRuntimeError;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

async function withShutdownDeadline<T>(
  label: string,
  startOperation: () => Promise<T> | undefined,
  deadline: number,
  signal?: AbortSignal,
): Promise<T | undefined> {
  const remainingMs = deadline - Date.now();
  if (remainingMs <= 0) {
    debugLog(`📊 Langfuse: Skipped ${label}; shutdown deadline elapsed`);
    return undefined;
  }

  const operation = startOperation();
  if (!operation) {
    return undefined;
  }

  let timeout: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<undefined>((resolve) => {
        timeout = setTimeout(() => {
          debugLog(`📊 Langfuse: ${label} timed out; shutdown deadline elapsed`);
          resolve(undefined);
        }, remainingMs);
      }),
    ]);
  } catch (error) {
    if (signal?.aborted && isAbortError(error)) {
      debugLog(`📊 Langfuse: ${label} aborted; shutdown deadline elapsed`);
      return undefined;
    }
    throw error;
  } finally {
    if (timeout) {
      clearTimeout(timeout);
    }
  }
}

async function runShutdownStep<T>(
  label: string,
  startOperation: () => Promise<T> | undefined,
  deadline: number,
  signal?: AbortSignal,
): Promise<T | undefined> {
  try {
    return await withShutdownDeadline(label, startOperation, deadline, signal);
  } catch (error) {
    rememberRuntimeError(`runtime shutdown: ${label}`, error);
    console.warn(`📊 Langfuse: Failed ${label} during shutdown`, error);
    return undefined;
  }
}

function getRuntimeConfig(rt: LangfuseRuntime) {
  return rt.runtimeConfig ?? state.config;
}

function ingestionHeaders(rt: LangfuseRuntime): Record<string, string> {
  const config = getRuntimeConfig(rt);
  if (!config) {
    throw new Error("Langfuse runtime config is unavailable");
  }

  const auth = Buffer.from(`${config.publicKey}:${config.secretKey}`).toString("base64");
  return {
    Authorization: `Basic ${auth}`,
    "Content-Type": "application/json",
  };
}

/**
 * POST a batch to `/api/public/ingestion`. Langfuse v4 removed this endpoint
 * for trace/observation events; `score-create` remains supported after the
 * cutover, so this path is used by score ingestion only. All trace and
 * observation data goes through the OTel exporter instead.
 */
async function ingestBatch(rt: LangfuseRuntime, batch: unknown[], signal: AbortSignal): Promise<unknown[]> {
  const config = getRuntimeConfig(rt);
  if (!config) {
    throw new Error("Langfuse runtime config is unavailable");
  }

  const response = await fetch(`${config.host.replace(/\/$/, "")}/api/public/ingestion`, {
    method: "POST",
    headers: ingestionHeaders(rt),
    body: JSON.stringify({ batch }),
    signal,
  });
  if (!response.ok) {
    throw new Error(`Langfuse ingestion failed with HTTP ${response.status}`);
  }

  const text = await response.text();
  if (!text) {
    return [];
  }

  const responseBody = JSON.parse(text) as { errors?: unknown[] };
  return Array.isArray(responseBody.errors) ? responseBody.errors : [];
}

async function flushPendingScores(rt: LangfuseRuntime, signal: AbortSignal): Promise<void> {
  const pendingScores = rt.pendingScores;
  if (!pendingScores || pendingScores.length === 0) {
    return;
  }

  while (pendingScores.length > 0) {
    const scores = pendingScores.slice(0, MAX_SCORE_BATCH_SIZE);
    for (const score of scores) {
      score.id ??= randomUUID();
    }
    try {
      const errors = await ingestBatch(
        rt,
        scores.map((score) => ({
          type: "score-create",
          id: randomUUID(),
          timestamp: nowIso(),
          body: score,
        })),
        signal,
      );
      pendingScores.splice(0, scores.length);
      if (errors.length > 0) {
        rememberRuntimeError("score ingestion", new Error(JSON.stringify(errors)));
        console.warn("📊 Langfuse: Score ingestion reported errors", errors);
      }
    } catch (error) {
      if ((error as { name?: string }).name !== "AbortError") {
        rememberRuntimeError("score ingestion", error);
        console.warn("📊 Langfuse: Failed to flush scores", error);
      }
      return;
    }
  }
}

function clearScoreFlushTimer(rt: LangfuseRuntime) {
  if (rt.scoreFlushTimer) {
    clearTimeout(rt.scoreFlushTimer);
    rt.scoreFlushTimer = undefined;
  }
}

function scheduleScoreFlush(rt: LangfuseRuntime) {
  if (
    rt.scoreFlushStopped
    || rt.scoreFlushTimer
    || rt.scoreFlushPromise
    || !rt.pendingScores?.length
  ) {
    return;
  }

  rt.scoreFlushTimer = setTimeout(() => {
    rt.scoreFlushTimer = undefined;
    void startScoreFlush(rt);
  }, rt.scoreFlushIntervalMs ?? DEFAULT_SCORE_FLUSH_INTERVAL_MS);
  rt.scoreFlushTimer.unref?.();
}

function startScoreFlush(rt: LangfuseRuntime): Promise<void> {
  if (rt.scoreFlushPromise) {
    return rt.scoreFlushPromise;
  }

  clearScoreFlushTimer(rt);
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(new DOMException("Langfuse score request timed out", "AbortError")),
    rt.scoreRequestTimeoutMs ?? DEFAULT_LANGFUSE_REQUEST_TIMEOUT_SECONDS * 1_000,
  );
  timeout.unref?.();
  rt.scoreFlushController = controller;

  const promise = flushPendingScores(rt, controller.signal).finally(() => {
    clearTimeout(timeout);
    if (rt.scoreFlushPromise === promise) {
      rt.scoreFlushPromise = undefined;
    }
    if (rt.scoreFlushController === controller) {
      rt.scoreFlushController = undefined;
    }
    scheduleScoreFlush(rt);
  });
  rt.scoreFlushPromise = promise;
  return promise;
}

function stopScoreFlush(rt: LangfuseRuntime) {
  rt.scoreFlushStopped = true;
  clearScoreFlushTimer(rt);
  rt.scoreFlushController?.abort(
    new DOMException("Langfuse score flushing stopped", "AbortError"),
  );
}

/**
 * Wraps a Langfuse observation so children created via `startObservation` are
 * wrapped the same way. `update`, `end`, and `setTraceIO` delegate to the
 * underlying observation unchanged; OTel is the only trace/observation
 * ingestion path.
 */
function wrapObservation(observation: any): any {
  return {
    ...observation,
    id: observation.id || randomUUID(),
    traceId: observation.traceId || randomUUID(),
    update(updateBody?: Record<string, unknown>) {
      const updated = observation.update(updateBody);
      return updated === observation ? this : updated;
    },
    end(_endBody?: Record<string, unknown>) {
      return observation.end();
    },
    startObservation(childName: string, childBody?: Record<string, unknown>, options?: { asType?: string }) {
      const child = observation.startObservation(childName, childBody, options);
      return wrapObservation(child);
    },
    setTraceIO(traceBody?: { input?: unknown; output?: unknown }) {
      return observation.setTraceIO?.(traceBody);
    },
  };
}

export async function getRuntime(): Promise<LangfuseRuntime> {
  if (!state.config) {
    throw new Error("Langfuse config is not set");
  }

  // Track the current session as a runtime consumer.
  // Multiple sessions can share the same runtime; shutdown is deferred
  // until the last session releases it.
  const sessionId = state.currentSessionId;
  if (sessionId) {
    activeSessions.add(sessionId);
  }

  if (!runtime) {
    const [
      { BasicTracerProvider },
      { resources },
      { context },
      { AsyncHooksContextManager },
      { LangfuseSpanProcessor },
      tracing,
      { LangfuseClient },
    ] = await Promise.all([
      import("@opentelemetry/sdk-trace-base"),
      import("@opentelemetry/sdk-node"),
      import("@opentelemetry/api"),
      import("@opentelemetry/context-async-hooks"),
      import("@langfuse/otel"),
      import("@langfuse/tracing"),
      import("@langfuse/client"),
    ]);

    try {
      ensureOtelContextManager(context, AsyncHooksContextManager);
      const scoreFlushAt = resolvePositiveEnvNumber("LANGFUSE_FLUSH_AT", DEFAULT_SCORE_FLUSH_AT, true);
      const scoreFlushIntervalMs =
        resolvePositiveEnvNumber("LANGFUSE_FLUSH_INTERVAL", DEFAULT_SCORE_FLUSH_INTERVAL_MS / 1_000) * 1_000;
      const scoreRequestTimeoutMs =
        resolvePositiveEnvNumber("LANGFUSE_TIMEOUT", DEFAULT_LANGFUSE_REQUEST_TIMEOUT_SECONDS) * 1_000;
      const spanProcessor = new LangfuseSpanProcessor({
        publicKey: state.config.publicKey,
        secretKey: state.config.secretKey,
        baseUrl: state.config.host,
        additionalHeaders: { "x-langfuse-ingestion-version": "4" },
      });
      const resource = resources.defaultResource().merge(
        resources.detectResources({ detectors: [resources.envDetector] }),
      );
      const tracerProvider = new BasicTracerProvider({ resource, spanProcessors: [spanProcessor] });
      tracing.setLangfuseTracerProvider(tracerProvider);

      runtime = {
        startObservation: ((name: string, body?: Record<string, unknown>, options?: { asType?: string }) => {
          const observation = (tracing as any).startObservation(name, body, options);
          return wrapObservation(observation);
        }) as unknown as LangfuseRuntime["startObservation"],
        propagateAttributes: tracing.propagateAttributes as unknown as LangfuseRuntime["propagateAttributes"],
        scoreClient: new LangfuseClient({
          publicKey: state.config.publicKey,
          secretKey: state.config.secretKey,
          baseUrl: state.config.host,
        }) as LangfuseScoreClient,
        spanProcessor,
        tracerProvider,
        clearTracerProvider: () => tracing.setLangfuseTracerProvider(null),
        pendingScores: [],
        scoreFlushAt,
        scoreFlushIntervalMs,
        scoreRequestTimeoutMs,
        scoreFlushStopped: false,
        runtimeConfig: {
          publicKey: state.config.publicKey,
          secretKey: state.config.secretKey,
          host: state.config.host,
        },
      };
      lastRuntimeError = null;
    } catch (e) {
      rememberRuntimeError("runtime init", e);
      throw e;
    }
  }

  return runtime as LangfuseRuntime;
}

function doShutdownRuntime(): Promise<void> {
  return (async () => {
    if (!runtime) {
      return;
    }

    const rt = runtime;
    runtime = null;
    const deadline = Date.now() + shutdownStepTimeoutMs;
    const scoreController = new AbortController();
    const scoreShutdownTimeoutMs = Math.min(getScoreShutdownTimeoutMs(), shutdownStepTimeoutMs);
    const scoreDeadline = Math.min(deadline, Date.now() + scoreShutdownTimeoutMs);
    const scoreAbortTimeout = setTimeout(
      () => scoreController.abort(),
      scoreShutdownTimeoutMs,
    );
    scoreAbortTimeout.unref?.();
    stopScoreFlush(rt);

    try {
      await runShutdownStep(
        "Active score flush",
        () => rt.scoreFlushPromise,
        deadline,
      );
      await runShutdownStep(
        "Pending score flush",
        () => flushPendingScores(rt, scoreController.signal),
        scoreDeadline,
        scoreController.signal,
      );
      await runShutdownStep("OTel force flush", () => rt.tracerProvider?.forceFlush?.(), deadline);
      await runShutdownStep("Langfuse score flush", () => rt.scoreClient.flush?.(), deadline);
      await runShutdownStep("Langfuse client shutdown", () => rt.scoreClient.shutdown?.(), deadline);
      await runShutdownStep("OTel tracer shutdown", () => rt.tracerProvider?.shutdown?.(), deadline);
    } finally {
      clearTimeout(scoreAbortTimeout);
      scoreController.abort();
      clearScoreFlushTimer(rt);
      rt.scoreFlushController?.abort();
      rt.scoreFlushController = undefined;
      rt.scoreFlushPromise = undefined;
      if (!runtime) {
        rt.clearTracerProvider?.();
      }
    }
  })();
}

/**
 * Release the current session's reference to the Langfuse runtime.
 * Only actually shuts down the runtime when the last session releases it.
 * Accepts an optional sessionId for use outside of withSession (e.g. deferred callbacks).
 */
export async function shutdownRuntime(sessionId?: string): Promise<void> {
  const sid = sessionId ?? state.currentSessionId;
  if (sid) {
    activeSessions.delete(sid);
  }

  // Still have active sessions — keep the runtime alive.
  if (activeSessions.size > 0) {
    return;
  }

  await doShutdownRuntime();
}

/**
 * Force-shutdown the Langfuse runtime regardless of active session references.
 * Used when the user manually reconfigures (e.g. /langfuse-setup) and needs
 * a fresh runtime with new credentials.
 */
export async function forceShutdownRuntime(): Promise<void> {
  activeSessions.clear();
  await doShutdownRuntime();
}

export function __setRuntimeForTest(rt: LangfuseRuntime | null, timeoutMs = DEFAULT_SHUTDOWN_STEP_TIMEOUT_MS): void {
  if (runtime && runtime !== rt) {
    stopScoreFlush(runtime);
  }
  runtime = rt;
  if (rt) {
    rt.scoreFlushStopped = false;
  }
  shutdownStepTimeoutMs = timeoutMs;
  activeSessions.clear();
}

export async function sendScore(name: string, value: number, options: { traceId?: string; observationId?: string } = {}) {
  try {
    const rt = await getRuntime();
    const score: PendingScore = {
      name,
      value,
      dataType: name === "session_had_errors" || name === "tool_is_error" ? "BOOLEAN" : "NUMERIC",
      traceId: options.traceId,
      observationId: options.observationId,
      sessionId: options.traceId ? undefined : state.currentSessionId || undefined,
      ...(process.env.LANGFUSE_TRACING_ENVIRONMENT
        ? { environment: process.env.LANGFUSE_TRACING_ENVIRONMENT }
        : {}),
    };
    if (!rt.pendingScores) {
      return;
    }
    if (rt.pendingScores.length >= MAX_SCORE_QUEUE_SIZE) {
      const error = new Error(
        `Langfuse score queue is full (${MAX_SCORE_QUEUE_SIZE}); dropping score`,
      );
      rememberRuntimeError("score queue", error);
      console.warn(`📊 Langfuse: ${error.message}`);
      return;
    }

    rt.pendingScores.push(score);
    if (rt.pendingScores.length >= (rt.scoreFlushAt ?? DEFAULT_SCORE_FLUSH_AT)) {
      void startScoreFlush(rt);
    } else {
      scheduleScoreFlush(rt);
    }
  } catch (e) {
    rememberRuntimeError(`score ${name}`, e);
    console.warn(`📊 Langfuse: Failed to send score ${name}`, e);
  }
}
