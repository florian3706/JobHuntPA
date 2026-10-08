"""Job digest emails (accounts design §9, §13 item 2 and §16; Phase J).

A digest lists the jobs a user's *scheduled* searches found since their last digest, best fit first, as small cards
(``email_templates.job_digest``). Settings are in ``email_prefs`` (plaintext: the unsubscribe link works without
anyone's key).

    GET  /api/account/digest   {enabled, every_days, can_send, problems: [{code, text}], send_time, time_zone,
                               last_sent_at}
    PUT  /api/account/digest   {enabled?: bool, every_days?: 1-7} -> the same answer. Turning it on sets
                               ``opted_in_at`` (the cutoff of the first digest). The ``email_prefs`` row, with its
                               ``unsubscribe_id``, is made when missing. ``problems`` says why a digest can't be sent yet (email not set up,
                               address not confirmed, background access off, no membership for scheduled searches);
                               the setting can be saved anyway.

    GET  /unsubscribe?token=               a confirmation page. Changes nothing: mail scanners fetch links.
    POST /unsubscribe?token=               turns the digest off. No session, no CSRF token: the signed token is
                                           the proof. Takes RFC 8058's ``List-Unsubscribe=One-Click`` form body (as
                                           mail providers send it, with no Origin and no cookies) or the page's own
                                           form. 200 with a page; 410 when the token isn't valid.
    POST /unsubscribe/resubscribe?token=   turns it on again (the page's "Subscribe again"). Takes only the
                                           short-lived ``resub`` token that an unsubscribe page made just now, never
                                           the one in the emails: an old email can't turn digests back on.

The unsubscribe routes are outside /api, so ``sessions.AuthMiddleware`` (which guards /api only) never sees them:
there is no exemption to maintain, and nothing else is reachable without a session. The emails' token is
``keys.sign("unsub", unsubscribe_id)`` (it only turns the digest off), checked in constant time against the current and the previous signing
keys; a token that fails, or names no account, gets the same answer, and changes nothing. 30 POSTs a minute per
client address.

**The tick** (``tick``, every 10 minutes from ``schedule.start_scheduler``'s loop, so off with
JOBHUNT_SCHEDULER=off): does nothing unless email is on. A user gets a digest when ``digest_enabled``, the address
is confirmed, the account is active and the server holds their key (background access). Due: from 07:00 in the
time zone of the user's first workspace's schedule, once ``digest_every_days`` local days have passed since the
last digest (or since opting in), and not checked yet that day. Selection runs under the user's key, unwrapped with
the server's: the user's jobs first linked during a scheduled run started since the cutoff, minus merged copies
(SQL), minus excluded, hidden and closed jobs (Python: those columns are sealed). No new jobs: only
``last_checked_at`` is set. ``last_sent_at`` is set only after the mail went; a send that failed because the mail
server is down or busy leaves everything as it was, so the next tick (10 minutes later) tries again, and the tick
gives up after MAX_FAILURES of those. Trouble that only that account has (a key the server can't open, no address,
a refused recipient) is not a down mail server: it is skipped until the account's next day, and never counts
against the others. Nothing here logs an address or anything about a job.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, time, timezone
from html import escape
from time import sleep
from typing import Any, Iterable, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend import crypto, email_templates, mail_queue, membership, ratelimit, schedule, users
from backend.db import (EmailPrefs, FitResult, Job, SearchRun, SessionLocal, User, UserKeys, Workspace, get_db,
                        utcnow)
from backend.pool import jobs_query
from backend.sessions import AuthUser, client_address, current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["digest"])
public_router = APIRouter(tags=["digest"])

SEND_FROM = time(7, 0)        # local time: a digest goes out at or after this
MAX_CARDS = 60                # more are left out: "And N more in JobHuntPA"
MAX_FAILURES = 3              # failed sends in one tick before it gives up (the mail server is probably down)
SKIPPED = "skipped"           # _digest_for: this account's own trouble, not the mail server's
# What mailer.send reports for a message that this address, and not the server, makes impossible.
_ADDRESS_ERRORS = frozenset({"SMTPRecipientsRefused", "SMTPDataError", "invalid message"})
RECORD_TRIES = 3              # commits of "sent" attempted before it is kept to record on the next tick
_unrecorded: dict[int, datetime] = {}  # user id -> when a digest went whose "sent" couldn't be saved
RESUBSCRIBE_TTL = 3600        # seconds the "Subscribe again" button of an unsubscribe page works
UNSUBSCRIBE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")

PROBLEM_TEXT = {
    "email_off": "Email isn't set up on this JobHuntPA yet. The person who runs it needs to set it up.",
    "email_unconfirmed": "Confirm your email address first. Check your inbox for the link, or ask for a new one in "
                         "your account settings.",
    "background_off": "Turn on background access, so JobHuntPA can open your jobs while you're signed out.",
    "membership_required": "Digests list the jobs your scheduled searches find, and scheduled searches need a "
                           "membership.",
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def ensure_prefs(db: Session, user_id: int) -> EmailPrefs:
    """The user's ``email_prefs`` row, made (digest off, with a new ``unsubscribe_id``) when missing. Commits only
    when it makes one."""
    row = db.get(EmailPrefs, user_id)
    if row is not None:
        return row
    try:
        db.add(EmailPrefs(user_id=user_id, unsubscribe_id=secrets.token_urlsafe(16)))
        db.commit()
    except IntegrityError:  # made by another request a moment ago
        db.rollback()
    return db.get(EmailPrefs, user_id)


def problems(db: Session, user_id: int, now: Optional[datetime] = None) -> list[dict]:
    """Why a digest can't be sent to this user yet: ``[{code, text}]``, empty when it can."""
    codes = []
    user = db.get(User, user_id)
    if not mail_queue.enabled():
        codes.append("email_off")
    if user is None or not user.email_verified:
        codes.append("email_unconfirmed")
    if not users.background_enabled(db, user_id):
        codes.append("background_off")
    if membership.scheduled_block(db, user_id, now):
        codes.append("membership_required")
    return [{"code": c, "text": PROBLEM_TEXT[c]} for c in codes]


