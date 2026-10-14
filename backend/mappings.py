"""AI mappings (docs/design/open-source.md §1.15; the extension's contract is Part 8 there): when the browser extension
can't read a job page, or the app can't find the jobs on a careers page, the AI works out which parts of the page hold
them, from a trimmed outline of the page the extension sends. All AI traffic goes through the app
(``llm_providers.chat``); the extension holds no AI key. Who may call each endpoint:

    POST   /api/extension/work-out          signed in (an extension key, or a session with its CSRF token), any
                                            workspace of the account (X-Workspace: the tasks tray shows it there).
                                            {kind, url, outline}: the mapping the AI worked out, not saved. For a
                                            careers page the app first reads the page the usual ways, and with the
                                            mapping only when they find nothing; either way the answer holds the
                                            jobs the app itself found ("test"), for the person to confirm before
                                            the source is saved (POST /api/sources with the mapping).
    GET    /api/extension/mappings          signed in (a key or a session): the account's saved job-site mappings
    GET    /api/extension/mappings/{site}   signed in: the account's mapping for that site (404 not_found)
    PUT    /api/extension/mappings/{site}   signed in: saves the account's mapping for that site, {mapping}
                                            (201 new, 200 replaced)
    DELETE /api/extension/mappings/{site}   signed in: forgets it (404 not_found when there is none)

**The outline** is the person's data: it is read with a size cap (``mapping_format.OUTLINE_BODY_MAX`` bytes, before
anything is parsed), checked against the contract and the element caps, rendered for the AI and dropped with the
request. It is never stored and never logged, nor is the AI's answer; the log says which account, what kind of page
and why an answer was refused (a fixed code). **The answer** must be selectors in the agreed shape
(``mapping_format.parse_answer``); anything else is refused whole and the person is told the page couldn't be worked
out. **A saved mapping** is data: per site (a normalised host name) and per account (an older install's accounts
don't share them), sealed under the account's key and found by a blind index of the site. One work-out runs at a time
per account (429 ``busy``).
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend import crypto, llm_providers, mapping_format, tasks
from backend.db import SiteMapping, get_db, utcnow
from backend.sessions import AuthProblem, AuthUser, current_user
from backend.workspaces import current_workspace

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/extension", tags=["extension"])

TASK_KINDS = {"job": "mapping_job", "careers": "mapping_careers"}  # backend/tasks.py KINDS
URL_MAX = 2048
PUT_BODY_MAX = 16 * 1024    # bytes: a mapping is at most a few kilobytes
MAX_SITE_MAPPINGS = 500     # saved job-site mappings per account
SAMPLE = 8                  # jobs shown from a careers page's dry run (as Test shows them)
NOT_WORKED_OUT = {
    "job": "JobHuntPA couldn't work out this site: the AI didn't answer with a page layout it can use. Try again, "
           "or type the job's details in.",
    "careers": "JobHuntPA couldn't work out where this page lists its jobs: the AI didn't answer with a page layout "
               "it can use. Try again later.",
}
BUSY = "JobHuntPA is already working out a page for you. Try again when it has finished."

_busy: set[int] = set()
_busy_lock = threading.Lock()


def _invalid(detail: str, field: Optional[str] = None) -> AuthProblem:
    return AuthProblem(422, "invalid", detail, field=field)


def _iso(value) -> Optional[str]:
    return value.replace(microsecond=0).isoformat() + "Z" if value else None


async def _json_body(request: Request, limit: int, too_big: AuthProblem) -> dict:
    """The request's JSON object, read with a size limit: a body over ``limit`` bytes is refused from its
    Content-Length, or as soon as it grows past it, before any of it is parsed."""
    length = request.headers.get("content-length")
    if length is not None:
        if not length.strip().isdigit():
            raise _invalid("This request's length isn't a number.")
        if int(length) > limit:
            raise too_big
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_big
        chunks.append(chunk)
    try:
        data = await run_in_threadpool(json.loads, b"".join(chunks) or b"null")
    except (ValueError, RecursionError):
        raise _invalid("Send it as JSON.") from None
    if not isinstance(data, dict):
        raise _invalid("Send it as a JSON object.")
    return data


def _http_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > URL_MAX:
        return None
    value = value.strip()
    return value if mapping_format.site_of(value) and value.lower().startswith(("http://", "https://")) else None


# ---------------------------------------------------------------------------
# Working a page out
# ---------------------------------------------------------------------------

@dataclass
class WorkOutIn:
    kind: str
    url: str
    site: str
    outline: mapping_format.Outline


OUTLINE_TOO_LARGE = (f"This page is too big to work out: at most {mapping_format.OUTLINE_BODY_MAX:,} bytes of "
                     "outline can be sent.")


async def work_out_body(request: Request) -> WorkOutIn:
    """The request (``{kind, url, outline}``), checked: 413 ``outline_too_large`` over the caps, 422 ``invalid`` (with
    the field) for anything outside the contract."""
    data = await _json_body(request, mapping_format.OUTLINE_BODY_MAX,
                            AuthProblem(413, "outline_too_large", OUTLINE_TOO_LARGE, field="outline"))
    for name in data:
        if name not in ("kind", "url", "outline"):
            raise _invalid(f'JobHuntPA doesn\'t know the value "{str(name)[:40]}" this extension sends. Update the '
                           "extension.", field=str(name)[:40])
    kind = data.get("kind")
    if kind not in mapping_format.KINDS:
        raise _invalid('Say what kind of page it is: "job" or "careers".', field="kind")
    raw = data.get("url")
    if not isinstance(raw, str) or len(raw) > URL_MAX or not raw.strip().lower().startswith(("http://", "https://")):
        raise _invalid("Only pages with an http:// or https:// address can be worked out.", field="url")
    if kind == "careers":  # (the app reads a careers page itself: never one on the local network or this computer)
        from backend.sources import refused_address

        why = refused_address(raw.strip())
        if why:
            raise AuthProblem(422, "refused_address", f"This address can't be a source: {why}", field="url")
    url = _http_url(raw)
    if url is None:
        raise _invalid("This page's address has no site name JobHuntPA can use (a page at a bare IPv6 address, such "
                       "as http://[2001:db8::1]/, can't be worked out).", field="url")
    try:
        outline = await run_in_threadpool(mapping_format.check_outline, data.get("outline"))
    except mapping_format.OutlineRefused as exc:
        if exc.too_large:
            raise AuthProblem(413, "outline_too_large", str(exc), field="outline") from None
        raise _invalid(str(exc), field="outline") from None
    return WorkOutIn(kind=kind, url=url, site=mapping_format.site_of(url), outline=outline)


@contextmanager
def one_at_a_time(user_id: int) -> Iterator[None]:
    with _busy_lock:
        if user_id in _busy:
            raise AuthProblem(429, "busy", BUSY, headers={"Retry-After": "5"})
        _busy.add(user_id)
    try:
        yield
    finally:
        with _busy_lock:
            _busy.discard(user_id)


def _ai_config(ws: int) -> dict:
    from backend.scorer import config_problem, get_config

    cfg = {**get_config("scoring"), "purpose": "mapping", "workspace_id": ws}
    problem = config_problem(cfg)
    if problem:
        raise AuthProblem(400, "ai_not_set_up", f"Working out a page needs the AI. {problem}")
    return cfg


def ask_ai(body: WorkOutIn, cfg: dict, user_id: int) -> dict:
    """The mapping the AI works out from the outline, checked (``parse_answer``). 502 ``ai_failed`` when the AI
    service fails, 422 ``not_worked_out`` when its answer isn't a mapping."""
    try:
        raw = llm_providers.chat(mapping_format.messages(body.kind, body.url, body.outline), cfg, json_mode=True)
    except llm_providers.ScorerError as exc:
        log.warning("Mapping: user %s, %s page: the AI service failed (%s)", user_id, body.kind,
                    exc.__class__.__name__)
        raise AuthProblem(502, "ai_failed", llm_providers.user_message(exc)) from None
    try:
        found = mapping_format.parse_answer(raw, body.kind)
    except mapping_format.MappingRefused as exc:
        log.info("Mapping: user %s, %s page: the AI's answer was refused (%s)", user_id, body.kind, exc.reason)
        raise AuthProblem(422, "not_worked_out", NOT_WORKED_OUT[body.kind]) from None
    log.info("Mapping: user %s, %s page worked out (%d fields, %d outline elements)", user_id, body.kind,
             len(found["fields"]), body.outline.elements)
    return found


