"""Single source for role access and the sidebar that presents it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    label: str
    path: str
    entitlement: str


SHARED_SECTIONS = (
    Section("Today", "/app/today", "today"),
    Section("Fixtures", "/app/fixtures", "fixtures"),
    Section("Picks", "/app/picks", "pro"),
    Section("Tickets", "/app/tickets", "tickets"),
    Section("Results", "/app/results", "results"),
    Section("Performance", "/app/performance", "performance"),
    Section("Learn", "/app/learn", "learn"),
    Section("Account", "/app/account", "account"),
)

ADMIN_SECTIONS = (
    Section("Users and plans", "/admin/users-and-plans", "admin"),
    Section("Data and ingestion", "/admin/data-and-ingestion", "admin"),
    Section("Markets registry", "/admin/markets-registry", "admin"),
    Section("Models and calibration", "/admin/models-and-calibration", "admin"),
    Section("Agents", "/admin/agents", "admin"),
    Section(
        "Backtests and pre-launch tracking", "/admin/backtests-and-pre-launch-tracking", "admin"
    ),
    Section("Builder and thresholds", "/admin/builder-and-thresholds", "admin"),
    Section("Messaging", "/admin/messaging", "admin"),
    Section("Payments", "/admin/payments", "admin"),
    Section("Jobs and logs", "/admin/jobs-and-logs", "admin"),
    Section("Audit log", "/admin/audit-log", "admin"),
    Section("Site content", "/admin/site-content", "admin"),
)

ROLE_ENTITLEMENTS = {
    "free": {"today", "fixtures", "tickets", "results", "performance", "learn", "account"},
    "pro": {"today", "fixtures", "pro", "tickets", "results", "performance", "learn", "account"},
    "admin": {
        "today",
        "fixtures",
        "pro",
        "tickets",
        "results",
        "performance",
        "learn",
        "account",
        "admin",
    },
}


def allowed(role: str, entitlement: str) -> bool:
    return entitlement in ROLE_ENTITLEMENTS.get(role, set())


def sidebar_for(role: str) -> tuple[tuple[str, tuple[Section, ...]], ...]:
    groups: list[tuple[str, tuple[Section, ...]]] = [("", SHARED_SECTIONS)]
    if role == "admin":
        groups.append(("Administration", ADMIN_SECTIONS))
    return tuple(groups)


def free_page_note(section: str) -> str | None:
    """Describe Free limits without exposing unavailable data to templates."""
    return {
        "fixtures": "Today's fixtures are available here.",
        "tickets": "Recent settled tickets and today's locked ticket cards appear here.",
        "results": "Settled tickets from one to three days ago appear here.",
        "performance": "The 30-day headline and sample size appear here.",
    }.get(section)
