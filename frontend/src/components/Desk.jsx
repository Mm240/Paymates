import React, { useEffect, useRef, useState } from 'react'
import { Icon, STATUS, INTENT_LABEL, fmtMs, fmtTime } from './ui.jsx'
import { inr, TPL } from '../engine/core.js'

export const SUGGEST = {
  M1001: [
    { tag: 'Demo 1', text: "Customer's payment failed but money was deducted." },
    { tag: 'Demo 2', text: 'Kal ka settlement nahi aaya' },
    { text: 'Soundbox ki awaaz theek hai kya? Check karo' },
    { text: 'Statement kaise download karein?' },
  ],
  M1002: [
    { tag: 'Demo 3', text: 'Customer says refund not received.' },
    { text: 'Change my Soundbox announcement language to Tamil' },
    { text: 'Which documents do I need for KYC?' },
    { text: 'I want to talk to a real person about a customer dispute.' },
  ],
  M1003: [
    { tag: 'Demo 4', text: 'ग्राहक के पैसे कट गए पर पेमेंट फेल दिखा रहा है' },
    { text: 'मेरा साउंडबॉक्स भुगतान बोल नहीं रहा' },
    { text: 'कल का सेटलमेंट अभी तक बैंक में नहीं आया' },
    { text: 'Something is off with my account, can you look into it?' },
  ],
}

export const GREETING = {
  hinglish: (n) => `Namaste ${n} ji! Main PayMates hoon, aapka support teammate. Payment, settlement, refund ya Soundbox, kuch bhi poochiye.`,
  en: (n) => `Hi ${n}! I'm PayMates, your support teammate. Ask me about payments, settlements, refunds or your Soundbox.`,
  hi: (n) => `नमस्ते ${n} जी! मैं PayMates हूँ, आपका सपोर्ट साथी। भुगतान, सेटलमेंट, रिफंड या साउंडबॉक्स, कुछ भी पूछिए।`,
}
const LANG_TAG = { hinglish: 'Hinglish', en: 'English', hi: 'हिन्दी' }
const isDeva = (t) => /[ऀ-ॿ]/.test(t || '')

// ====================================================================== rich reply card (generative UI, built from facts)
const STATUS_ICON = {
  success: () => <Icon.ok />,
  held: () => <Icon.hold />,
  pending: () => <Icon.hold />,
  info: () => <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></svg>,
}

export function ReplyCard({ card, onAction }) {
  const Ic = STATUS_ICON[card.status] || STATUS_ICON.info
  const total = (card.bars || []).reduce((a, b) => a + b.value, 0)
  return (
    <div className={`rcard ${card.status}`} data-type={card.type}>
      <div className="rcard-top">
        <span className="rcard-title">{card.title}</span>
        <span className={`rcard-chip ${card.status}`}><Ic />{card.status_label}</span>
      </div>
      {card.amount && <div className="rcard-amount num">{card.amount}</div>}
      {card.bars && (
        <div className="rcard-bars">
          <div className="rcard-bar" role="img" aria-label={card.bars.map((b) => `${b.label} ${b.amount}`).join(', ')}>
            {card.bars.map((b) => <span key={b.kind} className={b.kind} style={{ flexGrow: total ? b.value : 1 }} />)}
          </div>
          <div className="rcard-legend">
            {card.bars.map((b) => <span key={b.kind}><i className={b.kind} />{b.label}<b className="num">{b.amount}</b></span>)}
          </div>
        </div>
      )}
      {card.steps?.length > 0 && (
        <ol className={`rcard-steps n${card.steps.length}`}>
          {card.steps.map((st, i) => <li key={i} className={st.state}><span className="dot" />{st.label}</li>)}
        </ol>
      )}
      {(card.refs.length > 0 || card.rows.length > 0) && (
        <dl className="rcard-kv">
          {card.refs.map((r, i) => <div key={`r${i}`}><dt>{r.label}</dt><dd className="mono">{r.value}</dd></div>)}
          {card.rows.map((r, i) => <div key={`w${i}`}><dt>{r.label}</dt><dd className={r.vote ? `vote ${r.vote}` : ''}>{r.value}</dd></div>)}
        </dl>
      )}
      {card.actions?.length > 0 && (
        <div className="rcard-actions">
          {card.actions.map((a, i) => <button key={a.action} type="button" className={i === 0 ? 'primary' : ''} onClick={() => onAction(a.action, card)}>{a.label}</button>)}
        </div>
      )}
    </div>
  )
}

