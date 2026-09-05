<script lang="ts">
  import { api } from './api';
  import Icon from './Icon.svelte';

  type MessageStatus = 'queued' | 'delivered' | 'answered' | 'failed' | 'demo';
  type Message = {
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    status: MessageStatus;
    created_at: string;
    error?: string;
  };
  type ConversationResponse = {
    messages: Message[];
    available: boolean;
    unavailable_reason?: string;
  };

  let { scope, demo = false }: { scope: 'orchestrator' | string; demo?: boolean } = $props();
  let messages = $state<Message[]>([]);
  let available = $state(true);
  let unavailableReason = $state('');
  let loading = $state(true);
  let refreshError = $state('');
  let draft = $state('');
  let sendError = $state('');
  let sending = $state(false);
  let pendingContent = $state('');
  let pendingClientId = $state('');
  let scopeGeneration = 0;
  let updateGeneration = 0;
  let sendController: AbortController | undefined;
  const title = $derived(scope === 'orchestrator' ? 'Talk to Werft' : 'Task conversation');
  const scopeLabel = $derived(scope === 'orchestrator' ? 'Orchestrator' : 'This task’s agent');
  const reasons: Record<string, string> = {
    conversation_credentials_unavailable:
      'Configure a conversation model in your manager to talk to Werft.',
    runner_conversation_unavailable:
      'This agent session is not accepting messages. Messaging requires a session started with conversation support.',
    runner_conversation_closed: 'The session ended before this message was answered.',
    provider_request_failed:
      'Werft could not obtain a reply. Check the model connection. Review recorded actions before sending again.',
  };
  const readableReason = (value: string) => reasons[value] ?? value;

  function demoMessages(): Message[] {
    return [
      {
        id: 'demo-system',
        role: 'system',
        content:
          'Local demo only. This discussion is illustrative and is not connected to a manager or agent.',
        status: 'demo',
        created_at: '2026-01-01T09:00:00Z',
      },
      {
        id: 'demo-assistant',
        role: 'assistant',
        content:
          'When connected, you can ask questions or give direction here. The manager will show each recorded reply and delivery status.',
        status: 'demo',
        created_at: '2026-01-01T09:01:00Z',
      },
    ];
  }

  function makeClientId(): string | null {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    if (!globalThis.crypto?.getRandomValues) return null;
    const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function statusLabel(status: MessageStatus) {
    return status === 'demo' ? 'Demo' : status;
  }

  $effect(() => {
    const currentScope = scope;
    const currentScopeGeneration = ++scopeGeneration;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let wake: (() => void) | undefined;
    loading = true;
    messages = [];
    available = true;
    unavailableReason = '';
    refreshError = '';
    draft = '';
    sendError = '';
    pendingContent = '';
    pendingClientId = '';
    sending = false;
    sendController?.abort();
    sendController = undefined;
    if (demo) {
      messages = demoMessages();
      available = false;
      unavailableReason = 'Preview is not connected to a manager.';
      loading = false;
      return () => {};
    }
    const refresh = async () => {
      const currentUpdateGeneration = updateGeneration;
      try {
        const response = await api<ConversationResponse>(
          `/conversations/${encodeURIComponent(currentScope)}`,
          { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]) },
        );
        if (
          controller.signal.aborted ||
          scope !== currentScope ||
          scopeGeneration !== currentScopeGeneration ||
          updateGeneration !== currentUpdateGeneration
        )
          return;
        messages = response.messages;
        available = response.available;
        unavailableReason = response.unavailable_reason ?? '';
        refreshError = '';
      } catch (err) {
        if (!controller.signal.aborted && scopeGeneration === currentScopeGeneration) {
          refreshError = err instanceof Error ? err.message : 'Conversation could not refresh.';
        }
      } finally {
        if (!controller.signal.aborted && scopeGeneration === currentScopeGeneration)
          loading = false;
      }
    };
    const wait = (ms: number) =>
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, ms);
        wake = () => {
          if (timer) clearTimeout(timer);
          timer = undefined;
          wake = undefined;
          resolve();
        };
      });
    const onVisibility = () => {
      if (document.visibilityState === 'visible') wake?.();
    };
    document.addEventListener('visibilitychange', onVisibility);
    const poll = async () => {
      await refresh();
      while (!controller.signal.aborted) {
        await wait(document.visibilityState === 'visible' ? 2000 : 60_000);
        if (!controller.signal.aborted && document.visibilityState === 'visible') await refresh();
      }
    };
    void poll();
    return () => {
      controller.abort();
      sendController?.abort();
      if (timer) clearTimeout(timer);
      wake?.();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  });

  async function send() {
    const content = pendingContent || draft.trim();
    if (!content || sending) return;
    if (demo) {
      messages = [
        ...messages,
        {
          id: `demo-user-${Date.now()}`,
          role: 'user',
          content,
          status: 'demo',
          created_at: new Date().toISOString(),
        },
        {
          id: `demo-reply-${Date.now()}`,
          role: 'assistant',
          content:
            'Local demo only: this preview is not connected, so no direction or question was delivered.',
          status: 'demo',
          created_at: new Date().toISOString(),
        },
      ];
      draft = '';
      return;
    }
    const currentScope = scope;
    const currentScopeGeneration = scopeGeneration;
    pendingContent = content;
    pendingClientId ||= makeClientId() ?? '';
    if (!pendingClientId) {
      sendError =
        'Secure message ID generation is unavailable. Reload in a supported browser and try again.';
      return;
    }
    const currentUpdateGeneration = ++updateGeneration;
    const controller = new AbortController();
    sendController = controller;
    sending = true;
    sendError = '';
    try {
      const response = await api<ConversationResponse>(
        `/conversations/${encodeURIComponent(currentScope)}/messages`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content, client_id: pendingClientId }),
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(90_000)]),
        },
      );
      if (
        controller.signal.aborted ||
        scope !== currentScope ||
        scopeGeneration !== currentScopeGeneration ||
        updateGeneration !== currentUpdateGeneration
      )
        return;
      messages = response.messages;
      available = response.available;
      unavailableReason = response.unavailable_reason ?? '';
      draft = '';
      pendingContent = '';
      pendingClientId = '';
    } catch (err) {
      if (controller.signal.aborted || scopeGeneration !== currentScopeGeneration) return;
      sendError = `${err instanceof Error ? err.message : 'Message could not be sent.'} Retry uses the same message ID.`;
    } finally {
      if (scopeGeneration === currentScopeGeneration && sendController === controller) {
        sending = false;
        sendController = undefined;
      }
    }
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  }
</script>

