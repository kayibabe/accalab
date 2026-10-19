"""Register trained model artifacts and promotion evidence."""

from alembic import op
import sqlalchemy as sa


revision = "7a15c6b0d4e2"
down_revision = "3ce4e10c2ab7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("feature_version", sa.String(length=40), nullable=False),
        sa.Column("trained_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True)),
        sa.Column("metrics", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_model_versions_status_created", "model_versions", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_model_versions_status_created", table_name="model_versions")
    op.drop_table("model_versions")
