/** Shared type definitions for Alexa frontend. */

export type HealthStatus = 'healthy' | 'degraded' | 'unavailable' | 'not_checked'
export type ServiceKey = 'web_backend' | 'llm_connectivity' | 'llm_inference' | 'mcp_connectivity' | 'mcp_discovery'
export type HealthService = { status: HealthStatus; summary: string; checked_at?: string }
export type HealthSnapshot = {
  overall_status: HealthStatus
  mode: string
  checked_at?: string
  services: Partial<Record<ServiceKey, HealthService>>
}

export type Data = Record<string, unknown>

export type Provenance = {
  source_type?: string
  execution_type?: string
  source_label?: string
  is_simulated?: boolean
}

export type ToolResult = {
  tool_result_id: string
  tool_name: string
  status: string
  summary: string
  result?: unknown
  error?: unknown
  provenance?: Provenance
}

export type Activity = {
  status: string
  summary?: string
  events: Array<{
    sequence?: number
    label?: string
    status?: string
    result_summary?: string
  }>
}

export type StructuredResult = {
  type?: string
  status?: string
  display?: {
    title?: string
    summary?: string
    disclaimer?: string
  }
  payload?: unknown
  provenance?: Provenance
}

export type PendingAction = {
  action_type: string
  confirmation_id?: string
  appliance_id?: string
  brand?: string
  model_number?: string
  symptom?: string
  is_simulated?: boolean
}

export type OrchestrationMode = 'live' | 'deterministic'

export type Conclusion = {
  status?: string
  summary?: string
  next_action?: string
  requires_user_confirmation?: boolean
  pending_action?: PendingAction
  can_fallback_to_deterministic?: boolean
  original_prompt?: string
}

export type ChatMessage = {
  id: string
  responseId?: string
  role: 'user' | 'assistant'
  content: string
  reasoning?: string
  status?: string
  provenance?: Provenance
  activity?: Activity
  toolResults?: ToolResult[]
  structuredResult?: StructuredResult | null
  conclusion?: Conclusion
  canFallbackToDeterministic?: boolean
}

export type Conversation = {
  conversation_id: string
  created_at: string
  response_count: number
}
