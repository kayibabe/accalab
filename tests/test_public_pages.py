from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from accalab.entitlements import ROLE_ENTITLEMENTS
from accalab.models import AuditLog
from accalab.web.public import public_plan_rows, save_site_content

from test_auth_and_shell import _client_for_role


PUBLIC_ROUTES = (
    "/",
    "/pricing",
    "/how-it-works",
    "/record",
    "/register",
    "/responsible-gambling",
    "/terms",
    "/privacy",
)


def test_public_pages_render_and_do_not_make_guaranteed_profit_claims():
    client, _factory, engine = _client_for_role("free")
    client.post("/logout", data={"csrf_token": re.search(
        r'name="csrf_token" value="([^"]+)"', client.get("/login").text
    ).group(1)})
    banned_claim = re.compile(
        r"\b(guaranteed?\s+(?:profit|returns?)|risk[- ]free|sure win|easy money)\b",
        re.IGNORECASE,
    )
    for route in PUBLIC_ROUTES:
        response = client.get(route)
        assert response.status_code == 200, route
        assert "18+" in response.text or route == "/register"
        assert not banned_claim.search(response.text), route
    engine.dispose()


def test_registration_closed_waitlist_requires_consent_and_deduplicates():
    client, factory, engine = _client_for_role("free")
    page = client.get("/register")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    missing_consent = client.post(
        "/register", data={"csrf_token": token, "email": "person@example.com"}
    )
    assert missing_consent.status_code == 200
    assert "Please agree" in missing_consent.text
    for _ in range(2):
        response = client.post(
            "/register",
            data={
                "csrf_token": token,
                "email": "PERSON@example.com",
                "consent": "yes",
            },
        )
        assert "interest has been recorded" in response.text
    with factory() as session:
        from accalab.models import WaitlistEntry

        assert session.query(WaitlistEntry).count() == 1
        assert session.query(WaitlistEntry).one().email == "person@example.com"
    engine.dispose()


def test_plan_comparison_is_derived_from_entitlements_and_config():
    content = {
        "plans": {
            "free": {"price_mwk": 0, "billing_period": "", "description": "Free copy"},
            "pro": {"price_mwk": None, "billing_period": "30 days", "description": "Pro copy"},
        }
    }
    rows = public_plan_rows(content)
    assert rows[0]["capabilities"] == ROLE_ENTITLEMENTS["free"]
    assert rows[1]["capabilities"] == ROLE_ENTITLEMENTS["pro"]
    assert rows[1]["price"] == "Price to be announced"


def test_admin_site_copy_save_validates_and_preserves_registration_flag(tmp_path: Path):
    path = tmp_path / "site_content.json"
    path.write_text(
        json.dumps(
            {
                "registration_open": False,
                "headline": "Old",
                "promise": "Old summary",
                "plans": {
                    "free": {"price_mwk": 0, "billing_period": "", "description": "Free"},
                    "pro": {"price_mwk": None, "billing_period": "30 days", "description": "Pro"},
                },
            }
        ),
        encoding="utf-8",
    )
    updated = save_site_content(
        headline="Clear football context",
        promise="Review fixtures and dated prices.",
        free_description="A limited set of fixture and result views.",
        pro_description="Full selection detail and history.",
        pro_price_mwk="12500",
        path=path,
    )
    assert updated["registration_open"] is False
    assert updated["plans"]["pro"]["price_mwk"] == 12500
    with pytest.raises(ValueError):
        save_site_content(
            headline="",
            promise="Summary",
            free_description="Free",
            pro_description="Pro",
            pro_price_mwk="invalid",
            path=path,
        )


def test_site_content_update_is_admin_only_and_audited(monkeypatch):
    import accalab.web.app as app_module

    pro_client, _pro_factory, pro_engine = _client_for_role("pro")
    pro_page = pro_client.get("/admin/site-content")
    assert pro_page.status_code == 403
    token = re.search(r'name="csrf_token" value="([^"]+)"', pro_client.get("/login").text).group(1)
    denied = pro_client.post(
        "/admin/site-content",
        data={
            "csrf_token": token,
            "headline": "Headline",
            "promise": "Summary",
            "free_description": "Free plan",
            "pro_description": "Pro plan",
            "pro_price_mwk": "",
        },
    )
    assert denied.status_code == 403
    pro_engine.dispose()

    admin_client, factory, engine = _client_for_role("admin")
    monkeypatch.setattr(app_module, "save_site_content", lambda **_kwargs: {})
    page = admin_client.get("/admin/site-content")
    assert "Edit public copy and plans" in page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    saved = admin_client.post(
        "/admin/site-content",
        data={
            "csrf_token": token,
            "headline": "Headline",
            "promise": "Summary",
            "free_description": "Free plan",
            "pro_description": "Pro plan",
            "pro_price_mwk": "",
        },
    )
    assert saved.status_code == 303
    with factory() as session:
        event = session.query(AuditLog).filter_by(action="update_site_content").one()
        assert event.target_type == "site_content"
    engine.dispose()
