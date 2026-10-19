from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from accalab.db import Base
from accalab.models import (
    Fixture,
    FreeWeeklySample,
    MarketRegistry,
    OddsSnapshot,
    Result,
    SingleSelection,
    Team,
    Ticket,
    TicketLeg,
)
from accalab.tracking.metrics import build_performance_report
from accalab.tracking.sample import select_weekly_sample
from accalab.tracking.settlement import settle_completed_tickets, settle_ticket


def _database() -> tuple[object, Session]:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def _seed_market(session: Session) -> None:
    session.add(
        MarketRegistry(
            id=1,
            canonical_name="1X2",
            display_name="Match Winner",
            supported=True,
            ticket_eligible=True,
        )
    )


def _seed_fixture(
    session: Session,
    *,
    fixture_id: int,
    home_id: int,
    away_id: int,
    kickoff: datetime,
    home_goals: int | None = None,
    away_goals: int | None = None,
    status: str = "NS",
) -> None:
    session.add_all(
        [
            Team(id=home_id, name=f"Home {home_id}"),
            Team(id=away_id, name=f"Away {away_id}"),
            Fixture(
                id=fixture_id,
                league_id=39,
                league_name="Test League",
                fixture_date=kickoff,
                kickoff_at=kickoff,
                home_team_id=home_id,
                away_team_id=away_id,
                status=status,
                home_goals=home_goals,
                away_goals=away_goals,
            ),
            OddsSnapshot(
                fixture_id=fixture_id,
                bookmaker_id=8,
                bookmaker_name="Book",
                market_id=1,
                market_name="Match Winner",
                selection="Home",
                odds=Decimal("2.00"),
                as_of=kickoff - timedelta(hours=3),
                raw={},
            ),
        ]
    )


def _seed_ticket(
    session: Session,
    *,
    ticket_id: str,
    target_date: date,
    fixture_ids: tuple[int, ...],
    band: str = "T1",
    status: str = "official",
    as_of: datetime,
    probabilities: tuple[Decimal, ...] | None = None,
) -> None:
    session.add(
        Ticket(
            id=ticket_id,
            target_date=target_date,
            status=status,
            band=band,
            combined_odds=Decimal("4.000000"),
            as_of=as_of,
        )
    )
    for index, fixture_id in enumerate(fixture_ids):
        odds_snapshot_id = session.scalar(
            select(OddsSnapshot.id)
            .where(OddsSnapshot.fixture_id == fixture_id, OddsSnapshot.selection == "Home")
            .limit(1)
        )
        session.add(
            TicketLeg(
                ticket_id=ticket_id,
                fixture_id=fixture_id,
                market_id=1,
                selection="Home",
                odds_snapshot_id=odds_snapshot_id,
                odds=Decimal("2.00"),
                probability=(probabilities or (Decimal("0.70"),) * len(fixture_ids))[index],
                p_low=Decimal("0.65"),
                why="Saved reason for this selection.",
            )
        )
    session.flush()


