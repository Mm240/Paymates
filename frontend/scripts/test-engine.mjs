// Tests for the in-browser engine (src/engine/local.js), run in Node through Vite: npm test
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'

const vite = await createServer({ server: { middlewareMode: true }, appType: 'custom', logLevel: 'error' })
const E = await vite.ssrLoadModule('/src/engine/local.js')
const results = []
async function test(name, fn) {
  try { await fn(); results.push(['ok', name]) } catch (e) { results.push(['FAIL', name, e.message]) }
}
const fresh = () => E.localEngine.reset()
const chat = (mid, text) => E.handleMessage(mid, text, async () => {}, { pacing: 0 })

// Question words ("what", "how", "when") must not dilute a specific intent.
for (const [text, intent] of [
  ['what is this refund i didnt get yet', 'REFUND_ISSUE'],
  ['when will my settlement come', 'SETTLEMENT_DELAY'],
  ['how is my payment failed but money deducted', 'FAILED_PAYMENT'],
  ['what happened to my soundbox, it is not announcing', 'SOUNDBOX'],
  ['how do i finish my kyc', 'KYC'],
]) {
  await test(`triage: "${text}" -> ${intent} >= 0.6`, () => {
    const r = E.rulesTriage(text)
    assert.equal(r.intent, intent)
    assert.ok(r.confidence >= 0.6, `confidence ${r.confidence}`)
  })
}

await test('refund question is acted on, not escalated as LOW_CONFIDENCE', async () => {
  await fresh()
  const t = await chat('M1002', 'what is this refund i didnt get yet')
  assert.equal(t.intent, 'REFUND_ISSUE')
  assert.equal(t.outcome, 'REFUND_NEEDS_APPROVAL')
})

await test('refund amount without currency does not select a different refund', async () => {
  await fresh()
  const tri = E.rulesTriage('2000 refund not done')
  assert.equal(tri.entities.amount, 2000)
  const t = await chat('M1002', '2000 refund not done')
  assert.equal(t.outcome, 'REFUND_NOT_FOUND')
  assert.equal(t.facts.refund_id, undefined)
})

await test('refund amount with attached rs does not select a different refund', async () => {
  await fresh()
  const tri = E.rulesTriage('1000rs refund')
  assert.equal(tri.entities.amount, 1000)
  const t = await chat('M1001', '1000rs refund')
  assert.equal(t.outcome, 'REFUND_NOT_FOUND')
  assert.equal(t.facts.refund_id, undefined)
})

await test('accuracy eval: >= 90% overall on 45 messages', async () => {
  const out = await E.runEvals()
  assert.equal(out.total, 45)
  assert.ok(out.accuracy >= 0.9, JSON.stringify(out.failures))
})

await test('demo 1: failed payment -> PAYMENT_RECOVERED, ₹1,250', async () => {
  await fresh()
  const t = await chat('M1001', "Customer's payment failed but money was deducted.")
  assert.equal(t.outcome, 'PAYMENT_RECOVERED')
  assert.equal(t.recovered_amount, 1250)
})

await test('demo 2: Hinglish settlement -> SETTLEMENT_RETRIED', async () => {
  await fresh()
  const t = await chat('M1001', 'Kal ka settlement nahi aaya')
  assert.equal(t.outcome, 'SETTLEMENT_RETRIED')
  assert.equal(t.lang, 'hinglish')
})

await test('demo 3: refund over limit -> REFUND_NEEDS_APPROVAL', async () => {
  await fresh()
  const t = await chat('M1002', 'Customer says refund not received.')
  assert.equal(t.outcome, 'REFUND_NEEDS_APPROVAL')
  assert.equal(t.status, 'ESCALATED')
})

// Rich reply cards: same type and amounts as the backend.
await test('cards: demos 1-3 give payment_recovered / settlement_resent / refund_held', async () => {
  await fresh()
  const t1 = await chat('M1001', "Customer's payment failed but money was deducted.")
  assert.deepEqual([t1.card.type, t1.card.status, t1.card.amount], ['payment_recovered', 'success', '₹1,250'])
  const t2 = await chat('M1001', 'Kal ka settlement nahi aaya')
  assert.deepEqual([t2.card.type, t2.card.amount, t2.card.steps.map((x) => x.state).join()], ['settlement_resent', '₹18,640', 'done,done,current'])
  const t3 = await chat('M1002', 'Customer says refund not received.')
  assert.deepEqual([t3.card.type, t3.card.status, t3.card.amount, t3.card.actions[0].action], ['refund_held', 'held', '₹7,800', 'talk_to_specialist'])
})

