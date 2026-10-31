import React, { useCallback, useEffect, useRef, useState } from 'react'
import { localEngine, liveAvailable } from './engine/local.js'
import { remoteEngine, backendReachable } from './engine/remote.js'
import { BrandMark, Toast } from './components/ui.jsx'
import { MerchantPhone, AgentTrace, OutcomeSide } from './components/Desk.jsx'
import { Dashboard } from './components/Dashboard.jsx'
import { HumanConsole } from './components/Console.jsx'
import { HowItWorks } from './components/How.jsx'

const VIEWS = [
  ['desk', 'Live desk'],
  ['dashboard', 'Ops dashboard'],
  ['console', 'Human console'],
  ['how', 'How it works'],
]
const BUSY_LABEL = {
  watcher: 'Checking your account',
  triage: 'Understanding your message', context: 'Checking your account', action: 'Working on it',
  council: 'The council is voting', guardrails: 'Running safety checks', escalation: 'Briefing a specialist', responder: 'Writing a reply',
}

function viewFromHash() {
  const h = (typeof location !== 'undefined' && location.hash.replace('#', '')) || ''
  return VIEWS.some(([k]) => k === h) ? h : 'desk'
}

export default function App() {
  const [engine, setEngine] = useState(null)
  const [info, setInfo] = useState(null)
  const [live, setLive] = useState(false)
  const [canLive, setCanLive] = useState(false)
  const [view, setView] = useState(viewFromHash)
  const [merchants, setMerchants] = useState([])
  const [merchantId, setMerchantId] = useState('M1001')
  const [convos, setConvos] = useState({})
  const [trace, setTrace] = useState(null)
  const [busy, setBusy] = useState(false)
  const [busyLabel, setBusyLabel] = useState('')
  const [metrics, setMetrics] = useState(null)
  const [tickets, setTickets] = useState([])
  const [escalations, setEscalations] = useState([])
  const [audit, setAudit] = useState([])
  const [sim, setSim] = useState({ running: false, done: false, i: 0, n: 0 })
  const [voice, setVoice] = useState(false)
  const [toast, setToast] = useState('')
  const [decidingId, setDecidingId] = useState(null)
  const [policy, setPolicy] = useState(null)
  const toastTimer = useRef(null)
  const engineRef = useRef(null)

  const say = useCallback((t) => {
    setToast(t)
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(''), 3800)
  }, [])

  // ---- pick the engine: Python backend when reachable, otherwise the in-browser engine
  useEffect(() => {
    (async () => {
      let e = localEngine
      let forceBrowser = false
      try { forceBrowser = new URLSearchParams(location.search).get('engine') === 'browser' } catch { /* ignore */ }
      if (!__ARTIFACT__ && !forceBrowser && (await backendReachable())) e = remoteEngine
      engineRef.current = e
      setEngine(e)
    })()
  }, [])

  const refresh = useCallback(async (e = engineRef.current) => {
    if (!e) return
    try {
      const [m, t, es, a] = await Promise.all([e.metrics(), e.tickets(), e.escalations(), e.audit()])
      setMetrics(m); setTickets(t); setEscalations(es); setAudit(a)
    } catch (err) { say(`Couldn't refresh: ${err.message}`) }
  }, [say])

  useEffect(() => {
    if (!engine) return
    ;(async () => {
      setInfo(await engine.info())
      setMerchants(await engine.merchants())
      try { setPolicy(await engine.policy()) } catch { /* older backend */ }
      refresh(engine)
      if (engine.kind === 'browser') setCanLive(await liveAvailable())
    })()
  }, [engine, refresh])

  useEffect(() => {
    const onHash = () => setView(viewFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const go = (v) => {
    setView(v)
    try { history.replaceState(null, '', `#${v}`) } catch { /* sandboxed */ }
    window.scrollTo({ top: 0 })
  }

  const speak = useCallback((text, lang) => {
    if (!voice || typeof speechSynthesis === 'undefined') return
    try {
      speechSynthesis.cancel()
      const u = new SpeechSynthesisUtterance(text)
      u.lang = lang === 'hi' ? 'hi-IN' : 'en-IN'
      const v = speechSynthesis.getVoices().find((x) => x.lang === u.lang)
      if (v) u.voice = v
      speechSynthesis.speak(u)
    } catch { /* voice is a nice-to-have */ }
  }, [voice])

  const addMsg = (mid, msg) => setConvos((c) => ({ ...c, [mid]: [...(c[mid] || []), msg] }))

  // One handler for chat and check-in streams. A check-in streams several tickets; each 'ticket' event starts a fresh trace.
  const traceHandler = (mid) => (ev) => {
    if (ev.type === 'ticket') {
      setTrace((t) => (ev.ticket.source === 'proactive'
        ? { merchantId: mid, message: ev.ticket.message, stages: {}, tools: [], reply: null, ticketId: ev.ticket.id, ticket: { ...ev.ticket, status: 'OPEN' } }
        : { ...t, ticketId: ev.ticket.id, ticket: { ...ev.ticket, status: 'OPEN' } }))
    } else if (ev.type === 'step') {
      setTrace((t) => ({ ...t, stages: { ...t.stages, [ev.agent]: ev } }))
      if (ev.status === 'running') setBusyLabel(BUSY_LABEL[ev.agent] || 'Working')
    } else if (ev.type === 'tool') setTrace((t) => ({ ...t, tools: [...t.tools, ev] }))
    else if (ev.type === 'reply') setTrace((t) => ({ ...t, reply: ev.text }))
    else if (ev.type === 'done') {
      setTrace((t) => ({ ...t, ticket: ev.ticket }))
      addMsg(mid, { role: 'bot', text: ev.ticket.reply, ticket: ev.ticket, proactive: ev.ticket.source === 'proactive' })
      speak(ev.ticket.reply, ev.ticket.lang)
    } else if (ev.type === 'checkin_done' && ev.message) {
      addMsg(mid, { role: 'bot', text: ev.message, proactive: true })
      speak(ev.message)
    }
  }

  const checkin = async (mid) => {
    const e = engineRef.current
    if (!e || busy) return
    if (view !== 'desk') go('desk')
    setBusy(true)
    setBusyLabel('PayMates is checking your account')
    setTrace({ merchantId: mid, message: 'PayMates check-in: scanning payments, settlements, refunds and Soundbox', stages: {}, tools: [], reply: null, ticket: null })
    try {
      const out = await e.checkin(mid, traceHandler(mid))
      const n = out?.found ?? 0
      say(n ? `Check-in found and handled ${n} issue${n === 1 ? '' : 's'}` : 'Check-in: all clear')
    } catch (err) {
      say(`Check-in failed: ${err.message}`)
    } finally {
      setBusy(false)
      refresh()
    }
  }

  const send = async (mid, text) => {
    const e = engineRef.current
    if (!e || busy) return
    setMerchantId(mid)
    if (view !== 'desk') go('desk')
    addMsg(mid, { role: 'merchant', text, ts: Date.now() })
    setBusy(true)
    setBusyLabel('PayMates is checking')
    setTrace({ merchantId: mid, message: text, stages: {}, tools: [], reply: null, ticket: null })
    const onEvent = traceHandler(mid)
    try {
      await e.chat(mid, text, onEvent)
    } catch (err) {
      if (e.kind === 'backend') {
        // Keep the demo alive: switch to the in-browser engine and retry once.
        engineRef.current = localEngine
        setEngine(localEngine)
        say('Backend unreachable, switched to the in-browser engine')
        setTrace({ merchantId: mid, message: text, stages: {}, tools: [], reply: null, ticket: null })
        try { await localEngine.chat(mid, text, onEvent) } catch (err2) { say(`Something went wrong: ${err2.message}`) }
      } else {
        say(`Something went wrong: ${err.message}`)
      }
    } finally {
      setBusy(false)
      refresh()
    }
  }

  const decide = async (id, approve, specialist, note) => {
    setDecidingId(id)
    try {
      const out = await engineRef.current.decide(id, approve, specialist || 'Specialist', note)
      addMsg(out.followup.merchant_id, { role: 'human', text: out.followup.text, from: (specialist || 'Specialist').split(' ')[0] })
      speak(out.followup.text, out.ticket.lang)
      say(approve ? `Approved. ${out.ticket.id} is closed and the merchant has been updated.` : 'Declined. The merchant has been told a specialist will call.')
      await refresh()
    } catch (err) {
      say(err.message)
    } finally { setDecidingId(null) }
  }

  const simulate = async (n) => {
    setSim({ running: true, done: false, i: 0, n })
    try {
      await engineRef.current.simulate(n, (ev) => {
        if (ev.type === 'sim_progress') { setSim({ running: true, done: false, i: ev.i, n: ev.n }); setMetrics(ev.metrics) }
      })
      say(`Simulated ${n} tickets`)
    } catch (err) { say(err.message) }
    setSim((s) => ({ ...s, running: false, done: true }))
    refresh()
  }

  const changePolicy = async (level) => {
    try {
      const p = await engineRef.current.setPolicy(level, 'Ops')
      setPolicy(p)
      const lv = p.levels.find((x) => x.id === level)
      say(`Autonomy set to ${lv.label}. ${lv.explain}`)
      refresh()
    } catch (err) { say(err.message) }
  }

  const reset = async () => {
    await engineRef.current.reset()
    try { setPolicy(await engineRef.current.policy()) } catch { /* ignore */ }
    setConvos({}); setTrace(null); setSim({ running: false, done: false, i: 0, n: 0 })
    setMerchants(await engineRef.current.merchants())
    refresh()
    say('Demo data restored')
  }

  const toggleLive = async () => {
    const next = !live
    localEngine.setLive(next)
    setLive(next)
    setInfo(await localEngine.info())
    say(next ? 'Claude now powers triage, actions and replies. Your first message asks for permission.' : 'Back to the built-in rules engine')
  }

  const openCount = escalations.filter((x) => x.status === 'OPEN').length
  const modelsLive = info?.routing ? Object.values(info.routing).filter((v) => !/rules|playbook|template/.test(v)).length : 0
  const engineText = !engine ? 'Connecting…'
    : engine.kind === 'backend' ? (modelsLive ? `Python backend · ${new Set(Object.values(info.routing)).size} model routes live` : 'Python backend · rules engine')
      : live ? 'Prototype · Claude live' : 'Prototype · in-browser engine'

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <a className="brand" href="#desk" onClick={(e) => { e.preventDefault(); go('desk') }}>
            <BrandMark />
            <span><span className="brand-name">PayMates</span><span className="brand-sub" style={{ display: 'block' }}>AI support teammate for merchants</span></span>
          </a>
          <nav className="nav" aria-label="Views">
            {VIEWS.map(([k, label]) => (
              <button key={k} aria-current={view === k ? 'page' : undefined} onClick={() => go(k)}>
                {label}{k === 'console' && openCount > 0 && <span className="badge">{openCount}</span>}
              </button>
            ))}
          </nav>
          <span className="spacer" />
          <span className="engine-pill" title={info?.routing ? Object.entries(info.routing).map(([a, m]) => `${a}: ${m}`).join('\n') : ''}>
            <span className={`dot ${engine?.kind === 'backend' || live ? '' : 'warn'}`} />
            {engineText}
            {policy && <span className={`autonomy-tag ${policy.autonomy_level}`} title="Autonomy level, set by ops in the Human console">Autonomy: {policy.levels.find((x) => x.id === policy.autonomy_level)?.label}</span>}
            {engine?.kind === 'browser' && canLive && (
              <button className="switch" role="switch" aria-checked={live} onClick={toggleLive} disabled={busy}>
                <span className="knob" />Claude
              </button>
            )}
          </span>
        </div>
      </header>

      <main className="page">
        {view === 'desk' && (
          <div className="desk">
            <MerchantPhone
              merchants={merchants} merchantId={merchantId} onPick={setMerchantId}
              messages={convos[merchantId] || []} busy={busy} busyLabel={busyLabel}
              onSend={(t) => send(merchantId, t)} onCheckin={() => checkin(merchantId)} voice={voice} onVoice={() => setVoice((v) => !v)}
              canMic={!__ARTIFACT__ && typeof window !== 'undefined' && Boolean(window.SpeechRecognition || window.webkitSpeechRecognition)}
            />
            <AgentTrace trace={trace} merchants={merchants} onTry={send} live={live} />
            <OutcomeSide trace={trace} metrics={metrics} openQueue={openCount} goDashboard={() => go('dashboard')} onConsole={() => go('console')} />
          </div>
        )}
        {view === 'dashboard' && (
          <Dashboard metrics={metrics} tickets={tickets} audit={audit} info={info} onSimulate={simulate} simState={sim} onReset={reset} />
        )}
        {view === 'console' && <HumanConsole escalations={escalations} onDecide={decide} busyId={decidingId} policy={policy} onPolicy={changePolicy} />}
        {view === 'how' && <HowItWorks info={info} />}
      </main>
      <Toast text={toast} />
    </>
  )
}
