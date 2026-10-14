"""Sessions, and the checks every /api request passes (accounts design, section 3).

**A session** is a row in ``sessions``. The browser's cookie holds a random 32-byte secret; the row's id is its
SHA-256 and the row keeps the user's data key (DEK) wrapped under a key derived from the secret, and beside it the
identity key (IK, ``wrapped_ik``), which only the admin panel's details chain needs (backend/admin_key.py). So no key
stays in memory between requests, restarting the server signs nobody out, and the database alone can't unwrap
anything. A request opens the IK only when it asks (``crypto.open_ik``); a session made before the IK existed has
none, and the admin panel says to sign in again.

**The cookie** is ``__Host-jh_session`` (Secure) over HTTPS, and ``jh_session`` on plain http://127.0.0.1 (local
installs; Safari refuses Secure cookies there). When the address has a port, the port is part of the name
(``jh_session_8001``, ``__Host-jh_session_8443``): browsers keep cookies per host name and ignore the port, so two
copies of JobHuntPA on one computer (a test instance next to the live app) would otherwise sign each other out.
HTTPS means the request's scheme after uvicorn's proxy headers, or a Host that matches an https PUBLIC_BASE_URL.
(A port does not isolate other apps on the same host name: they still receive the cookie. JobHuntPA should have a
host name of its own.) Always HttpOnly, SameSite=Lax, Path=/; Max-Age only with "Keep me
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

**Browser-extension keys** (backend/extension.py). A request with ``Authorization: Bearer jhpa_...`` is checked by the
key alone, never the cookie (a key that doesn't work answers 401 ``token_invalid``, even with a good cookie). Any other
``Authorization`` value (a sign-in proxy's ``Bearer <JWT>`` or ``Basic``) isn't ours: the request goes the cookie's
way, with all its checks. A key request skips steps 1, 3 and 5 (a browser never sends that header by itself, and
another site's page can't add it without a CORS preflight this server never approves), keeps 2, 6 and 7, and reaches
only ``KEY_ENDPOINTS`` (403 ``token_scope`` otherwise). Its ``AuthUser`` has ``key`` set and never a recent sign-in.
Keys that don't work are counted per client address (``ratelimit.BAD_KEY``); past its limit a key that doesn't work
answers 429, and one that works still gets in. A key's row is a ``sessions`` row (``state = "extension"``), so
everything that ends all of an account's sessions ends its keys too.

**Sign-in** takes the name and password and makes an ``active`` session; there is no second step. ``load`` never
unwraps a row that isn't ``active`` (or an extension key's, through ``load_key``), so a leftover row of another state
reaches nothing. Sign-up and resetting a password with a recovery code are PRE_SESSION too: what is in the body (an
invitation, a recovery code) proves what a session would.

The DB work runs in the thread pool, so a slow write elsewhere never stalls the event loop.
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy import null
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, Response

from backend import admin_key, crypto, public_url, ratelimit, users
from backend.db import AuthSession, SessionLocal, User, utcnow

log = logging.getLogger(__name__)

COOKIE = "jh_session"
IDLE = {False: timedelta(hours=24), True: timedelta(days=30)}
ABSOLUTE = {False: timedelta(days=7), True: timedelta(days=90)}
TOUCH_EVERY = timedelta(minutes=5)   # last_seen_at (and the idle expiry) is written at most this often
RECENT_AUTH = timedelta(minutes=15)  # sensitive actions need the password entered this recently (Phases E, F)

HEALTH = "/api/health"
STATE = "/api/auth/state"
FIRST_RUN = "/api/auth/first-run"
LOGIN = "/api/auth/login"
LOGOUT = "/api/auth/logout"
RECOVER = "/api/auth/recover"       # a forgotten password, reset with a recovery code (the data is kept)
SIGNUP = "/api/auth/signup"
# Answered without a session.
OPEN_PATHS = frozenset({HEALTH, STATE, FIRST_RUN, LOGIN, RECOVER, LOGOUT, SIGNUP})
# Answered before the first account exists.
SETUP_PATHS = frozenset({HEALTH, STATE, FIRST_RUN})
# Sign-in endpoints: JSON only and rate-limited per client address; no CSRF token (there may be no session yet).
PRE_SESSION = frozenset({FIRST_RUN, LOGIN, RECOVER, SIGNUP})
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
    key: bool = False  # a browser-extension key, not a browser session: no recent sign-in, KEY_ENDPOINTS only

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def __repr__(self) -> str:  # never the CSRF token
        return f"<AuthUser id={self.id} role={self.role}{' key' if self.key else ''}>"


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
    public = public_url.public_base_url()
    if public:
        parts = urlsplit(public)
        return parts.scheme == "https" and parts.netloc.lower() == _host(scope)
    return False


def _port(scope) -> str:
    """The port in the request's Host header ("" when it has none, or it isn't plain digits)."""
    host = _host(scope)
    if host.startswith("["):  # [::1]:8040
        host = host[host.find("]") + 1:]
    port = host.rpartition(":")[2] if ":" in host else ""
    return port if port.isdigit() and len(port) <= 5 else ""


def named_cookie(scope, plain: str) -> str:
    """A cookie's name for this request: ``__Host-`` over HTTPS, and ``_<port>`` after it when the Host has a port,
    so instances on different ports of one host name keep their own cookies."""
    port = _port(scope)
    return ("__Host-" if is_secure(scope) else "") + plain + (f"_{port}" if port else "")


def cookie_name(scope) -> str:
    return named_cookie(scope, COOKIE)


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

def create(db: Session, user: User, dek: bytes, ik: Optional[bytes], *, persistent: bool) -> bytes:
    """A new session for ``user`` (added to ``db``, not committed). Returns the cookie secret. ``ik`` is the identity
    key (None: the account has none yet, so the session has none). Signing in also re-seals the person's details for
    admins when they aren't sealed to the current admin key (backend.admin_key)."""
    admin_key.heal_details(db, user, dek)
    secret = crypto.new_key()
    sid = crypto.session_id(secret)
    now = utcnow()
    absolute = now + ABSOLUTE[persistent]
    wrapped_ik = crypto.wrap(crypto.session_kek(secret), ik, f"sess-ik|{sid}") if ik is not None else None
    db.add(AuthSession(id=sid, user_id=user.id, state="active", persistent=persistent,
                       wrapped_dek=crypto.wrap_session_dek(secret, dek), wrapped_ik=wrapped_ik,
                       created_at=now, last_seen_at=now,
                       idle_expires_at=min(now + IDLE[persistent], absolute), absolute_expires_at=absolute,
                       reauth_at=now))
    return secret


