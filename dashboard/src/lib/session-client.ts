import { api } from './api';

export interface SessionRuntime {
  generated_at: string;
  manager_available: boolean;
  attended: boolean;
  attempt_no: number | null;
  run: {
    run_id: string;
    status: string;
    provider: string | null;
    container_id: string | null;
    attempt_started_at: string | null;
    last_heartbeat_at: string | null;
    lease_expires_at: string | null;
    hard_deadline_at: string | null;
    next_attempt_at: string | null;
    parked_reason: string | null;
    updated_at: string;
  } | null;
}

export interface SessionLogChunk {
  available: boolean;
  reason: 'output_not_available' | 'unsafe_output' | null;
  content: string;
  next_offset: number | null;
  generation: string | null;
  reset: boolean;
  truncated: boolean;
  has_more: boolean;
}

export const getSessionRuntime = (runId: string, signal: AbortSignal) =>
  api<SessionRuntime>(`/runs/${encodeURIComponent(runId)}/runtime`, { signal });

export function getSessionLog(
  runId: string,
  cursor: { offset?: number; generation?: string },
  signal: AbortSignal,
) {
  const query = new URLSearchParams();
  if (cursor.offset !== undefined) query.set('offset', String(cursor.offset));
  if (cursor.generation) query.set('generation', cursor.generation);
  const suffix = query.size ? `?${query}` : '';
  return api<SessionLogChunk>(`/runs/${encodeURIComponent(runId)}/log${suffix}`, { signal });
}
