import type { RunStatus } from './types';

export const activeStatuses: RunStatus[] = ['claimed', 'running', 'awaiting_ci', 'merging'];
export const statusLabels: Record<RunStatus, string> = {
  queued: 'Queued',
  claimed: 'Preparing',
  running: 'Working',
  awaiting_ci: 'Verifying',
  awaiting_review: 'Needs review',
  merging: 'Merging',
  blocked_quota: 'Quota limited',
  failed: 'Failed',
  parked: 'Parked',
  merged: 'Completed',
  canceled: 'Canceled',
};
export function relativeTime(value: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return 'Unknown';
  if (seconds < 60) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
export function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const i = Math.min(3, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** i).toFixed(i === 1 ? 0 : 1)} ${['B', 'KB', 'MB', 'GB'][i]}`;
}
export function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
export function percent(value: number, total: number): number {
  return total > 0 ? Math.min(100, Math.max(0, (value / total) * 100)) : 0;
}
export function safeExternalUrl(value: string | null): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : undefined;
  } catch {
    return undefined;
  }
}
