from __future__ import annotations

import os
import re
from datetime import timedelta
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
from accalab.models import AuthToken, User, utc_now
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
