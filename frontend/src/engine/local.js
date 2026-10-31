// In-browser PayMates engine: the same agent team as the Python backend, running locally.
// Used by the hosted prototype, and as an automatic fallback when the backend is unreachable.
// With "Claude live" switched on (hosted prototype only), triage, action and replies are
// produced by Claude through the page's sample capability, with page functions as tools.
import seed from '../../../shared/seed.json'
import {
  LEX, TPL, tokens, detectLanguage, keywordHits, extractEntities, inr, phrase, fill, render, ago, maskPii,
  HelpCenterIndex, LANG_LABEL,
} from './core.js'

const POLICY = seed.policy
const LIMIT = POLICY.auto_money_limit
const RETRYABLE = new Set(POLICY.retryable_hold_reasons)
const MONEY_TOOLS = new Set(['mark_payment_recovered', 'retry_settlement', 'reinitiate_refund'])
const now = () => Date.now()
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const clone = (x) => JSON.parse(JSON.stringify(x))
const index = new HelpCenterIndex(seed.kb)

// ====================================================================== store
function freshStore() {
  const t0 = now()
  const at = (m) => (m == null ? null : t0 + m * 60000)
  const st = {
    merchants: new Map(), txns: new Map(), settlements: new Map(), refunds: new Map(),
    tickets: [], ops: [], escalations: [], audit: [], messages: [],
    counters: { ticket: 2400, ops: 5100, esc: 300, utr: 880000, ref: 61000 },
  }
  seed.merchants.forEach((m) => st.merchants.set(m.id, clone(m)))
  seed.transactions.forEach((t) => st.txns.set(t.id, { ...t, created_at: at(t.at_min), bank_checked: false, recovered: false }))
  seed.settlements.forEach((s) => st.settlements.set(s.id, { ...s, expected_at: at(s.expected_min), settled_at: at(s.settled_min) }))
  seed.refunds.forEach((r) => st.refunds.set(r.id, { ...r, initiated_at: at(r.initiated_min), eta_at: at(r.eta_min) }))
  seed.ticket_history.forEach((h) =>
    st.tickets.push({ id: h.id, merchant_id: h.merchant_id, message: '', lang: 'en', intent: h.intent, confidence: 1, status: h.status,
      outcome: 'HISTORY', summary: h.summary, reply: '', created_at: at(h.at_min), resolved_at: at(h.at_min), duration_ms: null,
      recovered_amount: 0, models: {}, source: 'history' }))
  return st
}
let S = freshStore()
const nextId = (kind, prefix) => {
  S.counters[kind] = (S.counters[kind] || 10000) + 1
  return `${prefix}${S.counters[kind]}`
}
const audit = (ticket_id, actor, action, detail) => S.audit.unshift({ seq: S.audit.length + 1, ts: now(), ticket_id, actor, action, detail })
const byDesc = (key) => (a, b) => (b[key] || 0) - (a[key] || 0)

// ====================================================================== guardrails
function verdict(allowed, rule, reason, requires_approval = false) {
  return { allowed, requires_approval, rule, reason }
}
function check(name, args, mid, approvedBy) {
  for (const [key, table] of [['txn_id', S.txns], ['settlement_id', S.settlements], ['refund_id', S.refunds]]) {
    if (key in args) {
      const row = table.get(String(args[key]))
      if (!row) return verdict(false, 'record-exists', `${args[key]} does not exist`)
      if (row.merchant_id !== mid) return verdict(false, 'merchant-scope', `${args[key]} belongs to another merchant`)
    }
  }
  if (name === 'mark_payment_recovered') {
    const t = S.txns.get(String(args.txn_id))
    if (!t.bank_checked || t.bank_status !== 'SUCCESS') return verdict(false, 'evidence-required', 'Bank has not confirmed this payment; recheck status first')
    if (t.amount > LIMIT && !approvedBy) return verdict(false, 'money-limit', `${inr(t.amount)} is above the ${inr(LIMIT)} autonomous limit`, true)
    return verdict(true, 'money-limit', approvedBy ? `Approved by ${approvedBy}` : `${inr(t.amount)} is within the ${inr(LIMIT)} limit and bank-confirmed`)
  }
  if (name === 'retry_settlement') {
    const s = S.settlements.get(String(args.settlement_id))
    if (s.status !== 'ON_HOLD') return verdict(false, 'state-check', `Settlement is ${s.status}, nothing to retry`)
    if (!RETRYABLE.has(s.hold_reason)) return verdict(false, 'retryable-only', `Hold reason ${s.hold_reason} cannot be retried automatically`)
    const m = S.merchants.get(mid)
    if (!m.bank_verified || s.destination !== m.bank) return verdict(false, 'verified-destination', "Destination is not the merchant's verified bank account")
    return verdict(true, 'verified-destination', `Same amount to the same verified account (${s.destination}); retry is idempotent`)
  }
  if (name === 'reinitiate_refund') {
    const r = S.refunds.get(String(args.refund_id))
    if (r.status !== 'FAILED') return verdict(false, 'state-check', `Refund is ${r.status}, nothing to re-send`)
    if (r.amount > LIMIT && !approvedBy) return verdict(false, 'money-limit', `${inr(r.amount)} is above the ${inr(LIMIT)} autonomous limit`, true)
    return verdict(true, 'money-limit', approvedBy ? `Approved by ${approvedBy}` : `${inr(r.amount)} is within the ${inr(LIMIT)} limit`)
  }
  if (name === 'update_soundbox_settings') {
    if (args.volume != null && !(args.volume >= 0 && args.volume <= 100)) return verdict(false, 'value-range', 'Volume must be between 0 and 100')
    return verdict(true, 'device-settings', 'Device settings change, no money involved')
  }
  return verdict(true, 'read-or-low-risk', 'Read-only or low-risk action')
}

// ====================================================================== tools (mock Paytm APIs)
const txnView = (t) => ({ txn_id: t.id, amount: t.amount, amount_display: inr(t.amount), method: t.method, payer: maskPii(t.payer),
  gateway_status: t.gateway_status, customer_debited: t.bank_debited, recovered: t.recovered, minutes_ago: Math.round((now() - t.created_at) / 60000) })
const refundView = (r) => ({ refund_id: r.id, txn_id: r.txn_id, amount: r.amount, amount_display: inr(r.amount), status: r.status,
  failure_reason: r.reason, arn: r.arn, eta_minutes: r.eta_at ? Math.max(0, Math.round((r.eta_at - now()) / 60000)) : null })

const IMPL = {
  get_merchant_profile: (mid) => {
    const m = S.merchants.get(mid)
    return { merchant_id: m.id, name: m.name, owner: m.owner, city: m.city, plan: m.plan, settlement_cycle: m.settlement_cycle,
      kyc_status: m.kyc_status, bank: m.bank, preferred_language: m.lang }
  },
  list_transactions: (mid, a) => ({
    transactions: [...S.txns.values()].filter((t) => t.merchant_id === mid && (!a.status || t.gateway_status === String(a.status).toUpperCase()))
      .sort(byDesc('created_at')).slice(0, a.limit || 6).map(txnView),
  }),
  get_transaction: (mid, a) => { const t = S.txns.get(String(a.txn_id)); return t ? txnView(t) : { error: 'not found' } },
  recheck_payment_status: (mid, a) => {
    const t = S.txns.get(String(a.txn_id))
    t.bank_checked = true
    return { txn_id: t.id, bank_final_status: t.bank_status, customer_debited: t.bank_debited,
      note: t.bank_status === 'SUCCESS' && t.gateway_status === 'FAILED' ? 'Bank confirms the debit succeeded (late confirmation)'
        : t.bank_debited ? 'Bank confirms failure; auto-reversal to customer within T+1' : 'No debit happened' }
  },
  mark_payment_recovered: (mid, a) => {
    const t = S.txns.get(String(a.txn_id))
    if (t.recovered) return { txn_id: t.id, already_recovered: true }
    t.gateway_status = 'SUCCESS'; t.recovered = true
    return { txn_id: t.id, new_status: 'SUCCESS', credited_to_next_settlement: t.amount, amount_display: inr(t.amount), recovered: true }
  },
  list_settlements: (mid, a) => ({
    settlements: [...S.settlements.values()].filter((s) => s.merchant_id === mid).sort(byDesc('expected_at')).slice(0, a.limit || 5)
      .map((s) => ({ settlement_id: s.id, amount: s.amount, amount_display: inr(s.amount), status: s.status, hold_reason: s.hold_reason,
        utr: s.utr, destination: s.destination, overdue_minutes: s.status !== 'SETTLED' ? Math.max(0, Math.round((now() - s.expected_at) / 60000)) : 0 })),
  }),
  retry_settlement: (mid, a) => {
    const s = S.settlements.get(String(a.settlement_id))
    const code = [['SBI', 'SBIN'], ['HDFC', 'HDFC'], ['PNB', 'PUNB']].find(([k]) => s.destination.startsWith(k))?.[1] || 'UTRN'
    const utr = `${code}5261${nextId('utr', '')}04`
    Object.assign(s, { status: 'PROCESSING', hold_reason: null, utr, expected_at: now() + 2 * 3600000 })
    return { settlement_id: s.id, new_status: 'PROCESSING', new_utr: utr, eta_minutes: 120, amount_display: inr(s.amount) }
  },
  list_refunds: (mid, a) => ({
    refunds: [...S.refunds.values()].filter((r) => r.merchant_id === mid).sort(byDesc('initiated_at')).slice(0, a.limit || 5).map(refundView),
  }),
  get_refund: (mid, a) => { const r = S.refunds.get(String(a.refund_id)); return r ? refundView(r) : { error: 'not found' } },
  reinitiate_refund: (mid, a) => {
    const r = S.refunds.get(String(a.refund_id))
    const ref = nextId('ref', 'RF')
    Object.assign(r, { status: 'PROCESSING', reason: null, eta_at: now() + 24 * 3600000 })
    return { refund_id: r.id, new_status: 'PROCESSING', new_reference: ref, amount_display: inr(r.amount), destination: "customer's original payment source" }
  },
  get_kyc_status: (mid) => { const m = S.merchants.get(mid); return { kyc_status: m.kyc_status, missing_documents: m.kyc_missing } },
  run_soundbox_diagnostics: (mid) => {
    const sb = S.merchants.get(mid).soundbox
    const issues = []
    if (sb.fault) issues.push('hardware_fault')
    if (!sb.online) issues.push('offline')
    if (!sb.volume) issues.push('volume_zero')
    if (sb.signal === 'weak' && !sb.network_fallback) issues.push('weak_signal_no_fallback')
    return { device_id: sb.id, model: sb.model, online: sb.online, volume: sb.volume, signal: sb.signal, network_fallback: sb.network_fallback, issues }
  },
  update_soundbox_settings: (mid, a) => {
    const sb = S.merchants.get(mid).soundbox
    if (a.volume != null) sb.volume = Number(a.volume)
    if (a.network_fallback != null) {
      sb.network_fallback = Boolean(a.network_fallback)
      if (sb.network_fallback && sb.signal === 'weak') sb.signal = 'good (fallback network)'
    }
    return { device_id: sb.id, volume: sb.volume, network_fallback: sb.network_fallback, test_announcement: 'played', signal: sb.signal }
  },
  raise_ticket: (mid, a, ticketId) => {
    const ref = nextId('ops', 'OPS-')
    S.ops.push({ id: ref, merchant_id: mid, category: a.category || 'general', priority: a.priority || 'P3', summary: a.summary || '', created_at: now(), parent_ticket: ticketId })
    return { ticket_ref: ref, priority: a.priority || 'P3', queue: a.category || 'general' }
  },
  search_help_center: (mid, a) => ({ results: index.search(String(a.query || ''), 3).map(({ id, title, score, snippet }) => ({ id, title, score, snippet })) }),
}

