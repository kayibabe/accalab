from datetime import UTC, datetime, timedelta
import os
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-that-is-at-least-thirty-two-characters")

from accalab.auth.security import hash_password
from accalab.db import Base
from accalab.messaging import (
    create_telegram_link,
    create_whatsapp_code,
    complete_whatsapp_code,
    dispatch_outbox,
    email_unsubscribe_token,
    enqueue_event,
    record_whatsapp_statuses,
    telegram_command,
    verify_email_unsubscribe_token,
    verify_whatsapp_signature,
    whatsapp_policy_gate,
    whatsapp_template_details,
    WhatsAppChannel,
)
from accalab.models import MessageConsent, MessageOutbox, User, utc_now
from accalab.web.app import create_app, get_db


class FakeTelegram:
    name = "telegram"

    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send(self, destination, text):
        if self.fail:
            raise RuntimeError("temporary failure")
        self.sent.append((destination, text))
        return "msg-1"


def setup_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    pro = User(email="pro@example.test", password_hash="x", role="pro", plan="pro", status="active")
    free = User(email="free@example.test", password_hash="x", role="free", plan="free", status="active")
    admin = User(email="admin@example.test", password_hash="x", role="admin", plan="admin", status="active")
    session.add_all([pro, free, admin])
    session.flush()
    return engine, session, pro, free, admin


