<script lang="ts">
  import type { QuotaAccount } from './types';

  interface Props {
    accounts: QuotaAccount[];
  }

  let { accounts }: Props = $props();

  // The operator should never have to do arithmetic in their head: render
  // seconds as h:mm rather than raw seconds.
  function formatDuration(seconds: number | null | undefined): string {
    if (seconds == null || Number.isNaN(seconds)) return '—';
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    return `${hours}:${String(minutes).padStart(2, '0')}`;
  }
</script>

<section class="quota-strip">
  {#each accounts as account (account.provider + '/' + account.label)}
    <div class="quota-card">
      <h3 class="quota-card__title">{account.provider} / {account.label}</h3>
      <dl class="quota-card__figures">
        <dt>Consumed</dt>
        <dd>{formatDuration(account.consumed_seconds)}</dd>
        <dt>Reserved</dt>
        <dd>{formatDuration(account.reserved_seconds)}</dd>
        <dt>Ceiling</dt>
        <dd>{formatDuration(account.ceiling_seconds)}</dd>
        <dt>Headroom</dt>
        <dd>{formatDuration(account.headroom_seconds)}</dd>
      </dl>
      {#if account.exhausted_until}
        <p class="quota-card__exhausted">
          Exhausted until {account.exhausted_until}
          {#if account.exhausted_source}(source: {account.exhausted_source}){/if}
        </p>
      {/if}
      {#if account.last_reading_at}
        <p class="quota-card__reading">
          Last reading: {account.last_reading_utilization ?? '—'}
          {#if account.last_reading_source}via {account.last_reading_source}{/if}
          at {account.last_reading_at}
        </p>
      {/if}
    </div>
  {/each}
</section>
