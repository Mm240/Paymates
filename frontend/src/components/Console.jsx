import React, { useEffect, useState } from 'react'
import { Icon, INTENT_LABEL, fmtTime } from './ui.jsx'

const isDeva = (t) => /[ऀ-ॿ]/.test(t || '')

function AutonomyDial({ policy, onPolicy }) {
  if (!policy) return null
  const cur = policy.levels.find((x) => x.id === policy.autonomy_level)
  return (
    <section className="panel autonomy">
      <div className="autonomy-top">
        <div>
          <div className="eyebrow">Autonomy level</div>
          <div className="muted" style={{ fontSize: 12.5 }}>Ops decides how much PayMates may do on its own. Every change is written to the audit log.</div>
        </div>
        <div className="seg" role="radiogroup" aria-label="Autonomy level">
          {policy.levels.map((l) => (
            <button key={l.id} role="radio" aria-checked={l.id === policy.autonomy_level} aria-pressed={l.id === policy.autonomy_level} onClick={() => onPolicy(l.id)}>{l.label}</button>
          ))}
        </div>
      </div>
      <ul className="autonomy-levels">
        {policy.levels.map((l) => (
          <li key={l.id} aria-current={l.id === cur?.id}><b>{l.label}</b>{l.explain}</li>
        ))}
      </ul>
    </section>
  )
}

