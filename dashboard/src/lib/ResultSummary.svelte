<script lang="ts">
  import { duration } from './format';
  type UnknownRecord = Record<string, unknown>;
  let { result }: { result: UnknownRecord } = $props();
  const record = (value: unknown): UnknownRecord | null =>
    value && typeof value === 'object' && !Array.isArray(value) ? (value as UnknownRecord) : null;
  const string = (value: unknown) => (typeof value === 'string' ? value : null);
  const number = (value: unknown) =>
    typeof value === 'number' && Number.isFinite(value) ? value : null;
  const envelope = $derived(record(result.result_json) ?? result);
  const usage = $derived(record(result.usage) ?? record(envelope.usage));
  const resultError = $derived(record(envelope.error));
  const problem = $derived(string(result.problem));
  const exitMeaning = $derived(string(result.exit_meaning));
</script>

<section class="result-summary" aria-label="Run outcome">
  <div class="result-heading">
    <h3>Run outcome</h3>
    <strong>{string(envelope.status) ?? 'Reported result'}</strong>
  </div>
  <dl>
    {#if string(envelope.commit_sha)}<div>
        <dt>Commit</dt>
        <dd><code>{string(envelope.commit_sha)}</code></dd>
      </div>{/if}
    {#if typeof envelope.pushed === 'boolean'}<div>
        <dt>Branch update</dt>
        <dd>{envelope.pushed ? 'Pushed' : 'Not pushed'}</dd>
      </div>{/if}
    {#if number(envelope.duration_seconds) !== null}<div>
        <dt>Session duration</dt>
        <dd>{duration(number(envelope.duration_seconds)!)}</dd>
      </div>{/if}
    {#if string(envelope.started_at)}<div>
        <dt>Started</dt>
        <dd>{new Date(string(envelope.started_at)!).toLocaleString()}</dd>
      </div>{/if}
    {#if string(envelope.ended_at)}<div>
        <dt>Finished</dt>
        <dd>{new Date(string(envelope.ended_at)!).toLocaleString()}</dd>
      </div>{/if}
    {#if exitMeaning}<div>
        <dt>Exit meaning</dt>
        <dd>{exitMeaning}</dd>
      </div>{/if}
  </dl>
  {#if problem}<p class="result-error"><strong>Problem</strong>: {problem}</p>{/if}
  {#if resultError}<p class="result-error">
      <strong>{string(resultError.code) ?? 'Run error'}</strong>{#if string(resultError.message)}: {string(
          resultError.message,
        )}{/if}
    </p>{/if}
  {#if usage}<div class="usage">
      <strong>Observed provider usage</strong>{#if number(usage.input_tokens) !== null}<span
          >{number(usage.input_tokens)} input tokens</span
        >{/if}{#if number(usage.output_tokens) !== null}<span
          >{number(usage.output_tokens)} output tokens</span
        >{/if}{#if number(usage.cache_creation_input_tokens) !== null}<span
          >{number(usage.cache_creation_input_tokens)} cache-write tokens</span
        >{/if}{#if number(usage.cache_read_input_tokens) !== null}<span
          >{number(usage.cache_read_input_tokens)} cache-read tokens</span
        >{/if}{#if number(usage.total_cost_usd) !== null}<span
          >${number(usage.total_cost_usd)!.toFixed(2)} reported cost</span
        >{/if}
    </div>{/if}
  <details>
    <summary>Raw result</summary>
    <pre>{JSON.stringify(result, null, 2)}</pre>
  </details>
</section>

<style>
  .result-summary {
    margin-top: 20px;
    padding: 18px;
    background: #edf3ff;
    border-radius: 12px;
    font-size: 14px;
    color: #172b4d;
  }
  .result-heading {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: baseline;
  }
  h3 {
    font-size: 16px;
  }
  .result-heading strong {
    text-transform: capitalize;
    color: #215bc7;
  }
  dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
    margin: 16px 0 0;
  }
  dt {
    color: #52657e;
    font-size: 13px;
    margin-bottom: 4px;
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  code {
    font-size: 13px;
  }
  .usage,
  .result-error {
    margin: 16px 0 0;
    padding-top: 14px;
    border-top: 1px solid #dce5f0;
    display: flex;
    flex-wrap: wrap;
    gap: 6px 14px;
    color: #52657e;
  }
  .usage strong,
  .result-error strong {
    color: #172b4d;
  }
  .result-error {
    color: #8a462d;
  }
  details {
    margin-top: 14px;
  }
  summary {
    cursor: pointer;
    color: #365e95;
    font-size: 14px;
    min-height: 44px;
    display: flex;
    align-items: center;
  }
  pre {
    max-height: 260px;
    overflow: auto;
    margin: 10px 0 0;
    padding: 12px;
    background: #fff;
    border-radius: 8px;
    font-size: 13px;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  @media (max-width: 600px) {
    dl {
      grid-template-columns: 1fr;
    }
  }
</style>
