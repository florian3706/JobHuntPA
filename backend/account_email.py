"""The signed-in user's email address: checking it, changing it, and the security notices (accounts design §1.6, §6.1
step 2, §6.9 and §9). Everything here is off while email isn't set up (``mail_queue.enabled``: SMTP and the public
address), and says so with 409 ``email_not_set_up``.

    GET  /api/auth/email                   {enabled, missing, email, verified, pending_change, mfa_enabled}
    POST /api/auth/verify-email/resend     "send it again" (3 an hour): {ok, sent, already_verified}
    POST /api/auth/email/change            {new_email, password, code? | recovery_code?} (recent sign-in): sends the link
                                           to the NEW address
    POST /api/auth/verify-email/info       {token}: no session needed -> {kind: verify|change, email}: what the link
                                           is for, to show before anything is confirmed; doesn't use it up
    POST /api/auth/verify-email            {token}: no session needed (the link may open in another browser); the page
                                           sends it only when the person presses "Confirm" -> {ok, email_verified,
                                           changed}; 410 ``link_invalid`` when the link is unknown, used up, expired or
                                           no longer fits the account

A link is an ``auth_tokens`` row (backend/tokens.py): 256 random bits, only its hash stored, single use, 24 hours.
Kinds: ``verify_email`` (the address the account has) and ``email_change`` (a new one). The link goes in the URL
fragment (``verify-email.html#token=...``), so it never reaches a server log or a Referer header. A token's payload,
sealed under a key made from the token, names the address it is for, so a link for an old address can't confirm a
new one.

**Changing the address** (§6.9) needs recent sign-in **and the password in the request itself** (and a code from the
app or a recovery code with two-step sign-in on): a session cookie taken a few minutes after sign-in must not be
enough to move the account to another mailbox, where "forgot password" would hand it over. The old address gets an
``email_change_started`` notice when the change starts (a password change or reset ends a pending change), and the
link to the new address names what it is for. The request, which has the user's key, seals the new
``pii_self`` under the data key and keeps that blob (and the name, for ``pii_admin``) in the token's payload; so
confirming needs no key and works in any browser: it sets ``email_hash`` (under the current pepper),
``pii_self`` and ``pii_admin`` (sealed to the admin details key's public half), marks the address verified, and ends
every outstanding verification link. The old address gets an ``email_changed`` security notice. The blob is bound to
the data key's id, so it is refused if the key changed in between (a password reset that wiped the data).

**Security notices** (``notify`` and its helpers): "Your password was changed", "Two-step sign-in was turned off",
"The email address was changed". They go to ``pii_self`` in a session, ``pii_admin`` in an admin's action (§1.6), and
never block or fail the action they are about.
"""
from __future__ import annotations

import base64
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import crypto, email_templates, mail_queue, membership, ratelimit, tokens, users
from backend.db import AuthToken, SessionLocal, User, UserKeys, get_db, utcnow
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["email"])

VERIFY_KIND = "verify_email"
CHANGE_KIND = "email_change"
RESET_KIND = "reset"  # (backend/password_reset.py makes these; named here so this module needn't import it)
VERIFY_TTL = timedelta(hours=24)

LINK_INVALID = ("This link isn't valid any more: it may have expired or been used already. Sign in and ask for a new "
                "one.")


def off_problem() -> AuthProblem:
    return AuthProblem(409, "email_not_set_up", mail_queue.OFF_TEXT)


def _utc(naive: datetime) -> datetime:
    return naive.replace(tzinfo=timezone.utc)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _hash_hex(email: str) -> str:
    return users.server_keys().email_hash(email).hex()


# ---------------------------------------------------------------------------
# Security notices
# ---------------------------------------------------------------------------

def notify(event: str, address: Optional[str], user_id: Optional[int] = None, *, config=None) -> bool:
    """Queue a security notice to ``address``. True when it was queued; False when email is off, there is no address
    or the queue is full. Never raises. ``config``: the mail server settings to send through (default: the current)."""
    if not address or not isinstance(address, str):
        return False
    try:
        template = email_templates.security_notice(event, when=_utc(utcnow()))
        queued = mail_queue.send(address, template, user_id=user_id, priority=True, config=config) is not None
        if not queued and mail_queue.enabled():
            log.warning("A security notice was not queued: the mail queue is full (user %s)", user_id)
        return queued
    except Exception as exc:  # noqa: BLE001  (a notice must never undo what it is about)
        log.warning("A security notice could not be queued (%s)", exc.__class__.__name__)
        return False


