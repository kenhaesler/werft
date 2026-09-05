import { api } from './api';
import type { RunSummary, RunsResponse, RunStatus } from './types';

/** States shown on the canvas. Terminal runs are intentionally excluded. */
export const CANVAS_STATUSES: readonly RunStatus[] = [
  'queued',
  'claimed',
  'running',
  'awaiting_ci',
  'awaiting_review',
  'merging',
  'blocked_quota',
  'failed',
  'parked',
];

const PAGE_SIZE = 200;

function pageUrl(offset: number): string {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
  for (const status of CANVAS_STATUSES) query.append('statuses', status);
  return `/runs?${query.toString()}`;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

/**
 * Load a complete, authenticated canvas snapshot.
 *
 * A partial result is never returned: any request failure, malformed total,
 * or pagination stall rejects the whole operation so callers keep their
 * previous snapshot and can report the refresh failure.
 */
export async function loadCanvasRuns(signal: AbortSignal): Promise<RunSummary[]> {
  const runsById = new Map<string, RunSummary>();
  let offset = 0;
  let total: number | undefined;

  while (total === undefined || offset < total) {
    if (signal.aborted) throw new DOMException('The canvas refresh was aborted.', 'AbortError');

    let page: RunsResponse;
    try {
      page = await api<RunsResponse>(pageUrl(offset), { signal });
    } catch (error) {
      // Preserve the platform abort error (and its identity) for callers.
      if (signal.aborted || isAbortError(error)) throw error;
      throw error;
    }

    if (!Number.isInteger(page.total) || page.total < 0 || !Array.isArray(page.runs)) {
      throw new Error('The runs response did not contain a valid complete snapshot.');
    }
    if (total === undefined) total = page.total;
    else if (page.total !== total) {
      throw new Error('The runs total changed during pagination; snapshot is incomplete.');
    }

    if (total === 0) return [];

    const before = runsById.size;
    for (const run of page.runs) {
      if (run && typeof run.id === 'string') runsById.set(run.id, run);
    }

    // Advancing without seeing a new ID can otherwise loop forever against a
    // broken or unstable endpoint while making the result look complete.
    if (runsById.size === before) {
      throw new Error('The runs endpoint made no pagination progress; snapshot is incomplete.');
    }

    offset += page.runs.length;
    if (page.runs.length === 0 || offset <= 0) {
      throw new Error('The runs endpoint returned an incomplete page.');
    }
  }

  if (runsById.size < total) {
    throw new Error('The runs endpoint returned an incomplete snapshot.');
  }
  return [...runsById.values()];
}
