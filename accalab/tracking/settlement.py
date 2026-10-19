"""Apply final fixture results to tickets while retaining correction history."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from accalab.models import (
    DataSnapshot,
    Fixture,
    MarketRegistry,
    Result,
    SingleResult,
    SingleSelection,
    Team,
    Ticket,
    TicketLeg,
)
from accalab.settlement.engine import (
    MissingSettlementData,
    Outcome,
    UnsupportedMarket,
    settle_market,
)

FINAL_STATUSES = {"FT", "AET", "PEN", "AWD", "WO"}
VOID_STATUSES = {"CANC", "ABD"}


def _latest_result(session: Session, leg_id: int) -> Result | None:
    return session.scalar(
        select(Result)
        .where(Result.ticket_leg_id == leg_id)
        .order_by(Result.settled_at.desc(), Result.id.desc())
        .limit(1)
    )


def _fixture_stats(session: Session, fixture_id: int) -> dict[str, int | float | None]:
    snapshot = session.scalar(
        select(DataSnapshot)
        .where(
            DataSnapshot.data_type == "fixture_statistics",
            DataSnapshot.scope_type == "fixture",
            DataSnapshot.scope_key == str(fixture_id),
        )
        .order_by(DataSnapshot.as_of.desc(), DataSnapshot.id.desc())
        .limit(1)
    )
    if snapshot is None or not isinstance(snapshot.payload, list):
        return {}
    result: dict[str, int | float | None] = {}
    fixture = session.get(Fixture, fixture_id)
    if fixture is None:
        return {}
    home = session.get(Team, fixture.home_team_id)
    away = session.get(Team, fixture.away_team_id)
    side_by_name = {}
    if home is not None:
        side_by_name[home.name.casefold()] = "home"
    if away is not None:
        side_by_name[away.name.casefold()] = "away"
    labels = {
        "corner kicks": "corners",
        "yellow cards": "yellow_cards",
        "total yellow cards": "yellow_cards",
        "red cards": "red_cards",
    }
    for item in snapshot.payload:
        if not isinstance(item, dict):
            continue
        side = side_by_name.get(str((item.get("team") or {}).get("name", "")).casefold())
        if side is None:
            continue
        for stat in item.get("statistics", []):
            if not isinstance(stat, dict):
                continue
            stat_name = labels.get(str(stat.get("type", "")).casefold())
            if not stat_name:
                continue
            raw_value = stat.get("value")
            try:
                value = float(str(raw_value).replace("%", "")) if raw_value is not None else None
            except ValueError:
                value = None
            result[f"{side}_{stat_name}"] = value
    for side in ("home", "away"):
        yellow, red = result.get(f"{side}_yellow_cards"), result.get(f"{side}_red_cards")
        result[f"{side}_cards"] = yellow + red if yellow is not None and red is not None else None
    return result


def _settle_leg(session: Session, leg: TicketLeg, settled_at: datetime) -> Result | None:
    fixture = session.get(Fixture, leg.fixture_id)
    market = session.get(MarketRegistry, leg.market_id)
    if fixture is None or market is None:
        return None
    status = fixture.status.upper()
    if status not in FINAL_STATUSES | VOID_STATUSES:
        return None
    try:
        settlement = settle_market(
            market.canonical_name,
            leg.selection,
            home_goals=fixture.home_goals,
            away_goals=fixture.away_goals,
            halftime_home_goals=fixture.halftime_home_goals,
            halftime_away_goals=fixture.halftime_away_goals,
            line=leg.line,
            odds=leg.odds,
            stats=_fixture_stats(session, fixture.id),
            void=status in VOID_STATUSES,
        )
    except (MissingSettlementData, UnsupportedMarket, ValueError):
        return None
    latest = _latest_result(session, leg.id)
    if (
        latest is not None
        and latest.outcome == settlement.outcome.value
        and latest.payout_multiplier == settlement.payout_multiplier
    ):
        return latest
    result = Result(
        ticket_leg_id=leg.id,
        fixture_id=fixture.id,
        outcome=settlement.outcome.value,
        payout_multiplier=settlement.payout_multiplier,
        settled_at=settled_at,
        correction_of_id=latest.id if latest else None,
        raw={"fixture_status": status, "market": market.canonical_name},
    )
    session.add(result)
    session.flush()
    return result


def _latest_single_result(session: Session, single_id: int) -> SingleResult | None:
    return session.scalar(
        select(SingleResult)
        .where(SingleResult.single_selection_id == single_id)
        .order_by(SingleResult.settled_at.desc(), SingleResult.id.desc())
        .limit(1)
    )


def _settle_single(
    session: Session, single: SingleSelection, settled_at: datetime
) -> tuple[bool, bool]:
    fixture = session.get(Fixture, single.fixture_id)
    market = session.get(MarketRegistry, single.market_id)
    if fixture is None or market is None:
        return False, False
    status = fixture.status.upper()
    if status not in FINAL_STATUSES | VOID_STATUSES:
        return False, False
    try:
        settlement = settle_market(
            market.canonical_name,
            single.selection,
            home_goals=fixture.home_goals,
            away_goals=fixture.away_goals,
            halftime_home_goals=fixture.halftime_home_goals,
            halftime_away_goals=fixture.halftime_away_goals,
            line=single.line,
            odds=single.odds,
            stats=_fixture_stats(session, fixture.id),
            void=status in VOID_STATUSES,
        )
    except (MissingSettlementData, UnsupportedMarket, ValueError):
        return False, False
    latest = _latest_single_result(session, single.id)
    if (
        latest is not None
        and latest.outcome == settlement.outcome.value
        and latest.payout_multiplier == settlement.payout_multiplier
    ):
        return False, False
    session.add(
        SingleResult(
            single_selection_id=single.id,
            outcome=settlement.outcome.value,
            payout_multiplier=settlement.payout_multiplier,
            settled_at=settled_at,
            correction_of_id=latest.id if latest else None,
            raw={"fixture_status": status, "market": market.canonical_name},
        )
    )
    session.flush()
    return True, latest is not None


def _update_ticket_status(session: Session, ticket: Ticket) -> bool:
    legs = session.scalars(select(TicketLeg).where(TicketLeg.ticket_id == ticket.id)).all()
    if not legs:
        return False
    results = [_latest_result(session, leg.id) for leg in legs]
    if any(result is None for result in results):
        return False
    current = [result for result in results if result is not None]
    outcomes = {result.outcome for result in current}
    if outcomes & {Outcome.LOSS.value, Outcome.HALF_LOSS.value}:
        status = "lost"
    else:
        status = "won"
    changed = ticket.result_status != status
    ticket.result_status = status
    return changed


def settle_ticket(session: Session, ticket_id: str, *, settled_at: datetime | None = None) -> dict:
    """Settle a ticket from its current final fixture facts; safe to rerun."""
    settled_at = settled_at or datetime.now(UTC)
    if settled_at.tzinfo is None:
        raise ValueError("settled_at must include a timezone.")
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise ValueError(f"Ticket {ticket_id!r} does not exist.")
    legs = session.scalars(select(TicketLeg).where(TicketLeg.ticket_id == ticket.id)).all()
    new_results = 0
    corrections = 0
    for leg in legs:
        prior = _latest_result(session, leg.id)
        result = _settle_leg(session, leg, settled_at)
        if result is not None and result is not prior:
            new_results += 1
            corrections += int(result.correction_of_id is not None)
    status_changed = _update_ticket_status(session, ticket)
    return {
        "ticket_id": ticket.id,
        "result_status": ticket.result_status,
        "new_results": new_results,
        "corrections": corrections,
        "status_changed": status_changed,
    }


def settle_completed_tickets(session: Session, *, settled_at: datetime | None = None) -> dict:
    """Settle every ticket with newly available final fixture data."""
    ids = session.scalars(
        select(Ticket.id).order_by(Ticket.target_date, Ticket.band, Ticket.id)
    ).all()
    updates = [settle_ticket(session, ticket_id, settled_at=settled_at) for ticket_id in ids]
    at = settled_at or datetime.now(UTC)
    singles = session.scalars(
        select(SingleSelection).where(SingleSelection.run_type == "official")
    ).all()
    single_changes = [_settle_single(session, single, at) for single in singles]
    return {
        "tickets_checked": len(updates),
        "tickets_settled": sum(update["result_status"] is not None for update in updates),
        "new_results": sum(update["new_results"] for update in updates),
        "corrections": sum(update["corrections"] for update in updates),
        "singles_settled": sum(changed for changed, _ in single_changes),
        "single_corrections": sum(corrected for _, corrected in single_changes),
    }
