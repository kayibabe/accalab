"""Quota-aware daily ingestion of fixtures, bookmaker odds, and market catalogue."""

from __future__ import annotations

import re
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from accalab.config import Settings
from accalab.ingest.api_football import ApiFootballClient, ApiFootballError
from accalab.markets.registry import market_defaults, normalize_market_name
from accalab.models import DataSnapshot, Fixture, MarketRegistry, OddsSnapshot, Team, utc_now


@dataclass(frozen=True)
class IngestSummary:
    run_date: date
    league_count: int
    fixture_count: int
    odds_count: int
    market_count: int
    requests_used: int
    cache_hits: int
    context_snapshots: int = 0
    enrichment_skipped: int = 0


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return utc_now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _line(value: str) -> Decimal | None:
    match = re.search(r"(?:^|\s)([+-]?\d+(?:\.\d+)?)\s*$", value.strip())
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def _selection_and_line(market_name: str, value: str) -> tuple[str, Decimal | None]:
    canonical = normalize_market_name(market_name)
    line_markets = {
        "Over/Under Goals",
        "Over/Under Goals - First Half",
        "Asian Handicap",
        "Asian Handicap - First Half",
        "European Handicap",
        "Team Total Goals",
        "Home Team Total Goals",
        "Away Team Total Goals",
        "Match Corners",
        "Match Cards",
    }
    parsed_line = _line(value) if canonical in line_markets else None
    if parsed_line is None:
        return value.strip(), None
    selection = re.sub(r"[+-]?\d+(?:\.\d+)?\s*$", "", value).strip().rstrip("+-").strip()
    return selection, parsed_line


def sync_market_registry(session: Session, rows: list[dict[str, Any]]) -> int:
    """Update provider market labels while preserving accumulated registry flags."""
    count = 0
    for row in rows:
        market_id = _as_int(row.get("id"))
        name = str(row.get("name") or "").strip()
        if market_id is None or not name:
            continue
        market = session.get(MarketRegistry, market_id)
        supported, eligible = market_defaults(name)
        if market is None:
            market = MarketRegistry(
                id=market_id,
                canonical_name=normalize_market_name(name),
                display_name=name,
                supported=supported,
                ticket_eligible=eligible,
                raw=row,
            )
            session.add(market)
        else:
            market.canonical_name = normalize_market_name(name)
            market.display_name = name
            # A discovered label can add safe defaults; it never clears an Admin's explicit enablement.
            market.supported = market.supported or supported
            market.ticket_eligible = market.ticket_eligible or eligible
            market.raw = row
            market.updated_at = utc_now()
        count += 1
    session.flush()
    return count


def _store_fixture(session: Session, item: dict[str, Any]) -> bool:
    fixture_data = item.get("fixture") or {}
    league = item.get("league") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}
    score = item.get("score") or {}
    halftime = score.get("halftime") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    fixture_id = _as_int(fixture_data.get("id"))
    home_id = _as_int(home.get("id"))
    away_id = _as_int(away.get("id"))
    league_id = _as_int(league.get("id"))
    if None in (fixture_id, home_id, away_id, league_id):
        return False
    for data in (home, away):
        team_id = int(data["id"])
        team = session.get(Team, team_id)
        if team is None:
            session.add(
                Team(
                    id=team_id,
                    name=str(data.get("name") or f"Team {team_id}"),
                    country=data.get("country"),
                    logo_url=data.get("logo"),
                    raw=data,
                )
            )
        else:
            team.name = str(data.get("name") or team.name)
            team.country = data.get("country")
            team.logo_url = data.get("logo")
            team.raw = data
            team.updated_at = utc_now()
    session.flush()
    venue = fixture_data.get("venue") or {}
    status = fixture_data.get("status") or {}
    row = session.get(Fixture, fixture_id)
    values = {
        "league_id": league_id,
        "league_name": str(league.get("name") or f"League {league_id}"),
        "country": league.get("country"),
        "season": _as_int(league.get("season")),
        "fixture_date": _parse_datetime(fixture_data.get("date")),
        "kickoff_at": _parse_datetime(fixture_data.get("date")),
        "timezone": fixture_data.get("timezone"),
        "home_team_id": home_id,
        "away_team_id": away_id,
        "venue": venue.get("name"),
        "status": str(status.get("short") or "NS"),
        "elapsed": _as_int(status.get("elapsed")),
        "home_goals": _as_int(goals.get("home")),
        "away_goals": _as_int(goals.get("away")),
        "halftime_home_goals": _as_int(halftime.get("home")),
        "halftime_away_goals": _as_int(halftime.get("away")),
        "raw": item,
        "updated_at": utc_now(),
    }
    if row is None:
        row = Fixture(id=fixture_id, **values)
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    return True