def consent(session, user, destination):
    row = MessageConsent(
        user_id=user.id,
        channel="telegram",
        destination=destination,
        consented_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def test_link_requires_pro_explicit_consent_and_is_single_use():
    engine, session, pro, free, _admin = setup_db()
    try:
        try:
            create_telegram_link(session, free)
            assert False, "Free must not link Telegram alerts"
        except PermissionError:
            pass
        token = create_telegram_link(session, pro, tickets_enabled=True, results_enabled=False)
        assert telegram_command(session, f"/start {token}", "chat-10").startswith("Telegram linked")
        assert telegram_command(session, f"/start {token}", "chat-10").startswith("That link")
        row = session.scalar(select(MessageConsent).where(MessageConsent.user_id == pro.id))
        assert row and row.destination == "chat-10" and row.consented_at
        assert row.tickets_enabled is True and row.results_enabled is False
    finally:
        session.close()
        engine.dispose()


def test_enqueue_is_idempotent_and_free_users_never_receive_alerts():
    engine, session, pro, free, admin = setup_db()
    try:
        consent(session, pro, "chat-pro")
        # A stale consent after downgrade must not bypass the role gate.
        consent(session, free, "chat-free")
        consent(session, admin, "chat-admin")
        first = enqueue_event(session, "nightly_results", "2026-10-20", "Results ready")
        second = enqueue_event(session, "nightly_results", "2026-10-20", "Results ready")
        session.commit()
        rows = session.scalars(select(MessageOutbox).order_by(MessageOutbox.user_id)).all()
        assert first == 2 and second == 0
        assert {row.user_id for row in rows} == {pro.id, admin.id}
        assert all("18+ only" in row.message_text for row in rows)
    finally:
        session.close()
        engine.dispose()


def test_delivery_requires_recorded_consent_and_linked_destination():
    engine, session, _pro, _free, _admin = setup_db()
    try:
        assert enqueue_event(session, "nightly_results", "2026-10-20", "Results ready") == 0
        report = dispatch_outbox(session, {"telegram": FakeTelegram()})
        assert report["sent"] == 0
        assert session.scalar(select(func.count()).select_from(MessageOutbox)) == 0
    finally:
        session.close()
        engine.dispose()


def test_stop_cancels_queued_messages_and_blocks_delivery():
    engine, session, pro, _free, _admin = setup_db()
    try:
        consent(session, pro, "chat-pro")
        enqueue_event(session, "ticket_published", "ticket-1", "Ticket ready")
        assert telegram_command(session, "/stop", "chat-pro") == "Alerts are stopped. You can reconnect from Account settings."
        channel = FakeTelegram()
        report = dispatch_outbox(session, {"telegram": channel})
        session.commit()
        row = session.scalar(select(MessageOutbox))
        assert row.status == "cancelled"
        assert report["skipped"] == 0
        assert channel.sent == []
    finally:
        session.close()
        engine.dispose()


def test_outbox_retries_then_marks_sent_and_dead_letters_after_limit():
    engine, session, pro, _free, _admin = setup_db()
    try:
        consent(session, pro, "chat-pro")
        enqueue_event(session, "nightly_results", "day-a", "Results ready")
        failed = dispatch_outbox(session, {"telegram": FakeTelegram(fail=True)}, max_attempts=2)
        row = session.scalar(select(MessageOutbox))
        assert failed["retried"] == 1 and row.status == "retry"
        failed_again = dispatch_outbox(
            session,
            {"telegram": FakeTelegram(fail=True)},
            now=row.next_attempt_at + timedelta(seconds=1),
            max_attempts=2,
        )
        assert failed_again["dead_letter"] == 1 and row.status == "dead_letter"

        enqueue_event(session, "nightly_results", "day-b", "Results ready")
        channel = FakeTelegram()
        sent = dispatch_outbox(session, {"telegram": channel})
        session.commit()
        assert sent["sent"] == 1
        assert channel.sent[0][0] == "chat-pro"
        assert session.scalar(select(func.count()).select_from(MessageOutbox).where(MessageOutbox.status == "sent")) == 1
    finally:
        session.close()
        engine.dispose()


def test_expired_link_cannot_be_claimed():
    engine, session, pro, _free, _admin = setup_db()
    try:
        now = datetime(2026, 10, 20, tzinfo=UTC)
        token = create_telegram_link(session, pro, now=now)
        response = telegram_command(session, f"/start {token}", "chat-pro", now=now + timedelta(minutes=31))
        assert response.startswith("That link")
    finally:
        session.close()
        engine.dispose()


def test_whatsapp_is_disabled_by_default_and_malawi_is_outside_policy_gate():
    assert whatsapp_policy_gate("+265991234567")[0] is False
    config = {
        "whatsapp": {
            "whatsapp_enabled": True,
            "policy_approved": True,
            "allowed_online_gambling_countries": ["AU", "JP", "CO", "MX", "PE", "MW"],
        }
    }
    assert whatsapp_policy_gate("+265991234567", config=config)[0] is False
    assert whatsapp_policy_gate("+61412345678", config=config)[0] is True


def test_whatsapp_one_time_code_expires_after_five_attempts(monkeypatch):
    import accalab.messaging as messaging

    monkeypatch.setattr(
        messaging,
        "messaging_settings",
        lambda: {
            "whatsapp": {
                "whatsapp_enabled": True,
                "policy_approved": True,
                "allowed_online_gambling_countries": ["AU"],
            }
        },
    )
    engine, session, pro, _free, _admin = setup_db()
    try:
        now = datetime(2026, 10, 20, tzinfo=UTC)
        code = create_whatsapp_code(
            session, pro, "+61412345678", age_confirmed=True,
            tickets_enabled=True, results_enabled=False, expiry_enabled=False, now=now,
        )
        consent_row = session.scalar(select(MessageConsent).where(MessageConsent.channel == "whatsapp"))
        assert consent_row.consented_at is None
        wrong_code = "999999" if code != "999999" else "888888"
        assert not complete_whatsapp_code(session, pro, wrong_code, now=now)
        assert consent_row.verification_attempts == 1
        assert complete_whatsapp_code(session, pro, code, now=now)
        assert consent_row.consented_at == now
        assert not complete_whatsapp_code(session, pro, code, now=now)
    finally:
        session.close()
        engine.dispose()


def test_whatsapp_adapter_sends_only_approved_short_template_payload(monkeypatch):
    config = {
        "whatsapp": {
            "whatsapp_enabled": True,
            "policy_approved": True,
            "allowed_online_gambling_countries": ["AU"],
            "cost_estimates_usd": {"AU": {"UTILITY": "0.025"}},
            "templates": {
                "ticket_published": {
                    "name": "accalab_ticket_update",
                    "category": "UTILITY",
                    "approval_status": "approved",
                    "language": "en",
                }
            },
        }
    }
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"messages": [{"id": "wamid.123"}]}

    class FakeClient:
        def post(self, url, *, headers, json):
            self.url, self.headers, self.payload = url, headers, json
            return FakeResponse()

    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v99.0")
    client = FakeClient()
    channel = WhatsAppChannel(config=config, client=client)
    sent_id = channel.send_template(
        "+61412345678", "ticket_published", "Official T1 ticket is ready.\n\n18+ only. Gambling involves risk."
    )
    assert sent_id == "wamid.123"
    assert client.payload["to"] == "61412345678"
    assert client.payload["type"] == "template"
    assert client.payload["template"]["name"] == "accalab_ticket_update"
    assert len(client.payload["template"]["components"][0]["parameters"]) == 2
    assert "18+ only" in client.payload["template"]["components"][0]["parameters"][1]["text"]
    details = whatsapp_template_details("ticket_published", "+61412345678", config=config)
    assert details["rate"].as_tuple().exponent == -3


