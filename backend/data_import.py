"""Import: bring workspaces back from a data export's zip (docs/design/open-source.md §1.7 and Part 7).

    POST /api/account/import/check            recent password; body: the export zip. The workspaces in it, with counts
    POST /api/account/import?pick=0,2         recent password; body: the same zip. Imports the picked workspaces

Both are POSTs with the CSRF token, for the signed-in person only, with the password entered in the last 15 minutes
(as the export asks): checked before the body is read. One import (or check) per account at a time (429) and
``MAX_AT_ONCE`` in the whole app (503), since the zip is held in memory; an upload that hasn't arrived within
``READ_SECONDS`` gives the place back (408).

**The zip is someone else's file.** It is read in memory from the request body (never spooled or unpacked to disk,
and never by its own names): at most ``MAX_ZIP_BYTES`` of it, refused when any entry's path could leave its folder
(absolute, a drive letter, a backslash or a ``..`` part) or is encrypted. Only the import file (``FILE_NAME``) and the
original files it names are read, each counted as it is unpacked: no more than ``MAX_FILE_BYTES`` for the import file,
``docs.MAX_UPLOAD_BYTES`` for an original, ``MAX_UNPACKED_BYTES`` in all, never more than the entry says it holds, and
no entry over a megabyte that unpacks to more than ``MAX_RATIO`` times its packed size. Every value is typed and
bounded on the way in (backend/import_file.py) and stored as data; pages show it escaped, as any other job or document.

**What an import does.** Each picked workspace becomes a new workspace of the signed-in account (nothing is merged
into an existing one), named as in the file, or with " (imported 2026-10-13)" when the name is taken, then ", 2", ", 3".
Documents (originals sealed again under this account's key), jobs, chats, the search setup, pins, sources (with their
page layouts; never one on the local network or this computer) and schedule come with it. The account's saved job-site
layouts come in for the sites it has none for. A job whose address the shared pool already has is linked to that posting, with the workspace's own
values on top; any other job is a private job. Nothing is written to the pool. Company research from the file is
taken for the companies of the imported jobs only where this install has none, or an older one. Everything happens
in one transaction: a failed import leaves nothing behind (its sealed uploads are deleted again).

The log says that user N imported, with counts, never a name or any content.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import threading
import zipfile
import zlib
from datetime import date
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend import commute, docs, mapping_format, schedule, source_bulk
from backend.db import (
    JOB_CONTENT,
    ChatMessage,
    ChatThread,
    CompanyProfile,
    CompanySource,
    CoverLetter,
    DealbreakerSet,
    Document,
    FitResult,
    Job,
    Pin,
    Posting,
    SearchSchedule,
    UserProfile,
    Workspace,
    company_key,
    get_db,
    utcnow,
)
from backend.export import MAX_EXPORT_BYTES
from backend.import_file import (
    APPLIED_AT,
    FILE_NAME,
    FIRST_APP_VERSION,
    FORMAT,
    FORMAT_VERSION,
    CompanyIn,
    ImportFile,
    WorkspaceIn,
    web_address,
)
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])

# An export holds at most MAX_EXPORT_BYTES of files before compression, the import file and every text file in
# every type included (backend/export.py): each limit here takes in whatever an export can hold.
MAX_ZIP_BYTES = MAX_EXPORT_BYTES + 50 * 1024 * 1024   # the uploaded zip (and the zip's own headers)
MAX_FILE_BYTES = MAX_EXPORT_BYTES                    # the import file, unpacked
MAX_UNPACKED_BYTES = MAX_EXPORT_BYTES + 20 * 1024 * 1024  # the import file and the originals together, unpacked
# Jobs, documents, chat messages, sources, pins and companies in the whole file: each becomes objects in memory before
# anything is written, so a file of a great many tiny ones is refused before they are made (a Raspberry Pi has little).
MAX_ITEMS = 200_000
MAX_RATIO = 200                          # unpacked / packed, for an entry over RATIO_FROM bytes unpacked
RATIO_FROM = 1024 * 1024
MAX_ENTRIES = 200_000
MAX_AT_ONCE = 1                          # imports (and checks) at the same time, for everyone: the zip is in memory
NAME_MAX = 80                            # a workspace's name (backend/workspaces.py)
READ_CHUNK = 1024 * 1024
READ_SECONDS = 600 + MAX_ZIP_BYTES / (100 * 1024)  # the upload of the largest zip at 100 KB/s, and ten minutes more


def problem(status: int, code: str, detail: str) -> AuthProblem:
    return AuthProblem(status, code, detail)


def too_large() -> AuthProblem:
    return problem(413, "import_too_large", f"This file is over {MAX_ZIP_BYTES // 1024 // 1024} MB, which is more "
                                            "than an export from JobHuntPA holds, so it wasn't imported.")


def not_an_export() -> AuthProblem:
    return problem(422, "import_not_export", "This file isn't a JobHuntPA export. Choose the .zip file that "
                                             "Download your data saved.")


def damaged(where: str = "") -> AuthProblem:
    detail = "The import file in this export is damaged, so nothing was imported."
    if where:
        detail += f" (The problem is at {where}.)"
    return problem(422, "import_damaged", detail)


# ---------------------------------------------------------------------------
# Reading the zip
# ---------------------------------------------------------------------------

def unsafe_path(name: str) -> bool:
    """A path that could leave the folder it is unpacked into, or name something else than a file in it."""
    return (not name or name.startswith("/") or "\\" in name or "\x00" in name or bool(re.match(r"^[A-Za-z]:", name))
            or any(part in ("..", ".") for part in name.split("/")))


class ExportZip:
    """An export's zip, opened in memory, with only the import file and the originals it names readable."""

    def __init__(self, data: bytes) -> None:
        if len(data) > MAX_ZIP_BYTES:
            raise too_large()
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zip.infolist()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError):
            raise not_an_export() from None
        if len(infos) > MAX_ENTRIES:
            raise not_an_export()
        for info in infos:
            if unsafe_path(info.filename):
                raise problem(422, "import_unsafe_path", "This zip has a file whose path leads out of its folder, so it "
                                                         "wasn't opened. It wasn't made by JobHuntPA.")
        self.infos = {i.filename: i for i in infos if not i.is_dir()}
        found = [n for n in self.infos if n == FILE_NAME or n.endswith("/" + FILE_NAME)]
        if len(found) > 1:
            raise problem(422, "import_two_files", "This zip holds more than one export. Import each export on its own.")
        if not found:
            if any(n == "Account.md" or n.endswith("/Account.md") for n in self.infos):
                raise problem(422, "import_no_file", "This export has no import file: it was made by a JobHuntPA older "
                                                     f"than {FIRST_APP_VERSION}. Make a new export with JobHuntPA "
                                                     f"{FIRST_APP_VERSION} or later and import that.")
            raise not_an_export()
        self.base = found[0][: -len(FILE_NAME)]   # the export's folder ("JobHuntPA export 2026-10-13/")
        self.unpacked = 0

    def read(self, name: str, limit: int) -> bytes:
        """An entry's bytes, unpacked in memory with every limit counted as it goes."""
        info = self.infos[name]
        if info.flag_bits & 0x1:
            raise problem(422, "import_encrypted", "This zip is password-protected. Import the zip exactly as "
                                                   "Download your data saved it.")
        if info.file_size > limit or self.unpacked + info.file_size > MAX_UNPACKED_BYTES:
            raise too_large_unpacked()
        if info.file_size > RATIO_FROM and info.file_size > MAX_RATIO * max(1, info.compress_size):
            raise too_large_unpacked()
        out = bytearray()
        try:
            with self.zip.open(info) as f:
                while chunk := f.read(READ_CHUNK):
                    out += chunk
                    self.unpacked += len(chunk)
                    if len(out) > info.file_size or len(out) > limit or self.unpacked > MAX_UNPACKED_BYTES:
                        raise too_large_unpacked()
        except (zipfile.BadZipFile, OSError, ValueError, EOFError, zlib.error, NotImplementedError, RuntimeError):
            raise damaged() from None
        return bytes(out)

    def original(self, path: Optional[str]) -> Optional[str]:
        """The zip's entry for a document's original file named in the import file, if it is there and safe."""
        if not path or unsafe_path(path):
            return None
        name = self.base + path
        return name if name in self.infos and name != self.base + FILE_NAME else None

    def import_file(self) -> ImportFile:
        """The import file, read and checked: refused when it is from a newer format version."""
        raw = self.read(self.base + FILE_NAME, MAX_FILE_BYTES)

        def no_constant(_value):  # NaN and Infinity aren't JSON
            raise ValueError("not JSON")
        try:
            data = json.loads(raw.decode("utf-8"), parse_constant=no_constant)
        except (ValueError, RecursionError):
            raise damaged() from None
        del raw
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise damaged()
        version = data.get("format_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise damaged()
        if version > FORMAT_VERSION:
            raise problem(422, "import_newer", "This export was made by a newer JobHuntPA (import file format "
                                               f"{version}); this one reads format {FORMAT_VERSION}. Update JobHuntPA, "
                                               "then import it again.")
        if items(data) > MAX_ITEMS:
            raise problem(413, "import_too_many", f"This file holds more than {MAX_ITEMS:,} jobs, documents, chat "
                                                  "messages and other things, which is more than one import takes. "
                                                  "Nothing was imported.")
        try:
            return ImportFile.model_validate(data)
        except ValidationError as exc:
            # Only the place of the first problem: an error's message can quote the value, which is the person's text.
            where = ".".join(str(p) for p in exc.errors(include_input=False, include_url=False)[0]["loc"])
            raise damaged(where) from None
        except RecursionError:
            raise damaged() from None


def items(data: dict) -> int:
    """How many jobs, documents, chat messages, sources, pins, schedule days and times and companies the (parsed, not
    yet checked) file holds: counted on the plain JSON, before any of them becomes an object."""
    def size(value) -> int:
        return len(value) if isinstance(value, list) else 0

    workspaces = data.get("workspaces")
    total = size(workspaces) + size(data.get("companies"))
    for ws in workspaces if isinstance(workspaces, list) else []:
        if not isinstance(ws, dict):
            continue
        total += sum(size(ws.get(name)) for name in ("jobs", "documents", "chats", "sources", "pins"))
        sched = ws.get("schedule")
        if isinstance(sched, dict):
            total += size(sched.get("days")) + size(sched.get("times"))
        for chat in ws.get("chats") if isinstance(ws.get("chats"), list) else []:
            total += size(chat.get("messages")) if isinstance(chat, dict) else 0
    return total


def too_large_unpacked() -> AuthProblem:
    return problem(413, "import_too_large", "This zip unpacks to more than an export from JobHuntPA can hold, so it "
                                            "wasn't imported.")


def counts(ws: WorkspaceIn) -> dict:
    """What the file holds for a workspace (the check's list). An import says what it brought in instead."""
    return {"jobs": sum(1 for j in ws.jobs if j.duplicate_of is None), "documents": len(ws.documents),
            "chats": len(ws.chats), "cover_letters": sum(1 for j in ws.jobs if j.cover_letter is not None),
            "sources": len(ws.sources), "pins": len(ws.pins)}


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def _fold(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def new_name(wanted: str, taken: set[str], today: date) -> str:
    """The workspace's name, or with " (imported <today>)" when that is taken, then ", 2", ", 3"... ``taken`` holds
    the account's names (folded); the chosen one is added to it."""
    base = " ".join((wanted or "").split()) or "Imported workspace"
    n = 0
    while True:
        tail = "" if n == 0 else f" (imported {today.isoformat()})" if n == 1 else f" (imported {today.isoformat()}, {n})"
        name = base[: max(1, NAME_MAX - len(tail))].rstrip() + tail
        if _fold(name) not in taken:
            taken.add(_fold(name))
            return name
        n += 1


# ---------------------------------------------------------------------------
# Writing the rows
# ---------------------------------------------------------------------------

def starting_points_in(profile: UserProfile, points: list, pin_ids: dict[int, int]) -> None:
    """The search setup's starting points, stored as the search setup stores them (backend/commute.py): a pin start
    names the new copy of its pin (``pin_ids``: file ref to new row id) and is left out when the file has no such pin;
    a text item (the first import files' "Commute from" stations) is a public-transport place. The older columns are
    emptied, as a save of the search setup does (backend/profile.py)."""
    items = []
    for p in points:
        if isinstance(p, str):
            items.extend(commute.legacy_starting_points([p]))
        elif p.pin is not None:
            if p.pin in pin_ids:
                items.append({"label": p.label or "", "pin_id": pin_ids[p.pin], "use": p.use or "both"})
        elif p.place:
            items.append({"label": p.label or "", "place": p.place, "use": p.use or "both"})
    profile.starting_points = commute.clean_starting_points(items)
    profile.commute_from, profile.commute_arrive_by, profile.commute_leave_at = None, "", ""


_web = web_address  # (pages link these addresses: only a web address is kept)


class Importer:
    """One import, in the importing person's key scope, on one database session."""

    def __init__(self, db: Session, user_id: int, data: ImportFile, archive: ExportZip) -> None:
        self.db, self.user_id, self.data, self.archive = db, user_id, data, archive
        self.stored: list[str] = []   # uploads sealed so far (deleted again if the import fails)
        self.company_keys: set[str] = set()
        self.totals = {"jobs": 0, "documents": 0, "chats": 0, "originals": 0}

    def run(self, picked: list[int], today: date) -> dict:
        taken = {_fold(w.name) for w in self.db.query(Workspace).filter(Workspace.owner_id == self.user_id)}
        made = []
        for index in picked:
            ws_in = self.data.workspaces[index]
            ws = Workspace(name=new_name(ws_in.name, taken, today), owner_id=self.user_id)
            self.db.add(ws)
            self.db.flush()
            made.append({"id": ws.id, "name": ws.name, **self.workspace(ws.id, ws_in)})
        companies = self.research()
        return {"workspaces": made, "companies": companies, "site_mappings": self.site_mappings()}

    def site_mappings(self) -> int:
        """The file's saved job-site mappings, for the sites this account has none for (its own are kept), each
        checked for shape (another shape is left out), up to the account's limit."""
        from backend import mappings

        db, added = self.db, 0
        have = len(mappings.for_account(db, self.user_id))
        for item in self.data.site_mappings:
            site = mapping_format.site_of(item.site) if isinstance(item.site, str) and "://" not in item.site else None
            found = mapping_format.stored(item.mapping, "job")
            if site is None or found is None or have >= mappings.MAX_SITE_MAPPINGS:
                continue
            if mappings.row_for(db, self.user_id, site) is not None:
                continue
            row, _ = mappings.save(db, self.user_id, site, found)
            if item.updated_at is not None:
                row.updated_at = item.updated_at
            db.flush()
            added += 1
            have += 1
        return added

    def workspace(self, ws: int, data: WorkspaceIn) -> dict:
        """Writes one workspace's rows; returns what it brought in (as ``counts`` names them)."""
        db = self.db
        made = {"jobs": 0, "documents": 0, "chats": 0, "cover_letters": 0, "sources": 0, "pins": len(data.pins)}
        pin_ids: dict[int, int] = {}  # (pins first: a starting point names its pin's new row)
        for p in data.pins:
            pin = Pin(workspace_id=ws, label=p.label or p.kind.capitalize(), kind=p.kind, lat=p.lat, lng=p.lng,
                      radius_km=p.radius_km)
            db.add(pin)
            if p.ref is not None and p.ref not in pin_ids:
                db.flush()
                pin_ids[p.ref] = pin.id
        setup = data.search_setup
        if setup is not None and setup.model_fields_set - {"dealbreakers"}:  # (a setup can hold dealbreakers alone)
            profile = UserProfile(id=ws, titles=list(setup.titles), keywords_include=list(setup.keywords_include),
                                  keywords_exclude=list(setup.keywords_exclude), salary_floor=setup.salary_floor,
                                  remote_aus_ok=setup.remote_aus_ok, remote_global_ok=setup.remote_global_ok,
                                  allow_hybrid=setup.allow_hybrid, max_office_days=setup.max_office_days,
                                  allow_onsite=setup.allow_onsite, seek_enabled=setup.seek_enabled,
                                  seek_locations=list(setup.seek_locations))
            starting_points_in(profile, setup.starting_points, pin_ids)
            db.add(profile)
        if setup is not None and setup.dealbreakers is not None:
            db.add(DealbreakerSet(id=ws, industries=list(setup.dealbreakers.industries),
                                  keywords=list(setup.dealbreakers.keywords)))
        if data.schedule is not None:
            s = data.schedule
            db.add(SearchSchedule(id=ws, enabled=s.enabled, times=list(s.times), days=sorted(set(s.days)),
                                  skip_holidays=s.skip_holidays, state=s.state))
        else:
            schedule.start_off(db, ws)  # (a file with no schedule: a new workspace's scheduled searches start off)
        sources: dict[int, CompanySource] = {}
        seen_urls: set[str] = set()
        for s in data.sources:
            url = _web(s.url)
            # (a source is read by this install: never one on the local network or this computer, as a single add)
            if url is None or url in seen_urls or source_bulk.address_flags(url).blocked:
                continue
            seen_urls.add(url)
            row = CompanySource(workspace_id=ws, label=s.label or "", careers_url=url, url=url,
                                source_type=s.type or "auto", enabled=s.enabled,
                                mapping=mapping_format.stored(s.mapping, "careers"))
            db.add(row)
            made["sources"] += 1
            if s.ref is not None:
                sources[s.ref] = row
        made["documents"] = self.documents(ws, data)
        db.flush()
        jobs = self.jobs(ws, data, {ref: row.id for ref, row in sources.items()}, made)
        made["chats"] = self.chats(ws, data, jobs)
        return made

    def documents(self, ws: int, data: WorkspaceIn) -> int:
        rows: dict[int, Document] = {}
        for d in data.documents:
            stored = None
            name = self.archive.original(d.original)
            if name is not None:
                content = self.archive.read(name, docs.MAX_UPLOAD_BYTES) if \
                    self.archive.infos[name].file_size <= docs.MAX_UPLOAD_BYTES else None
                if content is not None:
                    stored = docs.save_upload(content)
                    self.stored.append(stored)
                    self.totals["originals"] += 1
            filetype = d.filetype if d.filetype in (docs.NOTE_FILETYPE, docs.SUMMARY_FILETYPE) or \
                (d.filetype or "").lower() in docs.ALLOWED_EXTENSIONS else None
            rows[d.ref] = Document(
                workspace_id=ws, filename=d.filename or "Document", filetype=filetype, stored_name=stored,
                text=d.text, kind=d.kind or "other", use_for_scoring=d.use_for_scoring, created_at=d.created_at or utcnow(),
                updated_at=d.updated_at, concise_text=d.concise_text, use_concise=d.use_concise,
                concise_model=d.concise_model, concise_at=d.concise_at, summary_of=None)
            self.db.add(rows[d.ref])
        self.db.flush()
        for d in data.documents:
            if d.summary_of:
                rows[d.ref].summary_of = [rows[r].id for r in d.summary_of if r in rows and r != d.ref] or None
        self.totals["documents"] += len(rows)
        return len(rows)

    def _postings(self, data: WorkspaceIn) -> dict[str, Posting]:
        """The pool's postings for the jobs' addresses (looked up, never written)."""
        urls = sorted({_web(j.ad.url) for j in data.jobs if _web(j.ad.url)})
        found: dict[str, Posting] = {}
        for start in range(0, len(urls), 400):
            for p in self.db.query(Posting).filter(Posting.url.in_(urls[start:start + 400])):
                if not p.not_a_job:
                    found[p.url] = p
        return found

    def jobs(self, ws: int, data: WorkspaceIn, source_ids: dict[int, int], made: dict) -> dict[int, int]:
        db = self.db
        postings = self._postings(data)
        rows: dict[int, Job] = {}
        urls: set[str] = set()
        now = utcnow()
        for j in data.jobs:
            url = _web(j.ad.url)  # (pages link a job's address: only a web address is kept)
            if j.ref in rows or (url and url in urls):
                continue  # (a job is in a workspace once)
            ad = j.ad
            posting = postings.get(url) if url else None
            job = Job(workspace_id=ws, status=j.status, first_seen=j.first_seen or now, last_seen=j.last_seen or now,
                      hidden=j.hidden, excluded_reason=j.excluded_reason or None, location_verdict=j.location_verdict,
                      lat=j.lat, lng=j.lng, distance_km=j.distance_km, source_id=source_ids.get(j.source))
            if posting is not None:
                job.posting, job.origin = posting, "pool"
                for name in j.own:  # the workspace's own values on top of the pool's ad
                    setattr(job, name, getattr(ad, name))
            else:
                job.url, job.origin = url, "private"
                for name in JOB_CONTENT:
                    setattr(job, name, getattr(ad, name))
                job.closed_at = j.closed_at
            if j.applied_at is not None and hasattr(Job, APPLIED_AT):
                setattr(job, APPLIED_AT, j.applied_at)
            if j.office is not None:
                job.office_text, job.office_source = j.office.text, j.office.source
                job.office_lat, job.office_lng = j.office.lat, j.office.lng
                job.office_checked_at = j.office.checked_at
            db.add(job)
            rows[j.ref] = job
            if url:
                urls.add(url)
        db.flush()
        by_ref = {j.ref: j for j in data.jobs}
        for ref, job in rows.items():
            j = by_ref[ref]
            main = rows.get(j.duplicate_of) if j.duplicate_of not in (None, ref) else None
            if main is not None and by_ref[j.duplicate_of].duplicate_of is None:
                job.duplicate_of = main.id
            elif job.duplicate_of is None and job.company:
                self.company_keys.add(company_key(job.company))
            if j.fit is not None:
                f = j.fit
                db.add(FitResult(job_id=job.id, status=f.status, score=f.score, evidence_json=json.dumps(f.evidence.stored()),
                                 error=f.error, profile_hash=f.profile_hash, model=f.model, created_at=f.created_at or now))
            if j.cover_letter is not None:
                c = j.cover_letter
                db.add(CoverLetter(job_id=job.id, text=c.text, instructions=c.instructions, edited=c.edited,
                                   model=c.model, reasoning_effort=c.reasoning_effort, created_at=c.created_at or now,
                                   updated_at=c.updated_at or c.created_at or now))
                made["cover_letters"] += 1
        made["jobs"] = sum(1 for job in rows.values() if job.duplicate_of is None)
        self.totals["jobs"] += made["jobs"]
        return {ref: job.id for ref, job in rows.items()}

    def chats(self, ws: int, data: WorkspaceIn, jobs: dict[int, int]) -> int:
        now = utcnow()
        for c in data.chats:
            ids = sorted({jobs[r] for r in c.jobs if r in jobs})
            thread = ChatThread(workspace_id=ws, job_ids_json=json.dumps(ids), title=c.title,
                                created_at=c.created_at or now, updated_at=c.updated_at or c.created_at or now)
            self.db.add(thread)
            self.db.flush()
            for m in c.messages:
                pages = [{"title": p.title or "", "url": p.url} for p in m.sources or [] if _web(p.url)]
                self.db.add(ChatMessage(thread_id=thread.id, role=m.role, content=m.content,
                                        sources_json=json.dumps(pages) if m.sources is not None else None,
                                        web_search=m.web_search, model=m.model, created_at=m.created_at or now))
            self.totals["chats"] += 1
        return len(data.chats)

    def research(self) -> int:
        """Company research for the imported jobs' companies: taken where the install has none, or an older one.

        The rows are shared by every account on the install and read by fixed keys (pages, job chats, the office
        search), so only the shapes the app writes come in (``import_file.CompanyIn``), no time is later than now
        (``import_file.When``) and the research version is at most this one's. Offices this install already knows are
        kept, with the file's added by address."""
        from backend.research import RESEARCH_VERSION

        taken = 0
        for c in self.data.companies:
            key = company_key(c.key)
            if not key or key != c.key or key not in self.company_keys:
                continue
            row = self.db.get(CompanyProfile, key)
            if row is not None:
                mine, theirs = row.researched_at, c.researched_at
                if theirs is None or (mine is not None and mine >= theirs):
                    continue
            else:
                row = CompanyProfile(key=key)
                self.db.add(row)
            row.name, row.status = c.name or key, c.status if c.status in ("done", "error", "skipped", "missing") else "done"
            row.official_name, row.is_recruiter = c.official_name, c.is_recruiter
            row.business_model, row.ownership, row.headquarters = c.business_model, c.ownership, c.headquarters
            row.controversies_json = json.dumps([x.stored() for x in c.controversies])
            row.controversy_note, row.news_researched_at = c.controversy_note, c.news_researched_at
            row.sources_json = json.dumps([x.stored() for x in c.sources])
            row.employee_count, row.glassdoor_url = c.employee_count, _web(c.glassdoor_url)
            row.sentiment_json = json.dumps(c.sentiment.stored()) if c.sentiment is not None else None
            row.sentiment_researched_at = c.sentiment_researched_at
            row.research_version = min(c.research_version, RESEARCH_VERSION)
            self._offices(row, c)
            row.unverified_dropped, row.error, row.model = c.unverified_dropped, c.error, c.model
            row.researched_at = c.researched_at
            taken += 1
        return taken

    @staticmethod
    def _offices(row: CompanyProfile, c: CompanyIn) -> None:
        """The file's offices added to those the install knows (by address), as research does (backend/research.py)."""
        try:
            known = json.loads(row.offices_json) if row.offices_json else []
            known = [o for o in known if isinstance(o, dict) and isinstance(o.get("address"), str)] \
                if isinstance(known, list) else []
        except ValueError:
            known = []
        seen = {o["address"].lower() for o in known}
        added = []
        for o in c.offices or []:
            if o.address.lower() not in seen:
                seen.add(o.address.lower())
                added.append(o.stored())
        if known or c.offices is not None:
            row.offices_json = json.dumps(known + added)
        if c.offices_checked_at is not None and (row.offices_checked_at is None
                                                 or row.offices_checked_at < c.offices_checked_at):
            row.offices_checked_at = c.offices_checked_at
        if c.offices_cities is not None:
            try:
                mine = json.loads(row.offices_cities) if row.offices_cities else []
            except ValueError:
                mine = []
            mine = [x for x in mine if isinstance(x, str)] if isinstance(mine, list) else []
            row.offices_cities = json.dumps(sorted(set(mine) | {x.lower() for x in c.offices_cities}))

    def undo_uploads(self) -> None:
        for stored in self.stored:
            docs.delete_upload(stored)
        self.stored = []


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------

_running: set[int] = set()
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_AT_ONCE)


async def _read_body(request: Request) -> bytes:
    """The request body (the zip), in memory, at most ``MAX_ZIP_BYTES``, within ``READ_SECONDS``: an upload that
    stalls must not hold the place for ever."""
    length = request.headers.get("content-length")
    if length is not None and length.strip().isdigit() and int(length) > MAX_ZIP_BYTES:
        raise too_large()
    chunks: list[bytes] = []

    async def read() -> int:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_ZIP_BYTES:
                raise too_large()
            chunks.append(chunk)
        return size
    try:
        size = await asyncio.wait_for(read(), READ_SECONDS)
    except asyncio.TimeoutError:
        raise problem(408, "import_slow", "The file took too long to arrive, so it wasn't imported. Try again.") from None
    if not size:
        raise problem(422, "import_empty", "Choose an export's .zip file first.")
    return b"".join(chunks)


async def export_zip(request: Request, user: AuthUser = Depends(current_user)) -> AsyncIterator[tuple[AuthUser, bytes]]:
    """The uploaded zip, for the signed-in person with a recent password, holding their place (one at a time) until
    the request has ended. The password and the place are checked before the body is read."""
    require_recent_auth(user)
    with _lock:
        if user.id in _running:
            raise problem(429, "import_running", "Your import is still running. Wait for it to finish.")
        _running.add(user.id)
    if not _slots.acquire(blocking=False):
        with _lock:
            _running.discard(user.id)
        raise problem(503, "import_busy", "Another import is running. Try again in a minute.")
    try:
        yield user, await _read_body(request)
    finally:
        _slots.release()
        with _lock:
            _running.discard(user.id)


@router.post("/import/check")
def check_import(upload: tuple[AuthUser, bytes] = Depends(export_zip)):
    """What the export holds: its workspaces with counts, for the person to pick from. Recent password."""
    _user, body = upload
    archive = ExportZip(body)
    data = archive.import_file()
    return {"format_version": data.format_version, "app_version": data.app_version,
            "exported_at": data.exported_at.isoformat() + "Z" if data.exported_at else None,
            "workspaces": [{"index": n, "name": w.name, **counts(w)} for n, w in enumerate(data.workspaces)],
            "companies": len(data.companies)}


def _picked(pick: str, available: int) -> list[int]:
    try:
        picked = [int(p) for p in pick.split(",") if p.strip()]
    except ValueError:
        picked = []
    if not picked or len(set(picked)) != len(picked) or any(not 0 <= p < available for p in picked):
        raise problem(422, "import_pick", "Pick at least one of the workspaces in this export.")
    return picked


@router.post("/import")
def run_import(pick: str = Query(..., max_length=4000), upload: tuple[AuthUser, bytes] = Depends(export_zip),
               db: Session = Depends(get_db)):
    """Imports the picked workspaces (``pick``: their places in the check's list, comma-separated) as new workspaces
    of the signed-in account. Recent password."""
    user, body = upload
    archive = ExportZip(body)
    data = archive.import_file()
    picked = _picked(pick, len(data.workspaces))
    importer = Importer(db, user.id, data, archive)
    try:
        result = importer.run(picked, date.today())
        db.commit()
    except BaseException:
        db.rollback()
        importer.undo_uploads()
        raise
    t = importer.totals
    log.info("user %s imported %d workspace(s): %d jobs, %d documents (%d original files), %d chats, research for %d "
             "companies, %d job sites' layouts", user.id, len(result["workspaces"]), t["jobs"], t["documents"],
             t["originals"], t["chats"], result["companies"], result["site_mappings"])
    return result
