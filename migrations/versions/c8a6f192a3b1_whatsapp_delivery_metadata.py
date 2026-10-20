"""Track WhatsApp template delivery state and estimate delivered-message cost."""

from alembic import op
import sqlalchemy as sa


revision = "c8a6f192a3b1"
down_revision = "e912c20af630"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_consents",
        sa.Column("age_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "message_consents",
        sa.Column("verification_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("message_outbox", sa.Column("template_name", sa.String(length=100)))
    op.add_column("message_outbox", sa.Column("template_category", sa.String(length=32)))
    op.add_column("message_outbox", sa.Column("whatsapp_status", sa.String(length=24)))
    op.add_column("message_outbox", sa.Column("estimated_cost_usd", sa.Numeric(12, 6)))
    op.add_column("message_outbox", sa.Column("delivered_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_message_outbox_provider_message_id",
        "message_outbox",
        ["provider_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_outbox_provider_message_id", table_name="message_outbox")
    op.drop_column("message_outbox", "delivered_at")
    op.drop_column("message_outbox", "estimated_cost_usd")
    op.drop_column("message_outbox", "whatsapp_status")
    op.drop_column("message_outbox", "template_category")
    op.drop_column("message_outbox", "template_name")
    op.drop_column("message_consents", "age_confirmed")
    op.drop_column("message_consents", "verification_attempts")
