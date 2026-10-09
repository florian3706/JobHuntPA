"""Forgot password by email (accounts design §6.5 and §13 item 1) and the reset the link leads to.

    POST /api/auth/forgot        {email} -> 202 {ok, message}, the same for every address that looks like one
    POST /api/auth/reset/info    {token} -> {keeps_data, mfa_required}
    POST /api/auth/reset         {token, new_password, code? | recovery_code?, accept_wipe?, remember?}
                                 -> {ok, user, kept_data, wiped, recovery_codes?, admin_key_lost} + session cookie

All three need no session (``sessions.PRE_SESSION``: JSON only, same-site Origin, rate-limited per client address).
The page is ``reset-password.html``; the link in the email carries the token in the fragment
(``#token=...``), so it never reaches a log or a Referer header.

**Forgot.** Always 202, whether or not an account has the address, with the same body and about the same work: the
request looks the address up, counts it against a per-address limit, and queues a job on the mail thread
(``mail_queue``); for an address with no account the request makes a token and throws it away so it does the same
hashing either way, and the job is a pause as long as a typical send (``mail_queue.decoy``), so the mail thread is
as busy afterwards and the wait for the next email can't show which addresses have accounts. The job of a real
account makes the token and sends the link. Refused by a limit or because the account is turned off: still 202,
nothing sent. Only "email isn't set up" is told (409), which says nothing about any account.
The limits (3 an hour and 6 a day per address and per account, ``ratelimit``) are for anonymous requests only; the
admin's "send a reset email" has its own (``RESET_BY_ADMIN``). A request that the limits would refuse still goes out
when the account has no link that can be used (every earlier one was used or has expired), so someone who keeps the
limits used up can't leave the owner without a working link; at most one such extra email goes out per hour.
Requests held back are logged by account id.

**The token** is an ``auth_tokens`` row of kind ``reset``: 32 random bytes signed with the server key and the
account's id (``link_token``), 1 hour, single use, only its hash stored, and a payload (sealed under a key made from
the token) with the address it went to. The signature is what stops someone who can only write the database from making
a link for another account: a row they add (or an account id they change) has no token that verifies. With background
access the address must also be the one in the account's own sealed details (``pii_self``): the database's
``email_hash`` is not enough to point a link somewhere else (docs/design/accounts.md §2.5). A new link replaces any older
unused one, and so does changing the address, the password or resetting with a recovery code (``account_email.
end_open_links``); a link whose address is no longer the account's is refused. The address in the payload is what a
wipe rebuilds ``pii_self`` from, and where the "password changed" notice goes.

**Reset.**
  * With two-step sign-in on, the app's code (or a recovery code, which is used up) is required: the secret is under
    the server key, so it can be checked here (§3 item 6). Wrong codes count like wrong passwords (5 in a row from
    one place make that place wait: ``lockout``), and, whoever sends them, about 10 an hour make every place that
    isn't a known browser of the account wait, and 5 on one link use the link up (410: ask for a new one).
  * With background access the server opens the data key, wraps it under the new password and keeps everything
    (``users.reset_password_keeping_data``) **except the recovery codes**: all eight are replaced, because each opens
    the same data key and the person who held the account before (someone who signed up with this address first)
    may have kept them. The answer carries the new codes (``recovery_codes``), shown once like after a wipe. The
    server key can't open the identity key (accounts design §1.2), so the account also gets a new key pair and
    identity key: an admin loses their share of the admin details key (``admin_key_lost``), as after a wipe, and
    another admin shares it again.
  * Without it the data is locked with the old password: refused (409 ``wipe_not_accepted``) unless ``accept_wipe``,
    then everything saved is deleted and the account starts again (``users.reset_password_wiping_data``). The
    response carries the new recovery codes, shown once; ``admin_key_lost`` says an admin lost their share of the
    admin details key.
  * Either way every session ends, a "password changed" notice is sent, and **the person is signed in**: they proved
    the mailbox (and the code, with two-step on) and just chose the password, the same as ``/api/auth/recover``; and
    the new recovery codes (after a wipe or a reset that keeps the data) can only be shown to someone signed in.
The token is used up before anything changes, and given back if the change fails.
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from backend import (account_email, admin_key, crypto, email_templates, keys, lockout, mail_queue, ratelimit, sessions,
                     tokens, users)
from backend.auth import _after_sign_in, _signed_in
from backend.db import AuthToken, SessionLocal, User, get_db, utcnow
from backend.keys import normalise_email
from backend.security import SecondFactorIn, check_second_factor
from backend.sessions import AuthProblem

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["password-reset"])

RESET_KIND = account_email.RESET_KIND
RESET_TTL = timedelta(hours=1)

SENT = ("If an account has that address, we've sent a link to it. The link works once and expires in 1 hour. Check "
        "your spam folder, and wait a few minutes before asking again.")
LINK_USED_UP = ("Too many wrong codes were entered for this link, so it can't be used any more. Ask for a new one on "
                "the sign-in page.")
LINK_INVALID = ("This link isn't valid any more: it may have expired or been used already. Ask for a new one on the "
                "sign-in page.")
DISABLED = "This account is turned off. Ask the person who runs this JobHuntPA."
WIPE_NEEDED = ("Your saved searches, documents and jobs are locked with your old password. Setting a new password "
               "without a recovery code deletes them, and they can't be brought back. Tick the box to go ahead.")


# ---------------------------------------------------------------------------
# Making and sending the link (the forgot-password form and the admin's button)
# ---------------------------------------------------------------------------

def _purpose(user_id: int) -> str:
    return f"reset_u{int(user_id)}"


def link_token(user_id: int) -> str:
    """A new reset token for the account: random letters, then the server key's signature of them (the account's id is
    part of what is signed). Letters, digits, - and _ only, like every token in a link."""
    return users.server_keys().sign(_purpose(user_id), tokens.new_token()).replace(".", "")


def _signed_for(token: str, user_id: int) -> bool:
    """Whether the server made ``token`` for this account."""
    if not isinstance(token, str) or len(token) <= keys.MAC_CHARS:
        return False
    payload, mac = token[:-keys.MAC_CHARS], token[-keys.MAC_CHARS:]
    return users.server_keys().verify(_purpose(user_id), f"{payload}.{mac}") == payload


def make_reset_email(user_id: int, address: str, *, created_by: Optional[int] = None) -> tuple:
    """A new reset link for the account, committed (older unused ones stop working): returns the message to send to
    ``address``. Runs on whatever thread calls it; it needs no key."""
    db = SessionLocal()
    try:
        account_email._forget_unused(db, user_id, RESET_KIND)
        token, row = tokens.create(db, RESET_KIND, ttl=RESET_TTL, user_id=user_id, created_by=created_by,
                                   payload={"email": address}, token=link_token(user_id))
        db.commit()
        expires = row.expires_at.replace(tzinfo=timezone.utc)
    finally:
        db.close()
    return email_templates.password_reset(token, expires)


def _forgot_job(user_id: int, address: str) -> None:
    """On the mail thread: make the link and send it."""
    mail_queue.deliver_now(address, make_reset_email(user_id, address), user_id)


def send_for_admin(user_id: int, address: str, admin_id: int) -> bool:
    """The admin panel's "send a reset email": the link is made now and sent in the background. False when too much
    mail is waiting."""
    return mail_queue.send(address, make_reset_email(user_id, address, created_by=admin_id), user_id=user_id) is not None


def _has_live_link(db: Session, user_id: int) -> bool:
    """Whether the account has a reset link that can still be used (sent, not used, not expired)."""
    return db.query(AuthToken.id).filter(AuthToken.user_id == user_id, AuthToken.kind == RESET_KIND,
                                         AuthToken.used_at.is_(None), AuthToken.expires_at > utcnow()
                                         ).first() is not None


class ForgotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field("", max_length=1000)


@router.post("/forgot", status_code=202)
def forgot(body: ForgotIn, db: Session = Depends(get_db)):
    if not mail_queue.enabled():
        raise AuthProblem(409, "email_not_set_up", "This JobHuntPA can't send reset links by email. Use a recovery code "
                                                   "instead, or ask the person who runs it.")
    try:
        # One spelling (NFKC, lower case) is what the account is found by, so it is also where the link goes and what
        # the token keeps: the address as typed is the person's guess at it, and may differ in case.
        address = normalise_email(users.clean_email(body.email))
    except users.AccountError as exc:
        raise AuthProblem(422, "invalid", exc.message, field="email") from None
    server = users.server_keys()
    user = users.find_by_email(db, address)
    allowed = ratelimit.FORGOT_ADDRESS.hit(server.email_hash(address)) is None
    send = False
    if user is not None and user.status == "active":
        if allowed and ratelimit.RESET_ACCOUNT.hit(user.id) is None:
            send = True
        elif not _has_live_link(db, user.id) and ratelimit.FORGOT_NO_LINK.hit(user.id) is None:
            # The limits are used up, but every link has run out: the owner can always get one. Counted here, not
            # when the job makes the token, so requests in flight together (or queued behind other mail) can't each
            # send one: at most one such email an hour.
            send = True
        else:
            log.info("Reset link requests for user %s are being held back by the limits", user.id)
    if send:
        mail_queue.submit(_forgot_job, user.id, address)
    else:
        # The same hashing and sealing a real link takes, so the answer isn't quicker for an address with no account,
        # and the same time on the mail thread, so the next email isn't later for one that has an account.
        fake = link_token(0)
        tokens.hash_token(fake)
        tokens.seal_payload(fake, RESET_KIND, {"email": address})
        mail_queue.submit(mail_queue.decoy)
    return JSONResponse({"ok": True, "message": SENT}, status_code=202)


# ---------------------------------------------------------------------------
# The reset page
# ---------------------------------------------------------------------------

class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field("", max_length=2000)  # (a longer one is never ours: ``tokens.find`` says no)


def _row_and_user(db: Session, token: str) -> tuple[AuthToken, User, str]:
    """The link's row, its account and the address it was sent to. The link is refused when that address is no longer
    the account's (it was changed since): a link in the old mailbox must not work."""
    row = tokens.find(db, RESET_KIND, token)
    user = db.get(User, row.user_id) if row is not None and row.user_id is not None else None
    if row is None or user is None or not _signed_for(token, user.id):
        raise AuthProblem(410, "link_invalid", LINK_INVALID)
    if user.status != "active":
        raise AuthProblem(403, "disabled", DISABLED)
    payload = tokens.open_payload(token, RESET_KIND, row.payload_enc) or {}
    address = payload.get("email") if isinstance(payload.get("email"), str) else ""
    if not address or user.email_hash not in users.server_keys().email_hash_candidates(address):
        raise AuthProblem(410, "link_invalid", LINK_INVALID)
    sealed = _sealed_address(db, user)
    if sealed is not None:
        # Background access: the reset keeps the data, so the link must name the address the account itself holds
        # (sealed under its own key). The lookup column ``email_hash`` is plaintext and could have been rewritten.
        if not sealed or normalise_email(sealed) != normalise_email(address):
            log.warning("Reset link for user %s names an address that isn't the account's own; refused", user.id)
            raise AuthProblem(410, "link_invalid", LINK_INVALID)
        address = sealed
    return row, user, address


