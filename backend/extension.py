"""The browser extensions (Chrome and Firefox): add the page in the current tab as a job, or as a company source.

The extension's API contract: docs/design/accounts.md §19. Who may call each endpoint:

    GET    /api/extension/status    signed in (an extension key or a session): the key's name and expiry (null for a
                                    session), the account's workspaces and the default one
    POST   /api/extension/preview   signed in, any workspace of the account (X-Workspace): the address a save would
                                    store, and the workspace's job with that address if it has one. Stores nothing.
    POST   /api/extension/jobs      signed in, as above: stores the job as the workspace's own (201), or updates the
                                    workspace's job with that address (200)
    POST   /api/extension/jobs/batch  signed in, as above: adds the jobs of a results page the workspace doesn't
                                    have yet; the ones it has are left as they are
    GET    /api/extension/keys      a session only (an extension key gets 403 token_scope): the account's keys
    POST   /api/extension/keys      a session only, with a recent sign-in (403 reauth_required): a new key {label},
                                    shown once
    DELETE /api/extension/keys/{id} a session only: revokes one of the account's keys (404 for anyone else's)

**Extension keys.** ``jhpa_`` and 43 base64url characters (32 random bytes). The server keeps a ``sessions`` row
(``state = "extension"``) whose id is a hash of the secret and whose ``wrapped_dek`` is the person's data key
wrapped under a key derived from the secret, so a key opens the person's data the way a session cookie does, and the
database alone opens nothing. A key is checked by backend/sessions.py (``Authorization: Bearer jhpa_...``; any
other ``Authorization`` value is left to the cookie's checks): no cookie, CSRF token or Origin is used with it, and
it reaches only the endpoints in ``sessions.KEY_ENDPOINTS``. It never counts as a recent sign-in. It lasts 60 days
from its last use and a year at most; every "sign out everywhere" ends it (a password change that signs out the
other devices, a reset, recovery, "Sign out everywhere else", an admin turning off the account or its two-step
sign-in, deleting the account).

**Jobs.** The extension reads the page in the person's own browser and sends only the finished job: its values and
the description as plain text (``JobIn``). No HTML and no whole-page text reach the server, and nothing here parses
or re-reads a page: the request is checked (types, limits), its values trimmed, and the address to store decided
(``stored_url``), because that decides which job a save updates. The job is then stored like an imported page
(``pool.import_private``): private to the workspace unless the shared pool already has the address; the exclusion
filters, the pipeline's own derived values (country, office days, work mode) and the duplicate scan follow as for an
import. The save's answer also says ``excluded_reason``: why the filters leave the job out of the Jobs list, or null.

**A page of results.** On a search results page the person has open (SEEK's, signed in, on any view or page of it)
the extension reads the job cards and sends the ones the person ticked: for each its own address and the card's
values, the summary as plain text (``ListIn``). The person turns the pages; nothing here fetches anything. Each
address must be on the results page's own site (``listed_url``), and a batch only ever adds: a job the workspace
already has is left exactly as it is, because a card says less than a stored ad; a posting the pool has is linked
and read as the pool has it; anything else becomes a private job with the card's values (``pool.add_listed``).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any, Literal, Optional
from urllib.parse import parse_qs, urljoin, urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend import crypto, sessions
from backend.adapters.base import Posting
from backend.adapters.seek import JOB_URL as SEEK_JOB_URL
from backend.db import PROJECT_ROOT, AuthSession, Job, UserKeys, Workspace, get_db, scrub_wal, utcnow
from backend.employment import normalise_employment_type
from backend.scraping.text import clean, normalize_block
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth
from backend.workspaces import current_workspace

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/extension", tags=["extension"])

API_VERSION = 2            # 2: POST /api/extension/jobs/batch (a page of results)
SOURCE = "browser"          # jobs.source of a job added from the extension (frontend: "browser extension")
MAX_BODY = 512 * 1024       # bytes: the longest job the contract allows is ~110,000 characters
MAX_KEYS = 20               # extension keys per account
LABEL_MAX = 80
FULL_AD_CHARS = 200         # a description at least this long counts as the full ad (like the JSON-LD importer)
SEEK_HOSTS = ("seek.com.au", "seek.co.nz")
WORK_MODES = ("onsite", "hybrid", "remote", "unknown")
# The longest value of each field (characters), as the contract states them.
LIMITS = {"url": 2048, "canonical_url": 2048, "title": 500, "company": 500, "location_text": 500, "salary_text": 500,
          "employment_type": 100, "posted_at": 40, "description": 100_000}
MAX_BATCH = 50              # jobs in one results-page request (SEEK shows about 30 a page)
SUMMARY_MAX = 5000          # characters of a results card's summary (a teaser and a few points)
INDUSTRY_MAX = 200          # characters of a results card's classification

_keys_lock = threading.Lock()  # counting an account's keys and adding one happen together (one server process)


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
# The job a request carries
# ---------------------------------------------------------------------------

def _text(value: Any) -> Any:
    """A string as UTF-8 can store it: an unpaired surrogate (JavaScript strings may hold one) becomes "?"; null is
    an absent value (""). Anything else is left for the type check."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,9})?)?"
                       r"(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?", re.ASCII)