// ====================================================================== phone
export function MerchantPhone({ merchants, merchantId, onPick, messages, busy, busyLabel, onSend, onCheckin, onHisaab, onToast, voice, onVoice, canMic }) {
  const [text, setText] = useState('')
  const inputRef = useRef(null)
  const [listening, setListening] = useState(false)
  const chatRef = useRef(null)
  const m = merchants.find((x) => x.id === merchantId)
  useEffect(() => {
    const el = chatRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, busy, merchantId])

  const submit = (t) => {
    const v = (t ?? text).trim()
    if (!v || busy) return
    onSend(v)
    setText('')
  }
  // Card buttons do real things: send a message, focus the input, speak, or confirm with a toast.
  const cardAction = (action, card) => {
    const lang = m?.lang || 'en'
    if (action === 'talk_to_specialist') submit(TPL.cards.labels.m_talk_to_specialist[lang])
    else if (action === 'add_txn_id') { setText('Txn ID: '); setTimeout(() => inputRef.current?.focus(), 0) }
    else if (action === 'upload_kyc') onToast?.('KYC upload opens in the Paytm for Business app (demo)')
    else if (action === 'track') {
      const ref = card.refs.find((r) => /UTR|Ticket|टिकट|Ref|Txn/.test(r.label)) || card.refs[0]
      onToast?.(`Tracking ${ref ? `${ref.label} ${ref.value}` : card.title}: you'll get an update here`)
    } else if (action === 'play_test') {
      try {
        speechSynthesis.cancel()
        const u = new SpeechSynthesisUtterance('₹1 received on PayMates')
        u.lang = 'en-IN'
        speechSynthesis.speak(u)
        onToast?.('Playing a test announcement: "₹1 received on PayMates"')
      } catch { onToast?.('This browser cannot play the test announcement') }
    }
  }
  const listen = () => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SR || listening) return
    const rec = new SR()
    rec.lang = m?.lang === 'hi' ? 'hi-IN' : 'en-IN'
    rec.interimResults = false
    rec.onresult = (e) => setText(e.results[0][0].transcript)
    rec.onend = () => setListening(false)
    rec.onerror = () => setListening(false)
    setListening(true)
    try { rec.start() } catch { setListening(false) }
  }

  return (
    <div>
      <div className="merchant-switch" role="group" aria-label="Choose merchant">
        {merchants.map((x) => (
          <button key={x.id} aria-pressed={x.id === merchantId} onClick={() => onPick(x.id)} disabled={busy}>
            <b>{x.name}</b>
            <span>{x.owner.split(' ')[0]} · {x.city} · {LANG_TAG[x.lang]}</span>
          </button>
        ))}
      </div>
      <div className="phone">
        <div className="phone-screen">
          <div className="phone-status"><span>9:41</span><span className="notch" /><span>4G ▮▮▮</span></div>
          <div className="phone-app">
            <div className="avatar pm" aria-hidden="true">P</div>
            <div className="t">
              <b>PayMates</b>
              <small><span className="dot" />Support for {m?.name}</small>
            </div>
            <button className="icon-btn" aria-pressed={voice} onClick={onVoice} title="Read replies aloud, Soundbox style" aria-label="Read replies aloud">
              <Icon.speaker />
            </button>
          </div>
          <div className="chat" ref={chatRef} aria-live="polite">
            <span className="day">Today</span>
            {m && (
              <div className="bubble-wrap">
                <div className="bubble bot" lang={m.lang === 'hi' ? 'hi' : undefined}>{GREETING[m.lang](m.owner.split(' ')[0])}</div>
              </div>
            )}
            {messages.map((msg, i) => (
              <div key={i} className={`bubble-wrap ${msg.role === 'merchant' ? 'me' : ''}`}>
                {msg.role === 'human' && <div className="bubble-meta"><span className="avatar human" style={{ width: 18, height: 18, fontSize: 10, borderRadius: 6 }}>{msg.from?.[0] || 'S'}</span>{msg.from} · Specialist</div>}
                {msg.proactive && <div className="bubble-meta"><span className="chip noticed"><Icon.watcher />PayMates noticed</span></div>}
                {msg.hisaab && <div className="bubble-meta"><span className="chip hisaab">₹ Today's hisaab</span></div>}
                <div className={`bubble ${msg.role === 'merchant' ? 'me' : msg.role === 'human' ? 'human' : 'bot'}${msg.card ? ' has-card' : ''}`} lang={isDeva(msg.text) ? 'hi' : undefined}>
                  {msg.card && <ReplyCard card={msg.card} onAction={cardAction} />}
                  {msg.text}
                </div>
                {msg.role === 'bot' && msg.ticket && (
                  <div className="bubble-meta">
                    <span className={`chip ${STATUS[msg.ticket.status]?.cls || ''}`} style={{ padding: '0 7px', fontSize: 11 }}>{STATUS[msg.ticket.status]?.short}</span>
                    <span className="mono">{msg.ticket.id}</span>
                    {msg.ticket.status === 'RESOLVED_AI' && <span>· {fmtMs(msg.ticket.duration_ms)}</span>}
                  </div>
                )}
                {msg.role === 'merchant' && <div className="bubble-meta">{fmtTime(msg.ts)}</div>}
              </div>
            ))}
            {busy && <div className="typing"><i /><i /><i /> {busyLabel || 'PayMates is checking'}</div>}
          </div>
          <div className="checkin-row">
            <button className="checkin-btn" onClick={onCheckin} disabled={busy} title="PayMates scans this merchant's payments, settlements, refunds and Soundbox and fixes what it can">
              <Icon.watcher />PayMates check-in
            </button>
            <button className="hisaab-btn" onClick={onHisaab} disabled={busy} title="Today's money in one message: UPI received, card/wallet pending, and what PayMates fixed">
              ₹ Today's hisaab
            </button>
          </div>
          <div className="suggest" aria-label="Try a message">
            {(SUGGEST[merchantId] || []).map((s) => (
              <button key={s.text} onClick={() => submit(s.text)} disabled={busy} title={s.text} lang={isDeva(s.text) ? 'hi' : undefined}>
                {s.tag && <span className="tag">{s.tag}</span>}{s.text}
              </button>
            ))}
          </div>
          <form className="composer" onSubmit={(e) => { e.preventDefault(); submit() }}>
            <label htmlFor="composer" className="sr-only" style={{ position: 'absolute', left: -9999 }}>Message</label>
            <input id="composer" ref={inputRef} value={text} onChange={(e) => setText(e.target.value)} placeholder={m?.lang === 'hi' ? 'अपनी समस्या लिखें…' : m?.lang === 'hinglish' ? 'Apni problem likhiye…' : 'Describe your issue…'} disabled={busy} />
            {canMic && (
              <button type="button" className="icon-btn" onClick={listen} aria-pressed={listening} aria-label="Speak your message" title="Speak">
                <Icon.mic />
              </button>
            )}
            <button className="send" disabled={busy || !text.trim()} aria-label="Send"><Icon.send /></button>
          </form>
        </div>
      </div>
    </div>
  )
}

