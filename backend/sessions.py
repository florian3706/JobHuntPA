"""Sessions, and the checks every /api request passes (accounts design, section 3).

**A session** is a row in ``sessions``. The browser's cookie holds a random 32-byte secret; the row's id is its
SHA-256 and the row keeps the user's data key (DEK) wrapped under a key derived from the secret. So no key stays in
memory between requests, restarting the server signs nobody out, and the database alone can't unwrap anything.

**The cookie** is ``__Host-jh_session`` (Secure) over HTTPS, and ``jh_session`` on plain http://127.0.0.1 (local
installs; Safari refuses Secure cookies there). HTTPS means the request's scheme after uvicorn's proxy headers, or a
Host that matches an https PUBLIC_BASE_URL. Always HttpOnly, SameSite=Lax, Path=/; Max-Age only with "Keep me
signed in". Lifetimes: 24 hours idle and 7 days in all, or 30 days idle and 90 in all when kept signed in.

**AuthMiddleware**, in order, for every /api request:
  1. POST, PUT, PATCH and DELETE must come from this site: an Origin (or Referer) that names another site is
     refused (403 ``bad_origin``). A request with neither isn't from a browser page, so no other site can have
     sent it; the CSRF token below still guards a session.
  2. Before the first account exists, everything but /api/health, /api/auth/state and /api/auth/first-run answers
     401 ``setup_required``.
  3. The sign-in endpoints (PRE_SESSION) take JSON only (415), which a form on another site can't send, and are
     rate-limited per client address (429 with Retry-After).
  4. The cookie's session is loaded, checked (state, expiry, account active) and its DEK unwrapped. Without one,
     anything but OPEN_PATHS answers 401 ``signed_out``.
  5. With a session, POST, PUT, PATCH and DELETE need ``X-CSRF-Token`` = ``b64url(HMAC(cookie secret, "csrf"))``
     (403 ``csrf``); /api/auth/me hands the page its token.
  6. The request runs inside ``crypto.as_user(user, DEK)``, so sealed values open with this user's key (sync
     endpoints run in a copy of this context, and so does a background run started from one).
  7. Until the one-time encryption of the data from before accounts has run (backend.migrate_encryption), every
     API but health, /api/auth/* and /api/encryption/* answers 423 ``encrypting`` with its progress; a request
     from the admin whose key it needs starts or resumes it.
Later phases add their own paths: the unsubscribe link and Stripe's webhook (no session, no CSRF; signed instead),
sign-up, forgot, reset and recover (PRE_SESSION), and the MFA step (a ``pending_mfa`` session).

The DB work runs in the thread pool, so a slow write elsewhere never stalls the event loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, Response

from backend import crypto, mailer, ratelimit, users
from backend.db import AuthSession, SessionLocal, User, utcnow

log = logging.getLogger(__name__)

COOKIE = "jh_session"
SECURE_COOKIE = "__Host-jh_session"
IDLE = {False: timedelta(hours=24), True: timedelta(days=30)}
ABSOLUTE = {False: timedelta(days=7), True: timedelta(days=90)}
TOUCH_EVERY = timedelta(minutes=5)   # last_seen_at (and the idle expiry) is written at most this often
RECENT_AUTH = timedelta(minutes=15)  # sensitive actions need the password entered this recently (Phases E, F)

HEALTH = "/api/health"
STATE = "/api/auth/state"
FIRST_RUN = "/api/auth/first-run"
LOGIN = "/api/auth/login"
LOGOUT = "/api/auth/logout"
# Answered without a session.
OPEN_PATHS = frozenset({HEALTH, STATE, FIRST_RUN, LOGIN, LOGOUT})
# Answered before the first account exists.
SETUP_PATHS = frozenset({HEALTH, STATE, FIRST_RUN})
# Sign-in endpoints: JSON only and rate-limited per client address; no CSRF token (there may be no session yet).
PRE_SESSION = frozenset({FIRST_RUN, LOGIN})
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class AuthUser:
    """Who a request acts for (``current_user``)."""
    id: int
    role: str
    session_id: str
    persistent: bool
    reauth_at: Optional[datetime]
    csrf: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def __repr__(self) -> str:  # never the CSRF token
        return f"<AuthUser id={self.id} role={self.role}>"


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

def _host(scope) -> str:
    return Headers(scope=scope).get("host", "").strip().lower()


def is_secure(scope) -> bool:
    """HTTPS: the scheme after uvicorn's proxy headers, or a Host matching an https PUBLIC_BASE_URL (a proxy such as
    tailscale serve that terminates TLS without saying so)."""
    if scope.get("scheme") == "https":
        return True
    public = mailer.public_base_url()
    if public:
        parts = urlsplit(public)
        return parts.scheme == "https" and parts.netloc.lower() == _host(scope)
    return False


def cookie_name(scope) -> str:
    return SECURE_COOKIE if is_secure(scope) else COOKIE


def read_secret(scope) -> Optional[bytes]:
    """The session secret from this request's cookie (only the name that fits its scheme), or None."""
    value = cookie_parser(Headers(scope=scope).get("cookie", "")).get(cookie_name(scope))
    if not value or len(value) > 64:
        return None
    try:
        secret = crypto.b64d(value)
    except ValueError:
        return None
    return secret if len(secret) == crypto.KEY_LEN else None