def _sealed_address(db: Session, user: User) -> Optional[str]:
    """The address in the account's own sealed details (``pii_self``), opened with the server's copy of its key:
    None without background access, "" when it is missing or doesn't open."""
    dek = users.background_key(db, user.id)
    if dek is None:
        return None
    try:
        with crypto.as_user(user.id, dek):
            email = users.details(db, user.id).get("email")
    except (crypto.CryptoError, ValueError, AttributeError):
        return ""
    return email if isinstance(email, str) else ""


@router.post("/reset/info")
def reset_info(body: TokenIn, db: Session = Depends(get_db)):
    """What the reset page must say before it asks for the new password: whether the data is kept (the account has
    background access), whether a code is needed, and whether an admin loses their share of the admin key (and is
    the last who has it). Read-only; doesn't use the link up."""
    _row, user, _address = _row_and_user(db, body.token)
    keeps = users.background_key(db, user.id) is not None
    # An admin who resets by email (with the data kept or deleted) loses their share of the admin key: only a
    # recovery code keeps it. When no other admin holds it, only the server's operator can restore it.
    loses = admin_key.holds_key(db, user.id)
    return {"keeps_data": keeps, "mfa_required": bool(user.mfa_enabled), "admin_key_will_be_lost": loses,
            "last_key_holder": loses and not admin_key.other_holders(db, user.id)}