await test('cards: check-in on Gupta Medicals + hisaab', async () => {
  await fresh()
  const out = await E.checkin('M1003', async () => {}, { pacing: 0 })
  const by = Object.fromEntries(out.tickets.map((t) => [t.subject, t.card]))
  assert.deepEqual([by.T88340299.type, by.T88340299.amount], ['payment_recovered', '₹860'])
  assert.deepEqual([by.T88340266.type, by.T88340266.amount], ['council_hold', '₹3,400'])
  assert.ok(by.T88340266.rows.some((r) => r.vote === 'HOLD'))
  assert.deepEqual([by.S2610302.type, by.S2610302.amount], ['settlement_kyc_hold', '₹9,415'])
  assert.equal(by['SB-41890'].title, 'साउंडबॉक्स म्यूट है')
  assert.deepEqual(out.hisaab_card.bars.map((b) => b.value), [740, 1180])
})

await test('cards: specialist follow-up after approving demo 3', async () => {
  await fresh()
  await chat('M1002', 'Customer says refund not received.')
  const esc = (await E.localEngine.escalations())[0]
  const out = await E.decide(esc.id, true, 'Priya (Support)')
  assert.deepEqual([out.followup.card.type, out.followup.card.amount], ['refund_status', '₹7,800'])
})

// ---- understanding: ask, remember, don't guess (mirrors the pytest cases)
const C = await vite.ssrLoadModule('/src/engine/core.js')
const send = (mid, text) => chat(mid, text)

await test('vague payment message asks for the reference and touches nothing', async () => {
  await fresh()
  const t = await send('M1001', 'Mera ek payment fail hua')
  assert.deepEqual([t.status, t.outcome], ['NEEDS_INFO', 'PAYMENT_NOT_FOUND'])
  assert.match(t.reply, /UTR number \(12 digit\)/)
  assert.ok(t.card.quick.some((q) => q.action === 'add_txn_id'))
  const txn = (await E.localEngine.tickets()).length
  assert.equal(txn, 1)
})

await test('multi-turn: the transaction ID continues the SAME case and recovers ₹1,250', async () => {
  await fresh()
  const first = await send('M1001', 'Mera ek payment fail hua')
  const second = await send('M1001', 'T88120045')
  assert.equal(second.id, first.id)
  assert.equal((await E.localEngine.tickets()).length, 1)
  assert.deepEqual([second.status, second.outcome, second.recovered_amount, second.lang], ['RESOLVED_AI', 'PAYMENT_RECOVERED', 1250, 'hinglish'])
})

await test('multi-turn: answered with a 12-digit UTR, or just the amount', async () => {
  await fresh()
  await send('M1001', 'Mera ek payment fail hua')
  assert.equal((await send('M1001', 'UTR 488120045011')).outcome, 'PAYMENT_RECOVERED')
  await fresh()
  await send('M1001', 'Mera ek payment fail hua')
  assert.equal((await send('M1001', '₹1,250')).outcome, 'PAYMENT_RECOVERED')
})

await test('a UTR inside a message survives PII masking', async () => {
  await fresh()
  assert.equal((await send('M1001', 'payment UTR 488120045011 failed')).outcome, 'PAYMENT_RECOVERED')
})

await test('unknown ID: asks to re-check, no escalation; the corrected ID continues the case', async () => {
  await fresh()
  const t = await send('M1001', 'payment T99999999 failed')
  assert.deepEqual([t.status, t.outcome], ['NEEDS_INFO', 'ID_NOT_FOUND'])
  assert.match(t.reply, /T99999999/)
  assert.equal((await E.localEngine.escalations()).length, 0)
  const again = await send('M1001', 'T88120045')
  assert.deepEqual([again.id, again.outcome], [t.id, 'PAYMENT_RECOVERED'])
})

