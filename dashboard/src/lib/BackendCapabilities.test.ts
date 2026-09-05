import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';
import BackendCapabilities from './BackendCapabilities.svelte';

afterEach(() => vi.unstubAllGlobals());

it('separates configured and schema validated capability states and shows dispatch limits', async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        capabilities: { github: true, docker: false },
        readiness: { github: 'configured', docker: 'unconfigured' },
        dispatch: [
          {
            project: 'alpha',
            configured: true,
            schema_validated: true,
            provider: 'claude',
            model: 'sonnet',
            image_digest: 'sha256:abc',
            timeout_seconds: 3600,
            memory_bytes: 268435456,
            nano_cpus: 1000000000,
            registries: ['ghcr.io'],
            extra_hosts: [],
            egress_hosts: ['github.com'],
            mode: 'bootstrap',
          },
        ],
      }),
    ),
  );
  vi.stubGlobal('fetch', fetchMock);
  const screen = render(BackendCapabilities);
  await waitFor(() => expect(screen.getByText('Configured', { exact: true })).toBeTruthy());
  expect(screen.getByText('GitHub')).toBeTruthy();
  await fireEvent.click(screen.getByText('alpha'));
  expect(screen.getByText('Schema validated')).toBeTruthy();
  expect(screen.getByText(/256 MiB/)).toBeTruthy();
  expect(screen.getByText('github.com')).toBeTruthy();
  expect(screen.getByText('1.00 vCPU')).toBeTruthy();
});

it('offers a retry action when capabilities cannot be read', async () => {
  const fetchMock = vi.fn().mockRejectedValue(new Error('Manager offline'));
  vi.stubGlobal('fetch', fetchMock);
  const screen = render(BackendCapabilities);
  await waitFor(() => expect(screen.getByText('Manager offline')).toBeTruthy());
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
});

it('keeps demo capabilities explicitly unavailable until a manager is connected', () => {
  const screen = render(BackendCapabilities, { props: { demo: true } });
  expect(screen.getByText('SAMPLE ONLY')).toBeTruthy();
  expect(
    screen.getByText('Connect a manager to inspect its configured integrations.'),
  ).toBeTruthy();
});
