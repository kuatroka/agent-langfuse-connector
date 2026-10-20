import type { PluginAPI, ToolCall, ToolResult } from '@ampcode/plugin'

export const description = 'Exports opted-in Amp turns and tool activity to Langfuse via OTLP.'

const KEYCHAIN_SERVICE = 'amp-langfuse'
const MAX_VALUE_CHARS = 20_000
const USAGE_TIMEOUT_MS = (() => {
	const override = Number(process.env.AMP_LANGFUSE_USAGE_TIMEOUT_MS)
	return Number.isFinite(override) && override > 0 ? override : 15_000
})()
const USAGE_POLL_INTERVAL_MS = 5_000

// Current standard-routing details from https://ampcode.com/modes.
// Amp may change this routing or override it for connected provider subscriptions.
const BUILTIN_MODE_DETAILS: Record<string, { model: string; reasoningEffort: string }> = {
	low: { model: 'GLM-5.2', reasoningEffort: 'med' },
	medium: { model: 'GPT-5.6 Sol', reasoningEffort: 'med' },
	high: { model: 'GPT-5.6 Sol', reasoningEffort: 'xhigh' },
	ultra: { model: 'Fable 5', reasoningEffort: 'high' },
}
type Credentials = {
	baseUrl: string
	publicKey: string
	secretKey: string
}

type ActiveTurn = {
	startedAt: bigint
	prompt: string
	messageID: string
	mode?: string
	model?: string
	modelSource?: 'configured-agent' | 'inferred-mode-routing' | 'thread-export'
	reasoningEffort?: string
	reasoningEffortSource?: 'configured-agent' | 'inferred-mode-routing' | 'thread-export'
	tools: Map<string, { call: ToolCall; startedAt: bigint; result?: ToolResult; endedAt?: bigint }>
}

type TurnUsage = {
	input: number
	output: number
	total: number
	cache_read_input_tokens?: number
	cache_creation_input_tokens?: number
}

type ExportedTurnDetails = {
	model?: string
	reasoningEffort?: string
	reasoningEffortSource?: 'configured-agent' | 'thread-export'
	usage?: TurnUsage
	usageComplete?: boolean
	usageCountedMessages?: number
	usageExpectedMessages?: number
	turnNumber?: number
}

type OtlpAttribute = {
	key: string
	value: Record<string, unknown>
}

type AgentDefinitionDetails = {
	kind: string
	mode?: string
	model?: string
	name?: string
	reasoningEffort?: string
	display?: { label: string }
}

const turns = new Map<string, ActiveTurn>()

function turnKey(threadID: string, messageID: string | number) {
	return `${threadID}:${messageID}`
}

function nowNs() {
	return BigInt(Date.now()) * 1_000_000n
}

