from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hypothesis import given, settings, strategies as st
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from accalab.db import Base
from accalab.modeling.config import load_model_config
from accalab.modeling.value import assess_leg
from accalab.models import (
    DataSnapshot,
    Fixture,
    MarketRegistry,
    OddsSnapshot,
    Prediction,
    SingleSelection,
    Team,
    Ticket,
    TicketLeg,
)
from accalab.tickets.builder import (
    BANDS,
    BuilderConfig,
    CandidateLeg,
    DayTypeConfig,
    build_day,
    build_and_persist_day,
)


TARGET = date(2026, 10, 20)  # Tuesday
AS_OF = datetime(2026, 10, 19, 12, tzinfo=UTC)
KICKOFF = datetime(2026, 10, 20, 16, tzinfo=UTC)
ALL_BANDS = tuple(band.name for band in BANDS)


def config(*, enabled: tuple[str, ...] = ALL_BANDS, minimum_pool: int = 1) -> BuilderConfig:
    day = DayTypeConfig(minimum_pool=minimum_pool, enabled_bands=enabled)
    return BuilderConfig(
        timezone="Africa/Blantyre",
        log_scale=100_000,
        solver_max_seconds=2.0,
        weekday=day,
        weekend=day,
    )


def leg(index: int, odds: Decimal = Decimal("1.80"), *, fixture: int | None = None) -> CandidateLeg:
    return CandidateLeg(
        fixture_id=fixture if fixture is not None else index,
        market_id=1,
        selection=f"home_{index}",
        line=None,
        odds_snapshot_id=index,
        odds=odds,
        probability=0.95,
        p_low=0.90,
        overround=0.05,
        market_ticket_eligible=True,
        kickoff_at=KICKOFF,
        prediction_as_of=AS_OF - timedelta(minutes=10),
        odds_as_of=AS_OF - timedelta(minutes=5),
        why="Calibrated edge with a current price.",
    )


@given(
    odds_cents=st.lists(st.integers(min_value=130, max_value=220), min_size=1, max_size=28),
)
@settings(max_examples=25, deadline=None)
def test_builder_properties_for_generated_candidate_pools(odds_cents: list[int]) -> None:
    candidates = [
        leg(i, Decimal(value) / Decimal(100)) for i, value in enumerate(odds_cents, start=1)
    ]
    result = build_day(
        target_date=TARGET,
        as_of=AS_OF,
        run_type="preview",
        candidates=candidates,
        config=config(),
        model_config=load_model_config(),
    )

    assert [ticket.band for ticket in result.tickets] == sorted(
        (ticket.band for ticket in result.tickets), key=lambda name: int(name[1:])
    )
    seen_keys = set()
    for ticket in result.tickets:
        band = next(item for item in BANDS if item.name == ticket.band)
        assert band.contains(ticket.combined_odds)
        assert ticket.legs
        assert len({candidate.fixture_id for candidate in ticket.legs}) == len(ticket.legs)
        for candidate in ticket.legs:
            assessment = assess_leg(
                probability=candidate.probability,
                p_low=candidate.p_low,
                odds=float(candidate.odds),
                overround=candidate.overround,
                market_ticket_eligible=candidate.market_ticket_eligible,
                config=load_model_config(),
            )
            assert assessment.eligible
            assert (
                load_model_config().minimum_odds
                <= float(candidate.odds)
                <= load_model_config().maximum_odds
            )
            assert candidate.key not in seen_keys
            seen_keys.add(candidate.key)


def test_bands_fill_in_priority_order_and_remove_legs_globally() -> None:
    candidates = [leg(i) for i in range(1, 31)]
    result = build_day(
        target_date=TARGET,
        as_of=AS_OF,
        run_type="official",
        candidates=candidates,
        config=config(),
    )
    assert [ticket.band for ticket in result.tickets] == ["T1", "T2", "T3", "T4", "T5"]
    used = [candidate.key for ticket in result.tickets for candidate in ticket.legs]
    assert len(used) == len(set(used))
    assert result.run_type == "official"


