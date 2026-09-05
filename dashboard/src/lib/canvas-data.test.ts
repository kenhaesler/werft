import { beforeEach, describe, expect, it, vi } from 'vitest';
import { loadCanvasRuns } from './canvas-data';
import { api } from './api';
import type { RunSummary } from './types';

vi.mock('./api', () => ({ api: vi.fn() }));

const apiMock = vi.mocked(api);

function run(id: string, status: RunSummary['status'] = 'running'): RunSummary {
  return {
    id,
    project_slug: 'demo',
    status,
    issue_number: Number(id.replace(/\D/g, '')) || 1,
    issue_title: `Issue ${id}`,
    attempt_count: 1,
    max_attempts: 3,
    latest_outcome: null,
    parked_reason: null,
    pr_number: null,
    pr_url: null,
    created_at: '2026-09-05T00:00:00Z',
    updated_at: '2026-09-05T00:00:00Z',
  };
}

describe('loadCanvasRuns', () => {
  beforeEach(() => apiMock.mockReset());

  it('loads all pages, sends every non-terminal status, and deduplicates IDs', async () => {
    const first = [run('run-1'), run('run-2', 'failed')];
    const second = [run('run-2', 'failed'), run('run-3', 'parked')];
    apiMock.mockResolvedValueOnce({ runs: first, total: 3 });
    apiMock.mockResolvedValueOnce({ runs: second, total: 3 });

    const result = await loadCanvasRuns(new AbortController().signal);

    expect(result.map((item) => item.id)).toEqual(['run-1', 'run-2', 'run-3']);
    expect(apiMock).toHaveBeenCalledTimes(2);
    const firstUrl = String(apiMock.mock.calls[0][0]);
    expect(firstUrl).toContain('limit=200');
    expect(firstUrl).toContain('offset=0');
    expect(firstUrl.match(/statuses=/g)).toHaveLength(9);
    expect(String(apiMock.mock.calls[1][0])).toContain('offset=2');
    expect(apiMock.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
  });

  it('rejects on a later page instead of returning a partial snapshot', async () => {
    apiMock.mockResolvedValueOnce({ runs: [run('run-1')], total: 2 });
    const failure = new Error('network down');
    apiMock.mockRejectedValueOnce(failure);

    await expect(loadCanvasRuns(new AbortController().signal)).rejects.toBe(failure);
    expect(apiMock).toHaveBeenCalledTimes(2);
  });

  it('honors abort before making a request', async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(loadCanvasRuns(controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
    expect(apiMock).not.toHaveBeenCalled();
  });

  it('rejects when pagination makes no progress', async () => {
    apiMock.mockResolvedValue({ runs: [run('run-1')], total: 3 });

    await expect(loadCanvasRuns(new AbortController().signal)).rejects.toThrow(
      /no pagination progress/,
    );
    expect(apiMock).toHaveBeenCalledTimes(2);
  });
});