def own_address(db: Session, user_id: int) -> Optional[str]:
    """The address in the user's own sealed details (``pii_self``). Needs their key scope."""
    try:
        return users.details(db, user_id).get("email") or None
    except (crypto.CryptoError, ValueError):
        return None


def notify_in_session(db: Session, user_id: int, event: str) -> bool:
    """A notice to the signed-in user's own address (their key scope is the request's)."""
    if not mail_queue.enabled():
        return False
    return notify(event, own_address(db, user_id), user_id)


def notify_with_key(db: Session, user_id: int, dek: bytes, event: str) -> bool:
    """A notice for a flow that has the data key but no session (a reset with a recovery code)."""
    if not mail_queue.enabled():
        return False
    with crypto.as_user(user_id, dek):
        return notify(event, own_address(db, user_id), user_id)


# ---------------------------------------------------------------------------
# Verification links
# ---------------------------------------------------------------------------

def _forget_unused(db: Session, user_id: int, *kinds: str) -> None:
    """Older links of these kinds for this account stop working when a new one is made."""
    db.execute(delete(AuthToken).where(AuthToken.user_id == user_id, AuthToken.kind.in_(kinds),
                                       AuthToken.used_at.is_(None)))


def end_open_links(db: Session, user_id: int) -> None:
    """After the password changed or the address changed: reset links and address-change links not yet used stop
    working, so a link in an old mailbox can't undo what the owner just did. Not committed; the caller's commit does it.
    Call it after consuming the link in use (a used link is left alone)."""
    _forget_unused(db, user_id, RESET_KIND, CHANGE_KIND)


def make_verification(db: Session, user_id: int, email: str) -> tuple:
    """A ``verify_email`` link for ``email`` (the address the account has), committed; returns the message to send.
    Older unused verification links of the account stop working."""
    _forget_unused(db, user_id, VERIFY_KIND)
    token, row = tokens.create(db, VERIFY_KIND, ttl=VERIFY_TTL, user_id=user_id,
                               payload={"email": email, "h": _hash_hex(email)})
    db.commit()
    return email_templates.verify_email(token, expires=_utc(row.expires_at))


def verify_after_sign_up(user_id: int, email: str) -> bool:
    """After sign-up (§6.1 step 2): email the new account a verification link unless its address already counts as
    verified (an invitation sent to the same address). Never raises, never waits for the mail server. True when a
    message was queued."""
    if not mail_queue.enabled() or not mail_queue.has_room():
        return False
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None or user.email_verified:
            return False
        clean = users.clean_email(email)
        return mail_queue.send(clean, make_verification(db, user_id, clean), user_id=user_id) is not None
    except Exception as exc:  # noqa: BLE001  (signing up must not fail because of email)
        db.rollback()
        log.warning("The verification email for user %s could not be queued (%s)", user_id, exc.__class__.__name__)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# What the page shows, and sending the link again
# ---------------------------------------------------------------------------

