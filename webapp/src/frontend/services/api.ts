/** API service layer — all backend communication in one place. */

import type { Data, HealthSnapshot, HealthStatus, Conversation, ToolResult, ChatMessage, PendingAction } from '../types'

/* ── constants ─────────────────────────────────────────────── */

export const SERVICE_KEYS = [
  { key: 'web_backend' as const, label: 'Web backend' },
  { key: 'llm_connectivity' as const, label: 'LLM gateway connectivity' },
  { key: 'llm_inference' as const, label: 'LLM test inference' },
  { key: 'mcp_connectivity' as const, label: 'MCP connectivity' },
  { key: 'mcp_discovery' as const, label: 'MCP tool discovery' },
] as const

export const STATUS_LABELS: Record<HealthStatus, string> = {
  healthy: 'Healthy',
  degraded: 'Degraded',
  unavailable: 'Unavailable',
  not_checked: 'Not checked',
}

export const SCENARIOS = [
  { id: 'washing_machine_error', title: 'Washing machine error', prompt: 'My washing machine shows error code E21. What does it mean, and what safe next step should I take?' },
  { id: 'refrigerator_not_cooling', title: 'Refrigerator not cooling', prompt: 'My refrigerator is not cooling. Help me check this safely and tell me when to stop and call a technician.' },
  { id: 'warranty_eligibility', title: 'Warranty eligibility', prompt: 'Can you check whether my washing machine is still under warranty?' },
  { id: 'arrange_appliance_service', title: 'Arrange appliance service', prompt: 'What service options are available for my appliance? Do not book anything without asking me first.' },
  { id: 'previous_appliance_issue', title: 'Previous appliance issue', prompt: 'What happened the last time I reported an issue with my refrigerator?' },
  { id: 'unknown_appliance_model', title: 'Unknown appliance model', prompt: "I need help troubleshooting an appliance, but I don't know its exact model." },
] as const

/* ── helpers ───────────────────────────────────────────────── */

function isHealthStatus(value: unknown): value is HealthStatus {
  return typeof value === 'string' && value in STATUS_LABELS
}

export function readSnapshot(value: unknown): HealthSnapshot {
  if (!value || typeof value !== 'object') throw new Error('Health response was invalid.')
  const response = value as Data
  const normalizedServices: HealthSnapshot['services'] = {}
  if (response.services && typeof response.services === 'object') {
    for (const { key } of SERVICE_KEYS) {
      const item = (response.services as Data)[key]
      if (!item || typeof item !== 'object') continue
      const service = item as Data
      normalizedServices[key] = {
        status: isHealthStatus(service.status) ? service.status : 'not_checked',
        summary: typeof service.summary === 'string' ? service.summary : 'No status detail available.',
        checked_at: typeof service.checked_at === 'string' ? service.checked_at : undefined,
      }
    }
  }
  return {
    overall_status: isHealthStatus(response.overall_status) ? response.overall_status : 'not_checked',
    mode: typeof response.mode === 'string' ? response.mode : 'unknown',
    checked_at: typeof response.checked_at === 'string' ? response.checked_at : undefined,
    services: normalizedServices,
  }
}

