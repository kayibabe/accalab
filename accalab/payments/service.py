"""Payment verification, subscription activation and expiry maintenance."""

from __future__ import annotations

import tomllib
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from accalab.config import ROOT
from accalab.models import Payment, Subscription, User, utc_now
from accalab.payments.providers import PaymentProvider, PaymentProviderError


class PaymentVerificationError(ValueError):
    """Verified payment data did not match the pending plan purchase."""


def payment_settings(path: Path | None = None) -> dict:
    source = path or ROOT / "config" / "payments.toml"
    return tomllib.loads(source.read_text(encoding="utf-8"))


def new_payment(session: Session, user: User, amount_mwk: int, provider: str) -> Payment:
    if amount_mwk <= 0:
        raise ValueError("Configure a positive Pro price before accepting payments.")
    payment = Payment(
        id=str(uuid.uuid4()),
        user_id=user.id,
        provider=provider,
        tx_ref=f"ACCA-{uuid.uuid4().hex}",
        amount_mwk=amount_mwk,
        currency="MWK",
        status="pending",
    )
    session.add(payment)
    session.flush()
    return payment


def apply_verified_payment(
    session: Session,
    payment: Payment,
    details: dict,
    *,
    period_days: int,
    grace_days: int,
    now: datetime | None = None,
) -> Subscription | None:
    now = now or utc_now()
    if details.get("tx_ref") != payment.tx_ref:
        raise PaymentVerificationError("Verified transaction reference does not match.")
    outcome = str(details.get("status", "")).casefold()
    if outcome in {"failed", "cancelled", "canceled"}:
        if payment.status == "pending":
            payment.status = "cancelled" if outcome in {"cancelled", "canceled"} else "failed"
            payment.updated_at = now
        return None
    if outcome not in {"success", "successful"}:
        return None
    if str(details.get("currency", "")).upper() != "MWK":
        raise PaymentVerificationError("Verified payment currency does not match MWK.")
    try:
        paid_amount = int(details["amount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PaymentVerificationError("Verified payment amount is invalid.") from exc
    if paid_amount < payment.amount_mwk:
        raise PaymentVerificationError("Verified payment amount is below the plan price.")

    existing = session.scalar(select(Subscription).where(Subscription.payment_id == payment.id))
    payment.status = "succeeded"
    payment.paid_amount_mwk = paid_amount
    payment.provider_reference = str(details.get("reference") or "") or None
    payment.paid_at = payment.paid_at or now
    payment.updated_at = now
    if existing is not None:
        return existing

    user = session.get(User, payment.user_id)
    if user is None:
        raise PaymentVerificationError("Payment account no longer exists.")
    starts = user.plan_end if user.plan == "pro" and user.plan_end and user.plan_end > now else now
    ends = starts + timedelta(days=max(1, period_days))
    grace_ends = ends + timedelta(days=max(0, grace_days))
    for previous in session.scalars(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status.in_(("active", "grace")),
        )
    ):
        previous.status = "superseded"
    subscription = Subscription(
        id=str(uuid.uuid4()),
        user_id=user.id,
        payment_id=payment.id,
        plan="pro",
        status="active",
        starts_at=starts,
        ends_at=ends,
        grace_ends_at=grace_ends,
        created_at=now,
    )
    session.add(subscription)
    user.plan = "pro"
    if user.role != "admin":
        user.role = "pro"
    user.plan_start = user.plan_start or starts
    user.plan_end = ends
    return subscription


def mark_manual_payment_paid(
    session: Session,
    payment: Payment,
    reference: str,
    *,
    period_days: int,
    grace_days: int,
    now: datetime | None = None,
) -> Subscription:
    reference = reference.strip()
    if payment.provider != "manual":
        raise PaymentVerificationError("Only manual payments can be marked paid here.")
    if not reference or len(reference) > 120:
        raise PaymentVerificationError("Enter a payment reference of 1 to 120 characters.")
    if payment.status == "succeeded":
        existing = session.scalar(select(Subscription).where(Subscription.payment_id == payment.id))
        if existing is None or payment.manual_reference != reference:
            raise PaymentVerificationError("This payment is already completed with another reference.")
        return existing
    if payment.status != "pending":
        raise PaymentVerificationError("Only pending manual payments can be marked paid.")
    payment.manual_reference = reference
    subscription = apply_verified_payment(
        session,
        payment,
        {
            "tx_ref": payment.tx_ref,
            "status": "success",
            "currency": "MWK",
            "amount": payment.amount_mwk,
            "reference": reference,
        },
        period_days=period_days,
        grace_days=grace_days,
        now=now,
    )
    if subscription is None:
        raise PaymentVerificationError("Manual payment could not be activated.")
    return subscription


def run_payment_maintenance(
    session: Session,
    providers: dict[str, PaymentProvider],
    *,
    settings: dict | None = None,
    now: datetime | None = None,
    verify: Callable[[PaymentProvider, str], dict | None] | None = None,
) -> dict:
    settings = settings or payment_settings()
    now = now or utc_now()
    subscription_config = settings.get("subscription", {})
    reconcile_after = timedelta(minutes=int(subscription_config.get("pending_reconcile_minutes", 30)))
    expire_after = timedelta(hours=int(subscription_config.get("pending_expire_hours", 24)))
    period_days = int(subscription_config.get("period_days", 30))
    grace_days = int(subscription_config.get("grace_days", 5))
    verify_payment = verify or (lambda provider, tx_ref: provider.verify_payment(tx_ref))
    report = {"verified": 0, "failed": 0, "expired_payments": 0, "grace": 0, "downgraded": 0, "errors": 0}

    pending = session.scalars(
        select(Payment).where(Payment.status == "pending", Payment.created_at <= now - reconcile_after)
    ).all()
    for payment in pending:
        provider = providers.get(payment.provider)
        details = None
        if provider is not None:
            try:
                details = verify_payment(provider, payment.tx_ref)
            except PaymentProviderError:
                report["errors"] += 1
                continue
        if details:
            previous_status = payment.status
            subscription = apply_verified_payment(
                session,
                payment,
                details,
                period_days=period_days,
                grace_days=grace_days,
                now=now,
            )
            if subscription is not None:
                report["verified"] += 1
            elif payment.status != previous_status:
                report["failed"] += 1
        if payment.status == "pending" and payment.created_at <= now - expire_after:
            payment.status = "expired"
            payment.updated_at = now
            report["expired_payments"] += 1

    subscriptions = session.scalars(
        select(Subscription).where(Subscription.status.in_(("active", "grace")))
    ).all()
    for subscription in subscriptions:
        user = session.get(User, subscription.user_id)
        if now >= subscription.grace_ends_at:
            subscription.status = "expired"
            if user is not None and user.role != "admin" and user.plan == "pro":
                if user.plan_end is None or user.plan_end + timedelta(days=grace_days) <= now:
                    user.role = "free"
                    user.plan = "free"
                    report["downgraded"] += 1
        elif now >= subscription.ends_at and subscription.status == "active":
            subscription.status = "grace"
            report["grace"] += 1
    return report