class JobIn(BaseModel):
    """The job as the extension read it from the page (and the person corrected it in the popup)."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field("", max_length=LIMITS["title"])
    company: str = Field("", max_length=LIMITS["company"])
    location_text: str = Field("", max_length=LIMITS["location_text"])
    salary_text: str = Field("", max_length=LIMITS["salary_text"])
    employment_type: str = Field("", max_length=LIMITS["employment_type"])
    work_mode: Literal["onsite", "hybrid", "remote", "unknown", ""] = ""
    posted_at: str = Field("", max_length=LIMITS["posted_at"])
    url: str = Field("", max_length=LIMITS["url"])
    description: str = Field("", max_length=LIMITS["description"])
    description_from: Literal["selection", "structured", "site", "main", "body", ""] = ""
    method: Literal["jsonld", "site", "page", ""] = ""

    @field_validator("*", mode="before")
    @classmethod
    def _strings(cls, value: Any) -> Any:
        return _text(value)

    @field_validator("posted_at")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        value = value.strip()
        if value and not _ISO_DATE.fullmatch(value):
            raise ValueError("not an ISO date")
        return value


class PageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., max_length=LIMITS["url"])
    canonical_url: str = Field("", max_length=LIMITS["canonical_url"])
    job: JobIn = Field(default_factory=JobIn)

    @field_validator("canonical_url", mode="before")
    @classmethod
    def _canonical(cls, value: Any) -> Any:
        return _text(value)

    @field_validator("job", mode="before")
    @classmethod
    def _job(cls, value: Any) -> Any:
        return {} if value is None else value


class ListedIn(BaseModel):
    """One job of a results page, as its card shows it: the job's own address and the listing's summary."""
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., max_length=LIMITS["url"])
    title: str = Field("", max_length=LIMITS["title"])
    company: str = Field("", max_length=LIMITS["company"])
    location_text: str = Field("", max_length=LIMITS["location_text"])
    salary_text: str = Field("", max_length=LIMITS["salary_text"])
    employment_type: str = Field("", max_length=LIMITS["employment_type"])
    work_mode: Literal["onsite", "hybrid", "remote", "unknown", ""] = ""
    posted_at: str = Field("", max_length=LIMITS["posted_at"])
    industry: str = Field("", max_length=INDUSTRY_MAX)  # the card's classification (the dealbreakers read it)
    description: str = Field("", max_length=SUMMARY_MAX)

    @field_validator("*", mode="before")
    @classmethod
    def _strings(cls, value: Any) -> Any:
        return _text(value)

    @field_validator("posted_at")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        value = value.strip()
        if value and not _ISO_DATE.fullmatch(value):
            raise ValueError("not an ISO date")
        return value


class ListIn(BaseModel):
    """A results page: its address and the jobs on it the person ticked."""
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., max_length=LIMITS["url"])
    jobs: list[ListedIn] = Field(..., min_length=1, max_length=MAX_BATCH)

    @field_validator("url", mode="before")
    @classmethod
    def _url(cls, value: Any) -> Any:
        return _text(value)


def _invalid(detail: str, field: Optional[str] = None) -> AuthProblem:
    return AuthProblem(422, "invalid", detail, field=field)


_FIELD_NAMES = {"url": "the page address", "canonical_url": "the page's canonical address", "job": "the job",
                "title": "the job title", "company": "the employer", "location_text": "the location",
                "salary_text": "the salary", "employment_type": "the employment type", "work_mode": "the work mode",
                "posted_at": "the date posted", "description": "the description",
                "description_from": "where the description came from", "method": "how the page was read",
                "jobs": "the jobs", "industry": "the classification"}
TOO_BIG = "This job is too big to send. Select the job ad's text on the page and try again."


def _too_big(field: Optional[str] = None, limit: Optional[int] = None) -> AuthProblem:
    limit = limit or LIMITS.get(field or "")
    if limit:
        detail = f"{_FIELD_NAMES[field][0].upper()}{_FIELD_NAMES[field][1:]} is too long: at most {limit:,} " \
                 "characters can be sent."
        return AuthProblem(413, "too_large", detail, field=field)
    return AuthProblem(413, "too_large", TOO_BIG)


def _refused(exc: ValidationError) -> AuthProblem:
    """The answer to a body that doesn't fit ``PageIn`` or ``ListIn``: 422 ``invalid`` for a wrong type, an unknown
    field or a value outside its list (the first one), else 413 ``too_large`` for a value over its limit (also more
    jobs than one request takes). ``field`` names the value: a job value by its own name (``title``), anything else
    by its place (``url``, ``job``, ``jobs``, or the unknown name)."""
    errors = exc.errors()
    wrong = [e for e in errors if e.get("type") not in ("string_too_long", "too_long")]
    error = (wrong or errors)[0] if errors else {}
    where = [str(p) for p in error.get("loc", ()) if isinstance(p, str)]
    name = where[-1] if where else ""
    if not wrong:
        if error.get("type") == "too_long":  # the list of jobs
            return AuthProblem(413, "too_large", f"At most {MAX_BATCH} jobs can be added at once.", field="jobs")
        limit = (error.get("ctx") or {}).get("max_length")
        return _too_big(name, limit if name in _FIELD_NAMES else None)
    if error.get("type") == "too_short":
        return _invalid("No jobs were sent. Tick at least one job, then add.", field="jobs")
    if error.get("type") == "extra_forbidden":
        return _invalid(f'JobHuntPA doesn\'t know the value "{name[:40]}" this extension sends. Update the '
                        "extension.", field=name[:40] or None)
    return _invalid(f"Check {_FIELD_NAMES.get(name, 'what was sent')}: it is missing or not what JobHuntPA "
                    "expects.", field=name[:40] or None)


def _http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


async def _json_body(request: Request) -> dict:
    """The request's JSON object, read with a size limit: a body over ``MAX_BODY`` bytes is refused (413) from its
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
    except (ValueError, RecursionError):  # not JSON, or nested too deep to read
        raise _invalid("Send the job as JSON.") from None
    if not isinstance(data, dict):
        raise _invalid("Send the job as a JSON object.")
    return data


