"""The signed-in user's sign-in security (accounts design §6.3, §6.4 and §6.6; Phase E). Signing in and resetting
with a recovery code are in backend/auth.py. Every route here needs a signed-in session.

    GET  /api/auth/security              {recovery_codes_left, recovery_codes_pending, recovery_codes_old,
                                          other_sessions, recent_auth}
    POST /api/auth/reauth                {password}: "confirm your password": sensitive actions need it within 15 min
    POST /api/auth/password              {current_password, new_password, sign_out_others = true}
    POST /api/auth/recovery-codes        {password} (recent sign-in): eight new codes in place of all the old ones,
                                         returned once as ``recovery_codes``
    POST /api/auth/recovery-codes/show   {password}: the codes not yet confirmed as saved, again
                                         (a reload, or a return later); confirm with POST .../ack (backend/auth.py)
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

from backend import crypto, lockout, ratelimit, sessions, users
from backend.db import AuthSession, User, get_db, scrub_wal, utcnow
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["security"])


def confirm_password(request: Request, db: Session, auth: AuthUser, password: object, *, field: str = "password",
                     reset: bool = True, what: str = "passwords") -> users.Unlocked:
    """The signed-in user's DEK and identity key (``users.Unlocked``) if ``password`` is theirs; AuthProblem otherwise. Counted per address like a sign-in
    (10 in 5 minutes) and per account and source (``lockout.begin``: 5 wrong ones in a row make this browser or
    address wait), so a stolen session can't guess it. ``reset`` False gives a right password's try back instead of
    clearing the count; ``what`` names what was wrong in the wait message."""
    wait = ratelimit.AUTH.hit(sessions.client_address(request.scope))
    if wait is not None:
        raise AuthProblem(429, "rate_limited", f"Too many password checks from this network. Try again in "
                                               f"{ratelimit.wait_text(wait)}.",
                          headers={"Retry-After": ratelimit.retry_after(wait)})
    user = db.get(User, auth.id)
    ticket = lockout.begin(request.scope, user, what=what)  # counted before the slow check
    unlocked = users.unlock(db, user, password)
    if unlocked is None:
        log.info("Password check failed for user %s (%d in a row)", user.id, ticket.failures)
        raise AuthProblem(422, "wrong_password", "That password isn't right. Check it and try again.", field=field)
    ticket.succeed() if reset else ticket.refund()
    if unlocked.ik is not None and not crypto.has_ik():  # a session from before the identity key gets it now
        sessions.give_ik(db, request.scope, auth.session_id, unlocked.ik)
    return unlocked


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
        "recovery_codes_left": users.recovery_codes_left(db, auth.id),
        "recovery_codes_pending": users.has_pending_recovery_codes(db, auth.id),
        # An admin whose unused codes were made before the identity key: a reset with one keeps their data but loses
        # their share of the admin key, so Settings suggests making new codes.
        "recovery_codes_old": user.role == "admin" and users.recovery_codes_without_ik(db, auth.id) > 0,
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
    """Changes the password: the data key and the identity key are wrapped again under the new one and nothing is
    re-encrypted. Every other
    session ends (unless ``sign_out_others`` is false). Entering the current password counts as recent sign-in."""
    try:
        new_password = users.check_password(body.new_password)
    except users.AccountError as exc:
        raise AuthProblem(422, "invalid", exc.message, field="new_password") from None
    unlocked = confirm_password(request, db, auth, body.current_password, field="current_password")
    user = db.get(User, auth.id)
    users.apply_new_password(db, user, unlocked.dek, unlocked.ik, users.new_password_keys(new_password))
    db.query(AuthSession).filter(AuthSession.id == auth.session_id).update({AuthSession.reauth_at: utcnow()},
                                                                         synchronize_session=False)
    ended = 0
    if body.sign_out_others:
        ended = db.query(AuthSession).filter(AuthSession.user_id == auth.id, AuthSession.id != auth.session_id
                                             ).delete(synchronize_session=False)
    db.commit()
    scrub_wal()  # the old password's copies of the keys must not linger in the write-ahead log
    log.info("Password changed: user %s (%d other session(s) ended)", auth.id, ended)
    return {"ok": True, "signed_out_others": ended}


# ---------------------------------------------------------------------------
# Recovery codes and other sessions
# ---------------------------------------------------------------------------

class NewCodesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field("", max_length=4096)


@router.post("/recovery-codes")
def new_recovery_codes(request: Request, body: Optional[NewCodesIn] = None, auth: AuthUser = Depends(current_user),
                       db: Session = Depends(get_db)):
    """Eight new recovery codes; the old ones, used or not, stop working. They are kept sealed until the person
    confirms saving them: show them with GET /api/auth/recovery-codes, then POST /api/auth/recovery-codes/ack. Each
    code opens the data key and can set a new password, so a session alone (even a recent one: a cookie taken from
    a browser a few minutes after sign-in) must not make them: the request carries the password itself. The new codes
    are in the answer (``recovery_codes``): the page shows
    them at once, and a later look at the unsaved ones needs the password again (``/recovery-codes/show``)."""
    require_recent_auth(auth)
    body = body or NewCodesIn()
    if not body.password:
        raise AuthProblem(422, "invalid", "Enter your password to make new recovery codes.", field="password")
    unlocked = confirm_password(request, db, auth, body.password)
    try:
        codes = users.replace_recovery_codes(db, auth.id, crypto.current().dek, unlocked.ik)
    except crypto.KeyMismatch:  # the key was replaced under this request (a password reset that deleted the data)
        db.rollback()
        raise AuthProblem(401, "signed_out", "You're signed out. Sign in to continue.") from None
    db.commit()
    scrub_wal()  # the old codes' copies of the keys
    log.info("New recovery codes made: user %s", auth.id)
    return {"ok": True, "recovery_codes_left": users.RECOVERY_CODES, "recovery_codes_pending": True,
            "recovery_codes": codes}


@router.post("/recovery-codes/show")
def show_recovery_codes(request: Request, body: Optional[NewCodesIn] = None, auth: AuthUser = Depends(current_user),
                        db: Session = Depends(get_db)):
    """The recovery codes not yet confirmed as saved (after sign-up, first run, a reset or new codes), for a page that no
    longer has them. Each code opens the data key and can set a new password, and a new session counts as recent for
    15 minutes, so a session alone must not read them (a cookie taken from a browser just after sign-in): the request
    carries the password."""
    body = body or NewCodesIn()
    if not users.has_pending_recovery_codes(db, auth.id):
        raise AuthProblem(404, "none_pending", "There are no recovery codes waiting to be saved: you confirmed "
                                               "saving them already.")
    if not body.password:
        raise AuthProblem(422, "invalid", "Enter your password to see your recovery codes.", field="password")
    unlocked = confirm_password(request, db, auth, body.password)
    codes = users.pending_recovery_codes(db, auth.id, unlocked.ik)
    if codes is None:
        raise AuthProblem(404, "none_pending", "There are no recovery codes waiting to be saved: you confirmed "
                                               "saving them already.")
    return {"codes": codes}


@router.post("/sessions/end-others")
def end_other_sessions(auth: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """"Sign out everywhere else": ends every other session of this account and keeps the one asking. Browser-extension
    keys end too (they are session rows)."""
    ended = sessions.end_all(db, auth.id, keep=auth.session_id)
    if ended:
        scrub_wal()  # the rows held wrapped copies of the data key
    log.info("Other sessions ended: user %s (%d)", auth.id, ended)
    return {"ok": True, "ended": ended}
