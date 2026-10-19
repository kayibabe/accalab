"""Paired out-of-sample evaluation of calibrated forecasts with and without agents."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from accalab.models import AgentLog, Fixture, MarketRegistry, OddsSnapshot


@dataclass(frozen=True)
class AgentEvaluationCase:
    baseline_probability: float
    adjusted_probability: float
    observed: int
    clv: float | None
    vetoed: bool = False


def evaluate_agent_impact(cases: list[AgentEvaluationCase]) -> dict:
    """Compute paired binary Brier/log loss and CLV, retaining the sample count."""
    if not cases:
        return {
            "sample_size": 0,
            "without_agents": {"brier": None, "log_loss": None, "mean_clv": None, "clv_sample": 0},
            "with_agents": {"brier": None, "log_loss": None, "mean_clv": None, "clv_sample": 0},
            "brier_delta": None,
            "log_loss_delta": None,
            "clv_delta": None,
            "veto_count": 0,
        }

    def summarize(probabilities: list[float], clv: list[float]) -> dict:
        bounded = [max(1e-12, min(1 - 1e-12, value)) for value in probabilities]
        return {
            "brier": mean((p - row.observed) ** 2 for p, row in zip(bounded, cases, strict=True)),
            "log_loss": mean(
                -(row.observed * math.log(p) + (1 - row.observed) * math.log(1 - p))
                for p, row in zip(bounded, cases, strict=True)
            ),
            "mean_clv": mean(clv) if clv else None,
            "clv_sample": len(clv),
        }

    baseline_clv = [row.clv for row in cases if row.clv is not None]
    adjusted_clv = list(baseline_clv)
    without = summarize([row.baseline_probability for row in cases], baseline_clv)
    with_agents = summarize([row.adjusted_probability for row in cases], adjusted_clv)
    return {
        "sample_size": len(cases),
        "without_agents": without,
        "with_agents": with_agents,
        "brier_delta": with_agents["brier"] - without["brier"],
        "log_loss_delta": with_agents["log_loss"] - without["log_loss"],
        "clv_delta": (
            with_agents["mean_clv"] - without["mean_clv"]
            if with_agents["mean_clv"] is not None and without["mean_clv"] is not None
            else None
        ),
        "veto_count": sum(row.vetoed for row in cases),
    }


def _observed(market: str, selection: str, line, fixture: Fixture) -> int | None:
    if fixture.home_goals is None or fixture.away_goals is None:
        return None
    home, away, total = (
        fixture.home_goals,
        fixture.away_goals,
        fixture.home_goals + fixture.away_goals,
    )
    normalized = selection.strip().casefold()
    if market == "1X2":
        actual = "home" if home > away else "draw" if home == away else "away"
        return int(normalized == actual) if normalized in {"home", "draw", "away"} else None
    if market == "Both Teams To Score":
        actual_yes = home > 0 and away > 0
        if normalized in {"yes", "no"}:
            return int((normalized == "yes") == actual_yes)
    if market == "Over/Under Goals" and line is not None and float(line) == 2.5:
        actual_over = total >= 3
        if normalized in {"over", "under"}:
            return int((normalized == "over") == actual_over)
    return None


def load_agent_evaluation(session: Session) -> dict:
    """Read settled fixtures for agent-scored predictions and pair baseline with adjusted scores."""
    rows = session.execute(
        select(AgentLog, Fixture, MarketRegistry)
        .join(Fixture, Fixture.id == AgentLog.fixture_id)
        .join(MarketRegistry, MarketRegistry.id == AgentLog.market_id)
        .where(
            or_(AgentLog.status == "ok", AgentLog.veto_reason.is_not(None)),
            AgentLog.baseline_probability.is_not(None),
            AgentLog.adjusted_probability.is_not(None),
            Fixture.home_goals.is_not(None),
            Fixture.away_goals.is_not(None),
        )
        .order_by(AgentLog.id)
    ).all()
    runs = {}
    for log, fixture, market in rows:
        identity = log.prediction_id or (
            log.fixture_id,
            log.market_id,
            log.selection,
            str(log.line),
        )
        key = (identity, log.as_of)
        previous = runs.get(key)
        vetoed = bool(log.veto_reason) or (previous[2] if previous else False)
        runs[key] = ((log, fixture, market), vetoed)
    latest_run_by_prediction = {}
    for (identity, as_of), (row, vetoed) in runs.items():
        previous = latest_run_by_prediction.get(identity)
        if previous is None or as_of >= previous[0]:
            latest_run_by_prediction[identity] = (as_of, row, vetoed)
    cases = []
    for _, (log, fixture, market), vetoed in latest_run_by_prediction.values():
        observed = _observed(market.canonical_name, log.selection or "", log.line, fixture)
        if observed is None:
            continue
        clv = None
        if log.reference_odds and log.reference_odds > 1:
            quote_query = select(OddsSnapshot).where(
                OddsSnapshot.fixture_id == fixture.id,
                OddsSnapshot.market_id == log.market_id,
                OddsSnapshot.selection == log.selection,
                OddsSnapshot.as_of < fixture.kickoff_at,
            )
            quote_query = (
                quote_query.where(OddsSnapshot.line.is_(None))
                if log.line is None
                else quote_query.where(OddsSnapshot.line == log.line)
            )
            quotes = session.scalars(quote_query.order_by(OddsSnapshot.as_of.desc())).all()
            closing = next(
                (quote for quote in quotes if quote.bookmaker_id == log.reference_bookmaker_id),
                quotes[0] if quotes else None,
            )
            if closing and closing.odds > 1:
                clv = float(log.reference_odds / closing.odds - 1)
        cases.append(
            AgentEvaluationCase(
                baseline_probability=float(log.baseline_probability),
                adjusted_probability=float(log.adjusted_probability),
                observed=observed,
                clv=clv,
                vetoed=vetoed,
            )
        )
    return evaluate_agent_impact(cases)


def render_agent_evaluation(report: dict) -> str:
    def display(value) -> str:
        return "n/a" if value is None else f"{value:.4f}"

    no_agent = report["without_agents"]
    with_agent = report["with_agents"]
    lines = [
        "# Agent evaluation",
        "",
        "Paired settled forecast evaluation. Negative Brier and log-loss deltas mean the adjusted probabilities scored better. Agents do not change odds, so CLV is identical for each paired row.",
        "",
        f"Sample: {report['sample_size']} settled agent-scored selections; {report['veto_count']} vetoed.",
        "",
        "| Measure | Without agents | With agents | Change |",
        "|---|---:|---:|---:|",
        f"| Brier score | {display(no_agent['brier'])} | {display(with_agent['brier'])} | {display(report['brier_delta'])} |",
        f"| Log loss | {display(no_agent['log_loss'])} | {display(with_agent['log_loss'])} | {display(report['log_loss_delta'])} |",
        f"| Mean CLV ({no_agent['clv_sample']} selections) | {display(no_agent['mean_clv'])} | {display(with_agent['mean_clv'])} | {display(report['clv_delta'])} |",
        "",
        "A lower Brier score or log loss is better. This paired evaluation describes forecasts in the recorded sample and does not establish future performance.",
        "",
    ]
    return "\n".join(lines)
