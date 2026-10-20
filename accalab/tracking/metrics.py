"""Ticket, single, and leg-level performance summaries."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import random
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
from accalab.modeling.value import de_vig

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
    if selected is None or selected.bookmaker_id is None:
        return None
    quote = next((item for item in quotes if item.bookmaker_id == selected.bookmaker_id), None)
    if quote is None:
        return None
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


def _latest_single_results(session: Session, *, as_of: datetime) -> dict[int, SingleResult]:
    latest: dict[int, SingleResult] = {}
    for result in session.scalars(
        select(SingleResult)
        .where(SingleResult.settled_at <= as_of)
        .order_by(SingleResult.settled_at, SingleResult.id)
    ).all():
        latest[result.single_selection_id] = result
    return latest


def _closing_market_probability(session: Session, row: dict) -> float | None:
    """Return a proportional de-vig probability from the latest same-book quote set."""
    fixture, leg = row["fixture"], row["leg"]
    if fixture is None:
        return None
    selected_quote = session.get(OddsSnapshot, leg.odds_snapshot_id)
    if selected_quote is None or selected_quote.bookmaker_id is None:
        return None
    quotes = session.scalars(
        select(OddsSnapshot)
        .where(
            OddsSnapshot.fixture_id == leg.fixture_id,
            OddsSnapshot.market_id == leg.market_id,
            OddsSnapshot.line == leg.line,
            OddsSnapshot.bookmaker_id == selected_quote.bookmaker_id,
            OddsSnapshot.as_of < fixture.kickoff_at,
        )
        .order_by(OddsSnapshot.as_of.desc(), OddsSnapshot.id.desc())
    ).all()
    if not quotes:
        return None
    closing_at = quotes[0].as_of
    closing = {quote.selection: float(quote.odds) for quote in quotes if quote.as_of == closing_at}
    try:
        return de_vig(closing).fair_probabilities.get(leg.selection)
    except ValueError:
        return None


def build_prelaunch_report(session: Session, *, as_of: datetime | None = None) -> dict:
    """Build the Admin-only evidence summary for the proposed launch gate."""
    as_of = as_of or datetime.now(UTC)
    results = _latest_single_results(session, as_of=as_of)
    selections = session.scalars(
        select(SingleSelection).where(
            SingleSelection.run_type == "official", SingleSelection.as_of <= as_of
        )
    ).all()
    fixtures: dict[int, Fixture | None] = {}
    rows = []
    for selection in selections:
        result = results.get(selection.id)
        if result is None:
            continue
        fixture = fixtures.get(selection.fixture_id)
        if selection.fixture_id not in fixtures:
            fixture = session.get(Fixture, selection.fixture_id)
            fixtures[selection.fixture_id] = fixture
        rows.append(
            {
                "leg": selection,
                "result": result,
                "fixture": fixture,
                "target_date": selection.target_date,
            }
        )

    decisive = [row for row in rows if row["result"].outcome not in NON_DECISIVE]
    paired_brier = []
    for row in decisive:
        market_probability = _closing_market_probability(session, row)
        if market_probability is not None:
            observed = _observed_outcome(row["result"].outcome)
            paired_brier.append(
                (
                    (float(row["leg"].probability) - observed) ** 2,
                    (market_probability - observed) ** 2,
                )
            )

    calibration: dict[int, list[dict]] = defaultdict(list)
    for row in decisive:
        calibration[min(9, int(float(row["leg"].probability) * 10))].append(row)
    calibration_rows = []
    weighted_ece = 0.0
    for bucket, grouped in sorted(calibration.items()):
        predicted = mean(float(row["leg"].probability) for row in grouped)
        observed = mean(_observed_outcome(row["result"].outcome) for row in grouped)
        weighted_ece += len(grouped) / len(decisive) * abs(predicted - observed) if decisive else 0.0
        calibration_rows.append(
            {
                "bucket": f"{bucket / 10:.0%}–{(bucket + 1) / 10:.0%}",
                "predicted": f"{predicted:.1%}",
                "observed": f"{observed:.1%}",
                "sample": len(grouped),
                "sample_sort": len(grouped),
            }
        )

    clv_values = [
        value
        for row in rows
        if (value := _clv_for_leg(session, row)) is not None
    ]
    digest = sha256(f"prelaunch-clv:{len(clv_values)}".encode()).digest()
    clv_low, clv_high = _bootstrap_mean(
        clv_values, seed=int.from_bytes(digest[:4], "big")
    )
    claimed = [
        float(row["leg"].probability * row["leg"].odds - Decimal(1)) for row in rows
    ]
    realised = [
        float(row["result"].payout_multiplier - Decimal(1)) for row in rows
    ]
    starts = [selection.as_of for selection in selections]
    tracking_start = min(starts) if starts else None
    weeks = max(0.0, (as_of - tracking_start).total_seconds() / (7 * 24 * 3600)) if tracking_start else 0.0
    model_brier = mean([pair[0] for pair in paired_brier]) if paired_brier else None
    market_brier_score = mean([pair[1] for pair in paired_brier]) if paired_brier else None
    mean_clv = mean(clv_values) if clv_values else None

    checks = [
        ("Tracking duration", weeks >= 6, f"{weeks:.1f} weeks", "At least 6 weeks"),
        ("Settled eligible legs", len(rows) >= 800, str(len(rows)), "At least 800"),
        (
            "Calibration error (ECE)",
            bool(decisive) and weighted_ece <= 0.03,
            f"{weighted_ece:.3f}" if decisive else "Unavailable",
            "At most 0.030",
        ),
        (
            "Brier versus de-vigged market",
            model_brier is not None and market_brier_score is not None and model_brier <= market_brier_score,
            (
                f"Model {model_brier:.4f} · market {market_brier_score:.4f}"
                if model_brier is not None and market_brier_score is not None
                else "Unavailable"
            ),
            "Model no worse; paired sample shown below" if paired_brier else "Comparable market quotes required",
        ),
        (
            "Mean closing-line value",
            mean_clv is not None and mean_clv > 0 and clv_low is not None,
            (
                f"{mean_clv:.2%} · 95% CI {clv_low:.2%} to {clv_high:.2%}"
                if mean_clv is not None and clv_low is not None and clv_high is not None
                else "Unavailable"
            ),
            "Positive mean; confidence interval displayed",
        ),
        (
            "Claimed versus realised EV",
            None,
            "Review required" if rows else "Unavailable",
            "Document and explain the measured gap before launch",
        ),
        ("Job reliability", None, "Unverified", "Reliable scheduled runs; job history is not stored"),
    ]
    gate_rows = [
        {
            "criterion": label,
            "measured": measured,
            "requirement": requirement,
            "status": "PASS" if passed is True else "NOT MET" if passed is False else "UNVERIFIED",
        }
        for label, passed, measured, requirement in checks
    ]
    all_passed = all(passed is True for _, passed, _, _ in checks)
    return {
        "tracking_start": tracking_start,
        "weeks": weeks,
        "settled_legs": len(rows),
        "decisive_legs": len(decisive),
        "ece": weighted_ece if decisive else None,
        "brier": model_brier,
        "market_brier": market_brier_score,
        "market_brier_legs": len(paired_brier),
        "claimed_ev": mean(claimed) if claimed else None,
        "realised_ev": mean(realised) if realised else None,
        "clv_legs": len(clv_values),
        "mean_clv": mean_clv,
        "clv_ci_low": clv_low,
        "clv_ci_high": clv_high,
        "calibration": calibration_rows,
        "gate_rows": gate_rows,
        "gate_status": "All measured criteria pass" if all_passed else "Launch gate not met",
        "gate_ready": all_passed,
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
