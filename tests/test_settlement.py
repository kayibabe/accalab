from decimal import Decimal

import pytest

from accalab.markets.registry import market_defaults, normalize_market_name
from accalab.settlement.engine import (
    MissingSettlementData,
    Outcome,
    UnsupportedMarket,
    settle_market,
)


@pytest.mark.parametrize(
    ("market", "selection", "kwargs", "expected"),
    [
        ("Match Winner", "Home", {"home_goals": 2, "away_goals": 1}, Outcome.WIN),
        ("Double Chance", "Home/Draw", {"home_goals": 0, "away_goals": 0}, Outcome.WIN),
        ("Draw No Bet", "Away", {"home_goals": 1, "away_goals": 1}, Outcome.PUSH),
        ("Goals Over/Under", "Over", {"home_goals": 2, "away_goals": 1, "line": 2.5}, Outcome.WIN),
        (
            "Over/Under - First Half",
            "Under",
            {"halftime_home_goals": 0, "halftime_away_goals": 1, "line": 1.5},
            Outcome.WIN,
        ),
        ("Both Teams Score", "Yes", {"home_goals": 1, "away_goals": 1}, Outcome.WIN),
        ("Asian Handicap", "Home", {"home_goals": 2, "away_goals": 0, "line": -1}, Outcome.WIN),
        ("European Handicap", "Draw", {"home_goals": 1, "away_goals": 0, "line": -1}, Outcome.WIN),
        ("Correct Score", "2-1", {"home_goals": 2, "away_goals": 1}, Outcome.WIN),
        (
            "Half Time/Full Time",
            "Home/Draw",
            {"home_goals": 1, "away_goals": 1, "halftime_home_goals": 1, "halftime_away_goals": 0},
            Outcome.WIN,
        ),
        (
            "Team Total Goals",
            "home Over",
            {"home_goals": 2, "away_goals": 0, "line": 1.5},
            Outcome.WIN,
        ),
        ("Goals Odd/Even", "Odd", {"home_goals": 2, "away_goals": 1}, Outcome.WIN),
        (
            "Match Corners",
            "Over",
            {"line": 8.5, "stats": {"home_corners": 6, "away_corners": 4}},
            Outcome.WIN,
        ),
        (
            "Match Cards",
            "Under",
            {"line": 4.5, "stats": {"home_cards": 1, "away_cards": 2}},
            Outcome.WIN,
        ),
    ],
)
def test_supported_market_settlement(market, selection, kwargs, expected) -> None:
    result = settle_market(market, selection, odds="2.2", **kwargs)
    assert result.outcome == expected
    assert result.payout_multiplier == (
        Decimal("2.2")
        if expected == Outcome.WIN
        else Decimal(1)
        if expected == Outcome.PUSH
        else Decimal(0)
    )


@pytest.mark.parametrize(
    ("selection", "line", "home", "away", "expected", "multiplier"),
    [
        ("Home", "-0.25", 1, 1, Outcome.HALF_LOSS, Decimal("0.5")),
        ("Home", "+0.25", 1, 1, Outcome.HALF_WIN, Decimal("1.75")),
        ("Away", "+0.75", 2, 1, Outcome.HALF_LOSS, Decimal("0.5")),
        ("Over", "2.25", 1, 1, Outcome.HALF_LOSS, Decimal("0.5")),
        ("Over", "2.75", 2, 1, Outcome.HALF_WIN, Decimal("1.75")),
        ("Under", "2.0", 1, 1, Outcome.PUSH, Decimal(1)),
    ],
)
def test_quarter_lines_and_pushes(selection, line, home, away, expected, multiplier) -> None:
    market = "Asian Handicap" if selection in {"Home", "Away"} else "Over/Under Goals"
    result = settle_market(market, selection, home_goals=home, away_goals=away, line=line, odds=2.5)
    assert result == type(result)(expected, multiplier)


def test_void_bypasses_missing_scores_and_returns_stake() -> None:
    assert settle_market("1X2", "Home", void=True) == type(settle_market("1X2", "Home", void=True))(
        Outcome.VOID, Decimal(1)
    )


def test_missing_stats_and_unknown_markets_are_explicit() -> None:
    with pytest.raises(MissingSettlementData):
        settle_market("Match Corners", "Over", line=8.5, home_goals=1, away_goals=0)
    with pytest.raises(UnsupportedMarket):
        settle_market("Novel Prop", "Yes", home_goals=1, away_goals=0)


def test_registry_is_conservative_for_exotics_and_unknown_markets() -> None:
    assert normalize_market_name("Goals Over/Under") == "Over/Under Goals"
    assert market_defaults("Goals Over/Under") == (True, True)
    assert market_defaults("Correct Score") == (True, False)
    assert market_defaults("Home Team Total Goals") == (True, False)
    assert market_defaults("Novel Player Prop") == (False, False)
