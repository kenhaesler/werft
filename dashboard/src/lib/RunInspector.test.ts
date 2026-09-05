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

it('keeps lifetime session numbering separate from the current failure budget and loads artifact hashes', async () => {
  const run = { ...demoRuns[0], attempt_count: 1, max_attempts: 3 };
  const detail = {
    ...demoDetail(run),
    attempts: [{ ...demoDetail(run).attempts[0], attempt_no: 7 }],
    artifacts: [{ path: 'outputs/run.log', bytes: 22, collected_at: run.updated_at }],
    result: {
      result_json: {
        status: 'success',
        commit_sha: 'abc123',
        pushed: true,
        duration_seconds: 42,
        started_at: run.created_at,
        ended_at: run.updated_at,
        error: null,
      },
      problem: 'No remaining review blockers.',
      usage: {
        input_tokens: 120,
        output_tokens: 34,
        cache_creation_input_tokens: 7,
        cache_read_input_tokens: 9,
        total_cost_usd: null,
      },
      exit_meaning: 'agent completed successfully',
    },
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.endsWith('/artifacts')) {
        return new Response(
          JSON.stringify({
            artifacts: [{ ...detail.artifacts[0], content_hash: 'sha256:verified' }],
          }),
        );
      }
      return new Response(JSON.stringify(detail));
    }),
  );
  const screen = render(RunInspector, {
    props: { run, demo: false, busy: false, onclose: vi.fn(), onaction: vi.fn() },
  });
  await waitFor(() => expect(screen.getByText('Failure budget: 1 of 3 used.')).not.toBeNull());
  expect(screen.getByText('Current session #7')).not.toBeNull();
  await screen.getByRole('button', { name: /Evidence/ }).click();
  await waitFor(() => expect(screen.getByText('sha256:verified')).not.toBeNull());
  await screen.getByRole('button', { name: 'Timeline' }).click();
  expect(screen.getByText('agent completed successfully')).not.toBeNull();
  expect(screen.getByText('120 input tokens')).not.toBeNull();
  expect(screen.getByText('7 cache-write tokens')).not.toBeNull();
  expect(screen.queryByText('reported cost')).toBeNull();
});

it('immediately presents an authoritative action summary while retaining loaded detail', async () => {
  const run = demoRuns[0];
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(demoDetail(run)))),
  );
  const props = { run, demo: false, busy: false, onclose: vi.fn(), onaction: vi.fn() };
  const screen = render(RunInspector, { props });
  await waitFor(() => expect(screen.getByText('Agent session in progress')).not.toBeNull());
  await screen.rerender({
    ...props,
    run: {
      ...run,
      status: 'awaiting_review',
      updated_at: new Date(Date.now() + 1_000).toISOString(),
    },
  });
  await waitFor(() => expect(screen.getByText('Ready for your review')).not.toBeNull());
  await screen.getByRole('button', { name: 'Timeline' }).click();
  expect(screen.getByText('Agent environment started')).not.toBeNull();
});
