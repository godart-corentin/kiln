import { Link } from '@tanstack/react-router'
import { LogViewer } from '../components/LogViewer'

export function LogPage({ buildId, job }: { buildId: string; job: string }) {
  return (
    <>
      <Link className="back" to="/build/$buildId" params={{ buildId }}>← Build</Link>
      <div className="hero"><div><h1>{job}</h1><div className="muted">Live job log</div></div></div>
      <LogViewer buildId={buildId} job={job} />
    </>
  )
}
