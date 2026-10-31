// Shared helpers: language, tokens, money formatting, templates.
import lexicon from '../../../shared/lexicon.json'
import templates from '../../../shared/templates.json'

export const LEX = lexicon
export const TPL = templates

const TOKEN = /[a-z0-9]+|[ऀ-ॿ]+/g
const DEVA = /[ऀ-ॿ]/
const ASCII = /^[\x00-\x7F]*$/

export const tokens = (t) => (String(t).toLowerCase().match(TOKEN) || [])

export function detectLanguage(text) {
  if (DEVA.test(text)) return 'hi'
  const markers = new Set(LEX.hinglish_markers)
  const strong = new Set(LEX.strong_hinglish)
  const hits = tokens(text).filter((t) => markers.has(t))
  if (hits.length >= 2 || hits.some((t) => strong.has(t))) return 'hinglish'
  return 'en'
}

export function keywordHits(text, keywords) {
  const low = String(text).toLowerCase()
  const toks = tokens(text)
  const found = []
  for (const kw of keywords) {
    if (kw.includes(' ') || !ASCII.test(kw)) {
      if (low.includes(kw)) found.push(kw)
    } else if (kw.length < 4) {
      if (toks.includes(kw)) found.push(kw)
    } else if (toks.some((t) => t.startsWith(kw))) found.push(kw)
  }
  return found
}

const CUR = '(?:₹|rs\\.?|inr|rupees?|rupaye|rupay|रुपये|रुपए)'
const NUM = '(\\d{1,7}(?:,\\d{2,3})*(?:\\.\\d+)?)'
const DEBITED = /deduct|debit|\bcut\b|\bkat\b|\bkata\b|kat (?:gay|gaye|gya)|money (?:gone|taken)|charged|कट|डेबिट/i
const OTHER = /\b(?:another|different|some other|other (?:payment|txn|transaction|refund|settlement)|do+sr[ae]|dusr[aei]|koi aur|alag)\b|दूसरा|दूसरे|दूसरी|अलग|कोई और/i

// ₹1,250 · Rs 1250 · 1250 rupees · 1250 ka · 1.2k · 1250 रुपये. 12-digit UTRs and IDs are never amounts.
export function extractAmount(text) {
  let m = text.match(new RegExp(`${CUR}\\s?${NUM}\\s?(k)?(?![\\w])`, 'i'))
    || text.match(new RegExp(`(?<![\\w.])${NUM}\\s?(k)?\\s?(?:${CUR}|ka|ke|ki|का|के|की)(?![\\w])`, 'i'))
    || text.match(/(?<![\w.])(\d+(?:\.\d+)?)\s?(k)(?![\w])/i)
  if (!m && /\brefund\b|रिफंड/i.test(text)) m = text.match(/(?<![\w.])(\d{2,7}(?:,\d{2,3})*)(?![\w])/) // "2000 refund not done"
  if (!m) return null
  let value = parseFloat(m[1].replace(/,/g, ''))
  if (Number.isNaN(value)) return null
  if ((m[2] || '').toLowerCase() === 'k') value *= 1000
  return Math.round(value)
}

// Pull IDs and the amount out of the RAW message. Call this before PII masking: the mask hides long numbers like a UTR.
export function extractEntities(text) {
  const e = {}
  let m
  if ((m = text.match(/\bT\d{6,}\b/i))) e.txn_id = m[0].toUpperCase()
  if ((m = text.match(/\bS\d{6,}\b/i))) e.settlement_id = m[0].toUpperCase()
  if ((m = text.match(/\bR\d{4,}\b/i))) e.refund_id = m[0].toUpperCase()
  if ((m = text.match(/(?<![\w.])\d{12}(?![\w])/))) e.utr = m[0]
  const amount = extractAmount(text)
  if (amount !== null) e.amount = amount
  if (OTHER.test(text)) e.other = true
  if (DEBITED.test(text)) e.debited = true // the merchant says the customer's money was taken
  return e
}

// ---------------------------------------------------------------- small talk
const GREET = new Set(['hi', 'hii', 'hiii', 'hello', 'hey', 'heyy', 'hlo', 'helo', 'namaste', 'namaskar', 'hola', 'good morning', 'good afternoon',
  'good evening', 'gm', 'नमस्ते', 'नमस्कार', 'हेलो', 'हैलो', 'हाय'])
const THANKS = new Set(['thanks', 'thank you', 'thankyou', 'thx', 'ty', 'ok', 'okay', 'okk', 'k', 'kk', 'thik hai', 'theek hai', 'thik', 'theek', 'shukriya',
  'dhanyavad', 'dhanyawad', 'got it', 'fine', 'cool', 'great', 'accha', 'achha', 'acha', 'bye', 'धन्यवाद', 'शुक्रिया', 'ठीक है', 'ठीक',
  'अच्छा', 'बढ़िया', 'थैंक्यू', 'थैंक यू'])
