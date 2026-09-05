<script lang="ts">
  import Icon from './Icon.svelte';
  import { duration, percent, relativeTime } from './format';
  import type { QuotaAccount } from './types';
  let { accounts, compact = false }: { accounts: QuotaAccount[]; compact?: boolean } = $props();
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
