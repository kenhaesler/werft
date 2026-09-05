import { describe, expect, it } from 'vitest';
import { fireEvent, render } from '@testing-library/svelte';
import QuotaPanel from './QuotaPanel.svelte';
import type { QuotaAccount } from './types';

const account: QuotaAccount = {
  provider: 'claude',
  label: 'primary',
  ceiling_seconds: 100,
  consumed_seconds: 0,
  reserved_seconds: 0,
  headroom_seconds: 100,
  exhausted_until: '2020-01-01T00:00:00Z',
  exhausted_source: null,
  last_reading_utilization: null,
  last_reading_source: null,
  last_reading_at: null,
};

describe('QuotaPanel diagnostics', () => {
  it('distinguishes zero ledger values and unavailable provider observations', async () => {
    const screen = render(QuotaPanel, { props: { accounts: [account] } });
    await fireEvent.click(screen.getByText('Provider diagnostics'));

    expect(screen.getByText('0%')).toBeTruthy();
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText(/Ended/)).toBeTruthy();
    expect(screen.queryByText('No exhaustion recorded')).toBeNull();
  });
});
