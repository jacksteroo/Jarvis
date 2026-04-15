import { useState, useEffect } from 'react'
import { api, SystemStatus, Commitment } from '../api'

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
  doneBtn: {
    background: 'transparent', color: '#22c55e', border: '1px solid #22c55e44',
    borderRadius: '4px', padding: '2px 8px', cursor: 'pointer', fontSize: '11px',
    fontFamily: 'inherit', whiteSpace: 'nowrap' as const, flexShrink: 0,
  },
}

export default function Status() {
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [commitments, setCommitments] = useState<Commitment[]>([])
  const [briefing, setBriefing] = useState(false)
  const [reviewing, setReviewing] = useState(false)
  const [error, setError] = useState('')

  const load = async () => {
    try {
      const [s, c] = await Promise.all([api.getStatus(), api.getCommitments()])
      setStatus(s)
      setCommitments(c.commitments || [])
      setError('')
    } catch {
      setError('Cannot reach Pepper at localhost:8000 — is it running?')
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
