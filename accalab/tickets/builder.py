"""Point-in-time ticket construction with priority-ordered CP-SAT bands."""

from __future__ import annotations

import math
import tomllib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

from ortools.sat.python import cp_model
from sqlalchemy import select
from sqlalchemy.orm import Session

from accalab.config import ROOT
from accalab.modeling.config import ModelConfig, load_model_config
from accalab.modeling.value import assess_leg
from accalab.models import (
    DataSnapshot,
    Fixture,
    MarketRegistry,
    OddsSnapshot,
    Prediction,
    SingleSelection,
    Ticket,
    TicketLeg,
)

RunType = Literal["preview", "official"]


@dataclass(frozen=True)
class Band:
    name: str
    lower: Decimal
    upper: Decimal | None

    def contains(self, odds: Decimal) -> bool:
        return odds >= self.lower and (self.upper is None or odds < self.upper)


BANDS = (
    Band("T1", Decimal("3"), Decimal("5")),
    Band("T2", Decimal("5"), Decimal("10")),
    Band("T3", Decimal("10"), Decimal("20")),
    Band("T4", Decimal("20"), Decimal("50")),
    Band("T5", Decimal("50"), None),
)


@dataclass(frozen=True)
class DayTypeConfig:
    minimum_pool: int
    enabled_bands: tuple[str, ...]


@dataclass(frozen=True)
class BuilderConfig:
    timezone: str
    log_scale: int
    solver_max_seconds: float
    weekday: DayTypeConfig
    weekend: DayTypeConfig

    def for_date(self, target_date: date) -> tuple[str, DayTypeConfig]:
        is_weekend = target_date.weekday() >= 5
        return ("weekend", self.weekend) if is_weekend else ("weekday", self.weekday)


@dataclass(frozen=True)
class CandidateLeg:
    fixture_id: int
    market_id: int
    selection: str
    line: Decimal | None
    odds_snapshot_id: int
    odds: Decimal
    probability: float
    p_low: float
    overround: float
    market_ticket_eligible: bool
    kickoff_at: datetime
    prediction_as_of: datetime
    odds_as_of: datetime
    why: str

    @property
    def key(self) -> tuple[int, int, str, Decimal | None]:
        return (self.fixture_id, self.market_id, self.selection.strip().casefold(), self.line)


@dataclass(frozen=True)
class BuiltTicket:
    band: str
    legs: tuple[CandidateLeg, ...]
    combined_odds: Decimal
    p_low_product: float
    solver_status: str


@dataclass(frozen=True)
class BuildResult:
    target_date: date
    run_type: RunType
    as_of: datetime
    day_type: str
    tickets: tuple[BuiltTicket, ...]
    eligible_legs: tuple[CandidateLeg, ...]
    diagnostics: dict


def load_builder_config(path: Path | None = None) -> BuilderConfig:
    raw = tomllib.loads((path or ROOT / "config" / "builder.toml").read_text(encoding="utf-8"))
    common = raw["builder"]

    def day(name: str) -> DayTypeConfig:
        item = raw[name]
        enabled = tuple(item["enabled_bands"])
        if any(band not in {known.name for known in BANDS} for band in enabled):
            raise ValueError(f"Unknown ticket band in [{name}].")
        if (
            len(set(enabled)) != len(enabled)
            or tuple(b for b in (x.name for x in BANDS) if b in enabled) != enabled
        ):
            raise ValueError(f"[{name}].enabled_bands must list unique bands in priority order.")
        minimum = int(item["minimum_pool"])
        if minimum < 1:
            raise ValueError(f"[{name}].minimum_pool must be positive.")
        return DayTypeConfig(minimum_pool=minimum, enabled_bands=enabled)

    config = BuilderConfig(
        timezone=str(common["timezone"]),
        log_scale=int(common["log_scale"]),
        solver_max_seconds=float(common["solver_max_seconds"]),
        weekday=day("weekday"),
        weekend=day("weekend"),
    )
    ZoneInfo(config.timezone)
    if config.log_scale < 100 or config.solver_max_seconds <= 0:
        raise ValueError("Builder log scale and solver time limit must be positive.")
    return config


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local_date(value: datetime, timezone_name: str) -> date:
    return _aware(value).astimezone(ZoneInfo(timezone_name)).date()


