"""Derive full-time market forecasts from a single scoreline matrix."""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose
from typing import Mapping

ScoreMatrix = tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class AsianOutcomeMass:
    full_win: float
    half_win: float
    push: float
    half_loss: float
    full_loss: float

@dataclass(frozen=True)
class MarketForecast:
    markets: Mapping[str, Mapping[float | None, Mapping[str, float]]]
    asian_handicaps: Mapping[float, Mapping[str, AsianOutcomeMass]]


def _validate_matrix(matrix: ScoreMatrix) -> None:
    if not matrix or any(len(row) != len(matrix) for row in matrix):
        raise ValueError("Score matrix must be non-empty and square.")
    values = [value for row in matrix for value in row]
    if any(value < 0 for value in values) or not isclose(sum(values), 1.0, abs_tol=1e-8):
        raise ValueError("Score matrix probabilities must be non-negative and sum to one.")


def _asian_outcomes(matrix: ScoreMatrix, *, side: str, line: float) -> AsianOutcomeMass:
    if side not in {"Home", "Away"}:
        raise ValueError("Asian handicap side must be Home or Away.")
    if not isclose(line * 4, round(line * 4), abs_tol=1e-8):
        raise ValueError("Asian handicap lines must use quarter-goal increments.")
    lower = int(round(line * 2)) / 2
    if isclose(line, lower, abs_tol=1e-8):
        component_lines = (line, line)
    elif line > lower:
        component_lines = (lower, lower + 0.5)
    else:
        component_lines = (lower - 0.5, lower)
    masses = {"full_win": 0.0, "half_win": 0.0, "push": 0.0, "half_loss": 0.0, "full_loss": 0.0}
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            margin = home_goals - away_goals if side == "Home" else away_goals - home_goals
            result_types = []
            for handicap in component_lines:
                adjusted = margin + handicap
                result_types.append(1 if adjusted > 0 else 0 if isclose(adjusted, 0, abs_tol=1e-8) else -1)
            pair = tuple(result_types)
            bucket = {
                (1, 1): "full_win",
                (1, 0): "half_win",
                (0, 1): "half_win",
                (0, 0): "push",
                (0, -1): "half_loss",
                (-1, 0): "half_loss",
                (-1, -1): "full_loss",
            }.get(pair)
            if bucket is None:
                raise ArithmeticError("Quarter-line settlement produced an impossible outcome pair.")
            masses[bucket] += probability
    return AsianOutcomeMass(**masses)


def asian_handicap_mass(matrix: ScoreMatrix, *, side: str, line: float) -> AsianOutcomeMass:
    """Return full/half win, push and loss probabilities for an Asian handicap."""
    _validate_matrix(matrix)
    return _asian_outcomes(matrix, side=side, line=line)


def asian_handicap_ev(mass: AsianOutcomeMass, odds: float) -> float:
    if odds <= 1:
        raise ValueError("Decimal odds must exceed 1.")
    return (
        mass.full_win * (odds - 1)
        + mass.half_win * (odds - 1) * 0.5
        - mass.half_loss * 0.5
        - mass.full_loss
    )


def derive_market_forecast(
    matrix: ScoreMatrix,
    *,
    goal_lines: tuple[float, ...] = (0.5, 1.5, 2.5, 3.5, 4.5),
    asian_lines: tuple[float, ...] = (-2.0, -1.5, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0),
) -> MarketForecast:
    """Derive 1X2, double chance, DNB, totals, team totals, BTTS and handicaps."""
    _validate_matrix(matrix)
    size = len(matrix)
    home_win = draw = away_win = 0.0
    btts_yes = 0.0
    home_totals = [0.0] * size
    away_totals = [0.0] * size
    total_goals = [0.0] * (2 * size - 1)
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            if home_goals > away_goals:
                home_win += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away_win += probability
            if home_goals > 0 and away_goals > 0:
                btts_yes += probability
            home_totals[home_goals] += probability
            away_totals[away_goals] += probability
            total_goals[home_goals + away_goals] += probability

    markets: dict[str, dict[float | None, dict[str, float]]] = {
        "1X2": {
            None: {"Home": home_win, "Draw": draw, "Away": away_win}
        },
        "Double Chance": {
            None: {
                "Home/Draw": home_win + draw,
                "Home/Away": home_win + away_win,
                "Draw/Away": draw + away_win,
            }
        },
        "Draw No Bet": {
            None: {
                "Home": home_win / (home_win + away_win) if home_win + away_win else 0.0,
                "Away": away_win / (home_win + away_win) if home_win + away_win else 0.0,
            }
        },
        "Both Teams To Score": {None: {"Yes": btts_yes, "No": 1.0 - btts_yes}},
    }
    total_market: dict[float | None, dict[str, float]] = {}
    home_market: dict[float | None, dict[str, float]] = {}
    away_market: dict[float | None, dict[str, float]] = {}
    for line in goal_lines:
        if not isclose(line * 2, round(line * 2), abs_tol=1e-8):
            raise ValueError("Goal totals must use half-goal increments.")
        over = sum(prob for goals, prob in enumerate(total_goals) if goals > line)
        home_over = sum(prob for goals, prob in enumerate(home_totals) if goals > line)
        away_over = sum(prob for goals, prob in enumerate(away_totals) if goals > line)
        total_market[line] = {"Over": over, "Under": 1.0 - over}
        home_market[line] = {"Over": home_over, "Under": 1.0 - home_over}
        away_market[line] = {"Over": away_over, "Under": 1.0 - away_over}
    markets["Over/Under Goals"] = total_market
    markets["Home Team Total Goals"] = home_market
    markets["Away Team Total Goals"] = away_market
    handicaps = {
        line: {
            side: _asian_outcomes(matrix, side=side, line=line)
            for side in ("Home", "Away")
        }
        for line in asian_lines
    }
    return MarketForecast(markets=markets, asian_handicaps=handicaps)