def _test(adapter, postings) -> dict:
    return {"ok": True, "method": adapter.method, "count": len(postings),
            "sample": [{"title": p.title, "location": p.location_text, "url": p.url} for p in postings[:SAMPLE]],
            "notes": list(adapter.notes)}


def _careers(body: WorkOutIn, ws: int, user_id: int) -> dict:
    """A careers page: the app reads it itself, through the scraping layer (robots.txt, crawl delay, honest
    User-Agent). The usual ways first; when they find no jobs, the AI's mapping, and the page read with it."""
    from backend.adapters.apsjobs import is_apsjobs_url
    from backend.adapters.ats import detect_ats
    from backend.adapters.base import SourceError
    from backend.adapters.generic import LISTING_TTL, GenericAdapter, source_adapter
    from backend.scraping.http import FetchError, PoliteClient

    out = {"site": body.site, "kind": "careers", "url": body.url, "mapping": None, "worked_out": False}
    client = PoliteClient()
    try:
        try:
            client.get(body.url, ttl=LISTING_TTL)  # (cached for the readings that follow)
        except FetchError as exc:  # robots.txt, a bot check, an error page: a layout can't help
            return {**out, "test": {"ok": False, "error": str(exc), "notes": []}}
        usual = source_adapter(client, body.url)
        error = "no job postings found on the careers page"
        try:
            postings = usual.list_postings()
        except (SourceError, FetchError, ValueError) as exc:
            postings, error = [], str(exc) or error
        if postings:
            return {**out, "test": _test(usual, postings)}
        if is_apsjobs_url(body.url) or detect_ats(body.url):
            # A job board JobHuntPA reads its own way (APSJobs, an ATS board's address): a search never reads it
            # with a layout, so none is worked out (the dry run must be the reading searches repeat).
            notes = list(getattr(usual, "notes", []))
            notes.append("this address is a job board JobHuntPA reads its own way, so a page layout can't help")
            return {**out, "test": {"ok": False, "error": error, "notes": notes}}
        found = ask_ai(body, _ai_config(ws), user_id)
        adapter = GenericAdapter(client, body.url, mapping=found)
        try:
            postings = adapter.mapped_postings()
            test = _test(adapter, postings)
        except (SourceError, FetchError) as exc:
            test = {"ok": False, "error": str(exc), "notes": list(adapter.notes)}
        return {**out, "mapping": found, "worked_out": True, "test": test}
    finally:
        client.close()


