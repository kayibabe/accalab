"""Store agent scope, bounded effects, tokens and evaluation inputs."""

from alembic import op
import sqlalchemy as sa


revision = "b5d2c14f8103"
down_revision = "7a15c6b0d4e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_logs") as batch_op:
        batch_op.add_column(sa.Column("prediction_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("market_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("selection", sa.String(length=160), nullable=True))
        batch_op.add_column(sa.Column("line", sa.Numeric(precision=8, scale=3), nullable=True))
        batch_op.add_column(sa.Column("baseline_probability", sa.Numeric(precision=8, scale=7), nullable=True))
        batch_op.add_column(sa.Column("adjusted_probability", sa.Numeric(precision=8, scale=7), nullable=True))
        batch_op.add_column(sa.Column("reference_odds", sa.Numeric(precision=12, scale=5), nullable=True))
        batch_op.add_column(sa.Column("reference_bookmaker_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("model_id", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("status", sa.String(length=24), nullable=False, server_default="ok"))
        batch_op.add_column(sa.Column("error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("why_this_pick", sa.String(length=140), nullable=True))
        batch_op.create_foreign_key("fk_agent_logs_prediction_id_predictions", "predictions", ["prediction_id"], ["id"])
        batch_op.create_foreign_key("fk_agent_logs_market_id_markets_registry", "markets_registry", ["market_id"], ["id"])
        batch_op.alter_column("status", server_default=None)
    op.create_index(
        "ix_agent_logs_fixture_market_asof", "agent_logs", ["fixture_id", "market_id", "as_of"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_logs_fixture_market_asof", table_name="agent_logs")
    with op.batch_alter_table("agent_logs") as batch_op:
        batch_op.drop_constraint("fk_agent_logs_market_id_markets_registry", type_="foreignkey")
        batch_op.drop_constraint("fk_agent_logs_prediction_id_predictions", type_="foreignkey")
        batch_op.drop_column("why_this_pick")
        batch_op.drop_column("error")
        batch_op.drop_column("status")
        batch_op.drop_column("model_id")
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")
        batch_op.drop_column("reference_bookmaker_id")
        batch_op.drop_column("reference_odds")
        batch_op.drop_column("adjusted_probability")
        batch_op.drop_column("baseline_probability")
        batch_op.drop_column("line")
        batch_op.drop_column("selection")
        batch_op.drop_column("market_id")
        batch_op.drop_column("prediction_id")