def set_cookie(response: Response, scope, secret: bytes, persistent: bool) -> None:
    response.set_cookie(cookie_name(scope), crypto.b64e(secret), path="/", httponly=True, samesite="lax",
                        secure=is_secure(scope),
                        max_age=int(ABSOLUTE[True].total_seconds()) if persistent else None)


def clear_cookie(response: Response, scope) -> None:
    response.delete_cookie(cookie_name(scope), path="/", httponly=True, samesite="lax", secure=is_secure(scope))


# ---------------------------------------------------------------------------
# Session rows
# ---------------------------------------------------------------------------

def create(db: Session, user: User, dek: bytes, *, persistent: bool, state: str = "active") -> bytes:
    """A new session for ``user`` (added to ``db``, not committed). Returns the cookie secret."""
    secret = crypto.new_key()
    now = utcnow()
    absolute = now + ABSOLUTE[persistent]
    db.add(AuthSession(id=crypto.session_id(secret), user_id=user.id, state=state, persistent=persistent,
                       wrapped_dek=crypto.wrap_session_dek(secret, dek), created_at=now, last_seen_at=now,
                       idle_expires_at=min(now + IDLE[persistent], absolute), absolute_expires_at=absolute,
                       reauth_at=now))
    return secret


def end(db: Session, session_id: str) -> None:
    db.query(AuthSession).filter(AuthSession.id == session_id).delete(synchronize_session=False)
    db.commit()


def end_all(db: Session, user_id: int, *, keep: Optional[str] = None) -> int:
    """Sign the user out everywhere (but ``keep``): password change or reset, disabling or deleting the account."""
    q = db.query(AuthSession).filter(AuthSession.user_id == user_id)
    if keep:
        q = q.filter(AuthSession.id != keep)
    n = q.delete(synchronize_session=False)
    db.commit()
    return n


def _expired(row: AuthSession, now: datetime) -> bool:
    return now >= row.idle_expires_at or now >= row.absolute_expires_at


_active_on: dict[int, object] = {}  # user id -> the day last written to users.last_active_on (fewer writes)


def load(secret: bytes) -> Optional[tuple[AuthUser, bytes]]:
    """The signed-in user and their DEK for a cookie secret, or None (no such session, expired, waiting for the MFA
    code, account not active). Moves the idle expiry on (at most every 5 minutes) and records the day of activity."""
    db = SessionLocal()
    try:
        sid = crypto.session_id(secret)
        row = db.get(AuthSession, sid)
        if row is None:
            return None
        now = utcnow()
        if _expired(row, now):
            db.delete(row)
            db.commit()
            return None
        if row.state != "active":
            return None
        user = db.get(User, row.user_id)
        if user is None or user.status != "active":
            return None
        try:
            dek = crypto.unwrap_session_dek(secret, row.wrapped_dek)
        except crypto.CryptoError:
            return None
        auth = AuthUser(id=user.id, role=user.role, session_id=sid, persistent=bool(row.persistent),
                        reauth_at=row.reauth_at, csrf=crypto.csrf_token(secret))
        today = datetime.now(timezone.utc).date()
        try:
            if now - (row.last_seen_at or row.created_at or now) >= TOUCH_EVERY:
                row.last_seen_at = now
                row.idle_expires_at = min(now + IDLE[bool(row.persistent)], row.absolute_expires_at)
            if _active_on.get(user.id) != today:
                if user.last_active_on != today:
                    user.last_active_on = today  # by day only (design 2.5); background work doesn't count
                _active_on[user.id] = today
            db.commit()
        except OperationalError:  # the database is busy: the request matters more than the bookkeeping
            db.rollback()
        return auth, dek
    finally:
        db.close()


def has_recent_auth(user: AuthUser, now: Optional[datetime] = None) -> bool:
    """The password was entered within the last 15 minutes (for sensitive actions, Phases E and F)."""
    return user.reauth_at is not None and (now or utcnow()) - user.reauth_at <= RECENT_AUTH


# ---------------------------------------------------------------------------
# The checks (see the module docstring)
# ---------------------------------------------------------------------------

