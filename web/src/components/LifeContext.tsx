import { useState, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import { api } from '../api'

const s = {
  root: { padding: '24px', overflowY: 'auto' as const, height: '100%', maxWidth: '760px' },
  header: { marginBottom: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' },
  title: { fontSize: '14px', color: '#4a9eff', marginBottom: '4px' },
  note: {
    fontSize: '12px', color: '#666', background: '#111', border: '1px solid #1e1e1e',
    borderRadius: '6px', padding: '8px 12px', marginBottom: '20px',
  },
  content: { fontSize: '14px', lineHeight: '1.8', color: '#ccc' },
}

export default function LifeContext() {
  const [content, setContent] = useState('')
  const [path, setPath] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.getLifeContext()
      .then((d) => { setContent(d.content); setPath(d.path) })
      .catch(() => setContent('Could not load life context. Is Pepper running?'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <div style={{ padding: 24, color: '#666' }}>Loading…</div>

  return (
    <div style={s.root}>
      <div style={s.header}>
        <div>
          <div style={s.title}>Life Context Document</div>
          <div style={{ fontSize: '11px', color: '#555' }}>{path}</div>
        </div>
      </div>
      <div style={s.note}>
        📝 This document is read-only here. To update it, tell Pepper in the chat: <em>"Update my life context: [section] [new content]"</em>
      </div>
      <div style={s.content}>
        <ReactMarkdown>{content}</ReactMarkdown>
      </div>
    </div>
  )
}
