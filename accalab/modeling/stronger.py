"""Time-ordered LightGBM challengers calibrated and compared with Dixon-Coles."""

from __future__ import annotations

import math
import hashlib
import pickle
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
import tomllib
from lightgbm import LGBMClassifier
from sklearn.isotonic import IsotonicRegression

from accalab.config import ROOT
from accalab.modeling.config import ModelConfig, load_model_config
from accalab.modeling.dixon_coles import MatchResult, fit_dixon_coles
from accalab.modeling.markets import derive_market_forecast
from accalab.modeling.value import probability_lower_bound

FEATURE_VERSION = "team-form-v1"
MARKETS = ("1X2", "OU25", "BTTS")
OUTCOMES = ("Home", "Draw", "Away")
FEATURE_NAMES = (
    "league_id_log",
    "home_played_log",
    "away_played_log",
    "home_goals_for_5",
    "home_goals_against_5",
    "home_points_5",
    "home_goals_for_10",
    "home_goals_against_10",
    "home_points_10",
    "away_goals_for_5",
    "away_goals_against_5",
    "away_points_5",
    "away_goals_for_10",
    "away_goals_against_10",
    "away_points_10",
    "league_goals_for_mean",
    "home_advantage_rate",
)


@dataclass(frozen=True)
class StrongModelConfig:
    minimum_matches: int = 240
    training_fraction: float = 0.65
    calibration_fraction: float = 0.15
    minimum_calibration_matches: int = 60
    minimum_evaluation_matches: int = 60
    minimum_log_loss_improvement: float = 0.005
    blend_steps: int = 10
    estimators: int = 160
    learning_rate: float = 0.04
    num_leaves: int = 15
    random_seed: int = 1729
    artifact_directory: str = "data/models"


def load_strong_model_config() -> StrongModelConfig:
    raw = tomllib.loads((ROOT / "config" / "strong_model.toml").read_text("utf-8"))["training"]
    config = StrongModelConfig(
        minimum_matches=int(raw["minimum_matches"]),
        training_fraction=float(raw["training_fraction"]),
        calibration_fraction=float(raw["calibration_fraction"]),
        minimum_calibration_matches=int(raw["minimum_calibration_matches"]),
        minimum_evaluation_matches=int(raw["minimum_evaluation_matches"]),
        minimum_log_loss_improvement=float(raw["minimum_log_loss_improvement"]),
        blend_steps=int(raw["blend_steps"]),
        estimators=int(raw["estimators"]),
        learning_rate=float(raw["learning_rate"]),
        num_leaves=int(raw["num_leaves"]),
        random_seed=int(raw["random_seed"]),
        artifact_directory=str(raw["artifact_directory"]),
    )
    if (
        config.minimum_matches < 30
        or not 0 < config.training_fraction < 1
        or not 0 < config.calibration_fraction < 1
        or config.training_fraction + config.calibration_fraction >= 1
        or config.blend_steps < 1
        or config.minimum_calibration_matches < 10
        or config.minimum_evaluation_matches < 10
    ):
        raise ValueError("Strong model training and holdout configuration is invalid.")
    return config


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _team_summary(rows: deque[tuple[int, int, int, int]], count: int) -> tuple[float, ...]:
    sample = list(rows)[-count:]
    if not sample:
        return (0.0, 0.0, 0.0)
    return (
        sum(row[0] for row in sample) / len(sample),
        sum(row[1] for row in sample) / len(sample),
        sum(row[2] for row in sample) / len(sample),
    )


