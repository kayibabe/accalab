"""Ticket, single, and leg-level performance summaries."""

from __future__ import annotations

import random
from hashlib import sha256
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import mean
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from accalab.models import (
    Fixture,
    MarketRegistry,
    OddsSnapshot,
    Result,
    SingleResult,
    SingleSelection,
    Team,
    Ticket,
    TicketLeg,
)

SAMPLE_SIZE_NOTE = (
    "For a 5% ROI at about two standard deviations, a rough sample-size guide is "
    "1,600 × the average combined odds in the tickets."
)
LOSING_OUTCOMES = {"loss", "half_loss"}
NON_DECISIVE = {"push", "void"}


def _observed_outcome(outcome: str) -> float:
    if outcome == "win":
        return 1.0
    if outcome in {"half_win", "half_loss"}:
        return 0.5
    return 0.0


def _latest_results(session: Session) -> dict[int, Result]:
    latest: dict[int, Result] = {}
    rows = session.scalars(select(Result).order_by(Result.settled_at, Result.id)).all()
    for row in rows:
        latest[row.ticket_leg_id] = row
    return latest


def _ticket_return(legs: list[TicketLeg], results: dict[int, Result]) -> Decimal | None:
    if not legs or any(leg.id not in results for leg in legs):
        return None
    returned = Decimal(1)
    for leg in legs:
        returned *= results[leg.id].payout_multiplier
    return returned


def _tier(band: str) -> str:
    return {
        "T1": "core",
        "T2": "core",
        "T3": "conditional",
        "T4": "high odds",
        "T5": "high odds",
    }.get(band, "other")


def _bootstrap_roi(values: list[Decimal], *, seed: int) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    rng = random.Random(seed)
    count = len(values)
    returns = [float(value) for value in values]
    samples = []
    for _ in range(1000):
        samples.append(sum(rng.choices(returns, k=count)) / count - 1.0)
    samples.sort()
    return samples[24], samples[974]


def _bootstrap_mean(values: list[float], *, seed: int) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    rng = random.Random(seed)
    count = len(values)
    samples = sorted(mean(rng.choices(values, k=count)) for _ in range(1000))
    return samples[24], samples[974]


def _summary(items: list[dict], *, key: str) -> dict:
    returns = [item["returned"] for item in items]
    count = len(returns)
    returned = sum(returns, Decimal(0))
    staked = Decimal(count)
    net = returned - staked
    roi = float(net / staked) if staked else None
    digest = sha256(f"{key}:{count}".encode()).digest()
    seed = int.from_bytes(digest[:4], "big")
    low, high = _bootstrap_roi(returns, seed=seed)
    average_odds = mean(float(item["combined_odds"]) for item in items) if items else None
    note = SAMPLE_SIZE_NOTE
    if average_odds is not None:
        note = f"{SAMPLE_SIZE_NOTE} Average odds in this group: {average_odds:.2f}."
    return {
        "tickets": count,
        "staked": staked,
        "returned": returned,
        "hit_rate": sum(item["result_status"] == "won" for item in items) / count
        if count
        else None,
        "net": net,
        "roi": roi,
        "roi_ci_low": low,
        "roi_ci_high": high,
        "sample_note": note,
        "average_odds": average_odds,
    }


def _current_data(session: Session) -> tuple[list[dict], list[dict]]:
    results = _latest_results(session)
    legs_by_ticket: dict[str, list[TicketLeg]] = defaultdict(list)
    legs = session.scalars(select(TicketLeg)).all()
    for leg in legs:
        legs_by_ticket[leg.ticket_id].append(leg)

    ticket_rows = []
    settled_leg_rows = []
    tickets = session.scalars(select(Ticket).where(Ticket.status == "official")).all()
    fixtures: dict[int, Fixture] = {}
    for ticket in tickets:
        ticket_legs = legs_by_ticket.get(ticket.id, [])
        returned = _ticket_return(ticket_legs, results)
        if returned is None:
            continue
        result_rows = [results[leg.id] for leg in ticket_legs]
        outcome_set = {row.outcome for row in result_rows}
        status = "lost" if outcome_set & LOSING_OUTCOMES else "won"
        ticket_rows.append(
            {
                "id": ticket.id,
                "target_date": ticket.target_date,
                "band": ticket.band,
                "tier": _tier(ticket.band),
                "combined_odds": Decimal(ticket.combined_odds),
                "returned": returned,
                "result_status": status,
                "legs": ticket_legs,
            }
        )
    single_results: dict[int, SingleResult] = {}
    for result in session.scalars(
        select(SingleResult).order_by(SingleResult.settled_at, SingleResult.id)
    ).all():
        single_results[result.single_selection_id] = result
    for single in session.scalars(
        select(SingleSelection).where(SingleSelection.run_type == "official")
    ).all():
        result = single_results.get(single.id)
        if result is None:
            continue
        fixture = fixtures.get(single.fixture_id)
        if fixture is None:
            fixture = session.get(Fixture, single.fixture_id)
            if fixture is not None:
                fixtures[single.fixture_id] = fixture
        market = session.get(MarketRegistry, single.market_id)
        home = session.get(Team, fixture.home_team_id) if fixture is not None else None
        away = session.get(Team, fixture.away_team_id) if fixture is not None else None
        settled_leg_rows.append(
            {
                "ticket_id": None,
                "leg": single,
                "result": result,
                "fixture": fixture,
                "fixture_name": f"{home.name if home else 'Home'} v {away.name if away else 'Away'}",
                "market_name": market.display_name if market else str(single.market_id),
                "band": None,
                "target_date": single.target_date,
            }
        )
    return ticket_rows, settled_leg_rows