def client_address(scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


def same_site(scope) -> bool:
    """Whether a state-changing request may have come from a page of this site (step 1)."""
    headers = Headers(scope=scope)
    if headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = headers.get("origin")
    if origin is None:
        origin = headers.get("referer")
        if not origin:
            return True  # not sent by a browser page (curl, scripts): no other site can be behind it
    if origin == "null":
        return False
    try:
        parts = urlsplit(origin)
        netloc = parts.netloc.lower()
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not netloc:
        return False
    if netloc == _host(scope):
        return True  # this server, however it was reached (a proxy may have changed the scheme)
    public = mailer.public_base_url()
    if public:
        pub = urlsplit(public)
        return (pub.scheme, pub.netloc.lower()) == (parts.scheme, netloc)
    return False


def _deny(status: int, code: str, detail: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status, headers=headers)


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api"):
            return await self.app(scope, receive, send)
        response = await self._check(scope)
        if isinstance(response, Response):
            return await response(scope, receive, send)
        if response is None:
            return await self.app(scope, receive, send)
        auth, dek = response
        scope["jh_auth"] = auth
        with crypto.as_user(auth.id, dek):
            await self.app(scope, receive, send)

    async def _check(self, scope):
        path = scope["path"].rstrip("/") or "/"
        unsafe = scope.get("method", "GET") in UNSAFE
        headers = Headers(scope=scope)
        if unsafe and not same_site(scope):
            return _deny(403, "bad_origin", "This request came from another website, so it was refused.")
        if not (users.known_to_exist() or await run_in_threadpool(users.users_exist)) and path not in SETUP_PATHS:
            return _deny(401, "setup_required", "JobHuntPA isn't set up yet: create the admin account first.")
        if path in PRE_SESSION and unsafe:
            if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return _deny(415, "json_required", "Send this as JSON.")
            wait = ratelimit.AUTH.hit(client_address(scope))
            if wait is not None:
                return _deny(429, "rate_limited",
                             f"Too many sign-in attempts from this network. Try again in {ratelimit.wait_text(wait)}.",
                             {"Retry-After": ratelimit.retry_after(wait)})
        secret = read_secret(scope)
        found = await run_in_threadpool(load, secret) if secret else None
        if found is None:
            if path in OPEN_PATHS:
                return None
            response = _deny(401, "signed_out", "You're signed out. Sign in to continue.")
            if secret:
                clear_cookie(response, scope)
            return response
        if unsafe and path not in PRE_SESSION and not crypto.check_csrf(secret, headers.get("x-csrf-token")):
            return _deny(403, "csrf", "This page's security token is out of date. Reload the page and try again.")
        if not encryption_open(path):
            blocked = await _while_encrypting(*found)
            if blocked is not None:
                return blocked
        return found


def encryption_open(path: str) -> bool:
    """Answered while the data is being encrypted: health, signing in and out, and the encryption's progress."""
    return path == HEALTH or path.startswith("/api/auth/") or path.startswith("/api/encryption/")


async def _while_encrypting(auth: AuthUser, dek: bytes) -> Optional[JSONResponse]:
    """Step 7: until the encryption migration has run (backend.migrate_encryption), data APIs answer 423 with its
    progress. A request from the admin whose key it needs starts (or resumes) it; a small database is done within
    that request, which then goes ahead."""
    from backend import migrate_encryption

    if migrate_encryption.known_done():
        return None
    if await run_in_threadpool(migrate_encryption.ensure_started, auth.id, dek):
        return None
    info = await run_in_threadpool(migrate_encryption.progress)
    if info.get("state") == "failed":
        detail = (f"Encrypting your data stopped ({info.get('error')}). JobHuntPA tries again in a minute, or when "
                  "its admin signs in again.")
    elif await run_in_threadpool(migrate_encryption.is_owner, auth.id):
        detail = "JobHuntPA is encrypting your data. This page reloads by itself when it has finished."
    else:
        detail = "JobHuntPA is waiting for its admin to sign in, to finish encrypting the data. Try again later."
    return JSONResponse({"detail": detail, "code": "encrypting", "state": info.get("state"), "done": info["done"],
                         "total": info["total"]}, status_code=423)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def signed_in_user(request: Request) -> Optional[AuthUser]:
    return request.scope.get("jh_auth")


def current_user(request: Request) -> AuthUser:
    """The signed-in user (the middleware has already refused requests without one; this is the second check)."""
    user = request.scope.get("jh_auth")
    if user is None:
        raise HTTPException(status_code=401, detail="You're signed out. Sign in to continue.")
    return user


def require_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    """App-wide settings (the LLM, the commute keys) are the admins': ordinary users can neither see nor change
    them (accounts design §14.8)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Only an admin can see or change this.")
    return user
