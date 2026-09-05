import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';
import EventHistory, { type EventItem } from './EventHistory.svelte';

const event: EventItem = {
  id: 1,
  run_id: 'run-1',
  project_slug: 'demo',
  issue_number: 12,
  issue_title: 'Fix queue',
  run_status: 'running',
  event_type: 'status_changed',
  phase: null,
  from_status: 'queued',
  to_status: 'running',
  created_at: '2026-01-01T12:00:00Z',
  payload: { from: 'queued', to: 'running' },
};

describe('EventHistory', () => {
  it('renders snapshot events with payload disclosure and task drilldown', async () => {
    const ontask = vi.fn();
    const screen = render(EventHistory, { props: { demo: true, events: [event], ontask } });

    expect(screen.getByText('Fix queue')).toBeTruthy();
    await fireEvent.click(screen.getByText(/2026/));
    expect(screen.getByText(/"from": "queued"/)).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: /open task/i }));
    expect(ontask).toHaveBeenCalledWith('run-1');
  });

  it('shows a clear empty state for a snapshot with no events', () => {
    const screen = render(EventHistory, { props: { demo: true, events: [] } });
    expect(screen.getByText('No recorded task events yet.')).toBeTruthy();
  });

  it('requests six events per page and preserves search then pagination order live', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ total: 7, events: [event] }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ total: 7, events: [event] }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ total: 7, events: [] }), { status: 200 }),
      );
    vi.stubGlobal('fetch', fetchMock);
    const screen = render(EventHistory, { props: { demo: false } });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const input = screen.getByRole('textbox', { name: 'Search event history' });
    await fireEvent.input(input, { target: { value: 'queue' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await fireEvent.click(screen.getByRole('button', { name: 'Next events' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/events?limit=6&offset=0',
      '/api/v1/events?limit=6&offset=0&q=queue',
      '/api/v1/events?limit=6&offset=6&q=queue',
    ]);
  });

  it('ignores an older live response after a newer search request starts', async () => {
    let resolveFirst: (value: Response) => void = () => {};
    let resolveSecond: (value: Response) => void = () => {};
    const first = new Promise<Response>((resolve) => (resolveFirst = resolve));
    const second = new Promise<Response>((resolve) => (resolveSecond = resolve));
    const fetchMock = vi.fn().mockReturnValueOnce(first).mockReturnValueOnce(second);
    vi.stubGlobal('fetch', fetchMock);
    const screen = render(EventHistory, { props: { demo: false } });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Search event history' }), {
      target: { value: 'new' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    resolveSecond(
      new Response(
        JSON.stringify({ total: 1, events: [{ ...event, issue_title: 'New result' }] }),
        { status: 200 },
      ),
    );
    await waitFor(() => expect(screen.getByText('New result')).toBeTruthy());
    resolveFirst(
      new Response(
        JSON.stringify({ total: 1, events: [{ ...event, issue_title: 'Old result' }] }),
        { status: 200 },
      ),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText('Old result')).toBeNull();
  });
});
