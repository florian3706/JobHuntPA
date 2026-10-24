"""Signing in and out, and the first run (accounts design, sections 3, 5 and 6; open-source spec §1.2).

    GET  /api/auth/state                  {setup_required, folder_required, signed_in} (open)
    POST /api/auth/data-folder[/check]    the first run's step 1, where the data lives: backend/data_folder.py
    POST /api/auth/first-run              {token, name, password, remember?, background?} (open, and only while no
                                          account exists: 409 already_set_up after that)
                                          -> 201 {csrf, recovery_codes: [8 codes, shown once]} + session cookie
    POST /api/auth/login                  {name, password, remember?} (open) -> {csrf, user} + session cookie
    POST /api/auth/recover                {name, recovery_code, new_password} (open)
                                          -> {csrf, user, recovery_codes_left} + session cookie: a forgotten password
                                          reset with a recovery code, and the data kept
    POST /api/auth/logout                 ends this session; Clear-Site-Data: "cache" (open)
    GET  /api/auth/me                     {user: {id, name}, csrf, recovery_codes_pending, features} (signed in)
    POST /api/auth/recovery-codes/show    {password} -> {codes}: the codes not yet confirmed as
                                          saved (404 when none); new ones are made with POST /api/auth/recovery-codes
                                          (both are in backend/security.py and need the password)
    POST /api/auth/recovery-codes/ack     "I've saved my recovery codes": the copy kept to show them is deleted
                                          (signed in)

**One account per install.** The first run makes it; there is no sign-up and no invitation, and once an account
exists nothing here makes another (``users.create_account`` refuses under the database's write lock). ``name`` is
the sign-in name; on an install from before the open-source version, the email address an account was made with is
its name, so ``email`` is still accepted in place of ``name`` by sign-in and recovery.

Errors are ``{"detail": "<plain words>", "code": "...", "field"?: "name" | "password" | ...}``. A wrong password and
an unknown name get the same answer, after the same amount of work and the same time (every refusal waits for
``users.derivation_floor()``). Login, recover and first run go through the checks in backend/sessions.py first (same
site, JSON only, rate limit per address). Wrong passwords and codes are counted per account and per source before the
slow check (backend/lockout.py): five in a row from one network address or one browser that has signed in before make
that source wait, never the whole account. Every sign-in sets the browser's ``jh_device`` cookie.

Changing the password and the name, new recovery codes and signing out elsewhere are in backend/security.py. There is
no second sign-in step and no email: a forgotten password is reset with a recovery code (``/recover``), and without
one the reset tool on the computer itself starts over (backend/reset.py).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend import account, crypto, lockout, migrate_encryption, migrate_templates, sessions, users
from backend import db as database
from backend.db import AuthSession, SessionLocal, User, UserKeys, get_db, scrub_wal, utcnow
from backend.sessions import REAUTH_REQUIRED, AuthProblem, AuthUser, current_user, require_recent_auth  # noqa: F401

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

WRONG = "That name and password don't match an account. Check both and try again."
DISABLED = "This account is turned off, so it can't be used."
# The sign-in name; an older install's names are email addresses, and older pages and scripts sent them as "email".
NAME_FIELD = Field("", max_length=1000, validation_alias=AliasChoices("name", "email"))


SLOW_REFUSALS = frozenset({"wrong_password", "wrong_recovery", "locked"})  # padded to the same time: users.pad_failure


class FirstRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field("", max_length=200)
    name: str = Field("", max_length=1000)
    password: str = Field("", max_length=4096)
    remember: bool = False
    background: bool = True  # background access: scheduled searches keep running while signed out (Phase G)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = NAME_FIELD
    password: str = Field("", max_length=4096)
    remember: bool = False


def _end_browser_session(request: Request) -> None:
    """A new sign-in replaces the browser's old session rather than leaving it behind."""
    db = SessionLocal()
    try:
        sessions.end_cookie_session(db, request.scope)
    finally:
        db.close()


def _signed_in(request: Request, secret: bytes, remember: bool, body: dict, status: int = 200, *,
               user_id: int) -> JSONResponse:
    _end_browser_session(request)
    response = JSONResponse({**body, "csrf": crypto.csrf_token(secret)}, status_code=status)
    sessions.set_cookie(response, request.scope, secret, remember)
    lockout.remember_device(response, request.scope, user_id)  # (wrong tries by others never hold this browser up)
    return response


@router.get("/state")
def auth_state(request: Request):
    """Open: whether the first run is still to do (``folder_required``: its step 1, where the data lives, comes first;
    backend/data_folder.py), and whether this browser is signed in."""
    if not database.FOLDER_CHOSEN:
        return {"setup_required": True, "folder_required": True, "signed_in": False}
    return {"setup_required": not users.users_exist(), "folder_required": False,
            "signed_in": sessions.signed_in_user(request) is not None}


