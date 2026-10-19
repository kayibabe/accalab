from datetime import date
from pathlib import Path

import httpx
import pytest

from accalab.config import Settings
from accalab.ingest.api_football import ApiFootballClient, QuotaExceeded
from accalab.probe import render_report, run_probe


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="test-key",
        league_ids=(39, 140, 78, 135, 61),
        base_url="https://example.test",
        timeout_seconds=1,
        retries=1,
        backoff_seconds=0,
        requests_per_minute=6000,
        daily_quota_limit=100,
        cache_path=tmp_path / "cache.sqlite3",
        cache_ttl_seconds=300,
        timezone="UTC",
        future_days=5,
        historical_days=(1, 7, 30, 90),
        report_path=tmp_path / "PHASE0_REPORT.md",
    )


def test_client_caches_responses_and_counts_only_network_requests(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"x-ratelimit-requests-remaining": "98", "x-ratelimit-requests-limit": "100"},
            json={"response": [{"ok": True}]},
        )

    client = ApiFootballClient(
        api_key="test", base_url="https://example.test", cache_path=tmp_path / "cache.sqlite3",
        requests_per_minute=6000, daily_quota_limit=100,
        transport=httpx.MockTransport(handler), sleep=lambda _: None,
    )
    assert client.get("/fixtures", {"league": 39}) == client.get("fixtures", {"league": 39})
    assert calls == 1
    assert client.quota.requests_used == 1
    assert client.quota.provider_remaining == 98
    client.close()


def test_client_retries_transient_responses_and_stops_at_quota(tmp_path: Path) -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"message": "temporary"})
        return httpx.Response(200, json={"response": []})

    client = ApiFootballClient(
        api_key="test", base_url="https://example.test", cache_path=tmp_path / "cache.sqlite3",
        retries=1, backoff_seconds=0.25, requests_per_minute=6000, daily_quota_limit=2,
        transport=httpx.MockTransport(handler), sleep=delays.append,
    )
    assert client.get("/fixtures") == {"response": []}
    assert calls == 2
    assert 0.25 in delays
    with pytest.raises(QuotaExceeded):
        client.get("/odds")
    assert client.quota.requests_used == 2
    client.close()


def test_client_fetches_later_pages_only_when_reported(tmp_path: Path) -> None:
    pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        pages.append(page)
        if page is None:
            return httpx.Response(200, json={
                "response": [{"id": 1}],
                "paging": {"current": 1, "total": 2},
            })
        return httpx.Response(200, json={
            "response": [{"id": 2}],
            "paging": {"current": 2, "total": 2},
        })

    client = ApiFootballClient(
        api_key="test", base_url="https://example.test", cache_path=tmp_path / "cache.sqlite3",
        requests_per_minute=6000, daily_quota_limit=100,
        transport=httpx.MockTransport(handler), sleep=lambda _: None,
    )
    assert client.get_all("/leagues") == [{"id": 1}, {"id": 2}]
    assert pages == [None, "2"]
    client.close()


def test_probe_samples_historical_fixtures_and_summarizes_markets(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        if path.endswith("/status"):
            return httpx.Response(200, json={"response": {
                "requests": {"current": 0, "limit_day": 100},
                "subscription": {"plan": "Free"},
            }})
        if path.endswith("/leagues"):
            return httpx.Response(200, json={"response": [{
                "league": {"id": 39, "name": "League A"},
                "seasons": [{"current": True, "coverage": {"odds": True}}],
            }]})
        if path.endswith("/fixtures"):
            fixture_id = 123 if query.get("date", "") >= "2026-10-19" else 456
            return httpx.Response(200, json={"response": [
                {
                    "fixture": {"id": fixture_id, "status": {"short": "NS" if fixture_id == 123 else "FT"}},
                    "league": {"id": 39, "name": "League A"},
                }
            ]})
        fixture_id = 123 if query.get("date", "") >= "2026-10-19" else 456
        return httpx.Response(200, json={"response": [{
            "fixture": {"id": fixture_id},
            "league": {"id": 39, "name": "League A"},
            "bookmakers": [{"name": "Book A", "bets": [{"name": "Match Winner"}]}],
        }]})

    settings = _settings(tmp_path)
    settings = Settings(**{**settings.__dict__, "league_ids": ()})
    client = ApiFootballClient(
        api_key="test", base_url="https://example.test", cache_path=settings.cache_path,
        requests_per_minute=6000, daily_quota_limit=100,
        transport=httpx.MockTransport(handler), sleep=lambda _: None,
    )
    result = run_probe(client, settings, today=date(2026, 10, 19))
    league = result["leagues"]["39"]
    assert league["markets"] == ["Match Winner"]
    assert league["bookmakers"] == ["Book A"]
    assert league["historical"]["90"]["fixtures_with_odds"] == 1
    assert result["league_scope"] == "all current leagues"
    assert result["configured_limit"] == 100
    assert result["requests_used_this_run"] == 22
    client.close()


def test_report_discloses_missing_live_probe_and_terms() -> None:
    report = render_report({})
    assert "Live probe not run" in report
    assert "does not itself grant a license to publish" in report
    assert "API-Football Terms of Service" in report
