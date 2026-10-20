import { appendFileSync } from "node:fs"

const path = "/Users/yo_macbook/Documents/random/work/omp-langfuse-events.jsonl"

function safe(value: unknown) {
  try {
    return JSON.parse(JSON.stringify(value, (_key, item) => {
      if (typeof item === "string" && item.length > 300) return `${item.slice(0, 300)}…`
      return item
    }))
  } catch {
    return "[unserializable]"
  }
}

export default function (pi: any) {
  const events = [
    "session_start", "model_select", "before_agent_start", "agent_start",
    "turn_start", "before_provider_request", "after_provider_response",
    "message_update", "message_end", "tool_execution_start", "tool_call",
    "tool_result", "tool_execution_end", "turn_end", "agent_end", "session_shutdown",
  ]
  for (const name of events) {
    pi.on(name, (event: unknown, ctx: unknown) => {
      appendFileSync(path, JSON.stringify({ name, event: safe(event), ctx: safe(ctx) }) + "\n")
    })
  }
}
