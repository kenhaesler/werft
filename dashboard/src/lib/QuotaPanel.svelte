<script lang="ts">
  import Icon from './Icon.svelte';
  import { duration, percent, relativeTime } from './format';
  import type { QuotaAccount } from './types';
  let { accounts, compact = false }: { accounts: QuotaAccount[]; compact?: boolean } = $props();

  function reading(value: number | null): string {
    if (value == null || Number.isNaN(value)) return 'Unavailable';
    return `${Math.round(value <= 1 ? value * 100 : value)}%`;
  }

  function timestamp(value: string | null): string {
    return value ? new Date(value).toLocaleString() : 'Unavailable';
  }

  function exhaustion(value: string | null): string {
    if (!value) return 'No exhaustion recorded';
    const date = new Date(value);
    return date.getTime() <= Date.now()
      ? `Ended ${date.toLocaleString()}`
      : `Until ${date.toLocaleString()}`;
  }
</script>

<section class="quota-panel" class:compact>
  <div class="section-heading">
    <h2>Provider quota</h2>
    <Icon name="quota" size={17} />
  </div>
  {#each accounts as account (`${account.provider}-${account.label}`)}
    <div class="quota-account">
      <div class="provider-heading">
        <span class="provider-symbol"><Icon name="spark" size={20} /></span>
        <div>
          <strong>{account.provider === 'claude' ? 'Claude' : account.provider}</strong><small
            >{account.label}</small
          >
        </div>
        <strong class="quota-percentage"
          >{Math.round(percent(account.consumed_seconds, account.ceiling_seconds))}<span>%</span
          ></strong
        >
      </div>
      <div
        class="quota-track"
        role="meter"
        aria-label={`${account.provider} used quota`}
        aria-valuenow={account.consumed_seconds}
        aria-valuemin={0}
        aria-valuemax={Math.max(account.ceiling_seconds, account.consumed_seconds, 1)}
      >
        <span
          class="consumed"
          style:width={`${percent(account.consumed_seconds, account.ceiling_seconds)}%`}
        ></span><span
          class="reserved"
          style:width={`${Math.min(percent(account.reserved_seconds, account.ceiling_seconds), 100 - percent(account.consumed_seconds, account.ceiling_seconds))}%`}
        ></span>
      </div>
      <div class="quota-legend">
        <span><i></i>{duration(account.consumed_seconds)} used</span><span
          ><i></i>{duration(account.reserved_seconds)} reserved</span
        >
      </div>
      <div class="quota-headroom">
        <span>{account.exhausted_until ? 'Resumes' : 'Available headroom'}</span><strong
          >{account.exhausted_until
            ? new Date(account.exhausted_until).toLocaleString()
            : duration(account.headroom_seconds)}</strong
        >
      </div>
      <details class="quota-details">
        <summary>Provider diagnostics</summary>
        <dl>
          <div>
            <dt>Ledger utilization</dt>
            <dd>{reading(percent(account.consumed_seconds, account.ceiling_seconds))}</dd>
          </div>
          <div>
            <dt>Provider reading</dt>
            <dd>{reading(account.last_reading_utilization)}</dd>
          </div>
          <div>
            <dt>Reading source</dt>
            <dd>{account.last_reading_source || 'Unavailable'}</dd>
          </div>
          <div>
            <dt>Last reading</dt>
            <dd>{timestamp(account.last_reading_at)}</dd>
          </div>
          <div>
            <dt>Exhaustion source</dt>
            <dd>{account.exhausted_source || 'Unavailable'}</dd>
          </div>
          <div>
            <dt>Exhaustion status</dt>
            <dd>{exhaustion(account.exhausted_until)}</dd>
          </div>
        </dl>
      </details>
      {#if !compact}<p class="muted">
          Configured ceiling: {duration(account.ceiling_seconds)}. {account.last_reading_at
            ? `Last provider reading ${relativeTime(account.last_reading_at).toLowerCase()}.`
            : 'No provider reading available.'}
        </p>{/if}
    </div>
  {:else}<p class="muted empty-inline">
      No provider accounts configured. Set a quota ceiling on the manager to enable dispatch.
    </p>{/each}
</section>

<style>
  .quota-details {
    margin-top: 16px;
    color: var(--muted);
    font-size: 14px;
    line-height: 1.5;
  }
  .quota-details summary {
    cursor: pointer;
  }
  .quota-details dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px 20px;
    margin: 12px 0 0;
    padding-top: 12px;
    border-top: 1px solid var(--border);
  }
  .quota-details dt {
    font-size: 12px;
    color: var(--muted);
  }
  .quota-details dd {
    margin: 2px 0 0;
    overflow-wrap: anywhere;
  }
  @media (max-width: 480px) {
    .quota-details dl {
      grid-template-columns: 1fr;
    }
  }
</style>
