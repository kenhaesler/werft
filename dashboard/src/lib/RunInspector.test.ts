import { it, expect, vi, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import RunInspector from './RunInspector.svelte';
import { demoRuns, demoDetail } from './demo';
import { previewActivity } from './activity';
import { milestoneTitle } from './run-presentation';

afterEach(() => vi.unstubAllGlobals());

it('does not present stale or unavailable monitoring as a live attended session', async () => {
  const run = demoRuns[0];
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async () => new Response(JSON.stringify(demoDetail(run)))),
  );
  const activity = previewActivity([run]);
  const props = {
    run,
    activity,
    demo: false,
    busy: false,
    activityFetchedAt: Date.now(),
    onclose: vi.fn(),
    onaction: vi.fn(),
  };
  const screen = render(RunInspector, { props });
  await waitFor(() =>
    expect(screen.queryByText('Manager is monitoring this session')).not.toBeNull(),
  );
  await screen.rerender({ ...props, activityFetchedAt: Date.now() - 15_000 });
  await waitFor(() =>
    expect(screen.queryByText('Live session monitoring unavailable')).not.toBeNull(),
  );
  expect(screen.queryByText('Manager is monitoring this session')).toBeNull();
  await screen.rerender({
    ...props,
    activity: { ...activity, manager: { ...activity.manager, live_driver_run_ids: [] } },
  });
  await waitFor(() =>
    expect(screen.queryByText('Manager is not currently monitoring this session')).not.toBeNull(),
  );
});

it('reads real database status transitions and dispatch phases as milestones', () => {
  const base = {
    id: 1,
    created_at: '',
    event_type: 'status_changed',
    payload: { from: 'running', to: 'awaiting_review' },
  };
  expect(milestoneTitle(base)).toBe('Status changed to needs review');
  expect(
    milestoneTitle({ ...base, event_type: 'dispatch', payload: { phase: 'container_started' } }),
  ).toBe('Agent environment started');
});
