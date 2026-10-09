"""The browser extensions (Chrome and Firefox): add the page in the current tab as a job, or as a company source.

The extension's API contract: docs/design/accounts.md §19. Who may call each endpoint:

    GET    /api/extension/status    signed in (an extension key or a session): the key's name and expiry (null for a
                                    session), the account's workspaces and the default one
    POST   /api/extension/preview   signed in, any workspace of the account (X-Workspace): the job read from a page,
                                    and the workspace's job with that address if it has one. Stores nothing.
    POST   /api/extension/jobs      signed in, as above: stores the job as the workspace's own (201), or updates the
                                    workspace's job with that address (200)
    GET    /api/extension/keys      a session only (an extension key gets 403 token_scope): the account's keys
    POST   /api/extension/keys      a session only, with a recent sign-in (403 reauth_required): a new key {label},
                                    shown once
    DELETE /api/extension/keys/{id} a session only: revokes one of the account's keys (404 for anyone else's)

**Extension keys.** ``jhpa_`` and 43 base64url characters (32 random bytes). The server keeps a ``sessions`` row
(``state = "extension"``) whose id is a hash of the secret and whose ``wrapped_dek`` is the person's data key
wrapped under a key derived from the secret, so a key opens the person's data the way a session cookie does, and the
database alone opens nothing. A key is checked by backend/sessions.py (``Authorization: Bearer``): no cookie, CSRF
token or Origin is used with it, and it reaches only the endpoints in ``sessions.KEY_ENDPOINTS``. It never counts as
a recent sign-in. It lasts 60 days from its last use and a year at most; every "sign out everywhere" ends it (a
password change that signs out the other devices, a reset, recovery, "Sign out everywhere else", an admin turning
off the account or its two-step sign-in, deleting the account).

**Pages.** The extension sends the page from the person's own browser (address, title, HTML, visible text and any
selected text); nothing is fetched and no AI is asked (backend/scraping/page_job.py). The job is stored like an
imported page (``pool.import_private``): private to the workspace unless the shared pool already has the address.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend import crypto, sessions
from backend.db import PROJECT_ROOT, AuthSession, Job, UserKeys, Workspace, get_db, scrub_wal, utcnow
from backend.scraping import page_job
from backend.scraping.text import clean
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth
from backend.workspaces import current_workspace

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/extension", tags=["extension"])

API_VERSION = 1
SOURCE = "browser"          # jobs.source of a job added from the extension (frontend: "browser extension")
MAX_BODY = 16 * 1024 * 1024  # bytes: the page's HTML at its longest is 3 million characters, up to 4 bytes each in UTF-8
MAX_KEYS = 20               # extension keys per account
LABEL_MAX = 80
PREVIEW_CHARS = 1500


def _version() -> str:
    try:
        return (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


APP_VERSION = _version()


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.replace(microsecond=0).isoformat() + "Z" if value else None


def _expires(row: AuthSession) -> datetime:
    return min(row.idle_expires_at, row.absolute_expires_at)


def _label_of(row: AuthSession) -> str:
    """The key's name, opened with the request's key (the caller has checked the row is the account's)."""
    if row.label_enc is None:
        return ""
    try:
        return crypto.unseal(bytes(row.label_enc), f"sessions.label|{row.id}").decode("utf-8")
    except crypto.CryptoError:
        log.warning("user %s: an extension key's name doesn't open", row.user_id)
        return ""


def session_only(user: AuthUser = Depends(current_user)) -> AuthUser:
    """A signed-in browser session; an extension key is refused (the middleware refuses it before this)."""
    if user.key:
        raise AuthProblem(403, sessions.TOKEN_SCOPE, sessions.TOKEN_SCOPE_DETAIL)
    return user


# ---------------------------------------------------------------------------
# The page a request carries
# ---------------------------------------------------------------------------

class PageFields(BaseModel):
    """What the person typed into the extension's form; empty values change nothing."""
    model_config = ConfigDict(extra="ignore")
    title: str = Field("", max_length=page_job.FIELD_LIMITS["title"])
    company: str = Field("", max_length=page_job.FIELD_LIMITS["company"])
    location_text: str = Field("", max_length=page_job.FIELD_LIMITS["location_text"])
    salary_text: str = Field("", max_length=page_job.FIELD_LIMITS["salary_text"])
    employment_type: str = Field("", max_length=page_job.FIELD_LIMITS["employment_type"])
    work_mode: str = Field("", max_length=page_job.FIELD_LIMITS["work_mode"])


class PageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str = Field(..., max_length=page_job.LIMITS["url"])
    page_title: str = Field("", max_length=page_job.LIMITS["page_title"])
    html: str = Field("", max_length=page_job.LIMITS["html"])
    text: str = Field("", max_length=page_job.LIMITS["text"])
    selection: str = Field("", max_length=page_job.LIMITS["selection"])
    fields: PageFields = Field(default_factory=PageFields)


TOO_BIG = "This page is too big to send. Select the job ad's text on the page and try again."


def _too_big() -> AuthProblem:
    return AuthProblem(413, "too_large", TOO_BIG)


def _invalid(detail: str, field: Optional[str] = None) -> AuthProblem:
    return AuthProblem(422, "invalid", detail, field=field)


_FIELD_NAMES = {"url": "the page address", "page_title": "the page title", "title": "the job title",
                "company": "the employer", "location_text": "the location", "salary_text": "the salary",
                "employment_type": "the employment type", "work_mode": "the work mode"}


async def page_body(request: Request) -> PageIn:
    """The request's page, read with a size limit: a body over ``MAX_BODY`` bytes is refused (413) from its
    Content-Length, or as soon as it grows past it, before any of it is parsed."""
    length = request.headers.get("content-length")
    if length is not None:
        if not length.strip().isdigit():
            raise _invalid("This request's length isn't a number.")
        if int(length) > MAX_BODY:
            raise _too_big()
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise _too_big()
        chunks.append(chunk)
    try:
        data = await run_in_threadpool(json.loads, b"".join(chunks) or b"null")
    except ValueError:
        raise _invalid("Send the page as JSON.") from None
    if not isinstance(data, dict):
        raise _invalid("Send the page as a JSON object.")
    for name in ("html", "text", "selection"):
        value = data.get(name)
        if isinstance(value, str) and len(value) > page_job.LIMITS[name]:
            raise _too_big()
    try:
        page = PageIn.model_validate(data)
    except ValidationError as exc:
        where = exc.errors()[0].get("loc", ()) if exc.errors() else ()
        name = str(where[-1]) if where else ""
        raise _invalid(f"Check {_FIELD_NAMES.get(name, 'what was sent')}: it is missing, too long or not text.",
                       field=name or None) from None
    page.url = page.url.strip()
    try:
        parts = urlsplit(page.url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        raise _invalid("Only pages with an http:// or https:// address can be added.", field="url")
    mode = page.fields.work_mode.strip().lower()
    if mode and mode not in page_job.WORK_MODES:
        raise _invalid("The work mode must be onsite, hybrid, remote or unknown.", field="work_mode")
    return page


def _read(page: PageIn) -> page_job.PageJob:
    return page_job.read_page(page.url, page.page_title, page.html, page.text, page.selection)


def _listed(db: Session, job: Job) -> Job:
    """The job that is listed for ``job``: itself, or the job a merged copy points to (backend.duplicates). The
    owner is checked through the plaintext workspace before a row is loaded."""
    seen: set[int] = set()
    while job.duplicate_of and job.id not in seen:
        seen.add(job.id)
        owner = db.query(Job.workspace_id).filter(Job.id == job.duplicate_of).scalar()
        if owner != job.workspace_id:
            break
        job = db.get(Job, job.duplicate_of)
    return job


def job_link(job_id: int, ws: int) -> str:
    return f"/job.html?id={job_id}&ws={ws}"


def _existing(db: Session, ws: int, urls: list[str]) -> Optional[dict]:
    """The workspace's listed job with one of these addresses, or None."""
    from backend.pool import job_by_url

    for url in dict.fromkeys(u for u in urls if u):
        job = job_by_url(db, ws, url)
        if job is not None:
            job = _listed(db, job)
            return {"id": job.id, "title": job.title or "", "status": job.status, "link": job_link(job.id, ws)}
    return None


def _job_out(read: page_job.PageJob) -> dict:
    from backend.geo import classify_work_mode

    p = read.posting
    mode = p.work_mode if p.work_mode in page_job.WORK_MODES else "unknown"
    if mode == "unknown":
        mode = classify_work_mode(p.title or "", p.location_text or "", p.description or "")
    description = p.description or ""
    return {"title": p.title or "", "company": p.company or "", "location_text": p.location_text or "",
            "salary_text": p.salary_text or "", "employment_type": p.employment_type or "", "work_mode": mode,
            "posted_at": p.posted_at or "", "url": p.url,
            "description_preview": description[:PREVIEW_CHARS], "description_chars": len(description),
            "description_from": read.description_from}


NO_TITLE = "No job title was found on this page. Type one in, then save."


# ---------------------------------------------------------------------------
# What the extension calls
# ---------------------------------------------------------------------------

@router.get("/status")
def status(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """"Test connection" and the workspace list. Signed in (a key or a session)."""
    spaces = [{"id": w.id, "name": w.name}
              for w in db.query(Workspace).filter(Workspace.owner_id == user.id).order_by(Workspace.id)]
    label = expires = None
    if user.key:
        row = db.get(AuthSession, user.session_id)
        if row is not None and row.user_id == user.id:
            label, expires = _label_of(row), _iso(_expires(row))
    return {"ok": True, "api": API_VERSION, "app_version": APP_VERSION, "label": label, "expires_at": expires,
            "workspaces": spaces, "default_workspace": spaces[0]["id"] if spaces else None}


@router.post("/preview")
def preview(page: PageIn = Depends(page_body), db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """The job the page shows, for the extension's form. Stores nothing."""
    read = _read(page)
    if not read.posting.title and not page.fields.title.strip():
        raise _invalid(NO_TITLE, field="title")
    return {"method": read.method, "job": _job_out(read),
            "existing": _existing(db, ws, [read.posting.url, page.url]), "notes": read.notes}


@router.post("/jobs")
def save_job(response: Response, page: PageIn = Depends(page_body), db: Session = Depends(get_db),
             ws: int = Depends(current_workspace)):
    """Stores the page's job as this workspace's own (as ``/api/import/page`` does), with the person's edits over
    what the page said: a new job (201), or the workspace's job with that address brought up to date (200). The
    exclusion filters and the duplicate scan run as for an import, and the job waits for its score like one."""
    from backend.duplicates import scan_workspace
    from backend.pipeline import import_one

    read = _read(page)
    posting = read.posting
    page_job.apply_fields(posting, page.fields.model_dump())
    if not posting.title:
        raise _invalid(NO_TITLE, field="title")
    page_job.set_detail_status(posting)
    if posting.url != page.url and _existing(db, ws, [posting.url]) is None:
        known = _existing(db, ws, [page.url])  # saved before under the page's own address: update that one
        if known is not None:
            posting.url = page.url
    job_id, created = import_one(posting, SOURCE, ws)
    scan_workspace(ws)
    db.expire_all()
    job = _listed(db, db.get(Job, job_id))
    response.status_code = 201 if created else 200
    log.info("Browser extension: job %s %s in workspace %s", job.id, "added" if created else "updated", ws)
    return {"job_id": job.id, "created": created, "title": job.title or "", "company": job.company or "",
            "link": job_link(job.id, ws)}


# ---------------------------------------------------------------------------
# Keys (the Settings card)
# ---------------------------------------------------------------------------

class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Any = ""


def _key_out(row: AuthSession) -> dict:
    return {"id": row.public_id, "label": _label_of(row), "created_at": _iso(row.created_at),
            "last_used_at": _iso(row.last_seen_at), "expires_at": _iso(_expires(row))}


def _live_keys(db: Session, user_id: int) -> list[AuthSession]:
    now = utcnow()
    return (db.query(AuthSession)
            .filter(AuthSession.user_id == user_id, AuthSession.state == sessions.KEY_STATE,
                    AuthSession.idle_expires_at > now, AuthSession.absolute_expires_at > now)
            .order_by(AuthSession.created_at, AuthSession.id).all())


@router.get("/keys")
def list_keys(user: AuthUser = Depends(session_only), db: Session = Depends(get_db)):
    return [_key_out(row) for row in _live_keys(db, user.id)]


@router.post("/keys", status_code=201)
def create_key(body: KeyIn, user: AuthUser = Depends(session_only), db: Session = Depends(get_db)):
    """A new extension key, shown once. Needs the password entered in the last 15 minutes, and wraps the request's
    own data key (never a stored one), like turning on background access."""
    require_recent_auth(user)
    label = re.sub(r"\s+", " ", body.label).strip() if isinstance(body.label, str) else ""
    if not label:
        raise _invalid('Give it a name, such as "Firefox on my laptop".', field="label")
    if len(label) > LABEL_MAX:
        raise _invalid(f"Use a name of at most {LABEL_MAX} characters.", field="label")
    scope = crypto.current()
    if scope.user_id != user.id:
        raise AuthProblem(403, "wrong_account", "This request isn't for your account.")
    if len(_live_keys(db, user.id)) >= MAX_KEYS:
        raise AuthProblem(409, "too_many_keys", f"You have {MAX_KEYS} connected extensions, the most there can be. "
                                                "Disconnect one you no longer use first.")
    try:
        keys = db.get(UserKeys, user.id)
        if keys is None or crypto.key_id(scope.dek) != bytes(keys.dek_kid):
            raise AuthProblem(409, "key_mismatch", "The extension couldn't be connected. Sign out, sign in again "
                                                   "and try again.")
        key, row = sessions.new_extension_key(db, user.id, scope.dek, label)
        db.flush()
        out = _key_out(row)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    scrub_wal()
    log.info("Extension key made: user %s", user.id)
    return {k: out[k] for k in ("id", "label", "created_at", "expires_at")} | {"key": key}


@router.delete("/keys/{key_id}")
def revoke_key(key_id: str, user: AuthUser = Depends(session_only), db: Session = Depends(get_db)):
    """Revokes one of the signed-in account's keys: its row (and the data key wrapped in it) is deleted."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_id):
        raise AuthProblem(404, "not_found", "That extension isn't connected.")
    try:
        gone = (db.query(AuthSession)
                .filter(AuthSession.user_id == user.id, AuthSession.state == sessions.KEY_STATE,
                        AuthSession.public_id == key_id).delete(synchronize_session=False))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    if not gone:
        raise AuthProblem(404, "not_found", "That extension isn't connected.")
    scrub_wal()  # the row held a wrapped copy of the data key
    log.info("Extension key revoked: user %s", user.id)
    return {"ok": True}
