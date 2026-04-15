import { useState, useEffect, useRef, KeyboardEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import { api, Message } from '../api'
import { logError, logInfo, logWarn, nextTraceId, previewText } from '../logger'

const LOADING_PHRASES = [
  'Analyzing',
  'Consulting the Oracle',
  'Cross-referencing',
  'Traversing the Aether',
  'Running Diagnostics',
  'Seeking the Codex',
  'Querying Subsystems',
  'Invoking the Runes',
  'Scanning Archives',
  'Channeling Power',
  'Calculating',
  'Deciphering Sigils',
  'Accessing Mainframe',
  'Navigating the Void',
  'Compiling Data',
  'Reading the Scroll',
]

const STAR_FRAMES = [' ', '·', '✦', '✸', '✦', '·', ' ']  // breathe: none→small→med→big→med→small→none

function LoadingIndicator() {
  const [phraseIdx, setPhraseIdx] = useState(0)
  const [starFrame, setStarFrame] = useState(0)

  useEffect(() => {
    const starTimer = setInterval(() => {
      setStarFrame((f) => {
        const next = (f + 1) % STAR_FRAMES.length
        if (next === 0) setPhraseIdx((i) => (i + 1) % LOADING_PHRASES.length)
        return next
      })
    }, 700)
    return () => clearInterval(starTimer)
  }, [])

  return (
    <span>
      {LOADING_PHRASES[phraseIdx]}{' '}
      <span style={{ display: 'inline-block', width: '1ch' }}>{STAR_FRAMES[starFrame]}</span>
    </span>
  )
}

function getSessionId(): string {
  let id = localStorage.getItem('pepper_session_id')
  if (!id) {
    id = Math.random().toString(36).slice(2) + Date.now().toString(36)
    localStorage.setItem('pepper_session_id', id)
    logInfo('chat', 'session_created', { sessionId: id })
  } else {
    logInfo('chat', 'session_restored', { sessionId: id })
  }
  return id
}

const s = {
  root: { display: 'flex', flexDirection: 'column' as const, height: '100%' },
  messages: {
    flex: 1,
    overflowY: 'auto' as const,
    padding: '20px',
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '12px',
  },
  bubble: (role: string) => ({
    maxWidth: '72%',
    alignSelf: role === 'user' ? 'flex-end' : 'flex-start',
    background: role === 'user' ? '#4a9eff22' : '#1a1a1a',
    border: `1px solid ${role === 'user' ? '#4a9eff44' : '#2a2a2a'}`,
    borderRadius: '12px',
    padding: '10px 14px',
    fontSize: '14px',
    lineHeight: '1.6',
    color: '#e0e0e0',
  }),
  inputRow: {
    display: 'flex',
    gap: '8px',
    padding: '16px 20px',
    borderTop: '1px solid #1e1e1e',
    background: '#111',
  },
  input: {
    flex: 1,
    background: '#1a1a1a',
    border: '1px solid #2a2a2a',
    borderRadius: '8px',
    padding: '10px 14px',
    color: '#e0e0e0',
    fontSize: '14px',
    fontFamily: 'inherit',
    outline: 'none',
    resize: 'none' as const,
  },
  sendBtn: {
    background: '#4a9eff',
    color: '#000',
    border: 'none',
    borderRadius: '8px',
    padding: '10px 18px',
    cursor: 'pointer',
    fontFamily: 'inherit',
    fontSize: '14px',
    fontWeight: 600,
  },
  loading: {
    alignSelf: 'flex-start',
    color: '#4a9eff',
    fontSize: '13px',
    padding: '4px 0',
    fontStyle: 'italic' as const,
    letterSpacing: '0.03em',
  },
}

export default function Chat() {
  const [messages, setMessages] = useState<Message[]>([
    { role: 'assistant', content: 'Hi, I\'m Pepper! What\'s on your mind?' }
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const [sessionId] = useState(() => getSessionId())

  useEffect(() => {
    logInfo('chat', 'mounted', {
      sessionId,
      initialMessages: messages.length,
    })

    return () => {
      logInfo('chat', 'unmounted', { sessionId })
    }
  }, [sessionId])

  useEffect(() => {
    logInfo('chat', 'auto_scroll_requested', {
      sessionId,
      messageCount: messages.length,
      loading,
    })
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [loading, messages, sessionId])

  useEffect(() => {
    const lastMessage = messages[messages.length - 1]
    logInfo('chat', 'messages_updated', {
      sessionId,
      count: messages.length,
      lastRole: lastMessage?.role,
      lastPreview: lastMessage ? previewText(lastMessage.content, 140) : '',
    })
  }, [messages, sessionId])

  useEffect(() => {
    logInfo('chat', 'loading_changed', { sessionId, loading })
  }, [loading, sessionId])

  const send = async () => {
    const turnId = nextTraceId('chat-turn')
    const text = input.trim()
    logInfo('chat', 'send_requested', {
      turnId,
      sessionId,
      inputLength: input.length,
      trimmedLength: text.length,
      loading,
      draftPreview: previewText(input, 160),
    })

    if (!text) {
      logWarn('chat', 'send_blocked_empty_input', { turnId, sessionId })
      return
    }

    if (loading) {
      logWarn('chat', 'send_blocked_loading', { turnId, sessionId })
      return
    }

    const startedAt = performance.now()
    setInput('')
    logInfo('chat', 'input_cleared_for_send', { turnId, sessionId })
    setMessages((m) => [...m, { role: 'user', content: text }])
    logInfo('chat', 'user_message_queued', {
      turnId,
      sessionId,
      userMessagePreview: previewText(text, 160),
    })
    setLoading(true)

    try {
      logInfo('chat', 'api_chat_dispatch', {
        turnId,
        sessionId,
        outboundMessagePreview: previewText(text, 160),
      })
      const res = await api.chat(text, sessionId)
      logInfo('chat', 'api_chat_resolved', {
        turnId,
        sessionId,
        durationMs: Math.round(performance.now() - startedAt),
        responsePreview: previewText(res.response, 180),
      })
      setMessages((m) => [...m, { role: 'assistant', content: res.response }])
    } catch (e) {
      logError('chat', 'api_chat_failed', {
        turnId,
        sessionId,
        durationMs: Math.round(performance.now() - startedAt),
        error: e,
      })
      setMessages((m) => [...m, { role: 'assistant', content: '⚠️ Failed to reach Pepper. Is it running?' }])
    } finally {
      logInfo('chat', 'turn_finished', {
        turnId,
        sessionId,
        durationMs: Math.round(performance.now() - startedAt),
      })
      setLoading(false)
    }
  }

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    logInfo('chat', 'input_keydown', {
      sessionId,
      key: e.key,
      shiftKey: e.shiftKey,
      loading,
      draftLength: input.length,
    })
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div style={s.root}>
      <div style={s.messages}>
        {messages.map((m, i) => (
          <div key={i} style={s.bubble(m.role)}>
            {m.role === 'assistant'
              ? <ReactMarkdown>{m.content}</ReactMarkdown>
              : m.content}
          </div>
        ))}
        {loading && <div style={s.loading}><LoadingIndicator /></div>}
        <div ref={bottomRef} />
      </div>
      <div style={s.inputRow}>
        <textarea
          style={s.input}
          value={input}
          onChange={(e) => {
            const nextValue = e.target.value
            logInfo('chat', 'input_changed', {
              sessionId,
              length: nextValue.length,
              preview: previewText(nextValue, 120),
            })
            setInput(nextValue)
          }}
          onKeyDown={onKey}
          placeholder="Ask Pepper anything… (Enter to send, Shift+Enter for newline)"
          rows={2}
        />
        <button
          style={s.sendBtn}
          onClick={() => {
            logInfo('chat', 'send_button_clicked', { sessionId, loading, draftLength: input.length })
            send()
          }}
          disabled={loading}
        >
          Send
        </button>
      </div>
    </div>
  )
}