@router.post("/first-run", status_code=201)
def first_run(body: FirstRunIn, request: Request):
    """Open, with the one-time token, and only while no account exists: makes this install's one account."""
    if users.users_exist():
        raise AuthProblem(409, "already_set_up", "JobHuntPA is already set up. Sign in instead.")
    if not users.check_first_run_token(body.token):
        raise AuthProblem(403, "bad_token", "That setup code isn't right. Use the link JobHuntPA showed when it "
                                            "started, or copy the code from the file first-run.txt in its data "
                                            "folder.", field="token")
    try:
        account = users.create_account(body.name, body.password, created_via="first_run", background=body.background)
    except users.AccountError as exc:
        raise AuthProblem(422, "invalid", exc.message, field=exc.field) from None
    except users.AlreadySetUp:
        raise AuthProblem(409, "already_set_up", "JobHuntPA is already set up. Sign in instead.") from None
    db = SessionLocal()
    try:
        secret = sessions.create(db, db.get(User, account.user_id), account.dek, account.ik, persistent=body.remember)
        db.commit()
    finally:
        db.close()
    # Encrypt the data from before accounts with the new account's key (a new install: next to nothing, done here;
    # a big database carries on in the background while the app shows the progress).
    migrate_encryption.ensure_started(account.user_id, account.dek, wait_s=5)
    return _signed_in(request, secret, body.remember, {"ok": True, "recovery_codes": account.recovery_codes}, 201,
                      user_id=account.user_id)


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    began = time.monotonic()
    try:
        return _login(body, request, db)
    except AuthProblem as problem:
        if problem.code in SLOW_REFUSALS:
            users.pad_failure(began)  # an address with no account is refused as slowly as a wrong password
        raise


def _login(body: LoginIn, request: Request, db: Session):
    server = users.server_keys()
    user = users.find_by_name(db, body.name)
    # The try is counted before the slow password check, so tries sent at the same moment can't all get past the
    # limit; a right password gives it back. The count is for this source (this browser or network address), never
    # the whole account, so an outsider can't keep the person out (backend/lockout.py).
    ticket = lockout.begin(request.scope, user, server.email_hash(body.name))
    if user is None:
        users.dummy_derivation(body.password)
        raise AuthProblem(401, "wrong_password", WRONG)
    unlocked = users.unlock(db, user, body.password)  # (an account from before the identity key gets one here)
    if unlocked is None:
        log.info("Sign-in failed for user %s (%d in a row)", user.id, ticket.failures)
        raise AuthProblem(401, "wrong_password", WRONG)
    dek, ik = unlocked.dek, unlocked.ik
    if user.status != "active":
        ticket.succeed()
        raise AuthProblem(403, "disabled", DISABLED)
    ticket.succeed()
    secret = sessions.create(db, user, dek, ik, persistent=body.remember)
    db.commit()
    log.info("Signed in: user %s", user.id)
    _after_sign_in(user.id, dek)
    return _signed_in(request, secret, body.remember, {"ok": True, "user": {"id": user.id}}, user_id=user.id)


def _after_sign_in(user_id: int, dek: bytes) -> None:
    if not migrate_encryption.known_done():  # their key may be the one the encryption run is waiting for
        migrate_encryption.ensure_started(user_id, dek, wait_s=0)
    if migrate_templates.pending():  # a development build's letter templates: theirs become documents now
        try:
            migrate_templates.run(user_id, dek)
        except Exception as exc:  # never in the way of signing in; tried again at the next sign-in
            log.warning("Carrying the old letter templates into the documents failed: %s", exc.__class__.__name__)


class RecoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = NAME_FIELD
    recovery_code: str = Field("", max_length=64)
    new_password: str = Field("", max_length=4096)
    remember: bool = False


RECOVER_FAILED = ("That name and recovery code don't match an account. Check both and try again.")


@router.post("/recover")
def recover(body: RecoverIn, request: Request, db: Session = Depends(get_db)):
    """A forgotten password, reset with a recovery code (design §6.4). The code's own wrap of the data key opens it, and
    it is wrapped again under the new password, so the data is kept. The code is used up only when everything has
    checked out; then every session of the account ends and the person is signed in."""
    began = time.monotonic()
    try:
        return _recover(body, request, db)
    except AuthProblem as problem:
        if problem.code in SLOW_REFUSALS:
            users.pad_failure(began)
        raise