def give_ik(db: Session, scope, session_id: str, ik: bytes) -> None:
    """Gives the IK to the request's own session when it has none (made before the account had one): a password
    check that just opened it, or made it, is the one moment it is in hand. Commits. Does nothing for another cookie."""
    secret = read_secret(scope)
    if secret is None or crypto.session_id(secret) != session_id:
        return
    db.query(AuthSession).filter(AuthSession.id == session_id, AuthSession.state == "active",
                                 AuthSession.wrapped_ik.is_(None)).update(
        {AuthSession.wrapped_ik: crypto.wrap(crypto.session_kek(secret), ik, f"sess-ik|{session_id}")},
        synchronize_session=False)
    db.commit()


def end_cookie_session(db: Session, scope) -> None:
    """Ends the session this request's cookie names, whatever its state (a new sign-in or a sign-out replaces it)."""
    secret = read_secret(scope)
    if secret:
        end(db, crypto.session_id(secret))


def end(db: Session, session_id: str) -> None:
    db.query(AuthSession).filter(AuthSession.id == session_id).delete(synchronize_session=False)
    db.commit()


def end_all(db: Session, user_id: int, *, keep: Optional[str] = None) -> int:
    """Sign the user out everywhere (but ``keep``): password change or reset, disabling or deleting the account. The
    account's browser-extension keys are session rows too, so they end as well."""
    q = db.query(AuthSession).filter(AuthSession.user_id == user_id)
    if keep:
        q = q.filter(AuthSession.id != keep)
    n = q.delete(synchronize_session=False)
    db.commit()
    return n


def _expired(row: AuthSession, now: datetime) -> bool:
    return now >= row.idle_expires_at or now >= row.absolute_expires_at


_active_on: dict[int, object] = {}  # user id -> the day last written to users.last_active_on (fewer writes)


def load(secret: bytes) -> Optional[tuple[AuthUser, bytes, Optional[Callable[[], bytes]]]]:
    """The signed-in user, their DEK and a way to open their IK (``crypto.session_ik_opener``; None for a session
    from before the IK existed) for a cookie secret, or None (no such session, expired, not an ``active`` one,
    account not active). Moves the idle expiry on (at most every 5 minutes) and records the day of activity."""
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
        ik_opener = crypto.session_ik_opener(secret, row.wrapped_ik) if row.wrapped_ik is not None else None
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
        return auth, dek, ik_opener
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Browser-extension keys (backend/extension.py)
# ---------------------------------------------------------------------------