def build_feature_rows(
    matches: Iterable[MatchResult],
) -> tuple[np.ndarray, tuple[MatchResult, ...]]:
    """Build features from strictly earlier completed matches, including tied kickoffs safely."""
    ordered = tuple(sorted(matches, key=lambda row: (_aware(row.kickoff_at), row.fixture_id)))
    history: dict[tuple[int, int], deque[tuple[int, int, int, int]]] = defaultdict(
        lambda: deque(maxlen=10)
    )
    league_goals: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])
    vectors: list[list[float]] = []
    cursor = 0
    while cursor < len(ordered):
        instant = _aware(ordered[cursor].kickoff_at)
        stop = cursor
        while stop < len(ordered) and _aware(ordered[stop].kickoff_at) == instant:
            stop += 1
        group = ordered[cursor:stop]
        for match in group:
            home = history[(match.league_id, match.home_team_id)]
            away = history[(match.league_id, match.away_team_id)]
            league = league_goals[match.league_id]
            league_mean = (league[0] / (2 * league[2])) if league[2] else 0.0
            home5 = _team_summary(home, 5)
            home10 = _team_summary(home, 10)
            away5 = _team_summary(away, 5)
            away10 = _team_summary(away, 10)
            vectors.append(
                [
                    math.log1p(match.league_id),
                    math.log1p(len(home)),
                    math.log1p(len(away)),
                    *home5,
                    *home10,
                    *away5,
                    *away10,
                    league_mean,
                    (league[1] / league[2]) if league[2] else 0.5,
                ]
            )
        for match in group:
            home_key = (match.league_id, match.home_team_id)
            away_key = (match.league_id, match.away_team_id)
            home_points = (
                3
                if match.home_goals > match.away_goals
                else 1
                if match.home_goals == match.away_goals
                else 0
            )
            away_points = (
                3
                if match.away_goals > match.home_goals
                else 1
                if match.home_goals == match.away_goals
                else 0
            )
            history[home_key].append((match.home_goals, match.away_goals, home_points, 1))
            history[away_key].append((match.away_goals, match.home_goals, away_points, 0))
            league = league_goals[match.league_id]
            league[0] += match.home_goals + match.away_goals
            league[1] += int(match.home_goals > match.away_goals)
            league[2] += 1
        cursor = stop
    matrix = np.asarray(vectors, dtype=float)
    if matrix.shape != (len(ordered), len(FEATURE_NAMES)):
        raise AssertionError("Feature matrix shape does not match the declared feature version.")
    return matrix, ordered


def _labels(matches: tuple[MatchResult, ...], market: str) -> np.ndarray:
    if market == "1X2":
        return np.asarray(
            [
                0
                if row.home_goals > row.away_goals
                else 1
                if row.home_goals == row.away_goals
                else 2
                for row in matches
            ],
            dtype=int,
        )
    if market == "OU25":
        return np.asarray([int(row.home_goals + row.away_goals >= 3) for row in matches], dtype=int)
    return np.asarray(
        [int(row.home_goals > 0 and row.away_goals > 0) for row in matches], dtype=int
    )


def _classifier(market: str, config: StrongModelConfig) -> LGBMClassifier:
    return LGBMClassifier(
        objective="multiclass" if market == "1X2" else "binary",
        num_class=3 if market == "1X2" else 1,
        n_estimators=config.estimators,
        learning_rate=config.learning_rate,
        num_leaves=config.num_leaves,
        max_depth=-1,
        min_child_samples=20,
        reg_lambda=1.0,
        random_state=config.random_seed,
        n_jobs=1,
        verbosity=-1,
    )


def _calibrators(
    raw: np.ndarray, labels: np.ndarray, market: str
) -> tuple[IsotonicRegression, ...]:
    classes = range(3) if market == "1X2" else range(1)
    calibrators = []
    for index in classes:
        y = (labels == index).astype(int) if market == "1X2" else labels
        column = raw[:, index] if market == "1X2" else raw[:, 1]
        calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        calibrator.fit(column, y)
        calibrators.append(calibrator)
    return tuple(calibrators)


def _calibrate(
    raw: np.ndarray, calibrators: tuple[IsotonicRegression, ...], market: str
) -> np.ndarray:
    if market == "1X2":
        calibrated = np.column_stack(
            [calibrator.predict(raw[:, index]) for index, calibrator in enumerate(calibrators)]
        )
        totals = calibrated.sum(axis=1, keepdims=True)
        return np.divide(calibrated, totals, out=np.full_like(calibrated, 1 / 3), where=totals > 0)
    positive = calibrators[0].predict(raw[:, 1])
    return np.column_stack([1 - positive, positive])


