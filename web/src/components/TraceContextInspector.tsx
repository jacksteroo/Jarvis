// Epic 01 (#34) — Trace context inspector.
//
// Given a trace, breaks down what went into the prompt and why. Most
// sensitive view in the entire UI: surfaces raw life-context section
// names, raw memory IDs/scores, and (on re-render) the rendered system
// prompt. Localhost-bind is enforced server-side by `agent/traces/http.py`
// (see `_enforce_localhost_bind`), so this component just trusts the
// API guard and shows a banner so the operator never forgets.
//
// Why no react-router: the existing app uses a tab-based root. Adding
// a router for one drill-down adds a dependency for negligible benefit.
// Instead, the parent (Traces.tsx) owns selection state and switches to
// this component when "Inspect prompt construction" is clicked.

import { useEffect, useState } from 'react'
import {
  api,
  type RerenderPromptResponse,
  type TraceDetail,
} from '../api'
import { logError, logInfo } from '../logger'

const styles = {
  root: {
    height: '100%',
    overflowY: 'auto' as const,
    padding: 24,
    fontFamily: 'inherit',
  },
  banner: {
    background: '#3a1212',
    border: '1px solid #ef4444',
    color: '#fecaca',
    borderRadius: 6,
    padding: '10px 14px',
    fontSize: 13,
    fontWeight: 600,
    marginBottom: 16,
    letterSpacing: 0.2,
  },
  topbar: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    marginBottom: 16,
  },
  backButton: {
    background: '#1a1a1a',
    color: '#e0e0e0',
    border: '1px solid #2a2a2a',
    borderRadius: 4,
    padding: '6px 12px',
    cursor: 'pointer',
    fontSize: 12,
    fontFamily: 'inherit',
  },
  title: { fontSize: 18, fontWeight: 600 },
  meta: { fontSize: 11, color: '#888' },
  section: {
    marginTop: 20,
    border: '1px solid #1e1e1e',
    borderRadius: 6,
    background: '#0d0d0d',
    overflow: 'hidden' as const,
  },
  sectionHeader: {
    padding: '10px 14px',
    background: '#141414',
    borderBottom: '1px solid #1e1e1e',
    fontSize: 13,
    fontWeight: 600,
    display: 'flex',
    justifyContent: 'space-between' as const,
    alignItems: 'center',
  },
  sectionBody: { padding: 14 },
  reason: {
    fontSize: 12,
    color: '#9ca3af',
    fontStyle: 'italic' as const,
    marginBottom: 10,
  },
  pre: {
    background: '#080808',
    color: '#e0e0e0',
    padding: 10,
    borderRadius: 4,
    border: '1px solid #1e1e1e',
    fontFamily: 'ui-monospace, monospace',
    fontSize: 12,
    whiteSpace: 'pre-wrap' as const,
    overflowX: 'auto' as const,
    margin: 0,
  },
  list: { margin: 0, paddingLeft: 18, fontSize: 13 },
  pill: (color: string) => ({
    display: 'inline-block',
    padding: '1px 6px',
    borderRadius: 3,
    fontSize: 10,
    background: `${color}22`,
    color,
    marginRight: 6,
  }),
  empty: { color: '#666', fontSize: 12 },
  rerenderRow: {
    display: 'flex',
    gap: 10,
    alignItems: 'center',
    marginTop: 8,
  },
  primaryButton: {
    background: '#1e3a5f',
    color: '#bfdbfe',
    border: '1px solid #2a4a73',
    borderRadius: 4,
    padding: '6px 12px',
    cursor: 'pointer',
    fontSize: 12,
    fontFamily: 'inherit',
  },
  matchPillTrue: {
    color: '#86efac',
    background: '#14532d44',
    padding: '2px 8px',
    borderRadius: 3,
    fontSize: 11,
  },
  matchPillFalse: {
    color: '#fca5a5',
    background: '#7f1d1d44',
    padding: '2px 8px',
    borderRadius: 3,
    fontSize: 11,
  },
  capabilityDiff: {
    fontSize: 12,
    color: '#fcd34d',
  },
}

interface Props {
  traceId: string
  /** Optional pre-loaded detail; component refetches if missing. */
  detail?: TraceDetail | null
  onBack: () => void
}