export function readMessageResponse(value: unknown) {
  if (!value || typeof value !== 'object') throw new Error('The support response was invalid.')
  const response = value as Data
  const assistant = response.assistant_message
  const user = response.user_message
  if (!assistant || typeof assistant !== 'object' || !user || typeof user !== 'object')
    throw new Error('The support response was invalid.')
  const message = assistant as Data
  const userMessage = user as Data
  if (
    typeof message.message_id !== 'string' ||
    typeof message.content !== 'string' ||
    typeof userMessage.message_id !== 'string' ||
    typeof userMessage.content !== 'string'
  )
    throw new Error('The support response was invalid.')
  const activity = message.activity && typeof message.activity === 'object' ? (message.activity as Data) : undefined
  const toolResults = Array.isArray(message.tool_results)
    ? message.tool_results.flatMap((item, index): ToolResult[] => {
        if (!item || typeof item !== 'object') return []
        const result = item as Data
        if (typeof result.tool_name !== 'string' || typeof result.summary !== 'string') return []
        const provenance = result.provenance && typeof result.provenance === 'object' ? (result.provenance as Data) : undefined
        return [{
          tool_result_id: typeof result.tool_result_id === 'string' ? result.tool_result_id : `tool-${index}`,
          tool_name: result.tool_name,
          status: typeof result.status === 'string' ? result.status : 'unknown',
          summary: result.summary,
          result: result.result,
          error: result.error,
          provenance: provenance ? {
            source_type: typeof provenance.source_type === 'string' ? provenance.source_type : undefined,
            execution_type: typeof provenance.execution_type === 'string' ? provenance.execution_type : undefined,
            source_label: typeof provenance.source_label === 'string' ? provenance.source_label : undefined,
            is_simulated: provenance.is_simulated === true,
          } : undefined,
        }]
      })
    : []
  const structured = message.structured_result && typeof message.structured_result === 'object' ? (message.structured_result as Data) : null
  const display = structured?.display && typeof structured.display === 'object' ? (structured.display as Data) : undefined
  const structuredProvenance = structured?.provenance && typeof structured.provenance === 'object' ? (structured.provenance as Data) : undefined
  const conclusion = message.conclusion && typeof message.conclusion === 'object' ? (message.conclusion as Data) : undefined
  const reasoning = typeof message.reasoning === 'string' && message.reasoning.trim()
    ? message.reasoning.trim()
    : typeof message.reasoning_content === 'string' && message.reasoning_content.trim()
      ? message.reasoning_content.trim()
      : undefined

  return {
    user: { id: userMessage.message_id as string, role: 'user' as const, content: userMessage.content as string },
    assistant: {
      id: message.message_id as string,
      responseId: typeof response.response_id === 'string' ? response.response_id : undefined,
      role: 'assistant' as const,
      content: message.content as string,
      reasoning,
      status: typeof response.status === 'string' ? response.status : typeof activity?.status === 'string' ? activity.status : 'completed',
      provenance: message.provenance && typeof message.provenance === 'object'
        ? { source_type: typeof (message.provenance as Data).source_type === 'string' ? (message.provenance as Data).source_type as string : undefined, is_simulated: (message.provenance as Data).is_simulated === true }
        : undefined,
      activity: activity ? {
        status: typeof activity.status === 'string' ? activity.status : 'unknown',
        summary: typeof activity.summary === 'string' ? activity.summary : undefined,
        events: Array.isArray(activity.events)
          ? activity.events.filter((event): event is Data => !!event && typeof event === 'object').map((event) => ({
              sequence: typeof event.sequence === 'number' ? event.sequence : undefined,
              label: typeof event.label === 'string' ? event.label : undefined,
              status: typeof event.status === 'string' ? event.status : undefined,
              result_summary: typeof event.result_summary === 'string' ? event.result_summary : undefined,
            }))
          : [],
      } : undefined,
      toolResults,
      structuredResult: structured ? {
        type: typeof structured.type === 'string' ? structured.type : undefined,
        status: typeof structured.status === 'string' ? structured.status : undefined,
        display: display ? {
          title: typeof display.title === 'string' ? display.title : undefined,
          summary: typeof display.summary === 'string' ? display.summary : undefined,
          disclaimer: typeof display.disclaimer === 'string' ? display.disclaimer : undefined,
        } : undefined,
        payload: structured.payload,
        provenance: structuredProvenance ? {
          source_type: typeof structuredProvenance.source_type === 'string' ? structuredProvenance.source_type : undefined,
          source_label: typeof structuredProvenance.source_label === 'string' ? structuredProvenance.source_label : undefined,
          is_simulated: structuredProvenance.is_simulated === true,
        } : undefined,
      } : null,
      canFallbackToDeterministic:
        message.can_fallback_to_deterministic === true || conclusion?.can_fallback_to_deterministic === true,
      conclusion: conclusion ? {
        status: typeof conclusion.status === 'string' ? conclusion.status : undefined,
        summary: typeof conclusion.summary === 'string' ? conclusion.summary : undefined,
        next_action: typeof conclusion.next_action === 'string' ? conclusion.next_action : undefined,
        requires_user_confirmation: conclusion.requires_user_confirmation === true,
        pending_action: conclusion.pending_action && typeof conclusion.pending_action === 'object'
          ? (conclusion.pending_action as PendingAction)
          : undefined,
        can_fallback_to_deterministic: conclusion.can_fallback_to_deterministic === true,
        original_prompt: typeof conclusion.original_prompt === 'string' ? conclusion.original_prompt : undefined,
      } : undefined,
    } as ChatMessage,
  }
}