def _recover(body: RecoverIn, request: Request, db: Session):
    try:
        new_password = users.check_password(body.new_password)
    except users.AccountError as exc:
        raise AuthProblem(422, "invalid", exc.message, field="new_password") from None
    if users.normalise_recovery_code(body.recovery_code) is None:
        raise AuthProblem(422, "invalid", "A recovery code has 16 letters and numbers, like 7K3M-QX9D-4HFA-2WBN.",
                          field="recovery_code")
    # The new password is derived first, whoever the name belongs to: every answer takes the same time.
    fresh = users.new_password_keys(new_password)
    server = users.server_keys()
    user = users.find_by_name(db, body.name)
    # Counted before the code is looked at, per source like a sign-in. No ceiling over the whole account: a code has
    # 80 random bits and can't be guessed, so this is no way in, and it must stay open for the person who lost their
    # password while someone else is guessing (backend/lockout.py).
    ticket = lockout.begin(request.scope, user, server.email_hash(body.name), ceiling=False)
    if user is None:
        raise AuthProblem(401, "wrong_recovery", RECOVER_FAILED)
    first = users.open_recovery_code(db, user.id, body.recovery_code)
    if first is None:
        log.info("Recovery code refused for user %s (%d in a row)", user.id, ticket.failures)
        raise AuthProblem(401, "wrong_recovery", RECOVER_FAILED)
    if user.status != "active":
        ticket.refund()
        raise AuthProblem(403, "disabled", DISABLED)
    dek, ik = first[1], first[2]
    try:
        if not users.spend_recovery_code(db, first[0]):
            raise AuthProblem(401, "wrong_recovery", RECOVER_FAILED)  # used by another request just now
        if ik is None:  # the code was made before the identity key existed, so it can't open it
            ik = _identity_without_code(db, user, dek, fresh.derived.kek)
        users.apply_new_password(db, user, dek, ik, fresh)
        db.query(AuthSession).filter(AuthSession.user_id == user.id).delete(synchronize_session=False)
        secret = sessions.create(db, user, dek, ik, persistent=body.remember)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    scrub_wal()  # the old password's copy of the keys must not linger in the write-ahead log
    log.info("Password reset with a recovery code: user %s", user.id)
    _after_sign_in(user.id, dek)
    return _signed_in(request, secret, body.remember, {"ok": True, "user": {"id": user.id},
                                                       "recovery_codes_left": users.recovery_codes_left(db, user.id)},
                      user_id=user.id)


def _identity_without_code(db: Session, user: User, dek: bytes, password_kek: bytes) -> bytes:
    """The IK for a recovery reset with a code that carries no identity key. An account from before the IK is given
    one now: the code opens the DEK, which opens the private key, so nothing is lost. An account that already has one
    can't open it with this code, so it gets a new key pair and IK (``users.replace_identity``); its data is kept (it is
    under the DEK), and only the unconfirmed recovery codes, kept under the old IK, are dropped. In ``db``'s
    transaction."""
    row = db.get(UserKeys, user.id)
    if row.pw_wrapped_ik is None:
        try:
            return users.migrate_identity(db, row, user.id, dek, password_kek)
        except crypto.CryptoError:
            log.warning("user %s: the private key didn't open with the data key; making a new key pair", user.id)
    return users.replace_identity(db, user.id, row, password_kek)


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    sessions.end_cookie_session(db, request.scope)
    response = JSONResponse({"ok": True})
    sessions.clear_cookie(response, request.scope)
    response.headers["Clear-Site-Data"] = '"cache"'
    return response


@router.get("/me")
def me(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Signed in. A sync endpoint: it opens the user's sealed details, so it also shows the request's key scope reaches
    the thread pool."""
    info = users.details(db, user.id)
    return {
        "user": {"id": user.id, "name": info.get("name", "")},
        "csrf": user.csrf,
        "recovery_codes_pending": users.has_pending_recovery_codes(db, user.id),
        "workspaces": users.workspace_ids(db, user.id),
        # What this version offers (the walkthrough's optional steps).
        "features": account.features(),
        # The sign-up walkthrough (Phase K): the step to show next, NULL once finished; and the theme saved for the
        # account, which pages apply when this browser has none of its own.
        "onboarding_step": db.get(User, user.id).onboarding_step,
        "prefs": account.public_prefs(db, user.id),
    }


@router.post("/recovery-codes/ack")
def acknowledge_codes(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    users.acknowledge_recovery_codes(db, user.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Periodic clean-up (run by backend/schedule.py's loop)
# ---------------------------------------------------------------------------

def cleanup_tick(now: Optional[datetime] = None) -> dict:
    """Deletes sessions (and browser-extension keys) past their idle or absolute limit. Returns the count. Needs no
    key."""
    now = now or utcnow()
    db = SessionLocal()
    try:
        sessions_gone = db.query(AuthSession).filter((AuthSession.absolute_expires_at <= now)
                                                    | (AuthSession.idle_expires_at <= now)
                                                    ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    if sessions_gone:
        log.info("Clean-up: %d expired session(s) removed", sessions_gone)
    return {"sessions": sessions_gone}
