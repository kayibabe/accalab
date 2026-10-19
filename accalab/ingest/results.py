"""Quota-aware ingestion of completed API-Football fixtures for model evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from accalab.config import Settings
from accalab.ingest.api_football import ApiFootballClient
from accalab.ingest.daily import _as_int, _configure_quota, _store_fixture
from accalab.models import DataSnapshot

FINISHED_STATUSES = {"FT", "AET", "PEN"}
CHECKPOINT_TYPE = "historical_results_ingest"


@dataclass(frozen=True)
class ResultsIngestSummary:
    league_count: int
    season_pairs: int
    completed_pairs: int
    skipped_pairs: int
    fixtures_stored: int
    requests_used: int
    cache_hits: int
    quota_remaining: int | None


def run_results_ingest(
    client: ApiFootballClient,
    session: Session,
    settings: Settings,
    *,
    seasons_per_league: int = 2,
    today: date | None = None,
) -> ResultsIngestSummary:
    """Load finished fixture results in recent seasons for all current leagues.

    Each league-season is committed with a checkpoint, so a later run can resume
    without spending quota again on completed windows. The provider quota reserve
    leaves room for other same-day API operations.
    """
    if seasons_per_league < 1:
        raise ValueError("seasons_per_league must be at least one.")
    run_date = today or datetime.now(UTC).date()
    _configure_quota(client)
    reserve = min(250, max(25, client.quota.configured_limit // 20))
    client.daily_quota_limit = max(0, client.quota.configured_limit - reserve)

    league_rows = client.get_all("/leagues", {"current": "true"})
    leagues: dict[int, tuple[str, int]] = {}
    for row in league_rows:
        league = row.get("league") or {}
        league_id = _as_int(league.get("id"))
        current = next(
            (
                season
                for season in row.get("seasons", [])
                if season.get("current") is True and _as_int(season.get("year")) is not None
            ),
            None,
        )
        if league_id is None or current is None:
            continue
        if settings.league_ids and league_id not in settings.league_ids:
            continue
        year = min(int(current["year"]), run_date.year)
        leagues[league_id] = (str(league.get("name") or f"League {league_id}"), year)
    if not leagues:
        raise ValueError("No current leagues matched the configured league scope.")

    pairs = [
        (league_id, league_name, year)
        for league_id, (league_name, current_year) in sorted(leagues.items())
        for year in range(current_year, current_year - seasons_per_league, -1)
    ]
    existing = set(
        session.scalars(
            select(DataSnapshot.scope_key).where(
                DataSnapshot.data_type == CHECKPOINT_TYPE,
                DataSnapshot.scope_type == "league_season",
            )
        ).all()
    )
    completed_pairs = skipped_pairs = fixtures_stored = 0
    as_of = datetime.now(UTC)
    for league_id, league_name, year in pairs:
        scope_key = f"{league_id}:{year}"
        if scope_key in existing:
            skipped_pairs += 1
            continue
        params: dict[str, Any] = {
            "league": league_id,
            "season": year,
            "status": "FT-AET-PEN",
        }
        rows = client.get_all("/fixtures", params)
        stored = 0
        for item in rows:
            fixture = item.get("fixture") or {}
            status = str((fixture.get("status") or {}).get("short") or "")
            if status not in FINISHED_STATUSES:
                continue
            goals = item.get("goals") or {}
            if goals.get("home") is None or goals.get("away") is None:
                continue
            if _store_fixture(session, item):
                stored += 1
        session.add(
            DataSnapshot(
                data_type=CHECKPOINT_TYPE,
                scope_type="league_season",
                scope_key=scope_key,
                as_of=as_of,
                source="api-football-v3",
                payload={
                    "league_id": league_id,
                    "league_name": league_name,
                    "season": year,
                    "finished_fixtures": stored,
                    "ingested_at": as_of.isoformat(),
                },
            )
        )
        session.commit()
        existing.add(scope_key)
        completed_pairs += 1
        fixtures_stored += stored

    return ResultsIngestSummary(
        league_count=len(leagues),
        season_pairs=len(pairs),
        completed_pairs=completed_pairs,
        skipped_pairs=skipped_pairs,
        fixtures_stored=fixtures_stored,
        requests_used=client.requests_this_session,
        cache_hits=client.cache_hits,
        quota_remaining=client.quota.provider_remaining,
    )
