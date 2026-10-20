export interface ModelInfo {
  raw: string;
  base: string;
  reasoningEffort?: string;
  normalized: string;
}

const MODEL_WITH_EFFORT = /^(.*)\((low|medium|high|xhigh|max|ultra)\)$/i;
const EFFORT_KEYS = ["reasoning_effort", "reasoningEffort", "thinkingLevel", "thinking_level", "effort"];

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function effortFrom(value: unknown): string | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }

  const record = value as Record<string, unknown>;
  for (const key of EFFORT_KEYS) {
    const effort = stringValue(record[key]);
    if (effort) {
      return effort.toLowerCase();
    }
  }
  for (const key of ["reasoning", "thinking"]) {
    const nestedEffort = effortFrom(record[key]);
    if (nestedEffort) {
      return nestedEffort;
    }
  }
  return undefined;
}

function modelFrom(value: unknown): string | undefined {
  if (typeof value === "string") {
    return stringValue(value);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }

  const record = value as Record<string, unknown>;
  return stringValue(record.id) ?? stringValue(record.modelId) ?? stringValue(record.model);
}

/**
 * Pi providers may encode reasoning in the selected model (for example
 * `gpt-5.6-luna(xhigh)`) or pass it separately in their request payload.
 * Normalize both forms to one comparison-friendly model value.
 */
export function resolveModelInfo(...values: unknown[]): ModelInfo {
  const raw = values.map(modelFrom).find((value): value is string => Boolean(value)) ?? "";
  const suffix = raw.match(MODEL_WITH_EFFORT);
  const explicitEffort = values.map(effortFrom).find((value): value is string => Boolean(value));
  const reasoningEffort = explicitEffort ?? suffix?.[2]?.toLowerCase();
  const base = (suffix?.[1] ?? raw).trim();

  return {
    raw,
    base,
    reasoningEffort,
    normalized: base && reasoningEffort ? `${base}(${reasoningEffort})` : base,
  };
}

export function normalizedTraceMetadata(info: ModelInfo): Record<string, string> {
  return {
    harness: "omp",
    ...(info.normalized ? { model: info.normalized } : {}),
    ...(info.base ? { model_base: info.base } : {}),
    model_source: "omp-selected-model",
    ...(info.reasoningEffort ? { reasoning_effort: info.reasoningEffort } : {}),
    reasoning_effort_source: "omp-runtime",
  };
}

export function normalizedTraceTags(info: ModelInfo): string[] {
  return [
    "harness:omp",
    ...(info.normalized ? [`model:${info.normalized}`] : []),
    ...(info.reasoningEffort ? [`reasoning-effort:${info.reasoningEffort}`] : []),
  ];
}