// ====================================================================== trace
export const STAGES = [
  { key: 'watcher', name: 'Watcher', job: 'Finds problems in the records before the merchant asks' },
  { key: 'triage', name: 'Triage', job: 'Understands the message' },
  { key: 'context', name: 'Context', job: 'Pulls account history and help docs' },
  { key: 'action', name: 'Action', job: 'Decides, then acts through tools' },
  { key: 'council', name: 'Council', job: 'Independent reviewers must agree before money moves' },
  { key: 'guardrails', name: 'Guardrails', job: 'Checks every action before it runs' },
  { key: 'escalation', name: 'Escalation', job: 'Briefs a human when needed' },
  { key: 'responder', name: 'Reply', job: "Answers in the merchant's language" },
]

function short(v) {
  if (v == null) return ''
  if (typeof v === 'string') return v.length > 24 ? `"${v.slice(0, 22)}…"` : `"${v}"`
  return JSON.stringify(v)
}

function toolSummary(t) {
  const r = t.result || {}
  if (r.blocked) return r.reason
  if (r.error) return r.error
  switch (t.name) {
    case 'list_transactions': return `${(r.transactions || []).length} payment(s): ` + (r.transactions || []).slice(0, 3).map((x) => `${x.txn_id} ${x.amount_display} ${x.gateway_status}${x.customer_debited && x.gateway_status === 'FAILED' ? ' (debited)' : ''}`).join(' · ')
    case 'get_transaction': return `${r.txn_id} · ${r.amount_display} · ${r.gateway_status} · customer ${r.customer_debited ? 'debited' : 'not debited'}`
    case 'recheck_payment_status': return `Bank says ${r.bank_final_status}. ${r.note}`
    case 'mark_payment_recovered': return r.already_recovered ? 'Already recovered' : `Marked SUCCESS · ${r.amount_display} added to next settlement`
    case 'list_settlements': return (r.settlements || []).slice(0, 3).map((s) => `${s.settlement_id} ${s.amount_display} ${s.status}${s.hold_reason ? ` (${s.hold_reason})` : ''}`).join(' · ')
    case 'retry_settlement': return `Re-sent ${r.amount_display} · new UTR ${r.new_utr} · ETA ${r.eta_minutes} min`
    case 'list_refunds': return (r.refunds || []).slice(0, 3).map((x) => `${x.refund_id} ${x.amount_display} ${x.status}`).join(' · ')
    case 'get_refund': return `${r.refund_id} · ${r.amount_display} · ${r.status}`
    case 'reinitiate_refund': return `Re-sent ${r.amount_display} · ref ${r.new_reference}`
    case 'get_kyc_status': return `KYC ${r.kyc_status}${r.missing_documents?.length ? ` · missing: ${r.missing_documents.join(', ')}` : ''}`
    case 'run_soundbox_diagnostics': return `${r.device_id} · volume ${r.volume}% · signal ${r.signal} · battery ${r.battery}% · last ping ${r.last_ping_minutes} min · ${r.missed_announcements} missed · issues: ${(r.issues || []).join(', ') || 'none'}`
    case 'reconnect_device': return `Reconnected ${r.device_id}${r.switched_to_backup_network ? ' · moved to backup network' : ''} · signal ${r.signal}`
    case 'resend_missed_announcements': return `Re-announced ${r.replayed} missed payment(s)`
    case 'set_announcement_language': return `Announcements now in ${r.language}`
    case 'play_test_announcement': return r.audible ? `Test announcement played at volume ${r.volume}%` : 'Test announcement sent, but volume is 0'
    case 'raise_ticket': return `Opened ${r.ticket_ref} (${r.priority}) in ${r.queue}`
    case 'search_help_center': return (r.results || []).slice(0, 2).map((d) => `${d.id} ${d.title} (${d.score})`).join(' · ') || 'No match'
    case 'escalate_to_human': return 'Handed to a specialist'
    default: return 'Done'
  }
}