const schema = (props = {}, required = []) => ({ type: 'object', properties: props, required })
export const TOOL_DEFS = {
  get_merchant_profile: ['read', "Get the merchant's profile: plan, KYC status, bank, settlement cycle.", schema()],
  list_transactions: ['read', "List the merchant's recent payments. Optional status filter: SUCCESS or FAILED.", schema({ status: { type: 'string', enum: ['SUCCESS', 'FAILED'] }, limit: { type: 'integer' } })],
  get_transaction: ['read', 'Get one payment by transaction ID.', schema({ txn_id: { type: 'string' } }, ['txn_id'])],
  recheck_payment_status: ['check', "Ask the customer's bank / UPI network for the final status of a payment that shows FAILED. Required before marking it recovered.", schema({ txn_id: { type: 'string' } }, ['txn_id'])],
  mark_payment_recovered: ['write', "Mark a bank-confirmed payment as successful and credit it to the merchant's next settlement. Moves money; guarded.", schema({ txn_id: { type: 'string' } }, ['txn_id'])],
  list_settlements: ['read', "List recent settlements (payouts to the merchant's bank) with status, hold reason and UTR.", schema({ limit: { type: 'integer' } })],
  retry_settlement: ['write', "Re-send a settlement that is ON_HOLD for a retryable reason (BANK_TIMEOUT) to the merchant's verified bank account. Moves money; guarded.", schema({ settlement_id: { type: 'string' } }, ['settlement_id'])],
  list_refunds: ['read', 'List refunds the merchant issued to customers, with status and failure reason.', schema({ limit: { type: 'integer' } })],
  get_refund: ['read', 'Get one refund by refund ID.', schema({ refund_id: { type: 'string' } }, ['refund_id'])],
  reinitiate_refund: ['write', "Re-send a FAILED refund to the customer's original payment source. Moves money; amounts above the limit need human approval.", schema({ refund_id: { type: 'string' } }, ['refund_id'])],
  get_kyc_status: ['read', 'Get KYC status and any missing documents.', schema()],
  run_soundbox_diagnostics: ['check', "Run remote diagnostics on the merchant's Soundbox: online, volume, signal, faults.", schema()],
  update_soundbox_settings: ['write', 'Change Soundbox volume (0-100) and/or turn network fallback on. Plays a test announcement.', schema({ volume: { type: 'integer' }, network_fallback: { type: 'boolean' } })],
  raise_ticket: ['write', 'Open an operations ticket for tracking or follow-up by a team (e.g. settlements, kyc, devices).', schema({ category: { type: 'string' }, priority: { type: 'string', enum: ['P1', 'P2', 'P3'] }, summary: { type: 'string' } }, ['category', 'summary'])],
  search_help_center: ['read', 'Search the merchant help center for policies and how-to answers.', schema({ query: { type: 'string' } }, ['query'])],
  escalate_to_human: ['handoff', 'Hand the case to a human specialist with a reason. Use when unsure, when the merchant asks for a person, or when a needed action was held for approval.', schema({ reason: { type: 'string' } }, ['reason'])],
}

// ====================================================================== Model Council (mirrors backend/app/council.py)
// Policy says a money move is allowed; the council decides it is safe. Code auditors can veto;
// model jurors (Claude, when live) vote by majority. No single model can move money.
const DAILY_CAP = POLICY.council_daily_cap || 20000
const DAILY_CAP_KYC_PENDING = POLICY.council_daily_cap_kyc_pending || 2000
const DAY_MS = 24 * 60 * 60 * 1000
const CAPPED = new Set(['mark_payment_recovered', 'reinitiate_refund']) // money leaving the company's books; settlement retries are the merchant's own funds
const recordKey = (a) => a.txn_id || a.settlement_id || a.refund_id
function amountOf(name, a) {
  const row = name === 'mark_payment_recovered' ? S.txns.get(String(a.txn_id))
    : name === 'retry_settlement' ? S.settlements.get(String(a.settlement_id))
      : name === 'reinitiate_refund' ? S.refunds.get(String(a.refund_id)) : null
  return row ? row.amount : 0
}
const seat = (seatName, who, kind, vote, reason, ms = 0) => ({ seat: seatName, who, kind, vote, reason, ms })
function auditLedger(name, a) {
  let ok, why
  if (name === 'mark_payment_recovered') {
    const t = S.txns.get(String(a.txn_id))
    ok = t.bank_checked && t.bank_status === 'SUCCESS' && t.bank_debited
    why = ok ? 'Bank says SUCCESS and the customer was debited' : 'Ledger does not show a confirmed customer debit'
  } else if (name === 'retry_settlement') {
    const st = S.settlements.get(String(a.settlement_id)); const m = S.merchants.get(st.merchant_id)
    ok = st.status === 'ON_HOLD' && st.destination === m.bank && m.bank_verified
    why = ok ? `Payout still on hold, same verified account ${st.destination}` : 'Payout state or destination changed'
  } else {
    const r = S.refunds.get(String(a.refund_id))
    ok = r.status === 'FAILED'
    why = ok ? `Refund bounced (${r.reason}), customer has not been paid` : `Refund is ${r.status}`
  }
  return seat('Ledger auditor', 'ledger', 'auditor', ok ? 'APPROVE' : 'HOLD', why)
}
function priorMoves(mid) {
  const mine = new Set(S.tickets.filter((t) => t.merchant_id === mid).map((t) => t.id))
  return S.audit.filter((x) => mine.has(x.ticket_id) && MONEY_TOOLS.has(x.action) && x.detail?.allowed)
}
function auditReplay(prior, name, a) {
  const key = recordKey(a)
  return prior.some((x) => x.action === name && recordKey(x.detail.args || {}) === key)
    ? seat('Replay auditor', 'replay', 'auditor', 'HOLD', `${key} was already actioned; a second payout would double-pay`)
    : seat('Replay auditor', 'replay', 'auditor', 'APPROVE', `Audit log replayed: ${key} has never been paid out`)
}
function auditVelocity(mid, prior, name, a) {
  // Risk-tiered daily cap: merchants with incomplete KYC get a much smaller autonomous allowance.
  const kycOk = S.merchants.get(mid).kyc_status === 'VERIFIED'
  const cap = kycOk ? DAILY_CAP : DAILY_CAP_KYC_PENDING
  const tier = kycOk ? 'daily cap' : 'cap while KYC is pending'
  if (!CAPPED.has(name)) return seat('Velocity auditor', 'velocity', 'auditor', 'APPROVE', "Merchant's own payout to their verified account; not new money, so no cap applies")
  const since = now() - DAY_MS
  const total = prior.filter((x) => x.ts >= since && CAPPED.has(x.action)).reduce((sum, x) => sum + amountOf(x.action, x.detail.args || {}), 0) + amountOf(name, a)
  return total > cap
    ? seat('Velocity auditor', 'velocity', 'auditor', 'HOLD', `${inr(total)} in 24h would pass the ${inr(cap)} ${tier}`)
    : seat('Velocity auditor', 'velocity', 'auditor', 'APPROVE', `${inr(total)} of the ${inr(cap)} ${tier} used after this`)
}
async function askClaudeJuror(name, a, guardReason) {
  const t0 = performance.now()
  const s = await getSample()
  try {
    const ev = name === 'mark_payment_recovered' ? S.txns.get(String(a.txn_id)) : name === 'retry_settlement' ? S.settlements.get(String(a.settlement_id)) : S.refunds.get(String(a.refund_id))
    const data = await s.json('You are one juror on a payments safety council. An autonomous support agent wants to move money. Decide independently whether the EVIDENCE fully supports the ACTION. ' +
      'Vote HOLD if anything is missing, inconsistent, or suspicious; otherwise APPROVE. Reply with only JSON: {"vote": "APPROVE" | "HOLD", "reason": "<= 18 words"}\n\n' +
      JSON.stringify({ action: name, args: a, amount_inr: amountOf(name, a), evidence: { ...ev, payer: undefined }, policy: { autonomous_limit_inr: LIMIT, guardrail_verdict: guardReason } }),
    { modelTier: 'quick' })
    const vote = String(data.vote || '').toUpperCase().startsWith('HOLD') ? 'HOLD' : 'APPROVE'
    return seat('Claude', 'claude · quick tier', 'model', vote, String(data.reason || 'No reason given').slice(0, 160), Math.round(performance.now() - t0))
  } catch (e) {
    return seat('Claude', 'claude · quick tier', 'model', 'ABSTAIN', `Abstained (${e?.code || 'error'})`, Math.round(performance.now() - t0))
  }
}
async function reviewCouncil(mid, name, a, guardReason) {
  const t0 = performance.now()
  const prior = priorMoves(mid)
  const votes = [auditLedger(name, a), auditReplay(prior, name, a), auditVelocity(mid, prior, name, a)]
  const jurors = live && (await getSample()) ? 1 : 0
  if (jurors) votes.push(await askClaudeJuror(name, a, guardReason))
  const veto = votes.find((v) => v.kind === 'auditor' && v.vote === 'HOLD')
  const cast = votes.filter((v) => v.kind === 'model' && v.vote !== 'ABSTAIN')
  const approvals = cast.filter((v) => v.vote === 'APPROVE').length
  const majority = !cast.length || approvals * 2 > cast.length
  const reason = veto ? veto.reason : !majority ? `Model jury voted to hold (${cast.length - approvals} of ${cast.length})`
    : `${votes.filter((v) => v.vote === 'APPROVE').length} of ${votes.length} seats approved`
  return { approved: !veto && majority, reason, votes, amount: amountOf(name, a), ms: Math.round(performance.now() - t0), jurors }
}

