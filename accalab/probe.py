"""API-Football odds coverage probe."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from accalab.config import Settings
from accalab.ingest.api_football import ApiFootballClient, ApiFootballError, QuotaExceeded


FINISHED_STATUSES = {"FT", "AET", "PEN"}


def _response_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = payload.get("response", [])
    return response if isinstance(response, list) else []


def _fixture_id(fixture: dict[str, Any]) -> int | None:
    try:
        return int(fixture["fixture"]["id"])
    except (KeyError, TypeError, ValueError):
        return None


def _odds_summary(payload: dict[str, Any]) -> tuple[set[str], set[str]]:
    markets: set[str] = set()
    bookmakers: set[str] = set()
    for item in _response_items(payload):
        for bookmaker in item.get("bookmakers", []):
            if bookmaker.get("name"):
                bookmakers.add(str(bookmaker["name"]))
            for bet in bookmaker.get("bets", []):
                if bet.get("name"):
                    markets.add(str(bet["name"]))
    return markets, bookmakers


def _league_details(item: dict[str, Any]) -> tuple[int | None, str]:
    league = item.get("league", {})
    try:
        league_id = int(league["id"])
    except (KeyError, TypeError, ValueError):
        league_id = None
    return league_id, str(league.get("name") or f"League {league_id or 'unknown'}")


def _current_leagues(rows: list[dict[str, Any]]) -> tuple[dict[int, str], set[int]]:
    leagues: dict[int, str] = {}
    odds_covered: set[int] = set()
    for row in rows:
        league_id, name = _league_details(row)
        seasons = row.get("seasons", [])
        covered = any(
            season.get("current") is True and season.get("coverage", {}).get("odds") is True
            for season in seasons
        )
        if league_id is not None:
            leagues[league_id] = name
            if covered:
                odds_covered.add(league_id)
    return leagues, odds_covered


def run_probe(client: ApiFootballClient, settings: Settings, *, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    future_end = today + timedelta(days=settings.future_days)
    result: dict[str, Any] = {
        "run_date": today.isoformat(),
        "future_window": [today.isoformat(), future_end.isoformat()],
        "leagues": {},
        "league_scope": "all current leagues" if not settings.league_ids else "configured league IDs",
        "errors": [],
    }
    try:
        status = client.get("/status", use_cache=False).get("response", {})
        if not isinstance(status, dict):
            raise ApiFootballError("API-Football /status returned an unexpected response shape.")
        requests_status = status.get("requests", {})
        try:
            api_used = int(requests_status["current"])
            api_limit = int(requests_status["limit_day"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiFootballError("Could not read daily usage from API-Football /status.") from exc
        client.sync_quota(requests_used=api_used, daily_limit=api_limit)
        plan = str(status.get("subscription", {}).get("plan", "")).casefold()
        plan_rpm = {"free": 10, "pro": 300, "ultra": 450, "mega": 900, "custom": 1200}
        client.requests_per_minute = plan_rpm.get(plan, settings.requests_per_minute)
        result["account_plan"] = plan or "unknown"
        result["requests_used_start"] = api_used
        result["configured_limit"] = api_limit

        all_league_rows = client.get_all("/leagues", {"current": "true"})
        all_leagues, odds_covered = _current_leagues(all_league_rows)
        if settings.league_ids:
            all_leagues = {
                league_id: all_leagues.get(league_id, f"League {league_id}")
                for league_id in settings.league_ids
            }
            odds_covered.intersection_update(all_leagues)
        result["discovered_current_leagues"] = len(all_league_rows)
        result["odds_covered_leagues"] = len(odds_covered)
        result["leagues"] = {
            str(league_id): {
                "name": name,
                "odds_coverage": league_id in odds_covered,
                "upcoming_fixtures": 0,
                "future_fixtures_with_odds": 0,
                "markets": set(),
                "bookmakers": set(),
                "historical": {},
            }
            for league_id, name in all_leagues.items()
        }

        fixtures: list[dict[str, Any]] = []
        for day_offset in range(settings.future_days + 1):
            query_date = today + timedelta(days=day_offset)
            fixtures.extend(
                client.get_all("/fixtures", {"date": query_date.isoformat(), "timezone": settings.timezone})
            )
        for fixture in fixtures:
            league_id, _ = _league_details(fixture)
            if league_id in all_leagues:
                result["leagues"][str(league_id)]["upcoming_fixtures"] += 1

        upcoming_odds: list[dict[str, Any]] = []
        for day_offset in range(settings.future_days + 1):
            query_date = today + timedelta(days=day_offset)
            upcoming_odds.extend(
                client.get_all("/odds", {"date": query_date.isoformat()})
            )
        upcoming_odds_by_fixture = {_fixture_id(item): item for item in upcoming_odds}
        for fixture in fixtures:
            league_id, _ = _league_details(fixture)
            fixture_id = _fixture_id(fixture)
            if league_id not in all_leagues or fixture_id is None:
                continue
            odds_item = upcoming_odds_by_fixture.get(fixture_id)
            if odds_item:
                league_result = result["leagues"][str(league_id)]
                league_result["future_fixtures_with_odds"] += 1
                markets, bookmakers = _odds_summary({"response": [odds_item]})
                league_result["markets"].update(markets)
                league_result["bookmakers"].update(bookmakers)

        for age_days in settings.historical_days:
            sample_date = today - timedelta(days=age_days)
            finished = [
                fixture
                for fixture in client.get_all(
                    "/fixtures", {"date": sample_date.isoformat(), "timezone": settings.timezone}
                )
                if fixture.get("fixture", {}).get("status", {}).get("short") in FINISHED_STATUSES
                and _fixture_id(fixture) is not None
                and _league_details(fixture)[0] in all_leagues
            ]
            historical_odds = client.get_all("/odds", {"date": sample_date.isoformat()})
            odds_by_fixture = {_fixture_id(item): item for item in historical_odds}
            for fixture in finished:
                league_id, _ = _league_details(fixture)
                league_result = result["leagues"][str(league_id)]
                age_result = league_result["historical"].setdefault(
                    str(age_days),
                    {
                        "finished_date": sample_date.isoformat(),
                        "finished_fixtures_found": 0,
                        "fixtures_with_odds": 0,
                        "sample_fixture_id": None,
                        "markets": set(),
                        "bookmakers": set(),
                    },
                )
                age_result["finished_fixtures_found"] += 1
                age_result["sample_fixture_id"] = age_result["sample_fixture_id"] or _fixture_id(fixture)
                odds_item = odds_by_fixture.get(_fixture_id(fixture))
                if odds_item:
                    age_result["fixtures_with_odds"] += 1
                    markets, bookmakers = _odds_summary({"response": [odds_item]})
                    age_result["markets"].update(markets)
                    age_result["bookmakers"].update(bookmakers)
            result.setdefault("historical_totals", {})[str(age_days)] = {
                "finished_fixtures": len(finished),
                "fixtures_with_odds": sum(
                    row.get("fixtures_with_odds", 0)
                    for league in result["leagues"].values()
                    for key, row in league["historical"].items()
                    if key == str(age_days)
                ),
            }
    except (ApiFootballError, QuotaExceeded) as exc:
        result["errors"].append(str(exc))
    result["requests_used_end"] = client.quota.requests_used
    result["requests_used_this_run"] = client.requests_this_session
    result["cache_hits"] = client.cache_hits
    result["provider_remaining"] = client.quota.provider_remaining
    result["provider_limit"] = client.quota.provider_limit
    for league_result in result["leagues"].values():
        league_result["markets"] = sorted(league_result["markets"])
        league_result["bookmakers"] = sorted(league_result["bookmakers"])
        for age_result in league_result["historical"].values():
            age_result["markets"] = sorted(age_result["markets"])
            age_result["bookmakers"] = sorted(age_result["bookmakers"])
    return result


def render_report(probe: dict[str, Any], *, note: str = "") -> str:
    lines = [
        "# Phase 0a: API-Football odds probe",
        "",
        f"Probe date: {probe.get('run_date', 'not run')}",
        "",
        "## Upcoming fixtures and odds",
        "",
        f"Window: {probe.get('future_window', ['not run', 'not run'])[0]} through "
        f"{probe.get('future_window', ['not run', 'not run'])[-1]} (inclusive).",
        "",
    ]
    if not probe.get("leagues"):
        lines.append("Live probe not run or returned no league results. Check the run status and any probe errors below.")
    else:
        league_results = list(probe["leagues"].values())
        all_markets = sorted({market for row in league_results for market in row["markets"]})
        all_bookmakers = sorted({book for row in league_results for book in row["bookmakers"]})
        lines.extend(
            [
                f"League scope: {probe.get('league_scope', 'not stated')}",
                f"Current leagues discovered: {probe.get('discovered_current_leagues', 'not reported')}",
                f"Current leagues with odds coverage: {probe.get('odds_covered_leagues', len(league_results))}",
                f"Fixtures in window: {sum(row['upcoming_fixtures'] for row in league_results)}",
                f"Fixtures with odds returned: {sum(row['future_fixtures_with_odds'] for row in league_results)}",
                f"Markets returned: {', '.join(all_markets) or 'none'}",
                f"Bookmakers returned: {', '.join(all_bookmakers) or 'none'}",
                "",
                "### By league",
                "",
                "| League | Odds coverage | Fixtures | Fixtures with odds | Market types | Bookmakers |",
                "|---|:---:|---:|---:|---:|---:|",
            ]
        )
        for league_id, data in probe["leagues"].items():
            if data["upcoming_fixtures"] or data["future_fixtures_with_odds"]:
                lines.append(
                    f"| {data['name']} ({league_id}) | "
                    f"{'yes' if data['odds_coverage'] else 'no'} | "
                    f"{data['upcoming_fixtures']} | {data['future_fixtures_with_odds']} | "
                    f"{len(data['markets'])} | {len(data['bookmakers'])} |"
                )
    lines.extend(
        [
            "",
            "## Historical odds availability",
            "",
            "All finished fixtures on each target date are compared with odds returned for that date across the selected league scope.",
            "",
        ]
    )
    if not probe.get("leagues"):
        lines.append("No historical requests were completed.")
    lines.extend(
        [
            "",
            "API-Football's published guide describes pre-match odds as typically available 1 to 14 days before kickoff and says it retains seven days of odds history. This is documentation context only; date-specific availability below requires a live probe.",
            "",
        ]
    )
    for age_days, totals in probe.get("historical_totals", {}).items():
        lines.append(
            f"- {age_days} days ago: odds returned for {totals['fixtures_with_odds']} of "
            f"{totals['finished_fixtures']} finished fixtures."
        )
    lines.extend(
        [
            "",
            "## Request usage",
            "",
            f"Requests made during this run: {probe.get('requests_used_this_run', 0)}",
            f"Cached responses reused during this run: {probe.get('cache_hits', 0)} (cached reads do not use API quota)",
            f"API-Football requests used today at end: {probe.get('requests_used_end', 0)}",
            f"API plan: {probe.get('account_plan', 'not checked')}",
            f"Daily limit: {probe.get('configured_limit', 'not checked')}",
            f"Provider remaining / limit headers: {probe.get('provider_remaining')} / {probe.get('provider_limit')}",
            "",
            "## Paid-product terms",
            "",
            "The account status confirms API access on the Pro plan. Separately, API-Football's published general terms say that resale without permission is not allowed and that the subscription does not itself grant a license to publish data in an application, website, or product; the terms assign responsibility for any needed authorization to the user and note possible third-party commercial restrictions. Any separate license or addendum for paid publication is not represented in the API account status and should be retained with the project records.",
            "",
            "Sources: [API-Football Terms of Service](https://www.api-football.com/terms) and [API-Football odds documentation guide](https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide).",
            "",
        ]
    )
    if probe.get("errors"):
        lines.extend(["## Probe errors", "", *[f"- {error}" for error in probe["errors"]], ""])
    if note:
        lines.extend(["## Run status", "", note, ""])
    return "\n".join(lines)


def write_report(settings: Settings, content: str) -> None:
    settings.report_path.parent.mkdir(parents=True, exist_ok=True)
    settings.report_path.write_text(content, encoding="utf-8")