const FILLER = new Set(['a', 'lot', 'so', 'much', 'very', 'bhai', 'ji', 'sir', 'madam', 'didi', 'boss', 'team', 'paymates', 'there', 'again', 'bahut', 'जी', 'भाई', 'बहुत'])

// 'greet' | 'thanks' for a message that is only a greeting or an acknowledgement, else null.
export function smallTalkKind(text) {
  const t = text.toLowerCase().replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}!?.,;:"'()[\]{}<>|/\\~*_-]+/gu, ' ')
  if (!t.trim()) return /[\u{1F44D}\u{1F64F}]/u.test(text) ? 'thanks' : null
  const words = t.split(/\s+/).filter((w) => w && !FILLER.has(w))
  const joined = words.join(' ')
  if (GREET.has(joined) || (words.length && words.every((w) => GREET.has(w)))) return 'greet'
  if (THANKS.has(joined) || (words.length && words.every((w) => THANKS.has(w) || GREET.has(w)) && words.some((w) => THANKS.has(w)))) return 'thanks'
  return null
}

export function inr(n) {
  if (n == null) return '₹0'
  const v = Math.round(Number(n))
  return (v < 0 ? '-' : '') + '₹' + Math.abs(v).toLocaleString('en-IN')
}

export function phrase(key, lang, kw = {}) {
  const p = TPL.phrases[key]
  return fill((p && (p[lang] || p.en)) || '', kw)
}

export function fill(str, facts) {
  return String(str).replace(/\{(\w+)\}/g, (_, k) => (facts[k] !== undefined && facts[k] !== null ? String(facts[k]) : `{${k}}`))
}

export function render(code, lang, facts) {
  const t = TPL.replies[code] || TPL.replies.LOW_CONFIDENCE
  return fill(t[lang] || t.en, facts)
}

export function ago(ts, lang) {
  if (!ts) return ''
  const mins = Math.max(1, Math.round((Date.now() - ts) / 60000))
  if (mins < 60) return phrase('minutes_ago', lang, { n: mins })
  return phrase('hours_ago', lang, { n: Math.round(mins / 60) })
}

const PHONE = /\b([6-9]\d)\d{6}(\d{2})\b/g
const VPA = /\b([a-z0-9._-]{2})[a-z0-9._-]*@([a-z]+)\b/gi
const LONGNUM = /\b\d{8,15}(\d{4})\b/g
export function maskPii(text) {
  if (!text) return text
  return String(text)
    .replace(PHONE, (_, a, b) => `${a}XXXXXX${b}`)
    .replace(VPA, (_, a, b) => `${a}***@${b}`)
    .replace(LONGNUM, (_, a) => `••${a}`)
}

// ---------------------------------------------------------------- help-center retrieval (TF-IDF vectors)
const STOP = new Set(LEX.stopwords)
const SYN = LEX.rag_synonyms
function terms(text) {
  const out = []
  for (const t of tokens(text)) {
    if (STOP.has(t)) continue
    out.push(t)
    if (SYN[t]) out.push(...SYN[t].split(' '))
  }
  return out.map((t) => (t.length > 4 && t.endsWith('s') && ASCII.test(t) ? t.slice(0, -1) : t))
}
function counts(arr) {
  const c = new Map()
  for (const x of arr) c.set(x, (c.get(x) || 0) + 1)
  return c
}

export class HelpCenterIndex {
  constructor(docs) {
    this.docs = docs
    const tfs = docs.map((d) => counts(terms(`${d.title} ${d.title} ${d.body}`)))
    const df = new Map()
    tfs.forEach((c) => c.forEach((_, t) => df.set(t, (df.get(t) || 0) + 1)))
    const n = docs.length
    this.idf = new Map([...df].map(([t, c]) => [t, Math.log((1 + n) / (1 + c)) + 1]))
    this.vecs = tfs.map((c) => this.weigh(c))
  }
  weigh(c) {
    const v = new Map()
    c.forEach((cnt, t) => v.set(t, (1 + Math.log(cnt)) * (this.idf.get(t) || 0)))
    const norm = Math.sqrt([...v.values()].reduce((s, x) => s + x * x, 0)) || 1
    v.forEach((x, t) => v.set(t, x / norm))
    return v
  }
  search(query, k = 3) {
    const q = this.weigh(counts(terms(query)))
    return this.docs
      .map((d, i) => {
        let s = 0
        q.forEach((w, t) => (s += w * (this.vecs[i].get(t) || 0)))
        return { s, d }
      })
      .sort((a, b) => b.s - a.s)
      .slice(0, k)
      .filter((x) => x.s > 0)
      .map(({ s, d }) => ({ id: d.id, title: d.title, score: Math.round(s * 1000) / 1000, snippet: d.body.slice(0, 180), short: d.short }))
  }
}

export const LANG_LABEL = { en: 'English', hi: 'Hindi', hinglish: 'Hinglish' }
