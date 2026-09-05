import { ApiError, api, getToken } from './api';
import type { Artifact, ArtifactsResponse } from './types';

const API_PREFIX = '/api/v1';
export const MAX_PREVIEW_BYTES = 256 * 1024;

export function artifactKind(path: string): 'diff' | 'report' | 'logs' | 'files' {
  const value = path.toLowerCase();
  if (value.endsWith('.diff') || value.endsWith('.patch')) return 'diff';
  if (value.includes('playwright') || value.includes('test-results') || value.endsWith('.xml')) {
    return 'report';
  }
  if (/(\.log|\.jsonl|\.txt|\.json|transcript|egress)/.test(value)) return 'logs';
  return 'files';
}

export function canPreviewArtifact(artifact: Pick<Artifact, 'path' | 'bytes'>): boolean {
  return (
    artifact.bytes <= MAX_PREVIEW_BYTES &&
    /\.(diff|patch|log|txt|json|jsonl|xml|md|csv|yml|yaml)$/i.test(artifact.path)
  );
}

export async function loadArtifactMetadata(
  runId: string,
  signal?: AbortSignal,
): Promise<Artifact[]> {
  return (await api<ArtifactsResponse>(`/runs/${runId}/artifacts`, { signal })).artifacts;
}

function artifactUrl(runId: string, path: string): string {
  const encodedPath = path.split('/').map(encodeURIComponent).join('/');
  return `${API_PREFIX}/runs/${runId}/artifacts/${encodedPath}`;
}

/** Fetches a small text-only preview. Artifact bytes are never inserted as HTML. */
export async function loadArtifactPreview(
  runId: string,
  artifact: Pick<Artifact, 'path' | 'bytes'>,
  signal?: AbortSignal,
): Promise<string> {
  if (!canPreviewArtifact(artifact)) throw new Error('This file is available to download only.');
  if (artifact.bytes === 0) return '';
  const headers = new Headers({ Range: `bytes=0-${MAX_PREVIEW_BYTES - 1}` });
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(artifactUrl(runId, artifact.path), { headers, signal });
  if (!response.ok) throw new ApiError(response.status);
  const reader = response.body?.getReader();
  if (!reader) return '';
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (total < MAX_PREVIEW_BYTES) {
      const next = await reader.read();
      if (next.done) break;
      const chunk = next.value.slice(0, MAX_PREVIEW_BYTES - total);
      chunks.push(chunk);
      total += chunk.byteLength;
      if (chunk.byteLength !== next.value.byteLength) break;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
  const text = new TextDecoder('utf-8', { fatal: false }).decode(
    chunks.reduce((all, chunk) => {
      const joined = new Uint8Array(all.byteLength + chunk.byteLength);
      joined.set(all);
      joined.set(chunk, all.byteLength);
      return joined;
    }, new Uint8Array()),
  );
  return artifact.bytes > total
    ? `${text}\n\n[Preview limited to ${MAX_PREVIEW_BYTES / 1024} KB]`
    : text;
}
