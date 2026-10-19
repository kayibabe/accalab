"""Expanding-window, strictly pre-kickoff evaluation of the production model."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping

from accalab.modeling.config import ModelConfig, load_model_config
from accalab.modeling.dixon_coles import MatchResult, fit_dixon_coles
from accalab.modeling.markets import derive_market_forecast

OUTCOME_KEYS = ("Home", "Draw", "Away")


@dataclass(frozen=True)
class ForecastRecord:
    fixture_id: int
    league_id: int
    kickoff_at: datetime
    actual: str
    probabilities: Mapping[str, float]
    training_match_ids: tuple[int, ...]
    training_cutoff: datetime
    optimizer_converged: bool


@dataclass(frozen=True)
class LeagueMetrics:
    league_id: int
    test_count: int
    training_count_min: int
    training_count_median: int
    log_loss: float
    brier_score: float
    expected_calibration_error: float
    calibration_bins: tuple[tuple[int, int, float, float], ...]


@dataclass(frozen=True)
class BacktestReport:
    run_as_of: datetime
    forecasts: tuple[ForecastRecord, ...]
    league_metrics: tuple[LeagueMetrics, ...]
    skipped_leagues: Mapping[int, str]


def _actual_outcome(match: MatchResult) -> str:
    if match.home_goals > match.away_goals:
        return "Home"
    if match.home_goals == match.away_goals:
        return "Draw"
    return "Away"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def walk_forward_forecasts(
    matches: Iterable[MatchResult],
    *,
    config: ModelConfig | None = None,
    minimum_training_matches: int | None = None,
) -> tuple[ForecastRecord, ...]:
    """Fit only on completed fixtures strictly earlier than each test kickoff."""
    config = config or load_model_config()
    min_train = (
        config.minimum_training_matches
        if minimum_training_matches is None
        else minimum_training_matches
    )
    if min_train < 3:
        raise ValueError("Walk-forward evaluation requires at least three training matches.")
    grouped: dict[int, list[MatchResult]] = defaultdict(list)
    for match in matches:
        grouped[match.league_id].append(match)
    forecasts = []
    for league_id, league_matches in sorted(grouped.items()):
        league_matches.sort(key=lambda row: (_aware(row.kickoff_at), row.fixture_id))
        for index, test_match in enumerate(league_matches):
            cutoff = _aware(test_match.kickoff_at)
            training = [row for row in league_matches[:index] if _aware(row.kickoff_at) < cutoff]
            if len(training) < min_train:
                continue
            try:
                model = fit_dixon_coles(
                    training,
                    league_id=league_id,
                    as_of=cutoff,
                    config=config,
                    minimum_matches=min_train,
                )
                matrix = model.score_matrix(
                    test_match.home_team_id,
                    test_match.away_team_id,
                    max_goals=config.max_goals,
                )
                result = derive_market_forecast(matrix).markets["1X2"][None]
            except ValueError as exc:
                # Early-season teams can enter a league before enough observations exist.
                if "missing from this league model" in str(exc):
                    continue
                raise
            training_ids = set(model.training_match_ids)
            if test_match.fixture_id in training_ids or any(
                _aware(row.kickoff_at) >= cutoff for row in training
            ):
                raise AssertionError("Walk-forward leakage detected in the training window.")
            total = sum(result[key] for key in OUTCOME_KEYS)
            forecasts.append(
                ForecastRecord(
                    fixture_id=test_match.fixture_id,
                    league_id=league_id,
                    kickoff_at=cutoff,
                    actual=_actual_outcome(test_match),
                    probabilities={key: result[key] / total for key in OUTCOME_KEYS},
                    training_match_ids=model.training_match_ids,
                    training_cutoff=model.as_of,
                    optimizer_converged=model.optimizer_converged,
                )
            )
    return tuple(forecasts)


def _calibration_bins(
    forecasts: list[ForecastRecord], bin_count: int
) -> tuple[tuple[tuple[int, int, float, float], ...], float]:
    bins: list[list[tuple[float, float]]] = [[] for _ in range(bin_count)]
    for forecast in forecasts:
        for outcome, probability in forecast.probabilities.items():
            index = min(bin_count - 1, int(probability * bin_count))
            bins[index].append((probability, 1.0 if forecast.actual == outcome else 0.0))
    details = []
    total = sum(len(row) for row in bins)
    ece = 0.0
    for index, rows in enumerate(bins):
        if not rows:
            continue
        mean_prediction = sum(row[0] for row in rows) / len(rows)
        observed_rate = sum(row[1] for row in rows) / len(rows)
        ece += len(rows) / total * abs(mean_prediction - observed_rate)
        details.append((index, len(rows), mean_prediction, observed_rate))
    return tuple(details), ece


def calculate_metrics(
    forecasts: Iterable[ForecastRecord], *, calibration_bins: int | None = None, config: ModelConfig | None = None
) -> tuple[LeagueMetrics, ...]:
    config = config or load_model_config()
    grouped: dict[int, list[ForecastRecord]] = defaultdict(list)
    for forecast in forecasts:
        grouped[forecast.league_id].append(forecast)
    report = []
    for league_id, rows in sorted(grouped.items()):
        log_loss = 0.0
        brier = 0.0
        for forecast in rows:
            probability = max(1e-15, forecast.probabilities[forecast.actual])
            log_loss -= math.log(probability)
            brier += sum(
                (forecast.probabilities[outcome] - (1.0 if forecast.actual == outcome else 0.0)) ** 2
                for outcome in OUTCOME_KEYS
            )
        bins, ece = _calibration_bins(rows, calibration_bins or config.calibration_bins)
        training_sizes = [len(row.training_match_ids) for row in rows]
        sorted_sizes = sorted(training_sizes)
        report.append(
            LeagueMetrics(
                league_id=league_id,
                test_count=len(rows),
                training_count_min=min(training_sizes),
                training_count_median=sorted_sizes[len(sorted_sizes) // 2],
                log_loss=log_loss / len(rows),
                brier_score=brier / len(rows),
                expected_calibration_error=ece,
                calibration_bins=bins,
            )
        )
    return tuple(report)


def run_walk_forward_backtest(
    matches: Iterable[MatchResult],
    *,
    run_as_of: datetime | None = None,
    config: ModelConfig | None = None,
    minimum_training_matches: int | None = None,
) -> BacktestReport:
    config = config or load_model_config()
    rows = tuple(matches)
    forecasts = walk_forward_forecasts(
        rows, config=config, minimum_training_matches=minimum_training_matches
    )
    metrics = calculate_metrics(forecasts, config=config)
    all_leagues = {row.league_id for row in rows}
    evaluated = {row.league_id for row in forecasts}
    skipped = {
        league_id: "Insufficient earlier results or an unseen team in the evaluation window."
        for league_id in sorted(all_leagues - evaluated)
    }
    return BacktestReport(
        run_as_of=_aware(run_as_of or datetime.now(UTC)),
        forecasts=forecasts,
        league_metrics=metrics,
        skipped_leagues=skipped,
    )


def render_backtest_report(report: BacktestReport, *, config: ModelConfig | None = None) -> str:
    config = config or load_model_config()
    lines = [
        "# Phase 2 model evaluation",
        "",
        f"**Backtest (simulated)** · generated {report.run_as_of.isoformat()}",
        "",
        "This expanding-window evaluation predicts each match before kickoff using only earlier results in that league. It reports model quality, not betting performance or a live record.",
        "",
        f"Time decay: exp(-{config.time_decay_per_day:g} × age in days); score matrix maximum: {config.max_goals} goals per side.",
        "",
        "| League ID | Forecasts | Earlier results (min / median) | Log loss | Brier (3 outcomes) | Calibration error |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report.league_metrics:
        lines.append(
            f"| {row.league_id} | {row.test_count} | {row.training_count_min} / {row.training_count_median} | "
            f"{row.log_loss:.4f} | {row.brier_score:.4f} | {row.expected_calibration_error:.4f} |"
        )
    if not report.league_metrics:
        lines.append("| - | 0 | - | - | - | - |")
        lines.extend(
            [
                "",
                "No completed fixtures were available for evaluation, or no league had enough earlier results to form a test forecast.",
            ]
        )
    lines.extend(["", "Calibration bins compare mean predicted probability with observed outcome frequency (one-vs-rest, all three outcomes):", ""])
    for row in report.league_metrics:
        lines.extend([f"### League {row.league_id}", "", "| Bin | Forecasts | Mean predicted | Observed rate |", "|---:|---:|---:|---:|"])
        for bin_index, count, predicted, observed in row.calibration_bins:
            lines.append(f"| {bin_index + 1} | {count} | {predicted:.4f} | {observed:.4f} |")
        lines.append("")
    if report.skipped_leagues:
        lines.extend(["## Not evaluated", ""])
        lines.extend(f"- League {league_id}: {reason}" for league_id, reason in report.skipped_leagues.items())
        lines.append("")
    lines.extend(
        [
            "## Reading this report",
            "",
            "Log loss and Brier score are proper scoring rules; lower is better. Calibration error summarizes the gap between predicted probabilities and observed frequencies. League samples and test forecasts are listed above; small samples are diagnostic only. No edge or future outcome is established by these figures.",
            "",
        ]
    )
    return "\n".join(lines)


def write_backtest_report(path: Path, report: BacktestReport, *, config: ModelConfig | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_backtest_report(report, config=config), encoding="utf-8")