def _iso(naive: Optional[datetime]) -> Optional[str]:
    return naive.isoformat() + "Z" if naive else None


def _zone(db: Session, user_id: int) -> ZoneInfo:
    """The time zone of the user's first workspace's schedule (its state's; New South Wales when it has none)."""
    first = db.query(Workspace.id).filter(Workspace.owner_id == user_id).order_by(Workspace.id).first()
    state = schedule.get_schedule(db, first[0]).state if first else "NSW"
    return ZoneInfo(schedule.STATES[state][1])


def settings_info(db: Session, user_id: int, now: Optional[datetime] = None) -> dict:
    row = ensure_prefs(db, user_id)
    why = problems(db, user_id, now)
    return {"enabled": bool(row.digest_enabled), "every_days": _every(row), "can_send": not why, "problems": why,
            "send_time": SEND_FROM.strftime("%H:%M"), "time_zone": str(_zone(db, user_id)),
            "last_sent_at": _iso(row.last_sent_at)}


class DigestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    every_days: Optional[int] = Field(default=None, ge=1, le=7)


def turn_on(row: EmailPrefs, now: Optional[datetime] = None) -> None:
    """Digest on: from now (``opted_in_at`` is the cutoff of the first digest), checked afresh. Already on: no change."""
    if not row.digest_enabled:
        row.digest_enabled = True
        row.opted_in_at = now or utcnow()
        row.last_checked_at = None


