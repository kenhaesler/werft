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
  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  const response = await fetch(`${API_PREFIX}${path}`, { ...init, headers });

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

export const actions = {
  accept: (id: string): Promise<void> => post(`/runs/${id}/review/accept`),
  reject: (id: string): Promise<void> => post(`/runs/${id}/review/reject`),
  cancel: (id: string): Promise<void> => post(`/runs/${id}/cancel`),
  requeue: (id: string): Promise<void> => post(`/runs/${id}/requeue`),
};
