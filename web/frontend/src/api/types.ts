export type BuildState = 'queued' | 'preparing' | 'running' | 'success' | 'failed' | 'aborted'
export type JobState = 'pending' | 'running' | 'success' | 'failed' | 'skipped' | 'aborted'

export interface BuildSummary {
  build_id: string
  project: string
  sha: string
  ref: string
  type: 'ci' | 'release'
  state: BuildState
  created_at?: string | null
  started_at?: string | null
  finished_at?: string | null
  duration_seconds?: number | null
}

export interface PipelineJob {
  group?: string | null
  needs: string[]
  resolved_needs: string[]
  state: JobState
  log?: string | null
  started_at?: string | null
  finished_at?: string | null
  duration_seconds?: number | null
}

export interface PipelineStatus {
  groups: Record<string, string[]>
  jobs: Record<string, PipelineJob>
}

export interface BuildStatus extends BuildSummary {
  pipeline: PipelineStatus | null
}

export interface ArtifactItem {
  path: string
  size: number
}

export interface LogSnapshot {
  content: string
  offset: number
  truncated: boolean
  state?: BuildState | JobState | null
}

export interface JobEvent {
  name: string
  state: JobState
  duration_seconds?: number
}

export interface BuildEvent {
  state: BuildState
  duration_seconds?: number
}
