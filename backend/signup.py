"""Who may create an account (accounts design §4, §6.1 and §7): the sign-up mode, invitations and the checks of
``POST /api/auth/signup``.

``app_settings["signup_mode"]``:
  * ``invite`` (the default): only someone with a valid invitation link;
  * ``open``: anyone (an invitation still works, and pre-fills the address);
  * ``closed``: nobody, not even with an invitation.
``app_settings["free_searches"]`` is how many free searches a new account gets (3; ``membership.charge_search`` gives them out).

An invitation is an ``auth_tokens`` row of kind ``invite`` (backend/tokens.py): 7 days, single use, only the hash of its
token stored, and a payload sealed under a key made from the token: ``{email, emailed}``, the address it was made for
(if the admin gave one) and whether JobHuntPA actually sent the invitation to it. Only an invitation that was emailed
to the address (the mail server took it: ``mark_emailed``, set by the mail job) makes that address count as verified
when the invited person signs up with it; a link the admin copied and sent some other way proves nothing about the
mailbox, so the new account gets the usual verification email.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import migrate_encryption, tokens, users
from backend.access import FREE_SEARCHES
from backend.db import AuthToken, SessionLocal, User
from backend.sessions import AuthProblem

log = logging.getLogger(__name__)

MODES = ("invite", "open", "closed")
DEFAULT_MODE = "invite"
INVITE_KIND = "invite"
INVITE_TTL = timedelta(days=7)
MAX_FREE_SEARCHES = 1000

INVITE_NEEDED = ("You need an invitation to sign up. Ask the person who runs this JobHuntPA for an invite link.")
INVITE_INVALID = ("That invitation link isn't valid any more (it may have been used or have expired). Ask the person "
                  "who runs this JobHuntPA for a new one.")
CLOSED = "Sign-up is closed. Ask the person who runs this JobHuntPA for help."
ENCRYPTING = "JobHuntPA is still encrypting its data. Try signing up again in a few minutes."


def mode(db: Session) -> str:
    value = users._setting(db, "signup_mode", DEFAULT_MODE)
    return value if value in MODES else DEFAULT_MODE


def free_searches(db: Session) -> int:
    value = users._setting(db, "free_searches", FREE_SEARCHES)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_FREE_SEARCHES \
        else FREE_SEARCHES


def set_mode(db: Session, value: str) -> None:
    if value not in MODES:
        raise ValueError("Unknown sign-up mode")
    users._put_setting(db, "signup_mode", value)


def set_free_searches(db: Session, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_FREE_SEARCHES:
        raise ValueError("Free searches must be a whole number from 0 to 1000")
    users._put_setting(db, "free_searches", value)


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
        invited_for = None  # the address the invitation was emailed to (None: it wasn't emailed, or has no address)
        if row is not None:
            used = tokens.consume(db, INVITE_KIND, invite)  # one request wins when two use the same link
            if used is None:
                raise AuthProblem(403, "invite_invalid", INVITE_INVALID)
            payload = tokens.open_payload(invite, INVITE_KIND, used.payload_enc) or {}
            if payload.get("emailed") is True and isinstance(payload.get("email"), str):
                invited_for = payload["email"]
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
            _after_invite(db, used.id, account.user_id,
                          verified=invited_for is not None and _same_address(invited_for, clean_email))
        return account
    finally:
        db.close()


DUPLICATE = "An account with this email already exists. Sign in or reset your password."


def _same_address(a: str, b: str) -> bool:
    from backend import keys

    return keys.normalise_email(a) == keys.normalise_email(b)


def mark_emailed(token_id: int, token: str, email: str) -> None:
    """The mail server took the invitation for ``email``: from now on signing up with that address counts as
    verified. Runs on the mail thread after the send; the invitation's payload is sealed again with the flag."""
    db = SessionLocal()
    try:
        db.execute(update(AuthToken).where(AuthToken.id == token_id, AuthToken.kind == INVITE_KIND,
                                           AuthToken.used_at.is_(None))
                   .values(payload_enc=tokens.seal_payload(token, INVITE_KIND, {"email": email, "emailed": True})))
        db.commit()
    finally:
        db.close()


def _after_invite(db: Session, token_id: int, user_id: int, *, verified: bool) -> None:
    """Note who used the invitation; an address the invitation was emailed to counts as verified (design §6.1)."""
    db.execute(update(AuthToken).where(AuthToken.id == token_id).values(user_id=user_id))
    if verified:
        db.execute(update(User).where(User.id == user_id).values(email_verified=True))
    db.commit()
