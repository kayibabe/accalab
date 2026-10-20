"""Consent-led Telegram delivery with a persistent, idempotent outbox."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import smtplib
import tomllib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from accalab.config import ROOT
from accalab.models import MessageConsent, MessageOutbox, Ticket, User, utc_now

CHANNEL = "telegram"
EVENTS = {"ticket_published", "nightly_results", "expiry_reminder"}
RESPONSIBLE_GAMBLING_LINE = "18+ only. Gambling involves risk; never stake more than you can afford to lose."


class MessageDeliveryError(RuntimeError):
    """A channel rejected or could not deliver a message."""


class MessageChannel(Protocol):
    name: str

    def send(self, destination: str, text: str) -> str: ...


class TelegramChannel:
    name = CHANNEL

    def __init__(self, token: str | None = None, client: httpx.Client | None = None) -> None:
        self.token = (token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        if not self.token:
            raise MessageDeliveryError("Telegram bot token is not configured.")
        self.client = client or httpx.Client(timeout=15.0)
        self._owns_client = client is None

    def send(self, destination: str, text: str) -> str:
        try:
            response = self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": destination, "text": text},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MessageDeliveryError("Telegram delivery request failed.") from exc
        if not isinstance(payload, dict):
            raise MessageDeliveryError("Telegram returned an invalid response.")
        if not payload.get("ok") or not isinstance(payload.get("result"), dict):
            raise MessageDeliveryError("Telegram did not accept the message.")
        return str(payload["result"].get("message_id", ""))

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def messaging_settings(path: Path | None = None) -> dict:
    source = path or ROOT / "config" / "messaging.toml"
    if not source.is_file():
        return {"whatsapp": {"whatsapp_enabled": False, "policy_approved": False}}
    return tomllib.loads(source.read_text(encoding="utf-8"))


ONLINE_GAMBLING_COUNTRIES = {"AU", "JP", "CO", "MX", "PE"}
PHONE_PREFIX_COUNTRIES = {
    "+61": "AU",
    "+81": "JP",
    "+57": "CO",
    "+52": "MX",
    "+51": "PE",
    "+265": "MW",
}


def _phone_country(phone: str) -> str | None:
    for prefix in sorted(PHONE_PREFIX_COUNTRIES, key=len, reverse=True):
        if phone.startswith(prefix):
            return PHONE_PREFIX_COUNTRIES[prefix]
    return None


def whatsapp_country(phone: str) -> str | None:
    return _phone_country(normalize_whatsapp_phone(phone))


def normalize_whatsapp_phone(phone: str) -> str:
    compact = "".join(char for char in phone.strip() if char not in " -()")
    if not compact.startswith("+") or not compact[1:].isdigit() or not 8 <= len(compact[1:]) <= 15:
        raise ValueError("Enter a valid international phone number, including its country code.")
    return compact


def whatsapp_policy_gate(phone: str, *, config: dict | None = None) -> tuple[bool, str]:
    settings = (config or messaging_settings()).get("whatsapp", {})
    if not settings.get("whatsapp_enabled", False):
        return False, "WhatsApp alerts are disabled."
    if not settings.get("policy_approved", False):
        return False, "WhatsApp messaging has not passed the required policy approval."
    try:
        normalized = normalize_whatsapp_phone(phone)
    except ValueError as exc:
        return False, str(exc)
    country = _phone_country(normalized)
    allowed = set(settings.get("allowed_online_gambling_countries", [])) & ONLINE_GAMBLING_COUNTRIES
    if country not in allowed:
        return False, "WhatsApp messaging is not available for this country."
    return True, ""


def _whatsapp_template(
    event_type: str, *, destination: str, config: dict | None = None
) -> dict:
    settings = (config or messaging_settings()).get("whatsapp", {})
    template = settings.get("templates", {}).get(event_type)
    if not isinstance(template, dict):
        raise MessageDeliveryError(f"No WhatsApp template is configured for {event_type}.")
    if str(template.get("approval_status", "")).casefold() != "approved":
        raise MessageDeliveryError(f"WhatsApp template for {event_type} is not approved.")
    name = str(template.get("name", ""))
    category = str(template.get("category", "")).upper()
    language = str(template.get("language", "en"))
    if not name or category not in {"UTILITY", "AUTHENTICATION", "MARKETING"}:
        raise MessageDeliveryError("WhatsApp template configuration is invalid.")
    country = _phone_country(normalize_whatsapp_phone(destination))
    rates = settings.get("cost_estimates_usd", {}).get(country or "", {})
    try:
        rate = Decimal(str(rates.get(category, "")))
    except InvalidOperation as exc:
        raise MessageDeliveryError("WhatsApp estimated message cost is not configured.") from exc
    if rate <= 0:
        raise MessageDeliveryError("WhatsApp estimated message cost is not configured.")
    return {"name": name, "category": category, "language": language, "rate": rate}


def whatsapp_template_details(event_type: str, destination: str, *, config: dict | None = None) -> dict:
    return _whatsapp_template(event_type, destination=destination, config=config)


class WhatsAppChannel:
    """Approved-template sender for the WhatsApp Cloud API."""

    name = "whatsapp"

    def __init__(self, *, config: dict | None = None, client: httpx.Client | None = None) -> None:
        self.config = config or messaging_settings()
        whatsapp = self.config.get("whatsapp", {})
        if not whatsapp.get("whatsapp_enabled", False) or not whatsapp.get("policy_approved", False):
            raise MessageDeliveryError("WhatsApp is disabled or has not passed policy approval.")
        self.token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
        self.phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        self.api_version = os.getenv("META_GRAPH_API_VERSION", "").strip()
        if not (
            self.token
            and self.phone_number_id
            and re.fullmatch(r"v\d+\.\d+", self.api_version)
        ):
            raise MessageDeliveryError("WhatsApp Cloud API credentials are not configured.")
        self.client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def send_template(self, destination: str, event_type: str, text: str) -> str:
        permitted, reason = whatsapp_policy_gate(destination, config=self.config)
        if not permitted:
            raise MessageDeliveryError(reason)
        template = _whatsapp_template(event_type, destination=destination, config=self.config)
        footer = RESPONSIBLE_GAMBLING_LINE
        summary = text.removesuffix(f"\n\n{footer}").strip()[:160]
        parameters = [summary, footer]
        payload = {
            "messaging_product": "whatsapp",
            "to": normalize_whatsapp_phone(destination).removeprefix("+"),
            "type": "template",
            "template": {
                "name": template["name"],
                "language": {"code": template["language"]},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": value} for value in parameters],
                    }
                ],
            },
        }
        try:
            response = self.client.post(
                f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MessageDeliveryError("WhatsApp Cloud API request failed.") from exc
        messages = result.get("messages") if isinstance(result, dict) else None
        if not messages or not isinstance(messages[0], dict) or not messages[0].get("id"):
            raise MessageDeliveryError("WhatsApp did not accept the template message.")
        return str(messages[0]["id"])

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class EmailChannel:
    """Plain-text SMTP fallback; requires explicit per-account email consent."""

    name = "email"

    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "").strip()
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.username = os.getenv("SMTP_USERNAME", "").strip()
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.sender = os.getenv("SMTP_FROM", "").strip()
        self.use_ssl = os.getenv("SMTP_USE_SSL", "false").casefold() == "true"
        if not self.host or not self.sender:
            raise MessageDeliveryError("Email fallback is not configured.")

    def send(self, destination: str, text: str) -> str:
        message = EmailMessage()
        message["Subject"] = "AccaLab account update"
        message["From"] = self.sender
        message["To"] = destination
        message.set_content(text)
        try:
            smtp_class = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
            with smtp_class(self.host, self.port, timeout=20) as smtp:
                if not self.use_ssl:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise MessageDeliveryError("Email delivery failed.") from exc
        return ""


def create_whatsapp_code(
    session: Session,
    user: User,
    phone: str,
    *,
    age_confirmed: bool,
    tickets_enabled: bool,
    results_enabled: bool,
    expiry_enabled: bool,
    now: datetime | None = None,
) -> str:
    if user.role not in {"pro", "admin"}:
        raise PermissionError("WhatsApp alerts are available to Pro accounts.")
    phone = normalize_whatsapp_phone(phone)
    permitted, reason = whatsapp_policy_gate(phone)
    if not permitted:
        raise MessageDeliveryError(reason)
    if not age_confirmed:
        raise ValueError("WhatsApp alerts require confirmation that you are 18 or older.")
    if not any((tickets_enabled, results_enabled, expiry_enabled)):
        raise ValueError("Choose at least one alert type.")
    now = now or utc_now()
    code = f"{secrets.randbelow(1_000_000):06d}"
    consent = session.scalar(
        select(MessageConsent).where(
            MessageConsent.user_id == user.id, MessageConsent.channel == "whatsapp"
        )
    )
    if consent is not None and consent.link_token_hash and consent.updated_at > now - timedelta(minutes=1):
        raise ValueError("Wait one minute before requesting another WhatsApp verification code.")
    values = {
        "destination": phone,
        "consented_at": None,
        "stopped_at": None,
        "link_token_hash": _digest(code),
        "link_expires_at": now + timedelta(minutes=10),
        "tickets_enabled": tickets_enabled,
        "results_enabled": results_enabled,
        "expiry_enabled": expiry_enabled,
        "age_confirmed": True,
        "verification_attempts": 0,
        "updated_at": now,
    }
    if consent is None:
        consent = MessageConsent(user_id=user.id, channel="whatsapp", created_at=now, **values)
        session.add(consent)
    else:
        for key, value in values.items():
            setattr(consent, key, value)
    session.flush()
    return code


def complete_whatsapp_code(session: Session, user: User, code: str, *, now=None) -> bool:
    now = now or utc_now()
    consent = session.scalar(
        select(MessageConsent).where(
            MessageConsent.user_id == user.id,
            MessageConsent.channel == "whatsapp",
            MessageConsent.link_token_hash.is_not(None),
        )
    )
    if consent is None or not consent.destination or not consent.age_confirmed:
        return False
    if consent.verification_attempts >= 5:
        consent.link_token_hash = None
        consent.link_expires_at = None
        consent.updated_at = now
        session.flush()
        return False
    if not secrets.compare_digest(consent.link_token_hash, _digest(code.strip())):
        consent.verification_attempts += 1
        consent.updated_at = now
        if consent.verification_attempts >= 5:
            consent.link_token_hash = None
            consent.link_expires_at = None
        session.flush()
        return False
    if consent.link_expires_at is None or consent.link_expires_at <= now:
        consent.link_token_hash = None
        consent.link_expires_at = None
        consent.updated_at = now
        session.flush()
        return False
    consent.consented_at = now
    consent.link_token_hash = None
    consent.link_expires_at = None
    consent.verification_attempts = 0
    consent.updated_at = now
    session.flush()
    return True


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def email_unsubscribe_token(user_id: int, *, now: datetime | None = None) -> str | None:
    secret = os.getenv("SECRET_KEY", "").strip()
    if not secret:
        return None
    expires = int((now or utc_now()).timestamp()) + 10 * 365 * 24 * 60 * 60
    payload = f"{user_id}.{expires}"
    signature = hmac.new(secret.encode(), f"email-unsubscribe:{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_email_unsubscribe_token(token: str, *, now: datetime | None = None) -> int | None:
    secret = os.getenv("SECRET_KEY", "").strip()
    try:
        user_text, expiry_text, signature = token.split(".", 2)
        user_id, expires = int(user_text), int(expiry_text)
    except (ValueError, TypeError):
        return None
    if user_id <= 0 or expires <= int((now or utc_now()).timestamp()) or not secret:
        return None
    payload = f"{user_id}.{expires}"
    expected = hmac.new(secret.encode(), f"email-unsubscribe:{payload}".encode(), hashlib.sha256).hexdigest()
    return user_id if secrets.compare_digest(signature, expected) else None


def create_telegram_link(
    session: Session,
    user: User,
    *,
    tickets_enabled: bool = True,
    results_enabled: bool = True,
    expiry_enabled: bool = True,
    now: datetime | None = None,
) -> str:
    """Record explicit consent and return a single-use token for the bot deep link."""
    if user.role not in {"pro", "admin"}:
        raise PermissionError("Telegram alerts are available to Pro accounts.")
    now = now or utc_now()
    token = secrets.token_urlsafe(32)
    consent = session.scalar(
        select(MessageConsent).where(
            MessageConsent.user_id == user.id, MessageConsent.channel == CHANNEL
        )
    )
    values = {
        "consented_at": now,
        "stopped_at": None,
        "link_token_hash": _digest(token),
        "link_expires_at": now + timedelta(minutes=30),
        "tickets_enabled": tickets_enabled,
        "results_enabled": results_enabled,
        "expiry_enabled": expiry_enabled,
        "updated_at": now,
    }
    if consent is None:
        consent = MessageConsent(user_id=user.id, channel=CHANNEL, created_at=now, **values)
        session.add(consent)
    else:
        for key, value in values.items():
            setattr(consent, key, value)
    session.flush()
    return token


def complete_telegram_link(
    session: Session, token: str, chat_id: str, *, now: datetime | None = None
) -> bool:
    now = now or utc_now()
    consent = session.scalar(
        select(MessageConsent).where(MessageConsent.link_token_hash == _digest(token))
    )
    if (
        consent is None
        or consent.consented_at is None
        or consent.stopped_at is not None
        or consent.link_expires_at is None
        or consent.link_expires_at <= now
        or not chat_id
        or len(chat_id) > 160
    ):
        return False
    consent.destination = chat_id
    consent.link_token_hash = None
    consent.link_expires_at = None
    consent.updated_at = now
    session.flush()
    return True


def stop_channel(
    session: Session, channel: str, destination: str, *, now: datetime | None = None
) -> bool:
    now = now or utc_now()
    consent = session.scalar(
        select(MessageConsent).where(
            MessageConsent.channel == channel, MessageConsent.destination == destination
        )
    )
    if consent is None:
        return False
    consent.stopped_at = now
    consent.updated_at = now
    session.execute(
        update(MessageOutbox)
        .where(
            MessageOutbox.user_id == consent.user_id,
            MessageOutbox.channel == channel,
            MessageOutbox.status.in_(("pending", "retry", "sending")),
        )
        .values(status="cancelled", last_error="User sent STOP", updated_at=now)
    )
    return True


def telegram_command(session: Session, text: str, chat_id: str, *, now=None) -> str:
    command, _, argument = text.strip().partition(" ")
    command = command.split("@", 1)[0].casefold()
    if command == "/start" and argument:
        return "Telegram linked. You will receive the alerts you selected." if complete_telegram_link(
            session, argument.strip(), chat_id, now=now
        ) else "That link has expired or was already used. Start again from Account settings."
    if command in {"/stop", "stop"}:
        stop_channel(session, CHANNEL, chat_id, now=now)
        return "Alerts are stopped. You can reconnect from Account settings."
    return "Use /stop to stop AccaLab alerts."


def _message(text: str) -> str:
    suffix = f"\n\n{RESPONSIBLE_GAMBLING_LINE}"
    return text.strip()[: 4096 - len(suffix)] + suffix


def enqueue_event(
    session: Session,
    event_type: str,
    event_key: str,
    message: str,
    *,
    channel: str | None = None,
) -> int:
    if event_type not in EVENTS:
        raise ValueError(f"Unsupported message event: {event_type}")
    preference = {
        "ticket_published": MessageConsent.tickets_enabled,
        "nightly_results": MessageConsent.results_enabled,
        "expiry_reminder": MessageConsent.expiry_enabled,
    }[event_type]
    filters = [
            MessageConsent.destination.is_not(None),
            MessageConsent.consented_at.is_not(None),
            MessageConsent.stopped_at.is_(None),
            preference.is_(True),
            User.role.in_(("pro", "admin")),
            User.status == "active",
    ]
    if channel is not None:
        filters.append(MessageConsent.channel == channel)
    else:
        filters.append(MessageConsent.channel.in_(("telegram", "whatsapp", "email")))
    recipients = session.execute(
        select(MessageConsent.user_id, MessageConsent.channel)
        .join(User, User.id == MessageConsent.user_id)
        .where(*filters)
    ).all()
    queued = 0
    for user_id, recipient_channel in recipients:
        if recipient_channel == "whatsapp":
            permitted, _reason = whatsapp_policy_gate(
                session.scalar(
                    select(MessageConsent.destination).where(
                        MessageConsent.user_id == user_id,
                        MessageConsent.channel == "whatsapp",
                    )
                )
                or ""
            )
            consent_row = session.scalar(
                select(MessageConsent).where(
                    MessageConsent.user_id == user_id,
                    MessageConsent.channel == "whatsapp",
                )
            )
            if not permitted or consent_row is None or not consent_row.age_confirmed:
                continue
        key = f"{recipient_channel}:{event_type}:{event_key}:{user_id}"
        exists = session.scalar(
            select(MessageOutbox.id).where(MessageOutbox.idempotency_key == key)
        )
        if exists is not None:
            continue
        session.add(
            MessageOutbox(
                user_id=user_id,
                channel=recipient_channel,
                event_type=event_type,
                idempotency_key=key,
                message_text=_message(message),
                status="pending",
                attempts=0,
                next_attempt_at=utc_now(),
            )
        )
        queued += 1
    session.flush()
    return queued


def enqueue_ticket_published(session: Session, ticket_id: str) -> int:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None or ticket.status != "official":
        return 0
    message = (
        f"AccaLab official {ticket.band} ticket for {ticket.target_date.isoformat()} is published. "
        f"Combined odds: {float(ticket.combined_odds):.2f}. View it in Tickets."
    )
    return enqueue_event(session, "ticket_published", ticket.id, message)


def enqueue_nightly_results(
    session: Session,
    *,
    local_date: date | None = None,
    timezone_name: str = "Africa/Blantyre",
) -> int:
    local_date = local_date or datetime.now(ZoneInfo(timezone_name)).date()
    return enqueue_event(
        session,
        "nightly_results",
        local_date.isoformat(),
        "Nightly ticket results have been updated. Sign in to view the settled record.",
    )


def enqueue_expiry_reminders(
    session: Session, *, now: datetime | None = None, days_ahead: int = 3
) -> int:
    now = now or utc_now()
    users = session.scalars(
        select(User).where(
            User.role == "pro",
            User.status == "active",
            User.plan == "pro",
            User.plan_end.is_not(None),
            User.plan_end > now,
            User.plan_end <= now + timedelta(days=days_ahead),
        )
    ).all()
    queued = 0
    for user in users:
        queued += enqueue_event(
            session,
            "expiry_reminder",
            f"{user.id}:{user.plan_end.isoformat()}",
            f"Your Pro access ends on {user.plan_end.astimezone(ZoneInfo('Africa/Blantyre')):%Y-%m-%d %H:%M}.",
        )
    return queued


def dispatch_outbox(
    session: Session,
    channels: dict[str, MessageChannel],
    *,
    now: datetime | None = None,
    limit: int = 100,
    max_attempts: int = 8,
) -> dict[str, int]:
    """Deliver due messages and retain transient failures for bounded retry."""
    now = now or utc_now()
    report = {"sent": 0, "retried": 0, "dead_letter": 0, "skipped": 0}
    rows = session.scalars(
        select(MessageOutbox)
        .where(
            MessageOutbox.status.in_(("pending", "retry")),
            MessageOutbox.next_attempt_at <= now,
        )
        .order_by(MessageOutbox.id)
        .limit(max(1, limit))
    ).all()
    for row in rows:
        user = session.get(User, row.user_id)
        consent = session.scalar(
            select(MessageConsent).where(
                MessageConsent.user_id == row.user_id, MessageConsent.channel == row.channel
            )
        )
        if (
            user is None
            or user.status != "active"
            or user.role not in {"pro", "admin"}
            or consent is None
            or consent.stopped_at is not None
            or consent.consented_at is None
            or not consent.destination
        ):
            row.status = "cancelled"
            row.last_error = "Recipient is no longer eligible or consented."
            row.updated_at = now
            report["skipped"] += 1
            continue
        channel = channels.get(row.channel)
        if channel is None:
            report["skipped"] += 1
            continue
        template = None
        if row.channel == "whatsapp":
            permitted, _reason = whatsapp_policy_gate(consent.destination)
            if not permitted or not consent.age_confirmed:
                row.status = "cancelled"
                row.last_error = "WhatsApp country, age, or policy gate no longer passes."
                row.updated_at = now
                report["skipped"] += 1
                _enqueue_fallback(session, row, channels)
                continue
            try:
                template = _whatsapp_template(row.event_type, destination=consent.destination)
            except MessageDeliveryError as exc:
                row.status = "cancelled"
                row.last_error = str(exc)[:500]
                row.updated_at = now
                report["skipped"] += 1
                _enqueue_fallback(session, row, channels)
                continue
            row.template_name = template["name"]
            row.template_category = template["category"]
        row.status = "sending"
        row.attempts += 1
        row.updated_at = now
        session.flush()
        try:
            if row.channel == "whatsapp":
                row.provider_message_id = channel.send_template(
                    consent.destination, row.event_type, row.message_text
                )
                row.whatsapp_status = "sent"
                row.estimated_cost_usd = template["rate"]
            else:
                message_text = row.message_text
                if row.channel == "email":
                    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
                    token = email_unsubscribe_token(user.id)
                    if base_url.startswith("https://") and token:
                        message_text += f"\n\nStop email alerts: {base_url}/messaging/email/unsubscribe?token={token}"
                    else:
                        message_text += "\n\nTo stop email alerts, open Account and choose Notifications."
                row.provider_message_id = channel.send(consent.destination, message_text)
            row.status = "sent"
            row.sent_at = now
            row.last_error = None
            report["sent"] += 1
        except Exception as exc:  # channel adapters normalize provider errors
            row.last_error = str(exc)[:500]
            if row.attempts >= max_attempts:
                row.status = "dead_letter"
                report["dead_letter"] += 1
                if row.channel == "whatsapp":
                    _enqueue_fallback(session, row, channels)
            else:
                row.status = "retry"
                row.next_attempt_at = now + timedelta(minutes=min(360, 2 ** row.attempts))
                report["retried"] += 1
        row.updated_at = now
    session.flush()
    return report


def _enqueue_fallback(
    session: Session, original: MessageOutbox, channels: dict[str, MessageChannel]
) -> bool:
    if original.event_type not in EVENTS:
        return False
    settings = messaging_settings().get("whatsapp", {})
    fallbacks = settings.get("fallback_channels", ["telegram", "email"])
    for fallback in fallbacks:
        if fallback == "whatsapp" or fallback not in channels:
            continue
        consent = session.scalar(
            select(MessageConsent).where(
                MessageConsent.user_id == original.user_id,
                MessageConsent.channel == fallback,
                MessageConsent.destination.is_not(None),
                MessageConsent.consented_at.is_not(None),
                MessageConsent.stopped_at.is_(None),
            )
        )
        if consent is None:
            continue
        preference = {
            "ticket_published": consent.tickets_enabled,
            "nightly_results": consent.results_enabled,
            "expiry_reminder": consent.expiry_enabled,
        }[original.event_type]
        if not preference:
            continue
        if fallback == "email":
            user = session.get(User, original.user_id)
            if user is None or consent.destination.casefold() != user.email.casefold():
                continue
        key = f"fallback:{original.idempotency_key}:{fallback}"
        exists = session.scalar(
            select(MessageOutbox.id).where(MessageOutbox.idempotency_key == key)
        )
        if exists is not None:
            return True
        session.add(
            MessageOutbox(
                user_id=original.user_id,
                channel=fallback,
                event_type=original.event_type,
                idempotency_key=key,
                message_text=original.message_text,
                status="pending",
                attempts=0,
                next_attempt_at=utc_now(),
            )
        )
        session.flush()
        return True
    return False


def build_channels() -> dict[str, MessageChannel]:
    channels: dict[str, MessageChannel] = {}
    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        channels[CHANNEL] = TelegramChannel()
    whatsapp = messaging_settings().get("whatsapp", {})
    if (
        whatsapp.get("whatsapp_enabled", False)
        and whatsapp.get("policy_approved", False)
        and os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
        and os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        and os.getenv("META_GRAPH_API_VERSION", "").strip()
    ):
        channels["whatsapp"] = WhatsAppChannel()
    if os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip():
        channels["email"] = EmailChannel()
    return channels


def verify_telegram_webhook(secret_header: str, expected_secret: str) -> bool:
    return bool(expected_secret) and secrets.compare_digest(secret_header, expected_secret)


def verify_whatsapp_signature(raw_body: bytes, signature: str, app_secret: str) -> bool:
    if not app_secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return secrets.compare_digest(signature, expected)


def record_whatsapp_statuses(
    session: Session,
    payload: dict,
    channels: dict[str, MessageChannel],
    *,
    now: datetime | None = None,
) -> int:
    """Apply Cloud API delivery callbacks idempotently and queue allowed fallbacks."""
    now = now or utc_now()
    handled = 0
    entries = payload.get("entry", []) if isinstance(payload, dict) else []
    for entry in entries if isinstance(entries, list) else []:
        changes = entry.get("changes", []) if isinstance(entry, dict) else []
        for change in changes if isinstance(changes, list) else []:
            value = change.get("value", {}) if isinstance(change, dict) else {}
            incoming = value.get("messages", []) if isinstance(value, dict) else []
            for incoming_message in incoming if isinstance(incoming, list) else []:
                if not isinstance(incoming_message, dict):
                    continue
                message = incoming_message.get("text", {})
                body = message.get("body", "") if isinstance(message, dict) else ""
                sender = str(incoming_message.get("from", ""))
                if body.strip().casefold() in {"stop", "/stop", "unsubscribe", "cancel"} and sender.isdigit():
                    stop_channel(session, "whatsapp", f"+{sender}", now=now)
                    handled += 1
            statuses = value.get("statuses", []) if isinstance(value, dict) else []
            for status in statuses if isinstance(statuses, list) else []:
                if not isinstance(status, dict):
                    continue
                message_id = str(status.get("id", ""))
                delivery_status = str(status.get("status", "")).casefold()
                if not message_id or delivery_status not in {"sent", "delivered", "read", "failed"}:
                    continue
                row = session.scalar(
                    select(MessageOutbox).where(
                        MessageOutbox.channel == "whatsapp",
                        MessageOutbox.provider_message_id == message_id,
                    )
                )
                if row is None:
                    continue
                if row.delivered_at is not None and delivery_status == "failed":
                    continue
                if delivery_status in {"delivered", "read"}:
                    row.whatsapp_status = delivery_status
                    row.delivered_at = row.delivered_at or now
                elif delivery_status == "failed":
                    row.whatsapp_status = "failed"
                    row.status = "dead_letter"
                    error_data = status.get("errors", [])
                    row.last_error = "WhatsApp reported delivery failure."
                    if error_data and isinstance(error_data, list) and isinstance(error_data[0], dict):
                        row.last_error += f" Code {str(error_data[0].get('code', 'unknown'))[:40]}."
                    _enqueue_fallback(session, row, channels)
                elif row.whatsapp_status not in {"delivered", "read"}:
                    row.whatsapp_status = "sent"
                row.updated_at = now
                handled += 1
    session.flush()
    return handled
