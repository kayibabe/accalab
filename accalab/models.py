"""Core persistence models. Provider payloads are retained alongside normalized fields."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from accalab.db import Base, UTCDateTime


def utc_now() -> datetime:
    return datetime.now(UTC)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    country: Mapped[str | None] = mapped_column(String(100))
    logo_url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class Fixture(Base):
    __tablename__ = "fixtures"
    __table_args__ = (
        CheckConstraint("home_team_id != away_team_id", name="ck_fixtures_distinct_teams"),
        Index("ix_fixtures_kickoff", "kickoff_at"),
        Index("ix_fixtures_league_date", "league_id", "fixture_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # API-Football fixture ID
    league_id: Mapped[int] = mapped_column(Integer, nullable=False)
    league_name: Mapped[str] = mapped_column(String(160), nullable=False)
    country: Mapped[str | None] = mapped_column(String(100))
    season: Mapped[int | None] = mapped_column(Integer)
    fixture_date: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    kickoff_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    timezone: Mapped[str | None] = mapped_column(String(80))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    venue: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NS")
    elapsed: Mapped[int | None] = mapped_column(Integer)
    home_goals: Mapped[int | None] = mapped_column(Integer)
    away_goals: Mapped[int | None] = mapped_column(Integer)
    halftime_home_goals: Mapped[int | None] = mapped_column(Integer)
    halftime_away_goals: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class MarketRegistry(Base):
    __tablename__ = "markets_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # API-Football bet ID
    canonical_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ticket_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class DataSnapshot(Base):
    __tablename__ = "data_snapshots"
    __table_args__ = (Index("ix_data_snapshots_type_asof", "data_type", "as_of"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data_type: Mapped[str] = mapped_column(String(40), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict | list] = mapped_column(JSON, nullable=False)


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "fixture_id",
            "bookmaker_id",
            "market_id",
            "selection",
            "line",
            "as_of",
            name="uq_odds_snapshot_capture",
        ),
        Index("ix_odds_snapshot_fixture_asof", "fixture_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), nullable=False)
    bookmaker_id: Mapped[int | None] = mapped_column(Integer)
    bookmaker_name: Mapped[str] = mapped_column(String(120), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets_registry.id"), nullable=False)
    market_name: Mapped[str] = mapped_column(String(160), nullable=False)
    selection: Mapped[str] = mapped_column(String(160), nullable=False)
    line: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    odds: Mapped[Decimal] = mapped_column(Numeric(12, 5), nullable=False)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    provider_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (Index("ix_predictions_fixture_asof", "fixture_id", "as_of"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets_registry.id"), nullable=False)
    selection: Mapped[str] = mapped_column(String(160), nullable=False)
    line: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    probability: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    p_low: Mapped[Decimal | None] = mapped_column(Numeric(8, 7))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    why: Mapped[str | None] = mapped_column(String(140))
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_target_date_status", "target_date", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # preview or official
    band: Mapped[str] = mapped_column(String(4), nullable=False)
    combined_odds: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    result_status: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class TicketLeg(Base):
    __tablename__ = "ticket_legs"
    __table_args__ = (
        UniqueConstraint("ticket_id", "fixture_id", name="uq_ticket_one_leg_per_fixture"),
        Index("ix_ticket_legs_fixture", "fixture_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets_registry.id"), nullable=False)
    selection: Mapped[str] = mapped_column(String(160), nullable=False)
    line: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    odds_snapshot_id: Mapped[int] = mapped_column(ForeignKey("odds_snapshots.id"), nullable=False)
    odds: Mapped[Decimal] = mapped_column(Numeric(12, 5), nullable=False)
    probability: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    p_low: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    why: Mapped[str] = mapped_column(String(140), nullable=False)


class Result(Base):
    __tablename__ = "results"
    __table_args__ = (Index("ix_results_ticket_leg_settled", "ticket_leg_id", "settled_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_leg_id: Mapped[int] = mapped_column(ForeignKey("ticket_legs.id"), nullable=False)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    payout_multiplier: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    settled_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    correction_of_id: Mapped[int | None] = mapped_column(ForeignKey("results.id"))
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(40), nullable=False)
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    input_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    adjustment: Mapped[Decimal | None] = mapped_column(Numeric(8, 7))
    veto_reason: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="free")
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plan_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    plan_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AuthToken(Base):
    """Hashed, single-use verification and password-reset tokens."""

    __tablename__ = "auth_tokens"
    __table_args__ = (Index("ix_auth_tokens_user_purpose", "user_id", "purpose"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class LoginRateLimit(Base):
    """Persistent failed-login counters keyed by a one-way email digest."""

    __tablename__ = "login_rate_limits"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(160))
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


@event.listens_for(OddsSnapshot, "before_update")
def _immutable_snapshot_update(_mapper, _connection, _target) -> None:
    raise ValueError("Odds snapshots are append-only and cannot be updated.")


@event.listens_for(OddsSnapshot, "before_delete")
def _immutable_snapshot_delete(_mapper, _connection, _target) -> None:
    raise ValueError("Odds snapshots are append-only and cannot be deleted.")