class ToolRunner {
  constructor(mid, ticketId, emit, actor = 'action-agent') {
    Object.assign(this, { mid, ticketId, emit, actor, calls: [], blocked: [], handoff: null, recovered: 0, sessions: [] })
  }
  async run(name, rawArgs = {}, approvedBy = null) {
    const args = Object.fromEntries(Object.entries(rawArgs || {}).filter(([, v]) => v !== null && v !== undefined))
    if (!TOOL_DEFS[name]) return { error: `unknown tool ${name}` }
    const kind = TOOL_DEFS[name][0]
    const t0 = performance.now()
    let result, guard, session = null
    if (name === 'escalate_to_human') {
      this.handoff = String(args.reason || 'Agent requested a human')
      result = { handed_off: true }
      guard = { allowed: true, rule: 'handoff', reason: 'Human handoff requested' }
    } else {
      guard = check(name, args, this.mid, approvedBy)
      if (guard.allowed && MONEY_TOOLS.has(name) && !approvedBy) {
        await this.emit({ type: 'step', agent: 'council', status: 'running', title: `Council reviewing ${name.replace(/_/g, ' ')}` })
        session = await reviewCouncil(this.mid, name, args, guard.reason)
        this.sessions.push({ tool: name, args, ...session })
        audit(this.ticketId, 'model-council', 'review', { tool: name, args, approved: session.approved, reason: session.reason,
          votes: session.votes.map((v) => `${v.who}:${v.vote}`) })
        if (!session.approved) guard = { allowed: false, requires_approval: true, rule: 'model-council', reason: `Council held it: ${session.reason}` }
      }
      if (!guard.allowed) {
        result = { blocked: true, requires_approval: guard.requires_approval, reason: guard.reason }
        if (guard.requires_approval) this.blocked.push({ tool: name, args, reason: guard.reason })
      } else {
        try { result = IMPL[name](this.mid, args, this.ticketId) } catch (e) { result = { error: String(e.message || e) } }
        if (name === 'mark_payment_recovered' && result.recovered) this.recovered += result.credited_to_next_settlement || 0
      }
    }
    const rec = { name, kind, args, result, guard, ms: Math.round(performance.now() - t0), money: MONEY_TOOLS.has(name), council: session }
    this.calls.push(rec)
    audit(this.ticketId, approvedBy || this.actor, name, { args, allowed: guard.allowed, rule: guard.rule, reason: guard.reason })
    await this.emit({ type: 'tool', ...rec })
    return result
  }
}

// ====================================================================== Claude (hosted prototype only)
let live = false
let samplePromise = null
function getSample() {
  if (!samplePromise) {
    samplePromise = (typeof window !== 'undefined' && window.claude && typeof window.claude.use === 'function')
      ? window.claude.use('sample').catch(() => null)
      : Promise.resolve(null)
  }
  return samplePromise
}
export async function liveAvailable() {
  const s = await getSample()
  if (!s) return false
  try { const lim = await s.limits(); return Boolean(lim) } catch { return true }
}
const CLAUDE = { triage: 'claude · quick tier', action: 'claude · default tier', responder: 'claude · quick tier', escalation: 'claude · quick tier' }
async function modelFor(agent) {
  if (!live) return null
  const s = await getSample()
  return s ? { sample: s, label: CLAUDE[agent] } : null
}

// ====================================================================== agents
const INTENTS = {
  FAILED_PAYMENT: "A customer's payment failed, is stuck, or money was deducted without success.",
  SETTLEMENT_DELAY: "The merchant's payout/settlement to their bank is late, missing or on hold.",
  REFUND_ISSUE: 'A refund the merchant sent to a customer has not arrived, failed, or needs status.',
  SOUNDBOX: 'The Soundbox device is silent, not announcing, or needs settings changed.',
  KYC: 'KYC status, verification or which documents are needed.',
  HUMAN_REQUEST: 'The merchant explicitly asks for a human, a call, or a manager.',
  GENERAL: 'A how-to or policy question answerable from the help center.',
  UNKNOWN: 'Unclear or out of scope.',
}
const MONEY = new Set(['FAILED_PAYMENT', 'SETTLEMENT_DELAY', 'REFUND_ISSUE'])

export function rulesTriage(text) {
  const weights = LEX.intent_weights || {}
  const hits = {}, scores = {}
  for (const [intent, kws] of Object.entries(LEX.intents)) {
    hits[intent] = keywordHits(text, kws)
    scores[intent] = hits[intent].length * (weights[intent] ?? 1)
  }
  const ranked = Object.entries(scores).sort((a, b) => b[1] - a[1])
  let [top, s1] = ranked[0]
  const s2 = ranked[1][1]
  const entities = extractEntities(text)
  let conf, why
  if (s1 === 0) {
    const d = index.search(text, 1)
    if (d.length && d[0].score >= 0.22) { top = 'GENERAL'; conf = 0.62; why = `No intent keywords; help-center match ${d[0].id} (${d[0].score})` }
    else { top = 'UNKNOWN'; conf = 0.3; why = 'No intent keywords and no strong help-center match' }
  } else {
    conf = Math.min(0.97, 0.5 + 0.17 * s1)
    if (s2 === s1) conf -= 0.25
    else if (s2 > 0) conf -= 0.08
    why = 'Matched: ' + hits[top].slice(0, 4).join(', ')
  }
  if (entities.refund_id) { top = 'REFUND_ISSUE'; conf = Math.max(conf, 0.9) }
  else if (entities.settlement_id) { top = 'SETTLEMENT_DELAY'; conf = Math.max(conf, 0.9) }
  else if (entities.txn_id && (top === 'UNKNOWN' || top === 'GENERAL')) { top = 'FAILED_PAYMENT'; conf = Math.max(conf, 0.75) }
  return {
    intent: top, confidence: Math.round(Math.max(0.05, conf) * 100) / 100, entities,
    urgency: MONEY.has(top) ? 'high' : ['SOUNDBOX', 'KYC', 'HUMAN_REQUEST'].includes(top) ? 'medium' : 'low',
    language: detectLanguage(text), reasoning: why,
  }
}

