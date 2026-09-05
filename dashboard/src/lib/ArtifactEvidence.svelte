<script lang="ts">
  import { onDestroy } from 'svelte';
  import Icon from './Icon.svelte';
  import { artifactKind, canPreviewArtifact, loadArtifactPreview } from './artifact-evidence';
  import { bytes } from './format';
  import type { Artifact } from './types';

  let {
    runId,
    artifacts,
    demo = false,
    ondownload,
  }: {
    runId: string;
    artifacts: Artifact[];
    demo?: boolean;
    ondownload: (path: string) => Promise<void> | void;
  } = $props();
  let preview = $state<Artifact | null>(null);
  let previewText = $state('');
  let previewError = $state('');
  let loading = $state(false);
  let downloading = $state('');
  let controller: AbortController | undefined;
  let previewRunId = $state('');
  const groups = $derived(
    ['diff', 'report', 'logs', 'files']
      .map((kind) => ({
        kind,
        artifacts: artifacts.filter((artifact) => artifactKind(artifact.path) === kind),
      }))
      .filter((group) => group.artifacts.length),
  );
  const labels: Record<string, string> = {
    diff: 'Changes',
    report: 'Test reports',
    logs: 'Logs and output',
    files: 'Other files',
  };
  $effect(() => {
    if (!previewRunId) {
      previewRunId = runId;
      return;
    }
    if (previewRunId === runId) return;
    controller?.abort();
    controller = undefined;
    preview = null;
    previewText = '';
    previewError = '';
    loading = false;
    previewRunId = runId;
  });
  onDestroy(() => controller?.abort());
  async function openPreview(artifact: Artifact) {
    controller?.abort();
    const request = new AbortController();
    controller = request;
    preview = artifact;
    previewText = '';
    previewError = '';
    loading = true;
    try {
      const text = demo
        ? `Preview of ${artifact.path}\n\nThis sample preview is text only; collected HTML is never executed in the dashboard.`
        : await loadArtifactPreview(runId, artifact, request.signal);
      if (controller === request && !request.signal.aborted) previewText = text;
    } catch (error) {
      if (controller === request && !request.signal.aborted)
        previewError = error instanceof Error ? error.message : 'Could not load this preview.';
    } finally {
      if (controller === request && !request.signal.aborted) loading = false;
    }
  }
  async function download(path: string) {
    if (downloading) return;
    downloading = path;
    try {
      await ondownload(path);
    } finally {
      downloading = '';
    }
  }
</script>

<div class="evidence">
  {#each groups as group (group.kind)}<section class="evidence-group">
      <h3>{labels[group.kind]}</h3>
      {#each group.artifacts as artifact (artifact.path)}<div class="artifact">
          <div class="artifact-name">
            <Icon name="file" /><span
              ><strong>{artifact.path}</strong><small
                >{bytes(artifact.bytes)} · collected {new Date(
                  artifact.collected_at,
                ).toLocaleString()}{#if artifact.content_hash}
                  · SHA-256 <code>{artifact.content_hash}</code>{/if}</small
              ></span
            >
          </div>
          <div class="artifact-actions">
            {#if canPreviewArtifact(artifact)}<button
                class="text-button"
                aria-label={`Preview ${artifact.path}`}
                onclick={() => openPreview(artifact)}>Preview</button
              >{/if}<button
              class="text-button"
              aria-label={`Download ${artifact.path}`}
              disabled={!!downloading}
              onclick={() => download(artifact.path)}
              >{downloading === artifact.path ? 'Downloading…' : 'Download'}</button
            >
          </div>
        </div>{/each}
    </section>{/each}
  {#if preview}<section class="preview" aria-live="polite">
      <div>
        <h3>{preview.path}</h3>
        <button
          class="text-button"
          onclick={() => {
            controller?.abort();
            preview = null;
          }}>Close preview</button
        >
      </div>
      {#if loading}<p>Loading safe text preview…</p>{:else if previewError}<p class="preview-error">
          {previewError}
        </p>{:else}<pre>{previewText}</pre>{/if}
    </section>{/if}
</div>

<style>
  .evidence {
    font-size: 14px;
    color: #172b4d;
  }
  .evidence-group + .evidence-group {
    margin-top: 24px;
  }
  h3 {
    font-size: 15px;
    margin: 0 0 8px;
  }
  .artifact {
    display: flex;
    gap: 12px;
    align-items: center;
    justify-content: space-between;
    padding: 12px 0;
    border-bottom: 1px solid #dce5f0;
  }
  .artifact-name {
    display: flex;
    gap: 9px;
    min-width: 0;
    align-items: flex-start;
  }
  .artifact-name :global(svg) {
    color: #365e95;
    margin-top: 2px;
  }
  .artifact-name span {
    min-width: 0;
  }
  strong,
  small {
    display: block;
    overflow-wrap: anywhere;
  }
  small {
    color: #52657e;
    font-size: 13px;
    margin-top: 4px;
    line-height: 1.45;
  }
  code {
    font-size: 13px;
  }
  .artifact-actions {
    display: flex;
    flex: 0 0 auto;
    gap: 12px;
  }
  .text-button {
    border: 0;
    background: none;
    color: #365e95;
    padding: 10px 0;
    min-height: 44px;
    font-size: 14px;
  }
  .text-button:hover {
    color: #215bc7;
    text-decoration: underline;
    text-underline-offset: 4px;
  }
  .preview {
    margin-top: 24px;
    padding: 16px;
    background: #edf3ff;
    border-radius: 12px;
  }
  .preview > div {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
  }
  .preview p {
    color: #52657e;
    margin: 12px 0 0;
  }
  .preview-error {
    color: #8a462d !important;
  }
  pre {
    max-height: 360px;
    overflow: auto;
    margin: 12px 0 0;
    padding: 12px;
    background: #fff;
    border-radius: 8px;
    font-size: 14px;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  @media (max-width: 600px) {
    .artifact {
      align-items: flex-start;
      flex-direction: column;
    }
    .artifact-actions {
      padding-left: 25px;
    }
  }
</style>