@router.get("/email")
def email_status(auth: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    user = db.get(User, auth.id)
    pending = db.query(AuthToken.id).filter(AuthToken.user_id == auth.id, AuthToken.kind == CHANGE_KIND,
                                            AuthToken.used_at.is_(None), AuthToken.expires_at > utcnow()).first()
    return {"enabled": mail_queue.enabled(), "missing": mail_queue.missing(), "email": own_address(db, auth.id),
            "verified": bool(user.email_verified), "pending_change": pending is not None,
            "mfa_enabled": bool(user.mfa_enabled)}  # (changing the address then asks for a code too)


@router.post("/verify-email/resend")
def resend_verification(auth: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """"Send it again": a new link to the address on the account. Three an hour."""
    user = db.get(User, auth.id)
    if user.email_verified:
        return {"ok": True, "sent": False, "already_verified": True}
    if not mail_queue.enabled():
        raise off_problem()
    wait = ratelimit.VERIFY_RESEND.hit(auth.id)
    if wait is not None:
        raise AuthProblem(429, "rate_limited", f"A link was sent a moment ago. Check your inbox and spam folder, or "
                                               f"try again in {ratelimit.wait_text(wait)}.",
                          headers={"Retry-After": ratelimit.retry_after(wait)})
    address = own_address(db, auth.id)
    if not address:
        raise AuthProblem(409, "no_address", "This account has no email address saved.")
    if not mail_queue.has_room():  # the link in the inbox keeps working: a new one would replace it
        return {"ok": True, "sent": False, "already_verified": False}
    queued = mail_queue.send(address, make_verification(db, auth.id, address), user_id=auth.id)
    return {"ok": True, "sent": queued is not None, "already_verified": False}


# ---------------------------------------------------------------------------
# Changing the address
# ---------------------------------------------------------------------------

class ChangeEmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_email: str = Field("", max_length=1000)
    password: str = Field("", max_length=4096)
    code: Optional[str] = Field(None, max_length=64)           # with two-step sign-in on: the app's code ...
    recovery_code: Optional[str] = Field(None, max_length=64)  # ... or a recovery code (used up)


TAKEN = "An account with this email already exists."


@router.post("/email/change")
def change_email(body: ChangeEmailIn, request: Request, auth: AuthUser = Depends(current_user),
                 db: Session = Depends(get_db)):
    """Starts a change of address: a link goes to the NEW address, and nothing changes until it is opened. Needs
    recent sign-in and the password in the request (the page has the field; with two-step sign-in on, a code too)."""
    from backend.security import SecondFactorIn, check_second_factor, confirm_password  # (security imports this module)

    require_recent_auth(auth)
    if not mail_queue.enabled():
        raise off_problem()
    try:
        new = users.clean_email(body.new_email)
    except users.AccountError as exc:
        raise AuthProblem(422, "invalid", exc.message, field="new_email") from None
    if not body.password:
        raise AuthProblem(422, "invalid", "Enter your password to change your email address.", field="password")
    user = db.get(User, auth.id)
    mfa = bool(user.mfa_enabled)
    if mfa and bool((body.code or "").strip()) == bool((body.recovery_code or "").strip()):
        raise AuthProblem(422, "invalid", "Enter the 6-digit code from your authenticator app, or a recovery code.",
                          field="code")
    confirm_password(request, db, auth, body.password, reset=not mfa, what="passwords or codes")
    wait = ratelimit.EMAIL_CHANGE.hit(auth.id)
    if wait is not None:
        raise AuthProblem(429, "rate_limited", f"Too many address changes. Try again in {ratelimit.wait_text(wait)}.",
                          headers={"Retry-After": ratelimit.retry_after(wait)})
    server = users.server_keys()
    if user.email_hash in server.email_hash_candidates(new):
        raise AuthProblem(422, "same_address", "That's the address this account already has.", field="new_email")
    if users.find_by_email(db, new) is not None:
        raise AuthProblem(409, "email_taken", TAKEN, field="new_email")
    if mfa:
        check_second_factor(db, user, SecondFactorIn(code=body.code, recovery_code=body.recovery_code), request.scope,
                            what="passwords or codes")
    info = users.details(db, auth.id)
    old = info.get("email") or ""
    name = info.get("name") or ""
    scope = crypto.current()
    keys = db.get(UserKeys, auth.id, populate_existing=True)
    if not hmac.compare_digest(scope.kid, bytes(keys.dek_kid)):  # the key was replaced under this request (a wipe reset)
        raise AuthProblem(401, "signed_out", "You're signed out. Sign in to continue.")
    _forget_unused(db, auth.id, CHANGE_KIND)
    token, row = tokens.create(db, CHANGE_KIND, ttl=VERIFY_TTL, user_id=auth.id, payload={
        "email": new, "old": old, "name": name,
        "pii_self": _b64(crypto.seal(users._details(new, name), f"users.pii_self|{auth.id}")),
        "kid": _b64(scope.kid)})  # the key that sealed it: _confirm_change checks it is still the account's
    db.commit()
    template = email_templates.verify_email(token, expires=_utc(row.expires_at), change=True)
    queued = mail_queue.send(new, template, user_id=auth.id)
    log.info("Email change started: user %s", auth.id)
    notify("email_change_started", old, auth.id)  # the old mailbox hears of it before anything changes
    return {"ok": True, "sent": queued is not None}


# ---------------------------------------------------------------------------
# Opening the link
# ---------------------------------------------------------------------------

class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field("", max_length=2000)  # (a longer one is never ours: ``tokens.find`` says no)


def link_invalid() -> AuthProblem:
    return AuthProblem(410, "link_invalid", LINK_INVALID)


def _open_link(db: Session, token: str) -> tuple[str, AuthToken, User, dict]:
    """``(kind, row, user, payload)`` of a verification or address-change link that can still be used; 410
    ``link_invalid`` otherwise. Doesn't use it up."""
    kind = CHANGE_KIND
    row = tokens.find(db, CHANGE_KIND, token)
    if row is None:
        kind, row = VERIFY_KIND, tokens.find(db, VERIFY_KIND, token)
    if row is None:
        raise link_invalid()
    payload = tokens.open_payload(token, kind, row.payload_enc)
    user = db.get(User, row.user_id) if row.user_id is not None else None
    if payload is None or user is None or user.status != "active" or not isinstance(payload.get("email"), str):
        raise link_invalid()
    return kind, row, user, payload


@router.post("/verify-email/info")
def verify_email_info(body: TokenIn, db: Session = Depends(get_db)):
    """What a link is for, so the page can ask before it confirms anything: ``{kind: "verify" | "change", email}``.
    Opening the link must not do the confirming: a mail scanner or a stray click would plant one account on another
    person's address. Doesn't use the link up."""
    kind, _row, _user, payload = _open_link(db, body.token)
    return {"kind": "verify" if kind == VERIFY_KIND else "change", "email": payload["email"]}


@router.post("/verify-email")
def verify_email(body: TokenIn, db: Session = Depends(get_db)):
    """Confirms the address a link was made for. No session is needed: the link may open in another browser. One
    token confirms one address, once. The page sends this only when the person presses "Confirm"."""
    kind, row, user, payload = _open_link(db, body.token)
    if kind == VERIFY_KIND:
        return _confirm_address(db, row, user, payload, body.token)
    return _confirm_change(db, row, user, payload, body.token)


def _confirm_address(db: Session, row: AuthToken, user: User, payload: dict, token: str) -> dict:
    if user.email_hash not in users.server_keys().email_hash_candidates(payload["email"]):
        raise link_invalid()  # the address changed since this link was sent
    if tokens.consume(db, VERIFY_KIND, token) is None:
        raise link_invalid()
    db.execute(update(User).where(User.id == user.id).values(email_verified=True))
    db.commit()
    log.info("Email address verified: user %s", user.id)
    return {"ok": True, "email_verified": True, "changed": False}


def _confirm_change(db: Session, row: AuthToken, user: User, payload: dict, token: str) -> dict:
    server = users.server_keys()
    keys = db.get(UserKeys, user.id)
    try:
        blob, kid = base64.b64decode(payload["pii_self"], validate=True), base64.b64decode(payload["kid"], validate=True)
    except (KeyError, ValueError):
        raise link_invalid() from None
    new, old, name = payload["email"], payload.get("old") or "", payload.get("name") or ""
    if user.email_hash not in server.email_hash_candidates(old) or bytes(keys.dek_kid) != kid:
        raise link_invalid()  # the account's address or key changed since the link was sent
    if tokens.consume(db, CHANGE_KIND, token) is None:
        raise link_invalid()
    try:
        user.email_hash = server.email_hash(new)
        user.pii_self = blob
        user.email_verified = True
        apk = users.admin_public_key(db)
        if apk is not None:
            user.pii_admin = crypto.seal_to(apk[0], users._details(new, name), f"users.pii_admin|{user.id}")
            user.pii_admin_version = apk[1]
        _forget_unused(db, user.id, VERIFY_KIND, CHANGE_KIND, RESET_KIND)  # a reset link to the old address too
        db.commit()
    except IntegrityError:  # someone signed up with this address after the link was sent
        db.rollback()
        tokens.release(db, row.id)
        raise AuthProblem(409, "email_taken", TAKEN + " Ask for a new link with another address.") from None
    log.info("Email address changed: user %s", user.id)
    membership.update_customer_email(db, user.id, new)
    notify("email_changed", old, user.id)
    return {"ok": True, "email_verified": True, "changed": True}
