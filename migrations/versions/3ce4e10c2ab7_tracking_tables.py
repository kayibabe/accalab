"""Persist the rule-selected weekly Free ticket sample."""

from alembic import op
import sqlalchemy as sa

revision = "3ce4e10c2ab7"
down_revision = "9f241e62a1b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "free_weekly_samples",
        sa.Column("week_start", sa.Date(), primary_key=True),
        sa.Column("ticket_id", sa.String(length=36), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selection_rule", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.UniqueConstraint("ticket_id", name="uq_free_weekly_sample_ticket"),
    )
    op.create_table(
        "single_selections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("run_type", sa.String(length=16), nullable=False),
        sa.Column("fixture_id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("selection", sa.String(length=160), nullable=False),
        sa.Column("line", sa.Numeric(precision=8, scale=3)),
        sa.Column("odds_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("odds", sa.Numeric(precision=12, scale=5), nullable=False),
        sa.Column("probability", sa.Numeric(precision=8, scale=7), nullable=False),
        sa.Column("p_low", sa.Numeric(precision=8, scale=7), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("why", sa.String(length=140), nullable=False),
        sa.ForeignKeyConstraint(["fixture_id"], ["fixtures.id"]),
        sa.ForeignKeyConstraint(["market_id"], ["markets_registry.id"]),
        sa.ForeignKeyConstraint(["odds_snapshot_id"], ["odds_snapshots.id"]),
        sa.UniqueConstraint(
            "target_date",
            "fixture_id",
            "market_id",
            "selection",
            "line",
            "run_type",
            name="uq_single_selection_daily",
        ),
    )
    op.create_index("ix_single_selections_date", "single_selections", ["target_date"])
    op.create_table(
        "single_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("single_selection_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("payout_multiplier", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correction_of_id", sa.Integer()),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["single_selection_id"], ["single_selections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["correction_of_id"], ["single_results.id"]),
    )
    op.create_index(
        "ix_single_results_selection_settled",
        "single_results",
        ["single_selection_id", "settled_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_single_results_selection_settled", table_name="single_results")
    op.drop_table("single_results")
    op.drop_index("ix_single_selections_date", table_name="single_selections")
    op.drop_table("single_selections")
    op.drop_table("free_weekly_samples")