def _valid_candidate(
    leg: CandidateLeg,
    *,
    target_date: date,
    as_of: datetime,
    timezone_name: str,
    model_config: ModelConfig,
) -> tuple[bool, str | None]:
    if _local_date(leg.kickoff_at, timezone_name) != target_date:
        return False, "fixture_not_on_target_date"
    if _aware(leg.kickoff_at) <= as_of:
        return False, "fixture_already_started_at_as_of"
    if _aware(leg.prediction_as_of) > as_of or _aware(leg.odds_as_of) > as_of:
        return False, "evidence_after_run_as_of"
    if not leg.why or len(leg.why) > 140:
        return False, "missing_or_overlong_pick_reason"
    try:
        assessment = assess_leg(
            probability=leg.probability,
            p_low=leg.p_low,
            odds=float(leg.odds),
            overround=leg.overround,
            market_ticket_eligible=leg.market_ticket_eligible,
            config=model_config,
        )
    except ValueError:
        return False, "invalid_leg_values"
    if not assessment.eligible:
        return False, ",".join(assessment.reasons)
    return True, None


def _exact_odds(legs: Iterable[CandidateLeg]) -> Decimal:
    result = Decimal(1)
    for leg in legs:
        result *= leg.odds
    return result


def _solve_band(
    candidates: list[CandidateLeg],
    band: Band,
    *,
    config: BuilderConfig,
) -> tuple[tuple[CandidateLeg, ...] | None, str]:
    if not candidates:
        return None, "infeasible: no eligible unused legs remain"
    model = cp_model.CpModel()
    selected = [model.new_bool_var(f"leg_{i}") for i in range(len(candidates))]
    fixtures: dict[int, list[int]] = {}
    for index, leg in enumerate(candidates):
        fixtures.setdefault(leg.fixture_id, []).append(index)
    for indexes in fixtures.values():
        model.add(sum(selected[index] for index in indexes) <= 1)

    # CP-SAT is integer-only. Exact Decimal checks below reject a result that
    # crossed a band edge due to log quantization and ask for another solution.
    logs = [int(round(math.log(float(leg.odds)) * config.log_scale)) for leg in candidates]
    lower = math.ceil(math.log(float(band.lower)) * config.log_scale)
    upper = (
        None
        if band.upper is None
        else math.ceil(math.log(float(band.upper)) * config.log_scale) - 1
    )
    model.add(sum(value * selected[i] for i, value in enumerate(logs)) >= lower)
    if upper is not None:
        model.add(sum(value * selected[i] for i, value in enumerate(logs)) <= upper)

    # Integer log(p_low) maximizes the product of conservative probabilities.
    # A small secondary term favors fewer legs only when primary score ties.
    scores = [int(round(math.log(leg.p_low) * config.log_scale)) for leg in candidates]
    tie_scale = len(candidates) + 1
    model.maximize(
        sum(scores[i] * tie_scale * selected[i] - selected[i] for i in range(len(candidates)))
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.solver_max_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    for _attempt in range(20):
        status = solver.solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            label = solver.status_name(status).lower()
            return None, f"{label}: no feasible combination found within solver limit"
        indexes = [i for i, variable in enumerate(selected) if solver.value(variable)]
        legs = tuple(candidates[i] for i in indexes)
        if band.contains(_exact_odds(legs)):
            return legs, "optimal" if status == cp_model.OPTIMAL else "feasible_time_limit"
        # Forbid this exact assignment and resolve if log scaling crossed an edge.
        model.add(sum(selected[i] for i in indexes) <= len(indexes) - 1)
    return None, "infeasible: log-scale boundary retries exhausted"


def build_day(
    *,
    target_date: date,
    as_of: datetime,
    run_type: RunType,
    candidates: Iterable[CandidateLeg],
    config: BuilderConfig | None = None,
    model_config: ModelConfig | None = None,
    already_used: Iterable[tuple[int, int, str, Decimal | None]] = (),
) -> BuildResult:
    if run_type not in ("preview", "official"):
        raise ValueError("run_type must be 'preview' or 'official'.")
    config = config or load_builder_config()
    model_config = model_config or load_model_config()
    as_of = _aware(as_of)
    day_type, day_config = config.for_date(target_date)
    used = set(already_used)
    eligible_by_key: dict[tuple[int, int, str, Decimal | None], CandidateLeg] = {}
    all_eligible_by_key: dict[tuple[int, int, str, Decimal | None], CandidateLeg] = {}
    rejections: dict[str, int] = {}
    raw_count = 0
    for leg in candidates:
        raw_count += 1
        valid, reason = _valid_candidate(
            leg,
            target_date=target_date,
            as_of=as_of,
            timezone_name=config.timezone,
            model_config=model_config,
        )
        if not valid:
            rejections[reason or "ineligible"] = rejections.get(reason or "ineligible", 0) + 1
            continue
        single_candidate = all_eligible_by_key.get(leg.key)
        if single_candidate is None or (leg.odds, -leg.overround) > (
            single_candidate.odds,
            -single_candidate.overround,
        ):
            all_eligible_by_key[leg.key] = leg
        if leg.key in used:
            rejections["already_used_today"] = rejections.get("already_used_today", 0) + 1
            continue
        previous = eligible_by_key.get(leg.key)
        if previous is None or (leg.odds, -leg.overround) > (previous.odds, -previous.overround):
            eligible_by_key[leg.key] = leg
    pool = list(eligible_by_key.values())
    pool.sort(
        key=lambda leg: (
            -leg.p_low,
            leg.fixture_id,
            leg.market_id,
            leg.selection,
            leg.line or Decimal(0),
        )
    )
    diagnostics = {
        "run_type": run_type,
        "target_date": target_date.isoformat(),
        "as_of": as_of.isoformat(),
        "day_type": day_type,
        "minimum_pool": day_config.minimum_pool,
        "raw_candidate_count": raw_count,
        "eligible_pool_count": len(pool),
        "rejections": rejections,
        "bands": {},
    }
    eligible_legs = tuple(
        sorted(
            all_eligible_by_key.values(),
            key=lambda leg: (leg.fixture_id, leg.market_id, leg.selection, leg.line or Decimal(0)),
        )
    )
    tickets: list[BuiltTicket] = []
    for band in BANDS:
        if band.name not in day_config.enabled_bands:
            diagnostics["bands"][band.name] = {
                "status": "disabled",
                "reason": f"{day_type} configuration disables {band.name}.",
            }
            continue
        if len(pool) < day_config.minimum_pool:
            diagnostics["bands"][band.name] = {
                "status": "no_ticket",
                "reason": f"Eligible pool has {len(pool)} legs; {day_config.minimum_pool} required for {day_type}.",
                "remaining_pool_count": len(pool),
            }
            continue
        chosen, solver_status = _solve_band(pool, band, config=config)
        if chosen is None:
            diagnostics["bands"][band.name] = {
                "status": "no_ticket",
                "reason": solver_status,
                "remaining_pool_count": len(pool),
            }
            continue
        product = _exact_odds(chosen)
        ticket = BuiltTicket(
            band=band.name,
            legs=chosen,
            combined_odds=product,
            p_low_product=math.prod(leg.p_low for leg in chosen),
            solver_status=solver_status,
        )
        tickets.append(ticket)
        chosen_keys = {leg.key for leg in chosen}
        pool = [leg for leg in pool if leg.key not in chosen_keys]
        used.update(chosen_keys)
        diagnostics["bands"][band.name] = {
            "status": "built",
            "reason": None,
            "legs": len(chosen),
            "combined_odds": str(product),
            "solver_status": solver_status,
            "remaining_pool_count": len(pool),
        }
    diagnostics["remaining_pool_count"] = len(pool)
    return BuildResult(
        target_date, run_type, as_of, day_type, tuple(tickets), eligible_legs, diagnostics
    )


def _used_keys_for_day(
    session: Session, target_date: date
) -> set[tuple[int, int, str, Decimal | None]]:
    rows = session.execute(
        select(TicketLeg.fixture_id, TicketLeg.market_id, TicketLeg.selection, TicketLeg.line)
        .join(Ticket, Ticket.id == TicketLeg.ticket_id)
        .where(Ticket.target_date == target_date)
    ).all()
    return {
        (row.fixture_id, row.market_id, row.selection.strip().casefold(), row.line) for row in rows
    }


def persist_build(session: Session, result: BuildResult) -> list[Ticket]:
    """Stage tickets and diagnostics in the caller's transaction; caller commits."""
    records: list[Ticket] = []
    for built in result.tickets:
        ticket_id = str(uuid.uuid4())
        ticket = Ticket(
            id=ticket_id,
            target_date=result.target_date,
            status=result.run_type,
            band=built.band,
            combined_odds=built.combined_odds,
            as_of=result.as_of,
        )
        session.add(ticket)
        if result.run_type == "official":
            from accalab.messaging import enqueue_ticket_published

            enqueue_ticket_published(session, ticket_id)
        for leg in built.legs:
            session.add(
                TicketLeg(
                    ticket_id=ticket_id,
                    fixture_id=leg.fixture_id,
                    market_id=leg.market_id,
                    selection=leg.selection,
                    line=leg.line,
                    odds_snapshot_id=leg.odds_snapshot_id,
                    odds=leg.odds,
                    probability=Decimal(str(leg.probability)),
                    p_low=Decimal(str(leg.p_low)),
                    why=leg.why,
                )
            )
        records.append(ticket)
    if result.run_type == "official":
        existing_singles = session.scalars(
            select(SingleSelection).where(
                SingleSelection.target_date == result.target_date,
                SingleSelection.run_type == result.run_type,
            )
        ).all()
        existing_keys = {
            (row.fixture_id, row.market_id, row.selection.strip().casefold(), row.line)
            for row in existing_singles
        }
        for leg in result.eligible_legs:
            if leg.key in existing_keys:
                continue
            session.add(
                SingleSelection(
                    target_date=result.target_date,
                    run_type=result.run_type,
                    fixture_id=leg.fixture_id,
                    market_id=leg.market_id,
                    selection=leg.selection,
                    line=leg.line,
                    odds_snapshot_id=leg.odds_snapshot_id,
                    odds=leg.odds,
                    probability=Decimal(str(leg.probability)),
                    p_low=Decimal(str(leg.p_low)),
                    as_of=result.as_of,
                    why=leg.why,
                )
            )
            existing_keys.add(leg.key)
    session.add(
        DataSnapshot(
            data_type="builder_diagnostics",
            scope_type="day_run",
            scope_key=f"{result.target_date.isoformat()}:{result.run_type}:{result.as_of.isoformat()}",
            as_of=result.as_of,
            source="ticket_builder",
            payload=result.diagnostics,
        )
    )
    if result.run_type == "official":
        session.flush()
        from accalab.tracking.sample import select_weekly_sample

        select_weekly_sample(session, now=result.as_of)
    return records


def database_candidates(
    session: Session, *, target_date: date, as_of: datetime, timezone_name: str
) -> list[CandidateLeg]:
    """Join persisted predictions to their latest matching odds known at `as_of`."""
    as_of = _aware(as_of)
    fixtures = session.scalars(select(Fixture)).all()
    fixture_map = {
        fixture.id: fixture
        for fixture in fixtures
        if _local_date(fixture.kickoff_at, timezone_name) == target_date
    }
    if not fixture_map:
        return []
    predictions = session.scalars(
        select(Prediction)
        .where(Prediction.fixture_id.in_(fixture_map), Prediction.as_of <= as_of)
        .order_by(Prediction.as_of.desc())
    ).all()
    markets = {market.id: market for market in session.scalars(select(MarketRegistry)).all()}
    odds = session.scalars(
        select(OddsSnapshot)
        .where(OddsSnapshot.fixture_id.in_(fixture_map), OddsSnapshot.as_of <= as_of)
        .order_by(OddsSnapshot.as_of.desc())
    ).all()
    odds_map = {}
    implied_by_market: dict[tuple, float] = {}
    for row in odds:
        key = (row.fixture_id, row.market_id, row.selection.strip().casefold(), row.line)
        odds_map.setdefault(key, row)
        market_key = (
            row.fixture_id,
            row.bookmaker_id,
            row.bookmaker_name,
            row.market_id,
            row.line,
            _aware(row.as_of),
        )
        if float(row.odds) > 1:
            implied_by_market[market_key] = implied_by_market.get(market_key, 0.0) + 1.0 / float(
                row.odds
            )
    overround_by_market = {key: total - 1.0 for key, total in implied_by_market.items()}
    prediction_map = {}
    for prediction in predictions:
        key = (
            prediction.fixture_id,
            prediction.market_id,
            prediction.selection.strip().casefold(),
            prediction.line,
        )
        prediction_map.setdefault(key, prediction)
    result = []
    for key, prediction in prediction_map.items():
        quote = odds_map.get(key)
        market = markets.get(prediction.market_id)
        if quote is None or market is None or prediction.p_low is None or not prediction.why:
            continue
        market_key = (
            quote.fixture_id,
            quote.bookmaker_id,
            quote.bookmaker_name,
            quote.market_id,
            quote.line,
            _aware(quote.as_of),
        )
        overround = quote.raw.get("overround") if isinstance(quote.raw, dict) else None
        if overround is None:
            overround = overround_by_market.get(market_key)
        if overround is None or not math.isfinite(float(overround)) or float(overround) < 0:
            continue
        result.append(
            CandidateLeg(
                fixture_id=prediction.fixture_id,
                market_id=prediction.market_id,
                selection=prediction.selection,
                line=prediction.line,
                odds_snapshot_id=quote.id,
                odds=quote.odds,
                probability=float(prediction.probability),
                p_low=float(prediction.p_low),
                overround=float(overround),
                market_ticket_eligible=market.ticket_eligible,
                kickoff_at=fixture_map[prediction.fixture_id].kickoff_at,
                prediction_as_of=prediction.as_of,
                odds_as_of=quote.as_of,
                why=prediction.why,
            )
        )
    return result


def build_and_persist_day(
    session: Session,
    *,
    target_date: date,
    as_of: datetime,
    run_type: RunType,
    config: BuilderConfig | None = None,
    model_config: ModelConfig | None = None,
) -> BuildResult:
    config = config or load_builder_config()
    used = _used_keys_for_day(session, target_date)
    candidates = database_candidates(
        session, target_date=target_date, as_of=as_of, timezone_name=config.timezone
    )
    result = build_day(
        target_date=target_date,
        as_of=as_of,
        run_type=run_type,
        candidates=candidates,
        config=config,
        model_config=model_config,
        already_used=used,
    )
    persist_build(session, result)
    return result
