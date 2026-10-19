from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-that-is-at-least-thirty-two-characters")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from accalab.auth.security import (
    consume_auth_token,
    create_auth_token,
    hash_password,
    login_is_locked,
    record_login_failure,
)
from accalab.db import Base
from accalab.entitlements import ADMIN_SECTIONS, SHARED_SECTIONS
from accalab.config import load_settings
from accalab.models import (
    AuthToken,
    AgentLog,
    Fixture,
    FreeWeeklySample,
    MarketRegistry,
    OddsSnapshot,
    Team,
    Ticket,
    TicketLeg,
    User,
    utc_now,
)
from accalab.web.app import create_app, get_db


def _client_for_role(role: str):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        user = User(
            email=f"{role}@example.com",
            password_hash=hash_password("correct horse battery staple"),
            role=role,
            plan=role,
            status="active",
            email_verified_at=utc_now(),
        )
        session.add(user)
    app = create_app()

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, follow_redirects=False)
    response = client.get("/login")
    csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    client.post(
        "/login",
        data={
            "email": f"{role}@example.com",
            "password": "correct horse battery staple",
            "csrf_token": csrf_token,
        },
    )
    return client, factory, engine


def test_role_access_for_every_sidebar_route():
    for role in ("free", "pro", "admin"):
        client, _factory, engine = _client_for_role(role)
        for section in SHARED_SECTIONS:
            response = client.get(section.path)
            expected = 403 if role == "free" and section.entitlement == "pro" else 200
            assert response.status_code == expected, (role, section.path, response.status_code)
        for section in ADMIN_SECTIONS:
            response = client.get(section.path)
            assert response.status_code == (200 if role == "admin" else 403)
            if role == "admin":
                assert section.label in response.text
                if section.label not in {"Messaging", "Payments", "Backtests and pre-launch tracking"}:
                    assert "data-sortable" in response.text, section.path
        engine.dispose()


def test_admin_agent_view_uses_summary_and_never_renders_call_payloads():
    client, factory, engine = _client_for_role("admin")
    with factory.begin() as session:
        session.add(
            AgentLog(
                agent_name="DataQuality",
                as_of=utc_now(),
                input_data={"private": "DO NOT SHOW INPUT"},
                output_data={"private": "DO NOT SHOW OUTPUT"},
                cost_usd=Decimal("0.0123"),
                status="ok",
            )
        )
    response = client.get("/admin/agents")
    assert response.status_code == 200
    assert "DataQuality" in response.text
    assert "DO NOT SHOW INPUT" not in response.text
    assert "DO NOT SHOW OUTPUT" not in response.text
    assert "data-sortable" in response.text
    engine.dispose()