interface ProvenanceShape {
  life_context_sections_used?: string[]
  last_n_turns?: number
  memory_ids?: Array<[string, number]>
  skill_match?: Record<string, unknown> | null
  capability_block_version?: string
  selectors?: Record<string, Record<string, unknown>>
}

function provenanceOf(detail: TraceDetail | null): ProvenanceShape {
  if (!detail) return {}
  return (detail.assembled_context || {}) as ProvenanceShape
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString()
}

function formatMemoryScore(score: number | undefined | null): string {
  if (typeof score !== 'number' || Number.isNaN(score)) return '—'
  return score.toFixed(3)
}

interface ExpandableMemoryRowProps {
  id: string
  score: number
}

function ExpandableMemoryRow({ id, score }: ExpandableMemoryRowProps) {
  const [open, setOpen] = useState(false)
  return (
    <li
      style={{
        listStyle: 'none',
        padding: '6px 0',
        borderBottom: '1px solid #1a1a1a',
      }}
    >
      <div
        style={{ cursor: 'pointer', display: 'flex', gap: 8, alignItems: 'center' }}
        onClick={() => setOpen(!open)}
      >
        <span style={{ color: '#888', fontSize: 11, width: 14 }}>{open ? '▼' : '▶'}</span>
        <code style={{ fontSize: 12 }}>{id}</code>
        <span style={{ marginLeft: 'auto', color: '#9ca3af', fontSize: 11 }}>
          score: {formatMemoryScore(score)}
        </span>
      </div>
      {open && (
        <div
          style={{
            marginTop: 6,
            marginLeft: 22,
            padding: '8px 10px',
            background: '#080808',
            border: '1px solid #1e1e1e',
            borderRadius: 4,
            color: '#9ca3af',
            fontSize: 12,
          }}
        >
          Raw memory content fetch is not wired in this build — the
          inspector exposes the IDs and scores stored on the trace's
          provenance only. To read the underlying recall row, query
          the memory subsystem directly with this ID.
        </div>
      )}
    </li>
  )
}

