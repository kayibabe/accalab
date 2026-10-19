"""Role-filtered page data for the signed-in football tracking views."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from accalab.config import load_settings
from accalab.models import (
    Fixture,
    MarketRegistry,
    OddsSnapshot,
    Prediction,
    Result,
    Team,
    Ticket,
    TicketLeg,
    User,
)
from accalab.tracking.metrics import build_performance_report
from accalab.tracking.sample import select_weekly_sample


def _local_day_bounds(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


def _today(now: datetime, timezone_name: str) -> date:
    return now.astimezone(ZoneInfo(timezone_name)).date()


def _teams_fixtures(session: Session, *, day: date, timezone_name: str) -> list[dict]:
    start, end = _local_day_bounds(day, timezone_name)
    home = aliased(Team)
    away = aliased(Team)
    rows = session.execute(
        select(Fixture, home.name, away.name)
        .join(home, Fixture.home_team_id == home.id)
        .join(away, Fixture.away_team_id == away.id)
        .where(Fixture.kickoff_at >= start, Fixture.kickoff_at < end)
        .order_by(Fixture.kickoff_at, Fixture.id)
    ).all()
    return [
        {
            "fixture_id": fixture.id,
            "kickoff": fixture.kickoff_at.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M"),
            "kickoff_iso": fixture.kickoff_at.isoformat(),
            "kickoff_at": fixture.kickoff_at,
            "home": home_name,
            "away": away_name,
            "league": fixture.league_name,
            "status": fixture.status,
            "score": (
                f"{fixture.home_goals}–{fixture.away_goals}"
                if fixture.home_goals is not None and fixture.away_goals is not None
                else "—"
            ),
        }
        for fixture, home_name, away_name in rows
    ]


def _current_results(session: Session) -> dict[int, Result]:
    result_map: dict[int, Result] = {}
    for result in session.scalars(select(Result).order_by(Result.settled_at, Result.id)).all():
        result_map[result.ticket_leg_id] = result
    return result_map


def _ticket_card(
    session: Session,
    ticket: Ticket,
    *,
    role: str,
    is_sample: bool,
    results: dict[int, Result],
    timezone_name: str,
    now: datetime,
) -> dict:
    home = aliased(Team)
    away = aliased(Team)
    rows = session.execute(
        select(TicketLeg, Fixture, MarketRegistry, home.name, away.name)
        .join(Fixture, Fixture.id == TicketLeg.fixture_id)
        .join(MarketRegistry, MarketRegistry.id == TicketLeg.market_id)
        .join(home, Fixture.home_team_id == home.id)
        .join(away, Fixture.away_team_id == away.id)
        .where(TicketLeg.ticket_id == ticket.id)
        .order_by(Fixture.kickoff_at, TicketLeg.id)
    ).all()
    reveal = role != "free" or is_sample
    legs = []
    for leg, fixture, market, home_name, away_name in rows:
        settled = results.get(leg.id)
        item = {
            "fixture": f"{home_name} v {away_name}",
            "league": fixture.league_name,
            "kickoff": fixture.kickoff_at.astimezone(ZoneInfo(timezone_name)).strftime(
                "%d %b %H:%M"
            ),
            "market": market.display_name,
            "selection": leg.selection,
            "line": str(leg.line) if leg.line is not None else "",
            "odds": f"{leg.odds:.2f}",
            "result": settled.outcome.replace("_", " ").title() if settled else "Pending",
            "score": (
                f"{fixture.home_goals}–{fixture.away_goals}"
                if fixture.home_goals is not None and fixture.away_goals is not None
                else "—"
            ),
        }
        if role != "free":
            probability = float(leg.probability)
            price = float(leg.odds)
            item.update(
                {
                    "probability": f"{probability:.1%}",
                    "p_low": f"{float(leg.p_low):.1%}",
                    "ev": f"{probability * price - 1:+.1%}",
                    "why": leg.why,
                }
            )
        legs.append(item)
    locked = role == "free" and not is_sample and ticket.target_date >= _today(now, timezone_name)
    band_ranges = {
        "T1": "3.00–4.99",
        "T2": "5.00–9.99",
        "T3": "10.00–19.99",
        "T4": "20.00–49.99",
        "T5": "50.00+",
    }
    ticket_status = (
        ticket.result_status.replace("_", " ").title()
        if ticket.result_status
        else ticket.status.title()
    )
    return {
        "id": ticket.id,
        "target_date": ticket.target_date.isoformat(),
        "as_of": ticket.as_of.astimezone(ZoneInfo(timezone_name)).strftime("%d %b %Y %H:%M %Z"),
        "band": ticket.band,
        "tier": {
            "T1": "Core",
            "T2": "Core",
            "T3": "Conditional",
            "T4": "High odds",
            "T5": "High odds",
        }.get(ticket.band, "Other"),
        "leg_count": len(rows),
        "combined_odds": f"{ticket.combined_odds:.2f}",
        "locked_odds_range": band_ranges.get(ticket.band, "—"),
        "status": ticket_status,
        "run_type": ticket.status.title(),
        "locked": locked,
        "is_sample": is_sample,
        "legs": legs if reveal else [],
    }


def _tickets_for_page(session: Session, user: User, *, now: datetime, timezone_name: str) -> dict:
    today = _today(now, timezone_name)
    sample = select_weekly_sample(session, now=now) if user.role == "free" else None
    sample_id = sample.ticket_id if sample else None
    results = _current_results(session)
    query = select(Ticket).order_by(Ticket.target_date.desc(), Ticket.created_at.desc())
    tickets = session.scalars(query).all()
    if user.role == "free":
        min_day = today - timedelta(days=3)
        eligible = [
            ticket
            for ticket in tickets
            if (
                ticket.target_date == today
                or min_day <= ticket.target_date < today
                and ticket.result_status is not None
                or ticket.id == sample_id
            )
        ]
    else:
        eligible = tickets
    cards = [
        _ticket_card(
            session,
            ticket,
            role=user.role,
            is_sample=ticket.id == sample_id,
            results=results,
            timezone_name=timezone_name,
            now=now,
        )
        for ticket in eligible
    ]
    if user.role == "free":
        cards.sort(key=lambda card: (card["target_date"], card["is_sample"]), reverse=True)
    return {
        "ticket_cards": cards,
        "sample_rule": sample.selection_rule if sample else None,
        "sample_ticket_id": sample_id,
    }


def _pick_rows(session: Session, *, day: date, now: datetime, timezone_name: str) -> list[dict]:
    fixtures = [
        fixture
        for fixture in _teams_fixtures(session, day=day, timezone_name=timezone_name)
        if fixture["status"] in {"NS", "TBD"} and fixture["kickoff_at"] > now
    ]
    fixture_ids = [row["fixture_id"] for row in fixtures]
    if not fixture_ids:
        return []
    fixture_map = {row["fixture_id"]: row for row in fixtures}
    predictions = session.scalars(
        select(Prediction)
        .where(Prediction.fixture_id.in_(fixture_ids), Prediction.as_of <= now)
        .order_by(Prediction.as_of.desc(), Prediction.id.desc())
    ).all()
    odds = session.scalars(
        select(OddsSnapshot)
        .where(OddsSnapshot.fixture_id.in_(fixture_ids), OddsSnapshot.as_of <= now)
        .order_by(OddsSnapshot.as_of.desc(), OddsSnapshot.id.desc())
    ).all()
    odds_by_key = {}
    for quote in odds:
        key = (
            quote.fixture_id,
            quote.market_id,
            quote.selection.strip().casefold(),
            quote.line,
        )
        odds_by_key.setdefault(key, quote)
    prediction_by_key = {}
    for prediction in predictions:
        key = (
            prediction.fixture_id,
            prediction.market_id,
            prediction.selection.strip().casefold(),
            prediction.line,
        )
        prediction_by_key.setdefault(key, prediction)
    markets = {market.id: market for market in session.scalars(select(MarketRegistry)).all()}
    result = []
    for key, prediction in prediction_by_key.items():
        quote = odds_by_key.get(key)
        market = markets.get(prediction.market_id)
        fixture = fixture_map.get(prediction.fixture_id)
        if quote is None or market is None or fixture is None:
            continue
        probability = float(prediction.probability)
        odds_price = float(quote.odds)
        result.append(
            {
                "fixture": f"{fixture['home']} v {fixture['away']}",
                "league": fixture["league"],
                "kickoff": fixture["kickoff"],
                "market": market.display_name,
                "selection": (
                    f"{prediction.selection} · {prediction.line}"
                    if prediction.line is not None
                    else prediction.selection
                ),
                "odds": f"{odds_price:.2f}",
                "probability": f"{probability:.1%} / {float(prediction.p_low):.1%}"
                if prediction.p_low is not None
                else f"{probability:.1%}",
                "ev": f"{probability * odds_price - 1:+.1%}",
                "why": prediction.why or "—",
                "as_of": quote.as_of.astimezone(ZoneInfo(timezone_name)).strftime(
                    "%d %b %Y %H:%M %Z"
                ),
                "as_of_order": quote.as_of,
            }
        )
    return result


def build_page_context(
    session: Session,
    user: User,
    section: str,
    *,
    requested_date: str | None = None,
    requested_page: str | None = None,
    now: datetime | None = None,
) -> dict:
    settings = load_settings()
    timezone_name = settings.timezone
    now = now or datetime.now(UTC)
    today = _today(now, timezone_name)
    if user.role == "free" or not requested_date:
        day = today
    else:
        try:
            day = date.fromisoformat(requested_date)
        except ValueError:
            day = today
    data: dict = {
        "today": today.isoformat(),
        "selected_date": day.isoformat(),
        "timezone": timezone_name,
    }
    if section in {"today", "fixtures"}:
        data["fixtures"] = _teams_fixtures(session, day=day, timezone_name=timezone_name)
    if section in {"today", "tickets", "results"}:
        data.update(_tickets_for_page(session, user, now=now, timezone_name=timezone_name))
    if section == "picks":
        data["picks"] = _pick_rows(session, day=day, now=now, timezone_name=timezone_name)
        data["odds_as_of"] = max((row["as_of"] for row in data["picks"]), default=None)
    if section == "results":
        data["ticket_cards"] = [
            item for item in data["ticket_cards"] if item["status"] in {"Won", "Lost"}
        ]
        if user.role == "free":
            min_day = today - timedelta(days=3)
            data["ticket_cards"] = [
                item
                for item in data["ticket_cards"]
                if min_day <= date.fromisoformat(item["target_date"]) < today or item["is_sample"]
            ]
    if section == "performance":
        try:
            singles_page = max(1, int(requested_page or "1"))
        except ValueError:
            singles_page = 1
        report = build_performance_report(
            session,
            as_of=now,
            days=30 if user.role == "free" else None,
            timezone_name=timezone_name,
            include_details=user.role != "free",
            singles_page=singles_page,
        )
        data["performance"] = report
        if user.role == "free":
            return data
        legs = report["legs"]
        data["leg_health_rows"] = [
            {
                "metric": "Brier score",
                "value": f"{legs['brier']:.3f}" if legs["brier"] is not None else "—",
                "sample": legs["decisive_legs"],
            },
            {
                "metric": "Mean predicted probability",
                "value": f"{legs['mean_probability']:.1%}"
                if legs["mean_probability"] is not None
                else "—",
                "sample": legs["decisive_legs"],
            },
            {
                "metric": "Observed outcome rate",
                "value": f"{legs['observed_rate']:.1%}"
                if legs["observed_rate"] is not None
                else "—",
                "sample": legs["decisive_legs"],
            },
            {
                "metric": "Claimed EV per leg",
                "value": f"{legs['claimed_ev']:+.1%}" if legs["claimed_ev"] is not None else "—",
                "sample": legs["settled_legs"],
            },
            {
                "metric": "Realized return per leg",
                "value": f"{legs['realized_ev']:+.1%}" if legs["realized_ev"] is not None else "—",
                "sample": legs["settled_legs"],
            },
            {
                "metric": "Mean CLV · 95% range",
                "value": (
                    f"{legs['mean_clv']:+.1%} · {legs['clv_ci_low']:+.1%} to {legs['clv_ci_high']:+.1%}"
                    if legs["mean_clv"] is not None
                    else "—"
                ),
                "sample": legs["clv_legs"],
            },
        ]
        data["calibration_rows"] = [
            {
                "bucket": row["bucket"],
                "mean_probability": f"{row['mean_probability']:.1%}",
                "observed_rate": f"{row['observed_rate']:.1%}",
                "sample": row["sample"],
            }
            for row in legs["calibration"]
        ]
    return data
