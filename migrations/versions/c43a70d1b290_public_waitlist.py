"""Store public waitlist signups with consent."""

from alembic import op
import sqlalchemy as sa


revision = "c43a70d1b290"
down_revision = "b5d2c14f8103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waitlist_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False, unique=True),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_waitlist_entries_email", "waitlist_entries", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_waitlist_entries_email", table_name="waitlist_entries")
    op.drop_table("waitlist_entries")
