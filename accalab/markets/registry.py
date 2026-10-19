"""Canonicalize provider market labels and assign conservative support defaults."""

from __future__ import annotations

import re
import unicodedata


_CANONICAL: dict[str, tuple[str, bool, bool]] = {
    "1x2": ("1X2", True, True),
    "match winner": ("1X2", True, True),
    "match result": ("1X2", True, True),
    "fulltime result": ("1X2", True, True),
    "double chance": ("Double Chance", True, True),
    "draw no bet": ("Draw No Bet", True, True),
    "goals over under": ("Over/Under Goals", True, True),
    "over under": ("Over/Under Goals", True, True),
    "over under goals": ("Over/Under Goals", True, True),
    "goals over under first half": ("Over/Under Goals - First Half", True, True),
    "over under first half": ("Over/Under Goals - First Half", True, True),
    "over under goals first half": ("Over/Under Goals - First Half", True, True),
    "both teams score": ("Both Teams To Score", True, True),
    "btts": ("Both Teams To Score", True, True),
    "asian handicap": ("Asian Handicap", True, True),
    "asian handicap first half": ("Asian Handicap - First Half", True, True),
    "european handicap": ("European Handicap", True, False),
    "correct score": ("Correct Score", True, False),
    "exact score": ("Correct Score", True, False),
    "half time full time": ("Half Time/Full Time", True, False),
    "ht ft": ("Half Time/Full Time", True, False),
    "team total goals": ("Team Total Goals", True, False),
    "home team total goals": ("Home Team Total Goals", True, False),
    "away team total goals": ("Away Team Total Goals", True, False),
    "odd even": ("Goals Odd/Even", True, False),
    "goals odd even": ("Goals Odd/Even", True, False),
    "match corners": ("Match Corners", True, False),
    "total corners": ("Match Corners", True, False),
    "match cards": ("Match Cards", True, False),
    "total cards": ("Match Cards", True, False),
}


def _key(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def normalize_market_name(name: str) -> str:
    """Return a stable canonical label; unknown labels retain a normalized display form."""
    key = _key(name)
    if key in _CANONICAL:
        return _CANONICAL[key][0]
    return " ".join(word.capitalize() for word in key.split()) or "Unknown Market"


def market_defaults(name: str) -> tuple[bool, bool]:
    """Return `(supported, ticket_eligible)`; unknown/exotic markets are stored, not enabled."""
    rule = _CANONICAL.get(_key(name))
    return rule[1:] if rule else (False, False)
