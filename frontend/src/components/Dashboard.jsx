import React, { useMemo, useRef, useState } from 'react'
import { Icon, STATUS, INTENT_LABEL, StatusChip, fmtMs, fmtTime } from './ui.jsx'
import { inr } from '../engine/core.js'

const ORDER = ['RESOLVED_AI', 'ESCALATED', 'RESOLVED_HUMAN', 'NEEDS_INFO']
const LEGEND = [
  ['RESOLVED_AI', 'Resolved by PayMates'],
  ['ESCALATED', 'Waiting for specialist'],
  ['RESOLVED_HUMAN', 'Resolved with specialist'],
  ['NEEDS_INFO', 'Asked for details'],
]
const glyph = (s) => (STATUS[s] || STATUS.OPEN).glyph

function Ribbon({ series, lastId }) {
  const [tip, setTip] = useState(null)
  if (!series.length) return <div className="empty">No tickets yet. Send a message on the Live desk, or run simulated traffic below.</div>
  return (
    <div className="chart-wrap" onMouseLeave={() => setTip(null)}>
      <div className="ribbon" role="list" aria-label="Every ticket, in order, coloured by outcome">
        {series.map((p) => (
          <span key={p.i} role="listitem" className={`cell ${p.status} ${p.i === lastId ? 'live' : ''} ${p.source === 'proactive' ? 'proactive' : ''}`}
            aria-label={`Ticket ${p.i}: ${STATUS[p.status]?.label}, ${INTENT_LABEL[p.intent] || ''}${p.source === 'proactive' ? ', found by PayMates' : ''}`}
            onMouseEnter={(e) => {
              const box = e.currentTarget.parentElement.getBoundingClientRect()
              const r = e.currentTarget.getBoundingClientRect()
              setTip({ x: r.left - box.left + r.width / 2, y: r.top - box.top, text: `#${p.i} · ${INTENT_LABEL[p.intent] || 'Unclear'} · ${STATUS[p.status]?.short}${p.source === 'simulated' ? ' · simulated' : p.source === 'proactive' ? ' · proactive' : ''}` })
            }}>
            {glyph(p.status)}
          </span>
        ))}
      </div>
      {tip && <div className="tip" style={{ left: tip.x, top: tip.y }}>{tip.text}</div>}
    </div>
  )
}

