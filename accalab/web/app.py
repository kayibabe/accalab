"""Role-aware AccaLab application shell."""

from __future__ import annotations

import os
import re
import secrets
import hashlib
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from accalab.auth.security import (
    clear_login_failures,
    consume_auth_token,
    create_auth_token,
    hash_password,
    login_is_locked,
    normalize_email,
    record_login_failure,
    verify_password,
)
from accalab.config import ROOT, _load_dotenv
from accalab.db import make_engine, make_session_factory
from accalab.entitlements import (
    ADMIN_SECTIONS,
    SHARED_SECTIONS,
    free_page_note,
    sidebar_for,
)
from accalab.models import AuditLog, Payment, User, WaitlistEntry
from accalab.models import MessageConsent, utc_now
from accalab.messaging import (
    MessageDeliveryError,
    MessageOutbox,
    TelegramChannel,
    WhatsAppChannel,
    build_channels,
    complete_whatsapp_code,
    create_whatsapp_code,
    create_telegram_link,
    messaging_settings,
    record_whatsapp_statuses,
    telegram_command,
    verify_telegram_webhook,
    verify_whatsapp_signature,
    verify_email_unsubscribe_token,
    whatsapp_template_details,
)
from accalab.payments.providers import (
    PaymentProviderError,
    PayChanguProvider,
    build_payment_provider,
)
from accalab.payments.service import (
    PaymentVerificationError,
    apply_verified_payment,
    mark_manual_payment_paid,
    new_payment,
    payment_settings,
)
from accalab.services.entitlements import AccessDenied, require_entitlement
from accalab.web.admin import build_admin_page_context
from accalab.web.pages import build_page_context
from accalab.web.public import (
    PUBLIC_PAGES,
    build_plan_page_context,
    build_landing_context,
    build_record_context,
    load_site_content,
    save_site_content,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
engine = make_engine()
SessionFactory = make_session_factory(engine)


def get_db():
    with SessionFactory() as session:
        yield session


def _csrf(request: Request) -> str:
    value = request.session.get("csrf_token")
    if not value:
        value = secrets.token_urlsafe(24)
        request.session["csrf_token"] = value
    return value


def _check_csrf(request: Request, supplied: str) -> None:
    expected = request.session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid form token")


def _render(
    request: Request,
    template: str,
    *,
    user: User | None = None,
    status_code: int = 200,
    **context,
):
    return templates.TemplateResponse(
        request=request,
        name=template,
        status_code=status_code,
        context={
            "user": user,
            "csrf_token": _csrf(request),
            "sidebar": sidebar_for(user.role) if user else (),
            "request": request,
            **context,
        },
    )


def create_app() -> FastAPI:
    _load_dotenv(ROOT / ".env")
    secret_key = os.getenv("SECRET_KEY", "").strip()
    if len(secret_key) < 32:
        raise RuntimeError("Set SECRET_KEY to a random value of at least 32 characters.")

    app = FastAPI(title="AccaLab", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="accalab_session",
        max_age=60 * 60 * 12,
        same_site="lax",
        https_only=os.getenv("COOKIE_SECURE", "false").casefold() == "true",
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/health", response_class=HTMLResponse)
    def health():
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def public_home(request: Request, session: Session = Depends(get_db)):
        context = build_landing_context(session)
        return _render(
            request,
            "landing.html",
            title="Football analysis and tracking",
            **context,
        )

    @app.get("/pricing", response_class=HTMLResponse)
    def public_pricing(request: Request):
        context = build_plan_page_context()
        return _render(request, "pricing.html", title="Pricing", **context)

    @app.get("/record", response_class=HTMLResponse)
    def public_record(request: Request, session: Session = Depends(get_db)):
        context = build_record_context(session)
        return _render(
            request,
            "record.html",
            title="Live record",
            registration_open=load_site_content()["registration_open"],
            **context,
        )

    @app.get("/how-it-works", response_class=HTMLResponse)
    @app.get("/responsible-gambling", response_class=HTMLResponse)
    @app.get("/terms", response_class=HTMLResponse)
    @app.get("/privacy", response_class=HTMLResponse)
    def public_information_page(request: Request):
        page = PUBLIC_PAGES[request.url.path]
        return _render(
            request,
            "public_page.html",
            title=page["title"],
            page=page,
            registration_open=load_site_content()["registration_open"],
        )

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request):
        return _render(
            request,
            "register.html",
            title="Join the waitlist",
            registration_open=load_site_content()["registration_open"],
            success=None,
            error=None,
        )

    @app.post("/register", response_class=HTMLResponse)
    def register_waitlist(
        request: Request,
        email: str = Form(),
        consent: str = Form(default=""),
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        registration_open = load_site_content()["registration_open"]
        if registration_open:
            return _render(
                request,
                "register.html",
                title="Create an account",
                registration_open=True,
                success=None,
                error="Account setup is not available yet. Please contact support.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        normalized = normalize_email(email)
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized) or len(normalized) > 320:
            return _render(
                request,
                "register.html",
                title="Join the waitlist",
                registration_open=False,
                success=None,
                error="Enter a valid email address.",
            )
        if consent != "yes":
            return _render(
                request,
                "register.html",
                title="Join the waitlist",
                registration_open=False,
                success=None,
                error="Please agree to receive an email about AccaLab access.",
            )
        existing = session.scalar(select(WaitlistEntry).where(WaitlistEntry.email == normalized))
        if existing is None:
            session.add(WaitlistEntry(email=normalized))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
        return _render(
            request,
            "register.html",
            title="Join the waitlist",
            registration_open=False,
            success="Your interest has been recorded. We will contact you about AccaLab access.",
            error=None,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return _render(request, "login.html", title="Sign in", error=None)

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        email: str = Form(),
        password: str = Form(),
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        email = normalize_email(email)
        if login_is_locked(session, email):
            session.commit()
            return _render(
                request, "login.html", title="Sign in", error="Sign-in is temporarily unavailable."
            )
        user = session.scalar(select(User).where(User.email == email))
        valid = user is not None and verify_password(user.password_hash, password)
        if not valid or user is None or user.status != "active" or user.email_verified_at is None:
            record_login_failure(session, email)
            session.commit()
            return _render(
                request,
                "login.html",
                title="Sign in",
                error="Email or password could not be verified.",
            )
        clear_login_failures(session, email)
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["session_version"] = user.session_version
        request.session["csrf_token"] = secrets.token_urlsafe(24)
        session.add(
            AuditLog(
                actor_user_id=user.id, action="login", target_type="user", target_id=str(user.id)
            )
        )
        session.commit()
        return RedirectResponse("/app/today", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form()):
        _check_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/auth/verify/request")
    def request_verification(
        request: Request,
        email: str = Form(),
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = session.scalar(select(User).where(User.email == normalize_email(email)))
        if user is not None and user.email_verified_at is None:
            create_auth_token(session, user, "verify_email")
            session.commit()
        return Response(
            "If the account needs verification, the request was recorded.", media_type="text/plain"
        )

    @app.get("/auth/verify/{token}")
    def verify_email(token: str, session: Session = Depends(get_db)):
        user = consume_auth_token(session, token, "verify_email")
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Token is invalid or expired"
            )
        from accalab.models import utc_now

        user.email_verified_at = utc_now()
        session.commit()
        return RedirectResponse("/login?verified=1", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/auth/password-reset/request")
    def request_password_reset(
        request: Request,
        email: str = Form(),
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = session.scalar(select(User).where(User.email == normalize_email(email)))
        if user is not None:
            create_auth_token(session, user, "password_reset")
            session.commit()
        return Response(
            "If the account exists, the reset request was recorded.", media_type="text/plain"
        )

    @app.post("/auth/password-reset/{token}")
    def reset_password(
        token: str,
        request: Request,
        password: str = Form(),
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = consume_auth_token(session, token, "password_reset")
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Token is invalid or expired"
            )
        user.password_hash = hash_password(password)
        user.status = "active"
        user.session_version += 1
        request.session.clear()
        session.commit()
        return RedirectResponse("/login?reset=1", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/app/{section}", response_class=HTMLResponse)
    def app_section(section: str, request: Request, session: Session = Depends(get_db)):
        match = next(
            (item for item in SHARED_SECTIONS if item.path.rsplit("/", 1)[1] == section), None
        )
        if match is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        user = _load_user(request, session)
        try:
            require_entitlement(user.role, match.entitlement)
        except AccessDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
        if section == "account":
            return _account_page(request, session, user)
        if section in {"today", "fixtures", "picks", "tickets", "results", "performance"}:
            context = build_page_context(
                session,
                user,
                section,
                requested_date=request.query_params.get("date"),
                requested_page=request.query_params.get("page"),
            )
            session.commit()
            return _render(
                request,
                "tracking.html",
                user=user,
                title=match.label,
                section=section,
                **context,
            )
        page_note = free_page_note(section) if user.role == "free" else None
        return _render(
            request, "placeholder.html", user=user, title=match.label, page_note=page_note
        )

    @app.post("/app/account/notifications/telegram/link")
    def link_telegram(
        request: Request,
        csrf_token: str = Form(),
        consent_confirm: bool = Form(default=False),
        tickets_enabled: bool = Form(default=False),
        results_enabled: bool = Form(default=False),
        expiry_enabled: bool = Form(default=False),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        if user.role not in {"pro", "admin"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        if not consent_confirm or not any((tickets_enabled, results_enabled, expiry_enabled)):
            return _account_page(
                request,
                session,
                user,
                notification_error="Confirm consent and choose at least one alert type.",
            )
        username = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().removeprefix("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return _account_page(
                request, session, user, notification_error="Telegram bot is not configured."
            )
        try:
            token = create_telegram_link(
                session,
                user,
                tickets_enabled=tickets_enabled,
                results_enabled=results_enabled,
                expiry_enabled=expiry_enabled,
            )
            session.commit()
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
        return RedirectResponse(
            f"https://t.me/{username}?start={quote(token)}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/app/account/notifications/telegram/stop")
    def unlink_telegram(
        request: Request,
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        consent = session.scalar(
            select(MessageConsent).where(
                MessageConsent.user_id == user.id, MessageConsent.channel == "telegram"
            )
        )
        if consent is not None:
            from accalab.messaging import stop_channel

            if consent.destination:
                stop_channel(session, "telegram", consent.destination)
            else:
                consent.stopped_at = utc_now()
                consent.updated_at = consent.stopped_at
            session.commit()
        return RedirectResponse("/app/account?notifications=stopped", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/messaging/telegram/webhook")
    async def telegram_webhook(request: Request, session: Session = Depends(get_db)):
        expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not verify_telegram_webhook(supplied, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
        message = payload.get("message", {}) if isinstance(payload, dict) else {}
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        text = message.get("text", "") if isinstance(message, dict) else ""
        chat_id = str(chat.get("id", "")) if isinstance(chat, dict) else ""
        if not chat_id or not isinstance(text, str):
            return {"ok": True}
        reply = telegram_command(session, text, chat_id)
        session.commit()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if token:
            channel = TelegramChannel(token)
            try:
                from accalab.messaging import _message

                channel.send(chat_id, _message(reply))
            except MessageDeliveryError:
                pass
            finally:
                channel.close()
        return {"ok": True}

    @app.post("/app/account/notifications/email")
    def enable_email_alerts(
        request: Request,
        csrf_token: str = Form(),
        consent_confirm: bool = Form(default=False),
        tickets_enabled: bool = Form(default=False),
        results_enabled: bool = Form(default=False),
        expiry_enabled: bool = Form(default=False),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        if user.role not in {"pro", "admin"} or user.email_verified_at is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        if not consent_confirm or not any((tickets_enabled, results_enabled, expiry_enabled)):
            return _account_page(request, session, user, notification_error="Confirm consent and choose an email alert.")
        consent = session.scalar(select(MessageConsent).where(
            MessageConsent.user_id == user.id, MessageConsent.channel == "email"
        ))
        now = utc_now()
        values = {
            "destination": user.email,
            "consented_at": now,
            "stopped_at": None,
            "tickets_enabled": tickets_enabled,
            "results_enabled": results_enabled,
            "expiry_enabled": expiry_enabled,
            "updated_at": now,
        }
        if consent is None:
            session.add(MessageConsent(user_id=user.id, channel="email", created_at=now, **values))
        else:
            for key, value in values.items():
                setattr(consent, key, value)
        session.commit()
        return RedirectResponse("/app/account?notifications=email-enabled", status_code=303)

    @app.post("/app/account/notifications/email/stop")
    def stop_email_alerts(
        request: Request,
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        consent = session.scalar(select(MessageConsent).where(
            MessageConsent.user_id == user.id, MessageConsent.channel == "email"
        ))
        if consent and consent.destination:
            from accalab.messaging import stop_channel

            stop_channel(session, "email", consent.destination)
            session.commit()
        return RedirectResponse("/app/account?notifications=stopped", status_code=303)

    @app.post("/app/account/notifications/whatsapp/request-code")
    def request_whatsapp_code(
        request: Request,
        phone: str = Form(),
        csrf_token: str = Form(),
        consent_confirm: bool = Form(default=False),
        age_confirmed: bool = Form(default=False),
        tickets_enabled: bool = Form(default=False),
        results_enabled: bool = Form(default=False),
        expiry_enabled: bool = Form(default=False),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        if user.role not in {"pro", "admin"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        if not consent_confirm:
            return _account_page(request, session, user, notification_error="Confirm WhatsApp consent first.")
        channel = None
        try:
            code = create_whatsapp_code(
                session, user, phone, age_confirmed=age_confirmed,
                tickets_enabled=tickets_enabled, results_enabled=results_enabled,
                expiry_enabled=expiry_enabled,
            )
            channel = WhatsAppChannel(config=messaging_settings())
            provider_id = channel.send_template(phone, "phone_verification", code)
            phone_template = whatsapp_template_details(
                "phone_verification", phone, config=messaging_settings()
            )
            session.add(MessageOutbox(
                user_id=user.id, channel="whatsapp", event_type="phone_verification",
                idempotency_key=f"wa:verify:{user.id}:{hashlib.sha256(code.encode()).hexdigest()}",
                message_text="WhatsApp phone verification", status="sent", attempts=1,
                next_attempt_at=utc_now(), provider_message_id=provider_id,
                template_name=phone_template["name"],
                template_category=phone_template["category"],
                estimated_cost_usd=phone_template["rate"],
                whatsapp_status="sent",
            ))
            session.commit()
        except (ValueError, MessageDeliveryError) as exc:
            session.rollback()
            return _account_page(request, session, user, notification_error=str(exc))
        finally:
            if channel is not None:
                channel.close()
        return RedirectResponse("/app/account?whatsapp=code-sent", status_code=303)

    @app.post("/app/account/notifications/whatsapp/verify")
    def verify_whatsapp_code(
        request: Request,
        code: str = Form(),
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        if not complete_whatsapp_code(session, user, code):
            return _account_page(request, session, user, notification_error="Verification code is invalid or expired.")
        session.commit()
        return RedirectResponse("/app/account?whatsapp=connected", status_code=303)

    @app.post("/app/account/notifications/whatsapp/stop")
    def stop_whatsapp_alerts(
        request: Request,
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        consent = session.scalar(select(MessageConsent).where(
            MessageConsent.user_id == user.id, MessageConsent.channel == "whatsapp"
        ))
        if consent:
            if consent.destination:
                from accalab.messaging import stop_channel

                stop_channel(session, "whatsapp", consent.destination)
            consent.stopped_at = utc_now()
            consent.updated_at = consent.stopped_at
            session.commit()
        return RedirectResponse("/app/account?notifications=stopped", status_code=303)

    @app.get("/messaging/whatsapp/webhook")
    def verify_whatsapp_webhook(request: Request):
        expected = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "").strip()
        if (
            request.query_params.get("hub.mode") != "subscribe"
            or not expected
            or not secrets.compare_digest(request.query_params.get("hub.verify_token", ""), expected)
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return Response(request.query_params.get("hub.challenge", ""), media_type="text/plain")

    @app.post("/messaging/whatsapp/webhook")
    async def whatsapp_webhook(request: Request, session: Session = Depends(get_db)):
        raw_body = await request.body()
        if not verify_whatsapp_signature(
            raw_body, request.headers.get("X-Hub-Signature-256", ""),
            os.getenv("META_APP_SECRET", "").strip(),
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
        channels = build_channels()
        try:
            handled = record_whatsapp_statuses(session, payload, channels)
            session.commit()
        finally:
            for adapter in channels.values():
                close = getattr(adapter, "close", None)
                if callable(close):
                    close()
        return {"received": True, "statuses_updated": handled}

    @app.get("/messaging/email/unsubscribe", response_class=HTMLResponse)
    def confirm_email_unsubscribe(token: str):
        user_id = verify_email_unsubscribe_token(token)
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsubscribe link is invalid or expired.")
        return HTMLResponse(
            "<main><h1>Stop email alerts</h1>"
            "<form method='post' action='/messaging/email/unsubscribe'>"
            f"<input type='hidden' name='token' value='{token}'>"
            "<button type='submit'>Stop email alerts</button></form></main>"
        )

    @app.post("/messaging/email/unsubscribe", response_class=HTMLResponse)
    def unsubscribe_email(token: str = Form(), session: Session = Depends(get_db)):
        user_id = verify_email_unsubscribe_token(token)
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsubscribe link is invalid or expired.")
        consent = session.scalar(select(MessageConsent).where(
            MessageConsent.user_id == user_id, MessageConsent.channel == "email"
        ))
        if consent is not None and consent.destination:
            from accalab.messaging import stop_channel

            stop_channel(session, "email", consent.destination)
            session.commit()
        return HTMLResponse("<main><h1>Email alerts stopped</h1><p>You will not receive further AccaLab email alerts.</p></main>")

    @app.post("/app/account/plan", response_class=HTMLResponse)
    def start_plan_payment(
        request: Request,
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        try:
            require_entitlement(user.role, "account")
        except AccessDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
        if user.role == "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        content = load_site_content()
        amount = content["plans"]["pro"].get("price_mwk")
        if type(amount) is not int or amount <= 0:
            return _account_page(request, session, user, error="Pro pricing has not been set yet.")
        provider = None
        try:
            provider = build_payment_provider()
            payment = new_payment(session, user, amount, provider.name)
            session.commit()
            if provider.name == "paychangu":
                base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
                if not base_url.startswith("https://"):
                    raise PaymentProviderError("Secure public service URL is not configured.")
                checkout = provider.create_payment(
                    amount_mwk=amount,
                    email=user.email,
                    tx_ref=payment.tx_ref,
                    callback_url=f"{base_url}/payments/paychangu/return",
                    return_url=f"{base_url}/app/account?payment=returned",
                )
                payment.checkout_url = checkout.checkout_url
                payment.provider_reference = checkout.provider_reference
                session.commit()
                _close_provider(provider)
                return RedirectResponse(checkout.checkout_url, status_code=status.HTTP_303_SEE_OTHER)
            _close_provider(provider)
            return RedirectResponse("/app/account?payment=pending", status_code=status.HTTP_303_SEE_OTHER)
        except (PaymentProviderError, ValueError) as exc:
            if provider is not None:
                _close_provider(provider)
            session.rollback()
            if "payment" in locals():
                stored = session.get(Payment, payment.id)
                if stored is not None and stored.status == "pending":
                    stored.status = "failed"
                    session.commit()
            return _account_page(request, session, user, error=str(exc))

    @app.get("/payments/paychangu/return")
    def paychangu_return(tx_ref: str, session: Session = Depends(get_db)):
        provider = None
        try:
            provider = build_payment_provider("paychangu")
            if not isinstance(provider, PayChanguProvider):
                raise PaymentProviderError("PayChangu is unavailable.")
            payment = session.scalar(select(Payment).where(Payment.tx_ref == tx_ref))
            if payment is not None and payment.provider == "paychangu":
                details = provider.verify_payment(tx_ref)
                apply_verified_payment(
                    session,
                    payment,
                    details,
                    period_days=int(payment_settings()["subscription"]["period_days"]),
                    grace_days=int(payment_settings()["subscription"]["grace_days"]),
                )
                session.commit()
        except (PaymentProviderError, PaymentVerificationError):
            session.rollback()
        finally:
            if provider is not None:
                _close_provider(provider)
        return RedirectResponse("/app/account?payment=returned", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/payments/paychangu/webhook")
    async def paychangu_webhook(request: Request, session: Session = Depends(get_db)):
        raw_body = await request.body()
        signature = request.headers.get("Signature", "")
        provider = None
        try:
            provider = build_payment_provider("paychangu")
            if not isinstance(provider, PayChanguProvider):
                raise PaymentProviderError("PayChangu is unavailable.")
            payload = provider.handle_webhook(raw_body, signature)
            payment = session.scalar(select(Payment).where(Payment.tx_ref == payload["tx_ref"]))
            if payment is not None and payment.provider == "paychangu":
                details = provider.verify_payment(payment.tx_ref)
                settings = payment_settings()["subscription"]
                apply_verified_payment(
                    session,
                    payment,
                    details,
                    period_days=int(settings["period_days"]),
                    grace_days=int(settings["grace_days"]),
                )
                session.commit()
        except PaymentProviderError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from exc
        except PaymentVerificationError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
        finally:
            if provider is not None:
                _close_provider(provider)
        return {"received": True}

    @app.get("/admin/{section}", response_class=HTMLResponse)
    def admin_section(section: str, request: Request, session: Session = Depends(get_db)):
        match = next(
            (item for item in ADMIN_SECTIONS if item.path.rsplit("/", 1)[1] == section), None
        )
        if match is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        user = _load_user(request, session)
        try:
            require_entitlement(user.role, match.entitlement)
        except AccessDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
        context = build_admin_page_context(session, section)
        return _render(request, "admin.html", user=user, **context)

    @app.post("/admin/site-content", response_class=HTMLResponse)
    def update_site_content(
        request: Request,
        headline: str = Form(),
        promise: str = Form(),
        free_description: str = Form(),
        pro_description: str = Form(),
        pro_price_mwk: str = Form(default=""),
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        try:
            require_entitlement(user.role, "admin")
        except AccessDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
        try:
            save_site_content(
                headline=headline,
                promise=promise,
                free_description=free_description,
                pro_description=pro_description,
                pro_price_mwk=pro_price_mwk,
            )
        except ValueError as exc:
            context = build_admin_page_context(session, "site-content")
            context["form_error"] = str(exc)
            return _render(request, "admin.html", user=user, **context)
        session.add(
            AuditLog(
                actor_user_id=user.id,
                action="update_site_content",
                target_type="site_content",
                target_id="public",
            )
        )
        session.commit()
        return RedirectResponse("/admin/site-content?saved=1", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/admin/payments/{payment_id}/mark-paid")
    def admin_mark_payment_paid(
        payment_id: str,
        request: Request,
        reference: str = Form(),
        csrf_token: str = Form(),
        session: Session = Depends(get_db),
    ):
        _check_csrf(request, csrf_token)
        user = _load_user(request, session)
        try:
            require_entitlement(user.role, "admin")
        except AccessDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
        payment = session.get(Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        settings = payment_settings()["subscription"]
        try:
            mark_manual_payment_paid(
                session,
                payment,
                reference,
                period_days=int(settings["period_days"]),
                grace_days=int(settings["grace_days"]),
            )
            session.add(
                AuditLog(
                    actor_user_id=user.id,
                    action="mark_manual_payment_paid",
                    target_type="payment",
                    target_id=payment.id,
                    details={"reference": reference.strip()},
                )
            )
            session.commit()
        except PaymentVerificationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT) from exc
        return RedirectResponse("/admin/payments?updated=1", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/app", include_in_schema=False)
    def app_root():
        return RedirectResponse("/app/today", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and request.url.path.startswith(
            ("/app", "/admin")
        ):
            return RedirectResponse(
                f"/login?next={quote(request.url.path)}", status_code=status.HTTP_303_SEE_OTHER
            )
        return _render(request, "error.html", title="Page unavailable", status_code=exc.status_code)

    return app


def _load_user(request: Request, session: Session) -> User:
    user_id = request.session.get("user_id")
    user = session.get(User, user_id) if user_id is not None else None
    if (
        user is None
        or user.status != "active"
        or request.session.get("session_version") != user.session_version
    ):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def _close_provider(provider) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def _account_page(
    request: Request,
    session: Session,
    user: User,
    *,
    error: str | None = None,
    notification_error: str | None = None,
):
    rows = session.execute(
        select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc()).limit(50)
    ).scalars().all()
    payments = [
        {
            "date": row.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
            "amount": f"MWK {row.amount_mwk:,}",
            "provider": row.provider.title(),
            "status": row.status.title(),
            "reference": row.manual_reference or row.provider_reference or row.tx_ref,
            "checkout_url": row.checkout_url,
        }
        for row in rows
    ]
    content = load_site_content()
    billing = payment_settings()
    consent = session.scalar(
        select(MessageConsent).where(
            MessageConsent.user_id == user.id, MessageConsent.channel == "telegram"
        )
    )
    email_consent = session.scalar(
        select(MessageConsent).where(
            MessageConsent.user_id == user.id, MessageConsent.channel == "email"
        )
    )
    whatsapp_consent = session.scalar(
        select(MessageConsent).where(
            MessageConsent.user_id == user.id, MessageConsent.channel == "whatsapp"
        )
    )
    whatsapp_config = messaging_settings().get("whatsapp", {})
    return _render(
        request,
        "account.html",
        user=user,
        title="Account",
        error=error,
        plan=user.plan.title(),
        plan_end=user.plan_end.astimezone().strftime("%Y-%m-%d %H:%M") if user.plan_end else "—",
        pro_price=content["plans"]["pro"].get("price_mwk"),
        period_days=int(billing["subscription"]["period_days"]),
        manual_instructions=billing["manual"].get("instructions", ""),
        payments=payments,
        payment_state=request.query_params.get("payment"),
        telegram_linked=bool(consent and consent.destination and consent.stopped_at is None),
        telegram_consent=consent,
        notification_error=notification_error,
        notification_state=request.query_params.get("notifications"),
        email_linked=bool(email_consent and email_consent.consented_at and not email_consent.stopped_at),
        email_consent=email_consent,
        whatsapp_enabled=bool(
            whatsapp_config.get("whatsapp_enabled") and whatsapp_config.get("policy_approved")
        ),
        whatsapp_linked=bool(
            whatsapp_consent and whatsapp_consent.consented_at and not whatsapp_consent.stopped_at
        ),
        whatsapp_pending=bool(
            whatsapp_consent and whatsapp_consent.link_token_hash and not whatsapp_consent.stopped_at
        ),
        whatsapp_state=request.query_params.get("whatsapp"),
    )


app = create_app()