KEY_STATE = "extension"
KEY_PREFIX = "jhpa_"
KEY_IDLE = timedelta(days=60)       # a key not used for this long stops working
KEY_ABSOLUTE = timedelta(days=365)  # and every key after a year
KEY_LEN_CHARS = len(KEY_PREFIX) + 43  # 32 bytes in base64url, without padding
TOKEN_INVALID_DETAIL = ("This extension key doesn't work: it was disconnected, has expired or was copied wrong. "
                        "Connect the extension again in JobHuntPA's Settings, then paste the new key in its options.")
TOKEN_SCOPE = "token_scope"
TOKEN_SCOPE_DETAIL = "A browser-extension key can't be used for this. Open JobHuntPA in your browser instead."
# What an extension key may call: (method, path) with the path's trailing slash taken off. Everything else answers
# 403 token_scope, whatever the key.
KEY_ENDPOINTS = (
    ("GET", re.compile(r"/api/extension/status")),
    ("POST", re.compile(r"/api/extension/preview")),
    ("POST", re.compile(r"/api/extension/jobs")),
    ("POST", re.compile(r"/api/extension/jobs/batch")),
    ("GET", re.compile(r"/api/sources")),
    ("POST", re.compile(r"/api/sources")),
    ("POST", re.compile(r"/api/sources/[0-9]{1,18}/test")),
)


def key_may_call(method: str, path: str) -> bool:
    return any(method == m and pattern.fullmatch(path) for m, pattern in KEY_ENDPOINTS)


def new_extension_key(db: Session, user_id: int, dek: bytes, label: str) -> tuple[str, AuthSession]:
    """A new extension key for ``user_id`` (added to ``db``, not committed): returns the key as the person pastes it
    (``jhpa_`` + base64url of 32 random bytes; it is never stored) and its row. ``dek`` is the request's own data key;
    ``label`` the name the person gave it, sealed under that key and bound to the row. No identity key, no recent
    sign-in."""
    secret = crypto.new_key()
    sid = crypto.extension_key_id(secret)
    now = utcnow()
    absolute = now + KEY_ABSOLUTE
    row = AuthSession(id=sid, user_id=user_id, state=KEY_STATE, persistent=False,
                      wrapped_dek=crypto.wrap_extension_dek(secret, dek), wrapped_ik=None,
                      created_at=now, last_seen_at=null(), idle_expires_at=min(now + KEY_IDLE, absolute),
                      absolute_expires_at=absolute, reauth_at=None,
                      public_id=secrets.token_urlsafe(9),
                      label_enc=crypto.seal(label.encode("utf-8"), f"sessions.label|{sid}"))
    db.add(row)
    return KEY_PREFIX + crypto.b64e(secret), row


def bearer(headers: Headers) -> Optional[str]:
    """The extension key of an ``Authorization: Bearer jhpa_...`` header, or None. Anything else in that header (a
    sign-in proxy in front of the app passing on its own ``Bearer <JWT>``, or ``Basic``) is not ours: the cookie is
    used then."""
    value = headers.get("authorization")
    if value is None:
        return None
    scheme, _, token = value.strip().partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token.startswith(KEY_PREFIX) else None


def key_secret(token: str) -> Optional[bytes]:
    """The 32 bytes of a well-formed extension key, else None."""
    if len(token) != KEY_LEN_CHARS or not token.startswith(KEY_PREFIX):
        return None
    try:
        secret = crypto.b64d(token[len(KEY_PREFIX):])
    except ValueError:
        return None
    return secret if len(secret) == crypto.KEY_LEN else None