function RateChart({ series }) {
  const [hover, setHover] = useState(null)
  const ref = useRef(null)
  const W = 640, H = 220, L = 40, R = 16, T = 14, B = 28
  const pts = useMemo(() => {
    let ai = 0
    return series.map((p, idx) => { if (p.status === 'RESOLVED_AI') ai++; return { i: p.i, rate: ai / (idx + 1) } })
  }, [series])
  if (pts.length < 2) return <div className="empty">The resolution-rate line appears after two tickets.</div>
  const n = pts.length
  const x = (i) => L + ((i - 1) / Math.max(1, n - 1)) * (W - L - R)
  const y = (v) => T + (1 - v) * (H - T - B)
  const line = pts.map((p, k) => `${k ? 'L' : 'M'}${x(p.i).toFixed(1)},${y(p.rate).toFixed(1)}`).join(' ')
  const area = `${line} L${x(n).toFixed(1)},${y(0)} L${x(1)},${y(0)} Z`
  const ticks = [0, 0.25, 0.5, 0.75, 1]
  const xt = [1, Math.round(n / 2), n].filter((v, k, a) => a.indexOf(v) === k)
  const last = pts[n - 1]
  const onMove = (e) => {
    const box = ref.current.getBoundingClientRect()
    const px = ((e.clientX - box.left) / box.width) * W
    const i = Math.min(n, Math.max(1, Math.round(((px - L) / (W - L - R)) * (n - 1) + 1)))
    setHover(pts[i - 1])
  }
  const hp = hover || null
  return (
    <div className="chart-wrap">
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Share of tickets resolved without a human, cumulative. Now ${Math.round(last.rate * 100)} percent after ${n} tickets.`}
        onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <defs>
          <linearGradient id="rateFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="var(--ok)" stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--ok)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <g className="grid">{ticks.map((t) => <line key={t} x1={L} x2={W - R} y1={y(t)} y2={y(t)} />)}</g>
        <g className="axis">
          {ticks.map((t) => <text key={t} x={L - 8} y={y(t) + 4} textAnchor="end">{Math.round(t * 100)}%</text>)}
          {xt.map((i) => <text key={i} x={x(i)} y={H - 8} textAnchor="middle">#{i}</text>)}
        </g>
        <path d={area} fill="url(#rateFill)" />
        <path d={line} fill="none" stroke="var(--ok)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={x(last.i)} cy={y(last.rate)} r="4.5" fill="var(--ok)" stroke="var(--surface)" strokeWidth="2" />
        <text x={x(last.i) - 8} y={y(last.rate) - 10} textAnchor="end" fill="var(--ink)" fontSize="12" fontWeight="600">{Math.round(last.rate * 100)}%</text>
        {hp && (
          <g>
            <line x1={x(hp.i)} x2={x(hp.i)} y1={T} y2={H - B} stroke="var(--ink-2)" strokeDasharray="3 3" />
            <circle cx={x(hp.i)} cy={y(hp.rate)} r="5" fill="var(--ok)" stroke="var(--surface)" strokeWidth="2" />
          </g>
        )}
      </svg>
      {hp && (
        <div className="tip" style={{ left: `${(x(hp.i) / W) * 100}%`, top: `${(y(hp.rate) / H) * 100}%` }}>
          After ticket #{hp.i}: {Math.round(hp.rate * 100)}% resolved by PayMates
        </div>
      )}
    </div>
  )
}

function IntentBars({ byIntent }) {
  const rows = Object.entries(byIntent || {}).sort((a, b) => b[1].total - a[1].total)
  if (!rows.length) return <div className="empty">No tickets yet.</div>
  const max = Math.max(...rows.map(([, v]) => v.total))
  return (
    <div className="bars">
      <div className="legend"><span><i style={{ background: 'var(--ok)' }}>✓</i>Resolved by PayMates</span><span><i style={{ background: 'var(--hold-mark)' }}>!</i>Handed to a specialist or needs info</span></div>
      {rows.map(([k, v]) => (
        <div key={k} className="bar-row" title={`${INTENT_LABEL[k] || k}: ${v.ai} of ${v.total} resolved by PayMates`}>
          <span>{INTENT_LABEL[k] || k}</span>
          <span className="bar-track" style={{ width: `${(v.total / max) * 100}%` }}>
            {v.ai > 0 && <span style={{ flex: v.ai, background: 'var(--ok)' }} />}
            {v.total - v.ai > 0 && <span style={{ flex: v.total - v.ai, background: 'var(--hold-mark)' }} />}
          </span>
          <span className="n">{v.ai}/{v.total}</span>
        </div>
      ))}
    </div>
  )
}

// ₹ in lakh / crore, the way an Indian finance team reads it.
export function lakhCrore(n) {
  const sign = n < 0 ? '-' : ''
  const a = Math.abs(n)
  if (a >= 1e7) return `${sign}₹${(a / 1e7).toFixed(2)} crore`
  if (a >= 1e5) return `${sign}₹${(a / 1e5).toFixed(2)} lakh`
  return `${sign}${inr(a)}`
}

function Roi({ liveRate, hasTickets, manualMinutes }) {
  const [tickets, setTickets] = useState(100000)
  const [human, setHuman] = useState(50)
  const [ai, setAi] = useState(2)
  const [rate, setRate] = useState(null) // null = follow the live dashboard rate
  const r = rate ?? (hasTickets ? Math.round(liveRate * 100) : 60)
  const resolved = tickets * (r / 100)
  const monthly = resolved * human - tickets * ai // humans no longer handle the AI-resolved share; AI runs on every ticket
  const hours = (resolved * manualMinutes) / 60
  const num = (set) => (e) => set(Math.max(0, Number(e.target.value) || 0))
  return (
    <section className="panel roi">
      <div className="panel-head">
        <h2>ROI calculator</h2>
        <span className="chip hold">Example assumptions, replace with real numbers</span>
      </div>
      <div className="panel-body roi-body">
        <div className="roi-inputs">
          <label>Tickets per month<input type="number" min="0" step="1000" value={tickets} onChange={num(setTickets)} /></label>
          <label>Cost per human-handled ticket (₹)<input type="number" min="0" value={human} onChange={num(setHuman)} /></label>
          <label>AI cost per ticket (₹)<input type="number" min="0" step="0.5" value={ai} onChange={num(setAi)} /></label>
          <label>AI resolution rate (%)
            <input type="number" min="0" max="100" value={r} onChange={(e) => setRate(Math.min(100, Math.max(0, Number(e.target.value) || 0)))} />
            <small className="muted">{rate == null ? (hasTickets ? 'Following the live dashboard rate' : 'No tickets yet, so an example 60%') : <button className="link" onClick={() => setRate(null)}>Use the live rate</button>}</small>
          </label>
        </div>
        <div className="roi-out">
          <div><small>Monthly savings</small><b className="num">{lakhCrore(monthly)}</b></div>
          <div><small>Yearly savings</small><b className="num">{lakhCrore(monthly * 12)}</b></div>
          <div><small>Specialist hours freed / month</small><b className="num">{Math.round(hours).toLocaleString('en-IN')} h</b></div>
        </div>
        <p className="muted roi-note">Savings = tickets × resolution rate × human cost − tickets × AI cost. Hours = AI-resolved tickets × {manualMinutes} min. These inputs are illustrative assumptions, <b>not Paytm data</b>.</p>
      </div>
    </section>
  )
}

export function Dashboard({ metrics, tickets, audit, info, onSimulate, simState, onReset }) {
  const [n, setN] = useState(25)
  const m = metrics || { total: 0, ai_resolution_rate: 0, series: [], by_intent: {}, recovered_value: 0, hours_saved: 0, manual_minutes_baseline: 18 }
  const lastI = m.series.length ? m.series[m.series.length - 1].i : null
  const routing = info?.routing || {}
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Ops dashboard</h1>
          <p>Outcomes for every ticket PayMates has handled in this session. Figures come from simulated demo data; the method is what carries over to real tickets.</p>
        </div>
      </div>

      <div className="kpis">
        <section className="panel kpi">
          <span className="label">Resolved without a human</span>
          <span className="val">{Math.round(m.ai_resolution_rate * 100)}<small>%</small></span>
          <span className="muted">{m.resolved_ai} of {m.total} tickets</span>
          <span className="how">Resolved tickets ÷ all tickets</span>
        </section>
        <section className="panel kpi">
          <span className="label">Median time to resolution</span>
          <span className="val">{m.median_ai_ms == null ? '–' : fmtMs(m.median_ai_ms, true)}</span>
          <span className="muted">vs ~{m.manual_minutes_baseline} min by hand (assumed baseline)</span>
          <span className="how">Median of AI-resolved tickets, first message to closed, excluding on-screen pacing</span>
        </section>
        <section className="panel kpi">
          <span className="label">Failed-payment value recovered</span>
          <span className="val">{inr(m.recovered_value)}</span>
          <span className="muted">Bank-confirmed payments credited back to merchants</span>
          <span className="how">Sum of payments marked successful after a bank recheck</span>
        </section>
        <section className="panel kpi">
          <span className="label">Fixed before the merchant asked</span>
          <span className="val">{m.proactive_fixed || 0}</span>
          <span className="muted">Problems PayMates found and resolved on its own check-ins</span>
          <span className="how">Proactive tickets resolved without a human</span>
        </section>
        <section className="panel kpi">
          <span className="label">Specialist hours saved</span>
          <span className="val">{m.hours_saved}<small> h</small></span>
          <span className="muted">{m.escalated_open} waiting · {m.resolved_human} resolved with a specialist</span>
          <span className="how">AI-resolved tickets × {m.manual_minutes_baseline} min average handling time</span>
        </section>
      </div>

      <div className="grid-2">
        <section className="panel">
          <div className="panel-head"><h2>Every ticket, in order</h2><span className="spacer" />
            <div className="legend">{LEGEND.map(([s, label]) => <span key={s}><i className={`cell ${s}`} style={{ width: 14, height: 14, fontSize: 9, animation: 'none' }}>{glyph(s)}</i>{label}</span>)}<span><i style={{ background: 'var(--sky)', width: 8, height: 8, borderRadius: '50%' }} />Found by PayMates</span></div>
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 18 }}>
            <Ribbon series={m.series} lastId={lastI} />
            <div>
              <div className="eyebrow" style={{ marginBottom: 6 }}>Share resolved without a human, cumulative</div>
              <RateChart series={m.series} />
            </div>
            <div>
              <div className="eyebrow" style={{ marginBottom: 10 }}>By issue type</div>
              <IntentBars byIntent={m.by_intent} />
            </div>
          </div>
        </section>
        <div style={{ display: 'grid', gap: 18, alignContent: 'start' }}>
          <section className="panel">
            <div className="panel-head"><h2>Simulate merchant traffic</h2></div>
            <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
              <p className="muted" style={{ margin: 0 }}>Sends a batch of realistic tickets in English, Hindi and Hinglish from six simulated merchants through the same agents and guardrails, so the numbers above fill in live. The three demo merchants are not touched.</p>
              <div className="sim">
                <div className="seg" role="group" aria-label="Number of tickets">
                  {[10, 25, 50].map((v) => <button key={v} aria-pressed={n === v} onClick={() => setN(v)} disabled={simState.running}>{v}</button>)}
                </div>
                <button className="btn primary" onClick={() => onSimulate(n)} disabled={simState.running}><Icon.play />{simState.running ? 'Running…' : `Run ${n} tickets`}</button>
                <button className="btn" onClick={onReset} disabled={simState.running} title="Restore the original demo data"><Icon.reset />Reset demo</button>
              </div>
              {(simState.running || simState.done) && (
                <div className="sim">
                  <div className="progress" role="progressbar" aria-valuenow={simState.i} aria-valuemax={simState.n}><span style={{ width: `${(simState.i / Math.max(1, simState.n)) * 100}%` }} /></div>
                  <span className="muted num" style={{ fontSize: 12.5 }}>{simState.i}/{simState.n}</span>
                </div>
              )}
            </div>
          </section>
          <section className="panel">
            <div className="panel-head"><h2>Model routing</h2><span className="spacer" /><span className="chip">{info?.engine === 'backend' ? 'Python backend' : 'In-browser engine'}</span></div>
            <div className="panel-body routing">
              {['triage', 'action', 'responder', 'escalation'].map((a) => (
                <div key={a}><span style={{ textTransform: 'capitalize', fontWeight: 600 }}>{a}</span><span className="chip model" style={{ justifySelf: 'start' }}>{routing[a] || '–'}</span></div>
              ))}
              <div><span style={{ fontWeight: 600 }}>Council</span><span className="kv">
                {(info?.council || ['auditor:ledger', 'auditor:replay', 'auditor:velocity', ...(info?.live ? ['claude · quick tier'] : [])]).map((c) => <span key={c} className="chip model">{c}</span>)}
              </span></div>
            </div>
          </section>
        </div>
      </div>

      <Roi liveRate={m.ai_resolution_rate} hasTickets={m.total > 0} manualMinutes={m.manual_minutes_baseline || 18} />

      <div className="grid-2">
        <section className="panel">
          <div className="panel-head"><h2>Recent tickets</h2><span className="muted" style={{ fontSize: 12.5 }}>Also the table view of the charts above</span></div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Ticket</th><th>Merchant</th><th>Message</th><th>Issue</th><th>Outcome</th><th>Time</th><th>Recovered</th></tr></thead>
              <tbody>
                {tickets.slice(0, 14).map((t) => (
                  <tr key={t.id}>
                    <td className="mono" style={{ whiteSpace: 'nowrap' }}>{t.id}</td>
                    <td>{t.merchant_name || t.merchant_id}</td>
                    <td className="msg" title={t.message}>{t.source === 'proactive' && <span className="chip noticed" style={{ marginRight: 6 }}>proactive</span>}{t.message}</td>
                    <td>{INTENT_LABEL[t.intent] || '–'}</td>
                    <td><StatusChip status={t.status} /></td>
                    <td className="num" style={{ whiteSpace: 'nowrap' }}>{t.status === 'RESOLVED_AI' ? fmtMs(t.duration_ms) : '–'}</td>
                    <td className="num">{t.recovered_amount ? inr(t.recovered_amount) : '–'}</td>
                  </tr>
                ))}
                {!tickets.length && <tr><td colSpan="7" className="muted">No tickets yet.</td></tr>}
              </tbody>
            </table>
          </div>
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Audit log</h2><span className="muted" style={{ fontSize: 12.5 }}>Every action, who took it, which rule allowed it</span></div>
          <div className="audit">
            {audit.map((a) => (
              <div key={a.seq + '-' + a.ts}>
                <span className="muted">{fmtTime(a.ts)}</span>
                <span style={{ color: String(a.actor).startsWith('human') ? 'var(--human)' : 'var(--ink-2)' }}>{a.actor}</span>
                <span title={JSON.stringify(a.detail)}>
                  {a.action}
                  {a.detail && a.detail.allowed === false && <span style={{ color: 'var(--hold)' }}> · held: {a.detail.rule}</span>}
                  {a.detail && a.detail.allowed === true && a.detail.rule !== 'read-or-low-risk' && <span style={{ color: 'var(--ok)' }}> · {a.detail.rule}</span>}
                  {a.detail?.status && <span className="muted"> · {a.detail.status}</span>}
                </span>
              </div>
            ))}
            {!audit.length && <div className="muted" style={{ display: 'block' }}>Nothing logged yet.</div>}
          </div>
        </section>
      </div>
    </div>
  )
}