async function triage(text) {
  const rules = rulesTriage(text)
  const m = await modelFor('triage')
  if (!m) return [rules, 'rules-engine']
  try {
    const data = await m.sample.json(
      'You are the triage agent of PayMates, an AI support teammate for Indian payment merchants. Classify the merchant message ' +
      '(English, Hindi, or Hinglish). Reply with only a JSON object: {"intent": one of ' + JSON.stringify(Object.keys(INTENTS)) +
      ', "confidence": 0-1, "entities": {"txn_id"?, "settlement_id"?, "refund_id"?, "amount"? integer rupees}, "urgency": "high"|"medium"|"low", ' +
      '"reasoning": short phrase}.\nIntents:\n' + Object.entries(INTENTS).map(([k, v]) => `- ${k}: ${v}`).join('\n') +
      `\n\nMessage: ${maskPii(text)}`, { modelTier: 'quick' })
    const intent = String(data.intent || '').toUpperCase()
    if (!INTENTS[intent]) throw new Error('invalid intent')
    const ents = { ...rules.entities }
    for (const [k, v] of Object.entries(data.entities || {})) if (v !== null && v !== '' && v !== 0) ents[k] = v
    if (ents.amount != null) { const n = parseInt(String(ents.amount).replace(/[₹,]/g, ''), 10); if (Number.isNaN(n)) delete ents.amount; else ents.amount = n }
    return [{ intent, confidence: Math.round(Number(data.confidence ?? 0.7) * 100) / 100, entities: ents,
      urgency: data.urgency || rules.urgency, language: rules.language, reasoning: String(data.reasoning || '').slice(0, 160) }, m.label]
  } catch (e) {
    rules.reasoning += ` (Claude unavailable: ${e?.code || e?.message || 'error'})`
    return [rules, `rules-engine (fallback from ${m.label})`]
  }
}

const HINTS = { FAILED_PAYMENT: 'payment failed money deducted bank', SETTLEMENT_DELAY: 'settlement timeline hold bank', REFUND_ISSUE: 'refund failed timeline',
  SOUNDBOX: 'soundbox not announcing volume network', KYC: 'kyc documents', HUMAN_REQUEST: 'human specialist' }

function gather(mid, tri, text) {
  const m = S.merchants.get(mid)
  const records = {}, highlights = []
  const intent = tri.intent
  if (intent === 'FAILED_PAYMENT') {
    const failed = [...S.txns.values()].filter((t) => t.merchant_id === mid && t.gateway_status === 'FAILED').sort(byDesc('created_at')).slice(0, 5)
    records.failed_payments = failed.map((t) => ({ txn_id: t.id, amount: t.amount, debited: t.bank_debited, payer: maskPii(t.payer) }))
    highlights.push(`${failed.length} failed payment(s) in recent history`)
    failed.slice(0, 3).forEach((t) => highlights.push(`${t.id} · ${inr(t.amount)} · customer ${t.bank_debited ? 'debited' : 'not debited'}`))
  } else if (intent === 'SETTLEMENT_DELAY') {
    const ss = [...S.settlements.values()].filter((s) => s.merchant_id === mid).sort(byDesc('expected_at')).slice(0, 4)
    records.settlements = ss.map((s) => ({ id: s.id, amount: s.amount, status: s.status, hold: s.hold_reason }))
    ss.slice(0, 3).forEach((s) => highlights.push(`${s.id} · ${inr(s.amount)} · ${s.status}${s.hold_reason ? ` (${s.hold_reason})` : ''}`))
  } else if (intent === 'REFUND_ISSUE') {
    const rs = [...S.refunds.values()].filter((r) => r.merchant_id === mid).sort(byDesc('initiated_at')).slice(0, 4)
    records.refunds = rs.map((r) => ({ id: r.id, amount: r.amount, status: r.status }))
    rs.slice(0, 3).forEach((r) => highlights.push(`${r.id} · ${inr(r.amount)} · ${r.status}`))
  } else if (intent === 'SOUNDBOX') {
    records.soundbox = m.soundbox
    highlights.push(`${m.soundbox.id} (${m.soundbox.model}) · ${m.soundbox.online ? 'online' : 'offline'}`)
  } else if (intent === 'KYC') {
    records.kyc = { status: m.kyc_status, missing: m.kyc_missing }
    highlights.push(`KYC ${m.kyc_status}${m.kyc_missing.length ? ` · missing ${m.kyc_missing.length} doc(s)` : ''}`)
  }
  const past = pastTickets(mid, 3)
  if (past.length) highlights.push(`${past.length} past ticket(s); last: ${past[0].summary.slice(0, 70)}`)
  const kb = index.search(`${text} ${HINTS[intent] || ''}`, 3)
  return {
    merchant: { id: m.id, name: m.name, owner: m.owner, city: m.city, plan: m.plan, lang: m.lang, kyc: m.kyc_status, bank: m.bank },
    records, past_tickets: past, kb, highlights,
  }
}
const pastTickets = (mid, n) => S.tickets.filter((t) => t.merchant_id === mid && t.status !== 'OPEN' && t.summary).sort(byDesc('created_at')).slice(0, n)

const res = (status, outcome, facts, summary, proposed = null, reason = null) => ({
  status, facts, summary, proposed_action: proposed, escalation_reason: reason,
  // within the money limit, but the Model Council said wait
  outcome: outcome.endsWith('_NEEDS_APPROVAL') && String(reason || '').startsWith('Council held it') ? 'COUNCIL_HOLD' : outcome,
})
const reasonText = (code, lang) => (code && TPL.phrases[`reason_${code}`] ? phrase(`reason_${code}`, lang) : code || '')

async function pbFailedPayment(run, tri, base) {
  const ents = tri.entities
  let txn = null
  if (ents.txn_id) { const r = await run.run('get_transaction', { txn_id: ents.txn_id }); if (!r.blocked && !r.error) txn = r }
  if (!txn) {
    const r = await run.run('list_transactions', { status: 'FAILED' })
    let c = r.transactions || []
    if (ents.amount) { const f = c.filter((t) => t.amount === ents.amount); if (f.length) c = f }
    c.sort((a, b) => (a.customer_debited === b.customer_debited ? 0 : a.customer_debited ? -1 : 1) || (a.recovered - b.recovered) || (a.minutes_ago - b.minutes_ago))
    txn = c[0] || null
  }
  if (!txn) return res('NEEDS_INFO', 'PAYMENT_NOT_FOUND', base, 'No matching failed payment found; asked merchant for details')
  const facts = { ...base, txn_id: txn.txn_id, amount: inr(txn.amount) }
  if (txn.recovered) return res('RESOLVED_AI', 'PAYMENT_RECOVERED', facts, `${txn.txn_id} was already recovered`)
  if (!txn.customer_debited) return res('RESOLVED_AI', 'PAYMENT_NOT_DEBITED', facts, `${txn.txn_id} failed before debit; no money moved`)
  const chk = await run.run('recheck_payment_status', { txn_id: txn.txn_id })
  if (chk.bank_final_status === 'SUCCESS') {
    const r = await run.run('mark_payment_recovered', { txn_id: txn.txn_id })
    if (r.blocked && r.requires_approval) {
      return res('ESCALATE', 'MONEY_NEEDS_APPROVAL', facts, `Bank confirmed ${txn.txn_id} (${inr(txn.amount)}); crediting it needs approval`,
        { tool: 'mark_payment_recovered', args: { txn_id: txn.txn_id }, label: `Mark ${txn.txn_id} successful and credit ${inr(txn.amount)} to the merchant` }, r.reason)
    }
    return res('RESOLVED_AI', 'PAYMENT_RECOVERED', facts, `Bank confirmed late success; recovered ${inr(txn.amount)} for the merchant`)
  }
  return res('RESOLVED_AI', 'PAYMENT_AUTO_REVERSAL', facts, `Bank confirmed failure; ${inr(txn.amount)} auto-reverses to customer`)
}

async function pbSettlement(run, tri, base) {
  const lang = tri.language
  const r = await run.run('list_settlements', {})
  const ss = r.settlements || []
  const sid = tri.entities.settlement_id
  const target = (sid && ss.find((s) => s.settlement_id === sid)) || ss.find((s) => s.status === 'ON_HOLD') || ss.find((s) => s.status === 'PROCESSING') || ss[0]
  if (!target) return res('RESOLVED_AI', 'FAQ_ANSWER', { ...base, answer: seed.kb[0].short[lang] }, 'No settlements yet; explained timelines')
  const row = S.settlements.get(target.settlement_id)
  const facts = { ...base, settlement_id: row.id, amount: inr(row.amount), bank: row.destination, bank_name: row.destination.split(' ••')[0], utr: row.utr || '-' }
  if (row.status === 'SETTLED') return res('RESOLVED_AI', 'SETTLEMENT_ON_TIME', { ...facts, when: ago(row.settled_at, lang) }, `${row.id} already settled, UTR ${row.utr}`)
  if (row.status === 'PROCESSING') return res('RESOLVED_AI', 'SETTLEMENT_PROCESSING', facts, `${row.id} is processing, UTR ${row.utr}`)
  if (RETRYABLE.has(row.hold_reason)) {
    const x = await run.run('retry_settlement', { settlement_id: row.id })
    if (x.blocked) return res('ESCALATE', 'LOW_CONFIDENCE', facts, `Retry of ${row.id} was blocked: ${x.reason}`, null, x.reason)
    const t = await run.run('raise_ticket', { category: 'settlements', priority: 'P2', summary: `Track re-sent settlement ${row.id} (${inr(row.amount)}), UTR ${x.new_utr}` })
    return res('RESOLVED_AI', 'SETTLEMENT_RETRIED', { ...facts, utr: x.new_utr, ticket_ref: t.ticket_ref }, `Re-sent ${row.id} (${inr(row.amount)}) after bank timeout; new UTR ${x.new_utr}`)
  }
  if (row.hold_reason === 'KYC_PENDING') {
    const k = await run.run('get_kyc_status', {})
    const t = await run.run('raise_ticket', { category: 'kyc', priority: 'P2', summary: `Fast-track KYC review; settlement ${row.id} (${inr(row.amount)}) on hold` })
    return res('RESOLVED_AI', 'SETTLEMENT_KYC_HOLD', { ...facts, docs: (k.missing_documents || ['-']).join(', '), ticket_ref: t.ticket_ref }, `${row.id} held for KYC; guided merchant and opened ${t.ticket_ref}`)
  }
  return res('ESCALATE', 'LOW_CONFIDENCE', facts, `${row.id} on hold for ${row.hold_reason}; needs manual review`, null, `Unusual hold reason ${row.hold_reason}`)
}

