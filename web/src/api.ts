import { logError, logInfo, previewText, nextTraceId } from './logger'

const API = '/api'

export interface Message {
  role: 'user' | 'assistant'
  content: string
  timestamp?: string
}

export interface SystemStatus {
  initialized: boolean
  subsystems: Record<string, string>
  working_memory_size: number
  default_local_model: string
  frontier_model: string
  telegram_enabled: boolean
  scheduler?: {
    running: boolean
    jobs: string[]
    last_brief: string | null
    last_review: string | null
  }
}

export interface Commitment {
  id: number
  content: string
  importance_score: number
  created_at: string
}

function getBodyPreview(body: RequestInit['body']) {
  if (typeof body !== 'string') {
    return undefined
  }

  return previewText(body, 220)
}

async function req<T>(path: string, options?: RequestInit): Promise<T> {
  const requestId = nextTraceId('api')
  const method = options?.method ?? 'GET'
  const startedAt = performance.now()

  logInfo('api', 'request_started', {
    requestId,
    method,
    path,
    bodyPreview: getBodyPreview(options?.body),
  })

  let res: Response

  try {
    res = await fetch(`${API}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    })
  } catch (error) {
    logError('api', 'request_network_failed', {
      requestId,
      method,
      path,
      durationMs: Math.round(performance.now() - startedAt),
      error,
    })
    throw error
  }

  const rawText = await res.text()
  const durationMs = Math.round(performance.now() - startedAt)

  if (!res.ok) {
    logError('api', 'request_failed', {
      requestId,
      method,
      path,
      status: res.status,
      statusText: res.statusText,
      durationMs,
      responsePreview: previewText(rawText, 220),
    })
    throw new Error(`${res.status} ${res.statusText}`)
  }

  logInfo('api', 'request_succeeded', {
    requestId,
    method,
    path,
    status: res.status,
    durationMs,
    responsePreview: previewText(rawText, 220),
  })

  try {
    return JSON.parse(rawText) as T
  } catch (error) {
    logError('api', 'response_parse_failed', {
      requestId,
      method,
      path,
      durationMs,
      responsePreview: previewText(rawText, 220),
      error,
    })
    throw error
  }
}

export const api = {
  chat: (message: string, sessionId: string) =>
    req<{ response: string; session_id: string }>('/chat', {
      method: 'POST',
      body: JSON.stringify({ message, session_id: sessionId }),
    }),

  getStatus: () => req<SystemStatus>('/status'),

  getLifeContext: () => req<{ content: string; path: string }>('/life-context'),

  getConversations: (limit = 50) =>
    req<Array<{ id: number; session_id: string; role: string; content: string; created_at: string }>>(
      `/conversations?limit=${limit}`
    ),

  triggerBrief: () => req<{ ok: boolean; brief: string }>('/brief/now', { method: 'POST' }),

  triggerReview: () => req<{ ok: boolean; review: string }>('/review/now', { method: 'POST' }),

  getCommitments: () => req<{ commitments: Commitment[] }>('/commitments'),

  completeCommitment: (id: number) =>
    req<{ ok: boolean }>(`/commitments/${id}/complete`, { method: 'POST' }),

  health: () => req<{ status: string }>('/health'),

  getCapabilities: () =>
    req<{
      capabilities: Record<string, {
        display_name: string
        status: 'available' | 'not_configured' | 'permission_required' | 'temporarily_unavailable' | 'disabled'
        detail: string
        accounts: string[]
      }>
      available: string[]
    }>('/capabilities'),

  refreshCapabilities: () =>
    req<{ ok: boolean; capabilities: Record<string, unknown>; available: string[] }>(
      '/capabilities/refresh', { method: 'POST' }
    ),

  getPendingActions: () =>
    req<{
      pending: Array<{
        id: string
        tool_name: string
        args: Record<string, unknown>
        preview: string
        model_description?: string
        created_at: string
      }>
      count: number
    }>('/pending-actions'),

  actOnPending: (id: string, action: 'approve' | 'reject' | 'edit', edited_body?: string) =>
    req<{ ok: boolean }>(`/pending-actions/${id}`, {
      method: 'POST',
      body: JSON.stringify({ action, edited_body }),
    }),

  getCommsHealth: (quietDays = 14) =>
    req<{
      summary: {
        signals: string[]
        quiet_contact_count: number
        overdue_response_count: number
        summary: string
      }
      overdue_responses: {
        overdue: Array<{ from: string; channel: string; unread_count: number; last_message_at: string | null }>
        count: number
        summary: string
      }
      relationship_balance: {
        personal_contacts: number
        work_contacts: number
        personal_pct: number
        work_pct: number
        balance_note: string
        summary: string
      }
    }>(`/comms-health?quiet_days=${quietDays}`),
}
