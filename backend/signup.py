"""Who may create an account (accounts design §4, §6.1 and §7): the sign-up mode, invitations and the checks of
``POST /api/auth/signup``.

``app_settings["signup_mode"]``:
  * ``invite`` (the default): only someone with a valid invitation link;
  * ``open``: anyone (an invitation still works, and pre-fills the address);
  * ``closed``: nobody, not even with an invitation.

An invitation is an ``auth_tokens`` row of kind ``invite`` (backend/tokens.py): 7 days, single use, only the hash of its
token stored, and a payload sealed under a key made from the token: ``{email}``, the address it was made for (if the
admin gave one), which pre-fills the sign-up form. The admin copies the link and sends it themselves: JobHuntPA sends
no email.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import migrate_encryption, tokens, users
from backend.db import AuthToken, SessionLocal
from backend.sessions import AuthProblem

log = logging.getLogger(__name__)

MODES = ("invite", "open", "closed")
DEFAULT_MODE = "invite"
INVITE_KIND = "invite"
INVITE_TTL = timedelta(days=7)

INVITE_NEEDED = ("You need an invitation to sign up. Ask the person who runs this JobHuntPA for an invite link.")
INVITE_INVALID = ("That invitation link isn't valid any more (it may have been used or have expired). Ask the person "
                  "who runs this JobHuntPA for a new one.")
CLOSED = "Sign-up is closed. Ask the person who runs this JobHuntPA for help."
ENCRYPTING = "JobHuntPA is still encrypting its data. Try signing up again in a few minutes."


def mode(db: Session) -> str:
    value = users._setting(db, "signup_mode", DEFAULT_MODE)
    return value if value in MODES else DEFAULT_MODE


def set_mode(db: Session, value: str) -> None:
    if value not in MODES:
        raise ValueError("Unknown sign-up mode")
    users._put_setting(db, "signup_mode", value)


def encrypting() -> bool:
    """The one-time encryption of the data from before accounts is still to do (sign-ups wait)."""
    return not migrate_encryption.is_done()


def invite_info(db: Session, token: Any) -> dict:
    """{valid, email}: whether ``token`` is an invitation that can still be used, and the address it was made for
    (None when the admin gave none). Doesn't use it up."""
    row = tokens.find(db, INVITE_KIND, token)
    if row is None:
        return {"valid": False, "email": None}
    payload = tokens.open_payload(token, INVITE_KIND, row.payload_enc) or {}
    email = payload.get("email")
    return {"valid": True, "email": email if isinstance(email, str) and email else None}


def public_state(db: Session, invite: Optional[str]) -> dict:
    """What ``GET /api/auth/state`` says about signing up, to anyone."""
    current = mode(db)
    info = invite_info(db, invite) if invite else {"valid": False, "email": None}
    busy = encrypting()
    allowed = not busy and (current == "open" or (current == "invite" and info["valid"]))
    return {"mode": current, "allowed": allowed, "encrypting": busy, "invite": info}


def sign_up(email: Any, password: Any, *, name: Any = "", invite: Optional[str] = None) -> users.NewAccount:
    """Create an account if the mode (or the invitation) allows it. Raises AuthProblem with a plain message: 423
    while encrypting, 403 for a closed sign-up or a missing or invalid invitation, 422 for a bad value, 409 for an
    address that already has an account. An invitation is used up only if the account is made."""
    if encrypting():
        raise AuthProblem(423, "encrypting", ENCRYPTING)
    db = SessionLocal()
    try:
        current = mode(db)
        if current == "closed":
            raise AuthProblem(403, "signup_closed", CLOSED)
        row = tokens.find(db, INVITE_KIND, invite) if invite else None
        if current == "invite" and row is None:  # (in open mode a link that no longer works is simply not needed)
            raise AuthProblem(403, "invite_invalid" if invite else "invite_needed",
                              INVITE_INVALID if invite else INVITE_NEEDED)
        try:
            clean_email = users.clean_email(email)
            users.check_password(password)
            users.clean_name(name)
        except users.AccountError as exc:
            raise AuthProblem(422, "invalid", exc.message, field=exc.field) from None
        if users.find_by_email(db, clean_email) is not None:
            raise AuthProblem(409, "email_taken", DUPLICATE, field="email")
        if row is not None:
            used = tokens.consume(db, INVITE_KIND, invite)  # one request wins when two use the same link
            if used is None:
                raise AuthProblem(403, "invite_invalid", INVITE_INVALID)
        try:
            account = users.create_user(clean_email, password, name=name, created_via="invite" if row else "open")
        except users.AccountError as exc:
            if row is not None:
                tokens.release(db, used.id)
            status = 409 if exc.message == DUPLICATE else 422
            raise AuthProblem(status, "email_taken" if status == 409 else "invalid", exc.message,
                              field=exc.field) from None
        except IntegrityError:  # the same address was signed up by someone else a moment ago
            if row is not None:
                tokens.release(db, used.id)
            raise AuthProblem(409, "email_taken", DUPLICATE, field="email") from None
        except BaseException:
            if row is not None:
                tokens.release(db, used.id)
            raise
        if row is not None:
            _after_invite(db, used.id, account.user_id)
        return account
    finally:
        db.close()


DUPLICATE = "An account with this email already exists. Sign in instead."


def _after_invite(db: Session, token_id: int, user_id: int) -> None:
    """Note who used the invitation."""
    db.execute(update(AuthToken).where(AuthToken.id == token_id).values(user_id=user_id))
    db.commit()