@router.post("/work-out")
def work_out(body: WorkOutIn = Depends(work_out_body), user: AuthUser = Depends(current_user),
             ws: int = Depends(current_workspace)):
    """Works out a page's layout from its outline. Signed in (a key or a session), any workspace of the account. A
    job page: the mapping (not saved: the extension shows the job it finds, then saves it with PUT). A careers page:
    the jobs the app found reading it (``test``), with the mapping when one was needed (else null)."""
    cfg = _ai_config(ws) if body.kind == "job" else None
    with one_at_a_time(user.id):
        with tasks.track(TASK_KINDS[body.kind], ws):
            if body.kind == "careers":
                return _careers(body, ws, user.id)
            found = ask_ai(body, cfg, user.id)
            return {"site": body.site, "kind": "job", "mapping": found, "worked_out": True}


# ---------------------------------------------------------------------------
# Saved job-site mappings
# ---------------------------------------------------------------------------

def _site(value: str) -> str:
    site = mapping_format.site_of(value)
    if site is None or "://" in value:
        raise _invalid("That isn't a site's name (a host name such as jobs.example.com).", field="site")
    return site


def row_for(db: Session, user_id: int, site: str) -> Optional[SiteMapping]:
    """The account's mapping for ``site``: found by the plaintext owner and the blind index, before anything sealed
    is opened."""
    return (db.query(SiteMapping)
            .filter(SiteMapping.user_id == user_id,
                    SiteMapping.site_index == crypto.blind_index("site_mapping", site)).first())


