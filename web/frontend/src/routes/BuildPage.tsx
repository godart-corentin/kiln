import { Link } from '@tanstack/react-router'
import { useCallback, useEffect, useState } from 'react'
import { getArtifacts, getBuild } from '../api/client'
import type { ArtifactItem, BuildEvent, BuildStatus, JobEvent } from '../api/types'
import { PipelineGraph } from '../components/PipelineGraph'
import { StatusBadge } from '../components/StatusBadge'
import { duration, formatTime, shortRef } from '../utils'

export function BuildPage({ buildId }: { buildId: string }) {
  const [build, setBuild] = useState<BuildStatus | null>(null)
  const [artifacts, setArtifacts] = useState<ArtifactItem[]>([])
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [nextBuild, nextArtifacts] = await Promise.all([getBuild(buildId, signal), getArtifacts(buildId, signal)])
    setBuild(nextBuild)
    setArtifacts(nextArtifacts)
  }, [buildId])

  useEffect(() => {
    const controller = new AbortController()
    let source: EventSource | null = null

    refresh(controller.signal)
      .then(() => {
        if (controller.signal.aborted) return
        source = new EventSource(`/api/builds/${encodeURIComponent(buildId)}/events`)
        source.addEventListener('job', (raw) => {
          const event = JSON.parse((raw as MessageEvent<string>).data) as JobEvent
          setBuild((current) => {
            if (!current || !current.pipeline || !current.pipeline.jobs[event.name]) return current
            return {
              ...current,
              pipeline: {
                ...current.pipeline,
                jobs: {
                  ...current.pipeline.jobs,
                  [event.name]: { ...current.pipeline.jobs[event.name], ...event },
                },
              },
            }
          })
        })
        source.addEventListener('build', (raw) => {
          const event = JSON.parse((raw as MessageEvent<string>).data) as BuildEvent
          setBuild((current) => current ? { ...current, ...event } : current)
        })
        source.addEventListener('end', () => {
          source?.close()
          void refresh()
        })
        source.onerror = () => {
          void refresh()
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason))
      })

    return () => {
      controller.abort()
      source?.close()
    }
  }, [buildId, refresh])

  if (error) return <section className="panel empty">{error}</section>
  if (!build) return <section className="panel empty">Loading build…</section>

  return (
    <>
      <Link className="back" to="/">← All builds</Link>
      <div className="hero">
        <div>
          <h1>{build.project}</h1>
          <div className="meta"><span>{shortRef(build.ref)}</span><span className="sha">{build.sha}</span><span>{build.type}</span></div>
        </div>
        <StatusBadge state={build.state} />
      </div>
      <div className="meta"><span>Duration: {duration(build.duration_seconds)}</span><span>Started: {formatTime(build.started_at)}</span></div>

      <h2>Pipeline</h2>
      {build.pipeline ? (
        <section className="panel graph-panel"><PipelineGraph buildId={buildId} pipeline={build.pipeline} /></section>
      ) : (
        <section className="panel empty">Pipeline is preparing…</section>
      )}

      <h2>Jobs</h2>
      <section className="panel jobs-list">
        {build.pipeline ? Object.entries(build.pipeline.jobs).map(([name, job]) => (
          <div className="job-row" key={name}>
            <div className="muted">{job.group || '—'}</div>
            <div className="job-name">{name}</div>
            <StatusBadge state={job.state} />
            <div>{job.log ? <Link className="log-link" to="/build/$buildId/logs/$job" params={{ buildId, job: name }}>view log</Link> : '—'}</div>
          </div>
        )) : <div className="empty">No jobs yet.</div>}
      </section>
      <p><Link className="log-link" to="/build/$buildId/logs/$job" params={{ buildId, job: 'pipeline' }}>View pipeline log</Link></p>

      <h2>Artifacts</h2>
      <section className="panel">
        {artifacts.length === 0 && <div className="empty">No artifacts.</div>}
        {artifacts.map((artifact) => <div className="artifact" key={artifact.path}><span>{artifact.path}</span><span className="muted">{artifact.size.toLocaleString()} bytes</span></div>)}
      </section>
    </>
  )
}
