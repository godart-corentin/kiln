import type { ArtifactItem, BuildStatus, BuildSummary, LogSnapshot } from './types'

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    credentials: 'same-origin',
    cache: 'no-store',
    signal,
  })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const body = (await response.json()) as { error?: string }
      if (body.error) message = body.error
    } catch {
      // Keep the HTTP status when the response is not JSON.
    }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

export async function getBuilds(signal?: AbortSignal): Promise<BuildSummary[]> {
  const body = await getJson<{ builds: BuildSummary[] }>('/api/builds', signal)
  return body.builds
}

export function getBuild(buildId: string, signal?: AbortSignal): Promise<BuildStatus> {
  return getJson<BuildStatus>(`/api/builds/${encodeURIComponent(buildId)}`, signal)
}

export async function getArtifacts(buildId: string, signal?: AbortSignal): Promise<ArtifactItem[]> {
  const body = await getJson<{ artifacts: ArtifactItem[] }>(
    `/api/builds/${encodeURIComponent(buildId)}/artifacts`,
    signal,
  )
  return body.artifacts
}

export function getLog(buildId: string, job: string, signal?: AbortSignal): Promise<LogSnapshot> {
  return getJson<LogSnapshot>(
    `/api/builds/${encodeURIComponent(buildId)}/logs/${encodeURIComponent(job)}`,
    signal,
  )
}
