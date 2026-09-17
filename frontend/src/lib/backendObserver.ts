export type ObservationCause = { label: string; trigger: 'user' | 'effect' | 'poll'; code?: string }
export type ObservedRequest = { id: number; method: string; path: string; status: number; durationMs: number; ticket: string | null; startedAt: string; finishedAt: string; cause?: ObservationCause }
let enabled = false
let sequence = 0
let epoch = 0
let entries: ObservedRequest[] = []
const listeners = new Set<() => void>()
export const subscribeObserver = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
export const observerSnapshot = () => entries
export function configureObserver(value: boolean) {
  enabled = value; epoch++; entries = []; listeners.forEach(fn => fn())
}
export function startObservation(path: string, method: string, cause?: ObservationCause) {
  if (!enabled || path.startsWith('/backend-traces')) return null
  const started = performance.now(), currentEpoch = epoch, id = ++sequence
  const startedAt = new Date().toISOString()
  const safePath = path.split('?')[0].replace(/\/[0-9a-f-]{8,}(?=\/|$)|\/\d+(?=\/|$)/gi, '/:id')
  return (status: number, ticket: string | null) => {
    if (!enabled || epoch !== currentEpoch) return
    entries = [{ id, method, path: safePath, status, durationMs: performance.now() - started, ticket,
      startedAt, finishedAt: new Date().toISOString(), cause }, ...entries].slice(0, 30)
    listeners.forEach(fn => fn())
  }
}