export function createId(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`
}

export function formatTime(timestamp?: string) {
  if (!timestamp) return 'Not checked yet'
  const d = new Date(timestamp)
  return Number.isNaN(d.valueOf()) ? 'Not checked yet' : d.toLocaleString()
}

export function resultIsSimulated(result: ToolResult) {
  return result.provenance?.is_simulated === true || (!!result.result && typeof result.result === 'object' && (result.result as Data).is_simulated === true)
}

export function resultIsDemo(result: ToolResult) {
  return result.provenance?.source_type === 'deterministic_demo'
}

export function stringify(value: unknown) {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? ''
}

export function responseMode(message: ChatMessage) {
  if (message.provenance?.source_type === 'deterministic') return 'Deterministic response'
  if (message.toolResults?.some(resultIsDemo) || message.structuredResult?.provenance?.source_type === 'deterministic_demo') return 'Scripted demo response'
  return null
}

/* ── API calls ─────────────────────────────────────────────── */

export async function fetchHealth(recheck = false): Promise<HealthSnapshot> {
  const response = await fetch(recheck ? '/api/health/recheck' : '/api/health', {
    method: recheck ? 'POST' : 'GET',
    headers: recheck ? { Accept: 'application/json' } : undefined,
  })
  if (!response.ok) throw new Error('The web backend could not provide a health update.')
  return readSnapshot(await response.json())
}

export async function fetchConversations(): Promise<Conversation[]> {
  const response = await fetch('/api/conversations', { headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error('Conversation history could not be loaded.')
  const value = (await response.json()) as { conversations?: Conversation[] }
  return Array.isArray(value.conversations) ? value.conversations : []
}

export async function fetchConversationHistory(id: string) {
  const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`, { headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error('Conversation history could not be restored.')
  const history = (await response.json()) as { responses?: unknown[] }
  const restored: ChatMessage[] = []
  for (const item of Array.isArray(history.responses) ? history.responses : []) {
    const pair = readMessageResponse(item)
    restored.push(pair.user, pair.assistant)
  }
  return restored
}

export async function createNewConversation(): Promise<string> {
  const response = await fetch('/api/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: '{}',
  })
  if (!response.ok) throw new Error('A new conversation could not be created.')
  const created = (await response.json()) as { conversation_id: string }
  return created.conversation_id
}

export async function sendMessageToBackend(conversationId: string, clientMessageId: string, content: string, metadata?: Record<string, unknown>) {
  const payload: Record<string, unknown> = { conversation_id: conversationId, client_message_id: clientMessageId, content }
  if (metadata) payload.metadata = metadata
  const response = await fetch('/api/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('Support is temporarily unavailable.')
  return readMessageResponse(await response.json())
}

export type StreamCallback = (update: {
  deltaContent?: string
  deltaReasoning?: string
  toolEvent?: { tool_name: string; label?: string; status?: string; summary?: string }
  status?: string
}) => void

export async function sendMessageStreaming(
  conversationId: string,
  clientMessageId: string,
  content: string,
  onStreamUpdate?: StreamCallback,
  metadata?: Record<string, unknown>,
): Promise<ReturnType<typeof readMessageResponse>> {
  let response: Response
  const payload: Record<string, unknown> = { conversation_id: conversationId, client_message_id: clientMessageId, content }
  if (metadata) payload.metadata = metadata
  try {
    response = await fetch('/api/messages/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(payload),
    })
  } catch {
    return sendMessageToBackend(conversationId, clientMessageId, content, metadata)
  }

  if (!response.ok || !response.body) {
    return sendMessageToBackend(conversationId, clientMessageId, content, metadata)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finalResponse: unknown = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''

    let currentEvent = ''
    for (const rawLine of lines) {
      const line = rawLine.trim()
      if (!line) {
        currentEvent = ''
        continue
      }
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7).trim()
        continue
      }
      if (line.startsWith('data: ')) {
        const dataStr = line.slice(6).trim()
        if (currentEvent === 'done') {
          try {
            finalResponse = JSON.parse(dataStr)
          } catch {
            // ignore
          }
          continue
        }
        try {
          const parsed = JSON.parse(dataStr) as { type?: string; data?: unknown }
          const eventType = currentEvent || parsed.type || ''
          const dataPayload = parsed.data !== undefined ? parsed.data : parsed
          if (eventType === 'reasoning') {
            const delta = typeof dataPayload === 'string' ? dataPayload : ''
            onStreamUpdate?.({ deltaReasoning: delta })
          } else if (eventType === 'content') {
            const delta = typeof dataPayload === 'string' ? dataPayload : ''
            onStreamUpdate?.({ deltaContent: delta })
          } else if (eventType === 'tool_start' || eventType === 'tool_done') {
            const toolData = dataPayload as Record<string, string>
            onStreamUpdate?.({ toolEvent: { tool_name: toolData.tool_name ?? 'tool', label: toolData.label, status: toolData.status, summary: toolData.summary } })
          } else if (eventType === 'status') {
            const statusData = dataPayload as Record<string, string>
            onStreamUpdate?.({ status: statusData.status })
          }
        } catch {
          // ignore
        }
      }
    }
  }

  if (finalResponse) {
    return readMessageResponse(finalResponse)
  }
  return sendMessageToBackend(conversationId, clientMessageId, content, metadata)
}