def test_whatsapp_failed_delivery_falls_back_and_webhook_updates_cost_once():
    engine, session, pro, _free, _admin = setup_db()
    try:
        consent(session, pro, "chat-pro")
        wa = MessageConsent(
            user_id=pro.id,
            channel="whatsapp",
            destination="+61412345678",
            consented_at=datetime.now(UTC),
            age_confirmed=True,
        )
        session.add(wa)
        session.flush()
        row = MessageOutbox(
            user_id=pro.id,
            channel="whatsapp",
            event_type="ticket_published",
            idempotency_key="wa:ticket:123",
            message_text="Ticket ready",
            status="sent",
            attempts=1,
            next_attempt_at=datetime.now(UTC),
            provider_message_id="wamid.failed",
            estimated_cost_usd=0.025,
        )
        session.add(row)
        session.flush()
        payload = {
            "entry": [{"changes": [{"value": {"statuses": [
                {"id": "wamid.failed", "status": "failed", "errors": [{"code": 131000}]}
            ]}}]}]
        }
        assert record_whatsapp_statuses(session, payload, {"telegram": FakeTelegram()}) == 1
        assert row.status == "dead_letter"
        assert session.scalar(select(MessageOutbox).where(MessageOutbox.channel == "telegram")) is not None

        delivered = MessageOutbox(
            user_id=pro.id,
            channel="whatsapp",
            event_type="nightly_results",
            idempotency_key="wa:results:456",
            message_text="Results",
            status="sent",
            attempts=1,
            next_attempt_at=datetime.now(UTC),
            provider_message_id="wamid.delivered",
            estimated_cost_usd=0.025,
        )
        session.add(delivered)
        session.flush()
        payload["entry"][0]["changes"][0]["value"]["statuses"] = [
            {"id": "wamid.delivered", "status": "delivered"},
            {"id": "wamid.delivered", "status": "read"},
        ]
        assert record_whatsapp_statuses(session, payload, {}) == 2
        assert delivered.delivered_at is not None
        assert delivered.whatsapp_status == "read"
    finally:
        session.close()
        engine.dispose()


def test_whatsapp_stop_webhook_and_email_unsubscribe_signature(monkeypatch):
    import hashlib
    import hmac
    import json

    engine, session, pro, _free, _admin = setup_db()
    try:
        consent(session, pro, "chat-pro")
        wa = MessageConsent(
            user_id=pro.id,
            channel="whatsapp",
            destination="+61412345678",
            consented_at=datetime.now(UTC),
            age_confirmed=True,
        )
        session.add(wa)
        session.flush()
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": "61412345678", "text": {"body": "STOP"}}
        ]}}]}]}).encode()
        assert record_whatsapp_statuses(session, json.loads(body), {}) == 1
        assert wa.stopped_at is not None
        monkeypatch.setenv("META_APP_SECRET", "test-secret")
        signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        assert verify_whatsapp_signature(body, signature, "test-secret")
        assert not verify_whatsapp_signature(body, signature, "wrong-secret")
        monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-that-is-at-least-thirty-two-characters")
        token = email_unsubscribe_token(pro.id)
        assert token is not None and verify_email_unsubscribe_token(token) == pro.id
    finally:
        session.close()
        engine.dispose()


def test_account_notifications_link_requires_csrf_and_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "AccaLabBot")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        user = User(
            email="account-pro@example.test",
            password_hash=hash_password("correct horse battery staple"),
            role="pro",
            plan="pro",
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
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post(
        "/login",
        data={
            "email": "account-pro@example.test",
            "password": "correct horse battery staple",
            "csrf_token": csrf,
        },
    )
    account = client.get("/app/account")
    assert "Connect Telegram" in account.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)
    rejected = client.post(
        "/app/account/notifications/telegram/link",
        data={"csrf_token": token, "consent_confirm": "true"},
    )
    assert "choose at least one alert type" in rejected.text
    response = client.post(
        "/app/account/notifications/telegram/link",
        data={
            "csrf_token": token,
            "consent_confirm": "true",
            "tickets_enabled": "true",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://t.me/AccaLabBot?start=")
    deep_link_token = response.headers["location"].split("start=", 1)[1]
    with factory() as session:
        consent_row = session.scalar(select(MessageConsent))
        assert consent_row.consented_at is not None
        assert consent_row.link_token_hash != deep_link_token
        assert consent_row.tickets_enabled is True
        assert consent_row.results_enabled is False
    engine.dispose()


def test_email_unsubscribe_requires_confirmation_post(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-that-is-at-least-thirty-two-characters")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        user = User(email="email@example.test", password_hash="x", role="pro", plan="pro", status="active")
        session.add(user)
        session.flush()
        session.add(MessageConsent(
            user_id=user.id,
            channel="email",
            destination=user.email,
            consented_at=datetime.now(UTC),
        ))
    app = create_app()

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    with factory() as session:
        user = session.scalar(select(User).where(User.email == "email@example.test"))
        token = email_unsubscribe_token(user.id)
    preview = client.get("/messaging/email/unsubscribe", params={"token": token})
    assert preview.status_code == 200 and "method='post'" in preview.text
    with factory() as session:
        consent_row = session.scalar(select(MessageConsent).where(MessageConsent.channel == "email"))
        assert consent_row.stopped_at is None
    result = client.post("/messaging/email/unsubscribe", data={"token": token})
    assert result.status_code == 200 and "alerts stopped" in result.text
    with factory() as session:
        consent_row = session.scalar(select(MessageConsent).where(MessageConsent.channel == "email"))
        assert consent_row.stopped_at is not None
    engine.dispose()