export function HumanConsole({ escalations, onDecide, busyId, policy, onPolicy }) {
  const [sel, setSel] = useState(null)
  const [note, setNote] = useState('')
  const [specialist, setSpecialist] = useState('Priya (Support)')
  const open = escalations.filter((e) => e.status === 'OPEN')
  useEffect(() => {
    if (!escalations.length) { setSel(null); return }
    if (!sel || !escalations.find((e) => e.id === sel)) setSel((open[0] || escalations[0]).id)
  }, [escalations])  // eslint-disable-line react-hooks/exhaustive-deps
  const e = escalations.find((x) => x.id === sel)
  const b = e?.briefing

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Human console</h1>
          <p>Cases PayMates handed over. Each one arrives with a briefing: what the merchant said, what was already checked, and the exact action proposed. Approving runs that action and replies to the merchant.</p>
        </div>
      </div>
      <AutonomyDial policy={policy} onPolicy={onPolicy} />
      {!escalations.length && (
        <section className="panel empty">
          <p style={{ margin: '0 0 6px', color: 'var(--ink)', fontWeight: 600 }}>No handovers yet</p>
          <p style={{ margin: 0 }}>Try Demo 3 on the Live desk: a ₹7,800 refund is above PayMates' ₹5,000 limit, so it lands here for approval.</p>
        </section>
      )}
      {escalations.length > 0 && (
        <div className="console">
          <section className="panel">
            <div className="panel-head"><h2>Queue</h2><span className={`chip ${open.length ? 'hold' : 'ok'}`}>{open.length} open</span></div>
            <div className="queue">
              {escalations.map((x) => (
                <button key={x.id} aria-current={x.id === sel} onClick={() => { setSel(x.id); setNote('') }}>
                  <span className="kv" style={{ justifyContent: 'space-between' }}>
                    <span className="mono" style={{ fontSize: 12 }}>{x.id} · {x.ticket_id}</span>
                    <span className={`chip ${x.status === 'OPEN' ? 'hold' : x.status === 'APPROVED' ? 'ok' : 'bad'}`}>{x.status === 'OPEN' ? 'Open' : x.status === 'APPROVED' ? 'Approved' : 'Declined'}</span>
                  </span>
                  <b>{x.briefing?.headline}</b>
                  <span className="muted" style={{ fontSize: 12 }}>{x.briefing?.merchant?.name}, {x.briefing?.merchant?.city} · {fmtTime(x.created_at)}</span>
                </button>
              ))}
            </div>
          </section>
          {e && b && (
            <section className="panel">
              <div className="panel-body brief">
                <div>
                  <div className="eyebrow">{INTENT_LABEL[b.intent] || b.intent} · confidence {Math.round((b.confidence || 0) * 100)}% · replies in {{ en: 'English', hi: 'Hindi', hinglish: 'Hinglish' }[b.merchant?.language] || 'English'}</div>
                  <h2 style={{ marginTop: 4 }}>{b.headline}</h2>
                  <p className="muted" style={{ margin: '6px 0 0' }}>{b.merchant?.owner} · {b.merchant?.name}, {b.merchant?.city} · {b.merchant?.plan}</p>
                </div>
                <div>
                  <div className="eyebrow" style={{ marginBottom: 6 }}>Briefing</div>
                  <p style={{ margin: 0 }}>{b.summary}</p>
                </div>
                <div>
                  <div className="eyebrow" style={{ marginBottom: 6 }}>What the merchant said</div>
                  <div className="quote" lang={isDeva(b.said) ? 'hi' : undefined}>“{b.said}”</div>
                </div>
                {b.checks?.length > 0 && (
                  <div>
                    <div className="eyebrow" style={{ marginBottom: 6 }}>Already checked by PayMates</div>
                    <ul className="checklist">
                      {b.checks.map((c, i) => (
                        <li key={i}><span style={{ color: c.startsWith('Held') ? 'var(--hold)' : 'var(--ok)' }}>{c.startsWith('Held') ? <Icon.hold /> : <Icon.ok />}</span><span className="mono" style={{ fontSize: 12.5 }}>{c}</span></li>
                      ))}
                    </ul>
                  </div>
                )}
                {b.risk?.length > 0 && (
                  <div>
                    <div className="eyebrow" style={{ marginBottom: 6 }}>Why a human</div>
                    <div className="risk">{b.risk.map((r) => <span key={r} className="chip hold">{r}</span>)}</div>
                  </div>
                )}
                {e.proposed_action ? (
                  <div className="proposed">
                    <div className="eyebrow">Proposed action</div>
                    <b>{e.proposed_action.label}</b>
                    <span className="mono" style={{ fontSize: 12 }}>{e.proposed_action.tool}({JSON.stringify(e.proposed_action.args)})</span>
                  </div>
                ) : (
                  <div className="proposed" style={{ background: 'var(--surface-2)' }}>
                    <div className="eyebrow">No action proposed</div>
                    <span>Reply to the merchant and close the case, or decline to follow up by phone.</span>
                  </div>
                )}
                {e.status === 'OPEN' ? (
                  <div style={{ display: 'grid', gap: 10 }}>
                    <div className="decide">
                      <label className="muted" htmlFor="specialist" style={{ fontSize: 12.5 }}>Specialist</label>
                      <input id="specialist" value={specialist} onChange={(ev) => setSpecialist(ev.target.value)} style={{ flex: '0 1 200px' }} />
                      <input id="note" value={note} onChange={(ev) => setNote(ev.target.value)} placeholder="Optional note to the merchant" aria-label="Note to the merchant" />
                    </div>
                    <div className="decide">
                      <button className="btn ok" disabled={busyId === e.id} onClick={() => onDecide(e.id, true, specialist, note)}>
                        <Icon.ok />{e.proposed_action ? 'Approve and run' : 'Mark resolved'}
                      </button>
                      <button className="btn" disabled={busyId === e.id} onClick={() => onDecide(e.id, false, specialist, note)}>
                        <Icon.x />Decline, follow up by phone
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="chip ok" style={{ whiteSpace: 'normal', padding: '8px 12px', borderRadius: 10 }}>
                    {e.status === 'APPROVED' ? 'Approved' : 'Declined'} by {e.decided_by} at {fmtTime(e.decided_at)}{e.note ? ` · “${e.note}”` : ''}. The merchant has been updated in their chat.
                  </div>
                )}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  )
}