def _log_loss(labels: np.ndarray, probabilities: np.ndarray, market: str) -> float:
    if market == "1X2":
        return float(
            -np.log(np.clip(probabilities[np.arange(len(labels)), labels], 1e-12, 1)).mean()
        )
    p = np.clip(probabilities[:, 1], 1e-12, 1 - 1e-12)
    return float(-(labels * np.log(p) + (1 - labels) * np.log(1 - p)).mean())


def _brier(labels: np.ndarray, probabilities: np.ndarray, market: str) -> float:
    if market == "1X2":
        expected = np.eye(3)[labels]
        return float(np.square(probabilities - expected).sum(axis=1).mean())
    return float(np.square(probabilities[:, 1] - labels).mean())


def _baseline_probabilities(
    matches: tuple[MatchResult, ...],
    fit_rows: tuple[MatchResult, ...],
    *,
    model_config: ModelConfig,
) -> dict[int, dict[str, np.ndarray]]:
    """Monthly rolling-origin Dixon-Coles forecasts for the requested rows."""
    target_months: dict[tuple[int, int], list[MatchResult]] = defaultdict(list)
    for match in matches:
        instant = _aware(match.kickoff_at)
        target_months[(instant.year, instant.month)].append(match)
    baseline: dict[int, dict[str, np.ndarray]] = {}
    for month, target_rows in sorted(target_months.items()):
        month_start = datetime(month[0], month[1], 1, tzinfo=UTC)
        prior = tuple(row for row in fit_rows if _aware(row.kickoff_at) < month_start)
        by_league: dict[int, list[MatchResult]] = defaultdict(list)
        for row in prior:
            by_league[row.league_id].append(row)
        models = {}
        for league_id, rows in by_league.items():
            if len(rows) < model_config.minimum_training_matches:
                continue
            try:
                models[league_id] = fit_dixon_coles(
                    rows,
                    league_id=league_id,
                    as_of=month_start,
                    config=model_config,
                    minimum_matches=model_config.minimum_training_matches,
                )
            except (ArithmeticError, ValueError):
                continue
        for row in target_rows:
            model = models.get(row.league_id)
            if model is None:
                continue
            try:
                matrix = model.score_matrix(
                    row.home_team_id, row.away_team_id, max_goals=model_config.max_goals
                )
                markets = derive_market_forecast(matrix).markets
            except (ArithmeticError, ValueError):
                continue
            baseline[row.fixture_id] = {
                "1X2": np.asarray([markets["1X2"][None][key] for key in OUTCOMES]),
                "OU25": np.asarray(
                    [
                        markets["Over/Under Goals"][2.5]["Under"],
                        markets["Over/Under Goals"][2.5]["Over"],
                    ]
                ),
                "BTTS": np.asarray(
                    [
                        markets["Both Teams To Score"][None]["No"],
                        markets["Both Teams To Score"][None]["Yes"],
                    ]
                ),
            }
    return baseline


def _market_report(
    labels: np.ndarray,
    calibrated: np.ndarray,
    baseline: np.ndarray,
    market: str,
    blend_steps: int,
) -> dict[str, Any]:
    weights = np.linspace(0.0, 1.0, blend_steps + 1)
    weight = min(
        weights,
        key=lambda value: _log_loss(labels, value * calibrated + (1 - value) * baseline, market),
    )
    blended = weight * calibrated + (1 - weight) * baseline
    return {
        "count": int(len(labels)),
        "blend_weight": float(weight),
        "baseline_log_loss": _log_loss(labels, baseline, market),
        "candidate_log_loss": _log_loss(labels, blended, market),
        "baseline_brier": _brier(labels, baseline, market),
        "candidate_brier": _brier(labels, blended, market),
    }


