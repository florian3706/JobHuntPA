"""The wait after wrong passwords and codes, and the browsers that are exempt from it (accounts design, section 3.6).

Wrong tries are counted per account **and per source** (``ratelimit.GuessGuard``). A source is the network address
a try came from, or, for a browser that has signed in to the account before, that browser: it carries a signed
``jh_device`` cookie. So five wrong passwords from an outsider's address make *that address* wait; the person who
owns the account, on their own network or in their usual browser, is never held up by them. An account-wide
ceiling (100 wrong tries an hour from anywhere) slows a guesser who rotates addresses; a browser with the account's
cookie is exempt from it too, so an outsider can't use it to shut the owner out.

**The cookie** holds ``<user id>_<random id of the browser>_<start of the account's password salt>``, signed with the
server's signing key (``keys.sign``, one year). It proves nothing about the password and unlocks nothing: it only says
"this browser signed in to this account", so a try from it is counted against the browser rather than against the
network, and skips the ceiling. It is bound to the password salt, so changing or resetting the password makes every
copy useless (the next sign-in sets a fresh one). HttpOnly, SameSite=Strict, named like the session cookie (``__Host-``
over HTTPS, the port after it: backend/sessions.py). It is set by every sign-in (``sessions``' callers use
``remember_device``) and kept when signing out.

The same code counts email addresses that have no account (``ratelimit.unknown_guesses``), so the answers can't
tell the two apart.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import Response

from backend import ratelimit, sessions, users
from backend.db import SessionLocal, User
from backend.sessions import AuthProblem

DEVICE_COOKIE = "jh_device"
DEVICE_TTL = timedelta(days=365)
PURPOSE = "device"


def too_many(seconds: float, what: str = "passwords", ceiling: bool = False) -> AuthProblem:
    if ceiling:
        detail = (f"Too many wrong {what} were tried for this account lately, from several places. Wait "
                  f"{ratelimit.wait_text(seconds)}, then try again. A browser where you've signed in before isn't held "
                  "up.")
    else:
        detail = f"Too many wrong {what}. Wait {ratelimit.wait_text(seconds)}, then try again."
    return AuthProblem(429, "locked", detail, headers={"Retry-After": ratelimit.retry_after(seconds)})


def _bind(user: User) -> str:
    return bytes(user.kdf_salt).hex()[:16]


def device_id(scope, user: User) -> Optional[str]:
    """The random id of this browser if it carries a valid ``jh_device`` cookie for ``user``, else None."""
    raw = cookie_parser(Headers(scope=scope).get("cookie", "")).get(sessions.named_cookie(scope, DEVICE_COOKIE))
    payload = users.server_keys().verify(PURPOSE, raw) if raw else None
    parts = payload.split("_") if payload else []
    if len(parts) != 3 or parts[0] != str(user.id) or not secrets.compare_digest(parts[2], _bind(user)):
        return None
    return parts[1]


def remember_device(response: Response, scope, user_id: int) -> None:
    """Marks this browser as one that signed in to the account (call it on every successful sign-in)."""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return
        device = device_id(scope, user) or secrets.token_hex(8)
        token = users.server_keys().sign(PURPOSE, f"{user.id}_{device}_{_bind(user)}", ttl=DEVICE_TTL.total_seconds())
    finally:
        db.close()
    response.set_cookie(sessions.named_cookie(scope, DEVICE_COOKIE), token, path="/", httponly=True,
                        samesite="strict", secure=sessions.is_secure(scope),
                        max_age=int(DEVICE_TTL.total_seconds()))


def begin(scope, user: Optional[User], email_key: bytes = b"", *, ceiling: bool = True,
          what: str = "passwords") -> ratelimit.Ticket:
    """Counts one try at a password or code, before the slow check, and returns its ticket (``succeed()`` when the
    password was right, ``refund()`` when it was right but a code is to come, nothing when it was wrong). Raises the
    429 ``locked`` answer when this source (or, for a browser new to the account, the whole account) must wait.
    ``user`` None is an address with no account, counted under ``email_key``. ``ceiling`` False is for a step
    taken after a proof only the owner has (the password, a reset link, a recovery code)."""
    if user is None:
        guard, account, source, known = ratelimit.unknown_guesses, email_key, sessions.client_address(scope), False
    else:
        device = device_id(scope, user)
        guard, account = ratelimit.guesses, user.id
        source, known = (f"d:{device}", True) if device else (sessions.client_address(scope), False)
    try:
        return guard.begin(account, source, known=known, ceiling=ceiling)
    except ratelimit.Waiting as wait:
        raise too_many(wait.wait, what, wait.ceiling) from None


def forget(user_id: int) -> None:
    """The account starts again with no wrong tries: an admin unlocked it, or its password was changed or reset."""
    ratelimit.guesses.clear(user_id)


def waiting(user_id: int) -> float:
    """Seconds some source of the account still waits (the admin panel's "waiting after wrong passwords")."""
    return ratelimit.guesses.waiting(user_id)