@router.get("/digest")
def get_digest(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    return settings_info(db, user.id)


@router.put("/digest")
def put_digest(body: DigestIn, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    if body.enabled is None and body.every_days is None:
        raise HTTPException(status_code=422, detail="Send enabled or every_days.")
    row = ensure_prefs(db, user.id)
    if body.every_days is not None:
        row.digest_every_days = body.every_days
    if body.enabled is True:
        turn_on(row)
    elif body.enabled is False:
        row.digest_enabled = False
    db.commit()
    return settings_info(db, user.id)


def _every(row: EmailPrefs) -> int:
    return min(7, max(1, int(row.digest_every_days or 1)))


# ---------------------------------------------------------------------------
# When a digest is due
# ---------------------------------------------------------------------------

def _since(row: EmailPrefs) -> Optional[datetime]:
    """The cutoff: jobs found by runs from here on are new. The later of the last digest and opting in (opting in
    again after a pause starts afresh), as naive UTC; None when neither is set."""
    times = [t for t in (row.last_sent_at, row.opted_in_at) if t is not None]
    return max(times) if times else None


def _local_date(naive_utc: datetime, tz: ZoneInfo):
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(tz).date()


def is_due(row: EmailPrefs, tz: ZoneInfo, now: datetime) -> bool:
    """From 07:00 local time, ``digest_every_days`` local days after the last digest (or after opting in), and not
    checked already today. ``now`` is aware."""
    local = now.astimezone(tz)
    since = _since(row)
    if since is None or local.time() < SEND_FROM:
        return False
    if (local.date() - _local_date(since, tz)).days < _every(row):
        return False
    return row.last_checked_at is None or _local_date(row.last_checked_at, tz) < local.date()


# ---------------------------------------------------------------------------
# What is new
# ---------------------------------------------------------------------------

def _evidence(fit: Optional[FitResult]) -> dict:
    if fit is None or fit.status != "ok":
        return {}
    try:
        data = json.loads(fit.evidence_json or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def gap_count(evidence: dict) -> int:
    """The frontend's rule (``gapsSection`` in common.js): the listed gaps, or else the requirements not matched."""
    gaps = evidence.get("gaps")
    if isinstance(gaps, list) and gaps:
        return len(gaps)
    reqs = evidence.get("requirements")
    return sum(1 for r in reqs if isinstance(r, dict) and not r.get("matched")) if isinstance(reqs, list) else 0


def _chunks(items: list, size: int = 400) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def new_jobs(db: Session, owner: int, since: datetime) -> list[Job]:
    """The owner's jobs first linked during a scheduled run that started at or after ``since``, not merged copies,
    not excluded, hidden or closed. Needs the owner's key scope. Sealed columns are never filtered in SQL."""
    rows = (jobs_query(db, card=True)
            .join(SearchRun, SearchRun.id == Job.first_run_id)
            .join(Workspace, Workspace.id == Job.workspace_id)
            .filter(Workspace.owner_id == owner, SearchRun.workspace_id == Job.workspace_id,
                    SearchRun.scheduled.is_(True), SearchRun.started_at >= since, Job.duplicate_of.is_(None))
            .order_by(Job.id).all())
    return [j for j in rows if not j.hidden and not j.excluded_reason and j.closed_at is None]


def cards(db: Session, jobs: list[Job]) -> tuple[list[dict], int]:
    """(up to MAX_CARDS card dicts for ``email_templates.job_digest``, how many more there were). Best fit first
    (unscored last, then newest), kept together by workspace when there are several. Needs the owner's key scope."""
    scores: dict[int, Optional[int]] = {}
    for chunk in _chunks([j.id for j in jobs]):
        for job_id, status, score in (db.query(FitResult.job_id, FitResult.status, FitResult.score)
                                      .filter(FitResult.job_id.in_(chunk))):
            scores[job_id] = score if status == "ok" else None
    ranked = sorted(jobs, key=lambda j: (scores.get(j.id) is None, -(scores.get(j.id) or 0), -j.id))
    shown, more = ranked[:MAX_CARDS], max(0, len(ranked) - MAX_CARDS)
    fits: dict[int, FitResult] = {}
    for chunk in _chunks([j.id for j in shown]):
        fits.update({f.job_id: f for f in db.query(FitResult).filter(FitResult.job_id.in_(chunk))})
    workspaces = sorted({j.workspace_id for j in shown})
    names = {}
    if len(workspaces) > 1:  # which search a job came from matters only when there are several
        names = dict(db.query(Workspace.id, Workspace.name).filter(Workspace.id.in_(workspaces)))
    out = []
    for ws in workspaces:
        for job in (j for j in shown if j.workspace_id == ws):
            evidence = _evidence(fits.get(job.id))
            out.append({
                "id": job.id, "workspace_id": ws, "workspace_name": names.get(ws), "title": job.title,
                "company": job.company, "salary_min": job.salary_min, "salary_max": job.salary_max,
                "salary_text": job.salary_text, "office_text": job.office_text, "location_text": job.location_text,
                "office_days": job.office_days, "work_mode": job.work_mode, "employment_type": job.employment_type,
                "fit_score": scores.get(job.id), "fit_summary": evidence.get("summary"),
                "gap_count": gap_count(evidence) if job.id in fits and scores.get(job.id) is not None else None,
                "url": job.url, "source": job.source,
            })
    return out, more


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def _scheduled_run_active(db: Session, owner: int) -> bool:
    """A scheduled search of the user's is still queued or running: its jobs aren't all there yet, and the next
    digest's cutoff would skip the rest, so the check waits."""
    return db.query(SearchRun.id).join(Workspace, Workspace.id == SearchRun.workspace_id).filter(
        Workspace.owner_id == owner, SearchRun.scheduled.is_(True),
        SearchRun.state.in_(["queued", "running"])).first() is not None


def _record(db: Session, row: EmailPrefs, stamp: datetime, *, sent: bool) -> None:
    """Saves that the user was looked at (and, when ``sent``, that the digest went). Retried a few times: a busy
    database must not make a digest that already went go again."""
    for attempt in range(RECORD_TRIES):
        try:
            if sent:
                row.last_sent_at = stamp
            row.last_checked_at = stamp
            db.commit()
            return
        except Exception:  # noqa: BLE001  (a locked database)
            db.rollback()
            if attempt == RECORD_TRIES - 1:
                raise
            sleep(0.5 * (attempt + 1))


def _skip(db: Session, row: EmailPrefs, stamp: datetime) -> str:
    """Trouble only this account has: not tried again until its next day, so it never holds up anyone else."""
    _record(db, row, stamp, sent=False)
    return SKIPPED


def _digest_for(db: Session, row: EmailPrefs, now: datetime) -> Optional[bool | str]:
    """One user's digest. True: sent. False: nothing to send (checked). SKIPPED: this account can't get one (its key
    won't open, no address, a refused recipient): checked, so not tried again until its next day. None: the mail
    server failed (nothing recorded, so the next tick tries again)."""
    uid = row.user_id
    stamp = now.astimezone(timezone.utc).replace(tzinfo=None)
    if uid in _unrecorded:  # it went, but saving that failed: save it, never send it again
        _record(db, row, _unrecorded[uid], sent=True)
        del _unrecorded[uid]
        return False
    key = users.background_key(db, uid)
    if key is None:
        log.warning("Digest skipped for user %s: the server can't open their key", uid)
        return _skip(db, row, stamp)
    with crypto.as_user(uid, key):
        found, more = cards(db, new_jobs(db, uid, _since(row)))
        if not found:
            row.last_checked_at = stamp
            db.commit()
            return False
        who = users.details(db, uid)
        template = email_templates.job_digest(found, keys=users.server_keys(), unsubscribe_id=row.unsubscribe_id,
                                              every_days=_every(row), more_count=more, name=who.get("name") or None)
        address = who.get("email")
    if not address:
        log.warning("Digest skipped for user %s: no address on file", uid)
        return _skip(db, row, stamp)
    result = mail_queue.deliver_now(address, template, uid)
    if not result.ok:
        return _skip(db, row, stamp) if result.error in _ADDRESS_ERRORS else None
    try:
        _record(db, row, stamp, sent=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("Digest sent to user %s but not saved (%s): it will be saved on the next check", uid,
                    exc.__class__.__name__)
        _unrecorded[uid] = stamp
    return True


def tick(now: Optional[datetime] = None) -> int:
    """Send every digest that is due (see the module docstring). Returns how many went. Does nothing unless email is
    on. One user's trouble never stops the others, and the tick stops after MAX_FAILURES failed sends (the mail
    server is probably down): an account's own trouble doesn't count."""
    from backend import migrate_encryption

    now = now or datetime.now(timezone.utc)
    if not mail_queue.enabled() or not migrate_encryption.is_done():
        return 0
    sent = failures = 0
    db = SessionLocal()
    try:
        due = (db.query(EmailPrefs).join(User, User.id == EmailPrefs.user_id)
               .join(UserKeys, UserKeys.user_id == User.id)
               .filter(EmailPrefs.digest_enabled.is_(True), User.email_verified.is_(True), User.status == "active",
                       UserKeys.server_wrapped_dek.isnot(None)).order_by(EmailPrefs.user_id).all())
        for row in due:
            uid = row.user_id
            try:
                if uid not in _unrecorded and (not is_due(row, _zone(db, uid), now) or _scheduled_run_active(db, uid)):
                    continue
                outcome = _digest_for(db, row, now)
            except Exception as exc:  # noqa: BLE001  (never the message: it may hold a job's text)
                db.rollback()
                log.warning("Digest failed for user %s: %s", uid, exc.__class__.__name__)
                continue  # this account's trouble, not the mail server's: the others go on
            if outcome is True:
                sent += 1
            elif outcome is None:
                failures += 1
                if failures >= MAX_FAILURES:
                    log.warning("Digests stopped for now: %d failed in one check", failures)
                    break
    finally:
        db.close()
    return sent


# ---------------------------------------------------------------------------
# Unsubscribe (no session)
# ---------------------------------------------------------------------------

def prefs_for_token(db: Session, token: Any, purpose: str = "unsub") -> Optional[EmailPrefs]:
    """The row a valid token names (``unsub``: the one in every email; ``resub``: the short-lived one on an
    unsubscribe page), or None for anything else (a bad signature, a token too old, a token for no account): the
    caller can't and doesn't tell these apart."""
    payload = users.server_keys().verify(purpose, token)
    if payload is None or not UNSUBSCRIBE_ID_RE.fullmatch(payload):
        return None
    return db.query(EmailPrefs).filter(EmailPrefs.unsubscribe_id == payload).first()


_STYLE = """
:root{color-scheme:light dark;--bg:#fff;--text:#12161d;--muted:#4a5260;--accent:#3a2be0;--on-accent:#fff;--border:#c9ced8}
@media (prefers-color-scheme:dark){:root{--bg:#12161d;--text:#eef0f4;--muted:#b4bbc8;--accent:#b8b2ff;--on-accent:#12161d;--border:#3b4252}}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{box-sizing:border-box;max-width:36rem;margin:0 auto;padding:32px 16px}
h1{font-size:1.5rem;line-height:1.3;margin:0 0 16px}
p{margin:0 0 16px}
.brand{color:var(--muted);font-size:.875rem;margin:0 0 24px}
button{font:inherit;font-weight:700;min-height:44px;padding:8px 20px;border:2px solid var(--accent);border-radius:6px;background:var(--accent);color:var(--on-accent);cursor:pointer}
button:focus-visible,a:focus-visible{outline:3px solid var(--text);outline-offset:3px}
a{color:var(--accent)}
form{margin:0 0 16px}
"""

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    # The token is in the address: nothing here loads or sends anything but this page's own form.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
                               "frame-ancestors 'none'",
    "X-Robots-Tag": "noindex",
}


def _page(title: str, body: str, status: int = 200, extra: Optional[dict] = None) -> HTMLResponse:
    html = (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<meta name="robots" content="noindex">\n<title>{escape(title)} - JobHuntPA</title>\n'
            f'<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n<p class="brand">JobHuntPA</p>\n'
            f'<h1>{escape(title)}</h1>\n{body}\n</main>\n</body>\n</html>\n')
    return HTMLResponse(html, status_code=status, headers={**_HEADERS, **(extra or {})})


def _form(action: str, token: str, button: str) -> str:
    url = f"/unsubscribe{action}?token={quote(token, safe='')}"
    return f'<form method="post" action="{escape(url, quote=True)}"><button type="submit">{escape(button)}</button></form>'


def _invalid() -> HTMLResponse:
    return _page("This link isn't valid", '<p>It may be out of date. Sign in to JobHuntPA and change your job digest '
                 f'emails in {_WHERE}.</p>\n<p><a href="/">Open JobHuntPA</a></p>', 410)


def _limited(request: Request) -> Optional[HTMLResponse]:
    wait = ratelimit.UNSUBSCRIBE.hit(client_address(request.scope))
    if wait is None:
        return None
    return _page("Please wait a moment", f"<p>Too many requests from this network. Try again in "
                 f"{escape(ratelimit.wait_text(wait))}.</p>", 429, {"Retry-After": ratelimit.retry_after(wait)})


async def _refuse(request: Request) -> Optional[HTMLResponse]:
    """The answer for a POST that can't go ahead: over the limit, or with a body that is too big. The body (RFC
    8058's ``List-Unsubscribe=One-Click``, or the page's form) is only read to refuse a huge one: the signed token
    in the address is what decides, not the body."""
    if (limited := _limited(request)) is not None:
        return limited
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 4096:
            return _page("Request too large", "<p>That request was too large.</p>", 413)
    return None


def _with_token(token: str, change, purpose: str = "unsub") -> HTMLResponse:
    """Runs ``change(db, row)`` for the account a valid token names (and commits); the invalid-link page otherwise."""
    db = SessionLocal()
    try:
        row = prefs_for_token(db, token, purpose)
        if row is None:
            return _invalid()
        return change(db, row)
    finally:
        db.close()


_OPEN = '\n<p><a href="/">Open JobHuntPA</a></p>'
_WHERE = "Settings &gt; Job digest emails"  # (as HTML)


def _subscribe_again(row: EmailPrefs) -> str:
    """The "Subscribe again" button, with a token that lasts an hour and works for nothing else."""
    token = users.server_keys().sign("resub", row.unsubscribe_id, ttl=RESUBSCRIBE_TTL)
    return _form("/resubscribe", token, "Subscribe again")


def _read_page(db: Session, row: EmailPrefs, token: str) -> HTMLResponse:
    if row.digest_enabled:
        every = "day" if _every(row) == 1 else f"{_every(row)} days"
        return _page("Unsubscribe from job digests?",
                     f"<p>JobHuntPA emails you the new jobs its scheduled searches find, every {every}. Choose "
                     f"Unsubscribe to stop them.</p>\n{_form('', token, 'Unsubscribe')}")
    return _page("You are unsubscribed", f"<p>You don't get job digest emails. To turn them on again, sign in to "
                 f"JobHuntPA and go to {_WHERE}.</p>{_OPEN}")


def _turn_off(db: Session, row: EmailPrefs, token: str) -> HTMLResponse:
    if not row.digest_enabled:  # already off: nothing changed here, so no way back from this link either
        return _page("You are unsubscribed", f"<p>You don't get job digest emails. To turn them on again, sign in "
                     f"to JobHuntPA and go to {_WHERE}.</p>{_OPEN}")
    row.digest_enabled = False
    db.commit()
    log.info("user %s: digest emails turned off by their unsubscribe link", row.user_id)
    return _page("You are unsubscribed", "<p>You won't get job digest emails any more.</p>\n"
                 + _subscribe_again(row) + _OPEN)


def _turn_back_on(db: Session, row: EmailPrefs, token: str) -> HTMLResponse:
    if not row.digest_enabled:
        turn_on(row)
        db.commit()
        log.info("user %s: digest emails turned on again by their unsubscribe page", row.user_id)
    return _page("You are subscribed again", "<p>JobHuntPA will email you when your scheduled searches find new "
                 f"jobs. You can change how often, or turn it off, in {_WHERE}.</p>{_OPEN}")


@public_router.api_route("/unsubscribe", methods=["GET", "HEAD"], include_in_schema=False)
async def unsubscribe_page(token: str = ""):
    """The confirmation page. Reads, never changes: a link scanner that fetches it unsubscribes nobody."""
    return await run_in_threadpool(_with_token, token, lambda db, row: _read_page(db, row, token))


@public_router.post("/unsubscribe", include_in_schema=False)
async def unsubscribe(request: Request, token: str = ""):
    """RFC 8058 one-click, and the page's button: the digest goes off. No session: the signed token is the proof."""
    return await _refuse(request) or await run_in_threadpool(
        _with_token, token, lambda db, row: _turn_off(db, row, token))


@public_router.post("/unsubscribe/resubscribe", include_in_schema=False)
async def resubscribe(request: Request, token: str = ""):
    """"Subscribe again": the digest goes on, from now. Only the page's own short-lived token works here."""
    return await _refuse(request) or await run_in_threadpool(
        _with_token, token, lambda db, row: _turn_back_on(db, row, token), "resub")