function ToolCall({ t }) {
  const held = t.result?.blocked && t.result?.requires_approval
  const denied = t.result?.blocked && !held
  const KindIcon = Icon[t.kind] || Icon.read
  const argStr = Object.entries(t.args || {}).map(([k, v]) => `${k}: ${short(v)}`).join(', ')
  return (
    <div className={`tool ${t.kind === 'write' ? 'write' : ''} ${held ? 'held' : ''} ${denied ? 'denied' : ''}`}>
      <div className="tool-row">
        <span style={{ color: held ? 'var(--hold)' : t.kind === 'write' ? 'var(--sky)' : 'var(--muted)' }} title={t.kind}><KindIcon /></span>
        <span className="fn"><b>{t.name}</b><span>({argStr})</span></span>
        {t.money || t.kind === 'write' ? (
          held ? <span className="chip hold">Held for approval</span>
            : denied ? <span className="chip bad">Blocked</span>
              : <span className="chip ok">Allowed</span>
        ) : <span className="chip">{t.kind}</span>}
      </div>
      <div className="tool-res">
        {toolSummary(t)}
        {(t.money || t.kind === 'write') && !t.result?.blocked && <div className="why">Guardrail · {t.guard.rule}: {t.guard.reason}</div>}
      </div>
      <details className="raw"><summary>Raw response</summary><pre>{JSON.stringify(t.result, null, 2)}</pre></details>
    </div>
  )
}

