"""Role-aware AccaLab application shell."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
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
from accalab.models import AuditLog, User
from accalab.services.entitlements import AccessDenied, require_entitlement

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
        page_note = free_page_note(section) if user.role == "free" else None
        return _render(
            request, "placeholder.html", user=user, title=match.label, page_note=page_note
        )

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
        return _render(request, "placeholder.html", user=user, title=match.label, page_note=None)

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


app = create_app()
