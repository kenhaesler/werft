import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import RunRow from './RunRow.svelte';
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
