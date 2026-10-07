"""Signing in and out, and the first run (accounts design, sections 3, 5 and 6).

    GET  /api/auth/state[?invite=<token>] {setup_required, signed_in, mfa_pending?, signup?, email_enabled?} (no
                                          session needed; email_enabled: reset links by email work, set once set up)
                                          signup: {mode: invite|open|closed, allowed, encrypting,
                                                   invite: {valid, email}}
    POST /api/auth/signup                 {email, password, name?, remember?, invite?}
                                          -> 201 {csrf, recovery_codes} + session cookie (Phase F)
    POST /api/auth/first-run              {token, email, password, name?, remember?, background?}
                                          -> 201 {csrf, recovery_codes: [8 codes, shown once]} + session cookie
    POST /api/auth/login                  {email, password, remember?} -> {csrf, user} + session cookie; with two-step
                                          sign-in on: {mfa_required: true} and a session that is only pending_mfa
    POST /api/auth/login/mfa              {code} | {recovery_code} -> {csrf, user, recovery_codes_left?} + a new session
    POST /api/auth/recover                {email, recovery_code, new_password, code? | second_recovery_code?}
                                          -> {csrf, user, recovery_codes_left} + session cookie: a forgotten password
                                          reset with a recovery code, and the data kept
    POST /api/auth/logout                 ends this session (pending or active); Clear-Site-Data: "cache"
    GET  /api/auth/me                     {user: {id, email, name, role}, csrf, recovery_codes_pending, features}
    GET  /api/auth/recovery-codes         (recent sign-in) {codes}: the codes not yet confirmed as saved (404 when none)
    POST /api/auth/recovery-codes/ack     "I've saved my recovery codes": the copy kept to show them is deleted

Errors are ``{"detail": "<plain words>", "code": "...", "field"?: "email" | "password" | ...}``. A wrong password and
an unknown email address get the same answer, after the same amount of work. Login, the code step, recover and first
run go through the checks in backend/sessions.py first (same site, JSON only, rate limit per address).

Changing the password, two-step sign-in, new recovery codes and signing out elsewhere are in backend/security.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import account, account_email, crypto, mail_queue, migrate_encryption, ratelimit, sessions, signup, users
from backend.db import AuthSession, AuthToken, SessionLocal, User, get_db, scrub_wal, utcnow
from backend.sessions import REAUTH_REQUIRED, AuthProblem, AuthUser, current_user, require_recent_auth  # noqa: F401

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

WRONG = "That email address and password don't match an account. Check both and try again."


def _too_many(seconds: float, what: str = "passwords") -> AuthProblem:
    return AuthProblem(429, "locked", f"Too many wrong {what}. Wait {ratelimit.wait_text(seconds)}, then try "
                                      "again.", headers={"Retry-After": ratelimit.retry_after(seconds)})


class FirstRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field("", max_length=200)
    email: str = Field("", max_length=1000)
    password: str = Field("", max_length=4096)
    name: Optional[str] = Field(None, max_length=1000)
    remember: bool = False
    background: bool = True  # background access: scheduled searches keep running while signed out (Phase G)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field("", max_length=1000)
    password: str = Field("", max_length=4096)
    remember: bool = False


def _end_browser_session(request: Request) -> None:
    """A new sign-in replaces the browser's old session (active, or waiting for its code) rather than leaving it
    behind."""
    db = SessionLocal()
    try:
        sessions.end_cookie_session(db, request.scope)
    finally:
        db.close()


class SignupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field("", max_length=1000)
    password: str = Field("", max_length=4096)
    name: Optional[str] = Field(None, max_length=1000)
    remember: bool = False
    invite: Optional[str] = Field(None, max_length=200)


def _signed_in(request: Request, secret: bytes, remember: bool, body: dict, status: int = 200) -> JSONResponse:
    _end_browser_session(request)
    response = JSONResponse({**body, "csrf": crypto.csrf_token(secret)}, status_code=status)
    sessions.set_cookie(response, request.scope, secret, remember)
    return response


@router.get("/state")
def auth_state(request: Request, invite: Optional[str] = Query(None, max_length=200), db: Session = Depends(get_db)):
    out = {"setup_required": not users.users_exist(), "signed_in": sessions.signed_in_user(request) is not None}
    secret = sessions.read_secret(request.scope)
    if secret and not out["signed_in"] and sessions.is_pending(secret):
        out["mfa_pending"] = True  # the password was right; the sign-in page asks for the code
    if not out["setup_required"]:  # what the sign-up page needs: the mode, and whether this invitation works
        out["signup"] = signup.public_state(db, invite)
        out["email_enabled"] = mail_queue.enabled()  # reset links by email work (the sign-in page offers them)
    return out


@router.post("/first-run", status_code=201)
def first_run(body: FirstRunIn, request: Request):
    if users.users_exist():
        raise AuthProblem(409, "already_set_up", "JobHuntPA is already set up. Sign in instead.")
    if not users.check_first_run_token(body.token):
        raise AuthProblem(403, "bad_token", "That setup code isn't right. Use the link JobHuntPA showed when it "
                                            "started, or copy the code from the file first-run.txt in its data "
                                            "folder.", field="token")
    try:
        account = users.create_first_admin(body.email, body.password, name=body.name or "", created_via="first_run",
                                           background=body.background)
    except users.AccountError as exc:
        raise AuthProblem(422, "invalid", exc.message, field=exc.field) from None
    except users.AlreadySetUp:
        raise AuthProblem(409, "already_set_up", "JobHuntPA is already set up. Sign in instead.") from None
    db = SessionLocal()
    try:
        secret = sessions.create(db, db.get(User, account.user_id), account.dek, persistent=body.remember)
        db.commit()
    finally:
        db.close()
    # Encrypt the data from before accounts with the new admin's key (a new install: next to nothing, done here;
    # a big database carries on in the background while the app shows the progress).
    migrate_encryption.ensure_started(account.user_id, account.dek, wait_s=5)
    return _signed_in(request, secret, body.remember, {"ok": True, "recovery_codes": account.recovery_codes}, 201)


@router.post("/signup", status_code=201)
def sign_up(body: SignupIn, request: Request):
    """Create an account (the admin's sign-up mode or an invitation allows it) and sign it in. The new person sees
    their recovery codes next, then the wizard, as after the first run."""
    if not users.users_exist():
        raise AuthProblem(409, "setup_required", "JobHuntPA isn't set up yet: create the admin account first.")
    account = signup.sign_up(body.email, body.password, name=body.name or "", invite=body.invite)
    account_email.verify_after_sign_up(account.user_id, body.email)  # when email is set up and it isn't verified yet
    db = SessionLocal()
    try:
        secret = sessions.create(db, db.get(User, account.user_id), account.dek, persistent=body.remember)
        db.commit()
    finally:
        db.close()
    return _signed_in(request, secret, body.remember, {"ok": True, "recovery_codes": account.recovery_codes}, 201)


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    server = users.server_keys()
    user = users.find_by_email(db, body.email)
    if user is None:
        key = server.email_hash(body.email)
        wait = ratelimit.unknown_emails.locked_for(key)
        if wait:
            raise _too_many(wait)
        users.dummy_derivation(body.password)
        ratelimit.unknown_emails.fail(key)
        raise AuthProblem(401, "wrong_password", WRONG)
    wait = users.is_locked(user)
    if wait:
        raise _too_many(wait)
    dek = users.unlock_with_password(db, user, body.password)
    if dek is None:
        users.record_failed_attempt(db, user)
        log.info("Sign-in failed for user %s (%d in a row)", user.id, user.failed_logins)
        raise AuthProblem(401, "wrong_password", WRONG)
    if user.status != "active":
        user.failed_logins, user.locked_until = 0, None
        db.commit()
        raise AuthProblem(403, "disabled", "This account is turned off. Ask the person who runs this JobHuntPA.")
    if user.mfa_enabled:
        # Right password, but the data stays closed until the code is checked: a pending session holds the key
        # under its own label (backend/sessions.py). The failure count is kept, so wrong codes across several
        # sign-ins still add up to a wait; it is cleared when a code is right.
        secret = sessions.create_pending(db, user, dek, persistent=body.remember)
        db.commit()
        log.info("Password accepted for user %s; waiting for the two-step code", user.id)
        _end_browser_session(request)
        response = JSONResponse({"ok": True, "mfa_required": True})  # no CSRF token: nothing can be changed yet
        sessions.set_cookie(response, request.scope, secret, False)
        return response
    user.failed_logins, user.locked_until = 0, None
    secret = sessions.create(db, user, dek, persistent=body.remember)
    db.commit()
    log.info("Signed in: user %s", user.id)
    if not migrate_encryption.known_done():  # their key may be the one the encryption run is waiting for
        migrate_encryption.ensure_started(user.id, dek, wait_s=0)
    return _signed_in(request, secret, body.remember, {"ok": True, "user": {"id": user.id, "role": user.role}})


class MfaLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[str] = Field(None, max_length=64)           # the 6 digits from the authenticator app
    recovery_code: Optional[str] = Field(None, max_length=64)  # or one of the 8 recovery codes (used up)


def _mfa_expired(detail: str = "That sign-in took too long. Enter your email and password again.") -> AuthProblem:
    return AuthProblem(401, "mfa_expired", detail)


def _after_sign_in(user_id: int, dek: bytes) -> None:
    if not migrate_encryption.known_done():  # their key may be the one the encryption run is waiting for
        migrate_encryption.ensure_started(user_id, dek, wait_s=0)


@router.post("/login/mfa")
def login_mfa(body: MfaLoginIn, request: Request, db: Session = Depends(get_db)):
    """The second step of signing in: the code from the authenticator app, or a recovery code instead. Only a
    ``pending_mfa`` session gets here (the cookie of the login step); the session that comes out is a new one."""
    typed_code, typed_recovery = (body.code or "").strip(), (body.recovery_code or "").strip()
    if bool(typed_code) == bool(typed_recovery):
        raise AuthProblem(422, "invalid", "Enter the 6-digit code from your authenticator app, or a recovery code.",
                          field="code")
    secret = sessions.read_secret(request.scope)
    pending = sessions.load_pending(db, secret) if secret else None
    if pending is None:
        raise _mfa_expired()
    sid, remember = pending.id, bool(pending.persistent)
    user = db.get(User, pending.user_id)
    if user is None or user.status != "active" or not user.mfa_enabled:
        sessions.end(db, sid)
        raise _mfa_expired()
    wait = users.is_locked(user)
    if wait:
        raise _too_many(wait, "codes")
    # This try is counted before the code is looked at, so parallel guesses can't get past the limit of 5.
    attempts = sessions.count_mfa_attempt(db, sid)
    if attempts > sessions.MFA_ATTEMPTS:
        sessions.end(db, sid)
        raise _mfa_expired("Too many wrong codes. Enter your email and password again.")
    recovery_id: Optional[int] = None
    if typed_code:
        good = users.verify_totp(db, user, typed_code)
    else:
        opened = users.open_recovery_code(db, user.id, typed_recovery)
        recovery_id = opened[0] if opened else None
        good = opened is not None
    if not good:
        users.record_failed_attempt(db, user)
        log.info("Two-step code refused for user %s (try %d)", user.id, attempts)
        if attempts >= sessions.MFA_ATTEMPTS:
            sessions.end(db, sid)  # the 5th wrong code drops the pending session: the password has to be entered again
            raise _mfa_expired("Too many wrong codes. Enter your email and password again.")
        raise AuthProblem(422, "wrong_code", "That code isn't right. Check the code in your app and try again."
                          if typed_code else "That recovery code isn't right. Check it and try again.",
                          field="code" if typed_code else "recovery_code")
    dek = sessions.pending_dek(secret, pending)
    if recovery_id is not None and not users.spend_recovery_code(db, recovery_id):
        db.rollback()  # the same code was used by another request at the same moment
        raise AuthProblem(422, "wrong_recovery_code", "That recovery code isn't right. Check it and try again.",
                          field="recovery_code")
    user.failed_logins, user.locked_until = 0, None
    new_secret = sessions.create(db, user, dek, persistent=remember)
    db.delete(pending)
    db.commit()
    log.info("Signed in with two-step code: user %s%s", user.id, " (recovery code)" if recovery_id else "")
    _after_sign_in(user.id, dek)
    out: dict = {"ok": True, "user": {"id": user.id, "role": user.role}}
    if recovery_id is not None:
        out["recovery_codes_left"] = users.recovery_codes_left(db, user.id)
    return _signed_in(request, new_secret, remember, out)


class RecoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field("", max_length=1000)
    recovery_code: str = Field("", max_length=64)
    new_password: str = Field("", max_length=4096)
    code: Optional[str] = Field(None, max_length=64)                    # with two-step sign-in on: the app's code
    second_recovery_code: Optional[str] = Field(None, max_length=64)    # ... or a second recovery code
    remember: bool = False


RECOVER_FAILED = ("That email address and recovery code don't match an account. Check both and try again.")


@router.post("/recover")
def recover(body: RecoverIn, request: Request, db: Session = Depends(get_db)):
    """A forgotten password, reset with a recovery code (design §6.4). The code's own wrap of the data key opens it, and
    it is wrapped again under the new password, so the data is kept. With two-step sign-in on, a second proof is needed
    too: the app's code, or a second recovery code. The codes are used up only when everything has checked out; then
    every session of the account ends and the person is signed in."""
    try:
        new_password = users.check_password(body.new_password)
    except users.AccountError as exc:
        raise AuthProblem(422, "invalid", exc.message, field="new_password") from None
    if users.normalise_recovery_code(body.recovery_code) is None:
        raise AuthProblem(422, "invalid", "A recovery code has 16 letters and numbers, like 7K3M-QX9D-4HFA-2WBN.",
                          field="recovery_code")
    # The new password is derived first, whoever the email belongs to: every answer takes the same time.
    fresh = users.new_password_keys(new_password)
    server = users.server_keys()
    user = users.find_by_email(db, body.email)
    if user is None:
        key = server.email_hash(body.email)
        wait = ratelimit.unknown_emails.locked_for(key)
        if wait:
            raise _too_many(wait)
        ratelimit.unknown_emails.fail(key)
        raise AuthProblem(401, "wrong_recovery", RECOVER_FAILED)
    wait = users.is_locked(user)
    if wait:
        raise _too_many(wait)
    first = users.open_recovery_code(db, user.id, body.recovery_code)
    if first is None:
        users.record_failed_attempt(db, user)
        log.info("Recovery code refused for user %s (%d in a row)", user.id, user.failed_logins)
        raise AuthProblem(401, "wrong_recovery", RECOVER_FAILED)
    spend = [first[0]]
    if user.mfa_enabled:
        typed_code, typed_second = (body.code or "").strip(), (body.second_recovery_code or "").strip()
        if bool(typed_code) == bool(typed_second):
            raise AuthProblem(422, "second_factor_required", "This account uses two-step sign-in. Enter the code "
                              "from your authenticator app, or a second recovery code.", field="code")
        if typed_code:
            good = users.verify_totp(db, user, typed_code)
        else:
            second = users.open_recovery_code(db, user.id, typed_second, not_id=first[0])
            good = second is not None
            if good:
                spend.append(second[0])
        if not good:
            users.record_failed_attempt(db, user)
            log.info("Two-step proof refused in a recovery for user %s (%d in a row)", user.id, user.failed_logins)
            raise AuthProblem(422, "wrong_code", "That code isn't right. Check it and try again.",
                              field="code" if typed_code else "second_recovery_code")
    if user.status != "active":
        raise AuthProblem(403, "disabled", "This account is turned off. Ask the person who runs this JobHuntPA.")
    dek = first[1]
    try:
        for code_id in spend:
            if not users.spend_recovery_code(db, code_id):
                raise AuthProblem(401, "wrong_recovery", RECOVER_FAILED)  # used by another request just now
        users.apply_new_password(db, user, dek, fresh)
        db.query(AuthSession).filter(AuthSession.user_id == user.id).delete(synchronize_session=False)
        account_email.end_open_links(db, user.id)  # a reset link already sent must not undo this
        secret = sessions.create(db, user, dek, persistent=body.remember)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    scrub_wal()  # the old password's copy of the data key must not linger in the write-ahead log
    log.info("Password reset with a recovery code: user %s (%d code(s) used)", user.id, len(spend))
    account_email.notify_with_key(db, user.id, dek, "password_changed")
    _after_sign_in(user.id, dek)
    return _signed_in(request, secret, body.remember, {"ok": True, "user": {"id": user.id, "role": user.role},
                                                       "recovery_codes_left": users.recovery_codes_left(db, user.id)})


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    sessions.end_cookie_session(db, request.scope)  # an active session, or one still waiting for its code
    response = JSONResponse({"ok": True})
    sessions.clear_cookie(response, request.scope)
    response.headers["Clear-Site-Data"] = '"cache"'
    return response


@router.get("/me")
def me(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """A sync endpoint: it opens the user's sealed details, so it also shows the request's key scope reaches the
    thread pool."""
    info = users.details(db, user.id)
    return {
        "user": {"id": user.id, "email": info.get("email", ""), "name": info.get("name", ""), "role": user.role},
        "csrf": user.csrf,
        "recovery_codes_pending": users.pending_recovery_codes(db, user.id) is not None,
        "workspaces": users.workspace_ids(db, user.id),
        # What this version offers; admin_panel is true for admins only.
        "features": account.features(admin=user.is_admin),
        # The sign-up walkthrough (Phase K): the step to show next, NULL once finished; and the theme saved for the
        # account, which pages apply when this browser has none of its own.
        "onboarding_step": db.get(User, user.id).onboarding_step,
        "prefs": account.public_prefs(db, user.id),
    }


@router.get("/recovery-codes")
def recovery_codes(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """The codes can reset the password (``/recover``), so a session that hasn't entered the password lately can't
    read them. Sign-up, first run and the password dialog all count as recent."""
    require_recent_auth(user)
    codes = users.pending_recovery_codes(db, user.id)
    if codes is None:
        raise AuthProblem(404, "none_pending", "There are no recovery codes waiting to be saved: you confirmed "
                                               "saving them already.")
    return {"codes": codes}


@router.post("/recovery-codes/ack")
def acknowledge_codes(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    users.acknowledge_recovery_codes(db, user.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Periodic clean-up (accounts design §9; run by backend/schedule.py's loop)
# ---------------------------------------------------------------------------

TOKEN_KEEP = timedelta(days=1)     # a used or expired reset or verification link is kept this long, then deleted
INVITE_KEEP = timedelta(days=30)   # invitations stay in the admin's list this long after they end (see admin.py)


def cleanup_tick(now: Optional[datetime] = None) -> dict:
    """Deletes sessions past their idle or absolute limit and links that can't be used any more: reset and
    verification links a day after they were used or expired, invitations a month after (the admin's list shows them
    until then). Returns the counts. Needs no key."""
    now = now or utcnow()
    db = SessionLocal()
    try:
        sessions_gone = db.query(AuthSession).filter((AuthSession.absolute_expires_at <= now)
                                                    | (AuthSession.idle_expires_at <= now)
                                                    ).delete(synchronize_session=False)
        ended = (AuthToken.used_at.isnot(None)) | (AuthToken.expires_at <= now)
        tokens_gone = db.query(AuthToken).filter(AuthToken.kind != signup.INVITE_KIND, ended,
                                                 func.coalesce(AuthToken.used_at, AuthToken.expires_at)
                                                 <= now - TOKEN_KEEP).delete(synchronize_session=False)
        invites_gone = db.query(AuthToken).filter(AuthToken.kind == signup.INVITE_KIND, ended,
                                                  func.coalesce(AuthToken.used_at, AuthToken.expires_at)
                                                  <= now - INVITE_KEEP).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    if sessions_gone or tokens_gone or invites_gone:
        log.info("Clean-up: %d expired session(s), %d link(s), %d invitation(s) removed", sessions_gone, tokens_gone,
                 invites_gone)
    return {"sessions": sessions_gone, "tokens": tokens_gone, "invites": invites_gone}