async function pbRefund(run, tri, base) {
  const ents = tri.entities, lang = tri.language
  let rf = null
  if (ents.refund_id) { const r = await run.run('get_refund', { refund_id: ents.refund_id }); if (!r.blocked && !r.error) rf = r }
  if (!rf) {
    const r = await run.run('list_refunds', {})
    let c = r.refunds || []
    if (ents.amount) { const f = c.filter((x) => x.amount === ents.amount); if (f.length) c = f }
    const order = { FAILED: 0, PROCESSING: 1, PROCESSED: 2 }
    c = [...c].sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3))
    rf = c[0] || null
  }
  if (!rf) return res('NEEDS_INFO', 'REFUND_NOT_FOUND', base, 'No matching refund found; asked for details')
  const row = S.refunds.get(rf.refund_id)
  const facts = { ...base, refund_id: row.id, amount: inr(row.amount) }
  if (row.status === 'PROCESSED') return res('RESOLVED_AI', 'REFUND_DONE', { ...facts, arn: row.arn || '-' }, `${row.id} already processed, ARN ${row.arn}`)
  if (row.status === 'PROCESSING') return res('RESOLVED_AI', 'REFUND_IN_PROGRESS', { ...facts, eta: phrase('eta_day', lang) }, `${row.id} processing within SLA`)
  facts.reason = reasonText(row.reason, lang)
  const x = await run.run('reinitiate_refund', { refund_id: row.id })
  if (x.blocked && x.requires_approval) {
    return res('ESCALATE', 'REFUND_NEEDS_APPROVAL', facts, `${row.id} (${inr(row.amount)}) failed; re-send held for approval (above ${inr(LIMIT)})`,
      { tool: 'reinitiate_refund', args: { refund_id: row.id }, label: `Re-send refund ${row.id} of ${inr(row.amount)} to the customer's original payment source` }, x.reason)
  }
  if (x.blocked) return res('ESCALATE', 'LOW_CONFIDENCE', facts, `Re-send of ${row.id} blocked: ${x.reason}`, null, x.reason)
  return res('RESOLVED_AI', 'REFUND_REINITIATED', { ...facts, ref: x.new_reference }, `Re-sent ${row.id} (${inr(row.amount)}), ref ${x.new_reference}`)
}

async function pbSoundbox(run, tri, base, text) {
  const lang = tri.language
  const toks = new Set(tokens(text))
  if (toks.has('language') || toks.has('bhasha') || text.includes('भाषा')) {
    const r = await run.run('search_help_center', { query: 'soundbox announcement language' })
    const id = r.results?.[0]?.id || 'KB-106'
    return res('RESOLVED_AI', 'FAQ_ANSWER', { ...base, answer: seed.kb.find((d) => d.id === id).short[lang] }, 'Explained how to change announcement language')
  }
  const d = await run.run('run_soundbox_diagnostics', {})
  const facts = { ...base, device_id: d.device_id, volume: d.volume }
  const issues = d.issues || []
  if (issues.includes('hardware_fault') || issues.includes('offline')) {
    const t = await run.run('raise_ticket', { category: 'devices', priority: 'P2', summary: `Replace Soundbox ${d.device_id}: ${issues.join(', ')}` })
    return res('RESOLVED_AI', 'SOUNDBOX_REPLACEMENT', { ...facts, ticket_ref: t.ticket_ref }, `Booked replacement for ${d.device_id}`)
  }
  const fix = {}, it = [], ft = []
  if (issues.includes('volume_zero')) { fix.volume = 80; it.push(phrase('issue_volume', lang)); ft.push(phrase('fix_volume', lang)) }
  if (issues.includes('weak_signal_no_fallback')) { fix.network_fallback = true; it.push(phrase('issue_signal', lang)); ft.push(phrase('fix_fallback', lang)) }
  if (Object.keys(fix).length) {
    await run.run('update_soundbox_settings', fix)
    const j = phrase('and', lang)
    return res('RESOLVED_AI', 'SOUNDBOX_FIXED', { ...facts, issues: it.join(j), fixes: ft.join(j) }, `Fixed ${d.device_id} remotely: ${issues.join(', ')}`)
  }
  return res('RESOLVED_AI', 'SOUNDBOX_OK', facts, `${d.device_id} healthy; gave next steps`)
}

async function pbKyc(run, tri, base) {
  const k = await run.run('get_kyc_status', {})
  if (k.kyc_status !== 'VERIFIED') return res('RESOLVED_AI', 'KYC_GUIDANCE', { ...base, docs: (k.missing_documents || ['-']).join(', ') }, `KYC ${k.kyc_status}; listed missing documents`)
  return res('RESOLVED_AI', 'KYC_VERIFIED', { ...base, answer: seed.kb.find((d) => d.id === 'KB-104').short[tri.language] }, 'KYC verified; shared requirements')
}

async function pbGeneral(run, tri, base, text) {
  const r = await run.run('search_help_center', { query: text })
  const h = r.results || []
  if (h.length && h[0].score >= 0.15) {
    const doc = seed.kb.find((d) => d.id === h[0].id)
    return res('RESOLVED_AI', 'FAQ_ANSWER', { ...base, answer: doc.short[tri.language], source: doc.id }, `Answered from help center ${doc.id}`)
  }
  return res('ESCALATE', 'LOW_CONFIDENCE', base, 'No confident answer in help center', null, 'No confident help-center answer')
}

function runPlaybook(run, tri, base, text) {
  switch (tri.intent) {
    case 'FAILED_PAYMENT': return pbFailedPayment(run, tri, base)
    case 'SETTLEMENT_DELAY': return pbSettlement(run, tri, base)
    case 'REFUND_ISSUE': return pbRefund(run, tri, base)
    case 'SOUNDBOX': return pbSoundbox(run, tri, base, text)
    case 'KYC': return pbKyc(run, tri, base)
    default: return pbGeneral(run, tri, base, text)
  }
}

const PLAYBOOK_HINTS = {
  FAILED_PAYMENT: 'Find the failed payment (by ID, else amount, else most recent where the customer was debited). If debited, call recheck_payment_status. If the bank says SUCCESS, call mark_payment_recovered. If FAILED, explain auto-reversal.',
  SETTLEMENT_DELAY: 'list_settlements. If ON_HOLD with BANK_TIMEOUT, call retry_settlement then raise_ticket (category settlements, P2). If KYC_PENDING, call get_kyc_status and raise_ticket (category kyc). If SETTLED, give the UTR.',
  REFUND_ISSUE: 'Find the refund. PROCESSED -> give ARN. PROCESSING -> give ETA. FAILED -> call reinitiate_refund (held for approval if above limit).',
  SOUNDBOX: 'run_soundbox_diagnostics. Fix volume_zero (set 80) and weak_signal_no_fallback (network_fallback true) with update_soundbox_settings. Hardware fault or offline -> raise_ticket (devices).',
  KYC: 'get_kyc_status and list any missing documents.',
  GENERAL: 'search_help_center and answer from the best result.',
  UNKNOWN: 'Investigate briefly; if still unclear, escalate_to_human.',
}

function inferBlocked(b, base, lang, summary) {
  if (b.tool === 'reinitiate_refund') {
    const r = S.refunds.get(b.args.refund_id)
    return res('ESCALATE', 'REFUND_NEEDS_APPROVAL', { ...base, refund_id: r.id, amount: inr(r.amount), reason: reasonText(r.reason, lang) }, summary,
      { tool: b.tool, args: b.args, label: `Re-send refund ${r.id} of ${inr(r.amount)} to the customer's original payment source` }, b.reason)
  }
  const amount = b.tool === 'mark_payment_recovered' ? S.txns.get(b.args.txn_id)?.amount : S.settlements.get(b.args.settlement_id)?.amount
  return res('ESCALATE', 'MONEY_NEEDS_APPROVAL', { ...base, amount: inr(amount) }, summary, { tool: b.tool, args: b.args, label: `Run ${b.tool} ${JSON.stringify(b.args)}` }, b.reason)
}

