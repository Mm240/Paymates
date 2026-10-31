// Client for the FastAPI backend. Agent steps arrive as Server-Sent Events over a POST stream.
const BASE = (typeof window !== 'undefined' && window.PAYMATES_API) || ''

async function json(path, opts = {}) {
  const res = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...opts })
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

async function stream(path, body, onEvent) {
  const res = await fetch(BASE + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!res.ok || !res.body) throw new Error(`Backend error ${res.status}`)
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  let last = null
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i)
      buf = buf.slice(i + 2)
      const line = chunk.split('\n').find((l) => l.startsWith('data: '))
      if (!line) continue
      const ev = JSON.parse(line.slice(6))
      if (ev.type === 'error') throw new Error(ev.message)
      onEvent(ev)
      last = ev
    }
  }
  return last
}

export async function backendReachable(timeoutMs = 1500) {
  try {
    const ctl = new AbortController()
    const t = setTimeout(() => ctl.abort(), timeoutMs)
    const res = await fetch(BASE + '/api/health', { signal: ctl.signal })
    clearTimeout(t)
    if (!res.ok) return false
    const data = await res.json()
    return data && data.engine === 'backend'
  } catch {
    return false
  }
}

export const remoteEngine = {
  kind: 'backend',
  info: () => json('/api/health'),
  setLive() {},
  merchants: () => json('/api/merchants'),
  snapshot: (mid) => json(`/api/merchants/${mid}/snapshot`),
  async chat(mid, text, onEvent) {
    const last = await stream('/api/chat', { merchant_id: mid, message: text }, onEvent)
    return last && last.ticket
  },
  async checkin(mid, onEvent) {
    const last = await stream(`/api/merchants/${mid}/checkin`, {}, onEvent)
    return last
  },
  metrics: () => json('/api/metrics'),
  tickets: () => json('/api/tickets'),
  escalations: () => json('/api/escalations'),
  decide: (id, approve, specialist, note) =>
    json(`/api/escalations/${id}/${approve ? 'approve' : 'reject'}`, { method: 'POST', body: JSON.stringify({ specialist, note }) }),
  audit: () => json('/api/audit'),
  async simulate(n, onEvent) {
    const last = await stream('/api/simulate', { n }, onEvent)
    return last && last.metrics
  },
  reset: () => json('/api/reset', { method: 'POST' }),
}
