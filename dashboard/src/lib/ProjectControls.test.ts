import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';
import ProjectControls from './ProjectControls.svelte';
import type { Project } from './types';

const project: Project = {
  id: 'project-7',
  slug: 'search',
  owner: 'werft-labs',
  repo: 'search',
  lifecycle: 'bootstrap',
  onboarded_at: '2026-08-01T10:00:00Z',
  created_at: '2026-07-01T10:00:00Z',
};

afterEach(() => vi.unstubAllGlobals());

describe('ProjectControls', () => {
  it('renders the native details drilldown and repository link', async () => {
    const screen = render(ProjectControls, { props: { project, onupdated: vi.fn() } });
    const disclosure = screen.container.querySelector('details')!;
    expect(disclosure.hasAttribute('open')).toBe(false);
    expect(screen.getByText('Project settings')).toBeTruthy();
    expect(screen.getAllByText('Bootstrap').length).toBe(2);
    expect(screen.getByRole('link', { name: /open on github/i }).getAttribute('href')).toBe(
      'https://github.com/werft-labs/search',
    );
    await fireEvent.click(screen.getByText('Project settings'));
    expect(screen.getAllByText(/2026/).length).toBe(2);
  });

  it('posts the selected lifecycle and returns the refreshed project', async () => {
    const updated = { ...project, lifecycle: 'oracle_gated' };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(updated), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const onupdated = vi.fn();
    const screen = render(ProjectControls, { props: { project, onupdated } });

    await fireEvent.click(screen.getByRole('button', { name: 'Set oracle-gated' }));
    await waitFor(() => expect(onupdated).toHaveBeenCalledWith(updated));
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/projects/project-7/flip',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ to: 'oracle_gated' }),
      }),
    );
  });

  it('labels demo repairs as sample-only and updates through the callback without a request', async () => {
    const onupdated = vi.fn();
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const screen = render(ProjectControls, { props: { project, demo: true, onupdated } });

    await fireEvent.click(screen.getByText('Project settings'));
    expect(screen.getByText(/SAMPLE ONLY/)).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Set oracle-gated' }));
    await waitFor(() =>
      expect(onupdated).toHaveBeenCalledWith({ ...project, lifecycle: 'oracle_gated' }),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows authorization and conflict details with a refresh recovery action', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: 'stale project' }), { status: 409 }),
      );
    vi.stubGlobal('fetch', fetchMock);
    const onrefresh = vi.fn();
    const screen = render(ProjectControls, { props: { project, onupdated: vi.fn(), onrefresh } });

    await fireEvent.click(screen.getByText('Project settings'));
    await fireEvent.click(screen.getByRole('button', { name: 'Set oracle-gated' }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
    expect(screen.getByText('Project changed')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh project' }));
    expect(onrefresh).toHaveBeenCalledOnce();
  });

  it('loads paginated project history only for a live expanded project', async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/events?'))
        return new Response(
          JSON.stringify({
            total: 11,
            events: [
              {
                id: 1,
                event_type: 'lifecycle_flipped',
                payload: { to: 'oracle_gated' },
                created_at: '2026-08-02T10:00:00Z',
              },
            ],
          }),
        );
      return new Response(JSON.stringify({}), { status: 500 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const screen = render(ProjectControls, { props: { project, onupdated: vi.fn() } });
    await fireEvent.click(screen.getByText('Project settings'));
    await fireEvent.click(screen.getByText('Lifecycle history'));
    await waitFor(() => expect(screen.getByText('lifecycle flipped')).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/projects/project-7/events?limit=10&offset=0'),
      expect.anything(),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('offset=10'),
        expect.anything(),
      ),
    );
  });
});