function inferOutcome(calls, base, summary) {
  const ok = calls.filter((c) => c.guard.allowed && !c.result.error)
  const by = Object.fromEntries(ok.map((c) => [c.name, c]))
  if (by.mark_payment_recovered) { const t = S.txns.get(by.mark_payment_recovered.args.txn_id); return res('RESOLVED_AI', 'PAYMENT_RECOVERED', { ...base, txn_id: t.id, amount: inr(t.amount) }, summary) }
  if (by.retry_settlement) {
    const s = S.settlements.get(by.retry_settlement.args.settlement_id)
    return res('RESOLVED_AI', 'SETTLEMENT_RETRIED', { ...base, settlement_id: s.id, amount: inr(s.amount), bank: s.destination, bank_name: s.destination.split(' ••')[0], utr: s.utr, ticket_ref: by.raise_ticket?.result?.ticket_ref || '-' }, summary)
  }
  if (by.reinitiate_refund) { const r = S.refunds.get(by.reinitiate_refund.args.refund_id); return res('RESOLVED_AI', 'REFUND_REINITIATED', { ...base, refund_id: r.id, amount: inr(r.amount), reason: '-', ref: by.reinitiate_refund.result.new_reference }, summary) }
  if (by.update_soundbox_settings) return res('RESOLVED_AI', 'GENERIC_RESOLVED', { ...base, summary }, summary)
  if (by.recheck_payment_status) {
    const c = by.recheck_payment_status.result
    if (c.bank_final_status === 'FAILED' && c.customer_debited) { const t = S.txns.get(c.txn_id); return res('RESOLVED_AI', 'PAYMENT_AUTO_REVERSAL', { ...base, txn_id: t.id, amount: inr(t.amount) }, summary) }
  }
  return null
}

async function act(run, tri, ctx, text, base) {
  const m = await modelFor('action')
  if (!m) return [await runPlaybook(run, tri, base, text), 'playbook-engine']
  try {
    const brief = { merchant: ctx.merchant, message: maskPii(text), triage: { intent: tri.intent, confidence: tri.confidence, entities: tri.entities },
      relevant_records: ctx.records, help_center: ctx.kb.map(({ id, title, snippet }) => ({ id, title, snippet })),
      suggested_playbook: PLAYBOOK_HINTS[tri.intent] || PLAYBOOK_HINTS.UNKNOWN }
    const tools = Object.entries(TOOL_DEFS).map(([name, [, description, inputSchema]]) => ({
      name, description, inputSchema, execute: async (input) => run.run(name, input || {}),
    }))
    const prompt = 'You are the action agent of PayMates, an autonomous customer-service teammate for Paytm-style merchants in India. ' +
      'Resolve the issue by calling the tools. Rules: verify before acting; money movements above Rs ' + LIMIT + ' are held for human approval (expected, not an error); ' +
      'never invent IDs or amounts; if a tool returns blocked, do not work around it; if unclear after checking, call escalate_to_human; use 2-4 tool calls. ' +
      'When finished reply with ONLY a JSON object {"outcome": "resolved"|"escalate"|"needs_info", "summary": one sentence of what you found and did}.\n\nCASE:\n' +
      JSON.stringify(brief)
    const out = await m.sample(prompt, { tools, modelTier: 'default', effort: 'low' })
    let final = {}
    try { const t = out.text; final = JSON.parse(t.slice(t.lastIndexOf('{'), t.lastIndexOf('}') + 1)) } catch { final = { outcome: 'resolved', summary: out.text.slice(-240) } }
    const summary = String(final.summary || 'Handled by the action agent')
    if (run.blocked.length) return [inferBlocked(run.blocked[0], base, tri.language, summary), m.label]
    if (run.handoff) return [res('ESCALATE', 'LOW_CONFIDENCE', base, summary, null, run.handoff), m.label]
    const inf = inferOutcome(run.calls, base, summary)
    if (inf) return [inf, m.label]
    if (final.outcome === 'escalate') return [res('ESCALATE', 'LOW_CONFIDENCE', base, summary, null, summary), m.label]
    if (final.outcome === 'needs_info') return [res('NEEDS_INFO', 'GENERIC_RESOLVED', { ...base, summary }, summary), m.label]
    return [res('RESOLVED_AI', 'GENERIC_RESOLVED', { ...base, summary }, summary), m.label]
  } catch (e) {
    const r = await runPlaybook(run, tri, base, text)
    r.fallback_note = `Claude unavailable (${e?.code || e?.message || 'error'}), finished with playbook`
    return [r, `playbook (fallback from ${m.label})`]
  }
}

export function describeCall(c) {
  const { name: n, args: a, result: r, guard: g } = c
  if (r.blocked) return `Held: ${n} ${JSON.stringify(a)} — ${g.reason}`
  if (n === 'get_transaction' && r.txn_id) return `Payment ${r.txn_id} · ${r.amount_display} · gateway ${r.gateway_status} · customer ${r.customer_debited ? 'debited' : 'not debited'}`
  if (n === 'list_transactions') return `Looked up ${(r.transactions || []).length} recent payment(s)`
  if (n === 'recheck_payment_status') return `Bank recheck of ${r.txn_id}: ${r.bank_final_status}`
  if (n === 'list_settlements') return 'Settlements: ' + (r.settlements || []).slice(0, 3).map((s) => `${s.settlement_id} ${s.status}`).join('; ')
  if (n === 'list_refunds') return 'Refunds: ' + (r.refunds || []).slice(0, 3).map((x) => `${x.refund_id} ${x.amount_display} ${x.status}`).join('; ')
  if (n === 'get_refund') return `Refund ${r.refund_id} · ${r.amount_display} · ${r.status}`
  if (n === 'run_soundbox_diagnostics') return `Soundbox ${r.device_id} diagnostics: ${(r.issues && r.issues.length ? r.issues : ['no issues']).join(', ')}`
  if (n === 'get_kyc_status') return `KYC ${r.kyc_status}${r.missing_documents?.length ? `, missing ${r.missing_documents.join(', ')}` : ''}`
  if (n === 'search_help_center') return `Help center: ${r.results?.[0] ? `${r.results[0].id} ${r.results[0].title}` : 'no match'}`
  if (n === 'escalate_to_human') return `Agent asked for a human: ${a.reason}`
  return `${n}: done`
}

async function buildBriefing(merchant, text, tri, act, calls) {
  const p = act.proposed_action
  const risk = []
  if (p) {
    const amt = p.tool === 'reinitiate_refund' ? S.refunds.get(p.args.refund_id)?.amount : p.tool === 'mark_payment_recovered' ? S.txns.get(p.args.txn_id)?.amount : null
    if (amt > LIMIT) risk.push(`Moves ${inr(amt)}, above the ${inr(LIMIT)} autonomous limit`)
  }
  if (String(act.escalation_reason || '').startsWith('Council held it')) risk.push(act.escalation_reason)
  if (tri.confidence < POLICY.confidence_threshold) risk.push(`Low triage confidence (${Math.round(tri.confidence * 100)}%)`)
  if (tri.intent === 'HUMAN_REQUEST') risk.push('Merchant asked for a person')
  if (pastTickets(merchant.id, 5).filter((t) => t.intent === tri.intent).length >= 2) risk.push('Repeat issue for this merchant')
  const headline = {
    REFUND_NEEDS_APPROVAL: `Re-send failed refund ${act.facts.refund_id} (${act.facts.amount}) — needs approval`,
    MONEY_NEEDS_APPROVAL: `Money movement of ${act.facts.amount} needs approval`,
    COUNCIL_HOLD: `Model Council held a ${act.facts.amount} money movement`,
    HUMAN_HANDOFF: 'Merchant asked to speak to a person',
  }[act.outcome] || "Case needs a specialist's judgement"
  const briefing = {
    headline, said: text, intent: tri.intent, confidence: tri.confidence, checks: calls.map(describeCall), proposed: p, risk,
    reason: act.escalation_reason || act.summary,
    merchant: { id: merchant.id, name: merchant.name, owner: merchant.owner, city: merchant.city, plan: merchant.plan, language: tri.language },
    summary: `${merchant.owner} (${merchant.name}, ${merchant.city}) wrote about ${tri.intent.replace(/_/g, ' ').toLowerCase()}. ${act.summary}.`,
  }
  const m = await modelFor('escalation')
  if (!m) return [briefing, 'template']
  try {
    const out = await m.sample('Write a handover note for a human payment-support specialist. Two sentences, plain English, facts only from this input, ' +
      'end with the decision needed. Reply with only the note.\n\n' + JSON.stringify({ merchant: briefing.merchant, said: text, intent: tri.intent, checks: briefing.checks, proposed: p, risk, reason: briefing.reason }),
    { modelTier: 'quick', cache: false })
    if (out.text.trim()) briefing.summary = out.text.trim()
    return [briefing, m.label]
  } catch { return [briefing, `template (fallback from ${m.label})`] }
}

const CRITICAL = /₹[\d,]+|\b[A-Z]{1,4}-?\d{4,}\b|\b[A-Z]{4}\d{8,}\b/g
const LANG_NAMES = { en: 'English', hi: 'Hindi in Devanagari script', hinglish: 'Hinglish (Hindi written in Latin script, casual and warm)' }
async function respond(outcome, facts, lang, text) {
  const draft = render(outcome, lang, facts)
  const m = await modelFor('responder')
  if (!m || outcome === 'FAQ_ANSWER') return [draft, 'templates', true]
  try {
    const out = await m.sample(`You are PayMates, a friendly support teammate for small Indian merchants. Rewrite the DRAFT reply so it sounds natural and warm in ${LANG_NAMES[lang]}. ` +
      'Keep it under 70 words. Keep every amount, ID and reference EXACTLY as written. Do not add facts, promises or links. Reply with only the message text.\n\n' +
      `Merchant wrote: ${text}\n\nDRAFT: ${draft}`, { modelTier: 'quick', cache: false })
    const cand = out.text.trim().replace(/^"|"$/g, '')
    const needed = [...new Set(draft.match(CRITICAL) || [])]
    if (cand && needed.every((t) => cand.includes(t))) return [cand, m.label, true]
    return [draft, `templates (Claude reply failed grounding check)`, false]
  } catch { return [draft, `templates (fallback from ${m.label})`, true] }
}