@dataclass
class StrongModelBundle:
    version: str
    trained_through: datetime
    models: dict[str, LGBMClassifier]
    calibrators: dict[str, tuple[IsotonicRegression, ...]]
    blend_weights: dict[str, float]
    calibration_size: int
    metrics: dict[str, Any]

    def predict(self, features: np.ndarray) -> dict[str, np.ndarray]:
        values = pd.DataFrame(
            np.atleast_2d(np.asarray(features, dtype=float)), columns=FEATURE_NAMES
        )
        output = {}
        for market in MARKETS:
            raw = self.models[market].predict_proba(values)
            calibrated = _calibrate(raw, self.calibrators[market], market)
            output[market] = calibrated
        return output

    def predict_with_baseline(
        self, features: np.ndarray, baseline: Mapping[str, np.ndarray]
    ) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        predictions = self.predict(features)
        result = {}
        for market in MARKETS:
            weight = self.blend_weights.get(market, 1.0)
            candidate = predictions[market][0]
            base = np.asarray(baseline[market], dtype=float)
            combined = weight * candidate + (1 - weight) * base
            combined /= combined.sum()
            p_low = np.asarray(
                [probability_lower_bound(float(p), self.calibration_size) for p in combined]
            )
            result[market] = (combined, p_low)
        return result


@dataclass(frozen=True)
class StrongModelTrainingResult:
    bundle: StrongModelBundle | None
    report: dict[str, Any]
    promotable: bool


