from datetime import date
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from accalab.config import Settings
from accalab.db import Base, make_engine, make_session_factory
from accalab.ingest.daily import _selection_and_line, run_daily_ingest
from accalab.models import DataSnapshot, Fixture, MarketRegistry, OddsSnapshot


class FakeClient:
    requests_this_session = 0
    cache_hits = 2
    requests_per_minute = 300
    quota = SimpleNamespace(provider_remaining=None, configured_limit=7500, requests_used=0)

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
                    "seasons": [
                        {
                            "year": 2026,
                            "current": True,
                            "coverage": {
                                "injuries": True,
                                "standings": True,
                                "fixtures": {"lineups": True, "statistics": True},
                            },
                        }
                    ],
                }
            ]
        if path == "/odds/bets":
            return [
                {"id": 1, "name": "Match Winner"},
                {"id": 999, "name": "Novel Prop"},
            ]
        if path == "/fixtures":
            if "ids" in params:
                return [
                    {
                        "fixture": {"id": 9001},
                        "lineups": [{"team": "Home"}],
                        "statistics": [{"type": "Shots", "value": 5}],
                    }
                ]
            if params["date"] != "2026-10-19":
                return []
            return [
                {
                    "fixture": {
                        "id": 9001,
                        "date": "2026-10-19T12:00:00+00:00",
                        "timezone": "UTC",
                        "status": {"short": "NS"},
                    },
                    "league": {
                        "id": 39,
                        "name": "Premier League",
                        "country": "England",
                        "season": 2026,
                    },
                    "teams": {
                        "home": {"id": 10, "name": "Home", "country": "England"},
                        "away": {"id": 20, "name": "Away", "country": "England"},
                    },
                    "goals": {"home": None, "away": None},
                    "score": {"halftime": {"home": None, "away": None}},
                }
            ]
        if path == "/injuries":
            return []
        if path == "/standings":
            return [{"league": {"id": 39, "standings": [[{"rank": 1}]]}}]
        if path == "/teams/statistics":
            return [{"team": {"id": params["team"]}, "fixtures": {"played": {"total": 8}}}]
        if path == "/odds":
            if params["date"] != "2026-10-19":
                return []
            return [
                {
                    "fixture": {"id": 9001},
                    "bookmakers": [
                        {
                            "id": 8,
                            "name": "Book",
                            "bets": [
                                {
                                    "id": 1,
                                    "name": "Match Winner",
                                    "update": "2026-10-19T10:00:00+00:00",
                                    "values": [
                                        {"value": "Home", "odd": "1.9"},
                                        {"value": "Draw", "odd": "3.2"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        raise AssertionError(path)


def settings(tmp_path: Path, league_ids: tuple[int, ...] = ()) -> Settings:
    return Settings(
        api_key="test",
        league_ids=league_ids,
        base_url="https://example.test",
        timeout_seconds=1,
        retries=0,
        backoff_seconds=0,
        requests_per_minute=300,
        daily_quota_limit=7500,
        cache_path=tmp_path / "cache.db",
        cache_ttl_seconds=60,
        timezone="UTC",
        future_days=5,
        historical_days=(1, 7, 30, 90),
        report_path=tmp_path / "report.md",
    )


def test_line_markets_keep_selection_separate_from_quarter_handicap() -> None:
    selection, line = _selection_and_line("Asian Handicap", "Home -0.25")
    assert (selection, line) == ("Home", -0.25)
    selection, line = _selection_and_line("Goals Over/Under", "Over 2.5")
    assert (selection, line) == ("Over", 2.5)
    selection, line = _selection_and_line("Correct Score", "2-1")
    assert (selection, line) == ("2-1", None)


def test_daily_ingestion_stores_fixtures_odds_and_unknown_markets_safely(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'db.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        result = run_daily_ingest(
            FakeClient(), session, settings(tmp_path), today=date(2026, 10, 19)
        )
        assert result.league_count == 1
        assert result.fixture_count == 1
        assert result.odds_count == 2
        assert result.market_count == 2
        assert result.requests_used == 25
        assert result.context_snapshots == 11
        assert session.get(Fixture, 9001).home_team_id == 10
        assert session.get(MarketRegistry, 1).ticket_eligible is True
        assert session.get(MarketRegistry, 999).supported is False
        snapshots = session.scalars(select(OddsSnapshot).order_by(OddsSnapshot.selection)).all()
        assert [row.selection for row in snapshots] == ["Draw", "Home"]
        assert all(row.as_of.tzinfo is not None for row in snapshots)
        context_types = set(session.scalars(select(DataSnapshot.data_type)).all())
        assert context_types == {
            "injuries",
            "lineups",
            "fixture_statistics",
            "standings",
            "team_statistics",
        }
    engine.dispose()
