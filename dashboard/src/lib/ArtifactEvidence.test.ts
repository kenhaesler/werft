import { expect, it, vi, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import ArtifactEvidence from './ArtifactEvidence.svelte';

afterEach(() => vi.unstubAllGlobals());

it('renders hostile artifact contents as text and ignores a stale preview request', async () => {
  let resolveFirst!: (value: Response) => void;
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockImplementationOnce(
        async () => new Response('<img src=x onerror="window.pwned=1">second'),
      ),
  );
  const artifacts = [
    {
      path: 'outputs/first.log',
      bytes: 20,
      collected_at: '2026-01-01T00:00:00Z',
      content_hash: null,
    },
    {
      path: 'outputs/second.log',
      bytes: 20,
      collected_at: '2026-01-01T00:00:00Z',
      content_hash: null,
    },
  ];
  const screen = render(ArtifactEvidence, {
    props: { runId: 'run-1', artifacts, ondownload: vi.fn() },
  });
  await screen.getByRole('button', { name: 'Preview outputs/first.log' }).click();
  await screen.rerender({ runId: 'run-2', artifacts: [artifacts[1]], ondownload: vi.fn() });
  resolveFirst(new Response('stale first'));
  await waitFor(() => expect(screen.queryByText('Loading safe text preview…')).toBeNull());
  await screen.getByRole('button', { name: 'Preview outputs/second.log' }).click();
  await waitFor(() =>
    expect(screen.getByText('<img src=x onerror="window.pwned=1">second')).not.toBeNull(),
  );
  expect(document.querySelector('img')).toBeNull();
  expect(screen.queryByText('stale first')).toBeNull();
});
