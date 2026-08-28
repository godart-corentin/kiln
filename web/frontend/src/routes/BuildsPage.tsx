import { Link } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { getBuilds } from '../api/client'
import type { BuildSummary } from '../api/types'
import { StatusBadge } from '../components/StatusBadge'
import { duration, shortRef } from '../utils'

export function BuildsPage() {
  const [builds, setBuilds] = useState<BuildSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const refresh = () => {
      getBuilds(controller.signal)
        .then((items) => { setBuilds(items); setError(null) })
        .catch((reason: unknown) => {
          if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason))
        })
    }
    refresh()
    const timer = window.setInterval(refresh, 10_000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [])

  return (
    <>
      <div className="hero"><div><h1>Builds</h1><div className="muted">Latest Kilnr activity</div></div></div>
      {error && <section className="panel empty">{error}</section>}
      {builds && builds.length === 0 && <section className="panel empty">No builds yet.</section>}
      {builds && builds.length > 0 && (
        <section className="panel">
          {builds.map((build) => (
            <Link className="build-row" key={build.build_id} to="/build/$buildId" params={{ buildId: build.build_id }}>
              <div><div className="project">{build.project}</div><div className="muted">{shortRef(build.ref)} · <span className="sha">{build.sha.slice(0, 7)}</span></div></div>
              <div className="branch muted">{build.type}</div>
              <StatusBadge state={build.state} />
              <div className="duration muted">{duration(build.duration_seconds)}</div>
            </Link>
          ))}
        </section>
      )}
      {!builds && !error && <section className="panel empty">Loading builds…</section>}
    </>
  )
}