def train_strong_model(
    matches: Iterable[MatchResult],
    *,
    config: StrongModelConfig | None = None,
    model_config: ModelConfig | None = None,
    as_of: datetime | None = None,
) -> StrongModelTrainingResult:
    """Train on an early period, calibrate on a later holdout and evaluate on the newest holdout."""
    config = config or load_strong_model_config()
    model_config = model_config or load_model_config()
    x, ordered = build_feature_rows(matches)
    n = len(ordered)

    def split_at_time_boundary(index: int) -> int:
        if index <= 0 or index >= n:
            return index
        instant = _aware(ordered[index].kickoff_at)
        while index > 0 and _aware(ordered[index - 1].kickoff_at) == instant:
            index -= 1
        return index

    train_end = split_at_time_boundary(int(n * config.training_fraction))
    calibration_end = split_at_time_boundary(
        int(n * (config.training_fraction + config.calibration_fraction))
    )
    report: dict[str, Any] = {"feature_version": FEATURE_VERSION, "markets": {}}
    if n < config.minimum_matches or train_end < 3:
        return StrongModelTrainingResult(
            None, {**report, "reason": "insufficient completed matches", "matches": n}, False
        )
    if (
        calibration_end - train_end < config.minimum_calibration_matches
        or n - calibration_end < config.minimum_evaluation_matches
    ):
        return StrongModelTrainingResult(
            None, {**report, "reason": "time-ordered holdouts are too small", "matches": n}, False
        )

    train_rows = ordered[:train_end]
    calibration_rows = ordered[train_end:calibration_end]
    evaluation_rows = ordered[calibration_end:]
    report["splits"] = {
        "training_matches": len(train_rows),
        "calibration_matches": len(calibration_rows),
        "evaluation_matches": len(evaluation_rows),
        "training_cutoff": _aware(train_rows[-1].kickoff_at).isoformat(),
        "calibration_cutoff": _aware(calibration_rows[-1].kickoff_at).isoformat(),
        "evaluation_start": _aware(evaluation_rows[0].kickoff_at).isoformat(),
        "time_ordered": True,
    }
    train_x, calibration_x, evaluation_x = (
        pd.DataFrame(values, columns=FEATURE_NAMES)
        for values in (x[:train_end], x[train_end:calibration_end], x[calibration_end:])
    )
    calibration_ids = {row.fixture_id for row in calibration_rows}
    evaluation_ids = {row.fixture_id for row in evaluation_rows}
    train_labels = {market: _labels(train_rows, market) for market in MARKETS}
    calibration_labels = {market: _labels(calibration_rows, market) for market in MARKETS}
    evaluation_labels = {market: _labels(evaluation_rows, market) for market in MARKETS}
    models: dict[str, LGBMClassifier] = {}
    calibrators: dict[str, tuple[IsotonicRegression, ...]] = {}
    cal_predictions: dict[str, dict[int, np.ndarray]] = {}
    eval_predictions: dict[str, dict[int, np.ndarray]] = {}
    for market in MARKETS:
        if len(np.unique(train_labels[market])) < (3 if market == "1X2" else 2):
            return StrongModelTrainingResult(
                None,
                {**report, "reason": f"training split lacks classes for {market}", "matches": n},
                False,
            )
        estimator = _classifier(market, config)
        estimator.fit(train_x, train_labels[market])
        calibrator = _calibrators(
            estimator.predict_proba(calibration_x), calibration_labels[market], market
        )
        models[market] = estimator
        calibrators[market] = calibrator
        cal_values = _calibrate(estimator.predict_proba(calibration_x), calibrator, market)
        eval_values = _calibrate(estimator.predict_proba(evaluation_x), calibrator, market)
        cal_predictions[market] = {
            row.fixture_id: values for row, values in zip(calibration_rows, cal_values, strict=True)
        }
        eval_predictions[market] = {
            row.fixture_id: values for row, values in zip(evaluation_rows, eval_values, strict=True)
        }

    baseline_calibration = _baseline_probabilities(
        calibration_rows,
        tuple(row for row in ordered if row.fixture_id not in calibration_ids),
        model_config=model_config,
    )
    baseline_evaluation = _baseline_probabilities(
        evaluation_rows,
        tuple(row for row in ordered if row.fixture_id not in evaluation_ids),
        model_config=model_config,
    )
    supported_cal_ids = sorted(set(calibration_ids) & set(baseline_calibration))
    supported_eval_ids = sorted(set(evaluation_ids) & set(baseline_evaluation))
    if (
        len(supported_cal_ids) < config.minimum_calibration_matches
        or len(supported_eval_ids) < config.minimum_evaluation_matches
    ):
        return StrongModelTrainingResult(
            None,
            {
                **report,
                "reason": "Dixon-Coles baseline holdout lacks enough known-team forecasts",
                "calibration_baseline_count": len(supported_cal_ids),
                "evaluation_baseline_count": len(supported_eval_ids),
            },
            False,
        )

    blend_weights = {}
    for market in MARKETS:
        cal_y = np.asarray(
            [
                calibration_labels[market][
                    next(
                        i for i, row in enumerate(calibration_rows) if row.fixture_id == fixture_id
                    )
                ]
                for fixture_id in supported_cal_ids
            ]
        )
        cal_p = np.vstack([cal_predictions[market][fixture_id] for fixture_id in supported_cal_ids])
        cal_base = np.vstack(
            [baseline_calibration[fixture_id][market] for fixture_id in supported_cal_ids]
        )
        eval_y = np.asarray(
            [
                evaluation_labels[market][
                    next(i for i, row in enumerate(evaluation_rows) if row.fixture_id == fixture_id)
                ]
                for fixture_id in supported_eval_ids
            ]
        )
        eval_p = np.vstack(
            [eval_predictions[market][fixture_id] for fixture_id in supported_eval_ids]
        )
        eval_base = np.vstack(
            [baseline_evaluation[fixture_id][market] for fixture_id in supported_eval_ids]
        )
        validation = _market_report(cal_y, cal_p, cal_base, market, config.blend_steps)
        weight = validation["blend_weight"]
        final = _market_report(eval_y, eval_p, eval_base, market, max(1, config.blend_steps))
        combined_eval = weight * eval_p + (1 - weight) * eval_base
        final["blend_weight"] = float(weight)
        final["candidate_log_loss"] = _log_loss(eval_y, combined_eval, market)
        final["candidate_brier"] = _brier(eval_y, combined_eval, market)
        final["calibration_log_loss"] = validation["candidate_log_loss"]
        final["calibration_baseline_log_loss"] = validation["baseline_log_loss"]
        final["calibration_count"] = validation["count"]
        blend_weights[market] = float(weight)
        report["markets"][market] = final

    promotable = all(
        values["candidate_log_loss"]
        <= values["baseline_log_loss"] - config.minimum_log_loss_improvement
        and values["candidate_brier"] <= values["baseline_brier"]
        for values in report["markets"].values()
    )
    created_at = _aware(as_of or datetime.now(UTC))
    trained_through = _aware(train_rows[-1].kickoff_at)
    digest = hashlib.sha256(
        ",".join(str(row.fixture_id) for row in train_rows).encode()
    ).hexdigest()[:8]
    version = f"lgbm-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{digest}"
    bundle = StrongModelBundle(
        version=version,
        trained_through=trained_through,
        models=models,
        calibrators=calibrators,
        blend_weights=blend_weights,
        calibration_size=len(supported_cal_ids),
        metrics=report,
    )
    return StrongModelTrainingResult(bundle, report, promotable)


