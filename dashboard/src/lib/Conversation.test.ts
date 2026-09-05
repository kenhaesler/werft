import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';
import Conversation from './Conversation.svelte';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const conversation = (messages: object[] = []) => ({ messages, available: true });

function deferred<T>() {
  let resolve: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve: resolve! };
}

it('loads the selected scope and sends a message through its scoped endpoint', async () => {
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/conversations/orchestrator'))
      return new Response(JSON.stringify(conversation()));
    expect(url).toContain('/conversations/orchestrator/messages');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toMatchObject({ content: 'What should happen next?' });
    return new Response(
      JSON.stringify(
        conversation([
          {
            id: 'one',
            role: 'user',
            content: 'What should happen next?',
            status: 'queued',
            created_at: '2026-01-01T12:00:00Z',
          },
        ]),
      ),
    );
  });
  vi.stubGlobal('fetch', fetchMock);
  const screen = render(Conversation, { props: { scope: 'orchestrator' } });
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  await fireEvent.input(screen.getByLabelText('Message orchestrator'), {
    target: { value: 'What should happen next?' },
  });
  await fireEvent.click(screen.getByRole('button', { name: /Send message/ }));
  await waitFor(() => expect(screen.getByText('What should happen next?')).not.toBeNull());
});

it('keeps the idempotency key when a failed send is retried', async () => {
  let postCount = 0;
  const clientIds: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      if (!String(input).includes('/messages')) return new Response(JSON.stringify(conversation()));
      postCount += 1;
      clientIds.push(JSON.parse(String(init?.body)).client_id);
      if (postCount === 1) throw new Error('Network interrupted');
      return new Response(JSON.stringify(conversation()));
    }),
  );
  const screen = render(Conversation, { props: { scope: 'run-123' } });
  await waitFor(() => expect(screen.getByLabelText('Message this task’s agent')).not.toBeNull());
  await fireEvent.input(screen.getByLabelText('Message this task’s agent'), {
    target: { value: 'Pause before review.' },
  });
  await fireEvent.click(screen.getByRole('button', { name: /Send message/ }));
  await waitFor(() => expect(screen.getByText(/Retry uses the same message ID/)).not.toBeNull());
  await fireEvent.click(screen.getByRole('button', { name: /Retry message/ }));
  await waitFor(() => expect(postCount).toBe(2));
  expect(clientIds[0]).toBe(clientIds[1]);
  expect(clientIds[0]).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
  );
});

it('does not claim delivery in the local demo', async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  const screen = render(Conversation, { props: { scope: 'orchestrator', demo: true } });
  expect(screen.getByText('Local demo — messages stay in this browser preview.')).not.toBeNull();
  await fireEvent.input(screen.getByLabelText('Message orchestrator'), {
    target: { value: 'Change it.' },
  });
  await fireEvent.click(screen.getByRole('button', { name: /Send message/ }));
  expect(
    screen.getByText(/not connected, so no direction or question was delivered/),
  ).not.toBeNull();
  expect(fetchMock).not.toHaveBeenCalled();
});

it('refreshes when its conversation scope changes', async () => {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(conversation())));
  vi.stubGlobal('fetch', fetchMock);
  const screen = render(Conversation, { props: { scope: 'run-one' } });
  const requestedUrls = () =>
    (fetchMock.mock.calls as unknown as Array<[string | URL | Request]>).map(([url]) =>
      String(url),
    );
  await waitFor(() =>
    expect(requestedUrls().some((url) => url.includes('/conversations/run-one'))).toBe(true),
  );
  await screen.rerender({ scope: 'run-two' });
  await waitFor(() =>
    expect(requestedUrls().some((url) => url.includes('/conversations/run-two'))).toBe(true),
  );
});

it('drops a pending send when its scope changes', async () => {
  const pendingPost = deferred<Response>();
  vi.stubGlobal(
    'fetch',
    vi.fn((input: string | URL | Request) => {
      if (String(input).includes('/messages')) return pendingPost.promise;
      return Promise.resolve(new Response(JSON.stringify(conversation())));
    }),
  );
  const screen = render(Conversation, { props: { scope: 'run-one' } });
  await waitFor(() => expect(screen.getByLabelText('Message this task’s agent')).not.toBeNull());
  await fireEvent.input(screen.getByLabelText('Message this task’s agent'), {
    target: { value: 'Old direction' },
  });
  await fireEvent.click(screen.getByRole('button', { name: /Send message/ }));
  await screen.rerender({ scope: 'run-two' });
  pendingPost.resolve(
    new Response(
      JSON.stringify(
        conversation([
          {
            id: 'old',
            role: 'assistant',
            content: 'Old reply',
            status: 'answered',
            created_at: '',
          },
        ]),
      ),
    ),
  );
  await waitFor(() => expect(screen.getByLabelText('Message this task’s agent')).not.toBeNull());
  expect(screen.queryByText('Old reply')).toBeNull();
  expect((screen.getByLabelText('Message this task’s agent') as HTMLTextAreaElement).value).toBe(
    '',
  );
});

it('does not let an older GET overwrite a newer POST response', async () => {
  const pendingGet = deferred<Response>();
  let getCount = 0;
  vi.useFakeTimers();
  const fetchMock = vi.fn((input: string | URL | Request) => {
    if (String(input).includes('/messages')) {
      return Promise.resolve(
        new Response(
          JSON.stringify(
            conversation([
              {
                id: 'new',
                role: 'assistant',
                content: 'New reply',
                status: 'answered',
                created_at: '',
              },
            ]),
          ),
        ),
      );
    }
    getCount += 1;
    return getCount === 1
      ? Promise.resolve(new Response(JSON.stringify(conversation())))
      : pendingGet.promise;
  });
  vi.stubGlobal('fetch', fetchMock);
  const screen = render(Conversation, { props: { scope: 'orchestrator' } });
  await vi.advanceTimersByTimeAsync(0);
  expect(screen.getByText(/No messages yet/)).not.toBeNull();
  await vi.advanceTimersByTimeAsync(2000);
  expect(fetchMock).toHaveBeenCalledTimes(2);
  await fireEvent.input(screen.getByLabelText('Message orchestrator'), {
    target: { value: 'Question' },
  });
  await fireEvent.click(screen.getByRole('button', { name: /Send message/ }));
  await waitFor(() => expect(screen.getByText('New reply')).not.toBeNull());
  pendingGet.resolve(
    new Response(
      JSON.stringify(
        conversation([
          {
            id: 'old',
            role: 'assistant',
            content: 'Old refresh',
            status: 'answered',
            created_at: '',
          },
        ]),
      ),
    ),
  );
  await waitFor(() => expect(screen.queryByText('Old refresh')).toBeNull());
  expect(screen.getByText('New reply')).not.toBeNull();
});
