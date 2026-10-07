"""Signing in and out, and the first run (accounts design, sections 3, 5 and 6).

    GET  /api/auth/state                  {setup_required, signed_in}                      (no session needed)
    POST /api/auth/first-run              {token, email, password, name?, remember?, background?}
                                          -> 201 {csrf, recovery_codes: [8 codes, shown once]} + session cookie
    POST /api/auth/login                  {email, password, remember?} -> {csrf, user} + session cookie
    POST /api/auth/logout                 ends this session; Clear-Site-Data: "cache"
    GET  /api/auth/me                     {user: {id, email, name, role}, csrf, recovery_codes_pending, features}
    GET  /api/auth/recovery-codes         {codes}: the codes not yet confirmed as saved (404 when none)
    POST /api/auth/recovery-codes/ack     "I've saved my recovery codes": the copy kept to show them is deleted

Errors are ``{"detail": "<plain words>", "code": "...", "field"?: "email" | "password" | ...}``. A wrong password and
an unknown email address get the same answer, after the same amount of work. Login and first run go through the
checks in backend/sessions.py first (same site, JSON only, rate limit per address).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from backend import account, crypto, migrate_encryption, ratelimit, sessions, users
from backend.db import SessionLocal, User, get_db, utcnow
from backend.sessions import AuthUser, current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

WRONG = "That email address and password don't match an account. Check both and try again."


class AuthProblem(Exception):
    """An error answer in this module's shape (app.py turns it into the response)."""

    def __init__(self, status: int, code: str, detail: str, field: Optional[str] = None,
                 headers: Optional[dict] = None):
        super().__init__(detail)
        self.status, self.code, self.detail, self.field, self.headers = status, code, detail, field, headers

    def response(self) -> JSONResponse:
        body = {"detail": self.detail, "code": self.code}
        if self.field:
            body["field"] = self.field
        return JSONResponse(body, status_code=self.status, headers=self.headers)


def _too_many(seconds: float) -> AuthProblem:
    return AuthProblem(429, "locked", f"Too many wrong passwords. Wait {ratelimit.wait_text(seconds)}, then try "
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


def _signed_in(request: Request, secret: bytes, remember: bool, body: dict, status: int = 200) -> JSONResponse:
    previous = sessions.signed_in_user(request)
    if previous is not None:  # a new sign-in replaces the browser's old session rather than leaving it behind
        db = SessionLocal()
        try:
            sessions.end(db, previous.session_id)
        finally:
            db.close()
    response = JSONResponse({**body, "csrf": crypto.csrf_token(secret)}, status_code=status)
    sessions.set_cookie(response, request.scope, secret, remember)
    return response


@router.get("/state")
def auth_state(request: Request):
    return {"setup_required": not users.users_exist(), "signed_in": sessions.signed_in_user(request) is not None}


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
        db.execute(update(User).where(User.id == user.id).values(failed_logins=User.failed_logins + 1))
        db.refresh(user)
        seconds = ratelimit.lockout_seconds(user.failed_logins)
        if seconds:
            user.locked_until = utcnow() + timedelta(seconds=seconds)
        db.commit()
        log.info("Sign-in failed for user %s (%d in a row)", user.id, user.failed_logins)
        raise AuthProblem(401, "wrong_password", WRONG)
    user.failed_logins, user.locked_until = 0, None
    if user.status != "active":
        db.commit()
        raise AuthProblem(403, "disabled", "This account is turned off. Ask the person who runs this JobHuntPA.")
    if user.mfa_enabled:  # Phase E adds the code step (a pending_mfa session); until then, fail closed
        db.commit()
        raise AuthProblem(403, "mfa_unavailable", "This account uses two-step sign-in, which this version of "
                                                  "JobHuntPA can't check yet.")
    secret = sessions.create(db, user, dek, persistent=body.remember)
    db.commit()
    log.info("Signed in: user %s", user.id)
    if not migrate_encryption.known_done():  # their key may be the one the encryption run is waiting for
        migrate_encryption.ensure_started(user.id, dek, wait_s=0)
    return _signed_in(request, secret, body.remember, {"ok": True, "user": {"id": user.id, "role": user.role}})


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = sessions.signed_in_user(request)
    if user is not None:
        sessions.end(db, user.session_id)
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
        # What this version offers; the account menu shows "Admin" once the admin panel exists (Phase F).
        "features": account.features(),
        # The sign-up walkthrough (Phase K): the step to show next, NULL once finished; and the theme saved for the
        # account, which pages apply when this browser has none of its own.
        "onboarding_step": db.get(User, user.id).onboarding_step,
        "prefs": account.public_prefs(db, user.id),
    }


@router.get("/recovery-codes")
def recovery_codes(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    codes = users.pending_recovery_codes(db, user.id)
    if codes is None:
        raise AuthProblem(404, "none_pending", "There are no recovery codes waiting to be saved: you confirmed "
                                               "saving them already.")
    return {"codes": codes}


@router.post("/recovery-codes/ack")
def acknowledge_codes(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    users.acknowledge_recovery_codes(db, user.id)
    return {"ok": True}
