import {
  MAX_ARRAY_ITEMS,
  MAX_DEPTH,
  MAX_OBJECT_KEYS,
  MAX_PAYLOAD_NODES,
  MAX_STRING_LENGTH,
  MAX_TOOL_PAYLOAD_LENGTH,
} from "./constants.js";
import type { EnvLike } from "./capture-policy.js";
import { state } from "./state.js";

/**
 * Payload-shaping limits. Every field is a positive integer, or
 * `Number.POSITIVE_INFINITY` to disable that limit entirely (capture everything).
 * Resolved once from the environment and stored on the loaded config; consumers
 * read the resolved values via `getLimits()` rather than the raw constants.
 */
export interface PayloadLimits {
  /** Max characters kept per captured string (generation/agent inputs, outputs, system prompt). */
  readonly maxString: number;
  /** Max characters kept for tool inputs/outputs (their payloads run larger than chat strings). */
  readonly maxToolPayload: number;
  /** Max nesting depth walked when shaping a structured payload. */
  readonly maxDepth: number;
  /** Max array elements kept per array. */
  readonly maxArrayItems: number;
  /** Max own-keys kept per object. */
  readonly maxObjectKeys: number;
  /** Max total nodes visited across a whole payload before bailing with `[payload too large]`. */
  readonly maxNodes: number;
}

export const DEFAULT_LIMITS: PayloadLimits = {
  maxString: MAX_STRING_LENGTH,
  maxToolPayload: MAX_TOOL_PAYLOAD_LENGTH,
  maxDepth: MAX_DEPTH,
  maxArrayItems: MAX_ARRAY_ITEMS,
  maxObjectKeys: MAX_OBJECT_KEYS,
  maxNodes: MAX_PAYLOAD_NODES,
};

/** Words that mean "no limit" when supplied as an env value. */
const UNLIMITED_WORDS = new Set(["off", "none", "false", "no", "unlimited", "inf", "infinity"]);

/**
 * Parse one limit env value.
 * - unset / blank / unparseable  -> `fallback` (the built-in default)
 * - "off"/"none"/"unlimited"/... or a value <= 0 -> `Infinity` (limit removed)
 * - a positive number -> that integer
 */
export function parseLimit(raw: string | undefined, fallback: number): number {
  if (raw === undefined) {
    return fallback;
  }
  const trimmed = raw.trim().toLowerCase();
  if (trimmed === "") {
    return fallback;
  }
  if (UNLIMITED_WORDS.has(trimmed)) {
    return Number.POSITIVE_INFINITY;
  }
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  if (parsed <= 0) {
    return Number.POSITIVE_INFINITY;
  }
  return Math.floor(parsed);
}

/**
 * Resolve payload limits from the environment. Each `PI_LANGFUSE_MAX_*` var overrides
 * the corresponding default; set any to `0`/`off`/`unlimited` to remove that limit.
 * Namespaced `PI_LANGFUSE_*` (not `LANGFUSE_*`) to avoid clashing with Langfuse
 * server env vars such as `LANGFUSE_MAX_EVENT_SIZE_BYTES`.
 */
export function createPayloadLimits(env: EnvLike = process.env as EnvLike): PayloadLimits {
  return {
    maxString: parseLimit(env.PI_LANGFUSE_MAX_STRING_LENGTH, DEFAULT_LIMITS.maxString),
    maxToolPayload: parseLimit(env.PI_LANGFUSE_MAX_TOOL_PAYLOAD_LENGTH, DEFAULT_LIMITS.maxToolPayload),
    maxDepth: parseLimit(env.PI_LANGFUSE_MAX_DEPTH, DEFAULT_LIMITS.maxDepth),
    maxArrayItems: parseLimit(env.PI_LANGFUSE_MAX_ARRAY_ITEMS, DEFAULT_LIMITS.maxArrayItems),
    maxObjectKeys: parseLimit(env.PI_LANGFUSE_MAX_OBJECT_KEYS, DEFAULT_LIMITS.maxObjectKeys),
    maxNodes: parseLimit(env.PI_LANGFUSE_MAX_PAYLOAD_NODES, DEFAULT_LIMITS.maxNodes),
  };
}

/**
 * Resolved limits for the current session: the config-loaded values when a
 * config is active, otherwise a fresh resolve from the environment. Every
 * capture/redaction path reads limits through this so a single env change
 * (or config) governs truncation everywhere.
 */
export function getLimits(): PayloadLimits {
  return state.config?.limits ?? createPayloadLimits();
}
