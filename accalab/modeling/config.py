"""Model and eligibility thresholds loaded from config/model.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass

from accalab.config import ROOT


@dataclass(frozen=True)
class ModelConfig:
    max_goals: int
    time_decay_per_day: float
    minimum_training_matches: int
    maximum_backtest_teams: int
    regularization: float
    calibration_bins: int
    minimum_odds: float
    maximum_odds: float
    minimum_ev: float
    conservative_ev_minimum: float
    overround_reference: float
    overround_edge_scale: float
    probability_z: float


def load_model_config() -> ModelConfig:
    raw = tomllib.loads((ROOT / "config" / "model.toml").read_text(encoding="utf-8"))
    model = raw["model"]
    evaluation = raw["evaluation"]
    eligibility = raw["eligibility"]
    config = ModelConfig(
        max_goals=int(model["max_goals"]),
        time_decay_per_day=float(model["time_decay_per_day"]),
        minimum_training_matches=int(model["minimum_training_matches"]),
        maximum_backtest_teams=int(evaluation["maximum_backtest_teams"]),
        regularization=float(model["regularization"]),
        calibration_bins=int(evaluation["calibration_bins"]),
        minimum_odds=float(eligibility["minimum_odds"]),
        maximum_odds=float(eligibility["maximum_odds"]),
        minimum_ev=float(eligibility["minimum_ev"]),
        conservative_ev_minimum=float(eligibility["conservative_ev_minimum"]),
        overround_reference=float(eligibility["overround_reference"]),
        overround_edge_scale=float(eligibility["overround_edge_scale"]),
        probability_z=float(eligibility["probability_z"]),
    )
    if (
        config.max_goals < 4
        or config.minimum_training_matches < 3
        or config.maximum_backtest_teams < 3
    ):
        raise ValueError("Model goal range and minimum training sample are too small.")
    if config.minimum_odds <= 1 or config.maximum_odds < config.minimum_odds:
        raise ValueError("Configured decimal odds range is invalid.")
    return config