def test_settlement_is_idempotent_and_records_result_corrections() -> None:
    engine, session = _database()
    _seed_market(session)
    kickoff = datetime(2026, 10, 18, 12, tzinfo=UTC)
    _seed_fixture(
        session,
        fixture_id=10,
        home_id=1,
        away_id=2,
        kickoff=kickoff,
        home_goals=2,
        away_goals=0,
        status="FT",
    )
    _seed_fixture(
        session,
        fixture_id=11,
        home_id=3,
        away_id=4,
        kickoff=kickoff,
        home_goals=0,
        away_goals=1,
        status="FT",
    )
    _seed_ticket(
        session,
        ticket_id="ticket-1",
        target_date=kickoff.date(),
        fixture_ids=(10, 11),
        as_of=kickoff - timedelta(hours=4),
    )
    first_time = kickoff + timedelta(hours=2)

    first = settle_ticket(session, "ticket-1", settled_at=first_time)
    session.flush()
    assert first["new_results"] == 2
    assert first["result_status"] == "lost"

    repeated = settle_ticket(session, "ticket-1", settled_at=first_time + timedelta(minutes=1))
    assert repeated["new_results"] == 0
    assert (
        session.scalar(
            select(Result.id).where(Result.ticket_leg_id == 1).order_by(Result.id.desc())
        )
        == 1
    )

    corrected_fixture = session.get(Fixture, 11)
    corrected_fixture.home_goals = 2
    corrected_fixture.away_goals = 0
    corrected = settle_ticket(session, "ticket-1", settled_at=first_time + timedelta(hours=1))
    session.flush()
    assert corrected["new_results"] == 1
    assert corrected["corrections"] == 1
    assert corrected["result_status"] == "won"
    latest = session.scalar(
        select(Result)
        .where(Result.ticket_leg_id == 2)
        .order_by(Result.settled_at.desc(), Result.id.desc())
    )
    assert latest.correction_of_id is not None
    assert session.scalar(select(Ticket).where(Ticket.id == "ticket-1")).result_status == "won"
    session.close()
    engine.dispose()


def test_performance_report_excludes_previews_and_includes_singles_and_ci() -> None:
    engine, session = _database()
    _seed_market(session)
    kickoff = datetime(2026, 10, 10, 12, tzinfo=UTC)
    for fixture_id in (20, 21, 22):
        base_score = (2, 0) if fixture_id == 20 else (0, 1) if fixture_id == 21 else (2, 0)
        _seed_fixture(
            session,
            fixture_id=fixture_id,
            home_id=fixture_id * 2,
            away_id=fixture_id * 2 + 1,
            kickoff=kickoff,
            home_goals=base_score[0],
            away_goals=base_score[1],
            status="FT",
        )
    _seed_ticket(
        session,
        ticket_id="official-win",
        target_date=kickoff.date(),
        fixture_ids=(20,),
        as_of=kickoff - timedelta(hours=3),
    )
    for ticket_id, fixture_id in (("official-win", 20), ("official-loss", 21)):
        odds_snapshot_id = session.scalar(
            select(OddsSnapshot.id)
            .where(OddsSnapshot.fixture_id == fixture_id, OddsSnapshot.selection == "Home")
            .limit(1)
        )
        session.add(
            SingleSelection(
                target_date=kickoff.date(),
                run_type="official",
                fixture_id=fixture_id,
                market_id=1,
                selection="Home",
                line=None,
                odds_snapshot_id=odds_snapshot_id,
                odds=Decimal("2.00"),
                probability=Decimal("0.70"),
                p_low=Decimal("0.65"),
                as_of=kickoff - timedelta(hours=3),
                why="Single selection reason.",
            )
        )
    _seed_ticket(
        session,
        ticket_id="official-loss",
        target_date=kickoff.date() + timedelta(days=1),
        fixture_ids=(21,),
        as_of=kickoff - timedelta(hours=3),
    )
    _seed_ticket(
        session,
        ticket_id="preview-win",
        target_date=kickoff.date(),
        fixture_ids=(22,),
        status="preview",
        as_of=kickoff - timedelta(hours=3),
    )
    session.add(
        FreeWeeklySample(
            week_start=date(2026, 10, 5),
            ticket_id="official-win",
            selected_at=kickoff - timedelta(hours=3),
            selection_rule="saturday_then_next_available_day",
        )
    )
    for ticket_id in ("official-win", "official-loss", "preview-win"):
        settle_ticket(session, ticket_id, settled_at=kickoff + timedelta(hours=2))
    settle_completed_tickets(session, settled_at=kickoff + timedelta(hours=2))
    session.flush()

    report = build_performance_report(session, as_of=kickoff + timedelta(days=4))
    assert report["aggregate"]["tickets"] == 2
    assert {item["id"] for item in report["tickets"]} == {"official-win", "official-loss"}
    assert report["aggregate"]["staked"] == Decimal(2)
    assert report["aggregate"]["returned"] == Decimal(2)
    assert report["aggregate"]["net"] == Decimal(0)
    assert report["aggregate"]["roi"] == 0
    assert report["aggregate"]["roi_ci_low"] is not None
    assert report["aggregate"]["roi_ci_high"] is not None
    assert report["singles"]["tickets"] == 2
    assert report["legs"]["settled_legs"] == 2
    assert report["legs"]["clv_legs"] == 2
    assert report["by_day"][0]["roi_display"].endswith("%")
    assert "1,600" in report["sample_size_note"]
    session.close()
    engine.dispose()


