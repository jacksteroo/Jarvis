import { useState, useEffect } from 'react'
import { api, SystemStatus, Commitment } from '../api'

type CapabilityStatus =
  | 'available'
  | 'not_configured'
  | 'permission_required'
  | 'temporarily_unavailable'
  | 'disabled'
interface Capability {
  display_name: string
  status: CapabilityStatus
  detail: string
  accounts: string[]
}

interface PendingAction {
  id: string
  tool_name: string
  args: Record<string, unknown>
  preview: string
  model_description?: string
  created_at: string
}

// Pull the authoritative recipient / body out of the queued args rather than
// trusting any free-text summary. Approval is the security boundary for
// outbound writes, so the operator must see what will actually be sent.
const RECIPIENT_KEYS = ['to', 'recipient', 'channel', 'chat_id', 'address'] as const
const BODY_KEYS = ['body', 'text', 'message', 'content'] as const
const SUBJECT_KEYS = ['subject', 'title'] as const

const pickField = (args: Record<string, unknown>, keys: readonly string[]): string => {
  for (const k of keys) {
    const v = args[k]
    if (typeof v === 'string' && v.length > 0) return v
    if (typeof v === 'number') return String(v)
  }
  return ''
}

const formatArgs = (args: Record<string, unknown>): string => {
  try {
    return JSON.stringify(args, null, 2)
  } catch {
    return String(args)
  }
}

const statusColor = (s: CapabilityStatus): string => {
  switch (s) {
    case 'available': return '#22c55e'
    case 'not_configured': return '#666'
    case 'permission_required': return '#f59e0b'
    case 'temporarily_unavailable': return '#f59e0b'
    case 'disabled': return '#666'
    default: return '#888'
  }
}

const statusLabel = (s: CapabilityStatus): string => s.replace(/_/g, ' ')

const s = {
  root: { padding: '24px', overflowY: 'auto' as const, height: '100%' },
  section: { marginBottom: '28px' },
  heading: { fontSize: '12px', letterSpacing: '0.1em', color: '#666', textTransform: 'uppercase' as const, marginBottom: '10px' },
  card: { background: '#111', border: '1px solid #1e1e1e', borderRadius: '10px', padding: '14px 16px' },
  row: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '4px 0', fontSize: '13px', borderBottom: '1px solid #1a1a1a' },
  statusDot: (s: string) => ({
    width: 8, height: 8, borderRadius: '50%', display: 'inline-block', marginRight: 6,
    background: s === 'ok' ? '#22c55e' : s === 'degraded' ? '#f59e0b' : '#ef4444',
  }),
  btn: {
    background: '#4a9eff22', color: '#4a9eff', border: '1px solid #4a9eff44',
    borderRadius: '6px', padding: '6px 14px', cursor: 'pointer', fontSize: '12px',
    fontFamily: 'inherit', marginRight: '8px',
  },
  commitmentItem: {
    padding: '8px 0', borderBottom: '1px solid #1a1a1a', fontSize: '13px',
    display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '8px',
  },
  argsBox: {
    marginTop: '6px', padding: '8px', borderRadius: '6px',
    background: '#0b0b0b', border: '1px solid #1a1a1a', color: '#bdbdbd',
    fontSize: '11px', whiteSpace: 'pre-wrap' as const, wordBreak: 'break-word' as const,
  },
  doneBtn: {
    background: 'transparent', color: '#22c55e', border: '1px solid #22c55e44',
    borderRadius: '4px', padding: '2px 8px', cursor: 'pointer', fontSize: '11px',
    fontFamily: 'inherit', whiteSpace: 'nowrap' as const, flexShrink: 0,
  },
}