const VOTE = { APPROVE: ['ok', 'Approve'], HOLD: ['hold', 'Hold'], ABSTAIN: ['', 'Abstain'] }
const TOOL_VERB = { mark_payment_recovered: 'Credit', retry_settlement: 'Re-send payout', reinitiate_refund: 'Re-send refund' }

function CouncilSession({ x }) {
  const approve = x.votes.filter((v) => v.vote === 'APPROVE').length
  return (
    <div className={`council ${x.approved ? 'pass' : 'held'}`}>
      <div className="council-head">
        <span className="council-what">{TOOL_VERB[x.tool] || x.tool} <b className="num">{inr(x.amount)}</b> <span className="mono muted">{x.args.txn_id || x.args.settlement_id || x.args.refund_id}</span></span>
        <span className="council-tally" aria-label={`${approve} of ${x.votes.length} approved`}>
          {x.votes.map((v, i) => <i key={i} className={`pip ${VOTE[v.vote][0]}`} />)}
        </span>
        <span className={`chip ${x.approved ? 'ok' : 'hold'}`}>{x.approved ? 'Approved' : 'Held for a human'}</span>
      </div>
      <div className="seats">
        {x.votes.map((v, i) => (
          <div key={i} className={`seat ${VOTE[v.vote][0]}`} style={{ animationDelay: `${i * 90}ms` }}>
            <div className="seat-top">
              <span className={`seat-badge ${v.kind}`}>{v.kind === 'model' ? 'AI' : '</>'}</span>
              <b>{v.seat}</b>
              <span className={`chip ${VOTE[v.vote][0]}`}>{VOTE[v.vote][1]}</span>
            </div>
            <div className="seat-why">{v.reason}</div>
            {v.kind === 'model' && <div className="seat-who mono">{v.who}{v.ms ? ` · ${fmtMs(v.ms)}` : ''}</div>}
          </div>
        ))}
      </div>
      <div className="council-foot muted">{x.reason} · auditors can veto, AI jurors vote by majority</div>
    </div>
  )
}

