import { useState, useEffect } from 'react'
import { api } from '../api'

const s = {
  root: { padding: '24px', overflowY: 'auto' as const, height: '100%' },
  section: { marginBottom: '28px' },
  heading: {
    fontSize: '12px', letterSpacing: '0.1em', color: '#666',
    textTransform: 'uppercase' as const, marginBottom: '10px',
  },
  card: {
    background: '#111', border: '1px solid #1e1e1e', borderRadius: '10px', padding: '14px 16px',
  },
  row: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    padding: '6px 0', fontSize: '13px', borderBottom: '1px solid #1a1a1a',
  },
  signal: {
    padding: '6px 0', fontSize: '13px', color: '#e0e0e0', borderBottom: '1px solid #1a1a1a',
  },
  badge: (channel: string) => ({
    fontSize: '10px', padding: '2px 6px', borderRadius: '4px', marginLeft: '6px',
    background: channel === 'imessage' ? '#1d4ed833' : channel === 'whatsapp' ? '#16a34a33' : '#7c3aed33',
    color: channel === 'imessage' ? '#60a5fa' : channel === 'whatsapp' ? '#4ade80' : '#a78bfa',
  }),
  balanceBar: { height: '8px', borderRadius: '4px', background: '#1e1e1e', margin: '8px 0', overflow: 'hidden' },
  balanceFill: (pct: number) => ({
    height: '100%', width: `${pct}%`, background: '#4a9eff', borderRadius: '4px', transition: 'width 0.3s',
  }),
  empty: { color: '#555', fontSize: '13px', padding: '8px 0' },
  refreshBtn: {
    background: '#4a9eff22', color: '#4a9eff', border: '1px solid #4a9eff44',
    borderRadius: '6px', padding: '6px 14px', cursor: 'pointer', fontSize: '12px',
    fontFamily: 'inherit',
  },
}

type CommsHealth = Awaited<ReturnType<typeof api.getCommsHealth>>

export default function Relationships() {
  const [data, setData] = useState<CommsHealth | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const result = await api.getCommsHealth()
      setData(result)
      setError('')
    } catch {
      setError('Cannot reach Pepper at localhost:8000 — is it running?')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  if (error) return <div style={{ padding: 24, color: '#ef4444' }}>{error}</div>
  if (loading) return <div style={{ padding: 24, color: '#666' }}>Loading…</div>
  if (!data) return null

  const { summary, overdue_responses, relationship_balance } = data
  const balance = relationship_balance as any

  return (
    <div style={s.root}>
      {/* Health Signals */}
      <div style={s.section}>
        <div style={s.heading}>Communication Health</div>
        <div style={s.card}>
          {summary.signals && summary.signals.length > 0 ? (
            summary.signals.map((signal, i) => (
              <div key={i} style={s.signal}>• {signal}</div>
            ))
          ) : (
            <div style={s.empty}>All clear — no communication gaps detected.</div>
          )}
        </div>
      </div>

      {/* Overdue Responses */}
      <div style={s.section}>
        <div style={s.heading}>Needs a Reply ({overdue_responses.count})</div>
        <div style={s.card}>
          {overdue_responses.overdue && overdue_responses.overdue.length > 0 ? (
            overdue_responses.overdue.map((m, i) => (
              <div key={i} style={s.row}>
                <span>
                  {m.from}
                  <span style={s.badge(m.channel)}>{m.channel}</span>
                </span>
                <span style={{ color: '#888', fontSize: '12px' }}>
                  {m.unread_count} unread
                </span>
              </div>
            ))
          ) : (
            <div style={s.empty}>Inbox clear — no overdue responses.</div>
          )}
        </div>
      </div>

      {/* Relationship Balance */}
      {!balance.error && (
        <div style={s.section}>
          <div style={s.heading}>Relationship Balance (30d)</div>
          <div style={s.card}>
            <div style={s.row}>
              <span>Personal contacts</span>
              <span style={{ color: '#4a9eff' }}>{balance.personal_contacts}</span>
            </div>
            <div style={s.row}>
              <span>Work channels</span>
              <span style={{ color: '#888' }}>{balance.work_contacts}</span>
            </div>
            {typeof balance.personal_pct === 'number' && (
              <>
                <div style={s.balanceBar}>
                  <div style={s.balanceFill(balance.personal_pct)} />
                </div>
                <div style={{ fontSize: '11px', color: '#666' }}>
                  {balance.personal_pct}% personal / {balance.work_pct}% work
                </div>
              </>
            )}
            {balance.balance_note && (
              <div style={{ marginTop: '10px', fontSize: '12px', color: '#aaa' }}>
                {balance.balance_note}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Refresh */}
      <button style={s.refreshBtn} onClick={load}>Refresh</button>
    </div>
  )
}
