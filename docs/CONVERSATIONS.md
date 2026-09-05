# Conversations with Werft and task agents

Open **Talk to Werft** to ask about projects and tasks. Werft can send direction
to a running agent and change the priority of a queued task. Larger priority
values run first. It cannot yet create tasks or pause/resume execution through
conversation. Each executed action is recorded separately from the model's reply.

Open an agent and select **Conversation** to ask a question or give a correction.
Input is queued for the next turn of the same Claude process, preserving its
current workspace and context. It does not interrupt a tool in progress. When
the task completes, its conversation becomes read-only; a chat does not keep a
finished container alive or restart a completed task.

## Enable on a manager

1. Deploy the updated manager and run `uv run alembic upgrade head` from its
   application directory. Compose's existing migration service handles this
   when deploying the updated stack. Migration `0002` adds conversation history.
2. For the orchestrator, mount an Anthropic Console API key as a read-only file
   and set `WERFT_CONVERSATION_API_KEY_FILE` to its container path. Set
   `WERFT_CONVERSATION_MODEL` to a model available to that API account. This is
   separate from the existing runner's Claude OAuth token. Empty configuration
   leaves Talk to Werft unavailable with an explanation.
3. For agent messages, rebuild the configured runner image with the updated
   `runners/adapter/werft_adapter` package, update its configured image digest,
   and set `WERFT_AGENT_CONVERSATIONS_ENABLED=true` on the manager. This defaults
   to false so an old deployed adapter is never given incompatible input flags.
   Existing running sessions stay read-only; start new tasks with the rebuilt image.

For the included Compose deployment, put the API key at
`/opt/werft/secrets/conversation_api_key`, set `CONVERSATION_MODEL`, and use the
optional `deploy/compose.conversations.yaml` alongside `deploy/compose.yaml`.
Set `AGENT_CONVERSATIONS_ENABLED=true` only after the runner image update.
The manager needs HTTPS access to `api.anthropic.com` through its configured proxy.
The key is never sent to the browser or placed in the runner's message inbox.

## Delivery and retention

- **Queued:** saved by the manager, awaiting the next agent turn.
- **Delivered:** the Claude CLI echoed the operator message ID.
- **Answered:** the runtime returned a result for that turn.
- **Failed:** the message or response could not complete. Review recorded actions
  before sending another instruction; a response failure does not undo an action.

History lives in Postgres. Each agent message is bound to one run and attempt;
messages never carry over to an automatic retry. The driver imports replies
before finalizing the attempt, independently of whether the browser is open.
The API shows the latest 200 messages; model context includes the latest 30
conversation messages, 20 projects, and 40 tasks. These are bounded snapshots,
not an unlimited account of all historical work.

The authenticated API uses `GET /api/v1/conversations/{scope}` and
`POST /api/v1/conversations/{scope}/messages`, where scope is `orchestrator` or a
run UUID. POST requires `{content, client_id}` with a UUID idempotency key.
Replaying a key returns the recorded result, including a recorded failure,
without executing the instruction again. One orchestrator request runs at a time.
Inputs are limited to 16,000 characters. Messages to finished or unsupported
sessions are rejected; no fake delivery acknowledgements are generated.

## Verification

`scripts/check-conversation-cli.py` runs the actual pinned Claude 2.1.220 CLI
against a local fixture server with Docker networking disabled. It verifies
JSONL input, replayed message IDs, and a follow-up reply through the adapter:

```sh
docker run --rm --network none -v "$PWD:/repo:ro" -w /tmp \
  werft-runner-base:2026-08-01 python3.12 /repo/scripts/check-conversation-cli.py
```

This exercises protocol compatibility without credentials or paid inference.
Deployment still requires the operator's configured model and rebuilt runner.
The protocol uses the documented Claude CLI
[streaming input and replay flags](https://code.claude.com/docs/en/cli-reference).
