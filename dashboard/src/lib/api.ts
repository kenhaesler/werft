const TOKEN_KEY = 'werft_token';
const API_PREFIX = '/api/v1';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message?: string) {
    super(message ?? `Request failed with status ${status}`);
    this.name = 'ApiError';
    this.status = status;
  }
}

/**
 * Calls the manager's `/api/v1` surface, attaching the bearer token from
 * localStorage. Throws a typed ApiError (with `.status`) on any non-2xx
 * response so callers can distinguish 401 (bad/missing token) from 409
 * (state changed under you - refetch) from other failures.
 */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    headers,
    signal: init.signal ?? AbortSignal.timeout(15_000),
  });

  if (!response.ok) {
    throw new ApiError(response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  if (!text) {
    return undefined as T;
  }
  return JSON.parse(text) as T;
}

function post(path: string): Promise<void> {
  return api<void>(path, { method: 'POST' });
}

/**
 * Downloads a single artifact through the bearer-attaching fetch path (the
 * server route requires Authorization, so a plain `<a href>` can't work and
 * would otherwise mean putting the token in a URL). Fetches the bytes,
 * builds an object URL, and clicks a synthetic `<a download>` to trigger the
 * browser's save flow, then revokes the object URL once done.
 */
export async function downloadArtifact(runId: string, path: string): Promise<void> {
  const token = getToken();
  const headers = new Headers();
  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  const encodedPath = path
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/');
  const response = await fetch(`${API_PREFIX}/runs/${runId}/artifacts/${encodedPath}`, {
    headers,
  });

  if (!response.ok) {
    throw new ApiError(response.status);
  }

  const blob = await response.blob();
  const filename = path.split('/').pop() || path;
  const objectUrl = URL.createObjectURL(blob);
  try {
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

export const actions = {
  accept: (id: string): Promise<void> => post(`/runs/${id}/review/accept`),
  reject: (id: string): Promise<void> => post(`/runs/${id}/review/reject`),
  cancel: (id: string): Promise<void> => post(`/runs/${id}/cancel`),
  requeue: (id: string): Promise<void> => post(`/runs/${id}/requeue`),
};