def _store_odds(session: Session, item: dict[str, Any], *, as_of: datetime) -> int:
    fixture = item.get("fixture") or {}
    fixture_id = _as_int(fixture.get("id"))
    if fixture_id is None or session.get(Fixture, fixture_id) is None:
        return 0
    inserted = 0
    for bookmaker in item.get("bookmakers", []):
        bookmaker_id = _as_int(bookmaker.get("id"))
        bookmaker_name = str(bookmaker.get("name") or "Unknown bookmaker")
        for bet in bookmaker.get("bets", []):
            market_id = _as_int(bet.get("id"))
            if market_id is None or session.get(MarketRegistry, market_id) is None:
                continue
            market_name = str(bet.get("name") or "Unknown Market")
            for value in bet.get("values", []):
                provider_selection = str(value.get("value") or "").strip()
                selection, line = _selection_and_line(market_name, provider_selection)
                try:
                    price = Decimal(str(value.get("odd")))
                except InvalidOperation:
                    continue
                if not selection or price <= 1:
                    continue
                session.add(
                    OddsSnapshot(
                        fixture_id=fixture_id,
                        bookmaker_id=bookmaker_id,
                        bookmaker_name=bookmaker_name,
                        market_id=market_id,
                        market_name=market_name,
                        selection=selection,
                        line=line,
                        odds=price,
                        as_of=as_of,
                        provider_updated_at=_parse_datetime(bet.get("update"))
                        if bet.get("update")
                        else None,
                        raw={
                            "fixture": fixture,
                            "bookmaker": bookmaker,
                            "bet": bet,
                            "value": value,
                        },
                    )
                )
                inserted += 1
    return inserted


def _current_season(row: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (season for season in row.get("seasons", []) if season.get("current") is True), None
    )


def _store_snapshot(
    session: Session,
    *,
    data_type: str,
    scope_type: str,
    scope_key: str,
    as_of: datetime,
    payload: dict | list,
) -> None:
    session.add(
        DataSnapshot(
            data_type=data_type,
            scope_type=scope_type,
            scope_key=scope_key,
            as_of=as_of,
            source="api-football-v3",
            payload=payload,
        )
    )