export default function TraceContextInspector({ traceId, detail: detailProp, onBack }: Props) {
  const [detail, setDetail] = useState<TraceDetail | null>(detailProp ?? null)
  const [loading, setLoading] = useState(!detailProp)
  const [error, setError] = useState<string | null>(null)
  const [rerender, setRerender] = useState<RerenderPromptResponse | null>(null)
  const [rerendering, setRerendering] = useState(false)
  const [rerenderError, setRerenderError] = useState<string | null>(null)

  useEffect(() => {
    if (detailProp) {
      setDetail(detailProp)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .getTrace(traceId)
      .then((d) => {
        if (cancelled) return
        setDetail(d)
        logInfo('inspector', 'detail_loaded', { trace_id: traceId })
      })
      .catch((e) => {
        if (cancelled) return
        const message = e instanceof Error ? e.message : String(e)
        setError(message)
        logError('inspector', 'detail_failed', { trace_id: traceId, message })
      })
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [traceId, detailProp])

  const handleRerender = async () => {
    setRerendering(true)
    setRerenderError(null)
    try {
      const result = await api.rerenderPrompt(traceId)
      setRerender(result)
      logInfo('inspector', 'rerender_succeeded', {
        trace_id: traceId,
        matches_original: result.matches_original,
      })
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setRerenderError(message)
      logError('inspector', 'rerender_failed', { trace_id: traceId, message })
    } finally {
      setRerendering(false)
    }
  }

  if (loading) {
    return <div style={styles.root}>loading…</div>
  }
  if (error || !detail) {
    return (
      <div style={styles.root}>
        <button style={styles.backButton} onClick={onBack}>
          ← back
        </button>
        <div style={{ marginTop: 16, color: '#fca5a5' }}>
          error: {error ?? 'trace not found'}
        </div>
      </div>
    )
  }

  const prov = provenanceOf(detail)
  const reasons = detail.decision_reasons || {}
  const sections = prov.life_context_sections_used || []
  const memoryIds = prov.memory_ids || []
  const lifeContextContent =
    (prov.selectors?.life_context as { content?: string } | undefined)?.content ?? null

  return (
    <div style={styles.root}>
      <div style={styles.banner}>
        Raw personal data — local only. This panel inlines life-context
        sections and memory IDs from the active database. Localhost bind
        is enforced server-side; never expose this UI on a public network.
      </div>

      <div style={styles.topbar}>
        <button style={styles.backButton} onClick={onBack}>
          ← back to trace
        </button>
        <div>
          <div style={styles.title}>Prompt construction</div>
          <div style={styles.meta}>
            trace <code>{detail.trace_id}</code> ·{' '}
            {formatTimestamp(detail.created_at)} · {detail.model_selected || '<no model>'}
            {detail.prompt_version && (
              <>
                {' · prompt: '}
                <code>{detail.prompt_version}</code>
              </>
            )}
          </div>
        </div>
      </div>

      {/* ── Life context ──────────────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Life context</span>
          <span style={styles.pill('#a78bfa')}>{sections.length} section(s)</span>
        </div>
        <div style={styles.sectionBody}>
          {reasons.life_context && <div style={styles.reason}>{reasons.life_context}</div>}
          {sections.length === 0 && <div style={styles.empty}>no sections recorded</div>}
          {sections.length > 0 && (
            <ul style={styles.list}>
              {sections.map((s) => (
                <li key={s}>
                  <code>{s}</code>
                </li>
              ))}
            </ul>
          )}
          {lifeContextContent && (
            <pre style={{ ...styles.pre, marginTop: 10 }}>{lifeContextContent}</pre>
          )}
        </div>
      </div>

      {/* ── Conversation history ──────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Conversation history</span>
          <span style={styles.pill('#60a5fa')}>last {prov.last_n_turns ?? 0} turn(s)</span>
        </div>
        <div style={styles.sectionBody}>
          {reasons.last_n_turns && <div style={styles.reason}>{reasons.last_n_turns}</div>}
          <div style={styles.empty}>
            Provenance records the count and limit only; raw turn text isn't
            persisted on the trace row to keep history mutations append-only.
            The original turn was emitted at{' '}
            <code>{formatTimestamp(detail.created_at)}</code>.
          </div>
          <pre style={{ ...styles.pre, marginTop: 10 }}>
            {JSON.stringify(prov.selectors?.last_n_turns ?? {}, null, 2)}
          </pre>
        </div>
      </div>

      {/* ── Retrieved memories ────────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Retrieved memories</span>
          <span style={styles.pill('#34d399')}>{memoryIds.length} hit(s)</span>
        </div>
        <div style={styles.sectionBody}>
          {reasons.retrieved_memory && (
            <div style={styles.reason}>{reasons.retrieved_memory}</div>
          )}
          {memoryIds.length === 0 ? (
            <div style={styles.empty}>no recall hits for this turn</div>
          ) : (
            <ul style={{ ...styles.list, paddingLeft: 0 }}>
              {memoryIds.map(([id, score], i) => (
                <ExpandableMemoryRow key={`${id}-${i}`} id={String(id)} score={Number(score)} />
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* ── Skill match ───────────────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Skill match</span>
          <span style={styles.pill('#facc15')}>
            {prov.skill_match ? 'matched' : 'no per-turn match'}
          </span>
        </div>
        <div style={styles.sectionBody}>
          {reasons.skill_match && <div style={styles.reason}>{reasons.skill_match}</div>}
          {prov.skill_match ? (
            <pre style={styles.pre}>{JSON.stringify(prov.skill_match, null, 2)}</pre>
          ) : (
            <div style={styles.empty}>
              No per-turn similarity match — the system uses progressive
              disclosure: skills are listed in the system prompt and the
              model picks them via the <code>skill_view</code> tool.
            </div>
          )}
        </div>
      </div>

      {/* ── Capability block ──────────────────────────────────────── */}
      <CapabilityBlockSection
        version={prov.capability_block_version ?? ''}
        reason={reasons.capability_block}
        capabilityProvenance={prov.selectors?.capability_block ?? null}
        currentTraceId={detail.trace_id}
      />

      {/* ── Re-render ─────────────────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Re-render prompt</span>
          {rerender && (
            <span
              style={
                rerender.matches_original
                  ? styles.matchPillTrue
                  : styles.matchPillFalse
              }
            >
              {rerender.matches_original
                ? 'structural match'
                : 'diverged from original'}
            </span>
          )}
        </div>
        <div style={styles.sectionBody}>
          <div style={styles.reason}>
            Runs the live assembler against this trace's input. Result is
            in-browser only — never logged server-side.
          </div>
          <div style={styles.rerenderRow}>
            <button
              style={styles.primaryButton}
              onClick={handleRerender}
              disabled={rerendering}
            >
              {rerendering ? 'rendering…' : 'Re-render prompt'}
            </button>
            {rerender && (
              <span style={{ fontSize: 11, color: '#888' }}>
                hash: <code>{rerender.prompt_hash.slice(0, 12)}…</code>
              </span>
            )}
          </div>
          {rerenderError && (
            <div style={{ marginTop: 10, color: '#fca5a5', fontSize: 12 }}>
              error: {rerenderError}
            </div>
          )}
          {rerender && (
            <>
              {rerender.notes.length > 0 && (
                <ul
                  style={{
                    ...styles.list,
                    marginTop: 10,
                    color: '#9ca3af',
                    fontSize: 12,
                  }}
                >
                  {rerender.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              )}
              <div style={{ marginTop: 10, fontSize: 11, color: '#888' }}>
                rendered prompt:
              </div>
              <pre style={styles.pre}>{rerender.prompt}</pre>
              <div style={{ marginTop: 10, fontSize: 11, color: '#888' }}>
                provenance diff (original → re-rendered):
              </div>
              <pre style={styles.pre}>
                {JSON.stringify(
                  {
                    original: rerender.original_provenance,
                    rerendered: rerender.provenance,
                  },
                  null,
                  2,
                )}
              </pre>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

interface CapabilityBlockSectionProps {
  version: string
  reason?: string
  capabilityProvenance: Record<string, unknown> | null
  currentTraceId: string
}

function CapabilityBlockSection({
  version,
  reason,
  capabilityProvenance,
  currentTraceId,
}: CapabilityBlockSectionProps) {
  const [priorVersion, setPriorVersion] = useState<string | null>(null)
  const [priorTraceId, setPriorTraceId] = useState<string | null>(null)
  const [searched, setSearched] = useState(false)

  useEffect(() => {
    let cancelled = false
    // Fetch a small window of recent traces and look for the most recent
    // one with a different capability_block_version. This is best-effort:
    // if the operator wants a precise prior trace, they can navigate via
    // the trace list.
    api
      .listTraces({ limit: 25 })
      .then(async (resp) => {
        for (const summary of resp.traces) {
          if (cancelled) return
          if (summary.trace_id === currentTraceId) continue
          try {
            const d = await api.getTrace(summary.trace_id)
            if (cancelled) return
            const ver = (d.assembled_context as ProvenanceShape | undefined)
              ?.capability_block_version
            if (ver && ver !== version) {
              setPriorVersion(ver)
              setPriorTraceId(d.trace_id)
              break
            }
          } catch {
            // fall through to next summary
          }
        }
      })
      .catch(() => {
        // ignore — we'll just show "no prior version found"
      })
      .finally(() => !cancelled && setSearched(true))
    return () => {
      cancelled = true
    }
  }, [currentTraceId, version])

  return (
    <div style={styles.section}>
      <div style={styles.sectionHeader}>
        <span>Capability block</span>
        <code style={{ fontSize: 11, color: '#9ca3af' }}>{version || '<none>'}</code>
      </div>
      <div style={styles.sectionBody}>
        {reason && <div style={styles.reason}>{reason}</div>}
        {capabilityProvenance && (
          <pre style={styles.pre}>
            {JSON.stringify(capabilityProvenance, null, 2)}
          </pre>
        )}
        <div style={{ ...styles.capabilityDiff, marginTop: 10 }}>
          {!searched && 'searching for prior version…'}
          {searched && priorVersion && (
            <>
              prior trace <code>{priorTraceId}</code> ran with version{' '}
              <code>{priorVersion}</code>
              {priorVersion === version
                ? ' — unchanged'
                : ' — changed since last trace'}
            </>
          )}
          {searched && !priorVersion && (
            <span style={{ color: '#888' }}>
              no prior trace with a different version found in the last 25
              entries
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