// ====================================================================== orchestrator
export async function handleMessage(mid, text, emit = async () => {}, { source = 'live', pacing = 450 } = {}) {
  const merchant = S.merchants.get(mid)
  let paced = 0, lastPace = 0
  const pace = async (f = 1) => { lastPace = 0; if (pacing > 0) { paced += pacing * f; lastPace = pacing * f; await sleep(pacing * f) } }
  const started = performance.now()
  const ticketId = nextId('ticket', 'PM-')
  const created = now()
  const ticket = { id: ticketId, merchant_id: mid, message: text, lang: 'en', intent: null, confidence: null, status: 'OPEN', outcome: null,
    summary: null, reply: null, created_at: created, resolved_at: null, duration_ms: null, recovered_amount: 0, models: {}, source }
  S.tickets.push(ticket)
  S.messages.push({ merchant_id: mid, role: 'merchant', text, ts: created, ticket_id: ticketId })
  audit(ticketId, 'merchant', 'message', { text: maskPii(text) })
  await emit({ type: 'ticket', ticket: { id: ticketId, merchant_id: mid, created_at: created, message: text } })
  const models = {}
  const step = (agent, status, title, extra = {}) => emit({ type: 'step', agent, status, title, ...extra })
  const ms = (t) => Math.max(0, Math.round(performance.now() - t - lastPace))

  await step('triage', 'running', 'Reading the message')
  let t0 = performance.now()
  const [tri, triModel] = await triage(text)
  models.triage = triModel
  await pace()
  await step('triage', 'done', `${tri.intent.replace(/_/g, ' ').toLowerCase().replace(/^\w/, (c) => c.toUpperCase())} · ${Math.round(tri.confidence * 100)}% sure`, { model: models.triage, ms: ms(t0), data: tri })

  const base = { owner_first: merchant.owner.split(' ')[0], ticket_id: ticketId }
  const run = new ToolRunner(mid, ticketId, emit)

  await step('context', 'running', 'Pulling account history and help-center docs')
  t0 = performance.now()
  const ctx = gather(mid, tri, text)
  models.context = 'retrieval: records + TF-IDF vectors'
  await pace()
  await step('context', 'done', `${ctx.highlights.length} facts · ${ctx.kb.length} help docs`, { model: models.context, ms: ms(t0),
    data: { highlights: ctx.highlights, kb: ctx.kb.map(({ id, title, score }) => ({ id, title, score })), merchant: ctx.merchant } })

  const threshold = POLICY.confidence_threshold
  let a
  if (tri.intent === 'HUMAN_REQUEST') {
    a = res('ESCALATE', 'HUMAN_HANDOFF', base, 'Merchant asked for a person', null, 'Merchant asked for a person')
    models.action = 'policy: honour human request'
    await step('action', 'done', "Merchant asked for a person, so I'm handing over", { model: models.action, ms: 0, data: { decision: 'handoff' } })
  } else if (tri.confidence < threshold || tri.intent === 'UNKNOWN') {
    a = res('ESCALATE', 'LOW_CONFIDENCE', base, `Triage confidence ${Math.round(tri.confidence * 100)}% is below the ${Math.round(threshold * 100)}% threshold`, null,
      `Not confident enough to act (${Math.round(tri.confidence * 100)}%)`)
    models.action = 'policy: confidence threshold'
    await step('action', 'blocked', "Not sure enough to act, so I'm escalating instead of guessing", { model: models.action, ms: 0, data: { decision: 'escalate', threshold } })
  } else {
    await step('action', 'running', 'Deciding and acting')
    t0 = performance.now()
    const [ar, label] = await act(run, tri, ctx, text, base)
    a = ar; models.action = label
    await pace()
    const writes = run.calls.filter((c) => c.kind === 'write' && c.guard.allowed).length
    await step('action', a.status === 'ESCALATE' ? 'blocked' : 'done',
      `${run.calls.length} tool calls · ${writes} action(s) taken${run.blocked.length ? ` · ${run.blocked.length} held for approval` : ''}`,
      { model: models.action, ms: ms(t0), data: { summary: a.summary, outcome: a.outcome, fallback: a.fallback_note } })
  }

  if (run.sessions.length) {
    const last = run.sessions[run.sessions.length - 1]
    const held = run.sessions.filter((x) => !x.approved)
    await step('council', held.length ? 'blocked' : 'done',
      `${run.sessions.length} money move(s) reviewed · ${last.votes.length} seats · ${held.length ? 'held for a human' : 'approved'}`,
      { model: `3 auditors + ${last.jurors} model juror${last.jurors === 1 ? '' : 's'}`, ms: run.sessions.reduce((t, x) => t + x.ms, 0), data: { sessions: clone(run.sessions) } })
  } else {
    await step('council', 'skipped', 'No autonomous money move to review', { data: { sessions: [] } })
  }

  const allowed = run.calls.filter((c) => c.guard.allowed).length
  await step('guardrails', run.blocked.length ? 'blocked' : 'done', `${run.calls.length} call(s) checked · ${allowed} allowed · ${run.blocked.length} held`,
    { model: 'policy engine', ms: 0, data: { rules: [...new Set(run.calls.map((c) => c.guard.rule))].sort(), limit: LIMIT, held: run.blocked } })

  let escalation = null
  if (a.status === 'ESCALATE') {
    await step('escalation', 'running', 'Writing a briefing for a human specialist')
    t0 = performance.now()
    const [briefing, bl] = await buildBriefing(merchant, text, tri, a, run.calls)
    models.escalation = bl
    escalation = { id: nextId('esc', 'ESC-'), ticket_id: ticketId, merchant_id: mid, reason: a.escalation_reason || '', briefing,
      proposed_action: a.proposed_action, status: 'OPEN', created_at: now(), decided_at: null, decided_by: null, note: null }
    S.escalations.unshift(escalation)
    audit(ticketId, 'escalation-agent', 'escalate', { escalation: escalation.id, reason: escalation.reason })
    await pace()
    await step('escalation', 'done', `Handed to a specialist as ${escalation.id}`, { model: models.escalation, ms: ms(t0), data: { escalation: clone(escalation) } })
  }

  await step('responder', 'running', 'Writing the reply')
  t0 = performance.now()
  const [reply, rl, grounded] = await respond(a.outcome, a.facts, tri.language, text)
  models.responder = rl
  await pace(0.6)
  await step('responder', 'done', `Reply in ${LANG_LABEL[tri.language]}`, { model: models.responder, ms: ms(t0), data: { grounded } })
  S.messages.push({ merchant_id: mid, role: 'assistant', text: reply, ts: now(), ticket_id: ticketId })
  await emit({ type: 'reply', text: reply, lang: tri.language, ticket_id: ticketId })

  const status = { RESOLVED_AI: 'RESOLVED_AI', ESCALATE: 'ESCALATED', NEEDS_INFO: 'NEEDS_INFO' }[a.status]
  Object.assign(ticket, { lang: tri.language, intent: tri.intent, confidence: tri.confidence, status, outcome: a.outcome, summary: a.summary, reply,
    duration_ms: Math.max(0, Math.round(performance.now() - started - paced)), resolved_at: status === 'RESOLVED_AI' ? now() : null,
    recovered_amount: run.recovered, models })
  audit(ticketId, 'orchestrator', 'close', { status, outcome: a.outcome, duration_ms: ticket.duration_ms })
  await emit({ type: 'done', ticket: clone(ticket), escalation: escalation && clone(escalation) })
  return clone(ticket)
}

// ====================================================================== ops: metrics, human console, simulator
export function metrics() {
  const ts = S.tickets.filter((t) => t.source !== 'history').sort((a, b) => a.created_at - b.created_at)
  const count = (s) => ts.filter((t) => t.status === s).length
  const ai = count('RESOLVED_AI')
  const durs = ts.filter((t) => t.status === 'RESOLVED_AI' && t.duration_ms != null).map((t) => t.duration_ms).sort((a, b) => a - b)
  const median = durs.length ? (durs.length % 2 ? durs[(durs.length - 1) / 2] : Math.round((durs[durs.length / 2 - 1] + durs[durs.length / 2]) / 2)) : null
  const by = {}
  ts.forEach((t) => { const k = t.intent || 'UNKNOWN'; by[k] = by[k] || { total: 0, ai: 0 }; by[k].total++; if (t.status === 'RESOLVED_AI') by[k].ai++ })
  const manual = POLICY.manual_minutes_per_ticket
  return {
    total: ts.length, resolved_ai: ai, escalated_open: count('ESCALATED'), resolved_human: count('RESOLVED_HUMAN'), human_followup: count('HUMAN_FOLLOWUP'),
    needs_info: count('NEEDS_INFO'), ai_resolution_rate: ts.length ? Math.round((ai / ts.length) * 1000) / 1000 : 0, median_ai_ms: median,
    manual_minutes_baseline: manual, recovered_value: ts.reduce((s, t) => s + (t.recovered_amount || 0), 0), hours_saved: Math.round((ai * manual / 60) * 10) / 10,
    by_intent: by, series: ts.map((t, i) => ({ i: i + 1, status: t.status, intent: t.intent, source: t.source })), auto_money_limit: LIMIT,
  }
}