export default function Status() {
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [commitments, setCommitments] = useState<Commitment[]>([])
  const [capabilities, setCapabilities] = useState<Record<string, Capability>>({})
  const [pending, setPending] = useState<PendingAction[]>([])
  const [refreshingCaps, setRefreshingCaps] = useState(false)
  const [briefing, setBriefing] = useState(false)
  const [reviewing, setReviewing] = useState(false)
  const [error, setError] = useState('')

  const load = async () => {
    try {
      const [s, c, caps, pa] = await Promise.all([
        api.getStatus(),
        api.getCommitments(),
        api.getCapabilities(),
        api.getPendingActions(),
      ])
      setStatus(s)
      setCommitments(c.commitments || [])
      setCapabilities(caps.capabilities || {})
      setPending(pa.pending || [])
      setError('')
    } catch {
      setError('Cannot reach Pepper at localhost:8000 — is it running?')
    }
  }

  const refreshCapabilities = async () => {
    setRefreshingCaps(true)
    try {
      const r = await api.refreshCapabilities()
      setCapabilities((r.capabilities as Record<string, Capability>) || {})
    } finally {
      setRefreshingCaps(false)
    }
  }

  const actOnPending = async (id: string, action: 'approve' | 'reject') => {
    try {
      await api.actOnPending(id, action)
      // Only remove from UI on success (approve) or explicit reject.
      // A failed approve returns 500 — caught below — so the item stays visible.
      setPending((p) => p.filter((x) => x.id !== id))
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(`Action failed: ${msg}`)
    }
  }

  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [])

  const triggerBrief = async () => {
    setBriefing(true)
    try { await api.triggerBrief() } finally { setBriefing(false) }
  }
  const triggerReview = async () => {
    setReviewing(true)
    try { await api.triggerReview() } finally { setReviewing(false) }
  }
  const markDone = async (id: number) => {
    await api.completeCommitment(id)
    setCommitments((c) => c.filter((x) => x.id !== id))
  }

  if (error) return <div style={{ padding: 24, color: '#ef4444' }}>{error}</div>
  if (!status) return <div style={{ padding: 24, color: '#666' }}>Loading…</div>

  return (
    <div style={s.root}>
      <div style={s.section}>
        <div style={s.heading}>System</div>
        <div style={s.card}>
          <div style={s.row}>
            <span>Core</span>
            <span><span style={s.statusDot(status.initialized ? 'ok' : 'down')} />{status.initialized ? 'online' : 'offline'}</span>
          </div>
          <div style={s.row}><span>Local model</span><span style={{ color: '#888' }}>{status.default_local_model}</span></div>
          <div style={s.row}><span>Working memory</span><span style={{ color: '#888' }}>{status.working_memory_size} msgs</span></div>
          <div style={s.row}><span>Telegram</span><span style={{ color: status.telegram_enabled ? '#22c55e' : '#666' }}>{status.telegram_enabled ? 'enabled' : 'disabled'}</span></div>
          {status.scheduler && (
            <div style={s.row}>
              <span>Scheduler</span>
              <span style={{ color: status.scheduler.running ? '#22c55e' : '#666' }}>
                {status.scheduler.running ? `running (${status.scheduler.jobs?.length ?? 0} jobs)` : 'stopped'}
              </span>
            </div>
          )}
          {status.scheduler?.last_brief && (
            <div style={s.row}><span>Last brief</span><span style={{ color: '#888' }}>{status.scheduler.last_brief.slice(0, 16)}</span></div>
          )}
        </div>
      </div>

      <div style={s.section}>
        <div style={s.heading}>Subsystems</div>
        <div style={s.card}>
          {Object.entries(status.subsystems).map(([name, health]) => (
            <div key={name} style={s.row}>
              <span>{name}</span>
              <span><span style={s.statusDot(health)} />{health}</span>
            </div>
          ))}
        </div>
      </div>

      <div style={s.section}>
        <div style={s.heading}>Actions</div>
        <button style={s.btn} onClick={triggerBrief} disabled={briefing}>
          {briefing ? 'Generating…' : '☀️ Morning Brief'}
        </button>
        <button style={s.btn} onClick={triggerReview} disabled={reviewing}>
          {reviewing ? 'Generating…' : '📋 Weekly Review'}
        </button>
      </div>

      {Object.keys(capabilities).length > 0 && (
        <div style={s.section}>
          <div style={{ ...s.heading, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>Capabilities</span>
            <button
              style={{ ...s.btn, marginRight: 0, padding: '2px 10px', fontSize: '11px' }}
              onClick={refreshCapabilities}
              disabled={refreshingCaps}
            >
              {refreshingCaps ? 'Refreshing…' : '↻ Refresh'}
            </button>
          </div>
          <div style={s.card}>
            {Object.entries(capabilities).map(([key, cap]) => (
              <div key={key} style={s.row} title={cap.detail || ''}>
                <span>
                  {cap.display_name}
                  {cap.accounts.length > 0 && (
                    <span style={{ color: '#666', marginLeft: 6 }}>
                      ({cap.accounts.join(', ')})
                    </span>
                  )}
                </span>
                <span style={{ color: statusColor(cap.status) }}>
                  <span style={{ ...s.statusDot(cap.status === 'available' ? 'ok' : cap.status === 'disabled' || cap.status === 'not_configured' ? 'down' : 'degraded') }} />
                  {statusLabel(cap.status)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {pending.length > 0 && (
        <div style={s.section}>
          <div style={s.heading}>Pending Actions ({pending.length})</div>
          <div style={s.card}>
            {pending.map((p) => {
              const recipient = pickField(p.args, RECIPIENT_KEYS)
              const subject = pickField(p.args, SUBJECT_KEYS)
              const body = pickField(p.args, BODY_KEYS)
              return (
                <div key={p.id} style={s.commitmentItem}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ color: '#4a9eff', fontSize: '11px', marginBottom: 2 }}>{p.tool_name}</div>
                    {recipient && (
                      <div style={{ fontSize: 12 }}>
                        <span style={{ color: '#888' }}>To: </span>
                        <span style={{ wordBreak: 'break-all' }}>{recipient}</span>
                      </div>
                    )}
                    {subject && (
                      <div style={{ fontSize: 12 }}>
                        <span style={{ color: '#888' }}>Subject: </span>
                        {subject}
                      </div>
                    )}
                    {body && (
                      <div style={{ fontSize: 12, whiteSpace: 'pre-wrap', marginTop: 4 }}>
                        {body}
                      </div>
                    )}
                    {!recipient && !body && !subject && (
                      <div style={{ fontSize: 12 }}>{p.preview}</div>
                    )}
                    {p.model_description && (
                      <div style={{ fontSize: 11, color: '#888', marginTop: 6, fontStyle: 'italic' }}>
                        model says: {p.model_description}
                      </div>
                    )}
                    <div style={s.argsBox}>
                      {formatArgs(p.args)}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                    <button style={s.doneBtn} onClick={() => actOnPending(p.id, 'approve')}>Approve</button>
                    <button
                      style={{ ...s.doneBtn, color: '#ef4444', borderColor: '#ef444444' }}
                      onClick={() => actOnPending(p.id, 'reject')}
                    >
                      Reject
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {commitments.length > 0 && (
        <div style={s.section}>
          <div style={s.heading}>Pending Commitments ({commitments.length})</div>
          <div style={s.card}>
            {commitments.map((c) => (
              <div key={c.id} style={s.commitmentItem}>
                <span style={{ flex: 1 }}>{c.content.replace(/^COMMITMENT:\s*/i, '').slice(0, 140)}</span>
                <button style={s.doneBtn} onClick={() => markDone(c.id)}>Done</button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
