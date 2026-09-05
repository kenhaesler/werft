import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';
import MachinePanel from './MachinePanel.svelte';
import type { Machine } from './types';

const machine: Machine = {
  name: 'werft-host',
  os: 'Ubuntu 24.04 LTS',
  architecture: 'x86_64',
  engine_version: '29.6.2',
  cpus: 8,
  memory_bytes: 16 * 1024 ** 3,
  max_concurrent_runs: 4,
  containers: [
    {
      id: 'container-full-id-123456789',
      run_id: 'run-outside-loaded-page',
      name: 'agent-run',
      image: 'werft/agent:latest',
      state: 'running',
      status: 'Up 2 minutes',
    },
  ],
};

describe('MachinePanel task drilldown', () => {
  it('offers inspection for a container even when its run is absent from loaded runs', async () => {
    const oninspect = vi.fn().mockResolvedValue(undefined);
    const screen = render(MachinePanel, { props: { machine, oninspect } });

    await fireEvent.click(screen.getByRole('button', { name: 'Inspect' }));
    await waitFor(() => expect(oninspect).toHaveBeenCalledWith('run-outside-loaded-page'));
  });

  it('shows a failure and permits retrying direct task inspection', async () => {
    const oninspect = vi
      .fn()
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce(undefined);
    const screen = render(MachinePanel, { props: { machine, oninspect } });
    const inspect = screen.getByRole('button', { name: 'Inspect' });

    await fireEvent.click(inspect);
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toContain('could not be loaded'),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Inspect' }));
    await waitFor(() => expect(oninspect).toHaveBeenCalledTimes(2));
  });
});
