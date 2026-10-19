"""add hashed auth tokens and persistent login limits

Revision ID: 9f241e62a1b7
Revises: 4bf623a3886d
Create Date: 2026-10-19
"""

from alembic import op
import sqlalchemy as sa

import accalab.db


revision = "9f241e62a1b7"
down_revision = "4bf623a3886d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", accalab.db.UTCDateTime(), nullable=False),
        sa.Column("expires_at", accalab.db.UTCDateTime(), nullable=False),
        sa.Column("consumed_at", accalab.db.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_auth_tokens_user_purpose", "auth_tokens", ["user_id", "purpose"], unique=False
    )
    op.create_table(
        "login_rate_limits",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", accalab.db.UTCDateTime(), nullable=False),
        sa.Column("locked_until", accalab.db.UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("key_hash"),
    )


def downgrade() -> None:
    op.drop_table("login_rate_limits")
    op.drop_index("ix_auth_tokens_user_purpose", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_column("users", "session_version")
