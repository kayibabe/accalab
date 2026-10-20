"""Add prepaid payments and subscription periods."""

from alembic import op
import sqlalchemy as sa


revision = "d20f5a9c8e31"
down_revision = "c43a70d1b290"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("tx_ref", sa.String(length=80), nullable=False),
        sa.Column("provider_reference", sa.String(length=100)),
        sa.Column("manual_reference", sa.String(length=120), unique=True),
        sa.Column("amount_mwk", sa.Integer(), nullable=False),
        sa.Column("paid_amount_mwk", sa.Integer()),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("checkout_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tx_ref", name="uq_payments_tx_ref"),
    )
    op.create_index("ix_payments_status_created", "payments", ["status", "created_at"])
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("payment_id", sa.String(length=36), sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("plan", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grace_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("payment_id", name="uq_subscriptions_payment_id"),
    )
    op.create_index(
        "ix_subscriptions_user_status_ends", "subscriptions", ["user_id", "status", "ends_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_user_status_ends", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_payments_status_created", table_name="payments")
    op.drop_table("payments")
