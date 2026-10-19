"""Small API-Football v3 client with throttling, retries, cache and quota accounting."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


class ApiFootballError(RuntimeError):
    """An API-Football request failed or returned an API error."""


class QuotaExceeded(ApiFootballError):
    """The configured daily request ceiling would be exceeded."""


@dataclass(frozen=True)
class QuotaSnapshot:
    requests_used: int
    configured_limit: int
    provider_remaining: int | None = None
    provider_limit: int | None = None


class ApiFootballClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        cache_path: Path,
        cache_ttl_seconds: int = 900,
        timeout_seconds: float = 20,
        retries: int = 3,
        backoff_seconds: float = 0.5,
        requests_per_minute: int = 10,
        daily_quota_limit: int = 100,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        self.api_key = api_key
        self.cache_ttl_seconds = cache_ttl_seconds
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.requests_per_minute = requests_per_minute
        self.daily_quota_limit = daily_quota_limit
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        self._provider_remaining: int | None = None
        self._provider_limit: int | None = None
        self._requests_this_session = 0
        self._cache_hits = 0
        self._db_path = cache_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS response_cache "
                "(cache_key TEXT PRIMARY KEY, expires_at REAL NOT NULL, body TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS quota_usage "
                "(usage_date TEXT PRIMARY KEY, requests_used INTEGER NOT NULL)"
            )
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"x-apisports-key": api_key, "accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._db_path)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any]) -> str:
        material = json.dumps([path.lstrip("/"), sorted(params.items())], separators=(",", ":"))
        return hashlib.sha256(material.encode()).hexdigest()

    def _cached(self, key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT body, expires_at FROM response_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            body, expires_at = row
            if expires_at <= time.time():
                db.execute("DELETE FROM response_cache WHERE cache_key = ?", (key,))
                return None
            return json.loads(body)

    def _cache(self, key: str, body: dict[str, Any]) -> None:
        expires_at = time.time() + self.cache_ttl_seconds
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO response_cache(cache_key, expires_at, body) VALUES (?, ?, ?)",
                (key, expires_at, json.dumps(body, separators=(",", ":"))),
            )

    def _used_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT requests_used FROM quota_usage WHERE usage_date = ?", (today,)
            ).fetchone()
        return int(row[0]) if row else 0

    def _record_request(self) -> None:
        today = datetime.now(UTC).date().isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO quota_usage(usage_date, requests_used) VALUES (?, 1) "
                "ON CONFLICT(usage_date) DO UPDATE SET requests_used = requests_used + 1",
                (today,),
            )

    @staticmethod
    def _header_int(headers: httpx.Headers, *names: str) -> int | None:
        for name in names:
            value = headers.get(name)
            if value is not None:
                try:
                    return int(value)
                except ValueError:
                    continue
        return None

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        normalized = {key: value for key, value in (params or {}).items() if value is not None}
        key = self._cache_key(path, normalized)
        cached = self._cached(key) if use_cache else None
        if cached is not None:
            self._cache_hits += 1
            return cached
        if self._provider_remaining is not None and self._provider_remaining <= 0:
            raise QuotaExceeded("API-Football reports that no requests remain in today's quota.")
        if self._used_today() >= self.daily_quota_limit:
            raise QuotaExceeded(f"Configured daily API request ceiling ({self.daily_quota_limit}) reached.")

        interval = 60 / max(1, self.requests_per_minute)
        for attempt in range(self.retries + 1):
            if self._last_request_at is not None:
                delay = interval - (self._monotonic() - self._last_request_at)
                if delay > 0:
                    self._sleep(delay)
            self._last_request_at = self._monotonic()
            try:
                response = self._client.get(path.lstrip("/"), params=normalized)
            except httpx.TransportError as exc:
                if attempt >= self.retries:
                    raise ApiFootballError(f"Network request failed: {exc}") from exc
                self._sleep(self.backoff_seconds * (2**attempt))
                continue

            self._record_request()
            self._requests_this_session += 1
            self._provider_remaining = self._header_int(
                response.headers, "x-ratelimit-requests-remaining", "x-ratelimit-remaining"
            )
            self._provider_limit = self._header_int(
                response.headers, "x-ratelimit-requests-limit", "x-ratelimit-limit"
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.retries:
                    retry_after = response.headers.get("retry-after")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else (
                        self.backoff_seconds * (2**attempt)
                    )
                    self._sleep(delay)
                    continue
            try:
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise ApiFootballError(f"API-Football HTTP error ({response.status_code}): {exc}") from exc
            if not isinstance(body, dict):
                raise ApiFootballError("API-Football returned a non-object JSON response.")
            if body.get("errors"):
                raise ApiFootballError(f"API-Football error: {body['errors']}")
            if use_cache:
                self._cache(key, body)
            return body
        raise ApiFootballError("Request retries were exhausted.")

    def get_all(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch every page returned by an endpoint's paging metadata."""
        base_params = dict(params or {})
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            page_params = base_params if page == 1 else {**base_params, "page": page}
            payload = self.get(path, page_params)
            items.extend(item for item in payload.get("response", []) if isinstance(item, dict))
            paging = payload.get("paging", {})
            try:
                total_pages = max(1, int(paging.get("total", 1)))
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                return items
            page += 1

    def sync_quota(self, *, requests_used: int, daily_limit: int) -> None:
        """Reconcile local protection with API-Football's /status result."""
        today = datetime.now(UTC).date().isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO quota_usage(usage_date, requests_used) VALUES (?, ?) "
                "ON CONFLICT(usage_date) DO UPDATE SET requests_used = "
                "MAX(requests_used, excluded.requests_used)",
                (today, max(0, requests_used)),
            )
        self.daily_quota_limit = max(0, daily_limit)
        self._provider_limit = self.daily_quota_limit
        self._provider_remaining = max(0, self.daily_quota_limit - requests_used)

    @property
    def requests_this_session(self) -> int:
        return self._requests_this_session

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    @property
    def quota(self) -> QuotaSnapshot:
        return QuotaSnapshot(
            requests_used=self._used_today(),
            configured_limit=self.daily_quota_limit,
            provider_remaining=self._provider_remaining,
            provider_limit=self._provider_limit,
        )

    def close(self) -> None:
        self._client.close()
