"""Durable operator and run conversation messages."""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE conversation_messages (
          id uuid PRIMARY KEY DEFAULT uuidv7(),
          scope text NOT NULL,
          client_id uuid NULL,
          run_id uuid NULL REFERENCES runs(id) ON DELETE CASCADE,
          attempt_no smallint NULL,
          role text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
          content text NOT NULL,
          status text NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'delivered', 'answered', 'failed')),
          error text NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_conversation_messages_scope_client_id UNIQUE (scope, client_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_conversation_messages_scope_created_at "
        "ON conversation_messages (scope, created_at, id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE conversation_messages")