function StageBody({ k, s, tools, reply }) {
  const d = s?.data
  if (k === 'action' && tools.length) {
    return (
      <div className="stage-body">
        {tools.map((t, i) => <ToolCall key={i} t={t} />)}
        {d?.summary && <div className="muted">{d.summary}</div>}
        {d?.fallback && <div className="chip hold" style={{ whiteSpace: 'normal' }}>{d.fallback}</div>}
      </div>
    )
  }
  if (!d) return null
  if (k === 'triage') {
    const pct = Math.round(d.confidence * 100)
    return (
      <div className="stage-body">
        <div className="kv">
          <span className="chip sky">{INTENT_LABEL[d.intent] || d.intent}</span>
          <span className="chip">{{ en: 'English', hi: 'Hindi', hinglish: 'Hinglish' }[d.language]}</span>
          <span className={`chip ${d.urgency === 'high' ? 'hold' : ''}`}>{d.urgency} urgency</span>
          {Object.entries(d.entities || {}).map(([key, v]) => <span key={key} className="chip mono">{key}: {key === 'amount' ? inr(v) : v}</span>)}
        </div>
        <div>
          <div className="kv" style={{ justifyContent: 'space-between', fontSize: 12 }}><span>Confidence <b className="num">{pct}%</b></span><span className="muted">Acts on its own above 60%</span></div>
          <div className="meter" role="img" aria-label={`Confidence ${pct} percent, threshold 60 percent`}>
            <span style={{ width: `${pct}%`, background: pct >= 60 ? 'var(--ok)' : 'var(--hold-mark)' }} />
            <i className="mark" style={{ left: '60%' }} />
          </div>
        </div>
        {d.reasoning && <div className="muted" style={{ fontSize: 12.5 }}>{d.reasoning}</div>}
      </div>
    )
  }
  if (k === 'context') {
    return (
      <div className="stage-body">
        {d.highlights?.length > 0 && <ul>{d.highlights.map((h, i) => <li key={i} className="mono" style={{ fontSize: 12 }}>{h}</li>)}</ul>}
        {d.kb?.length > 0 && (
          <div className="kv">{d.kb.map((doc) => <span key={doc.id} className="chip" title={doc.title}>{doc.id} · {doc.title.slice(0, 32)}{doc.title.length > 32 ? '…' : ''} <b className="num">{doc.score}</b></span>)}</div>
        )}
      </div>
    )
  }
  if (k === 'action') {
    return <div className="stage-body"><div className="muted">{d.decision === 'handoff' ? 'Policy: a merchant who asks for a person always gets one.' : d.decision === 'escalate' ? `Confidence is below the ${Math.round((d.threshold || 0.6) * 100)}% threshold, so no action is taken.` : d.summary}</div></div>
  }
  if (k === 'council') {
    if (!d.sessions?.length) return null
    return (
      <div className="stage-body">
        {d.sessions.map((x, i) => <CouncilSession key={i} x={x} />)}
      </div>
    )
  }
  if (k === 'guardrails') {
    return (
      <div className="stage-body">
        <div className="kv">{(d.rules || []).map((r) => <span key={r} className="chip mono">{r}</span>)}<span className="chip">limit {inr(d.limit)}</span></div>
        {d.held?.length > 0 && d.held.map((h, i) => <div key={i} className="chip hold" style={{ whiteSpace: 'normal' }}>Held: {h.tool} · {h.reason}</div>)}
      </div>
    )
  }
  if (k === 'escalation') {
    const b = d.escalation?.briefing
    if (!b) return null
    return (
      <div className="stage-body">
        <b>{b.headline}</b>
        <div className="muted">{b.summary}</div>
        {b.risk?.length > 0 && <div className="kv">{b.risk.map((r) => <span key={r} className="chip hold">{r}</span>)}</div>}
      </div>
    )
  }
  if (k === 'responder' && reply) {
    return (
      <div className="stage-body">
        <div className="reply-box" lang={isDeva(reply) ? 'hi' : undefined}>{reply}</div>
        <div className="kv"><span className={`chip ${d.grounded === false ? 'hold' : 'ok'}`}>{d.grounded === false ? 'Model rewrite rejected, verified template sent' : 'Amounts and IDs match tool results'}</span></div>
      </div>
    )
  }
  return null
}

export function AgentTrace({ trace, merchants, onTry, live }) {
  const started = Boolean(trace)
  const done = trace?.ticket && trace.ticket.status !== 'OPEN'
  const m = merchants.find((x) => x.id === trace?.merchantId)
  return (
    <section className="panel" aria-label="Agent trace">
      <div className="panel-head trace-head">
        <h2>Agent trace</h2>
        {trace?.ticketId && <span className="chip mono">{trace.ticketId}</span>}
        {m && <span className="muted" style={{ fontSize: 12.5 }}>{m.name}, {m.city}</span>}
        <span className="spacer" />
        {live && <span className="chip sky"><Icon.spark /> Claude live</span>}
      </div>
      <div className="panel-body">
        {!started && (
          <div className="empty-trace">
            <h3>Watch the agent team work</h3>
            <p style={{ margin: '0 auto 14px', maxWidth: '46ch' }}>Send a message from the merchant's phone. Each agent lights up as it reads, checks records, acts through guarded tools and replies.</p>
            <div className="kv" style={{ justifyContent: 'center' }}>
              <button className="btn small" onClick={() => onTry('M1001', SUGGEST.M1001[0].text)}>Demo 1 · Failed payment</button>
              <button className="btn small" onClick={() => onTry('M1001', SUGGEST.M1001[1].text)}>Demo 2 · Late settlement</button>
              <button className="btn small" onClick={() => onTry('M1002', SUGGEST.M1002[0].text)}>Demo 3 · Refund over limit</button>
              <button className="btn small" onClick={() => onTry('M1003', SUGGEST.M1003[0].text)}>Demo 4 · Council veto</button>
            </div>
          </div>
        )}
        {started && trace.message && (
          <div className="quote" style={{ marginBottom: 14 }} lang={isDeva(trace.message) ? 'hi' : undefined}>“{trace.message}”</div>
        )}
        <ol className="pipeline">
          {STAGES.map((st) => {
            const s = trace?.stages?.[st.key]
            const status = s?.status || (done ? 'skipped' : 'idle')
            const StageIcon = Icon[st.key]
            const tools = st.key === 'action' ? trace?.tools || [] : []
            return (
              <li key={st.key} className="stage" data-status={status}>
                <span className="stage-icon"><StageIcon /></span>
                <div className="stage-card">
                  <div className="stage-top">
                    <span className="stage-name">{st.name}</span>
                    <span className="stage-title">{s?.title || (status === 'skipped' ? 'Not needed for this ticket' : st.job)}</span>
                    <span className="stage-meta">
                      {s?.model && <span className="chip model" title="Model or engine used">{s.model}</span>}
                      {s?.ms != null && s.status !== 'running' && <span className="chip num">{fmtMs(s.ms)}</span>}
                    </span>
                  </div>
                  <StageBody k={st.key} s={s} tools={tools} reply={trace?.reply} />
                </div>
              </li>
            )
          })}
        </ol>
      </div>
    </section>
  )
}