def test_free_today_reveals_only_the_logged_sample_and_locks_other_tickets():
    client, factory, engine = _client_for_role("free")
    timezone_name = load_settings().timezone
    from zoneinfo import ZoneInfo

    now = utc_now()
    today = datetime.now(ZoneInfo(timezone_name)).date()
    kickoff = datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo(timezone_name))
    with factory.begin() as session:
        session.add_all(
            [
                Team(id=101, name="Sample Home"),
                Team(id=102, name="Sample Away"),
                Team(id=103, name="Hidden Home"),
                Team(id=104, name="Hidden Away"),
            ]
        )
        session.add_all(
            [
                Fixture(
                    id=901,
                    league_id=39,
                    league_name="Visible League",
                    fixture_date=kickoff,
                    kickoff_at=kickoff,
                    home_team_id=101,
                    away_team_id=102,
                    status="NS",
                ),
                Fixture(
                    id=902,
                    league_id=39,
                    league_name="Visible League",
                    fixture_date=kickoff,
                    kickoff_at=kickoff,
                    home_team_id=103,
                    away_team_id=104,
                    status="NS",
                ),
            ]
        )
        session.add(
            MarketRegistry(
                id=1,
                canonical_name="1X2",
                display_name="Match Winner",
                supported=True,
                ticket_eligible=True,
            )
        )
        session.add_all(
            [
                OddsSnapshot(
                    fixture_id=901,
                    bookmaker_name="Book",
                    market_id=1,
                    market_name="Match Winner",
                    selection="SAMPLE SELECTION",
                    odds=Decimal("2.00"),
                    as_of=now,
                    raw={},
                ),
                OddsSnapshot(
                    fixture_id=902,
                    bookmaker_name="Book",
                    market_id=1,
                    market_name="Match Winner",
                    selection="SECRET SELECTION",
                    odds=Decimal("2.00"),
                    as_of=now,
                    raw={},
                ),
                Ticket(
                    id="sample-free-ticket",
                    target_date=today,
                    status="official",
                    band="T1",
                    combined_odds=Decimal("4.000000"),
                    as_of=now,
                ),
                Ticket(
                    id="locked-free-ticket",
                    target_date=today,
                    status="official",
                    band="T2",
                    combined_odds=Decimal("8.500000"),
                    as_of=now,
                ),
            ]
        )
        session.flush()
        quotes = {quote.fixture_id: quote.id for quote in session.query(OddsSnapshot).all()}
        session.add_all(
            [
                TicketLeg(
                    ticket_id="sample-free-ticket",
                    fixture_id=901,
                    market_id=1,
                    selection="SAMPLE SELECTION",
                    odds_snapshot_id=quotes[901],
                    odds=Decimal("2.00"),
                    probability=Decimal("0.9900000"),
                    p_low=Decimal("0.9500000"),
                    why="SAMPLE WHY CONTENT",
                ),
                TicketLeg(
                    ticket_id="locked-free-ticket",
                    fixture_id=902,
                    market_id=1,
                    selection="SECRET SELECTION",
                    odds_snapshot_id=quotes[902],
                    odds=Decimal("2.00"),
                    probability=Decimal("0.9900000"),
                    p_low=Decimal("0.9500000"),
                    why="SECRET WHY CONTENT",
                ),
            ]
        )
        session.add(
            FreeWeeklySample(
                week_start=today - timedelta(days=today.weekday()),
                ticket_id="sample-free-ticket",
                selected_at=now,
                selection_rule="saturday_then_next_available_day",
            )
        )
    response = client.get("/app/today")
    assert response.status_code == 200
    assert "Free sample" in response.text
    assert "SAMPLE SELECTION" in response.text
    assert "T2 ticket" in response.text
    assert "5.00–9.99" in response.text
    assert "SECRET SELECTION" not in response.text
    assert "SECRET WHY CONTENT" not in response.text
    assert "SAMPLE WHY CONTENT" not in response.text
    assert "0.9900000" not in response.text
    engine.dispose()


def test_login_requires_csrf_verification_and_applies_persistent_throttle():
    client, factory, engine = _client_for_role("free")
    denied = client.post(
        "/login",
        data={"email": "free@example.com", "password": "bad", "csrf_token": "invalid"},
    )
    assert denied.status_code == 403
    with factory.begin() as session:
        for _ in range(5):
            record_login_failure(session, "FREE@example.com")
        assert login_is_locked(session, "free@example.com")
    response = client.post(
        "/login",
        data={
            "email": "free@example.com",
            "password": "correct horse battery staple",
            "csrf_token": re.search(
                r'name="csrf_token" value="([^"]+)"', client.get("/login").text
            ).group(1),
        },
    )
    assert "temporarily unavailable" in response.text
    engine.dispose()


def test_auth_tokens_are_hashed_expiring_and_single_use():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        user = User(email="token@example.com", password_hash=hash_password("long enough password"))
        session.add(user)
        session.flush()
        token = create_auth_token(session, user, "password_reset", ttl=timedelta(minutes=5))
        assert token not in session.query(AuthToken).first().token_hash
        assert consume_auth_token(session, token, "password_reset").id == user.id
        assert consume_auth_token(session, token, "password_reset") is None
        assert consume_auth_token(session, "wrong", "verify_email") is None
        expired = create_auth_token(session, user, "verify_email", ttl=timedelta(seconds=-1))
        assert consume_auth_token(session, expired, "verify_email") is None
    engine.dispose()


def test_shared_sortable_table_and_template_copy_rules():
    root = Path(__file__).parents[1] / "accalab" / "web" / "templates"
    macro = (root / "components" / "sortable_table.html").read_text(encoding="utf-8")
    assert 'aria-sort="none"' in macro
    assert "data-sort-kind" in macro
    assert '<button type="button" class="table-sort">' in macro
    assert 'addEventListener("click"' in (root.parent / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    banned = re.compile(
        r"\b(research|experimental|prototype|paper\s+trading|beta)\b", re.IGNORECASE
    )
    for template in root.rglob("*.html"):
        if "admin" in template.parts:
            continue
        assert not banned.search(template.read_text(encoding="utf-8")), template
        assert not re.search(
            r"</?(?:b|strong)\b", template.read_text(encoding="utf-8"), re.IGNORECASE
        ), template
    css = (root.parent / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
    assert re.findall(r"font-weight\s*:\s*600", css) == ["font-weight: 600"]
