from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from accalab.modeling.backtest import (
    calculate_metrics,
    render_backtest_report,
    run_walk_forward_backtest,
    walk_forward_forecasts,
)
from accalab.modeling.config import load_model_config
from accalab.modeling.dixon_coles import MatchResult, fit_dixon_coles
from accalab.modeling.markets import (
    asian_handicap_ev,
    asian_handicap_mass,
    derive_market_forecast,
)
from accalab.modeling.value import (
    assess_asian_handicap,
    assess_leg,
    de_vig,
    probability_lower_bound,
    required_ev_threshold,
)


def synthetic_matches(count: int = 48, *, league_id: int = 39) -> list[MatchResult]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    matches = []
    for index in range(count):
        home = index % 6
        offset = 1 + ((index // 6) % 5)
        away = (home + offset) % 6
        matches.append(
            MatchResult(
                fixture_id=index + league_id * 1000,
                league_id=league_id,
                kickoff_at=start + timedelta(days=index),
                home_team_id=home + 1,
                away_team_id=away + 1,
                home_goals=(index * 3 + home) % 4,
                away_goals=(index * 7 + away) % 3,
            )
        )
    return matches


def test_dixon_coles_fit_is_time_decayed_and_returns_valid_matrix() -> None:
    matches = synthetic_matches()
    cutoff = matches[-1].kickoff_at + timedelta(days=1)
    model = fit_dixon_coles(matches, league_id=39, as_of=cutoff, minimum_matches=20)
    matrix = model.score_matrix(1, 2, max_goals=8)
    assert model.optimizer_converged
    assert model.effective_sample_size < len(matches)
    assert model.training_match_ids == tuple(row.fixture_id for row in matches)
    assert sum(sum(row) for row in matrix) == pytest.approx(1.0)
    assert all(probability >= 0 for row in matrix for probability in row)
    assert all(sum(model.team_attack.values()) == pytest.approx(0.0) for _ in [0])
    with pytest.raises(ValueError, match="strictly before"):
        fit_dixon_coles(matches, league_id=39, as_of=matches[-1].kickoff_at, minimum_matches=20)


def test_all_core_markets_are_derived_from_one_score_matrix() -> None:
    matrix = tuple(
        tuple(0.0 if (home, away) not in {(0, 0), (1, 0), (0, 1), (1, 1), (2, 0)} else 0.2 for away in range(3))
        for home in range(3)
    )
    forecast = derive_market_forecast(matrix, goal_lines=(1.5, 2.5), asian_lines=(-0.25, 0.0, 0.25))
    markets = forecast.markets
    assert markets["1X2"][None] == {"Home": 0.4, "Draw": 0.4, "Away": 0.2}
    assert sum(markets["Double Chance"][None].values()) > 1
    assert sum(markets["Draw No Bet"][None].values()) == pytest.approx(1.0)
    assert sum(markets["Both Teams To Score"][None].values()) == pytest.approx(1.0)
    for line in (1.5, 2.5):
        assert sum(markets["Over/Under Goals"][line].values()) == pytest.approx(1.0)
    for line, sides in forecast.asian_handicaps.items():
        for mass in sides.values():
            assert sum((mass.full_win, mass.half_win, mass.push, mass.half_loss, mass.full_loss)) == pytest.approx(1.0)
        assert asian_handicap_ev(sides["Home"], 2.0) == pytest.approx(
            (sides["Home"].full_win + 0.5 * sides["Home"].half_win) -
            (sides["Home"].full_loss + 0.5 * sides["Home"].half_loss)
        )
    quarter = asian_handicap_mass(matrix, side="Home", line=-0.25)
    assert quarter.half_loss == pytest.approx(0.4)


def test_market_de_vig_and_configured_leg_eligibility() -> None:
    result = de_vig({"Home": 2.0, "Draw": 3.0, "Away": 6.0})
    assert result.overround == pytest.approx(0.0)
    assert sum(result.fair_probabilities.values()) == pytest.approx(1.0)
    assert result.fair_probabilities["Home"] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        de_vig({"Home": 1.0, "Away": 2.0})

    config = load_model_config()
    assert required_ev_threshold(0.10, config) > config.minimum_ev
    assert probability_lower_bound(0.60, 100, config=config) < 0.60
    eligible = assess_leg(
        probability=0.60,
        p_low=0.56,
        odds=2.0,
        overround=0.10,
        market_ticket_eligible=True,
        config=config,
    )
    assert eligible.eligible
    assert eligible.ev == pytest.approx(0.2)
    assert eligible.conservative_ev == pytest.approx(0.12)
    ineligible = assess_leg(
        probability=0.60,
        p_low=0.56,
        odds=2.0,
        overround=0.10,
        market_ticket_eligible=False,
        config=config,
    )
    assert not ineligible.eligible
    assert "market_not_ticket_eligible" in ineligible.reasons


def test_asian_value_uses_settlement_masses_and_lower_ev() -> None:
    mass = asian_handicap_mass(
        ((0.2, 0.0, 0.0), (0.0, 0.5, 0.0), (0.3, 0.0, 0.0)),
        side="Home",
        line=-0.25,
    )
    ev, conservative, eligible, reasons = assess_asian_handicap(
        mass=mass,
        odds=2.0,
        overround=0.05,
        effective_sample_size=1000,
        market_ticket_eligible=True,
    )
    assert ev == pytest.approx(asian_handicap_ev(mass, 2.0))
    assert conservative <= ev
    assert eligible is False
    assert "ev_below_overround_scaled_threshold" in reasons


def test_walk_forward_forecasts_exclude_test_and_future_results() -> None:
    matches = synthetic_matches(42)
    forecasts = walk_forward_forecasts(matches, minimum_training_matches=12)
    assert forecasts
    source_by_id = {row.fixture_id: row for row in matches}
    for forecast in forecasts:
        assert forecast.fixture_id not in forecast.training_match_ids
        assert set(forecast.training_match_ids).isdisjoint(
            {row.fixture_id for row in matches if row.kickoff_at >= forecast.kickoff_at}
        )
        assert source_by_id[forecast.fixture_id].kickoff_at > forecast.training_cutoff - timedelta(microseconds=1)
        assert set(forecast.probabilities) == {"Home", "Draw", "Away"}
        assert sum(forecast.probabilities.values()) == pytest.approx(1.0)
    metrics = calculate_metrics(forecasts)
    assert metrics[0].test_count == len(forecasts)
    assert metrics[0].log_loss > 0
    assert metrics[0].brier_score >= 0
    assert 0 <= metrics[0].expected_calibration_error <= 1
    report = run_walk_forward_backtest(matches, minimum_training_matches=12)
    rendered = render_backtest_report(report)
    assert "Backtest (simulated)" in rendered
    assert "Log loss" in rendered
    assert "ROI" not in rendered
    with pytest.raises(ValueError, match="at least three"):
        walk_forward_forecasts(matches, minimum_training_matches=0)


def test_empty_report_explains_that_no_leagues_were_evaluated() -> None:
    report = run_walk_forward_backtest([])
    rendered = render_backtest_report(report)
    assert "No completed fixtures were available" in rendered
    assert "| - | 0 |" in rendered


def test_season_walk_forward_fits_before_the_test_season() -> None:
    matches = [
        MatchResult(
            fixture_id=row.fixture_id,
            league_id=row.league_id,
            kickoff_at=row.kickoff_at,
            home_team_id=row.home_team_id,
            away_team_id=row.away_team_id,
            home_goals=row.home_goals,
            away_goals=row.away_goals,
            season=2024 if index < 24 else 2025,
        )
        for index, row in enumerate(synthetic_matches(48))
    ]
    report = run_walk_forward_backtest(matches, minimum_training_matches=12)
    assert len(report.forecasts) == 24
    assert report.league_metrics[0].test_count == 24
    for forecast in report.forecasts:
        assert forecast.fixture_id not in forecast.training_match_ids
        assert forecast.training_cutoff < forecast.kickoff_at
        assert all(
            next(row for row in matches if row.fixture_id == fixture_id).season == 2024
            for fixture_id in forecast.training_match_ids
        )