async def page_body(request: Request) -> PageIn:
    """The request's job (``_json_body``'s limit, then the contract's types and limits)."""
    data = await _json_body(request)
    try:
        page = PageIn.model_validate(data)
    except ValidationError as exc:
        raise _refused(exc) from None
    page.url = page.url.strip()
    if not _http_url(page.url):
        raise _invalid("Only pages with an http:// or https:// address can be added.", field="url")
    page.canonical_url = page.canonical_url.strip()
    if page.canonical_url and not _http_url(page.canonical_url):
        raise _invalid("The page's canonical address must be an http:// or https:// address.", field="canonical_url")
    return page


async def list_body(request: Request) -> ListIn:
    """The request's results page (``_json_body``'s limit, then the contract's types and limits)."""
    data = await _json_body(request)
    try:
        listing = ListIn.model_validate(data)
    except ValidationError as exc:
        raise _refused(exc) from None
    listing.url = listing.url.strip()
    if not _http_url(listing.url):
        raise _invalid("Only pages with an http:// or https:// address can be added.", field="url")
    return listing


# ---------------------------------------------------------------------------
# The address to store
# ---------------------------------------------------------------------------

def _host_port(url: str) -> Optional[tuple[str, int]]:
    """The host (lower case) and port (the scheme's own when none is given) of an http(s) address with no user name
    or password in it, else None."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc:
        return None
    return host, port or (443 if parts.scheme == "https" else 80)


def seek_job_id(url: str) -> Optional[str]:
    """SEEK's id of the job at ``url`` when it is one of SEEK's own sites (seek.com.au, seek.co.nz or a subdomain of
    one) showing a job: ``/job/<digits>`` or ``?jobId=<digits>``. Else None."""
    site = _host_port(url)
    if site is None or not any(site[0] == d or site[0].endswith("." + d) for d in SEEK_HOSTS):
        return None
    parts = urlsplit(url)
    m = re.fullmatch(r"/job/([0-9]{1,20})(?:/.*)?", parts.path, re.ASCII)
    if m:
        return m.group(1)
    ids = parse_qs(parts.query).get("jobId") or []
    return ids[0] if ids and re.fullmatch(r"[0-9]{1,20}", ids[0], re.ASCII) else None


def stored_url(url: str, job_url: str = "", canonical_url: str = "") -> tuple[str, str]:
    """(The address a job read from the page at ``url`` is stored under, SEEK's job id or ""): a SEEK job (judged
    from ``url`` alone) as ``https://www.seek.com.au/job/<id>``, the address the searches give it; else the posting's
    own address, then the page's canonical one, when it is on exactly the same host and port as ``url`` (often
    without tracking parameters); else ``url``. Another site's address is never taken from the page, so a page can't
    choose which of the person's jobs (or the pool's postings) a save updates."""
    seek_id = seek_job_id(url)
    if seek_id:
        return SEEK_JOB_URL.format(id=seek_id), seek_id
    site = _host_port(url)
    for own in (job_url, canonical_url):
        if not own or site is None:
            continue
        try:
            joined = urljoin(url, own)
        except ValueError:
            continue
        if _host_port(joined) == site:
            return joined, ""
    return url, ""


def listed_url(page_url: str, job_url: str) -> Optional[tuple[str, str]]:
    """(The address a job listed on the results page at ``page_url`` is stored under, SEEK's job id or ""), or None
    when the job's address isn't one on the page's own site: an http(s) address on exactly the same host and port
    (a relative one is the page's site), with no user name. A SEEK job is stored as the searches store it. A results
    page therefore can't add a job under another site's address."""
    site = _host_port(page_url)
    try:
        joined = urljoin(page_url, job_url.strip())
    except ValueError:
        return None
    if site is None or not job_url.strip() or _host_port(joined) != site:
        return None
    seek_id = seek_job_id(joined)
    return (SEEK_JOB_URL.format(id=seek_id), seek_id) if seek_id else (joined, "")


def listed_posting(page_url: str, job: ListedIn) -> Optional[Posting]:
    """The job to store for one card: the browser's values trimmed, as a listing's summary. None when its address
    isn't on the page's site."""
    where = listed_url(page_url, job.url)
    if where is None:
        return None
    description = normalize_block(job.description)
    return Posting(url=where[0], title=clean(job.title), company=clean(job.company), external_id=where[1],
                   location_text=clean(job.location_text), work_mode=job.work_mode or "unknown",
                   employment_type=normalise_employment_type(clean(job.employment_type)),
                   salary_text=clean(job.salary_text), description=description,
                   detail_status="summary" if description else "none", posted_at=job.posted_at,
                   industry=clean(job.industry))


def posting_of(page: PageIn) -> Posting:
    """The job to store: the browser's values trimmed, under the address ``stored_url`` picks. Nothing is read from
    the page here; the pipeline works out what follows from the text (country, office days, work mode)."""
    job = page.job
    url, external_id = stored_url(page.url, job.url.strip(), page.canonical_url)
    description = normalize_block(job.description)
    return Posting(url=url, title=clean(job.title), company=clean(job.company), external_id=external_id,
                   location_text=clean(job.location_text), work_mode=job.work_mode or "unknown",
                   employment_type=normalise_employment_type(clean(job.employment_type)),
                   salary_text=clean(job.salary_text), description=description,
                   detail_status="full" if len(description) >= FULL_AD_CHARS else "summary" if description else "none",
                   posted_at=job.posted_at)


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


def _known(db: Session, ws: int, url: str, page_url: str) -> Optional[Job]:
    """The workspace's job with the address to store, else with the page's own address (saved before under it), as
    the job that is listed for it (a merged copy's original), or None."""
    from backend.pool import job_by_url

    job = job_by_url(db, ws, url)
    if job is None and page_url != url:
        job = job_by_url(db, ws, page_url)
    return _listed(db, job) if job is not None else None


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
def preview(page: PageIn = Depends(page_body), user: AuthUser = Depends(current_user), db: Session = Depends(get_db),
            ws: int = Depends(current_workspace)):
    """The address a save would store, and the workspace's (listed) job with it, for the popup's "Already in
    JobHuntPA". Stores nothing; ``job`` may be partial or empty."""
    url, _ = stored_url(page.url, page.job.url.strip(), page.canonical_url)
    job = _known(db, ws, url, page.url)
    existing = None if job is None else {"id": job.id, "title": job.title or "", "status": job.status,
                                         "link": job_link(job.id, ws)}
    return {"url": url, "existing": existing}


@router.post("/jobs")
def save_job(response: Response, page: PageIn = Depends(page_body), user: AuthUser = Depends(current_user),
             db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """Stores the job as this workspace's own (as ``/api/import/page`` does): a new job (201), or the workspace's job
    with that address brought up to date (200). When that job is a merged copy (backend.duplicates), the job it was
    merged into is updated instead: the one the person sees in the list. The exclusion filters and the duplicate
    scan run as for an import, and the job waits for its score like one. ``excluded_reason``: why the filters leave
    the saved job out of the Jobs list (as the list says it), or null."""
    from backend.duplicates import scan_workspace
    from backend.pipeline import import_one

    posting = posting_of(page)
    if not posting.title:
        raise _invalid(NO_TITLE, field="title")
    known = _known(db, ws, posting.url, page.url)
    if known is not None:
        posting.url = known.url or posting.url
    job_id, created = import_one(posting, SOURCE, ws)
    scan_workspace(ws)
    db.expire_all()
    job = _listed(db, db.get(Job, job_id))
    response.status_code = 201 if created else 200
    log.info("Browser extension: job %s %s in workspace %s", job.id, "added" if created else "updated", ws)
    return {"job_id": job.id, "created": created, "title": job.title or "", "company": job.company or "",
            "link": job_link(job.id, ws), "excluded_reason": job.excluded_reason or None}


@router.post("/jobs/batch")
def save_listed(listing: ListIn = Depends(list_body), user: AuthUser = Depends(current_user),
                db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """Adds the jobs of a results page the person has open. Signed in (a key or a session), any workspace of the
    account. Only jobs the workspace doesn't have are added, each from its card's summary (a pool posting with the
    address is linked instead); a job it has is left as it is. Nothing is stored unless every job has a title and
    an address on the page's own site (422 ``invalid``). The filters and the duplicate scan run as for an import.
    The answer lists the jobs in the order sent, each as the job that is listed for it."""
    from backend.duplicates import scan_workspace
    from backend.pipeline import add_listed

    postings = []
    for job in listing.jobs:
        posting = listed_posting(listing.url, job)
        if posting is None:
            raise _invalid("A job on this page has an address on another site, so it can't be added from here.",
                           field="url")
        if not posting.title:
            raise _invalid("A job on this page has no title. Untick it, then add the others.", field="title")
        postings.append(posting)
    stored = add_listed(postings, SOURCE, ws)
    added = sum(1 for _, is_new in stored if is_new)
    if added:
        scan_workspace(ws)
    db.expire_all()
    out = []
    for (job_id, is_new), posting in zip(stored, postings):
        job = _listed(db, db.get(Job, job_id))
        out.append({"url": posting.url, "job_id": job.id, "created": is_new, "title": job.title or "",
                    "company": job.company or "", "link": job_link(job.id, ws),
                    "excluded_reason": job.excluded_reason or None})
    log.info("Browser extension: %s of %s jobs from a results page added in workspace %s", added, len(stored), ws)
    return {"added": added, "existing": len(stored) - added, "jobs": out}


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
    with _keys_lock:  # two requests at once can't both pass the count
        try:
            if len(_live_keys(db, user.id)) >= MAX_KEYS:
                raise AuthProblem(409, "too_many_keys", f"You have {MAX_KEYS} connected extensions, the most there "
                                                        "can be. Disconnect one you no longer use first.")
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
