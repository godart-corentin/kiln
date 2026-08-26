import type { BuildState, JobState } from '../api/types'

type State = BuildState | JobState

const labels: Record<State, { symbol: string; label: string; className: string }> = {
  queued: { symbol: '•', label: 'Queued', className: 'pending' },
  preparing: { symbol: '●', label: 'Preparing', className: 'running' },
  pending: { symbol: '•', label: 'Pending', className: 'pending' },
  running: { symbol: '●', label: 'Running', className: 'running' },
  success: { symbol: '✓', label: 'Success', className: 'success' },
  failed: { symbol: '×', label: 'Failed', className: 'failed' },
  aborted: { symbol: '!', label: 'Aborted', className: 'aborted' },
  skipped: { symbol: '»', label: 'Skipped', className: 'skipped' },
}

export function StatusBadge({ state }: { state: State }) {
  const item = labels[state] ?? { symbol: '?', label: state, className: 'pending' }
  return <span className={`badge ${item.className}`}>{item.symbol} {item.label}</span>
}