def load_key(secret: bytes) -> Optional[tuple[AuthUser, bytes, None]]:
    """The user and DEK of an extension key, or None (unknown, revoked, expired, account not active). Moves its idle
    expiry on and records its last use, at most every 5 minutes. The user has no recent sign-in and no identity
    key."""
    db = SessionLocal()
    try:
        sid = crypto.extension_key_id(secret)
        row = db.get(AuthSession, sid)
        if row is None or row.state != KEY_STATE:
            return None
        now = utcnow()
        if _expired(row, now):
            db.delete(row)
            db.commit()
            return None
        user = db.get(User, row.user_id)
        if user is None or user.status != "active":
            return None
        try:
            dek = crypto.unwrap_extension_dek(secret, row.wrapped_dek)
        except crypto.CryptoError:
            return None
        auth = AuthUser(id=user.id, role=user.role, session_id=sid, persistent=False, reauth_at=None, csrf="",
                        key=True)
        today = datetime.now(timezone.utc).date()
        try:
            if row.last_seen_at is None or now - row.last_seen_at >= TOUCH_EVERY:
                row.last_seen_at = now
                row.idle_expires_at = min(now + KEY_IDLE, row.absolute_expires_at)
            if _active_on.get(user.id) != today:
                if user.last_active_on != today:
                    user.last_active_on = today
                _active_on[user.id] = today
            db.commit()
        except OperationalError:
            db.rollback()
        return auth, dek, None
    finally:
        db.close()


def has_recent_auth(user: AuthUser, now: Optional[datetime] = None) -> bool:
    """The password was entered within the last 15 minutes (for sensitive actions, Phases E and F). Never for an
    extension key."""
    return not user.key and user.reauth_at is not None and (now or utcnow()) - user.reauth_at <= RECENT_AUTH


class AuthProblem(Exception):
    """An error answer shaped ``{"detail", "code", "field"?}`` (app.py turns it into the response)."""

    def __init__(self, status: int, code: str, detail: str, field: Optional[str] = None,
                 headers: Optional[dict] = None):
        super().__init__(detail)
        self.status, self.code, self.detail, self.field, self.headers = status, code, detail, field, headers

    def response(self) -> JSONResponse:
        body = {"detail": self.detail, "code": self.code}
        if self.field:
            body["field"] = self.field
        return JSONResponse(body, status_code=self.status, headers=self.headers)


REAUTH_REQUIRED = "reauth_required"


def require_recent_auth(user: AuthUser) -> None:
    """Refuses a sensitive action unless the password was entered in the last 15 minutes (design §3). The page
    answers the 403 by asking for the password again (``withRecentAuth`` in frontend/common.js) and retrying."""
    if not has_recent_auth(user):
        raise AuthProblem(403, REAUTH_REQUIRED, "For your safety, enter your password again to do this.")


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
    public = public_url.public_base_url()
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
        auth, dek, ik_opener = response
        scope["jh_auth"] = auth
        with crypto.as_user(auth.id, dek, ik_opener):
            await self.app(scope, receive, send)

    async def _check(self, scope):
        path = scope["path"].rstrip("/") or "/"
        unsafe = scope.get("method", "GET") in UNSAFE
        headers = Headers(scope=scope)
        token = bearer(headers)
        if token is not None:
            return await self._check_key(scope, path, token)
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
            blocked = await _while_encrypting(found[0], found[1])
            if blocked is not None:
                return blocked
        return found

    async def _check_key(self, scope, path: str, token: str):
        """A request with ``Authorization: Bearer jhpa_...``: a browser-extension key, and only that (the cookie is
        never looked at, even when the key doesn't work). No Origin check or CSRF token: browsers never add this
        header by themselves, and a page on another site can't add it without a CORS preflight, which this server
        never approves. Keys that don't work are counted per client address (``ratelimit.BAD_KEY``): past its limit
        one answers 429, but a key that works is let in all the same (one address can be many people's: a shared
        network, a proxy), and guessing a key is hopeless (32 random bytes)."""
        if not (users.known_to_exist() or await run_in_threadpool(users.users_exist)):
            return _deny(401, "setup_required", "JobHuntPA isn't set up yet: create the admin account first.")
        secret = key_secret(token)
        found = await run_in_threadpool(load_key, secret) if secret else None
        if found is None:
            wait = ratelimit.BAD_KEY.hit(client_address(scope))
            if wait is not None:
                return _deny(429, "rate_limited", f"Too many tries with keys that don't work. Try again in "
                                                  f"{ratelimit.wait_text(wait)}.",
                             {"Retry-After": ratelimit.retry_after(wait)})
            return _deny(401, "token_invalid", TOKEN_INVALID_DETAIL)
        if not key_may_call(scope.get("method", "GET"), path):
            return _deny(403, TOKEN_SCOPE, TOKEN_SCOPE_DETAIL)
        if not encryption_open(path):
            blocked = await _while_encrypting(found[0], found[1])
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
