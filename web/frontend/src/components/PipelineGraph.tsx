import { Link } from '@tanstack/react-router'
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { PipelineStatus } from '../api/types'
import { StatusBadge } from './StatusBadge'

interface Edge {
  from: string
  to: string
}

interface DrawnEdge extends Edge {
  x1: number
  y1: number
  x2: number
  y2: number
}

function computeDepth(name: string, pipeline: PipelineStatus, memo: Map<string, number>): number {
  const cached = memo.get(name)
  if (cached !== undefined) return cached
  const job = pipeline.jobs[name]
  if (!job || job.resolved_needs.length === 0) {
    memo.set(name, 0)
    return 0
  }
  const depth = 1 + Math.max(...job.resolved_needs.map((dependency) => computeDepth(dependency, pipeline, memo)))
  memo.set(name, depth)
  return depth
}

export function PipelineGraph({ buildId, pipeline }: { buildId: string; pipeline: PipelineStatus }) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const nodeRefs = useRef(new Map<string, HTMLDivElement>())
  const [drawnEdges, setDrawnEdges] = useState<DrawnEdge[]>([])

  const { blocks, edges } = useMemo(() => {
    const depthMemo = new Map<string, number>()
    const jobDepth = new Map(
      Object.keys(pipeline.jobs).map((name) => [name, computeDepth(name, pipeline, depthMemo)]),
    )
    const assigned = new Set<string>()
    const nextBlocks: Array<{ key: string; label: string | null; jobs: string[]; depth: number }> = []

    for (const [group, members] of Object.entries(pipeline.groups)) {
      const jobs = members.filter((name) => pipeline.jobs[name])
      if (jobs.length === 0) continue
      jobs.forEach((name) => assigned.add(name))
      nextBlocks.push({
        key: `group:${group}`,
        label: group,
        jobs: [...jobs].sort((a, b) => (jobDepth.get(a) ?? 0) - (jobDepth.get(b) ?? 0) || a.localeCompare(b)),
        depth: Math.min(...jobs.map((name) => jobDepth.get(name) ?? 0)),
      })
    }

    for (const name of Object.keys(pipeline.jobs)) {
      if (assigned.has(name)) continue
      nextBlocks.push({ key: `job:${name}`, label: null, jobs: [name], depth: jobDepth.get(name) ?? 0 })
    }

    nextBlocks.sort((a, b) => a.depth - b.depth || a.key.localeCompare(b.key))

    const nextEdges: Edge[] = []
    for (const [to, job] of Object.entries(pipeline.jobs)) {
      for (const from of job.resolved_needs) nextEdges.push({ from, to })
    }
    return { blocks: nextBlocks, edges: nextEdges }
  }, [pipeline])

  const measure = useCallback(() => {
    const container = containerRef.current
    if (!container) return
    const base = container.getBoundingClientRect()
    const next: DrawnEdge[] = []
    for (const edge of edges) {
      const source = nodeRefs.current.get(edge.from)
      const target = nodeRefs.current.get(edge.to)
      if (!source || !target) continue
      const a = source.getBoundingClientRect()
      const b = target.getBoundingClientRect()
      next.push({
        ...edge,
        x1: a.right - base.left,
        y1: a.top + a.height / 2 - base.top,
        x2: b.left - base.left,
        y2: b.top + b.height / 2 - base.top,
      })
    }
    setDrawnEdges(next)
  }, [edges])

  useLayoutEffect(() => {
    measure()
    const observer = new ResizeObserver(measure)
    if (containerRef.current) observer.observe(containerRef.current)
    for (const node of nodeRefs.current.values()) observer.observe(node)
    window.addEventListener('resize', measure)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [measure, pipeline])

  return (
    <div className="dag-scroll">
      <div className="dag" ref={containerRef}>
        <svg className="dag-edges" aria-hidden="true">
          {drawnEdges.map((edge) => {
            const direction = edge.x2 >= edge.x1 ? 1 : -1
            const bend = Math.max(34, Math.abs(edge.x2 - edge.x1) * 0.45)
            const d = `M ${edge.x1} ${edge.y1} C ${edge.x1 + bend * direction} ${edge.y1}, ${edge.x2 - bend * direction} ${edge.y2}, ${edge.x2} ${edge.y2}`
            return <path key={`${edge.from}->${edge.to}`} d={d} />
          })}
        </svg>

        <div className="dag-blocks">
          {blocks.map((block) => (
            <section className={`dag-block ${block.label ? 'dag-group' : ''}`} key={block.key}>
              {block.label && <div className="dag-group-label">{block.label}</div>}
              <div className="dag-jobs">
                {block.jobs.map((name) => {
                  const job = pipeline.jobs[name]
                  return (
                    <div
                      className="dag-node"
                      key={name}
                      ref={(node: HTMLDivElement | null) => {
                        if (node) nodeRefs.current.set(name, node)
                        else nodeRefs.current.delete(name)
                      }}
                    >
                      <Link to="/build/$buildId/logs/$job" params={{ buildId, job: name }} className="dag-node-link">
                        <span className="dag-node-name">{name}</span>
                        <StatusBadge state={job.state} />
                      </Link>
                    </div>
                  )
                })}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}
