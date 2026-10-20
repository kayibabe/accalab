"""Public page configuration derived from the central entitlement registry."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accalab.config import ROOT, load_settings
from accalab.entitlements import ROLE_ENTITLEMENTS
from accalab.models import FreeWeeklySample, Ticket
from accalab.tracking.metrics import build_performance_report
from accalab.web.pages import _current_results, _ticket_card

SITE_CONTENT_PATH = ROOT / "config" / "site_content.json"
CAPABILITY_LABELS = {
    "today": "Today's fixture list",
    "fixtures": "Fixture browsing",
    "pro": "Picks and selection analysis",
    "tickets": "Ticket details and history",
    "results": "Settled results",
    "performance": "Performance summaries",
    "learn": "Learning pages",
    "account": "Account settings",
}


def load_site_content(path: Path = SITE_CONTENT_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("plans"), dict):
        raise ValueError("Site content configuration has an invalid shape.")
    return data


def public_plan_rows(content: dict) -> list[dict]:
    plans = content["plans"]
    rows = []
    for plan_name in ("free", "pro"):
        plan = plans[plan_name]
        entitlement_role = "free" if plan_name == "free" else "pro"
        price = plan.get("price_mwk")
        rows.append(
            {
                "name": plan_name.title(),
                "description": plan["description"],
                "price": "Free" if plan_name == "free" else (
                    f"MWK {int(price):,} / {plan['billing_period']}" if price else "Price to be announced"
                ),
                "price_mwk": price,
                "billing_period": plan["billing_period"],
                "capabilities": ROLE_ENTITLEMENTS[entitlement_role],
            }
        )
    return rows


def build_plan_page_context() -> dict:
    content = load_site_content()
    return {
        "content": content,
        "plans": public_plan_rows(content),
        "capability_labels": CAPABILITY_LABELS,
        "registration_open": content["registration_open"],
    }


def save_site_content(
    *,
    headline: str,
    promise: str,
    free_description: str,
    pro_description: str,
    pro_price_mwk: str,
    path: Path = SITE_CONTENT_PATH,
) -> dict:
    current = load_site_content(path)
    headline = headline.strip()
    promise = promise.strip()
    free_description = free_description.strip()
    pro_description = pro_description.strip()
    if not headline or len(headline) > 120:
        raise ValueError("Headline must contain 1 to 120 characters.")
    if not promise or len(promise) > 360:
        raise ValueError("Landing summary must contain 1 to 360 characters.")
    if not free_description or len(free_description) > 360:
        raise ValueError("Free plan description must contain 1 to 360 characters.")
    if not pro_description or len(pro_description) > 360:
        raise ValueError("Pro plan description must contain 1 to 360 characters.")
    cleaned_price = pro_price_mwk.strip().replace(",", "")
    if cleaned_price:
        if not re.fullmatch(r"[1-9][0-9]{0,8}", cleaned_price):
            raise ValueError("Pro price must be a whole MWK amount or left blank.")
        price = int(cleaned_price)
    else:
        price = None
    current.update({"headline": headline, "promise": promise})
    current["plans"]["free"].update({"description": free_description})
    current["plans"]["pro"].update({"description": pro_description, "price_mwk": price})

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix="site-content-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(current, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return current


PUBLIC_PAGES = {
    "/how-it-works": {
        "title": "How it works",
        "intro": "AccaLab organizes football information into a reviewable daily workflow.",
        "sections": [
            ("Review the fixtures", "See the scheduled matches and the information captured for each one."),
            ("Compare the available prices", "Odds are shown with their capture time so you can tell when a quote was observed."),
            ("Follow the record", "Published tickets and settled results are tracked over time. Historical outcomes do not predict future results."),
        ],
    },
    "/responsible-gambling": {
        "title": "Responsible gambling",
        "intro": "AccaLab is for adults aged 18 and over. Football outcomes are uncertain, and no analysis removes that uncertainty.",
        "sections": [
            ("Set your own limits", "Only choose to gamble where it is legal for you, set a limit you can afford, and stop when it is reached."),
            ("Take a break", "Do not chase losses or borrow to gamble. If gambling stops feeling manageable, step away and contact a trusted person or a local support service."),
            ("What AccaLab does", "AccaLab provides football analysis and ticket tracking. It does not accept stakes or place bets."),
        ],
    },
    "/terms": {
        "title": "Terms of use",
        "intro": "These terms describe the AccaLab analysis and tracking service.",
        "sections": [
            ("Service scope", "AccaLab presents football information, odds observations and accumulator-ticket analysis. It does not take deposits, accept bets or place bets for you."),
            ("Your responsibility", "You are responsible for checking information, complying with the laws that apply to you, and deciding whether to use any third-party betting service. Access is for adults aged 18 and over."),
            ("No outcome promise", "Predictions and past results are uncertain measures. They are not a promise of future accuracy, profit or a particular result."),
            ("Availability and changes", "Features and data sources may change. We may correct errors, update these terms, or suspend access to protect the service."),
        ],
    },
    "/privacy": {
        "title": "Privacy notice",
        "intro": "This notice explains the account and service information AccaLab currently handles.",
        "sections": [
            ("Information stored", "If you join the waitlist, we store your email address and the time you agreed to receive launch information. Account records include email, a password hash, plan and status. Payment records include amount, provider transaction references and payment status. AccaLab does not store card numbers or mobile money PINs. Sign-in and service events may be logged for security and operation."),
            ("How it is used", "Information is used to operate accounts, reconcile payments, extend prepaid access, respond to requests, protect access and provide the service. Waitlist details are used to contact people about launch."),
            ("Security and retention", "Passwords are stored as hashes. Session cookies are protected with HttpOnly and SameSite settings. We retain information for service operation and applicable recordkeeping, then remove it when no longer needed."),
            ("Questions", "Contact the service operator to ask about your information or request a correction or deletion, subject to records we must retain."),
        ],
    },
}


def build_landing_context(session: Session) -> dict:
    content = load_site_content()
    plan_rows = public_plan_rows(content)
    now = datetime.now(UTC)
    timezone_name = load_settings().timezone
    sample = session.scalar(
        select(FreeWeeklySample).order_by(FreeWeeklySample.week_start.desc()).limit(1)
    )
    ticket = session.get(Ticket, sample.ticket_id) if sample else None
    example_label = "Free sample" if ticket is not None else "Recent settled ticket"
    if ticket is None:
        today = now.astimezone(ZoneInfo(timezone_name)).date()
        ticket = session.scalar(
            select(Ticket)
            .where(
                Ticket.status == "official",
                Ticket.target_date == today - timedelta(days=1),
                Ticket.result_status.is_not(None),
            )
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
    example = None
    if ticket is not None:
        example = _ticket_card(
            session,
            ticket,
            role="free",
            is_sample=True,
            results=_current_results(session),
            timezone_name=timezone_name,
            now=now,
        )
    report = build_performance_report(session, include_details=False)
    aggregate = report["aggregate"]
    start = session.scalar(
        select(func.min(Ticket.target_date)).where(
            Ticket.status == "official", Ticket.result_status.is_not(None)
        )
    )
    return {
        "content": content,
        "plans": plan_rows,
        "example": example,
        "example_label": example_label if example else None,
        "live_record": {
            "start": start.isoformat() if start else None,
            "tickets": aggregate["tickets"],
            "roi": aggregate["roi"],
            "roi_ci_low": aggregate["roi_ci_low"],
            "roi_ci_high": aggregate["roi_ci_high"],
            "sample_note": report["sample_size_note"],
        },
        "registration_open": content["registration_open"],
        "capability_labels": CAPABILITY_LABELS,
    }


def build_record_context(session: Session) -> dict:
    context = build_landing_context(session)
    return {"record": context["live_record"]}