def _group_rows(ticket_rows: list[dict], field: str, formatter) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in ticket_rows:
        grouped[formatter(row)].append(row)
    return [
        {"period": group, **_summary(items, key=f"{field}:{group}")}
        for group, items in sorted(grouped.items())
    ]


def _decorate_metric_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        for field in ("hit_rate", "roi", "roi_ci_low", "roi_ci_high"):
            value = row[field]
            row[f"{field}_display"] = f"{value:.1%}" if value is not None else "—"
            row[f"{field}_sort"] = value if value is not None else -1
        low, high = row["roi_ci_low"], row["roi_ci_high"]
        row["roi_ci"] = f"{low:.1%} to {high:.1%}" if low is not None and high is not None else "—"
        row["roi_ci_sort"] = low if low is not None else -1
        roi = row["roi"]
        row["net_roi"] = (
            f"{row['net']:.2f} · {roi:.1%}" if roi is not None else f"{row['net']:.2f} · —"
        )
        row["net_roi_sort"] = roi if roi is not None else -1
        for field in ("staked", "returned", "net"):
            row[f"{field}_display"] = f"{row[field]:.2f}"
    return rows


def _clv_for_leg(session: Session, row: dict) -> float | None:
    fixture = row["fixture"]
    leg = row["leg"]
    if fixture is None:
        return None
    quotes = session.scalars(
        select(OddsSnapshot)
        .where(
            OddsSnapshot.fixture_id == leg.fixture_id,
            OddsSnapshot.market_id == leg.market_id,
            OddsSnapshot.selection == leg.selection,
            OddsSnapshot.line == leg.line,
            OddsSnapshot.as_of < fixture.kickoff_at,
        )
        .order_by(OddsSnapshot.as_of.desc(), OddsSnapshot.id.desc())
    ).all()
    if not quotes:
        return None
    selected = session.get(OddsSnapshot, leg.odds_snapshot_id)
    quote = next(
        (item for item in quotes if selected and item.bookmaker_id == selected.bookmaker_id),
        quotes[0],
    )
    if quote.odds <= 1:
        return None
    return float(leg.odds / quote.odds - Decimal(1))


def _leg_summary(session: Session, rows: list[dict]) -> dict:
    decisive = [row for row in rows if row["result"].outcome not in NON_DECISIVE]
    brier_values = []
    for row in decisive:
        observed = _observed_outcome(row["result"].outcome)
        brier_values.append((float(row["leg"].probability) - observed) ** 2)
    clv = [value for row in rows if (value := _clv_for_leg(session, row)) is not None]
    bins: dict[int, list[dict]] = defaultdict(list)
    for row in decisive:
        bucket = min(9, int(float(row["leg"].probability) * 10))
        bins[bucket].append(row)
    calibration = []
    for bucket, grouped in sorted(bins.items()):
        predictions = [float(row["leg"].probability) for row in grouped]
        outcomes = [_observed_outcome(row["result"].outcome) for row in grouped]
        calibration.append(
            {
                "bucket": f"{bucket / 10:.0%}–{(bucket + 1) / 10:.0%}",
                "mean_probability": mean(predictions),
                "observed_rate": mean(outcomes),
                "sample": len(grouped),
            }
        )
    count = len(rows)
    realized_ev = [float(row["result"].payout_multiplier - Decimal(1)) for row in rows]
    claimed_ev = [float(row["leg"].probability * row["leg"].odds - Decimal(1)) for row in rows]
    digest = sha256(f"clv:{len(clv)}".encode()).digest()
    clv_low, clv_high = _bootstrap_mean(clv, seed=int.from_bytes(digest[:4], "big"))
    return {
        "settled_legs": count,
        "decisive_legs": len(decisive),
        "brier": mean(brier_values) if brier_values else None,
        "mean_probability": mean(float(row["leg"].probability) for row in decisive)
        if decisive
        else None,
        "observed_rate": mean(_observed_outcome(row["result"].outcome) for row in decisive)
        if decisive
        else None,
        "claimed_ev": mean(claimed_ev) if claimed_ev else None,
        "realized_ev": mean(realized_ev) if realized_ev else None,
        "clv_legs": len(clv),
        "mean_clv": mean(clv) if clv else None,
        "clv_ci_low": clv_low,
        "clv_ci_high": clv_high,
        "calibration": calibration,
    }