def _run_active() -> AuthProblem:
    return AuthProblem(409, "run_active", "A search is still running. Wait for it to finish, then open the link "
                                          "again.")


class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field("", max_length=2000)  # (a longer one is never ours: ``tokens.find`` says no)
    new_password: str = Field("", max_length=4096)
    code: Optional[str] = Field(None, max_length=64)           # with two-step sign-in on: the app's code ...
    recovery_code: Optional[str] = Field(None, max_length=64)  # ... or a recovery code (used up)
    accept_wipe: bool = False
    remember: bool = False


@router.post("/reset")
def reset(body: ResetIn, request: Request, db: Session = Depends(get_db)):
    try:
        users.check_password(body.new_password)
    except users.AccountError as exc:
        raise AuthProblem(422, "invalid", exc.message, field="new_password") from None
    row, user, address = _row_and_user(db, body.token)
    keeps = users.background_key(db, user.id) is not None
    if not keeps and not body.accept_wipe:  # nothing has changed yet
        raise AuthProblem(409, "wipe_not_accepted", WIPE_NEEDED, field="accept_wipe")
    if not keeps and users.has_active_runs(db, user.id):  # before a code is spent on a reset that can't go ahead
        raise _run_active()
    if user.mfa_enabled:
        if bool((body.code or "").strip()) == bool((body.recovery_code or "").strip()):
            raise AuthProblem(422, "second_factor_required", "This account uses two-step sign-in. Enter the code from "
                              "your authenticator app, or a recovery code.", field="code")
        # Counted per place before the code is looked at, and against the codes' own ceiling over all places (see
        # ``check_second_factor``): the link is the proof of the mailbox, but it isn't used up by a wrong code, so
        # codes guessed from many addresses must run out. The same goes for the link itself: after 5 wrong codes it
        # is used up and a new one must be asked for (as for a pending sign-in: ``sessions.MFA_ATTEMPTS``).
        try:
            check_second_factor(db, user, SecondFactorIn(code=body.code, recovery_code=body.recovery_code),
                                request.scope)
        except AuthProblem as problem:
            if problem.code == "wrong_code" and ratelimit.reset_code_strikes.strike(row.token_hash) >= ratelimit.CODE_TRIES:
                tokens.consume(db, RESET_KIND, body.token)
                log.info("Reset link for user %s used up by wrong codes", user.id)
                raise AuthProblem(410, "link_invalid", LINK_USED_UP) from None
            raise
    used = tokens.consume(db, RESET_KIND, body.token)  # one request wins when two use the same link
    if used is None:
        raise AuthProblem(410, "link_invalid", LINK_INVALID)
    wiped: Optional[users.WipedReset] = None
    kept: Optional[users.KeptReset] = None
    kept_codes: Optional[list] = None
    try:  # the link is given back if the change itself fails
        if keeps:
            dek, kept = None, users.reset_password_keeping_data(db, user.id, body.new_password)
            if kept is not None:
                dek, ik, kept_codes = users.background_key(db, user.id), kept.ik, kept.recovery_codes
            if dek is None:  # background access was turned off or its key stopped opening just now
                raise AuthProblem(409, "try_again", "Something changed while you were resetting. Open the link again "
                                                    "and try once more.")
        else:
            wiped = users.reset_password_wiping_data(db, user.id, body.new_password, address)
            dek, ik = wiped.dek, wiped.ik
    except users.AccountBusy:
        tokens.release(db, used.id)
        raise _run_active() from None
    except BaseException:
        db.rollback()
        tokens.release(db, used.id)
        raise
    user = db.get(User, user.id, populate_existing=True)
    db.execute(update(User).where(User.id == user.id).values(email_verified=True))  # the mailbox answered
    secret = sessions.create(db, user, dek, ik, persistent=body.remember)
    account_email.end_open_links(db, user.id)  # any other link sent before this reset (the one used is already spent)
    db.commit()
    log.info("Password reset by email: user %s (%s)", user.id, "data kept" if keeps else "data deleted")
    account_email.notify("password_changed", address, user.id)
    _after_sign_in(user.id, dek)  # a pending encryption migration waits for its owner's key
    out = {"ok": True, "user": {"id": user.id, "role": user.role}, "kept_data": keeps, "wiped": not keeps,
           "admin_key_lost": bool((wiped and wiped.admin_key_lost) or (kept and kept.admin_key_lost))}
    out["admin_key_last_holder"] = out["admin_key_lost"] and not admin_key.other_holders(db, user.id)
    out["recovery_codes"] = wiped.recovery_codes if wiped is not None else kept_codes  # (never the old ones)
    return _signed_in(request, secret, body.remember, out, user_id=user.id)
