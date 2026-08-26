import { useEffect, useRef, useState } from 'react'
import { getLog } from '../api/client'
import type { LogSnapshot } from '../api/types'

const BOTTOM_THRESHOLD = 48

export function LogViewer({ buildId, job }: { buildId: string; job: string }) {
  const [snapshot, setSnapshot] = useState<LogSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [newOutput, setNewOutput] = useState(false)
  const preRef = useRef<HTMLPreElement | null>(null)
  const offsetRef = useRef(0)
  const followRef = useRef(true)

  useEffect(() => {
    let cancelled = false
    let stream: EventSource | null = null
    let reconnectTimer: number | null = null
    const controller = new AbortController()

    const connect = () => {
      if (cancelled) return
      stream = new EventSource(
        `/api/builds/${encodeURIComponent(buildId)}/logs/${encodeURIComponent(job)}/stream?offset=${offsetRef.current}`,
      )
      stream.addEventListener('chunk', (raw) => {
        const event = raw as MessageEvent<string>
        const chunk = JSON.parse(event.data) as { offset: number; content: string }
        offsetRef.current = chunk.offset
        setSnapshot((current) => current ? { ...current, content: current.content + chunk.content, offset: chunk.offset } : current)
        if (!followRef.current) setNewOutput(true)
      })
      stream.addEventListener('end', (raw) => {
        const event = raw as MessageEvent<string>
        const end = JSON.parse(event.data) as { offset: number; state?: LogSnapshot['state'] }
        offsetRef.current = end.offset
        setSnapshot((current) => current ? { ...current, offset: end.offset, state: end.state } : current)
        stream?.close()
      })
      stream.onerror = () => {
        stream?.close()
        if (!cancelled) reconnectTimer = window.setTimeout(connect, 750)
      }
    }

    getLog(buildId, job, controller.signal)
      .then((initial) => {
        if (cancelled) return
        offsetRef.current = initial.offset
        setSnapshot(initial)
        connect()
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
      })

    return () => {
      cancelled = true
      controller.abort()
      stream?.close()
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
    }
  }, [buildId, job])

  useEffect(() => {
    const pre = preRef.current
    if (!pre || !followRef.current) return
    pre.scrollTop = pre.scrollHeight
  }, [snapshot?.content])

  const onScroll = () => {
    const pre = preRef.current
    if (!pre) return
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight <= BOTTOM_THRESHOLD
    followRef.current = atBottom
    if (atBottom) setNewOutput(false)
  }

  const jumpToBottom = () => {
    const pre = preRef.current
    if (!pre) return
    followRef.current = true
    setNewOutput(false)
    pre.scrollTop = pre.scrollHeight
  }

  if (error) return <section className="panel empty">{error}</section>
  if (!snapshot) return <section className="panel empty">Loading log…</section>

  return (
    <div className="log-shell">
      {snapshot.truncated && <div className="notice">Showing the last 2 MiB. Use the Kiln CLI for the complete log.</div>}
      <section className="panel log-panel">
        <pre ref={preRef} onScroll={onScroll}>{snapshot.content}</pre>
      </section>
      {newOutput && <button className="jump-button" onClick={jumpToBottom}>Jump to bottom</button>}
    </div>
  )
}