def _configure_quota(client: ApiFootballClient) -> None:
    status = client.get("/status", use_cache=False).get("response", {})
    requests = status.get("requests", {})
    try:
        used = int(requests["current"])
        limit = int(requests["limit_day"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiFootballError("Could not read daily usage from API-Football /status.") from exc
    client.sync_quota(requests_used=used, daily_limit=limit)
    plan = str(status.get("subscription", {}).get("plan", "")).casefold()
    client.requests_per_minute = {
        "free": 10,
        "pro": 300,
        "ultra": 450,
        "mega": 900,
        "custom": 1200,
    }.get(plan, client.requests_per_minute)


def _ingest_context(
    client: ApiFootballClient,
    session: Session,
    *,
    days: list[date],
    leagues: dict[int, dict[str, Any]],
    fixtures: list[dict[str, Any]],
    as_of: datetime,
) -> tuple[int, int]:
    """Capture covered context, bounded by account headroom and a configurable daily budget."""
    remaining = client.quota.provider_remaining
    if remaining is None:
        remaining = max(0, client.quota.configured_limit - client.quota.requests_used)
    configured_budget = max(0, int(os.getenv("ACCALAB_DAILY_ENRICHMENT_BUDGET", "1000")))
    reserve = max(100, int(client.quota.configured_limit * 0.05))
    call_budget = min(configured_budget, max(0, remaining - reserve))
    calls_used = 0
    saved = 0
    skipped = 0

    def optional_get(path: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        nonlocal calls_used, skipped
        if calls_used >= call_budget:
            skipped += 1
            return None
        before = client.requests_this_session
        try:
            rows = client.get_all(path, params)
        except ApiFootballError:
            skipped += 1
            return None
        calls_used += client.requests_this_session - before
        return rows

    fixture_ids = {
        fixture_id
        for item in fixtures
        if (fixture_id := _as_int((item.get("fixture") or {}).get("id"))) is not None
    }
    fixtures_by_league: dict[int, list[dict[str, Any]]] = {}
    for item in fixtures:
        league_id = _as_int((item.get("league") or {}).get("id"))
        if league_id in leagues:
            fixtures_by_league.setdefault(league_id, []).append(item)

    injury_leagues = {
        league_id
        for league_id in fixtures_by_league
        if (leagues[league_id].get("coverage") or {}).get("injuries") is True
    }
    if injury_leagues:
        for day in days:
            rows = optional_get("/injuries", {"date": day.isoformat()})
            if rows is None:
                continue
            relevant = [
                row
                for row in rows
                if _as_int((row.get("league") or {}).get("id")) in injury_leagues
                and (
                    _as_int((row.get("fixture") or {}).get("id")) in fixture_ids
                    or not (row.get("fixture") or {}).get("id")
                )
            ]
            for league_id in injury_leagues:
                league_rows = [
                    row
                    for row in relevant
                    if _as_int((row.get("league") or {}).get("id")) == league_id
                ]
                _store_snapshot(
                    session,
                    data_type="injuries",
                    scope_type="league_date",
                    scope_key=f"{league_id}:{day.isoformat()}",
                    as_of=as_of,
                    payload=league_rows,
                )
                saved += 1

    detail_leagues = {
        league_id
        for league_id in fixtures_by_league
        if (leagues[league_id].get("coverage") or {}).get("fixtures", {}).get("lineups") is True
        or (leagues[league_id].get("coverage") or {}).get("fixtures", {}).get("statistics") is True
    }
    detail_ids = [
        fixture_id
        for item in fixtures
        if _as_int((item.get("league") or {}).get("id")) in detail_leagues
        if (fixture_id := _as_int((item.get("fixture") or {}).get("id"))) is not None
    ]
    for offset in range(0, len(detail_ids), 20):
        id_batch = detail_ids[offset : offset + 20]
        rows = optional_get("/fixtures", {"ids": "-".join(map(str, id_batch))})
        if rows is None:
            skipped += (len(detail_ids) - offset - len(id_batch) + 19) // 20
            break
        for row in rows:
            fixture_id = _as_int((row.get("fixture") or {}).get("id"))
            if fixture_id is None:
                continue
            for key, data_type in (("lineups", "lineups"), ("statistics", "fixture_statistics")):
                payload = row.get(key)
                if payload:
                    _store_snapshot(
                        session,
                        data_type=data_type,
                        scope_type="fixture",
                        scope_key=str(fixture_id),
                        as_of=as_of,
                        payload=payload,
                    )
                    saved += 1

    active_leagues = set(fixtures_by_league)
    for league_id in sorted(active_leagues):
        league = leagues[league_id]
        season = _current_season(league["row"])
        if season is None or (league.get("coverage") or {}).get("standings") is not True:
            continue
        year = _as_int(season.get("year"))
        if year is None:
            continue
        rows = optional_get("/standings", {"league": league_id, "season": year})
        if rows is None:
            continue
        _store_snapshot(
            session,
            data_type="standings",
            scope_type="league_season",
            scope_key=f"{league_id}:{year}",
            as_of=as_of,
            payload=rows,
        )
        saved += 1

    for league_id in sorted(active_leagues):
        league = leagues[league_id]
        season = _current_season(league["row"])
        coverage = (league.get("coverage") or {}).get("fixtures", {})
        if season is None or coverage.get("statistics") is not True:
            continue
        year = _as_int(season.get("year"))
        if year is None:
            continue
        teams = {
            team_id
            for item in fixtures_by_league[league_id]
            for team_id in (
                _as_int(((item.get("teams") or {}).get("home") or {}).get("id")),
                _as_int(((item.get("teams") or {}).get("away") or {}).get("id")),
            )
            if team_id is not None
        }
        for team_id in sorted(teams):
            rows = optional_get(
                "/teams/statistics", {"league": league_id, "season": year, "team": team_id}
            )
            if rows is None:
                continue
            _store_snapshot(
                session,
                data_type="team_statistics",
                scope_type="team_league_season",
                scope_key=f"{team_id}:{league_id}:{year}",
                as_of=as_of,
                payload=rows,
            )
            saved += 1
    return saved, skipped


def run_daily_ingest(
    client: ApiFootballClient,
    session: Session,
    settings: Settings,
    *,
    today: date | None = None,
) -> IngestSummary:
    """Ingest D through D+5 for all current leagues, optionally filtered by configured IDs."""
    run_date = today or datetime.now(UTC).date()
    _configure_quota(client)
    league_rows = client.get_all("/leagues", {"current": "true"})
    leagues = {
        league_id: {
            "row": row,
            "coverage": (_current_season(row) or {}).get("coverage", {}),
        }
        for row in league_rows
        if (league_id := _as_int((row.get("league") or {}).get("id"))) is not None
    }
    current_ids = set(leagues)
    if settings.league_ids:
        current_ids.intersection_update(settings.league_ids)
    leagues = {league_id: row for league_id, row in leagues.items() if league_id in current_ids}
    if not current_ids:
        raise ValueError("No current leagues matched the configured league scope.")

    markets = client.get_all("/odds/bets")
    market_count = sync_market_registry(session, markets)
    session.flush()

    fixtures_by_date: list[dict[str, Any]] = []
    odds_by_date: list[dict[str, Any]] = []
    for offset in range(settings.future_days + 1):
        day = run_date + timedelta(days=offset)
        fixtures_by_date.extend(
            client.get_all("/fixtures", {"date": day.isoformat(), "timezone": settings.timezone})
        )
        odds_by_date.extend(client.get_all("/odds", {"date": day.isoformat()}))

    fixture_ids: set[int] = set()
    fixture_count = 0
    for item in fixtures_by_date:
        league_id = _as_int((item.get("league") or {}).get("id"))
        fixture_id = _as_int((item.get("fixture") or {}).get("id"))
        if league_id in current_ids and _store_fixture(session, item):
            fixture_count += 1
            if fixture_id is not None:
                fixture_ids.add(fixture_id)
    session.flush()

    odds_by_fixture = {
        fixture_id: row
        for row in odds_by_date
        if (fixture_id := _as_int((row.get("fixture") or {}).get("id"))) in fixture_ids
    }
    as_of = datetime.now(UTC)
    odds_count = sum(_store_odds(session, row, as_of=as_of) for row in odds_by_fixture.values())
    days = [run_date + timedelta(days=offset) for offset in range(settings.future_days + 1)]
    context_count, enrichment_skipped = _ingest_context(
        client, session, days=days, leagues=leagues, fixtures=fixtures_by_date, as_of=as_of
    )
    session.commit()
    return IngestSummary(
        run_date=run_date,
        league_count=len(current_ids),
        fixture_count=fixture_count,
        odds_count=odds_count,
        market_count=market_count,
        requests_used=client.requests_this_session,
        cache_hits=client.cache_hits,
        context_snapshots=context_count,
        enrichment_skipped=enrichment_skipped,
    )
