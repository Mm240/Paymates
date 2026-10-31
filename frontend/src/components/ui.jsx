import React from 'react'

const S = (props) => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props} />
)

export const Icon = {
  triage: () => <S><path d="M3 5h18M6 12h12M10 19h4" /></S>,
  context: () => <S><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></S>,
  action: () => <S><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" /></S>,
  council: () => <S><circle cx="12" cy="5.5" r="2.2" /><circle cx="5" cy="11" r="2.2" /><circle cx="19" cy="11" r="2.2" /><path d="M3 20c0-2.6 1.6-4.4 4-4.4M21 20c0-2.6-1.6-4.4-4-4.4M8 20c0-3 1.8-5.2 4-5.2s4 2.2 4 5.2M12 8v3" /></S>,
  guardrails: () => <S><path d="M12 3 4 6v6c0 5 3.4 8.3 8 9 4.6-.7 8-4 8-9V6l-8-3z" /><path d="m9 12 2 2 4-4" /></S>,
  escalation: () => <S><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20c.6-3.4 3.3-5.5 6.5-5.5s5.9 2.1 6.5 5.5M17 8h5M19.5 5.5 22 8l-2.5 2.5" /></S>,
  responder: () => <S><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1.1-4.6A8 8 0 1 1 21 12z" /></S>,
  read: () => <S><circle cx="11" cy="11" r="6" /><path d="m20 20-4.5-4.5" /></S>,
  check: () => <S><path d="M3 12h4l3-8 4 16 3-8h4" /></S>,
  write: () => <S><path d="M4 20h4L19 9l-4-4L4 16v4z" /></S>,
  handoff: () => <S><path d="M7 17 17 7M8 7h9v9" /></S>,
  send: () => <S><path d="m5 12 14-7-5 15-3-6-6-2z" /></S>,
  speaker: () => <S><path d="M4 9v6h4l5 4V5L8 9H4z" /><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12" /></S>,
  mic: () => <S><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></S>,
  ok: () => <S><path d="m5 12 4.5 4.5L19 7" /></S>,
  hold: () => <S><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></S>,
  x: () => <S><path d="M6 6l12 12M18 6 6 18" /></S>,
  info: () => <S><circle cx="12" cy="12" r="9" /><path d="M12 8h.01M11 12h1v5h1" /></S>,
  play: () => <S><path d="M7 5v14l12-7L7 5z" /></S>,
  reset: () => <S><path d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5" /></S>,
  spark: () => <S><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6" /></S>,
}

export function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="9" fill="var(--navy)" />
      <path d="M9 23V9h7.5a4.5 4.5 0 010 9H13" stroke="#00B9F1" strokeWidth="3" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="22.5" cy="22" r="2.5" fill="#00B9F1" />
    </svg>
  )
}

export const STATUS = {
  RESOLVED_AI: { label: 'Resolved by PayMates', short: 'AI resolved', cls: 'ok', glyph: '✓' },
  ESCALATED: { label: 'Waiting for a specialist', short: 'With specialist', cls: 'hold', glyph: '!' },
  RESOLVED_HUMAN: { label: 'Resolved with a specialist', short: 'Specialist resolved', cls: 'human', glyph: 'H' },
  HUMAN_FOLLOWUP: { label: 'Specialist following up', short: 'Specialist follow-up', cls: 'human', glyph: 'H' },
  NEEDS_INFO: { label: 'Asked merchant for details', short: 'Needs info', cls: 'info', glyph: '?' },
  OPEN: { label: 'Working on it', short: 'Open', cls: 'sky', glyph: '•' },
}

export function StatusChip({ status }) {
  const s = STATUS[status] || STATUS.OPEN
  return <span className={`chip ${s.cls}`}><span aria-hidden="true">{s.glyph}</span>{s.short}</span>
}

export const INTENT_LABEL = {
  FAILED_PAYMENT: 'Failed payment', SETTLEMENT_DELAY: 'Settlement', REFUND_ISSUE: 'Refund', SOUNDBOX: 'Soundbox', KYC: 'KYC',
  HUMAN_REQUEST: 'Asked for a person', GENERAL: 'How-to question', UNKNOWN: 'Unclear',
}

export function fmtMs(ms, coarse = false) {
  if (ms == null) return '–'
  if (coarse && ms < 1000) return '< 1 s'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

export function fmtTime(ts) {
  if (!ts) return ''
  return new Date(ts).toLocaleTimeString('en-IN', { hour: 'numeric', minute: '2-digit' })
}

export function Toast({ text }) {
  if (!text) return null
  return <div className="toast" role="status">{text}</div>
}