await test("another merchant's ID is not found; 'another payment' and two candidates ask", async () => {
  await fresh()
  assert.equal((await send('M1001', 'payment T88230501 failed')).outcome, 'ID_NOT_FOUND')
  await fresh()
  assert.equal((await send('M1001', 'another payment failed, money was deducted')).outcome, 'PAYMENT_NOT_FOUND')
  await fresh()
  const t = await send('M1003', 'customer ke paise kat gaye payment fail dikha raha')
  assert.equal(t.outcome, 'PAYMENT_NOT_FOUND')
  const t2 = await send('M1003', '₹860')
  assert.deepEqual([t2.id, t2.outcome, t2.recovered_amount], [t.id, 'PAYMENT_RECOVERED', 860])
})

await test('three unanswered asks hand over to a person', async () => {
  await fresh()
  const a = await send('M1001', 'Mera ek payment fail hua')
  await send('M1001', 'T99999991')
  const c = await send('M1001', 'T99999992')
  assert.deepEqual([c.id, c.status, c.outcome], [a.id, 'ESCALATED', 'LOW_CONFIDENCE'])
})

await test('a succeeded payment is never recovered twice', async () => {
  await fresh()
  const t = await send('M1001', 'payment T88120012 failed')
  assert.equal(t.outcome, 'PAYMENT_ALREADY_OK')
})

for (const text of ['hi', 'Hello!', 'namaste', 'thanks a lot', 'ok', 'thik hai bhai', 'धन्यवाद']) {
  await test(`small talk "${text}": friendly reply, quick options, never escalated, not a support ticket`, async () => {
    await fresh()
    const t = await send('M1001', text)
    assert.equal(t.status, 'RESOLVED_AI')
    assert.ok(t.outcome.startsWith('SMALL_TALK'))
    assert.equal(t.lang, 'hinglish')
    assert.equal(t.card.quick.length, 6)
    assert.equal((await E.localEngine.escalations()).length, 0)
    assert.equal((await E.localEngine.metrics()).total, 0)
  })
}

await test('small talk keeps the open question open', async () => {
  await fresh()
  const first = await send('M1001', 'Mera ek payment fail hua')
  await send('M1001', 'ok')
  const t = await send('M1001', 'T88120045')
  assert.deepEqual([t.id, t.outcome], [first.id, 'PAYMENT_RECOVERED'])
})

await test('ambiguous message: one question with chips, then continues on the same ticket; twice unclear hands over', async () => {
  await fresh()
  const t = await send('M1002', 'Something is off with my account, can you look into it?')
  assert.deepEqual([t.status, t.outcome], ['NEEDS_INFO', 'CLARIFY'])
  assert.deepEqual(t.card.quick.map((q) => q.send), ['Payment failed', 'Settlement', 'Refund', 'Soundbox', 'KYC', 'Talk to a person'])
  const t2 = await send('M1002', 'Refund')
  assert.deepEqual([t2.id, t2.outcome], [t.id, 'REFUND_NEEDS_APPROVAL'])
  await fresh()
  const u = await send('M1002', 'Something is off with my account, can you look into it?')
  const u2 = await send('M1002', 'I dont know, it is just not right')
  assert.deepEqual([u2.id, u2.status, u2.outcome], [u.id, 'ESCALATED', 'LOW_CONFIDENCE'])
})

for (const [text, amount] of [['₹1,250 failed', 1250], ['Rs 1250 payment', 1250], ['Rs. 1,250', 1250], ['1250 rupees kata', 1250], ['1250 ka payment fail', 1250],
  ['1.2k payment failed', 1200], ['5k refund', 5000], ['1250 रुपये कट गए', 1250], ['1000rs refund', 1000], ['2000 refund not done', 2000]]) {
  await test(`amount format "${text}" -> ${amount}`, () => assert.equal(C.extractEntities(text).amount, amount))
}

await test('a UTR is not an amount; IDs are extracted', () => {
  const e = C.extractEntities('UTR 488120045011 ka status, T88120045, R77812, S2610300')
  assert.equal(e.utr, '488120045011')
  assert.equal(e.amount, undefined)
  assert.deepEqual([e.txn_id, e.refund_id, e.settlement_id], ['T88120045', 'R77812', 'S2610300'])
})

