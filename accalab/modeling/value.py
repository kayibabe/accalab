"""Proportional de-vig and configured price/edge eligibility rules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from accalab.modeling.config import ModelConfig, load_model_config
from accalab.modeling.markets import AsianOutcomeMass, asian_handicap_ev


@dataclass(frozen=True)
class DeVigResult:
    implied_probabilities: Mapping[str, float]
    fair_probabilities: Mapping[str, float]
    overround: float


@dataclass(frozen=True)
class LegAssessment:
    probability: float
    p_low: float
    odds: float
    overround: float
    ev: float
    conservative_ev: float
    required_ev: float
    eligible: bool
    reasons: tuple[str, ...]


def de_vig(odds: Mapping[str, float]) -> DeVigResult:
    """Proportional normalization; overround is raw implied sum minus one."""
    if len(odds) < 2 or any(value <= 1 or not math.isfinite(value) for value in odds.values()):
        raise ValueError("At least two finite decimal prices greater than 1 are required.")
    implied = {selection: 1.0 / price for selection, price in odds.items()}
    total = sum(implied.values())
    if total <= 0:
        raise ValueError("The odds set has no implied probability mass.")
    return DeVigResult(
        implied_probabilities=implied,
        fair_probabilities={selection: probability / total for selection, probability in implied.items()},
        overround=total - 1.0,
    )


def probability_lower_bound(
    probability: float, effective_sample_size: float, *, z: float | None = None, config: ModelConfig | None = None
) -> float:
    """Approximate lower normal bound using the model's time-weighted sample size."""
    config = config or load_model_config()
    if (
        not math.isfinite(probability)
        or not 0 <= probability <= 1
        or not math.isfinite(effective_sample_size)
        or effective_sample_size <= 0
    ):
        raise ValueError("Probability and effective sample size are outside their valid ranges.")
    z = config.probability_z if z is None else z
    return max(0.0, probability - z * math.sqrt(probability * (1 - probability) / effective_sample_size))


def required_ev_threshold(overround: float, config: ModelConfig | None = None) -> float:
    config = config or load_model_config()
    excess = max(0.0, overround - config.overround_reference)
    return config.minimum_ev * (1.0 + config.overround_edge_scale * excess)


def assess_leg(
    *,
    probability: float,
    p_low: float,
    odds: float,
    overround: float,
    market_ticket_eligible: bool,
    config: ModelConfig | None = None,
) -> LegAssessment:
    config = config or load_model_config()
    if (
        any(not math.isfinite(value) for value in (probability, p_low, odds, overround))
        or not 0 <= p_low <= probability <= 1
        or odds <= 1
    ):
        raise ValueError("Probability bounds or decimal price are invalid.")
    ev = probability * odds - 1.0
    conservative_ev = p_low * odds - 1.0
    threshold = required_ev_threshold(overround, config)
    reasons = []
    if not market_ticket_eligible:
        reasons.append("market_not_ticket_eligible")
    if not config.minimum_odds <= odds <= config.maximum_odds:
        reasons.append("odds_outside_configured_range")
    if ev <= threshold:
        reasons.append("ev_below_overround_scaled_threshold")
    if conservative_ev <= config.conservative_ev_minimum:
        reasons.append("conservative_ev_not_positive")
    return LegAssessment(
        probability=probability,
        p_low=p_low,
        odds=odds,
        overround=overround,
        ev=ev,
        conservative_ev=conservative_ev,
        required_ev=threshold,
        eligible=not reasons,
        reasons=tuple(reasons),
    )


def assess_asian_handicap(
    *,
    mass: AsianOutcomeMass,
    odds: float,
    overround: float,
    effective_sample_size: float,
    market_ticket_eligible: bool,
    config: ModelConfig | None = None,
) -> tuple[float, float, bool, tuple[str, ...]]:
    """Return expected and conservative EV for split handicap settlement outcomes."""
    config = config or load_model_config()
    if (
        not math.isfinite(odds)
        or not math.isfinite(overround)
        or not math.isfinite(effective_sample_size)
        or odds <= 1
        or effective_sample_size <= 0
    ):
        raise ValueError("Decimal odds must exceed 1.")
    expected_value = asian_handicap_ev(mass, odds)
    payoffs = (odds - 1, (odds - 1) / 2, 0.0, -0.5, -1.0)
    probabilities = (mass.full_win, mass.half_win, mass.push, mass.half_loss, mass.full_loss)
    variance = max(0.0, sum(p * payoff**2 for p, payoff in zip(probabilities, payoffs, strict=True)) - expected_value**2)
    conservative_ev = expected_value - config.probability_z * math.sqrt(variance / effective_sample_size)
    threshold = required_ev_threshold(overround, config)
    reasons = []
    if not market_ticket_eligible:
        reasons.append("market_not_ticket_eligible")
    if not config.minimum_odds <= odds <= config.maximum_odds:
        reasons.append("odds_outside_configured_range")
    if expected_value <= threshold:
        reasons.append("ev_below_overround_scaled_threshold")
    if conservative_ev <= config.conservative_ev_minimum:
        reasons.append("conservative_ev_not_positive")
    return expected_value, conservative_ev, not reasons, tuple(reasons)
