import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import ProjectCanvas from './ProjectCanvas.svelte';
import type { Project, RunSummary } from './types';

const project: Project = {
  id: 'project-1',
  slug: 'canvas',
  owner: 'werft',
  repo: 'werft/canvas',
  lifecycle: 'active',
  onboarded_at: null,
  created_at: '2026-01-01T00:00:00Z',
};

function run(index: number): RunSummary {
  return {
    id: `run-${index}`,
    project_slug: project.slug,
    status: 'running',
    issue_number: index,
    issue_title: `Task ${index}`,
    attempt_count: 1,
    max_attempts: 3,
    latest_outcome: null,
    parked_reason: null,
    pr_number: null,
    pr_url: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  };
}

const callbacks = {
  onproject: vi.fn(),
  onrun: vi.fn(),
  onnewtask: vi.fn(),
  onnewproject: vi.fn(),
  onrefresh: vi.fn(),
};

describe('ProjectCanvas', () => {
  it('places seven active task nodes at distinct canvas coordinates', () => {
    const { container } = render(ProjectCanvas, {
      props: {
        projects: [project],
        runs: Array.from({ length: 7 }, (_, index) => run(index + 1)),
        activity: null,
        demo: false,
        error: '',
        loading: false,
        project,
        ...callbacks,
      },
    });

    const nodes = [...container.querySelectorAll<HTMLElement>('[data-run-id]')];
    expect(nodes).toHaveLength(7);
    expect(new Set(nodes.map((node) => node.getAttribute('style'))).size).toBe(7);
    expect(nodes[0].getAttribute('style')).toContain('top: 280px');
    expect(nodes[6].getAttribute('style')).toContain('top: 720px');
  });

  it('explains the absence of sessions without inventing an idle agent', () => {
    const { getByText, queryByText } = render(ProjectCanvas, {
      props: {
        projects: [project],
        runs: [],
        activity: null,
        demo: false,
        error: '',
        loading: false,
        project,
        ...callbacks,
      },
    });

    expect(getByText('No active sessions')).toBeTruthy();
    expect(getByText('Agents start when an approved task is ready.')).toBeTruthy();
    expect(queryByText('Idle agent')).toBeNull();
  });
});
