"""Add consent records and the Telegram message outbox."""

from alembic import op
import sqlalchemy as sa


revision = "e912c20af630"
down_revision = "d20f5a9c8e31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_consents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("destination", sa.String(length=160)),
        sa.Column("consented_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("link_token_hash", sa.String(length=64), unique=True),
        sa.Column("link_expires_at", sa.DateTime(timezone=True)),
        sa.Column("tickets_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("results_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expiry_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "channel", name="uq_message_consent_user_channel"),
        sa.UniqueConstraint("channel", "destination", name="uq_message_consent_channel_destination"),
    )
    op.create_index(
        "ix_message_consents_channel_destination", "message_consents", ["channel", "destination"]
    )
    op.create_table(
        "message_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_message_id", sa.String(length=160)),
        sa.Column("last_error", sa.String(length=500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("idempotency_key", name="uq_message_outbox_idempotency"),
    )
    op.create_index(
        "ix_message_outbox_status_next_attempt", "message_outbox", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_message_outbox_status_next_attempt", table_name="message_outbox")
    op.drop_table("message_outbox")
    op.drop_index("ix_message_consents_channel_destination", table_name="message_consents")
    op.drop_table("message_consents")
