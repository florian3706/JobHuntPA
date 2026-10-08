"""The signed-in user's sign-in security (accounts design §6.3, §6.4 and §6.6; Phase E). Signing in, the code step and
resetting with a recovery code are in backend/auth.py.

    GET  /api/auth/security              {mfa_enabled, mfa_setup_started, recovery_codes_left, recovery_codes_pending,
                                          other_sessions, recent_auth}
    POST /api/auth/reauth                {password}: "confirm your password": sensitive actions need it within 15 min
    POST /api/auth/password              {current_password, new_password, sign_out_others = true}
    POST /api/auth/mfa/setup             (recent sign-in) -> {otpauth_uri, qr_svg, secret, secret_groups}
    POST /api/auth/mfa/enable            {code} (recent sign-in): the code from the app turns two-step sign-in on
    POST /api/auth/mfa/disable           {password, code | recovery_code} (recent sign-in)
    POST /api/auth/recovery-codes        {password, code? | recovery_code?} (recent sign-in; the code is needed with two-step
                                         sign-in on): eight new codes in place of all the old ones, and a notice by
                                         email; read them once with GET /api/auth/recovery-codes, confirm with
                                         POST .../ack (backend/auth.py)
    POST /api/auth/sessions/end-others   signs out every other browser, keeps this one

Errors are ``{"detail", "code", "field"?}``. A wrong password here is a 422 ``wrong_password`` (not a 401: the page
asking is signed in, and a 401 means "signed out"); 5 wrong ones in a row from this browser (counted with the
sign-in's: see backend/lockout.py) answer 429 ``locked`` with Retry-After. ``reauth_required`` (403) means "ask for the
password with POST /api/auth/reauth, then repeat the request" (``withRecentAuth`` in frontend/common.js).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend import account_email, crypto, lockout, ratelimit, sessions, totp, users
from backend.db import AuthSession, User, get_db, scrub_wal, utcnow
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["security"])


def confirm_password(request: Request, db: Session, auth: AuthUser, password: object, *, field: str = "password",
                     reset: bool = True, what: str = "passwords") -> bytes:
    """The signed-in user's DEK if ``password`` is theirs; AuthProblem otherwise. Counted per address like a sign-in
    (10 in 5 minutes) and per account and source (``lockout.begin``: 5 wrong ones in a row make this browser or
    address wait), so a stolen session can't guess it. ``reset`` False keeps the wrong-tries count for a caller that
    checks a code too (a right password gives the try back, and ``check_second_factor`` counts the code); ``what``
    names what was wrong in the wait message."""
    wait = ratelimit.AUTH.hit(sessions.client_address(request.scope))
    if wait is not None:
        raise AuthProblem(429, "rate_limited", f"Too many password checks from this network. Try again in "
                                               f"{ratelimit.wait_text(wait)}.",
                          headers={"Retry-After": ratelimit.retry_after(wait)})
    user = db.get(User, auth.id)
    ticket = lockout.begin(request.scope, user, what=what)  # counted before the slow check
    dek = users.unlock_with_password(db, user, password)
    if dek is None:
        log.info("Password check failed for user %s (%d in a row)", user.id, ticket.failures)
        raise AuthProblem(422, "wrong_password", "That password isn't right. Check it and try again.", field=field)
    ticket.succeed() if reset else ticket.refund()
    return dek


def _mark_recent(db: Session, auth: AuthUser) -> None:
    db.query(AuthSession).filter(AuthSession.id == auth.session_id).update({AuthSession.reauth_at: utcnow()},
                                                                         synchronize_session=False)
    db.commit()


class PasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field("", max_length=4096)


@router.get("/security")
def security_status(auth: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    user = db.get(User, auth.id)
    now = utcnow()
    others = db.query(AuthSession).filter(AuthSession.user_id == auth.id, AuthSession.state == "active",
                                          AuthSession.id != auth.session_id, AuthSession.idle_expires_at > now,
                                          AuthSession.absolute_expires_at > now).count()
    return {
        "mfa_enabled": bool(user.mfa_enabled),
        "mfa_setup_started": user.totp_pending_enc is not None and not user.mfa_enabled,
        "recovery_codes_left": users.recovery_codes_left(db, auth.id),
        "recovery_codes_pending": users.pending_recovery_codes(db, auth.id) is not None,
        "other_sessions": others,
        "recent_auth": sessions.has_recent_auth(auth),
    }


@router.post("/reauth")
def reauth(body: PasswordIn, request: Request, auth: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """"Confirm your password": starts a new 15 minutes of recent sign-in for this session."""
    confirm_password(request, db, auth, body.password)
    _mark_recent(db, auth)
    return {"ok": True, "minutes": int(sessions.RECENT_AUTH / timedelta(minutes=1))}


class ChangePasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field("", max_length=4096)
    new_password: str = Field("", max_length=4096)
    sign_out_others: bool = True


@router.post("/password")
def change_password(body: ChangePasswordIn, request: Request, auth: AuthUser = Depends(current_user),
                    db: Session = Depends(get_db)):
    """Changes the password: the data key is wrapped again under the new one and nothing is re-encrypted. Every other
    session ends (unless ``sign_out_others`` is false). Entering the current password counts as recent sign-in."""
    try:
        new_password = users.check_password(body.new_password)
    except users.AccountError as exc:
        raise AuthProblem(422, "invalid", exc.message, field="new_password") from None
    dek = confirm_password(request, db, auth, body.current_password, field="current_password")
    user = db.get(User, auth.id)
    users.apply_new_password(db, user, dek, users.new_password_keys(new_password))
    db.query(AuthSession).filter(AuthSession.id == auth.session_id).update({AuthSession.reauth_at: utcnow()},
                                                                         synchronize_session=False)
    ended = 0
    if body.sign_out_others:
        ended = db.query(AuthSession).filter(AuthSession.user_id == auth.id, AuthSession.id != auth.session_id
                                             ).delete(synchronize_session=False)
    account_email.end_open_links(db, auth.id)  # a reset link already sent must not undo this
    db.commit()
    scrub_wal()  # the old password's copy of the data key must not linger in the write-ahead log
    log.info("Password changed: user %s (%d other session(s) ended)", auth.id, ended)
    account_email.notify_in_session(db, auth.id, "password_changed")
    return {"ok": True, "signed_out_others": ended}


# ---------------------------------------------------------------------------
# Two-step sign-in
# ---------------------------------------------------------------------------

@router.post("/mfa/setup")
def mfa_setup(auth: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Starts setting up an authenticator app: a new pending secret (under the server key) with its QR code. Nothing
    changes for sign-in until ``/mfa/enable`` gets a code from the app."""
    require_recent_auth(auth)
    user = db.get(User, auth.id)
    if user.mfa_enabled:
        raise AuthProblem(409, "mfa_already_on", "Two-step sign-in is already on. Turn it off first to set it up "
                                                 "again.")
    secret = users.start_totp_enrolment(db, user)
    uri = totp.provisioning_uri(secret, users.details(db, auth.id).get("email", ""))
    return {"otpauth_uri": uri, "qr_svg": totp.qr_svg(uri), "secret": secret, "secret_groups": totp.grouped(secret)}


class CodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field("", max_length=64)


@router.post("/mfa/enable")
def mfa_enable(body: CodeIn, auth: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    require_recent_auth(auth)
    user = db.get(User, auth.id)
    if user.mfa_enabled:
        raise AuthProblem(409, "mfa_already_on", "Two-step sign-in is already on.")
    if not body.code.strip():
        raise AuthProblem(422, "invalid", "Enter the 6-digit code from your authenticator app.", field="code")
    done = users.enable_totp(db, user, body.code)
    if done is None:
        raise AuthProblem(409, "no_setup", "Set up your authenticator app first, then enter its code.")
    if not done:
        raise AuthProblem(422, "wrong_code", "That code isn't right. Check the code in your app and try again.",
                          field="code")
    log.info("Two-step sign-in turned on: user %s", auth.id)
    return {"ok": True, "mfa_enabled": True, "recovery_codes_left": users.recovery_codes_left(db, auth.id)}


class SecondFactorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[str] = Field(None, max_length=64)
    recovery_code: Optional[str] = Field(None, max_length=64)


class DisableMfaIn(SecondFactorIn):
    password: str = Field("", max_length=4096)


def check_second_factor(db: Session, user: User, body: SecondFactorIn, scope, *, what: str = "codes") -> None:
    """Needs the app's code or an unused recovery code (which is used up); AuthProblem otherwise. The try is counted
    before the code is looked at, like a password at sign-in (``lockout.begin``: five wrong ones in a row make this
    browser or address wait, and tries sent at the same moment can't all get past it); a right one clears the count.
    The ceiling over all of an account's sources isn't applied here: the caller holds a proof only the owner has (the
    password, a reset link)."""
    typed_code, typed_recovery = (body.code or "").strip(), (body.recovery_code or "").strip()
    if bool(typed_code) == bool(typed_recovery):
        raise AuthProblem(422, "invalid", "Enter the 6-digit code from your authenticator app, or a recovery code.",
                          field="code")
    ticket = lockout.begin(scope, user, ceiling=False, what=what)
    recovery_id: Optional[int] = None
    if typed_code:
        good = users.verify_totp(db, user, typed_code)
    else:
        opened = users.open_recovery_code(db, user.id, typed_recovery)
        recovery_id = opened[0] if opened else None
        good = opened is not None
    if not good:
        raise AuthProblem(422, "wrong_code", "That code isn't right. Check it and try again."
                          if typed_code else "That recovery code isn't right. Check it and try again.",
                          field="code" if typed_code else "recovery_code")
    if recovery_id is not None and not users.spend_recovery_code(db, recovery_id):
        db.rollback()
        raise AuthProblem(422, "wrong_code", "That recovery code isn't right. Check it and try again.",
                          field="recovery_code")
    db.commit()  # with the used-up recovery code, if one was typed
    if recovery_id is not None:
        scrub_wal()  # its copy of the data key must not linger in the write-ahead log
    ticket.succeed()


@router.post("/mfa/disable")
def mfa_disable(body: DisableMfaIn, request: Request, auth: AuthUser = Depends(current_user),
                db: Session = Depends(get_db)):
    """Turns two-step sign-in off. It takes the password and a code from the app (or a recovery code, which is used
    up), on top of recent sign-in."""
    require_recent_auth(auth)
    user = db.get(User, auth.id)
    if not user.mfa_enabled:
        raise AuthProblem(409, "mfa_off", "Two-step sign-in is already off.")
    if bool((body.code or "").strip()) == bool((body.recovery_code or "").strip()):
        raise AuthProblem(422, "invalid", "Enter the 6-digit code from your authenticator app, or a recovery code.",
                          field="code")
    # The right password keeps the wrong-tries count, so wrong codes add up to the wait (check_second_factor clears it).
    confirm_password(request, db, auth, body.password, reset=False, what="passwords or codes")
    check_second_factor(db, user, body, request.scope, what="passwords or codes")
    users.disable_totp(db, user)
    log.info("Two-step sign-in turned off: user %s", auth.id)
    return {"ok": True, "mfa_enabled": False, "recovery_codes_left": users.recovery_codes_left(db, auth.id)}


# ---------------------------------------------------------------------------
# Recovery codes and other sessions
# ---------------------------------------------------------------------------

class NewCodesIn(SecondFactorIn):
    password: str = Field("", max_length=4096)


@router.post("/recovery-codes")
def new_recovery_codes(request: Request, body: Optional[NewCodesIn] = None, auth: AuthUser = Depends(current_user),
                       db: Session = Depends(get_db)):
    """Eight new recovery codes; the old ones, used or not, stop working. They are kept sealed until the person
    confirms saving them: show them with GET /api/auth/recovery-codes, then POST /api/auth/recovery-codes/ack. Each
    code opens the data key and can set a new password, so a session alone (even a recent one: a cookie taken from
    a browser a few minutes after sign-in) must not make them: the request carries the password itself. With
    two-step sign-in on, the app's code (or a recovery code) is needed as well, because the new codes can stand in for
    it. A notice goes to the account's address."""
    require_recent_auth(auth)
    body = body or NewCodesIn()
    if not body.password:
        raise AuthProblem(422, "invalid", "Enter your password to make new recovery codes.", field="password")
    user = db.get(User, auth.id)
    mfa = bool(user.mfa_enabled)
    confirm_password(request, db, auth, body.password, reset=not mfa, what="passwords or codes")
    if mfa:
        check_second_factor(db, user, body, request.scope, what="passwords or codes")
    try:
        users.replace_recovery_codes(db, auth.id, crypto.current().dek)
    except crypto.KeyMismatch:  # the key was replaced under this request (a password reset that deleted the data)
        db.rollback()
        raise AuthProblem(401, "signed_out", "You're signed out. Sign in to continue.") from None
    db.commit()
    scrub_wal()  # the old codes' copies of the data key
    log.info("New recovery codes made: user %s", auth.id)
    account_email.notify_in_session(db, auth.id, "recovery_codes_replaced")
    return {"ok": True, "recovery_codes_left": users.RECOVERY_CODES, "recovery_codes_pending": True}


@router.post("/sessions/end-others")
def end_other_sessions(auth: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """"Sign out everywhere else": ends every other session of this account and keeps the one asking."""
    ended = sessions.end_all(db, auth.id, keep=auth.session_id)
    log.info("Other sessions ended: user %s (%d)", auth.id, ended)
    return {"ok": True, "ended": ended}