def build_performance_report(
    session: Session,
    *,
    as_of: datetime | None = None,
    days: int | None = None,
    timezone_name: str = "Africa/Blantyre",
    include_details: bool = True,
    singles_page: int = 1,
    singles_page_size: int = 100,
) -> dict:
    """Build ticket ROI cuts, a flat singles view, and leg-level health metrics."""
    as_of = as_of or datetime.now(UTC)
    ticket_rows, leg_rows = _current_data(session)
    if days is not None:
        cutoff = as_of.astimezone(ZoneInfo(timezone_name)).date() - timedelta(days=days - 1)
        ticket_rows = [row for row in ticket_rows if row["target_date"] >= cutoff]
        leg_rows = [row for row in leg_rows if row["target_date"] >= cutoff]
    aggregate = _summary(ticket_rows, key="aggregate")
    if not include_details:
        return {"aggregate": aggregate, "sample_size_note": SAMPLE_SIZE_NOTE}
    by_band: dict[str, list[dict]] = defaultdict(list)
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for row in ticket_rows:
        by_band[row["band"]].append(row)
        by_tier[row["tier"]].append(row)
    single_items = []
    for row in leg_rows:
        leg, result = row["leg"], row["result"]
        single_items.append(
            {
                "returned": result.payout_multiplier,
                "result_status": "won" if result.outcome in {"win", "half_win"} else "lost",
                "combined_odds": Decimal(leg.odds),
            }
        )
    singles_page_size = max(1, min(200, singles_page_size))
    singles_total = len(leg_rows)
    singles_pages = max(1, (singles_total + singles_page_size - 1) // singles_page_size)
    singles_page = max(1, min(singles_page, singles_pages))
    sorted_single_rows = sorted(
        leg_rows, key=lambda row: (row["target_date"], row["leg"].id), reverse=True
    )
    start = (singles_page - 1) * singles_page_size
    visible_singles = sorted_single_rows[start : start + singles_page_size]
    singles_display_rows = []
    for row in visible_singles:
        leg, result = row["leg"], row["result"]
        singles_display_rows.append(
            {
                "date": row["target_date"].isoformat(),
                "fixture": row["fixture_name"],
                "market": row["market_name"],
                "selection": leg.selection if leg.line is None else f"{leg.selection} · {leg.line}",
                "odds": f"{leg.odds:.2f}",
                "probability": f"{float(leg.probability):.1%} / {float(leg.p_low):.1%}",
                "result_return": (
                    f"{result.outcome.replace('_', ' ').title()} · {result.payout_multiplier:.2f}"
                ),
                "why": leg.why,
                "ev": float(leg.probability * leg.odds - Decimal(1)),
                "id": leg.id,
            }
        )
    return {
        "tickets": ticket_rows,
        "aggregate": aggregate,
        "by_day": _decorate_metric_rows(
            _group_rows(ticket_rows, "day", lambda row: row["target_date"].isoformat())
        ),
        "by_month": _decorate_metric_rows(
            _group_rows(ticket_rows, "month", lambda row: row["target_date"].strftime("%Y-%m"))
        ),
        "by_year": _decorate_metric_rows(
            _group_rows(ticket_rows, "year", lambda row: str(row["target_date"].year))
        ),
        "by_band": _decorate_metric_rows(
            [
                {"period": band, **_summary(items, key=f"band:{band}")}
                for band, items in sorted(by_band.items())
            ]
        ),
        "by_tier": _decorate_metric_rows(
            [
                {"period": tier, **_summary(items, key=f"tier:{tier}")}
                for tier, items in sorted(by_tier.items())
            ]
        ),
        "singles": _summary(single_items, key="singles"),
        "singles_rows": singles_display_rows,
        "singles_page": singles_page,
        "singles_pages": singles_pages,
        "singles_total": singles_total,
        "legs": _leg_summary(session, leg_rows),
        "sample_size_note": SAMPLE_SIZE_NOTE,
    }
