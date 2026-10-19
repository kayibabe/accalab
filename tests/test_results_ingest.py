from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from accalab.config import Settings
from accalab.db import Base, make_engine, make_session_factory
from accalab.ingest.results import CHECKPOINT_TYPE, run_results_ingest
from accalab.models import DataSnapshot, Fixture


class FakeClient:
    def __init__(self) -> None:
        self.requests_this_session = 0
        self.cache_hits = 0
        self.requests_per_minute = 300
        self.daily_quota_limit = 7500
        self.quota = SimpleNamespace(provider_remaining=None, configured_limit=7500, requests_used=0)
        self.fixture_params: list[dict] = []

    def get(self, path, params=None, *, use_cache=True):
        self.requests_this_session += 1
        assert path == "/status"
        return {
            "response": {
                "requests": {"current": 100, "limit_day": 7500},
                "subscription": {"plan": "pro"},
            }
        }

    def sync_quota(self, *, requests_used, daily_limit):
        self.quota = SimpleNamespace(
            provider_remaining=daily_limit - requests_used,
            configured_limit=daily_limit,
            requests_used=requests_used,
        )

    def get_all(self, path, params=None):
        self.requests_this_session += 1
        if path == "/leagues":
            return [
                {
                    "league": {"id": 39, "name": "Premier League"},
                    "seasons": [{"year": 2026, "current": True}],
                }
            ]
        assert path == "/fixtures"
        self.fixture_params.append(params)
        fixture_id = 9001 if params["season"] == 2026 else 9003
        return [
            fixture_row(fixture_id, params["season"], "FT", 2, 1),
            fixture_row(fixture_id + 1, params["season"], "NS", None, None),
        ]


def fixture_row(fixture_id: int, season: int, status: str, home_goals, away_goals) -> dict:
    return {
        "fixture": {
            "id": fixture_id,
            "date": f"{season}-05-01T12:00:00+00:00",
            "timezone": "UTC",
            "status": {"short": status},
        },
        "league": {"id": 39, "name": "Premier League", "country": "England", "season": season},
        "teams": {
            "home": {"id": 10, "name": "Home", "country": "England"},
            "away": {"id": 20, "name": "Away", "country": "England"},
        },
        "goals": {"home": home_goals, "away": away_goals},
        "score": {"halftime": {"home": None, "away": None}},
    }


def settings(*, league_ids: tuple[int, ...] = ()) -> Settings:
    return Settings(
        api_key="test",
        league_ids=league_ids,
        base_url="https://example.test",
        timeout_seconds=1,
        retries=0,
        backoff_seconds=0,
        requests_per_minute=300,
        daily_quota_limit=7500,
        cache_path=Path("cache.sqlite3"),
        cache_ttl_seconds=900,
        timezone="UTC",
        future_days=5,
        historical_days=(1, 7, 30, 90),
        report_path=Path("report.md"),
    )


def test_results_ingest_filters_finished_matches_and_resumes_from_checkpoints(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'results.sqlite3'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    client = FakeClient()
    try:
        with sessions() as session:
            summary = run_results_ingest(
                client, session, settings(), seasons_per_league=2, today=date(2026, 10, 19)
            )
            fixtures = session.scalars(select(Fixture).order_by(Fixture.id)).all()
            checkpoints = session.scalars(
                select(DataSnapshot).where(DataSnapshot.data_type == CHECKPOINT_TYPE)
            ).all()
            assert [(row.id, row.season, row.status, row.home_goals, row.away_goals) for row in fixtures] == [
                (9001, 2026, "FT", 2, 1),
                (9003, 2025, "FT", 2, 1),
            ]
            assert len(checkpoints) == 2
            assert [params["season"] for params in client.fixture_params] == [2026, 2025]
            assert all(params["status"] == "FT-AET-PEN" for params in client.fixture_params)
            assert summary.fixtures_stored == 2
            assert summary.completed_pairs == 2

            second = run_results_ingest(
                client, session, settings(), seasons_per_league=2, today=date(2026, 10, 19)
            )
            assert second.skipped_pairs == 2
            assert second.completed_pairs == 0
            assert len(client.fixture_params) == 2
    finally:
        engine.dispose()


def test_results_ingest_respects_configured_league_filter(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'filtered.sqlite3'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    client = FakeClient()
    try:
        import pytest

        with sessions() as session, pytest.raises(ValueError, match="No current leagues"):
            run_results_ingest(
                client,
                session,
                settings(league_ids=(140,)),
                today=date(2026, 10, 19),
            )
        assert client.fixture_params == []
    finally:
        engine.dispose()
