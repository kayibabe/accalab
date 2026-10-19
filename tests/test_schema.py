from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_migration_creates_schema_with_append_only_snapshot_triggers(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "migrated.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    command.upgrade(config, "head")
    tables = set(inspect(engine).get_table_names())
    assert {
        "fixtures",
        "teams",
        "odds_snapshots",
        "markets_registry",
        "predictions",
        "tickets",
        "free_weekly_samples",
        "single_selections",
        "single_results",
        "model_versions",
        "ticket_legs",
        "results",
        "agent_logs",
        "users",
        "audit_log",
        "data_snapshots",
        "auth_tokens",
        "login_rate_limits",
        "alembic_version",
    } <= tables
    assert "session_version" in {column["name"] for column in inspect(engine).get_columns("users")}
    agent_columns = {column["name"] for column in inspect(engine).get_columns("agent_logs")}
    assert {
        "prediction_id",
        "market_id",
        "baseline_probability",
        "adjusted_probability",
        "input_tokens",
        "output_tokens",
        "status",
        "why_this_pick",
    } <= agent_columns
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO teams (id, name, raw, updated_at) VALUES (10, 'H', '{}', '2026-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO teams (id, name, raw, updated_at) VALUES (20, 'A', '{}', '2026-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fixtures (id, league_id, league_name, fixture_date, kickoff_at, "
                "home_team_id, away_team_id, status, raw, updated_at) "
                "VALUES (1, 39, 'League', '2026-01-01', '2026-01-01', 10, 20, 'NS', '{}', '2026-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO markets_registry (id, canonical_name, display_name, supported, "
                "ticket_eligible, raw, discovered_at, updated_at) "
                "VALUES (1, '1X2', 'Match Winner', 1, 1, '{}', '2026-01-01', '2026-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO odds_snapshots (fixture_id, bookmaker_name, market_id, market_name, "
                "selection, odds, as_of, raw) VALUES (1, 'Book', 1, 'Match Winner', 'Home', 2, "
                "'2026-01-01', '{}')"
            )
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("UPDATE odds_snapshots SET odds=3 WHERE fixture_id=1"))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM odds_snapshots WHERE fixture_id=1"))
    engine.dispose()
