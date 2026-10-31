import React from 'react'
import { inr } from '../engine/core.js'

const AGENTS = [
  ['Triage', 'Reads the message in English, Hindi or Hinglish; picks the issue, pulls out IDs and amounts, scores its own confidence.', 'Fast model (Groq Llama · Gemini Flash · GPT mini)'],
  ['Context', 'Pulls the merchant’s live records, past tickets and the most relevant help-center docs before anyone replies.', 'Code: SQL + TF-IDF vector search'],
  ['Action', 'Plans and calls tools in a loop: check, recheck with the bank, retry, re-send, reconnect the Soundbox, raise a ticket.', 'Strongest tool-calling model (Claude Sonnet · GPT-4.1 · Gemini Pro)'],
  ['Council', 'Before any money moves on its own, three code auditors (ledger, replay, velocity) and one AI juror per provider vote. Auditors can veto; jurors decide by majority.', 'One juror per lab: Claude · GPT · Gemini · Llama (Groq) · OpenRouter · Ollama'],
  ['Escalation', 'When a human is needed, writes the briefing: what was said, what was checked, what to approve.', 'Careful summariser (Claude Haiku · GPT mini)'],
  ['Reply', 'Writes back in the merchant’s language, then a grounding check confirms every amount and ID matches the tool results.', 'Multilingual model (Gemini Flash) + templates'],
]

const STACK = [
  ['Agents & API', 'Python 3, FastAPI, async orchestrator, Server-Sent Events for the live trace'],
  ['Models', 'OpenAI, Anthropic Claude, Google Gemini, Groq (Llama), OpenRouter and local Ollama through one adapter layer, routed per agent; rules engine as fallback'],
  ['Tool calling', 'Provider-native function calling (OpenAI-compatible tools and Anthropic tool use), normalised to one format'],
  ['Knowledge (RAG)', 'Help-center index with TF-IDF vectors and cosine similarity; Hinglish and Hindi expanded to English terms'],
  ['Data', 'SQLite for the demo (merchants, payments, settlements, refunds, tickets, audit log); same SQL ports to PostgreSQL'],
  ['Web app', 'React 18 + Vite; hand-built SVG charts; Web Speech API for Soundbox-style voice replies'],
  ['Prototype', 'The same agent logic ported to JavaScript so the app runs fully in the browser; Claude can drive it live with page functions as tools'],
  ['Model Council', 'Parallel review with asyncio.gather; models that time out abstain; council verdicts written to the audit log'],
  ['Quality', 'pytest end-to-end tests for every demo scenario, guardrail, council vote and fallback path (18 tests)'],
]

export function HowItWorks({ info }) {
  const limit = info?.policy?.auto_money_limit ?? 5000
  return (
    <div style={{ display: 'grid', gap: 18 }}>
      <div className="page-head">
        <div>
          <h1>How PayMates works</h1>
          <p>A small team of specialised agents behind one merchant chat. Models do the understanding and planning; code does retrieval and enforcement; humans make the judgement calls.</p>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head"><h2>Architecture</h2></div>
        <div className="panel-body arch">
          <div className="arch-box"><b>Merchant channel</b><small>Chat in the merchant app (demo: this web app) · voice replies like a Soundbox · English, हिन्दी, Hinglish</small></div>
          <div className="arch-arrow">↓ every message becomes a ticket</div>
          <div className="arch-orch">
            <div className="eyebrow" style={{ color: 'inherit', opacity: 0.8, marginBottom: 8 }}>Agent orchestrator · streams each step live</div>
            <div className="arch-row cols-4">
              {AGENTS.filter((a) => !['Reply', 'Council'].includes(a[0])).map(([n, d]) => <div key={n} className="arch-box"><b>{n}</b><small>{d}</small></div>)}
            </div>
          </div>
          <div className="arch-arrow">↓ every tool call passes the guardrails first · every money move also goes to the council</div>
          <div className="arch-box council-arch"><b>Model Council: no single model can move money</b><small>Ledger, replay and velocity auditors (code, can veto) + one AI juror from each configured lab (majority vote). A hold goes to the human console with the council's reasons.</small></div>
          <div className="arch-row cols-3">
            <div className="arch-box"><b>Tools</b><small>Mock Paytm merchant APIs: payments, bank recheck, settlements, refunds, KYC, ops tickets, and Soundbox diagnostics plus the remote actions a connected device really allows: reconnect, re-announce missed payments, change language, test announcement</small></div>
            <div className="arch-box"><b>Knowledge</b><small>Help-center RAG with ranked sources shown in the trace</small></div>
            <div className="arch-box"><b>Memory</b><small>Merchant history, past tickets and the conversation so far</small></div>
          </div>
          <div className="arch-row cols-2">
            <div className="arch-box"><b>Guardrails + audit log</b><small>Scope, evidence, money limit ({inr(limit)}), retry rules, PII masking; every action logged with the rule that allowed it</small></div>
            <div className="arch-box"><b>Ops dashboard + human console</b><small>Resolution rate, time to resolve, value recovered, hours saved; approvals with full briefings</small></div>
          </div>
        </div>
      </section>

      <div className="grid-2" style={{ marginBottom: 0 }}>
        <section className="panel">
          <div className="panel-head"><h2>One job per agent, the right model for each job</h2></div>
          <div className="panel-body stack-list">
            {AGENTS.map(([n, d, m]) => <div key={n}><b>{n}</b><span>{d}<br /><span className="chip model" style={{ marginTop: 4 }}>{m}</span></span></div>)}
          </div>
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Guardrails</h2></div>
          <div className="panel-body stack-list">
            <div><b>Knows its limits</b><span>Below 60% triage confidence it escalates instead of guessing.</span></div>
            <div><b>Money needs a human</b><span>Refunds or credits above {inr(limit)} are held until a specialist approves them in the console.</span></div>
            <div><b>Evidence first</b><span>A failed payment is only credited after the bank confirms it. Settlements are only retried to the same verified account.</span></div>
            <div><b>Stays in scope</b><span>The agent can only touch records of the merchant it is serving.</span></div>
            <div><b>Grounded replies</b><span>A model's rewrite is sent only if every amount and ID matches the tool results; otherwise the verified template goes out.</span></div>
            <div><b>Private by default</b><span>Phone numbers, UPI IDs and account numbers are masked before logs or model calls.</span></div>
            <div><b>Honest about devices</b><span>Volume is a physical button, so PayMates never pretends to change it: it guides the merchant (press + three times) and remotely does only what a connected Soundbox allows. Hardware faults get a technician.</span></div>
            <div><b>A second opinion</b><span>Money moves need the Model Council to agree: independent code auditors plus AI jurors from different labs, so one model's mistake can't move money.</span></div>
            <div><b>Never stuck</b><span>If any model fails or times out, that step falls back to the rules engine and the ticket still closes.</span></div>
          </div>
        </section>
      </div>

      <section className="panel">
        <div className="panel-head"><h2>Tech stack</h2></div>
        <div className="panel-body stack-list">
          {STACK.map(([k, v]) => <div key={k}><b>{k}</b><span>{v}</span></div>)}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><h2>About the data</h2></div>
        <div className="panel-body prose">
          All merchants, payments, settlements and refunds here are simulated for the demo, and the help-center articles are sample policies written for this prototype, not official Paytm documents. The tools mirror the shape of real merchant-support APIs so that, in production, they would point at Paytm's internal services instead of the demo database.
        </div>
      </section>
    </div>
  )
}
