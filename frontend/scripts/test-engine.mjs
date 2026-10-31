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

await vite.close()
for (const [s, n, m] of results) console.log(`${s === 'ok' ? '  ok  ' : '  FAIL'} ${n}${m ? `\n        ${m}` : ''}`)
const failed = results.filter((r) => r[0] !== 'ok').length
console.log(`\n${results.length - failed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
