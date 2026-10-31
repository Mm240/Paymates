// Tests for the in-browser engine (src/engine/local.js), run in Node through Vite: npm test
import assert from 'node:assert/strict'
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

await test('accuracy eval: >= 85% overall on 45 messages', async () => {
  const out = await E.runEvals()
  assert.equal(out.total, 45)
  assert.ok(out.accuracy >= 0.85, JSON.stringify(out.failures))
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

await vite.close()
for (const [s, n, m] of results) console.log(`${s === 'ok' ? '  ok  ' : '  FAIL'} ${n}${m ? `\n        ${m}` : ''}`)
const failed = results.filter((r) => r[0] !== 'ok').length
console.log(`\n${results.length - failed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
