from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from accalab.modeling.config import load_model_config
from accalab.modeling.dixon_coles import MatchResult
from accalab.modeling.stronger import (
    StrongModelConfig,
    build_feature_rows,
    load_model_bundle,
    save_model_bundle,
    train_strong_model,
)


def matches(count: int = 240) -> list[MatchResult]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        home = index % 6
        away = (home + 1 + (index // 6) % 5) % 6
        rows.append(
            MatchResult(
                fixture_id=index + 1,
                league_id=39,
                kickoff_at=start + timedelta(days=index),
                home_team_id=home + 1,
                away_team_id=away + 1,
                home_goals=(index * 3 + home) % 4,
                away_goals=(index * 7 + away) % 3,
            )
        )
    return rows


def test_features_for_simultaneous_kickoffs_do_not_leak_between_matches() -> None:
    rows = matches(8)
    shared_time = rows[4].kickoff_at
    rows[5] = MatchResult(
        **{
            **rows[5].__dict__,
            "kickoff_at": shared_time,
            "home_team_id": rows[4].home_team_id,
            "away_team_id": rows[4].away_team_id,
        }
    )
    matrix, ordered = build_feature_rows(rows)
    first_index = next(index for index, row in enumerate(ordered) if row.fixture_id == 5)
    second_index = next(index for index, row in enumerate(ordered) if row.fixture_id == 6)
    assert matrix[first_index, 1:15].tolist() == matrix[second_index, 1:15].tolist()


def test_lightgbm_candidates_are_calibrated_blended_and_versioned(tmp_path) -> None:
    config = StrongModelConfig(
        minimum_matches=200,
        minimum_calibration_matches=20,
        minimum_evaluation_matches=20,
        estimators=12,
        blend_steps=4,
    )
    model_config = load_model_config()
    model_config = type(model_config)(**{**model_config.__dict__, "minimum_training_matches": 20})
    result = train_strong_model(matches(), config=config, model_config=model_config)

    assert result.bundle is not None
    assert set(result.bundle.models) == {"1X2", "OU25", "BTTS"}
    assert set(result.report["markets"]) == {"1X2", "OU25", "BTTS"}
    assert all(0 <= weight <= 1 for weight in result.bundle.blend_weights.values())
    predictions = result.bundle.predict(np.zeros((1, 17)))
    assert np.asarray(predictions["1X2"]).sum() == pytest.approx(1.0)
    assert np.asarray(predictions["OU25"]).sum() == pytest.approx(1.0)
    artifact = save_model_bundle(result.bundle, tmp_path)
    restored = load_model_bundle(artifact)
    assert restored.version == result.bundle.version
    assert restored.calibration_size == result.bundle.calibration_size
    for values in result.report["markets"].values():
        assert values["count"] >= config.minimum_evaluation_matches
        assert values["candidate_log_loss"] >= 0