const messy = JSON.parse(readFileSync(new URL('../../shared/messy_tests.json', import.meta.url))).items
for (const m of messy) {
  await test(`messy ${m.id}: "${m.text}" -> ${m.intent}`, () => {
    const r = E.rulesTriage(m.text)
    assert.equal(r.intent, m.intent)
    if (!['UNKNOWN', 'SMALL_TALK'].includes(m.intent)) assert.ok(r.confidence >= 0.6, `confidence ${r.confidence}`)
  })
}

// ---- unit economics + pilot gates (mirrors the pytest cases)
await test('price table: longest matching model name wins; local models are free', () => {
  assert.equal(E.priceFor('gemini-3.5-flash-lite').output, 0.4)
  assert.equal(E.priceFor('gemini-3.5-flash').output, 2.5)
  assert.ok(Math.abs(E.costInr('gemini-3.5-flash-lite', 1e6, 0) - 0.1 * 88) < 1e-9)
  assert.equal(E.costInr('llama3.1', 5000, 5000), 0)
})

await test('rules engine costs nothing: ₹0 per ticket, 100% of steps by code', async () => {
  await fresh()
  const t = await chat('M1001', "Customer's payment failed but money was deducted.")
  assert.deepEqual([t.ai_cost_inr, t.llm_calls.length], [0, 0])
  const m = await E.localEngine.metrics()
  assert.deepEqual([m.ai_cost_total, m.ai_cost_per_resolved, m.code_steps_pct, m.llm_calls], [0, 0, 1, 0])
})

await test('pilot numbers after the three demos: 2 autonomous payouts, 0 wrong, 1 blocked, 1/1 escalation briefed', async () => {
  await fresh()
  await chat('M1001', "Customer's payment failed but money was deducted.")
  await chat('M1001', 'Kal ka settlement nahi aaya')
  await chat('M1002', 'Customer says refund not received.')
  const p = (await E.localEngine.metrics()).pilot
  assert.deepEqual(p, { autonomous_payouts: 2, wrong_payouts: 0, blocked_unsafe: 1, escalations: 1, escalations_briefed: 1 })
})

await test('rules-only evals never touch a model and clear 90%', async () => {
  const out = await E.runEvals(true)
  assert.deepEqual(out.models, { 'rules-engine': 45 })
  assert.ok(out.accuracy >= 0.9)
})

await test('live model: code path skips the call (₹0); the unsure path is metered with tokens and ₹', async () => {
  const calls = []
  const fake = async (prompt) => { calls.push('text'); return { text: '{"outcome":"resolved","summary":"ok"}' } }
  fake.json = async () => { calls.push('json'); return { intent: 'SETTLEMENT_DELAY', confidence: 0.8, entities: {}, urgency: 'high', reasoning: 'x' } }
  fake.limits = async () => ({})
  globalThis.window = { claude: { use: async () => fake } }
  try {
    E.localEngine.setLive(true)
    await fresh()
    const confident = await chat('M1001', "Customer's payment failed but money was deducted.") // 97% from the rules alone
    assert.equal(confident.llm_calls.filter((c) => c.agent === 'triage').length, 0)
    await fresh()
    const unsure = await chat('M1001', 'Kal ka settlement nahi aaya') // 84%: not confident enough for the code path
    const tri = unsure.llm_calls.find((c) => c.agent === 'triage')
    assert.ok(tri, 'triage should have called the model')
    assert.deepEqual([tri.provider, tri.model, tri.estimated], ['claude', 'claude · quick tier', true])
    assert.ok(tri.input_tokens > 0 && tri.output_tokens > 0 && tri.cost_inr > 0)
    assert.ok(Math.abs(unsure.ai_cost_inr - unsure.llm_calls.reduce((t, c) => t + c.cost_inr, 0)) < 1e-6)
    const m = await E.localEngine.metrics()
    assert.ok(m.llm_calls >= 1 && m.ai_cost_total > 0)
  } finally {
    E.localEngine.setLive(false)
    delete globalThis.window
  }
})

await vite.close()
for (const [s, n, m] of results) console.log(`${s === 'ok' ? '  ok  ' : '  FAIL'} ${n}${m ? `\n        ${m}` : ''}`)
const failed = results.filter((r) => r[0] !== 'ok').length
console.log(`\n${results.length - failed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