function randomHex(bytes: number) {
	const value = new Uint8Array(bytes)
	crypto.getRandomValues(value)
	return Array.from(value, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function truncate(value: string) {
	return value.length <= MAX_VALUE_CHARS
		? value
		: `${value.slice(0, MAX_VALUE_CHARS)}… [truncated; original length ${value.length}]`
}

function json(value: unknown) {
	try {
		return truncate(JSON.stringify(value))
	} catch {
		return '"[unserializable value]"'
	}
}

function textFromMessages(messages: Array<{ role: string; content: Array<{ type: string; text?: string }> }>) {
	const assistant = messages.filter((message) => message.role === 'assistant').at(-1)
	if (!assistant) return ''
	return assistant.content
		.filter((block) => block.type === 'text' && typeof block.text === 'string')
		.map((block) => block.text)
		.join('\n')
}

function stringAttribute(key: string, value: string): OtlpAttribute {
	return { key, value: { stringValue: value } }
}

function jsonAttribute(key: string, value: unknown): OtlpAttribute {
	return stringAttribute(key, json(value))
}

function arrayAttribute(key: string, values: string[]): OtlpAttribute {
	return { key, value: { arrayValue: { values: values.map((value) => ({ stringValue: value })) } } }
}

function modelLabel(model?: string, reasoningEffort?: string) {
	if (!model) return undefined
	return reasoningEffort ? `${model}(${reasoningEffort})` : model
}

function modelName(model?: string) {
	return model?.split('/').at(-1)
}

// Amp's exported agent definition can omit reasoningEffort for custom modes.
// Recover it from the configured model variant or the mode label instead.
function effortFromModel(model?: string) {
	if (!model) return undefined
	const match = model.match(/[-(](none|minimal|low|medium|high|xhigh|max|ultra)\)?(?:$|[-_/])/i)
	return match?.[1]?.toLowerCase()
}

function effortFromLabel(label?: string) {
	if (!label) return undefined
	const match = label.match(/\b(none|minimal|low|medium|high|xhigh|max|ultra)\b/i)
	return match?.[1]?.toLowerCase()
}

async function agentDetails(thread: {
	agent: () => Promise<{
		definition: AgentDefinitionDetails
	}>
}) {
	try {
		const { definition } = await thread.agent()
		const isBuiltin = definition.kind === 'builtin-agent'
		const mode = definition.mode || definition.display?.label || definition.name
		const builtin = isBuiltin && mode ? BUILTIN_MODE_DETAILS[mode] : undefined
		const modelEffort = effortFromModel(definition.model)
		const labelEffort = effortFromLabel(mode)
		const reasoningEffort = definition.reasoningEffort || builtin?.reasoningEffort || modelEffort || labelEffort
		const reasoningEffortSource: ActiveTurn['reasoningEffortSource'] =
			definition.reasoningEffort ? 'configured-agent'
			: builtin?.reasoningEffort ? 'inferred-mode-routing'
			: modelEffort ? 'configured-agent'
			: labelEffort ? 'inferred-mode-routing'
			: undefined
		return {
			mode,
			modelSource: definition.model ? 'configured-agent' : 'inferred-mode-routing',
			model: definition.model || builtin?.model,
			reasoningEffort,
			reasoningEffortSource,
		}
	} catch {
		return {}
	}
}

async function turnDetailsFromThreadExport(amp: PluginAPI, threadID: string, messageID: string, expectedAssistantMessages: number): Promise<ExportedTurnDetails> {
	let latest: ExportedTurnDetails = {}
	const deadline = Date.now() + USAGE_TIMEOUT_MS
	for (;;) {
		try {
			const result = await amp.$`amp threads export ${threadID}`
			if (result.exitCode === 0) {
				const exported = JSON.parse(result.stdout) as {
					meta?: {
						agent?: { model?: unknown; reasoningEffort?: unknown; mode?: unknown; display?: { label?: unknown } }
						threadAgent?: { definition?: { model?: unknown; reasoningEffort?: unknown; mode?: unknown; display?: { label?: unknown } } }
					}
					messages?: Array<{
						role?: unknown
						messageId?: unknown
						protocolMessageID?: unknown
						content?: Array<{ type?: unknown; text?: unknown }> | unknown
						usage?: {
							model?: unknown
							inputTokens?: unknown
							outputTokens?: unknown
							totalInputTokens?: unknown
							cacheReadInputTokens?: unknown
							cacheCreationInputTokens?: unknown
						}
					}>
				}
				const messages = exported.messages || []
				const startIndex = messages.findIndex((message) =>
					String(message.messageId) === messageID || String(message.protocolMessageID) === messageID
				)
				// A user-authored prompt is a `role: "user"` message carrying a text
				// block and no `tool_result` block (tool results also arrive as
				// user-role messages). Count prompts up to and including the one
				// that opened this turn to derive the 1-based turn number. When
				// the message content shape is unavailable, fall back to counting
				// user messages that are not identifiable tool results.
				const isUserPrompt = (message: (typeof messages)[number]) => {
					if (message.role !== 'user') return false
					const content = message.content
					if (!Array.isArray(content)) return true
					const hasText = content.some((block) => block?.type === 'text' && typeof block?.text === 'string')
					const hasToolResult = content.some((block) => block?.type === 'tool_result' || block?.type === 'toolResult')
					return hasText && !hasToolResult
				}
				const turnNumber = startIndex >= 0
					? messages.slice(0, startIndex + 1).filter(isUserPrompt).length
					: undefined
				const assistantMessages = startIndex >= 0
					? messages.slice(startIndex + 1).filter((message) => message.role === 'assistant')
					: expectedAssistantMessages > 0
						? messages.filter((message) => message.role === 'assistant').slice(-expectedAssistantMessages)
						: []
				const usageMessages = assistantMessages.filter((message) => message.usage)
				const lastUsage = usageMessages.at(-1)?.usage
				const agent = exported.meta?.agent || exported.meta?.threadAgent?.definition
				const configuredEffort = typeof agent?.reasoningEffort === 'string' ? agent.reasoningEffort : undefined
				const configuredModel = typeof agent?.model === 'string' ? agent.model : undefined
				const exportedMode = typeof agent?.mode === 'string'
					? agent.mode
					: typeof agent?.display?.label === 'string'
						? agent.display.label
						: undefined
				const resolvedModel = typeof lastUsage?.model === 'string' ? lastUsage.model : configuredModel
				const inferredEffort = configuredEffort || effortFromModel(configuredModel) || effortFromLabel(exportedMode) || effortFromModel(resolvedModel)
				const number = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0
					const input = usageMessages.reduce((sum, message) => sum + number(message.usage?.inputTokens), 0)
					const output = usageMessages.reduce((sum, message) => sum + number(message.usage?.outputTokens), 0)
					const cacheRead = usageMessages.reduce((sum, message) => sum + number(message.usage?.cacheReadInputTokens), 0)
					const cacheCreation = usageMessages.reduce((sum, message) => sum + number(message.usage?.cacheCreationInputTokens), 0)
					latest = {
						model: resolvedModel,
						reasoningEffort: inferredEffort,
						reasoningEffortSource: inferredEffort ? 'thread-export' : undefined,
						usage: usageMessages.length > 0 ? {
							input,
							output,
							total: input + output + cacheRead + cacheCreation,
							...(cacheRead > 0 ? { cache_read_input_tokens: cacheRead } : {}),
							...(cacheCreation > 0 ? { cache_creation_input_tokens: cacheCreation } : {}),
						} : undefined,
						usageComplete: assistantMessages.length >= expectedAssistantMessages
							&& usageMessages.length === assistantMessages.length,
						usageCountedMessages: usageMessages.length,
						usageExpectedMessages: Math.max(expectedAssistantMessages, assistantMessages.length),
						turnNumber,
					}
					if (latest.usageComplete) return latest
				}
		} catch (error) {
			amp.logger.log(`Could not read the resolved Amp model: ${error instanceof Error ? error.message : String(error)}`)
		}
		if (Date.now() >= deadline) break
		await new Promise((resolve) => setTimeout(resolve, USAGE_POLL_INTERVAL_MS))
	}
	amp.logger.log(`Amp usage incomplete: counted ${latest.usageCountedMessages ?? 0} of ${latest.usageExpectedMessages ?? expectedAssistantMessages} assistant messages`)
	return latest
}

async function keychainValue(amp: PluginAPI, account: string) {
	const result = await amp.$`security find-generic-password -s ${KEYCHAIN_SERVICE} -a ${account} -w`
	return result.exitCode === 0 ? result.stdout.trim() : undefined
}

async function credentials(amp: PluginAPI): Promise<Credentials | undefined> {
	const [publicKey, secretKey, configuredBaseUrl] = await Promise.all([
		keychainValue(amp, 'public-key'),
		keychainValue(amp, 'secret-key'),
		keychainValue(amp, 'base-url'),
	])

	if (!publicKey || !secretKey) return undefined
	const baseUrl = (configuredBaseUrl || 'https://cloud.langfuse.com').replace(/\/$/, '')
	return { baseUrl, publicKey, secretKey }
}

async function exportTrace(amp: PluginAPI, trace: Record<string, unknown>) {
	try {
		const encoded = btoa(Array.from(new TextEncoder().encode(JSON.stringify(trace)), byte => String.fromCharCode(byte)).join(''))
		const local = await amp.$`/Users/yo_macbook/.local/bin/agent-eval ingest --otel --base64 ${encoded}`
		if (local.exitCode !== 0) amp.logger.log(`AgentEval local capture failed: ${local.stderr.slice(-500)}`)
	} catch { amp.logger.log('AgentEval local capture unavailable; remote backfill remains enabled.') }
	const config = await credentials(amp)
	if (!config) {
		amp.logger.log('Langfuse export skipped: credentials are not configured in the macOS Keychain.')
		return { ok: false, reason: 'credentials are not configured' }
	}

	const authorization = `Basic ${btoa(`${config.publicKey}:${config.secretKey}`)}`
	try {
		const response = await fetch(`${config.baseUrl}/api/public/otel/v1/traces`, {
			method: 'POST',
			headers: {
				Authorization: authorization,
				'Content-Type': 'application/json',
				'x-langfuse-ingestion-version': '4',
			},
			body: JSON.stringify({ resourceSpans: [trace] }),
		})
		if (!response.ok) {
			const detail = truncate(await response.text())
			amp.logger.log(`Langfuse export failed (${response.status}): ${detail}`)
			return { ok: false, reason: `Langfuse returned HTTP ${response.status}` }
		}
		return { ok: true }
	} catch (error) {
		amp.logger.log(`Langfuse export failed: ${error instanceof Error ? error.message : String(error)}`)
		return { ok: false, reason: 'network request failed' }
	}
}

function resourceTrace(spans: Record<string, unknown>[]) {
	return {
		resource: { attributes: [stringAttribute('service.name', 'amp')] },
		scopeSpans: [{ scope: { name: 'amp-langfuse-plugin', version: '1.0.0' }, spans }],
	}
}

function makeTurnTrace(threadID: string, prompt: string, output: string, startedAt: bigint, endedAt: bigint, tools: ActiveTurn['tools'], agent: Pick<ActiveTurn, 'mode' | 'model' | 'modelSource' | 'reasoningEffort' | 'reasoningEffortSource'> = {}, usage?: TurnUsage, usageComplete = true, usageCountedMessages?: number, usageExpectedMessages?: number, turnNumber?: number) {
	const traceID = randomHex(16)
	const rootSpanID = randomHex(8)
	const model = modelLabel(agent.model, agent.reasoningEffort)
	const tagModel = modelLabel(modelName(agent.model), agent.reasoningEffort)
	const traceName = turnNumber !== undefined ? `Amp - Turn ${turnNumber}` : 'Amp turn'
	const tags = ['harness:amp']
	if (agent.mode) tags.push(`amp-mode:${agent.mode}`)
	if (tagModel) tags.push(`model:${tagModel}`)
	if (agent.reasoningEffort) tags.push(`reasoning-effort:${agent.reasoningEffort}`)
	if (!usageComplete) tags.push('usage:incomplete')
	const traceContext = [
		stringAttribute('langfuse.trace.name', traceName),
		stringAttribute('langfuse.session.id', threadID),
		arrayAttribute('langfuse.trace.tags', tags),
		stringAttribute('langfuse.trace.metadata.harness', 'amp'),
		...(agent.mode ? [stringAttribute('langfuse.trace.metadata.mode', agent.mode)] : []),
		...(model ? [stringAttribute('langfuse.trace.metadata.model', model)] : []),
		...(agent.model ? [stringAttribute('langfuse.trace.metadata.model_base', agent.model)] : []),
		...(agent.modelSource ? [stringAttribute('langfuse.trace.metadata.model_source', agent.modelSource)] : []),
		...(agent.reasoningEffort ? [stringAttribute('langfuse.trace.metadata.reasoning_effort', agent.reasoningEffort)] : []),
		...(agent.reasoningEffortSource ? [stringAttribute('langfuse.trace.metadata.reasoning_effort_source', agent.reasoningEffortSource)] : []),
		stringAttribute('langfuse.trace.metadata.usage_status', usageComplete ? 'complete' : 'incomplete'),
		...(!usageComplete && usageCountedMessages !== undefined ? [jsonAttribute('langfuse.trace.metadata.usage_counted_messages', usageCountedMessages)] : []),
		...(!usageComplete && usageExpectedMessages !== undefined ? [jsonAttribute('langfuse.trace.metadata.usage_expected_messages', usageExpectedMessages)] : []),
		...(turnNumber !== undefined ? [jsonAttribute('langfuse.trace.metadata.turn_number', turnNumber)] : []),
	]
	const rootAttributes = [
		...traceContext,
		stringAttribute('langfuse.observation.type', 'span'),
		jsonAttribute('langfuse.observation.input', { role: 'user', content: truncate(prompt) }),
		jsonAttribute('langfuse.observation.output', { role: 'assistant', content: truncate(output) }),
	]
	const root = {
		traceId: traceID,
		spanId: rootSpanID,
		name: traceName,
		kind: 1,
		startTimeUnixNano: startedAt.toString(),
		endTimeUnixNano: endedAt.toString(),
		attributes: rootAttributes,
		status: { code: 1 },
	}
	const generation = {
		traceId: traceID,
		spanId: randomHex(8),
		parentSpanId: rootSpanID,
		name: 'Amp Generation',
		kind: 1,
		startTimeUnixNano: startedAt.toString(),
		endTimeUnixNano: endedAt.toString(),
		attributes: [
			...traceContext,
			stringAttribute('langfuse.observation.type', 'generation'),
			jsonAttribute('langfuse.observation.input', { role: 'user', content: truncate(prompt) }),
			jsonAttribute('langfuse.observation.output', { role: 'assistant', content: truncate(output) }),
			...(agent.model ? [stringAttribute('langfuse.observation.model.name', agent.model)] : []),
			...(usage ? [jsonAttribute('langfuse.observation.usage_details', usage)] : []),
			...(agent.reasoningEffort ? [stringAttribute('langfuse.observation.metadata.reasoning_effort', agent.reasoningEffort)] : []),
			...(agent.reasoningEffortSource ? [stringAttribute('langfuse.observation.metadata.reasoning_effort_source', agent.reasoningEffortSource)] : []),
		],
		status: { code: 1 },
	}
	const toolSpans = Array.from(tools.values()).map((tool) => ({
		traceId: traceID,
		spanId: randomHex(8),
		parentSpanId: rootSpanID,
		name: `Tool: ${tool.call.tool}`,
		kind: 1,
		startTimeUnixNano: tool.startedAt.toString(),
		endTimeUnixNano: (tool.endedAt || endedAt).toString(),
		attributes: [
			...traceContext,
			stringAttribute('langfuse.observation.type', 'tool'),
			jsonAttribute('langfuse.observation.input', tool.call.input),
			jsonAttribute('langfuse.observation.output', tool.result?.output),
			stringAttribute('langfuse.observation.metadata.tool_name', tool.call.tool),
			stringAttribute('langfuse.observation.metadata.status', tool.result?.status || 'unknown'),
		],
		status: { code: tool.result?.status === 'error' ? 2 : 1, message: tool.result?.error || '' },
	}))

	return resourceTrace([root, generation, ...toolSpans])
}

export default function (amp: PluginAPI) {
	amp.on('agent.start', async (event, ctx) => {
		const agent = await agentDetails(ctx.thread)
		turns.set(turnKey(event.thread.id, event.id), {
			startedAt: nowNs(),
			prompt: event.message,
			messageID: String(event.id),
			...agent,
			tools: new Map(),
		})
	})

	amp.on('tool.call', (event) => {
		const active = Array.from(turns.entries()).find(([key]) => key.startsWith(`${event.thread.id}:`))?.[1]
		if (active) active.tools.set(event.toolUseID, { call: event, startedAt: nowNs() })
		return { action: 'allow' }
	})

	amp.on('tool.result', (event) => {
		const active = Array.from(turns.entries()).find(([key]) => key.startsWith(`${event.thread.id}:`))?.[1]
		const tool = active?.tools.get(event.toolUseID)
		if (tool) {
			tool.result = event
			tool.endedAt = nowNs()
		}
	})

	amp.on('agent.end', async (event) => {
		const key = turnKey(event.thread.id, event.id)
		const active = turns.get(key)
		turns.delete(key)
		if (!active) return
		const endedAt = nowNs()
		const output = textFromMessages(event.messages)
		const expectedAssistantMessages = event.messages.filter((message) => message.role === 'assistant').length
		const exported = await turnDetailsFromThreadExport(amp, event.thread.id, active.messageID, expectedAssistantMessages)
		const traceAgent = {
			...active,
			...(exported.model ? { model: exported.model, modelSource: 'thread-export' as const } : {}),
			...(exported.reasoningEffort ? {
				reasoningEffort: exported.reasoningEffort,
				reasoningEffortSource: exported.reasoningEffortSource,
			} : {}),
		}
		await exportTrace(amp, makeTurnTrace(event.thread.id, active.prompt, output, active.startedAt, endedAt, active.tools, traceAgent, exported.usage, exported.usageComplete ?? false, exported.usageCountedMessages, exported.usageExpectedMessages, exported.turnNumber))
	})

	amp.registerCommand('evaluate', { title: 'Evaluate coding result', category: 'Evaluation',
		description: 'Open AgentEval to rate a completed turn or session.' }, async () => {
		await amp.$`/Users/yo_macbook/.local/bin/agent-eval open --harness amp`
	})
	amp.registerCommand(
		'test-langfuse-connection',
		{
			title: 'Test Langfuse connection',
			category: 'Langfuse',
			description: 'Export a safe test trace and confirm Langfuse connectivity.',
		},
		async (ctx) => {
			const now = nowNs()
			const result = await exportTrace(amp, makeTurnTrace('amp-langfuse-test', 'Connection test', 'Connection test succeeded', now, now + 1_000_000n, new Map()))
			await ctx.ui.notify(result.ok
				? 'Langfuse connection succeeded. Open Langfuse and search for the “Amp turn” trace in session amp-langfuse-test.'
				: `Langfuse connection failed: ${result.reason}. See the plugin log for details.`)
		},
	)
}
