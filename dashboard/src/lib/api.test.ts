import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { api, getToken, setToken, actions, downloadArtifact, ApiError } from './api';

describe('getToken / setToken', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('returns null when nothing is stored', () => {
    expect(getToken()).toBeNull();
  });

  it('round-trips through the werft_token localStorage key', () => {
    setToken('secret-token');
    expect(getToken()).toBe('secret-token');
    expect(localStorage.getItem('werft_token')).toBe('secret-token');
  });
});

describe('api()', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('prefixes the path with /api/v1 and attaches the bearer token header', async () => {
    setToken('secret-token');
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await api<{ ok: boolean }>('/quota');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/quota');
    const headers = new Headers(init.headers);
    expect(headers.get('Authorization')).toBe('Bearer secret-token');
    expect(result).toEqual({ ok: true });
  });

  it('omits the Authorization header when no token is stored', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await api('/quota');

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.has('Authorization')).toBe(false);
  });

  it('throws a typed ApiError carrying the response status on failure', async () => {
    setToken('secret-token');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })));

    await expect(api('/quota')).rejects.toBeInstanceOf(ApiError);
    await expect(api('/quota')).rejects.toMatchObject({ status: 401 });
  });
});

describe('actions', () => {
  beforeEach(() => {
    localStorage.clear();
    setToken('secret-token');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('POSTs each action to its B3 endpoint', async () => {
    // A fresh Response per call: each real fetch() response has its own
    // unread body, and reusing one mock instance would throw "Body has
    // already been read" on the second call.
    const fetchMock = vi
      .fn()
      .mockImplementation(() => Promise.resolve(new Response('', { status: 200 })));
    vi.stubGlobal('fetch', fetchMock);

    await actions.accept('run-1');
    await actions.reject('run-1');
    await actions.cancel('run-1');
    await actions.requeue('run-1');

    const calls = fetchMock.mock.calls as [string, RequestInit][];
    expect(calls[0][0]).toBe('/api/v1/runs/run-1/review/accept');
    expect(calls[1][0]).toBe('/api/v1/runs/run-1/review/reject');
    expect(calls[2][0]).toBe('/api/v1/runs/run-1/cancel');
    expect(calls[3][0]).toBe('/api/v1/runs/run-1/requeue');
    for (const [, init] of calls) {
      expect(init.method).toBe('POST');
    }
  });
});

describe('downloadArtifact', () => {
  beforeEach(() => {
    localStorage.clear();
    setToken('secret-token');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubBlobPlumbing() {
    // jsdom implements neither createObjectURL nor Blob-from-Response fully,
    // so both ends are stubbed: the object URL, and the anchor click that the
    // browser would turn into a save dialog.
    const createObjectURL = vi.fn(() => 'blob:werft/1');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL, revokeObjectURL }));
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined);
    return { createObjectURL, revokeObjectURL, click };
  }

  it('fetches the artifact with the token in the header, never in the URL', async () => {
    const { click } = stubBlobPlumbing();
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response('bytes', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await downloadArtifact('run-1', 'outputs/log file.jsonl');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // Per *segment*: the separators stay separators, the space does not.
    expect(url).toBe('/api/v1/runs/run-1/artifacts/outputs/log%20file.jsonl');
    expect(url).not.toContain('secret-token');
    expect(url).not.toContain('token');
    const headers = new Headers(init.headers);
    expect(headers.get('Authorization')).toBe('Bearer secret-token');
    expect(click).toHaveBeenCalledTimes(1);
  });

  it('throws a typed ApiError on a non-2xx and never clicks the anchor', async () => {
    const { click, createObjectURL } = stubBlobPlumbing();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 404 })));

    await expect(downloadArtifact('run-1', 'outputs/log.jsonl')).rejects.toBeInstanceOf(ApiError);
    expect(click).not.toHaveBeenCalled();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('carries the failing status on the thrown ApiError', async () => {
    stubBlobPlumbing();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 403 })));

    await expect(downloadArtifact('run-1', 'x.txt')).rejects.toMatchObject({ status: 403 });
  });
});