// ====================================================================== outcome side
export function OutcomeSide({ trace, metrics, openQueue, goDashboard, onConsole }) {
  const t = trace?.ticket
  const st = t ? STATUS[t.status] || STATUS.OPEN : null
  const tools = trace?.tools || []
  const writes = tools.filter((x) => x.kind === 'write' && x.guard?.allowed).length
  return (
    <aside className="side">
      <section className="panel" aria-label="This ticket">
        <div className="panel-head"><h2>This ticket</h2>{t && <span className="chip mono">{t.id}</span>}</div>
        {!t && <div className="outcome muted">The outcome of the current message appears here: who resolved it, how long it took, and any money recovered.</div>}
        {t && (
          <div className="outcome">
            <div className="status-big">
              <span className="ic" style={{ background: `var(--${st.cls}-soft)`, color: `var(--${st.cls === 'sky' ? 'sky' : st.cls})` }}>
                {t.status === 'RESOLVED_AI' ? <Icon.ok /> : t.status === 'ESCALATED' ? <Icon.hold /> : t.status === 'OPEN' ? <Icon.spark /> : <Icon.info />}
              </span>
              <div><b>{st.label}</b><span className="muted" style={{ fontSize: 12.5 }}>{INTENT_LABEL[t.intent] || 'Reading…'}{t.lang ? ` · ${{ en: 'English', hi: 'Hindi', hinglish: 'Hinglish' }[t.lang]}` : ''}</span></div>
            </div>
            {t.summary && <div style={{ fontSize: 13 }}>{t.summary}</div>}
            <div className="facts">
              <div className="fact"><small>Time to resolve</small><b>{t.status === 'RESOLVED_AI' ? fmtMs(t.duration_ms) : '–'}</b></div>
              <div className="fact"><small>Money recovered</small><b>{inr(t.recovered_amount || 0)}</b></div>
              <div className="fact"><small>Tool calls</small><b className="num">{tools.length}</b></div>
              <div className="fact"><small>Actions taken</small><b className="num">{writes}</b></div>
            </div>
            {t.status === 'ESCALATED' && (
              <button className="btn primary" onClick={onConsole}>Open human console</button>
            )}
          </div>
        )}
      </section>
      <section className="panel" aria-label="Team scoreboard">
        <div className="panel-head"><h2>Today so far</h2><span className="spacer" /><button className="link" onClick={goDashboard}>Dashboard →</button></div>
        <div className="mini-kpis">
          <div><b>{metrics ? `${Math.round(metrics.ai_resolution_rate * 100)}%` : '–'}</b><small>Resolved without a human</small></div>
          <div><b>{metrics?.total ?? 0}</b><small>Tickets handled</small></div>
          <div><b>{inr(metrics?.recovered_value || 0)}</b><small>Failed payments recovered</small></div>
          <div><b className="num" style={{ color: openQueue ? 'var(--hold)' : undefined }}>{openQueue}</b><small>Waiting for a specialist</small></div>
        </div>
      </section>
    </aside>
  )
}