def test_weekly_sample_chooses_configured_day_and_never_selects_after_kickoff() -> None:
    engine, session = _database()
    _seed_market(session)
    friday = datetime(2026, 10, 23, 17, tzinfo=UTC)
    saturday_kickoff = datetime(2026, 10, 24, 14, tzinfo=UTC)
    sunday_kickoff = datetime(2026, 10, 25, 14, tzinfo=UTC)
    _seed_fixture(session, fixture_id=30, home_id=61, away_id=62, kickoff=saturday_kickoff)
    _seed_fixture(
        session,
        fixture_id=31,
        home_id=63,
        away_id=64,
        kickoff=saturday_kickoff + timedelta(hours=1),
    )
    _seed_fixture(session, fixture_id=32, home_id=65, away_id=66, kickoff=sunday_kickoff)
    _seed_fixture(
        session, fixture_id=33, home_id=67, away_id=68, kickoff=sunday_kickoff + timedelta(hours=1)
    )
    _seed_ticket(
        session,
        ticket_id="saturday-t1",
        target_date=date(2026, 10, 24),
        fixture_ids=(30, 31),
        as_of=friday,
    )
    _seed_ticket(
        session,
        ticket_id="sunday-t1",
        target_date=date(2026, 10, 25),
        fixture_ids=(32, 33),
        as_of=friday,
    )

    sample = select_weekly_sample(session, now=friday)
    assert sample is not None
    assert sample.ticket_id == "saturday-t1"
    assert sample.selection_rule == "saturday_then_next_available_day"
    assert session.get(FreeWeeklySample, date(2026, 10, 19)).ticket_id == "saturday-t1"

    fallback_engine, fallback_session = _database()
    _seed_market(fallback_session)
    _seed_fixture(fallback_session, fixture_id=35, home_id=71, away_id=72, kickoff=sunday_kickoff)
    _seed_fixture(
        fallback_session,
        fixture_id=36,
        home_id=73,
        away_id=74,
        kickoff=sunday_kickoff + timedelta(hours=1),
    )
    _seed_ticket(
        fallback_session,
        ticket_id="sunday-fallback",
        target_date=date(2026, 10, 25),
        fixture_ids=(35, 36),
        as_of=friday,
    )
    fallback = select_weekly_sample(fallback_session, now=datetime(2026, 10, 24, 16, tzinfo=UTC))
    assert fallback is not None
    assert fallback.ticket_id == "sunday-fallback"

    other_engine, other_session = _database()
    _seed_market(other_session)
    _seed_fixture(other_session, fixture_id=40, home_id=81, away_id=82, kickoff=saturday_kickoff)
    _seed_fixture(
        other_session,
        fixture_id=41,
        home_id=83,
        away_id=84,
        kickoff=saturday_kickoff + timedelta(hours=1),
    )
    _seed_ticket(
        other_session,
        ticket_id="late-t1",
        target_date=date(2026, 10, 24),
        fixture_ids=(40, 41),
        as_of=friday,
    )
    assert select_weekly_sample(other_session, now=saturday_kickoff + timedelta(minutes=1)) is None
    session.close()
    engine.dispose()
    fallback_session.close()
    fallback_engine.dispose()
    other_session.close()
    other_engine.dispose()