<section class="conversation" aria-label={title}>
  <div class="conversation-heading">
    <div>
      {#if scope !== 'orchestrator'}<h2>{title}</h2>{/if}
      <p>{scopeLabel} · Ask a question or give direction.</p>
    </div>
    {#if !demo && !loading}<span class:unavailable={!available} class="conversation-availability"
        >{available ? 'Available' : 'Unavailable'}</span
      >{/if}
  </div>
  {#if demo}<p class="conversation-demo" role="status">
      Local demo — messages stay in this browser preview.
    </p>{/if}
  {#if unavailableReason}<p class="conversation-notice" role="status">
      {readableReason(unavailableReason)}
    </p>{/if}
  {#if refreshError}<p class="conversation-notice warning" role="status">
      Conversation updates interrupted: {refreshError}
    </p>{/if}
  <div class="conversation-messages" aria-live="polite" aria-busy={loading}>
    {#if loading}<p class="conversation-empty">Loading conversation…</p>{:else if messages.length}
      {#each messages as message (message.id)}
        <article class="conversation-message {message.role}">
          <div class="message-meta">
            <strong
              >{message.role === 'user'
                ? 'You'
                : message.role === 'assistant'
                  ? 'Werft'
                  : 'System'}</strong
            ><time datetime={message.created_at}
              >{new Date(message.created_at).toLocaleTimeString([], {
                hour: '2-digit',
                minute: '2-digit',
              })}</time
            >
          </div>
          <p>{message.content}</p>
          {#if message.status !== 'answered'}<small class:failed={message.status === 'failed'}
              >{statusLabel(message.status)}{message.error
                ? ` · ${readableReason(message.error)}`
                : ''}</small
            >{/if}
        </article>
      {/each}
    {:else}<p class="conversation-empty">
        No messages yet. Ask a question or share the next direction.
      </p>{/if}
  </div>
  <form
    class="conversation-composer"
    onsubmit={(event) => {
      event.preventDefault();
      void send();
    }}
  >
    <label for={`conversation-${scope}`}>Message {scopeLabel.toLowerCase()}</label>
    <textarea
      id={`conversation-${scope}`}
      bind:value={draft}
      onkeydown={onKeydown}
      disabled={sending || (!demo && !available)}
      placeholder="Ask about the work or describe what should change…"
      rows="3"
      maxlength="4000"></textarea>
    {#if sendError}<p class="conversation-send-error" role="alert">{sendError}</p>{/if}
    <div class="composer-actions">
      <small>Enter to send · Shift + Enter for a new line</small><button
        class="button primary"
        disabled={sending || (!pendingContent && !draft.trim()) || (!demo && !available)}
        >{sending ? 'Sending…' : pendingContent ? 'Retry message' : 'Send message'}<Icon
          name="arrow"
          size={15}
        /></button
      >
    </div>
  </form>
</section>

<style>
  .conversation {
    display: flex;
    flex: 1;
    min-height: 0;
    flex-direction: column;
    gap: 14px;
  }
  .conversation-heading {
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 16px;
  }
  .conversation-heading h2 {
    font-size: 18px;
  }
  .conversation-heading p,
  .conversation-empty,
  .composer-actions small {
    color: var(--muted);
    font-size: 13px;
    line-height: 1.55;
  }
  .conversation-availability {
    color: #215bc7;
    background: #eaf2ff;
    border: 1px solid #c5d9fb;
    border-radius: 6px;
    padding: 4px 7px;
    font-size: 12px;
    white-space: nowrap;
  }
  .conversation-availability.unavailable,
  .failed {
    color: #a34434;
  }
  .conversation-demo,
  .conversation-notice {
    margin: 0;
    padding: 10px 12px;
    background: #edf3ff;
    border: 1px solid #c5d9fb;
    border-radius: 9px;
    color: #365e95;
    font-size: 13px;
    line-height: 1.5;
  }
  .conversation-notice.warning,
  .conversation-send-error {
    color: #92531c;
    background: #fff7ea;
    border: 1px solid #ead1a4;
  }
  .conversation-messages {
    flex: 1;
    min-height: 180px;
    max-height: 440px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 2px;
  }
  .conversation-message {
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: 9px;
    background: white;
    overflow-wrap: anywhere;
  }
  .conversation-message.user {
    background: #eef4ff;
    border-color: #c5d9fb;
  }
  .conversation-message.system {
    background: #f7f9fc;
  }
  .message-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    color: var(--muted);
    font-size: 12px;
  }
  .message-meta strong {
    color: var(--text);
  }
  .conversation-message p {
    margin: 7px 0 0;
    white-space: pre-wrap;
    font-size: 14px;
    line-height: 1.6;
  }
  .conversation-message small {
    display: block;
    margin-top: 7px;
    color: var(--muted);
    font-size: 12px;
  }
  .conversation-composer {
    display: grid;
    gap: 7px;
    padding-top: 14px;
    border-top: 1px solid var(--border);
  }
  .conversation-composer label {
    font-size: 13px;
    font-weight: 550;
  }
  .conversation-composer textarea {
    margin-bottom: 9px;
    background: white;
    min-height: 76px;
    font-size: 14px;
    line-height: 1.55;
  }
  .conversation-send-error {
    margin: 0;
    padding: 9px 11px;
    border-radius: 7px;
    font-size: 13px;
    line-height: 1.45;
  }
  .composer-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .composer-actions .button {
    min-height: 40px;
    font-size: 13px;
  }
  @media (max-width: 600px) {
    .conversation-messages {
      max-height: 45dvh;
    }
    .composer-actions {
      align-items: end;
    }
    .composer-actions small {
      max-width: 19ch;
    }
  }
</style>
