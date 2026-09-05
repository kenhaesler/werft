import { afterEach, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import LiveSession from './LiveSession.svelte';

afterEach(() => vi.unstubAllGlobals());

it('shows raw output as inert text and exposes reported runtime facts', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes('/runtime'))
        return new Response(
          JSON.stringify({
            generated_at: '2026-01-01T00:00:00Z',
            manager_available: true,
            attended: true,
            attempt_no: 4,
            run: {
              run_id: 'run-1',
              status: 'running',
              provider: 'claude',
              container_id: 'runner-1',
              attempt_started_at: null,
              last_heartbeat_at: null,
              lease_expires_at: null,
              hard_deadline_at: null,
              next_attempt_at: null,
              parked_reason: null,
              updated_at: '2026-01-01T00:00:00Z',
            },
          }),
        );
      return new Response(
        JSON.stringify({
          available: true,
          reason: null,
          content: '<img src=x onerror=alert(1)>',
          next_offset: 27,
          generation: 'g1',
          reset: true,
          truncated: false,
          has_more: false,
        }),
      );
    }),
  );
  const screen = render(LiveSession, { props: { runId: 'run-1' } });
  await waitFor(() => expect(screen.getByText('<img src=x onerror=alert(1)>')).not.toBeNull());
  expect(document.querySelector('img')).toBeNull();
  await screen.getByText('Runtime facts').click();
  expect(screen.getByText('Attending this session')).not.toBeNull();
  expect(screen.getByText('#4')).not.toBeNull();
});

it('keeps received output when the backend reports it unavailable', async () => {
  let logCall = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request) => {
      if (String(input).includes('/runtime'))
        return new Response(
          JSON.stringify({
            generated_at: '',
            manager_available: false,
            attended: false,
            attempt_no: null,
            run: null,
          }),
        );
      logCall++;
      return new Response(
        JSON.stringify(
          logCall === 1
            ? {
                available: true,
                reason: null,
                content: 'retained text',
                next_offset: 13,
                generation: 'g1',
                reset: true,
                truncated: false,
                has_more: false,
              }
            : {
                available: false,
                reason: 'unsafe_output',
                content: '',
                next_offset: null,
                generation: 'g1',
                reset: false,
                truncated: false,
                has_more: false,
              },
        ),
      );
    }),
  );
  const screen = render(LiveSession, { props: { runId: 'run-1' } });
  await waitFor(() => expect(screen.getByText('retained text')).not.toBeNull());
  await new Promise((resolve) => setTimeout(resolve, 2_100));
  await waitFor(() =>
    expect(screen.getByText(/Output is unavailable because it was marked unsafe/)).not.toBeNull(),
  );
  expect(screen.getByText('retained text')).not.toBeNull();
});

it('serializes visibility wakes and resets cursors and output for a different run', async () => {
  let resolveRuntime!: (response: Response) => void;
  let resolveLog!: (response: Response) => void;
  const fetchMock = vi.fn((input: string | URL | Request) => {
    const pending = new Promise<Response>((resolve) => {
      if (String(input).includes('/runtime')) resolveRuntime = resolve;
      else resolveLog = resolve;
    });
    return pending;
  });
  vi.stubGlobal('fetch', fetchMock);
  const screen = render(LiveSession, { props: { runId: 'run-1' } });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  document.dispatchEvent(new Event('visibilitychange'));
  expect(fetchMock).toHaveBeenCalledTimes(2);
  resolveRuntime(
    new Response(
      JSON.stringify({
        generated_at: '',
        manager_available: true,
        attended: true,
        attempt_no: 1,
        run: null,
      }),
    ),
  );
  resolveLog(
    new Response(
      JSON.stringify({
        available: true,
        reason: null,
        content: 'run one',
        next_offset: 7,
        generation: 'one',
        reset: true,
        truncated: false,
        has_more: false,
      }),
    ),
  );
  await waitFor(() => expect(screen.getByText('run one')).not.toBeNull());
  await screen.rerender({ runId: 'run-2' });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
  expect(screen.queryByText('run one')).toBeNull();
  const calls = fetchMock.mock.calls.slice(2).map(([input]) => String(input));
  expect(calls.some((url) => url.includes('run-2/log') && !url.includes('offset='))).toBe(true);
});
