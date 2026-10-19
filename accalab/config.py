"""Configuration loading for the API probe."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    league_ids: tuple[int, ...]
    base_url: str
    timeout_seconds: float
    retries: int
    backoff_seconds: float
    requests_per_minute: int
    daily_quota_limit: int
    cache_path: Path
    cache_ttl_seconds: int
    timezone: str
    future_days: int
    historical_days: tuple[int, ...]
    report_path: Path


def load_settings(root: Path = ROOT) -> Settings:
    _load_dotenv(root / ".env")
    raw: dict[str, Any] = tomllib.loads((root / "config" / "settings.toml").read_text("utf-8"))
    api = raw["api"]
    probe = raw["probe"]
    ids_text = os.getenv("API_FOOTBALL_LEAGUE_IDS", "")
    ids = ids_text if ids_text.strip() else ",".join(str(item) for item in raw["leagues"]["ids"])
    league_ids = tuple(dict.fromkeys(int(item.strip()) for item in ids.split(",") if item.strip()))
    return Settings(
        api_key=os.getenv("API_FOOTBALL_KEY"),
        league_ids=league_ids,
        base_url=api["base_url"],
        timeout_seconds=float(api["timeout_seconds"]),
        retries=int(api["retries"]),
        backoff_seconds=float(api["backoff_seconds"]),
        requests_per_minute=int(api["requests_per_minute"]),
        daily_quota_limit=int(api["daily_quota_limit"]),
        cache_path=root / api["cache_path"],
        cache_ttl_seconds=int(api["cache_ttl_seconds"]),
        timezone=api["timezone"],
        future_days=int(probe["future_days"]),
        historical_days=tuple(int(day) for day in probe["historical_days"]),
        report_path=root / probe["report_path"],
    )


def validate_probe_settings(settings: Settings) -> None:
    if not settings.api_key:
        raise ValueError("API_FOOTBALL_KEY is missing; set it in the environment or .env.")
    if any(league_id <= 0 for league_id in settings.league_ids):
        raise ValueError("League IDs must be positive integers.")
