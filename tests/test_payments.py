from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from accalab.db import Base
from accalab.entitlements import allowed
from accalab.models import Payment, Subscription, User, utc_now
from accalab.payments.providers import (
    PaymentProviderError,
    PayChanguProvider,
)
from accalab.payments.service import (
    apply_verified_payment,
    mark_manual_payment_paid,
    new_payment,
    run_payment_maintenance,
)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            email="payer@example.com",
            password_hash="hash",
            role="free",
            plan="free",
            status="active",
            email_verified_at=utc_now(),
        )
        db.add(user)
        db.flush()
        yield db, user
    engine.dispose()


def _verified(payment: Payment, *, status="success", amount=12000) -> dict:
    return {
        "tx_ref": payment.tx_ref,
        "status": status,
        "currency": "MWK",
        "amount": amount,
        "reference": "PROVIDER-REF-1",
    }


def test_verified_payment_activates_pro_once_and_entitlement_is_immediate(session):
    db, user = session
    payment = new_payment(db, user, 12000, "paychangu")
    now = datetime(2026, 10, 19, tzinfo=UTC)
    first = apply_verified_payment(db, payment, _verified(payment), period_days=30, grace_days=5, now=now)
    end = user.plan_end
    second = apply_verified_payment(db, payment, _verified(payment), period_days=30, grace_days=5, now=now)

    assert first is second
    assert user.role == "pro" and user.plan == "pro"
    assert allowed(user.role, "pro")
    assert user.plan_end == end == now + timedelta(days=30)
    assert db.scalar(select(func.count()).select_from(Subscription)) == 1


def test_manual_reference_and_admin_period_are_idempotent(session):
    db, user = session
    payment = new_payment(db, user, 12000, "manual")
    first = mark_manual_payment_paid(db, payment, "MPAMBA-123", period_days=30, grace_days=5)
    end = user.plan_end
    second = mark_manual_payment_paid(db, payment, "MPAMBA-123", period_days=30, grace_days=5)
    assert first is second
    assert payment.manual_reference == "MPAMBA-123"
    assert user.plan_end == end
    assert db.scalar(select(func.count()).select_from(Subscription)) == 1


def test_verified_payment_rejects_wrong_currency_reference_and_short_amount(session):
    db, user = session
    payment = new_payment(db, user, 12000, "paychangu")
    for field, value in (("tx_ref", "wrong"), ("currency", "USD"), ("amount", 11999)):
        details = _verified(payment)
        details[field] = value
        with pytest.raises(ValueError):
            apply_verified_payment(db, payment, details, period_days=30, grace_days=5)


def test_maintenance_grace_and_expiry_downgrades_free_but_preserves_admin(session):
    db, user = session
    now = datetime(2026, 10, 19, tzinfo=UTC)
    payment = new_payment(db, user, 12000, "manual")
    subscription = mark_manual_payment_paid(
        db, payment, "MANUAL-1", period_days=30, grace_days=5, now=now - timedelta(days=30)
    )
    summary = run_payment_maintenance(db, {}, settings={"subscription": {"grace_days": 5}}, now=now)
    assert summary["grace"] == 1
    assert subscription.status == "grace"
    summary = run_payment_maintenance(
        db,
        {},
        settings={"subscription": {"grace_days": 5}},
        now=now + timedelta(days=5),
    )
    assert summary["downgraded"] == 1
    assert user.role == user.plan == "free"

    admin = User(
        email="admin@example.com",
        password_hash="hash",
        role="admin",
        plan="admin",
        status="active",
    )
    db.add(admin)
    db.flush()
    admin_payment = new_payment(db, admin, 12000, "manual")
    admin_subscription = mark_manual_payment_paid(
        db, admin_payment, "MANUAL-ADMIN", period_days=1, grace_days=0, now=now
    )
    run_payment_maintenance(
        db,
        {},
        settings={"subscription": {"grace_days": 0}},
        now=now + timedelta(days=2),
    )
    assert admin_subscription.status == "expired"
    assert admin.role == "admin"


def test_paychangu_checkout_and_verification_use_recorded_responses():
    requests = []
    fixtures = Path(__file__).parent / "fixtures" / "paychangu"
    checkout_response = json.loads((fixtures / "checkout_success.json").read_text())
    verify_response = json.loads((fixtures / "verify_success.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/payment":
            return httpx.Response(200, json=checkout_response)
        return httpx.Response(200, json=verify_response)

    provider = PayChanguProvider(
        secret_key="test-key",
        webhook_secret="test-webhook",
        client=httpx.Client(base_url="https://api.paychangu.com", transport=httpx.MockTransport(handler)),
    )
    checkout = provider.create_payment(
        amount_mwk=12000,
        email="payer@example.com",
        tx_ref="ACCA-1",
        callback_url="https://accalab.test/payments/paychangu/return",
        return_url="https://accalab.test/app/account",
    )
    assert checkout.checkout_url == "https://checkout.paychangu.com/pay/example-session"
    assert provider.verify_payment("ACCA-1")["tx_ref"] == "ACCA-1"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert json.loads(requests[0].content)["currency"] == "MWK"
    provider.close()


def test_paychangu_webhook_checks_hmac_over_raw_body_and_requires_reference():
    secret = "test-webhook-secret"
    provider = PayChanguProvider(secret_key="key", webhook_secret=secret)
    body = b'{"tx_ref":"ACCA-1","status":"success"}'
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert provider.handle_webhook(body, signature)["tx_ref"] == "ACCA-1"
    with pytest.raises(PaymentProviderError):
        provider.handle_webhook(body, "bad")
    provider.close()


def test_manual_marking_is_not_available_for_external_provider(session):
    db, user = session
    payment = new_payment(db, user, 12000, "paychangu")
    with pytest.raises(ValueError):
        mark_manual_payment_paid(db, payment, "REF-1", period_days=30, grace_days=5)
