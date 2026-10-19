"""Deterministic settlement for supported pre-match markets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Mapping

from accalab.markets.registry import normalize_market_name


class Outcome(StrEnum):
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"
    HALF_WIN = "half_win"
    HALF_LOSS = "half_loss"


@dataclass(frozen=True)
class Settlement:
    outcome: Outcome
    payout_multiplier: Decimal


class UnsupportedMarket(ValueError):
    """Raised when a market has no settlement rule."""


class MissingSettlementData(ValueError):
    """Raised when the final fixture facts needed to settle are absent."""


def _required(value: int | None, field: str) -> int:
    if value is None:
        raise MissingSettlementData(f"{field} is required for settlement.")
    return value


def _side(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "", value.casefold())
    aliases = {
        "1": "home",
        "home": "home",
        "h": "home",
        "x": "draw",
        "draw": "draw",
        "d": "draw",
        "2": "away",
        "away": "away",
        "a": "away",
    }
    if key not in aliases:
        raise ValueError(f"Unrecognized result selection: {value!r}.")
    return aliases[key]


def _result(home: int, away: int) -> str:
    return "home" if home > away else "away" if away > home else "draw"


def _decimal_odds(odds: Decimal | float | str) -> Decimal:
    value = Decimal(str(odds))
    if value < 1:
        raise ValueError("Decimal odds must be at least 1.0.")
    return value


def _from_components(statuses: list[str], odds: Decimal) -> Settlement:
    if all(status == "win" for status in statuses):
        return Settlement(Outcome.WIN, odds)
    if all(status == "loss" for status in statuses):
        return Settlement(Outcome.LOSS, Decimal(0))
    if all(status == "push" for status in statuses):
        return Settlement(Outcome.PUSH, Decimal(1))
    if "loss" not in statuses and "win" in statuses:
        return Settlement(Outcome.HALF_WIN, (odds + Decimal(1)) / 2)
    if "win" not in statuses and "loss" in statuses:
        return Settlement(Outcome.HALF_LOSS, Decimal("0.5"))
    # A split Asian line can never have a winning and losing half for integer scores.
    raise ValueError("Invalid split settlement components.")


def _split_line(line: Decimal) -> list[Decimal]:
    quarter = line * 4
    if quarter != quarter.to_integral_value():
        raise ValueError("Asian lines must be in quarter-goal increments.")
    if int(quarter) % 2 == 0:
        return [line]
    return [line - Decimal("0.25"), line + Decimal("0.25")]


def _over_under(total: int, selection: str, line: Decimal, odds: Decimal) -> Settlement:
    word = selection.strip().casefold().split()[-1]
    if word not in {"over", "under"}:
        raise ValueError(f"Unrecognized over/under selection: {selection!r}.")
    components = []
    for component_line in _split_line(line):
        if total == component_line:
            status = "push"
        elif (total > component_line) == (word == "over"):
            status = "win"
        else:
            status = "loss"
        components.append(status)
    return _from_components(components, odds)


def _handicap(home: int, away: int, selection: str, line: Decimal, odds: Decimal) -> Settlement:
    side = _side(selection)
    if side == "draw":
        raise ValueError("Asian handicap selection must be Home or Away.")
    margin = Decimal(home - away) if side == "home" else Decimal(away - home)
    return _from_components(
        [
            "win" if margin + item > 0 else "loss" if margin + item < 0 else "push"
            for item in _split_line(line)
        ],
        odds,
    )


def _parse_score(selection: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", selection)
    if not match:
        raise ValueError(f"Unrecognized correct-score selection: {selection!r}.")
    return int(match.group(1)), int(match.group(2))


def _pair_selection(selection: str) -> tuple[str, str]:
    tokens = re.split(r"\s*/\s*|\s+to\s+", selection.strip(), maxsplit=1, flags=re.I)
    if len(tokens) != 2:
        raise ValueError(f"Expected two-part selection such as Home/Draw: {selection!r}.")
    return _side(tokens[0]), _side(tokens[1])


def _stat_total(stats: Mapping[str, int | float | None] | None, names: tuple[str, ...]) -> int:
    if stats is None or any(stats.get(name) is None for name in names):
        raise MissingSettlementData(f"Stats {', '.join(names)} are required for settlement.")
    return int(sum(stats[name] for name in names if stats[name] is not None))


def settle_market(
    market_name: str,
    selection: str,
    *,
    home_goals: int | None = None,
    away_goals: int | None = None,
    line: Decimal | float | str | None = None,
    odds: Decimal | float | str = 2,
    halftime_home_goals: int | None = None,
    halftime_away_goals: int | None = None,
    stats: Mapping[str, int | float | None] | None = None,
    void: bool = False,
) -> Settlement:
    """Settle a supported market; payout multiplier is return per unit staked."""
    price = _decimal_odds(odds)
    if void:
        return Settlement(Outcome.VOID, Decimal(1))
    market = normalize_market_name(market_name)
    if market in {"Match Corners", "Match Cards"}:
        if line is None:
            raise MissingSettlementData("line is required for statistical totals.")
        names = (
            ("home_corners", "away_corners")
            if market == "Match Corners"
            else ("home_cards", "away_cards")
        )
        return _over_under(_stat_total(stats, names), selection, Decimal(str(line)), price)
    if market in {"Over/Under Goals - First Half", "Asian Handicap - First Half"}:
        home = _required(halftime_home_goals, "halftime_home_goals")
        away = _required(halftime_away_goals, "halftime_away_goals")
        base_market = market.removesuffix(" - First Half")
    else:
        home = _required(home_goals, "home_goals")
        away = _required(away_goals, "away_goals")
        base_market = market

    if base_market == "1X2":
        return Settlement(
            Outcome.WIN if _side(selection) == _result(home, away) else Outcome.LOSS,
            price if _side(selection) == _result(home, away) else Decimal(0),
        )
    if base_market == "Double Chance":
        result = _result(home, away)
        selected = re.split(r"\s*/\s*|\s+or\s+", selection, flags=re.I)
        return Settlement(
            Outcome.WIN if result in {_side(item) for item in selected} else Outcome.LOSS,
            price if result in {_side(item) for item in selected} else Decimal(0),
        )
    if base_market == "Draw No Bet":
        side = _side(selection)
        result = _result(home, away)
        outcome = (
            Outcome.PUSH if result == "draw" else Outcome.WIN if result == side else Outcome.LOSS
        )
        return Settlement(
            outcome,
            price
            if outcome == Outcome.WIN
            else Decimal(1)
            if outcome == Outcome.PUSH
            else Decimal(0),
        )
    if base_market == "Over/Under Goals":
        if line is None:
            raise MissingSettlementData("line is required for over/under settlement.")
        return _over_under(home + away, selection, Decimal(str(line)), price)
    if base_market == "Both Teams To Score":
        scored = home > 0 and away > 0
        wins = selection.strip().casefold() in ({"yes", "y"} if scored else {"no", "n"})
        return Settlement(Outcome.WIN if wins else Outcome.LOSS, price if wins else Decimal(0))
    if base_market in {"Asian Handicap", "Asian Handicap - First Half"}:
        if line is None:
            raise MissingSettlementData("line is required for Asian handicap settlement.")
        return _handicap(home, away, selection, Decimal(str(line)), price)
    if base_market == "European Handicap":
        if line is None:
            raise MissingSettlementData("line is required for European handicap settlement.")
        handicap = Decimal(str(line))
        if handicap != handicap.to_integral_value():
            raise ValueError("European handicap lines must be whole goals.")
        result = _result(home + int(handicap), away)
        outcome = Outcome.WIN if _side(selection) == result else Outcome.LOSS
        return Settlement(outcome, price if outcome == Outcome.WIN else Decimal(0))
    if base_market == "Correct Score":
        win = _parse_score(selection) == (home, away)
        return Settlement(Outcome.WIN if win else Outcome.LOSS, price if win else Decimal(0))
    if base_market == "Half Time/Full Time":
        half, full = _pair_selection(selection)
        win = half == _result(
            _required(halftime_home_goals, "halftime_home_goals"),
            _required(halftime_away_goals, "halftime_away_goals"),
        ) and full == _result(home, away)
        return Settlement(Outcome.WIN if win else Outcome.LOSS, price if win else Decimal(0))
    if base_market in {"Team Total Goals", "Home Team Total Goals", "Away Team Total Goals"}:
        if line is None:
            raise MissingSettlementData("line is required for team totals.")
        pieces = selection.casefold().split()
        side = (
            "home"
            if base_market == "Home Team Total Goals"
            else "away"
            if base_market == "Away Team Total Goals"
            else pieces[0]
        )
        if side not in {"home", "away"}:
            raise ValueError("Team total selection must identify Home or Away.")
        return _over_under(
            home if side == "home" else away, " ".join(pieces[-1:]), Decimal(str(line)), price
        )
    if base_market == "Goals Odd/Even":
        wanted = selection.strip().casefold()
        if wanted not in {"odd", "even"}:
            raise ValueError(f"Unrecognized odd/even selection: {selection!r}.")
        win = (home + away) % 2 == (1 if wanted == "odd" else 0)
        return Settlement(Outcome.WIN if win else Outcome.LOSS, price if win else Decimal(0))
    raise UnsupportedMarket(f"No settlement rule is registered for {market_name!r}.")
