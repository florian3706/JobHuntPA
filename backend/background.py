"""Background access: letting the server open your data while you're signed out (accounts design 1.2, 6.7).

    GET  /api/account/background   {enabled, since}: whether the server holds a copy of this account's key, and
                                   since when (ISO time, or null)
    POST /api/account/background   {enabled: true | false} -> the same answer

**On** needs the password entered in the last 15 minutes (``require_recent_auth``, answered 403
``reauth_required``; the page asks again and retries). The signed-in request's data key is wrapped under the
server KEK into ``user_keys.server_wrapped_dek`` (with ``server_kek_id`` and ``background_since``), with the same
label as at sign-up (``srv|{uid}``). Scheduled searches (backend.schedule) use it.

**Off** sets all three to NULL, destroying the server's copy (``PRAGMA secure_delete`` is on). It needs no extra
sign-in: giving access away is always allowed. Scheduled searches then pass over the account's slots, saying why.

An account that has no identity key yet (it hasn't entered its password since the update) is answered 409
``needs_sign_in``: its private key is still under the data key, which the server key would then open.

Both work on the signed-in account only; there is no way to change another account's setting (an admin's
included), because the key that's wrapped is the one the person's own session holds.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend import crypto, users
from backend.db import UserKeys, get_db
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


class BackgroundIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def background_info(db: Session, user_id: int) -> dict:
    row = db.get(UserKeys, user_id)
    on = row is not None and row.server_wrapped_dek is not None
    since = row.background_since if on else None
    return {"enabled": on, "since": since.isoformat() + "Z" if since else None}


@router.get("/background")
def get_background(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    return background_info(db, user.id)


@router.post("/background")
def set_background(body: BackgroundIn, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    if not body.enabled:
        users.disable_background(db, user.id)
        return background_info(db, user.id)
    require_recent_auth(user)
    scope = crypto.current()  # the request's key: this session's own, never a stored one
    if scope.user_id != user.id:
        raise AuthProblem(403, "wrong_account", "This request isn't for your account.")
    row = db.get(UserKeys, user.id)
    if row is None or row.pw_wrapped_ik is None:  # no identity key yet: the server key would open its private key
        raise AuthProblem(409, "needs_sign_in", "Background access can't be turned on yet. Sign out, sign in again "
                                                "and try again.")
    try:
        users.enable_background(db, user.id, scope.dek)
    except ValueError:
        log.error("user %s: background access refused: the session's key isn't the account's", user.id)
        raise AuthProblem(409, "key_mismatch", "Background access couldn't be turned on. Sign out, sign in again "
                                               "and try again.") from None
    return background_info(db, user.id)
