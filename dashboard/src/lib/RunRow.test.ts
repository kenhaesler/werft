import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';
import RunRow from './RunRow.svelte';
import { setToken } from './api';
import type { RunSummary } from './types';

function makeRun(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 'abc-123',
    project_slug: 'demo-project',
    status: 'awaiting_review',
    issue_number: 42,
    issue_title: 'Fix the thing',
    attempt_count: 1,
    max_attempts: 3,
    latest_outcome: null,
    parked_reason: null,
    pr_number: null,
    pr_url: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

describe('RunRow', () => {
  it('renders hostile run fields as inert text, never as markup ({@html} is banned)', () => {
    const run = makeRun({
      issue_title: '<script>alert(1)</script>',
      parked_reason: '<img src=x onerror=alert(1)>',
    });

    const { container } = render(RunRow, { props: { run } });

    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('<script>alert(1)</script>');
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('shows accept and reject on an awaiting_review row and fires the matching callback', async () => {
    const onAccept = vi.fn();
    const onReject = vi.fn();
    const run = makeRun({ status: 'awaiting_review' });

    const { getByText } = render(RunRow, {
      props: { run, onAccept, onReject },
    });

    await fireEvent.click(getByText('Accept'));
    await fireEvent.click(getByText('Reject'));

    expect(onAccept).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it('shows no action buttons on a merged (terminal) row', () => {
    const run = makeRun({ status: 'merged' });
    const { queryByText } = render(RunRow, { props: { run } });

    expect(queryByText('Accept')).toBeNull();
    expect(queryByText('Reject')).toBeNull();
    expect(queryByText('Cancel')).toBeNull();
    expect(queryByText('Requeue')).toBeNull();
  });

  it('shows cancel (not requeue) on a non-terminal, non-review row such as running', () => {
    const run = makeRun({ status: 'running' });
    const { queryByText } = render(RunRow, { props: { run } });

    expect(queryByText('Cancel')).not.toBeNull();
    expect(queryByText('Requeue')).toBeNull();
    expect(queryByText('Accept')).toBeNull();
  });

  it('shows requeue on a parked row and fires its callback', async () => {
    const onRequeue = vi.fn();
    const run = makeRun({ status: 'parked', parked_reason: 'quota exhausted' });
    const { getByText } = render(RunRow, { props: { run, onRequeue } });

    await fireEvent.click(getByText('Requeue'));

    expect(onRequeue).toHaveBeenCalledTimes(1);
  });
});

describe('RunRow artifacts (B4/B7: no tokens in URLs — fetched, not linked)', () => {
  beforeEach(() => {
    localStorage.clear();
    setToken('secret-token');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fetches the artifact listing through the bearer-attaching api() helper, never a bare href', () => {
    const run = makeRun();
    const { container } = render(RunRow, { props: { run } });

    // No `<a href>` to the artifacts endpoint anywhere in the row — that
    // would 401 (the endpoint requires the bearer header) and, if it ever
    // "worked", would mean a token in a URL.
    const artifactLinks = Array.from(container.querySelectorAll('a')).filter((a) =>
      a.getAttribute('href')?.includes('/artifacts'),
    );
    expect(artifactLinks).toHaveLength(0);
  });

  it('carries the Authorization header on the artifacts request and renders hostile paths as inert text', async () => {
    const run = makeRun();
    const hostilePath = '<script>alert(1)</script>';
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          artifacts: [
            {
              path: hostilePath,
              bytes: 12,
              collected_at: '2026-01-01T00:00:00Z',
              content_hash: null,
            },
          ],
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const { getByText, container } = render(RunRow, { props: { run } });

    await fireEvent.click(getByText('Artifacts'));
    await waitFor(() => expect(container.textContent).toContain(hostilePath));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/v1/runs/${run.id}/artifacts`);
    const headers = new Headers(init.headers);
    expect(headers.get('Authorization')).toBe('Bearer secret-token');

    expect(container.querySelector('script')).toBeNull();
  });

  it('shows a typed failure message rather than throwing when the request fails', async () => {
    const run = makeRun();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })));

    const { getByText, container } = render(RunRow, { props: { run } });

    await fireEvent.click(getByText('Artifacts'));
    await waitFor(() => expect(container.textContent).toContain('failed to load (401)'));
  });
});