def _out(row: SiteMapping) -> dict:
    return {"site": row.site, "mapping": mapping_format.stored(row.mapping, "job"), "updated_at": _iso(row.updated_at)}


def save(db: Session, user_id: int, site: str, found: dict) -> tuple[SiteMapping, bool]:
    """Saves the account's (checked) mapping for ``site`` (added to ``db``, not committed): (the row, whether it is
    new). 409 ``too_many_mappings`` past ``MAX_SITE_MAPPINGS``."""
    row = row_for(db, user_id, site)
    if row is not None:
        row.mapping, row.updated_at = found, utcnow()
        return row, False
    if db.query(SiteMapping.id).filter(SiteMapping.user_id == user_id).count() >= MAX_SITE_MAPPINGS:
        raise AuthProblem(409, "too_many_mappings", f"You have saved {MAX_SITE_MAPPINGS} sites' layouts, the most "
                                                    "there can be. Forget some you no longer use (Settings).")
    row = SiteMapping(user_id=user_id, site_index=crypto.blind_index("site_mapping", site), site=site, mapping=found)
    db.add(row)
    return row, True


def for_account(db: Session, user_id: int) -> list[SiteMapping]:
    """The account's saved mappings, by site (sorted here: the site is sealed)."""
    rows = db.query(SiteMapping).filter(SiteMapping.user_id == user_id).all()
    return sorted(rows, key=lambda r: r.site or "")


@router.get("/mappings")
def list_mappings(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """The account's saved job-site mappings: site, the fields each finds, when saved. Signed in (a key or a
    session)."""
    out = []
    for row in for_account(db, user.id):
        found = mapping_format.stored(row.mapping, "job")
        out.append({"site": row.site, "fields": list(found["fields"]) if found else [],
                    "updated_at": _iso(row.updated_at)})
    return {"mappings": out, "max": MAX_SITE_MAPPINGS}


@router.get("/mappings/{site}")
def get_mapping(site: str, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """The account's mapping for a site. Signed in (a key or a session)."""
    row = row_for(db, user.id, _site(site))
    if row is None:
        raise AuthProblem(404, "not_found", "No layout is saved for this site.")
    return _out(row)


async def put_body(request: Request) -> dict:
    data = await _json_body(request, PUT_BODY_MAX, AuthProblem(413, "too_large", "This mapping is too big."))
    if set(data) != {"mapping"}:
        raise _invalid("Send {mapping}.", field="mapping")
    return data


@router.put("/mappings/{site}")
def put_mapping(site: str, response: Response, data: dict = Depends(put_body), user: AuthUser = Depends(current_user),
                db: Session = Depends(get_db)):
    """Saves the account's mapping for a site (a job page's: kind ``job``), checked for shape: 201 new, 200 replaced,
    422 ``invalid_mapping``. Signed in (a key or a session)."""
    site = _site(site)
    try:
        found = mapping_format.check_mapping(data["mapping"], "job")
    except mapping_format.MappingRefused as exc:
        raise AuthProblem(422, "invalid_mapping", f"The page layout can't be used: {exc}", field="mapping") from None
    try:
        row, created = save(db, user.id, site, found)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    response.status_code = 201 if created else 200
    log.info("Mapping: user %s saved a job site's layout (%s)", user.id, "new" if created else "replaced")
    return {**_out(row), "created": created}


@router.delete("/mappings/{site}")
def delete_mapping(site: str, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Forgets the account's mapping for a site. Signed in (a key or a session)."""
    row = row_for(db, user.id, _site(site))
    if row is None:
        raise AuthProblem(404, "not_found", "No layout is saved for this site.")
    db.delete(row)
    db.commit()
    log.info("Mapping: user %s forgot a job site's layout", user.id)
    return {"ok": True}