def save_model_bundle(bundle: StrongModelBundle, directory: Path | None = None) -> Path:
    target_dir = directory or ROOT / load_strong_model_config().artifact_directory
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{bundle.version}.pkl"
    temporary = path.with_suffix(".pkl.tmp")
    with temporary.open("wb") as stream:
        pickle.dump(bundle, stream, protocol=pickle.HIGHEST_PROTOCOL)
    temporary.replace(path)
    return path


def load_model_bundle(path: Path) -> StrongModelBundle:
    with path.open("rb") as stream:
        bundle = pickle.load(stream)
    if not isinstance(bundle, StrongModelBundle) or bundle.version == "":
        raise ValueError("The model artifact is invalid or uses an unsupported format.")
    return bundle


def retrain_and_register(
    session,
    *,
    config: StrongModelConfig | None = None,
    model_config: ModelConfig | None = None,
    artifact_directory: Path | None = None,
    as_of: datetime | None = None,
) -> StrongModelTrainingResult:
    """Train a candidate and append its version and promotion evidence to the registry."""
    from sqlalchemy import select

    from accalab.models import Fixture, ModelVersion

    fixtures = session.execute(
        select(
            Fixture.id,
            Fixture.league_id,
            Fixture.kickoff_at,
            Fixture.home_team_id,
            Fixture.away_team_id,
            Fixture.home_goals,
            Fixture.away_goals,
            Fixture.season,
        )
        .where(
            Fixture.home_goals.is_not(None),
            Fixture.away_goals.is_not(None),
            Fixture.status.in_(("FT", "AET", "PEN")),
        )
        .order_by(Fixture.kickoff_at, Fixture.id)
    ).all()
    matches = tuple(
        MatchResult(
            fixture_id=row.id,
            league_id=row.league_id,
            kickoff_at=row.kickoff_at,
            home_team_id=row.home_team_id,
            away_team_id=row.away_team_id,
            home_goals=row.home_goals,
            away_goals=row.away_goals,
            season=row.season,
        )
        for row in fixtures
    )
    result = train_strong_model(matches, config=config, model_config=model_config, as_of=as_of)
    if result.bundle is None:
        return result
    artifact = save_model_bundle(result.bundle, artifact_directory)
    now = _aware(as_of or datetime.now(UTC))
    status = "rejected"
    if result.promotable:
        for current in session.scalars(
            select(ModelVersion).where(ModelVersion.status == "active")
        ).all():
            current.status = "retired"
        status = "active"
    session.add(
        ModelVersion(
            id=result.bundle.version,
            status=status,
            artifact_path=str(artifact.resolve()),
            feature_version=FEATURE_VERSION,
            trained_through=result.bundle.trained_through,
            created_at=now,
            promoted_at=now if status == "active" else None,
            metrics=result.report,
        )
    )
    return result
