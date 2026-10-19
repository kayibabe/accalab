"""Time-decayed, per-league Dixon-Coles Poisson goal model."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

import numpy as np
from scipy.optimize import minimize

from accalab.modeling.config import ModelConfig, load_model_config


@dataclass(frozen=True)
class MatchResult:
    fixture_id: int
    league_id: int
    kickoff_at: datetime
    home_team_id: int
    away_team_id: int
    home_goals: int
    away_goals: int


@dataclass(frozen=True)
class DixonColesModel:
    league_id: int
    as_of: datetime
    team_attack: dict[int, float]
    team_defence: dict[int, float]
    home_advantage: float
    rho: float
    training_match_ids: tuple[int, ...]
    effective_sample_size: float
    optimizer_converged: bool
    log_likelihood: float

    def score_matrix(
        self, home_team_id: int, away_team_id: int, max_goals: int = 10
    ) -> tuple[tuple[float, ...], ...]:
        if home_team_id == away_team_id:
            raise ValueError("A fixture must contain two different teams.")
        unknown = {home_team_id, away_team_id} - self.team_attack.keys()
        if unknown:
            raise ValueError(f"Teams are missing from this league model: {sorted(unknown)}")
        if max_goals < 1:
            raise ValueError("max_goals must be positive.")
        lam = math.exp(
            self.home_advantage
            + self.team_attack[home_team_id]
            + self.team_defence[away_team_id]
        )
        mu = math.exp(self.team_attack[away_team_id] + self.team_defence[home_team_id])
        rho = _safe_rho(self.rho, lam, mu)
        matrix = []
        for home_goals in range(max_goals + 1):
            row = []
            for away_goals in range(max_goals + 1):
                tau = _tau(home_goals, away_goals, lam, mu, rho)
                row.append(
                    _poisson_probability(home_goals, lam)
                    * _poisson_probability(away_goals, mu)
                    * tau
                )
            matrix.append(row)
        total = sum(sum(row) for row in matrix)
        if total <= 0 or not math.isfinite(total):
            raise ArithmeticError("The fitted model produced an invalid score matrix.")
        return tuple(tuple(value / total for value in row) for row in matrix)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _tau(home_goals: int, away_goals: int, lam: float, mu: float, rho: float) -> float:
    if (home_goals, away_goals) == (0, 0):
        return 1.0 - lam * mu * rho
    if (home_goals, away_goals) == (0, 1):
        return 1.0 + lam * rho
    if (home_goals, away_goals) == (1, 0):
        return 1.0 + mu * rho
    if (home_goals, away_goals) == (1, 1):
        return 1.0 - rho
    return 1.0


def _safe_rho(rho: float, lam: float, mu: float) -> float:
    """Keep Dixon-Coles low-score corrections positive for every predicted score."""
    lower = max(-0.2, -0.999 / lam, -0.999 / mu)
    upper = min(0.2, 0.999 / (lam * mu), 0.999)
    if lower > upper:
        raise ArithmeticError("No valid Dixon-Coles rho range for these expected goal rates.")
    return min(upper, max(lower, rho))


def _poisson_log_probability(goals: int, rate: float) -> float:
    return goals * math.log(rate) - rate - math.lgamma(goals + 1)


def _poisson_probability(goals: int, rate: float) -> float:
    return math.exp(_poisson_log_probability(goals, rate))


def fit_dixon_coles(
    matches: Iterable[MatchResult],
    *,
    league_id: int,
    as_of: datetime,
    config: ModelConfig | None = None,
    minimum_matches: int | None = None,
) -> DixonColesModel:
    config = config or load_model_config()
    cutoff = _aware(as_of)
    rows = sorted(
        (row for row in matches if row.league_id == league_id),
        key=lambda row: (_aware(row.kickoff_at), row.fixture_id),
    )
    if any(_aware(row.kickoff_at) >= cutoff for row in rows):
        raise ValueError("Training data must finish strictly before the model as_of time.")
    if any(
        row.home_goals < 0
        or row.away_goals < 0
        or row.home_team_id == row.away_team_id
        for row in rows
    ):
        raise ValueError("Training results contain invalid scores or teams.")
    min_rows = minimum_matches if minimum_matches is not None else config.minimum_training_matches
    teams = sorted({row.home_team_id for row in rows} | {row.away_team_id for row in rows})
    if len(rows) < min_rows or len(teams) < 3:
        raise ValueError(f"Need at least {min_rows} matches across three teams to fit this league.")
    indexes = {team_id: index for index, team_id in enumerate(teams)}
    ages = [max(0.0, (cutoff - _aware(row.kickoff_at)).total_seconds() / 86400) for row in rows]
    weights = np.exp(-config.time_decay_per_day * np.asarray(ages, dtype=float))
    effective_n = float(weights.sum() ** 2 / np.square(weights).sum())
    team_count = len(teams)
    home_mean = sum(row.home_goals for row in rows) / len(rows)
    away_mean = sum(row.away_goals for row in rows) / len(rows)
    initial = np.zeros(2 * (team_count - 1) + 2, dtype=float)
    initial[-2] = math.log(max(home_mean, 0.1) / max(away_mean, 0.1))

    def unpack(params: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
        size = team_count - 1
        attacks = np.append(params[:size], -float(params[:size].sum()))
        defenses = np.append(params[size : 2 * size], -float(params[size : 2 * size].sum()))
        return attacks, defenses, float(params[-2]), float(params[-1])

    def objective(params: np.ndarray) -> float:
        attacks, defenses, home_advantage, rho = unpack(params)
        loss = 0.0
        for row, weight in zip(rows, weights, strict=True):
            home_index = indexes[row.home_team_id]
            away_index = indexes[row.away_team_id]
            try:
                lam = math.exp(home_advantage + attacks[home_index] + defenses[away_index])
                mu = math.exp(attacks[away_index] + defenses[home_index])
            except OverflowError:
                return 1e100
            tau = _tau(row.home_goals, row.away_goals, lam, mu, rho)
            if tau <= 0 or not math.isfinite(tau):
                return 1e100
            if any(
                _tau(home_score, away_score, lam, mu, rho) <= 0
                for home_score, away_score in ((0, 0), (0, 1), (1, 0), (1, 1))
            ):
                return 1e100
            log_probability = (
                _poisson_log_probability(row.home_goals, lam)
                + _poisson_log_probability(row.away_goals, mu)
                + math.log(tau)
            )
            loss -= float(weight) * log_probability
        return loss + config.regularization * float(np.square(params[:-1]).sum())

    bounds = (
        [(-2.5, 2.5)] * (2 * (team_count - 1))
        + [(-1.0, 1.0), (-0.2, 0.2)]
    )
    result = minimize(
        objective,
        initial,
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": 500, "ftol": 1e-10},
    )
    if not np.isfinite(result.fun):
        raise ArithmeticError("Dixon-Coles optimization returned a non-finite likelihood.")
    attacks, defenses, home_advantage, rho = unpack(result.x)
    return DixonColesModel(
        league_id=league_id,
        as_of=cutoff,
        team_attack={team_id: float(attacks[index]) for index, team_id in enumerate(teams)},
        team_defence={team_id: float(defenses[index]) for index, team_id in enumerate(teams)},
        home_advantage=home_advantage,
        rho=rho,
        training_match_ids=tuple(row.fixture_id for row in rows),
        effective_sample_size=effective_n,
        optimizer_converged=bool(result.success),
        log_likelihood=-float(result.fun),
    )