export async function decide(escId, approve, specialist, note = '') {
  const esc = S.escalations.find((e) => e.id === escId)
  if (!esc) throw new Error('Escalation not found')
  if (esc.status !== 'OPEN') throw new Error(`${escId} is already ${esc.status}`)
  const ticket = S.tickets.find((t) => t.id === esc.ticket_id)
  const lang = ticket.lang || 'en'
  const p = esc.proposed_action
  let text
  if (approve) {
    let ref = escId, amount = null, recovered = 0
    if (p) {
      const run = new ToolRunner(esc.merchant_id, ticket.id, async () => {}, `human:${specialist}`)
      const r = await run.run(p.tool, p.args, specialist)
      if (r.blocked || r.error) throw new Error(`Approved action could not run: ${r.reason || r.error}`)
      ref = r.new_reference || r.new_utr || r.txn_id || escId
      recovered = run.recovered
      amount = p.tool === 'reinitiate_refund' ? S.refunds.get(p.args.refund_id).amount
        : p.tool === 'mark_payment_recovered' ? S.txns.get(p.args.txn_id).amount : S.settlements.get(p.args.settlement_id)?.amount
    }
    const ad = TPL.action_done[p ? p.tool : 'none'] || TPL.action_done.none
    let done = fill(ad[lang] || ad.en, { amount: inr(amount) })
    if (note) done = `${done} ${note}`
    text = render('APPROVED_FOLLOWUP', lang, { specialist, action_done: done, ref, ticket_id: ticket.id })
    Object.assign(esc, { status: 'APPROVED', decided_at: now(), decided_by: specialist, note })
    Object.assign(ticket, { status: 'RESOLVED_HUMAN', resolved_at: now(), recovered_amount: (ticket.recovered_amount || 0) + recovered })
  } else {
    text = render('REJECTED_FOLLOWUP', lang, { specialist, note: note || '' }).replace(/ {2}/g, ' ')
    Object.assign(esc, { status: 'REJECTED', decided_at: now(), decided_by: specialist, note })
    ticket.status = 'HUMAN_FOLLOWUP'
  }
  audit(ticket.id, `human:${specialist}`, approve ? 'approve' : 'reject', { escalation: escId, note })
  S.messages.push({ merchant_id: esc.merchant_id, role: 'assistant', text, ts: now(), ticket_id: ticket.id })
  return { escalation: clone(esc), ticket: clone(ticket), followup: { merchant_id: esc.merchant_id, ticket_id: ticket.id, text, from: specialist }, metrics: metrics() }
}

const SIM_WEIGHTS = { FAILED_PAYMENT: 5, SETTLEMENT_DELAY: 4, REFUND_ISSUE: 4, SOUNDBOX: 3, KYC: 2, GENERAL: 3, HUMAN_REQUEST: 1, UNCLEAR: 1 }
const SIM_MERCHANTS = [
  ['M2001', 'Sharma Tea Stall', 'Pawan Sharma', 'Jaipur', 'hinglish', 'VERIFIED', 'SBI ••1182'],
  ['M2002', 'Lakshmi Textiles', 'Lakshmi Rao', 'Surat', 'en', 'VERIFIED', 'HDFC Bank ••6620'],
  ['M2003', 'Patel Hardware', 'Hitesh Patel', 'Ahmedabad', 'en', 'PENDING_DOCS', 'PNB ••3091'],
  ['M2004', 'Fresh Fruits Co', 'Sneha Kulkarni', 'Pune', 'en', 'VERIFIED', 'ICICI Bank ••7745'],
  ['M2005', 'Noor Bakery', 'Imran Noor', 'Hyderabad', 'hinglish', 'VERIFIED', 'SBI ••2208'],
  ['M2006', 'Singh Auto Parts', 'Gurpreet Singh', 'Ludhiana', 'hi', 'PENDING_DOCS', 'PNB ••5514'],
]
function rng(seedVal) {
  let s = seedVal >>> 0 || 1
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296)
}
export async function simulate(n, onEvent = () => {}, seedVal = Date.now()) {
  const r = rng(seedVal)
  const pick = (arr) => arr[Math.floor(r() * arr.length)]
  SIM_MERCHANTS.forEach(([id, name, owner, city, lang, kyc, bank], i) => {
    if (!S.merchants.has(id)) {
      S.merchants.set(id, { id, name, owner, city, category: 'Retail', lang, phone: '9000000000', plan: 'Soundbox 4G + QR', settlement_cycle: 'T+1',
        kyc_status: kyc, kyc_missing: kyc === 'VERIFIED' ? [] : ['GST certificate'], bank, bank_verified: true,
        soundbox: { id: `SB-5${i}210`, model: 'Soundbox 4G', online: true, volume: 70, signal: 'good', language: 'Hindi', network_fallback: true, fault: null } })
    }
  })
  const intents = Object.entries(SIM_WEIGHTS).flatMap(([k, w]) => Array(w).fill(k))
  for (let i = 0; i < n; i++) {
    const intent = pick(intents)
    const mid = pick(SIM_MERCHANTS)[0]
    const m = S.merchants.get(mid)
    const fields = {}
    const t = now()
    if (intent === 'FAILED_PAYMENT') {
      const amount = pick([180, 450, 799, 1250, 2300, 3999, 6200])
      const id = nextId('sim_txn', 'T885')
      S.txns.set(id, { id, merchant_id: mid, amount, method: 'UPI', payer: 'customer@okhdfc', created_at: t - (20 + Math.floor(r() * 580)) * 60000,
        gateway_status: 'FAILED', bank_debited: true, bank_status: r() < 0.65 ? 'SUCCESS' : 'FAILED', bank_checked: false, recovered: false })
      fields.amount = inr(amount)
    } else if (intent === 'SETTLEMENT_DELAY') {
      const id = nextId('sim_stl', 'S2611')
      S.settlements.set(id, { id, merchant_id: mid, amount: 4000 + Math.floor(r() * 56000), status: 'ON_HOLD',
        hold_reason: m.kyc_status !== 'VERIFIED' ? 'KYC_PENDING' : 'BANK_TIMEOUT', expected_at: t - (30 + Math.floor(r() * 270)) * 60000, settled_at: null, utr: null, destination: m.bank })
    } else if (intent === 'REFUND_ISSUE') {
      const amount = pick([450, 1200, 2600, 7800, 9999])
      const id = nextId('sim_ref', 'R8')
      const failed = r() < 0.7
      S.refunds.set(id, { id, merchant_id: mid, txn_id: null, amount, status: failed ? 'FAILED' : 'PROCESSING', reason: failed ? 'BANK_REJECTED_TECHNICAL' : null,
        initiated_at: t - (1000 + Math.floor(r() * 4000)) * 60000, arn: null, eta_at: failed ? null : t + 1440 * 60000 })
      fields.amount = inr(amount)
    } else if (intent === 'SOUNDBOX') {
      const issue = pick(['volume', 'signal', 'both', 'fault'])
      Object.assign(m.soundbox, { volume: ['volume', 'both'].includes(issue) ? 0 : 70, signal: ['signal', 'both'].includes(issue) ? 'weak' : 'good',
        network_fallback: ['signal', 'both'].includes(issue) ? false : m.soundbox.network_fallback, fault: issue === 'fault' ? 'speaker' : null })
    }
    const tpl = pick(LEX.simulation[intent]).text
    const msg = fields.amount ? fill(tpl, fields) : tpl.replace('{amount}', '').replace(/ {2}/g, ' ')
    const ticket = await handleMessage(mid, msg, async () => {}, { source: 'simulated', pacing: 0 })
    onEvent({ type: 'sim_progress', i: i + 1, n, ticket, metrics: metrics() })
    await sleep(140) // let the dashboard animate
  }
  return metrics()
}

// ====================================================================== public engine
export const localEngine = {
  kind: 'browser',
  async info() {
    return {
      engine: 'browser', mode: live ? 'claude' : 'rules', live,
      routing: live ? { ...CLAUDE } : { triage: 'rules-engine', action: 'playbook-engine', responder: 'templates', escalation: 'template' },
      policy: POLICY,
    }
  },
  setLive(on) { live = Boolean(on) },
  async merchants() { return [...S.merchants.values()].filter((m) => m.id.startsWith('M1')).map(clone) },
  async snapshot(mid) {
    return { merchant: clone(S.merchants.get(mid)) }
  },
  chat(mid, text, onEvent) { return handleMessage(mid, text, async (e) => onEvent(e)) },
  async metrics() { return metrics() },
  async tickets() { return S.tickets.filter((t) => t.source !== 'history').sort(byDesc('created_at')).map((t) => ({ ...clone(t), merchant_name: S.merchants.get(t.merchant_id)?.name })) },
  async escalations() { return [...S.escalations].sort((a, b) => (a.status === 'OPEN' ? 0 : 1) - (b.status === 'OPEN' ? 0 : 1) || b.created_at - a.created_at).map(clone) },
  decide(id, approve, specialist, note) { return decide(id, approve, specialist, note) },
  async audit() { return S.audit.slice(0, 80).map(clone) },
  simulate(n, onEvent) { return simulate(n, onEvent) },
  async reset() { S = freshStore(); return { ok: true } },
}
