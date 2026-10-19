"""Rule-based selection and audit trail for the weekly Free ticket sample."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from accalab.config import ROOT
from accalab.models import Fixture, FreeWeeklySample, Result, Ticket, TicketLeg

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _settings(path: Path | None = None) -> tuple[bool, int, str]:
    raw = tomllib.loads((path or ROOT / "config" / "tracking.toml").read_text(encoding="utf-8"))
    item = raw["free_weekly_sample"]
    weekday = str(item["weekday"]).casefold()
    if weekday not in WEEKDAYS:
        raise ValueError("Free weekly sample weekday must be a day name.")
    timezone_name = str(item["timezone"])
    ZoneInfo(timezone_name)
    return bool(item["enabled"]), WEEKDAYS[weekday], timezone_name


def select_weekly_sample(
    session: Session,
    *,
    now: datetime | None = None,
    config_path: Path | None = None,
) -> FreeWeeklySample | None:
    """Select the first eligible official T1 in the configured weekday window."""
    enabled, configured_weekday, timezone_name = _settings(config_path)
    if not enabled:
        return None
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Sample selection time must include a timezone.")
    local_now = now.astimezone(ZoneInfo(timezone_name))
    week_start = local_now.date() - timedelta(days=local_now.weekday())
    current = session.get(FreeWeeklySample, week_start)
    if current is not None:
        return current
    first_day = week_start + timedelta(days=configured_weekday)
    candidates = session.scalars(
        select(Ticket)
        .where(
            Ticket.status == "official",
            Ticket.band == "T1",
            Ticket.target_date >= first_day,
            Ticket.target_date <= week_start + timedelta(days=6),
            Ticket.as_of <= now,
        )
        .order_by(Ticket.target_date, Ticket.created_at, Ticket.id)
    ).all()
    for ticket in candidates:
        legs = session.scalars(select(TicketLeg).where(TicketLeg.ticket_id == ticket.id)).all()
        if not legs:
            continue
        kickoff_times = [
            session.scalar(select(Fixture.kickoff_at).where(Fixture.id == leg.fixture_id))
            for leg in legs
        ]
        if not kickoff_times or any(kickoff is None or kickoff <= now for kickoff in kickoff_times):
            continue
        settled = session.scalar(
            select(Result.id).where(Result.ticket_leg_id.in_([leg.id for leg in legs])).limit(1)
        )
        if settled is not None:
            continue
        sample = FreeWeeklySample(
            week_start=week_start,
            ticket_id=ticket.id,
            selected_at=now,
            selection_rule=f"{WEEKDAYS_NAME[configured_weekday]}_then_next_available_day",
        )
        session.add(sample)
        session.flush()
        return sample
    return None


WEEKDAYS_NAME = {value: key for key, value in WEEKDAYS.items()}