def test_one_leg_per_fixture_and_day_pool_reason() -> None:
    candidates = [leg(1, fixture=77), leg(2, fixture=77), leg(3, fixture=78)]
    result = build_day(
        target_date=TARGET,
        as_of=AS_OF,
        run_type="preview",
        candidates=candidates,
        config=config(enabled=("T1",), minimum_pool=4),
    )
    assert not result.tickets
    assert result.diagnostics["bands"]["T1"]["status"] == "no_ticket"
    assert "3 legs; 4 required" in result.diagnostics["bands"]["T1"]["reason"]


def test_filters_future_evidence_and_existing_day_legs() -> None:
    stale = leg(1)
    future_prediction = CandidateLeg(
        **{**stale.__dict__, "prediction_as_of": AS_OF + timedelta(seconds=1)}
    )
    result = build_day(
        target_date=TARGET,
        as_of=AS_OF,
        run_type="preview",
        candidates=[stale, future_prediction],
        config=config(enabled=("T1",)),
        already_used=[stale.key],
    )
    assert not result.tickets
    assert result.eligible_legs == (stale,)
    assert result.diagnostics["rejections"] == {
        "already_used_today": 1,
        "evidence_after_run_as_of": 1,
    }


def test_disabled_bands_and_weekend_configuration_are_reported() -> None:
    saturday = date(2026, 10, 24)
    result = build_day(
        target_date=saturday,
        as_of=AS_OF,
        run_type="preview",
        candidates=[leg(1)],
        config=config(enabled=("T1",)),
    )
    assert result.day_type == "weekend"
    assert result.diagnostics["bands"]["T2"]["status"] == "disabled"


def test_database_build_derives_market_overround_and_persists_run_diagnostics() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Team(id=1, name="Home"),
                Team(id=2, name="Away"),
                Team(id=3, name="Home 2"),
                Team(id=4, name="Away 2"),
                Team(id=5, name="Home 3"),
                Team(id=6, name="Away 3"),
            ]
        )
        session.add(
            MarketRegistry(
                id=1, canonical_name="1X2", display_name="Match Winner", ticket_eligible=True
            )
        )
        for fixture_id, home_id, away_id in ((10, 1, 2), (11, 3, 4), (12, 5, 6)):
            session.add(
                Fixture(
                    id=fixture_id,
                    league_id=39,
                    league_name="Test League",
                    fixture_date=KICKOFF,
                    kickoff_at=KICKOFF,
                    home_team_id=home_id,
                    away_team_id=away_id,
                    status="NS",
                )
            )
            for selection in ("Home", "Away"):
                session.add(
                    OddsSnapshot(
                        fixture_id=fixture_id,
                        bookmaker_id=8,
                        bookmaker_name="Book",
                        market_id=1,
                        market_name="Match Winner",
                        selection=selection,
                        odds=Decimal("2.00"),
                        as_of=AS_OF - timedelta(minutes=5),
                        raw={},
                    )
                )
            session.add(
                Prediction(
                    fixture_id=fixture_id,
                    market_id=1,
                    selection="Home",
                    probability=Decimal("0.95"),
                    p_low=Decimal("0.90"),
                    as_of=AS_OF - timedelta(minutes=10),
                    model_version="test",
                    why="Calibrated edge with a current price.",
                )
            )
        session.commit()

        result = build_and_persist_day(
            session,
            target_date=TARGET,
            as_of=AS_OF,
            run_type="official",
            config=config(enabled=("T1",)),
        )
        session.commit()
        assert [ticket.band for ticket in result.tickets] == ["T1"]
        assert len(result.tickets[0].legs) == 2
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1
        assert session.scalar(select(func.count()).select_from(TicketLeg)) == 2
        assert session.scalar(select(func.count()).select_from(SingleSelection)) == 3
        diagnostics = session.scalar(
            select(DataSnapshot).where(DataSnapshot.data_type == "builder_diagnostics")
        )
        assert diagnostics is not None
        assert diagnostics.payload["bands"]["T1"]["status"] == "built"
    engine.dispose()
