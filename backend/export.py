"""Download your data: a zip of folders of Markdown files, made for people to read.

    POST /api/account/export     recent password; answers application/zip (JobHuntPA-export-YYYY-MM-DD.zip)

A POST with the CSRF token (a link or a prefetch must never start an export), and the password entered in the last
15 minutes. The zip is built in memory inside the request's key scope: no plaintext touches the disk (no temporary
files). Only the signed-in account's own workspaces are read (``Workspace.owner_id``); an admin exports their own
data like anyone else. One export per account at a time (429), and at most ``MAX_AT_ONCE`` in the whole app (503), and
no more than ``MAX_EXPORT_BYTES`` of files in one zip (413): the zip is held in memory, so the account's place and
the slot are kept until the download has ended (or ``MAX_STREAM_SECONDS``), not just until the build is done. The log says that user N exported, with the number
of files and the size, never a name or any content.

Layout (names are made safe for Windows, macOS and Linux: ``safe_name``)::

    JobHuntPA export 2026-10-07/
      README.md  Account.md
      Companies/<company>.md                   research on the companies of the jobs that have a file
      Workspaces/<workspace>/
        Search setup.md  Sources.md  Search history.md
        Documents/<title>.md  Documents/Original files/<upload>
        Jobs/Jobs.md  Jobs/Filtered out.md  Jobs/<Status>/<Company - Title>.md (+ " - Cover letter.md")
        Chats/<date title>.md

Ads and AI answers are other people's text: ``text`` and ``line`` escape raw HTML, images and link syntax so a
Markdown viewer shows them as text, and only addresses starting with http:// or https:// become links. Links between
the files are relative, so they work after unzipping, offline.
"""
from __future__ import annotations

import io
import json
import logging
import os
import posixpath
import re
import threading
import time
import unicodedata
import zipfile
from datetime import date, datetime
from typing import Any, Iterable, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session

from backend import account, crypto, docs, ghost, schedule, users
from backend.db import (
    UPLOAD_DIR,
    ChatMessage,
    ChatThread,
    CompanyProfile,
    CompanySource,
    CoverLetter,
    DealbreakerSet,
    Document,
    EmailPrefs,
    FitResult,
    Job,
    Membership,
    MembershipGrant,
    Pin,
    SearchRun,
    User,
    UserKeys,
    UserProfile,
    Workspace,
    company_key,
    get_db,
)
from backend.pool import jobs_query
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])

MAX_PART = 80
MAX_EXPORT_BYTES = 200 * 1024 * 1024   # of files in one zip, before compression: it is built in memory
MAX_AT_ONCE = 2                        # exports being made at the same time, for everyone
MAX_STREAM_SECONDS = 600               # a download is cut off after this long, so a slow reader can't hold a zip in memory
STATUS_FOLDER = {"to_review": "To review", "shortlisted": "Shortlisted", "applied": "Applied",
                 "interviewing": "Interviewing", "rejected": "Rejected", "not_interested": "Not interested"}
HIDDEN_FOLDER = "Hidden"
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_ENTITY = re.compile(r"&(?=#?\w+;)")
_BRACKET = re.compile(r"(\\*)\[")
_PIPE = re.compile(r"(\\*)\|")
MAX_PART_BYTES = 240                   # of one name, encoded: file systems stop at 255
CHUNK = 200


# ---------------------------------------------------------------------------
# Safe names
# ---------------------------------------------------------------------------

def _clip(value: str, chars: int, size: int) -> str:
    """The start of ``value`` that fits in ``chars`` characters and ``size`` UTF-8 bytes, without cutting a character."""
    return value[:chars].encode("utf-8")[:max(1, size)].decode("utf-8", "ignore")


def safe_name(value: Any, fallback: str = "Untitled", ext: str = "") -> str:
    """A file or folder name that is valid on Windows, macOS and Linux: no ``<>:"/\\|?*`` or control characters, no
    leading or trailing dots or spaces, no reserved Windows name (CON, NUL, COM1...), NFC-normalised, and at most 80
    characters (and 240 bytes, which some file systems count instead) including ``ext``."""
    text = unicodedata.normalize("NFC", str(value if value is not None else ""))
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
    text = _BAD.sub("-", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text.lstrip(" .") or fallback
    if text.split(".")[0].upper() in RESERVED:
        text = f"_{text}"
    room = max(1, MAX_PART - len(ext))
    size = MAX_PART_BYTES - len(ext.encode("utf-8"))
    text = _clip(text, room, size).rstrip(" .") or _clip(fallback, room, size)
    return text + ext


class Folder:
    """The names used in one folder, so two things never share a name (compared without case: macOS and Windows
    don't tell "A.md" and "a.md" apart). A second "Resume.md" becomes "Resume (2).md"."""

    def __init__(self) -> None:
        self.used: set[str] = set()

    def name(self, value: Any, fallback: str = "Untitled", ext: str = "") -> str:
        base = safe_name(value, fallback, ext)
        stem = base[: len(base) - len(ext)] if ext else base
        candidate, n = base, 1
        while candidate.casefold() in self.used:
            n += 1
            tail = f" ({n})"
            stem = _clip(stem, max(1, MAX_PART - len(ext) - len(tail)),
                         MAX_PART_BYTES - len((ext + tail).encode("utf-8"))).rstrip(" .")
            candidate = f"{stem}{tail}{ext}"
        self.used.add(candidate.casefold())
        return candidate


# ---------------------------------------------------------------------------
# Safe Markdown
# ---------------------------------------------------------------------------

def text(value: Any) -> str:
    """Other people's text, as Markdown that shows it as it is: raw HTML, images and link syntax are escaped. Every
    ``[`` is escaped (a backslash before it is doubled so it can't cancel the escape), so no inline, reference or
    definition link can form; only the addresses the exporter builds itself are links."""
    s = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    s = _ENTITY.sub("&amp;", s).replace("<", "&lt;")
    s = _BRACKET.sub(lambda m: m.group(1) * 2 + "\\[", s).replace("](", "]\\(")
    return s


def line(value: Any) -> str:
    """One line of it (headings, list items): the same, with line breaks as spaces."""
    return re.sub(r"\s+", " ", text(value)).strip()


def esc_pipe(value: str) -> str:
    """``|`` escaped for a table cell; a backslash before it is doubled so it can't cancel the escape."""
    return _PIPE.sub(lambda m: m.group(1) * 2 + "\\|", value)


def cell(value: Any) -> str:
    """A table cell: one line, with ``|`` escaped."""
    return esc_pipe(line(value))


def web_address(value: Any) -> Optional[str]:
    """The address when it is an http or https one, else None."""
    s = str(value or "").strip()
    return s if re.match(r"^https?://[^\s<>]+$", s, re.I) else None


def link(label: Any, address: Any) -> str:
    """A Markdown link when the address is http(s), else just the text."""
    url = web_address(address)
    if not url:
        return line(label) if label else ""
    for char, code in (("(", "%28"), (")", "%29"), ("[", "%5B"), ("]", "%5D"), ("|", "%7C"), ("\\", "%5C"), ("`", "%60")):
        url = url.replace(char, code)
    return f"[{line(label or url)}]({url})"


def rel_link(label: Any, from_path: str, to_path: str) -> str:
    """A relative link from one file in the zip to another."""
    target = posixpath.relpath(to_path, posixpath.dirname(from_path) or ".")
    return f"[{line(label)}]({'/'.join(quote(part) for part in target.split('/'))})"


def when(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if isinstance(value, datetime) else ""


def day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")[:10]


def yes(flag: Any) -> str:
    return "yes" if flag else "no"


def facts(rows: Iterable[tuple[str, Any]]) -> str:
    """A list of "- Label: value" lines, leaving out what is empty."""
    lines = [f"- {label}: {value}" for label, value in rows if value not in (None, "", [])]
    return "\n".join(lines) + "\n" if lines else ""


def bullets(items: Iterable[Any]) -> str:
    out = [f"- {line(i)}" for i in items if str(i or "").strip()]
    return "\n".join(out) + "\n" if out else ""


def value_md(value: Any, depth: int = 0) -> str:
    """Any JSON value (research answers whose shape grows over time) as an indented bullet list."""
    pad = "  " * depth
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if v in (None, "", [], {}):
                continue
            head = f"{pad}- **{line(str(k).replace('_', ' '))}:**"
            out.append(f"{head} {line(v)}" if not isinstance(v, (dict, list)) else f"{head}\n{value_md(v, depth + 1)}")
        return "\n".join(out)
    if isinstance(value, list):
        return "\n".join((f"{pad}- {line(v)}" if not isinstance(v, (dict, list)) else f"{pad}-\n{value_md(v, depth + 1)}")
                         for v in value if v not in (None, "", [], {}))
    return f"{pad}- {line(value)}"


# ---------------------------------------------------------------------------
# The zip
# ---------------------------------------------------------------------------

class Archive:
    """A zip in memory. Paths use forward slashes and UTF-8; ``add`` refuses anything that could leave the folder."""

    def __init__(self, root: str, stamp: datetime) -> None:
        self.root, self.stamp = root, stamp
        self.buffer = io.BytesIO()
        self.zip = zipfile.ZipFile(self.buffer, "w", zipfile.ZIP_DEFLATED)
        self.files = 0
        self.size = 0

    def add(self, path: str, data: bytes | str) -> str:
        parts = path.split("/")
        if not path or path.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise ValueError("unsafe path in export")
        full = f"{self.root}/{path}"
        data = data.encode("utf-8") if isinstance(data, str) else data
        self.size += len(data)
        if self.size > MAX_EXPORT_BYTES:
            raise too_large()
        info = zipfile.ZipInfo(full, date_time=self.stamp.timetuple()[:6])
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        self.zip.writestr(info, data)
        self.files += 1
        return path

    def abort(self) -> None:
        """Gives the memory back when the export is refused or fails half way: an unclosed ZipFile is closed by the
        garbage collector after its buffer, which prints an error."""
        try:
            self.zip.close()
        except Exception:  # noqa: BLE001  (nothing more to save)
            pass
        self.buffer.close()

    def finish(self) -> io.BytesIO:
        """The finished zip, read from the start. Not copied: the response streams it."""
        self.zip.close()
        self.buffer.seek(0)
        return self.buffer


def too_large() -> AuthProblem:
    return AuthProblem(413, "export_too_large", f"Your files add up to more than {MAX_EXPORT_BYTES // 1024 // 1024} "
                                                "MB, which is too much to export in one go. Delete uploaded files "
                                                "you no longer need and try again.")


def _readme(stamp: datetime, counts: dict[str, int]) -> str:
    return f"""# Your JobHuntPA data

Made on {when(stamp)}. This is a copy of everything JobHuntPA keeps for your account, as plain text you can read
in any editor or Markdown viewer. Open this folder's files in any order; links between them work offline.

**This folder is not encrypted.** In JobHuntPA your data is encrypted, but these files are not: keep them somewhere
safe and delete them when you no longer need them.

## What is where

- `Account.md`: your email, name, sign-in settings, theme and membership.
- `Workspaces/`: one folder for each of your {counts['workspaces']} workspace(s):
  - `Search setup.md`: job titles, keywords, dealbreakers, salary, work modes, locations, commute and the search schedule.
  - `Sources.md`: the company career pages and feeds you added.
  - `Documents/`: your resume, notes and other documents as text, and `Original files/` with each upload exactly as you uploaded it.
  - `Jobs/`: `Jobs.md` lists every job with a file of its own ({counts['jobs']}); the jobs are sorted into folders by your status, with `Hidden` for the ones you hid. `Filtered out.md` lists the jobs the search filters left out.
  - `Chats/`: your conversations about jobs.
  - `Search history.md`: each search run and how it went.
- `Companies/`: the company research (shared with other people who use this JobHuntPA) for the companies of those jobs.

## What is not included

- Your password, recovery codes and encryption keys, and the secret behind two-step sign-in: they can't and shouldn't be exported.
- Sign-in sessions, caches and logs.
- A job that is another copy of the same ad is not a file of its own: it is listed under "Also advertised at" in the main job's file (or in `Filtered out.md`, when the main job is there).
- A job the search filters left out and you never touched is only in `Filtered out.md`.
- Anyone else's data. Admins export only their own account, like everyone else.
"""


def _account_md(db: Session, user: User, workspaces: list[Workspace]) -> str:
    try:
        who = users.details(db, user.id)
    except (ValueError, crypto.CryptoError):
        who = {}
    prefs = account.read_prefs(db, user.id)
    keys = db.get(UserKeys, user.id)
    member = db.get(Membership, user.id)
    out = ["# Account\n\n"]
    out.append(facts([
        ("Email", line(who.get("email"))), ("Name", line(who.get("name"))),
        ("Account created", when(user.created_at)), ("Last active", day(user.last_active_on)),
        ("Role", "admin" if user.role == "admin" else "user"),
        ("Email address confirmed", yes(user.email_verified)),
        ("Two-step sign-in", "on" if user.mfa_enabled else "off"),
        ("Scheduled searches while signed out (background access)", "on" if keys is not None and keys.server_wrapped_dek is not None else "off"),
        ("Background access allowed since", when(keys.background_since) if keys is not None and keys.server_wrapped_dek is not None else ""),
        ("Theme", line(prefs.get("theme")) or "not chosen"),
    ]))
    mail = db.get(EmailPrefs, user.id)
    if mail is not None:
        every = "every day" if mail.digest_every_days == 1 else f"every {mail.digest_every_days} days"
        out.append(facts([("Job digest emails", f"on, {every}" if mail.digest_enabled else "off"),
                          ("Agreed to digest emails", when(mail.opted_in_at)), ("Last digest sent", when(mail.last_sent_at))]))
    out.append("\n## Membership\n\n")
    if member is None:
        out.append("No membership details.\n")
    else:
        out.append(facts([
            ("Exempt from payment", yes(member.exempt)), ("Free searches used", member.free_searches_used),
            ("Subscription", line(member.stripe_status)), ("Plan", line(member.plan)),
            ("Paid until", when(member.current_period_end)),
            ("Ends at the end of the period", yes(member.cancel_at_period_end) if member.stripe_status else ""),
        ]))
        grants = db.query(MembershipGrant).filter(MembershipGrant.user_id == user.id).order_by(MembershipGrant.id).all()
        if grants:
            out.append("\n### Free periods\n\n")
            out.append("| Kind | From | Until | Ended early |\n|---|---|---|---|\n")
            out.append("".join(f"| {cell(g.kind)} | {day(g.starts_at)} | {day(g.ends_at)} | {day(g.revoked_at)} |\n" for g in grants))
    out.append("\n## Workspaces\n\n")
    out.append(bullets(w.name for w in workspaces) or "None.\n")
    return "".join(out)


def _setup_md(db: Session, ws: Workspace) -> str:
    profile = db.get(UserProfile, ws.id)
    dealbreakers = db.get(DealbreakerSet, ws.id)
    pins = db.query(Pin).filter(Pin.workspace_id == ws.id).order_by(Pin.id).all()
    out = [f"# Search setup: {line(ws.name)}\n\n"]
    if profile is not None:
        modes = [m for m, on in (("remote (Australia)", profile.remote_aus_ok), ("remote (anywhere)", profile.remote_global_ok),
                                 ("hybrid", profile.allow_hybrid), ("on-site", profile.allow_onsite)) if on]
        out.append("## What you look for\n\n")
        out.append(facts([
            ("Job titles", ", ".join(line(t) for t in profile.titles or [])),
            ("Keywords to include", ", ".join(line(t) for t in profile.keywords_include or [])),
            ("Keywords to avoid", ", ".join(line(t) for t in profile.keywords_exclude or [])),
            ("Salary floor", f"{profile.salary_floor:,}" if profile.salary_floor else ""),
            ("Work modes accepted", ", ".join(modes) or "none"),
            ("Most office days a week (hybrid)", profile.max_office_days),
            ("Search SEEK", yes(profile.seek_enabled)),
            ("SEEK locations", ", ".join(line(t) for t in profile.seek_locations or [])),
        ]))
    if dealbreakers is not None:
        out.append("\n## Dealbreakers\n\n")
        out.append(facts([("Industries", ", ".join(line(t) for t in dealbreakers.industries or [])),
                          ("Words", ", ".join(line(t) for t in dealbreakers.keywords or []))]) or "None.\n")
    out.append("\n## Places\n\n")
    if pins:
        out.append("| Place | Kind | Latitude | Longitude | Radius (km) |\n|---|---|---|---|---|\n")
        out.append("".join(f"| {cell(p.label)} | {cell(p.kind)} | {p.lat} | {p.lng} | {p.radius_km} |\n" for p in pins))
    else:
        out.append("None.\n")
    if profile is not None:
        out.append("\n## Commute\n\n")
        out.append(facts([("Public transport from", ", ".join(line(t) for t in profile.commute_from or []) or "your home pin"),
                          ("Arrive at work by", line(profile.commute_arrive_by)), ("Leave work at", line(profile.commute_leave_at))]))
    sched = schedule.get_schedule(db, ws.id)
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out.append("\n## Search schedule\n\n")
    out.append(facts([("Scheduled searches", "on" if sched.enabled else "off"), ("Times", ", ".join(sched.times)),
                      ("Days", ", ".join(names[d] for d in sched.days if 0 <= d < 7)),
                      ("Skip public holidays", yes(sched.skip_holidays)), ("State", sched.state)]))
    return "".join(out)


def _sources_md(db: Session, ws: Workspace) -> str:
    rows = db.query(CompanySource).filter(CompanySource.workspace_id == ws.id).order_by(CompanySource.id).all()
    out = [f"# Sources: {line(ws.name)}\n\n", "Company career pages and feeds searched for this workspace.\n"]
    if not rows:
        return "".join(out + ["\nNone.\n"])
    out.append("\n| Name | Address | Type | On | Last run | Last result |\n|---|---|---|---|---|---|\n")
    for s in rows:
        result = f"error: {s.last_error}" if s.last_error else (f"{s.last_count} jobs" if s.last_run else "")
        out.append(f"| {cell(s.label or s.company)} | {cell(s.careers_url)} | {cell(s.source_type)} | {yes(s.enabled)} "
                   f"| {when(s.last_run)} | {cell(result)} |\n")
    return "".join(out)


def _history_md(db: Session, ws: Workspace) -> str:
    runs = db.query(SearchRun).filter(SearchRun.workspace_id == ws.id).order_by(SearchRun.id.desc()).all()
    out = [f"# Search history: {line(ws.name)}\n\n"]
    if not runs:
        return "".join(out + ["\nNo runs yet.\n"])
    out.append("\nNewest first.\n")
    for r in runs:
        d = r.to_dict()
        title = f"{when(r.started_at)}: {'search' if r.kind == 'search' else 'scoring'} ({'scheduled' if r.scheduled else 'started by you'})"
        out.append(f"\n## {title}\n\n")
        out.append(facts([("How it ended", d["state"]), ("Finished", when(r.finished_at)), ("Error", line(d["error"]))]))
        summary = d.get("summary") or {}
        search = summary.get("search") if isinstance(summary, dict) else None
        if isinstance(search, dict):
            if search.get("totals"):
                out.append("\n" + value_md(search["totals"]) + "\n")
            sources = [s for s in search.get("per_source") or [] if isinstance(s, dict)]
            if sources:
                out.append("\n| Source | Listed | New | Kept | Problem |\n|---|---|---|---|---|\n")
                out.append("".join(f"| {cell(s.get('label'))} | {s.get('listed', '')} | {s.get('new', '')} | {s.get('kept', '')} | {cell(s.get('error'))} |\n" for s in sources))
        elif summary:
            out.append("\n" + value_md(summary) + "\n")
    return "".join(out)


# ----- documents -----

def _documents(db: Session, ws: Workspace, base: str, arc: Archive) -> None:
    documents = db.query(Document).filter(Document.workspace_id == ws.id).order_by(Document.id).all()
    folder, originals = Folder(), Folder()
    names = {d.id: d for d in documents}
    for d in documents:
        stem = d.filename if d.filetype in (docs.NOTE_FILETYPE, docs.SUMMARY_FILETYPE) else re.sub(r"\.[A-Za-z0-9]{1,5}$", "", d.filename or "")
        md_path = f"{base}/Documents/{folder.name(stem, 'Document', '.md')}"
        original = ""
        if d.stored_name:
            try:
                data = docs.read_upload(d.stored_name)
            except (OSError, crypto.CryptoError):
                data = None
                original = "The original file couldn't be opened, so only its text is included."
            if data is not None:
                ext = posixpath.splitext(d.filename or "")[1]
                ext = ext if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext) else ""
                orig_path = arc.add(f"{base}/Documents/Original files/{originals.name(posixpath.splitext(d.filename or '')[0], 'File', ext)}", data)
                original = f"The original file: {rel_link(d.filename, md_path, orig_path)}"
        concise = (d.concise_text or "").strip()
        out = [f"# {line(d.filename) or 'Document'}\n\n", facts([
            ("Kind", line(d.kind)), ("Type", line(d.filetype)), ("Added", when(d.created_at)), ("Last edited", when(d.updated_at)),
            ("Used for scoring", yes(d.use_for_scoring)), ("Scoring uses the concise version", yes(d.use_concise and concise)),
            ("Made from", ", ".join(line(names[i].filename) for i in (d.summary_of or []) if isinstance(i, int) and i in names)),
        ])]
        if original:
            out.append(f"\n{original}\n")
        out.append(f"\n## Text\n\n{text(d.text) if (d.text or '').strip() else '(no text)'}\n")
        if concise:
            out.append(f"\n## Concise version\n\n{text(concise)}\n")
        arc.add(md_path, "".join(out))


# ----- jobs -----

class JobEntry:
    """What the rest of the export needs to know about a job: where its file is (None: it has none)."""

    def __init__(self, job_id: int, title: str, company: str) -> None:
        self.id, self.title, self.company = job_id, title, company
        self.path: Optional[str] = None
        self.letter_path: Optional[str] = None
        self.status = ""
        self.fit = ""
        self.closed = False
        self.copies: list[int] = []
        self.main: Optional[int] = None  # a merged copy: the job it was merged into
        self.url = ""
        self.reason = ""
        self.ghost: Optional[dict] = None  # backend.ghost: signs that the ad may not be a current vacancy (flagged only)


def _job_label(e: JobEntry) -> str:
    return " - ".join(p for p in (" ".join((e.company or "").split()), " ".join((e.title or "").split())) if p) or "Job"


def _plan_jobs(db: Session, ws: Workspace, base: str) -> tuple[dict[int, JobEntry], list[JobEntry]]:
    """Decides, for every job in the workspace, whether it has a file and where. Merged copies have none (they go
    in the main job's file); jobs the search filters left out and the person never touched are only listed."""
    entries: dict[int, JobEntry] = {}
    filtered: list[JobEntry] = []
    folders: dict[str, Folder] = {}
    jobs = jobs_query(db, card=True).filter(Job.workspace_id == ws.id).order_by(Job.id).all()
    letters = {j for (j,) in db.query(CoverLetter.job_id).join(Job, Job.id == CoverLetter.job_id).filter(Job.workspace_id == ws.id)}
    fits = {f.job_id: f for f in db.query(FitResult).join(Job, Job.id == FitResult.job_id).filter(Job.workspace_id == ws.id)}
    for job in jobs:
        entry = JobEntry(job.id, job.title or "", job.company or "")
        entries[job.id] = entry
        entry.status = job.status or "to_review"
        entry.closed = job.closed_at is not None
        entry.url = job.url or ""
        entry.main = job.duplicate_of
        fit = fits.get(job.id)
        entry.fit = (str(fit.score) if fit.status == "ok" and fit.score is not None else "scoring failed") if fit else ""
        if job.duplicate_of is not None:
            continue
        if job.excluded_reason and not job.hidden and entry.status == "to_review":
            entry.reason = job.excluded_reason
            filtered.append(entry)
            continue
        folder_name = HIDDEN_FOLDER if job.hidden else STATUS_FOLDER.get(entry.status, "To review")
        names = folders.setdefault(folder_name, Folder())
        stem = names.name(_job_label(entry), "Job", ".md")[:-3]
        entry.path = f"{base}/Jobs/{folder_name}/{stem}.md"
        if job.id in letters:
            entry.letter_path = f"{base}/Jobs/{folder_name}/{names.name(f'{stem} - Cover letter', 'Cover letter', '.md')}"
    for job in jobs:
        if job.duplicate_of is not None and job.duplicate_of in entries:
            entries[job.duplicate_of].copies.append(job.id)
    filed = [j for j in jobs if entries[j.id].path]
    keys = sorted({company_key(j.company or "") for j in filed} - {""})
    profiles = {p.key: p for start in range(0, len(keys), 400)
                for p in db.query(CompanyProfile).filter(CompanyProfile.key.in_(keys[start:start + 400]))}
    for job_id, signs in ghost.assess_jobs(db, filed, profiles).items():
        entries[job_id].ghost = signs if signs["level"] else None
    return entries, filtered


def _job_md(job: Job, entry: JobEntry, fit: Optional[FitResult], entries: dict[int, JobEntry],
            companies: dict[str, str]) -> str:
    path = entry.path or ""
    out = [f"# {line(job.title) or 'Job'}\n\n", f"**{line(job.company)}**\n\n" if job.company else ""]
    where = ", ".join(p for p in (line(job.location_text), line(job.country)) if p)
    if job.office_days is not None and (job.work_mode or "") == "hybrid":
        mode = f"hybrid, {job.office_days} office day(s) a week"
    else:
        mode = line(job.work_mode) if job.work_mode and job.work_mode != "unknown" else ""
    salary = line(job.salary_text) or (f"{job.salary_min or ''}-{job.salary_max or ''}".strip("-") if job.salary_min or job.salary_max else "")
    office = line(job.office_text) + (f" (from the {line(job.office_source)})" if job.office_text and job.office_source else "")
    out.append(facts([
        ("Your status", STATUS_FOLDER.get(entry.status, line(entry.status)) + (" (hidden)" if job.hidden else "")),
        ("Fit score", f"{fit.score} out of 100" if fit and fit.status == "ok" and fit.score is not None else ("scoring failed" if fit else "")),
        ("Where", where), ("Work mode", mode),
        ("Distance from home", f"{job.distance_km:.1f} km" if job.distance_km is not None else ""),
        ("Salary", salary), ("Employment type", line(job.employment_type)),
        ("Posted", line(job.posted_at)), ("First seen", day(job.first_seen)),
        ("Closed", day(job.closed_at) if job.closed_at else ""), ("Office", office),
        ("Your call on the location", line(job.location_verdict)),
        ("Left out by the search filters", line(job.excluded_reason)),
        ("Original ad", link(job.url, job.url) if web_address(job.url) else ""),
        ("Source", line(job.source)), ("Industry", line(job.industry)),
        ("Company research", rel_link(job.company, path, companies[company_key(job.company or "")]) if company_key(job.company or "") in companies else ""),
        ("Cover letter", rel_link("Cover letter", path, entry.letter_path) if entry.letter_path else ""),
    ]))
    if fit is not None and fit.status == "ok":
        try:
            data = json.loads(fit.evidence_json or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        out.append("\n## How well you fit\n")
        if data.get("summary"):
            out.append(f"\n{text(data['summary'])}\n")
        reqs = [r for r in data.get("requirements") or [] if isinstance(r, dict)]
        if reqs:
            out.append("\n### Requirements\n\n")
            for r in reqs:
                out.append(f"- {'Matched' if r.get('matched') else 'Not matched'}{', must have' if r.get('must_have') else ''}: {line(r.get('point'))}\n")
                for ev in r.get("evidence") or []:
                    out.append(f"  - Evidence: {line(ev.get('bullet'))}\n")
                    out.extend(f"    - {line(s)}\n" for s in ev.get("sub_bullets") or [])
        if data.get("gaps"):
            out.append("\n### Gaps\n\n" + bullets(data["gaps"]))
    elif fit is not None and fit.error:
        out.append(f"\n## How well you fit\n\nScoring failed: {line(fit.error)}\n")
    if entry.ghost:
        out.append("\n## Is this a real vacancy?\n\n")
        out.append(f"{'Several signs' if entry.ghost['level'] == 'likely' else 'Some signs'} suggest this ad may not be "
                   "a current vacancy:\n\n")
        for sign in entry.ghost["signals"]:
            out.append(f"- {line(sign['text'])}{': ' + line(sign['detail']) if sign.get('detail') else ''}\n")
        out.append("\nThese are only signs, and the job may well be real. You could ask the employer whether the role "
                   "is open now and when they need someone to start.\n")
    out.append(f"\n## Description\n\n{text(job.description) if (job.description or '').strip() else '(no description saved)'}\n")
    others = [entries[c] for c in entry.copies if c in entries]
    if others:
        out.append("\n## Also advertised at\n\n")
        for c in others:
            out.append(f"- {link(c.title or c.company or 'The same job', c.url) or line(c.title or c.company or 'The same job')}{' (closed)' if c.closed else ''}\n")
    return "".join(out)


def _jobs(db: Session, ws: Workspace, base: str, arc: Archive, companies: dict[str, str],
          entries: dict[int, JobEntry], filtered: list[JobEntry]) -> None:
    letters = {c.job_id: c for c in db.query(CoverLetter).join(Job, Job.id == CoverLetter.job_id).filter(Job.workspace_id == ws.id)}
    fits = {f.job_id: f for f in db.query(FitResult).join(Job, Job.id == FitResult.job_id).filter(Job.workspace_id == ws.id)}
    with_files = [e for e in entries.values() if e.path]
    for start in range(0, len(with_files), CHUNK):
        chunk = with_files[start:start + CHUNK]
        ids = [e.id for e in chunk]
        for job in jobs_query(db, descriptions=True).filter(Job.workspace_id == ws.id, Job.id.in_(ids)).order_by(Job.id):
            entry = entries[job.id]
            letter = letters.get(job.id)
            arc.add(entry.path, _job_md(job, entry, fits.get(job.id), entries, companies))
            if letter is not None and entry.letter_path:
                head = f"# Cover letter: {line(_job_label(entry))}\n\n"
                head += facts([("Job", rel_link(_job_label(entry), entry.letter_path, entry.path)), ("Written", when(letter.created_at)),
                               ("Last changed", when(letter.updated_at)), ("Edited by you", yes(letter.edited)),
                               ("Instructions you gave", line(letter.instructions))])
                arc.add(entry.letter_path, f"{head}\n---\n\n{text(letter.text)}\n")
    # the index
    index = f"{base}/Jobs/Jobs.md"
    out = ["# Jobs\n", f"\n{len(with_files)} job(s) with a file of their own.\n"]
    if filtered:
        out.append(f"\nAnother {len(filtered)} job(s) the search filters left out are in {rel_link('Filtered out', index, f'{base}/Jobs/Filtered out.md')}.\n")
    if with_files:
        out.append("\n| Job | Company | Status | Fit | Closed | Ghost job? |\n|---|---|---|---|---|---|\n")
        folders = [*STATUS_FOLDER.values(), HIDDEN_FOLDER]   # the order of the folders, Hidden last

        def folder(e: JobEntry) -> str:
            return HIDDEN_FOLDER if f"/Jobs/{HIDDEN_FOLDER}/" in e.path else STATUS_FOLDER.get(e.status, "To review")
        for e in sorted(with_files, key=lambda e: (folders.index(folder(e)), _job_label(e).casefold())):
            status = folder(e)
            name = rel_link(e.title or "Job", index, e.path)
            out.append(f"| {esc_pipe(name)} | {cell(e.company)} | {cell(status)} | {cell(e.fit)} | {'yes' if e.closed else ''} | {e.ghost['level'].capitalize() if e.ghost else ''} |\n")
    arc.add(index, "".join(out))
    out = ["# Filtered out\n", "\nJobs the search filters left out. They have no file of their own.\n"]
    if filtered:
        out.append("\n| Job | Company | Why | Link | Also advertised at |\n|---|---|---|---|---|\n")
        for e in filtered:
            same = " ".join(link(c.title or c.company or "The same job", c.url) for c in (entries[i] for i in e.copies if i in entries))
            out.append(f"| {cell(e.title)} | {cell(e.company)} | {cell(e.reason)} | {link('Original ad', e.url)} | {esc_pipe(same)} |\n")
    else:
        out.append("\nNone.\n")
    arc.add(f"{base}/Jobs/Filtered out.md", "".join(out))


# ----- chats -----

def _chats(db: Session, ws: Workspace, base: str, arc: Archive, entries: dict[int, JobEntry]) -> None:
    folder = Folder()
    for thread in db.query(ChatThread).filter(ChatThread.workspace_id == ws.id).order_by(ChatThread.id):
        title = thread.title or "Chat"
        path = f"{base}/Chats/{folder.name(f'{day(thread.created_at)} {title}', 'Chat', '.md')}"
        try:
            ids = [i for i in json.loads(thread.job_ids_json or "[]") if isinstance(i, int)]
        except ValueError:
            ids = []
        about, seen = [], set()
        for i in ids:
            e = entries.get(i)
            if e is not None and e.path is None and e.main in entries:
                e = entries[e.main]  # a merged copy: the job it was merged into
            if e is not None and id(e) in seen:
                continue  # a job and its merged copy are one job here
            seen.add(id(e))
            if e is None:
                about.append("a job that is no longer in this workspace")
            elif e.path:
                about.append(rel_link(_job_label(e), path, e.path))
            else:
                about.append(line(_job_label(e)))
        out = [f"# {line(title)}\n\n", facts([("Started", when(thread.created_at)), ("Last message", when(thread.updated_at)),
                                           ("About", "; ".join(about))])]
        for m in db.query(ChatMessage).filter(ChatMessage.thread_id == thread.id).order_by(ChatMessage.id):
            who = "You" if m.role == "user" else "JobHuntPA"
            out.append(f"\n## {who}, {when(m.created_at)}\n\n{text(m.content)}\n")
            try:
                sources = json.loads(m.sources_json) if m.sources_json else []
            except ValueError:
                sources = []
            pages = [link(s.get("title") or s.get("url"), s.get("url")) for s in sources if isinstance(s, dict) and web_address(s.get("url"))]
            if pages:
                out.append("\nPages used:\n\n" + "".join(f"- {p}\n" for p in pages))
        arc.add(path, "".join(out))


# ----- company research -----

def _company_md(p: CompanyProfile) -> str:
    d = p.to_dict()
    out = [f"# {line(d['official_name'] or d['name'])}\n\n", facts([
        ("Name on job ads", line(d["name"])), ("Recruiter or agency", yes(d["is_recruiter"]) if d["is_recruiter"] else ""),
        ("Headquarters", line(d["headquarters"])), ("Employees", line(d["employee_count"])),
        ("Glassdoor page", link("Glassdoor", d["glassdoor_url"])), ("Researched", day(d["researched_at"])),
    ])]
    for title, value in (("What the company does", d["business_model"]), ("Ownership", d["ownership"])):
        if value:
            out.append(f"\n## {title}\n\n{text(value)}\n")
    for title, value in (("Offices", d["offices"]), ("Employee sentiment", d["sentiment"]),
                         ("In the news", d["controversies"]), ("Sources", d["sources"])):
        if value:
            out.append(f"\n## {title}\n\n{value_md(value)}\n")
    if d["controversy_note"]:
        out.append(f"\n{text(d['controversy_note'])}\n")
    if d["error"] and not (d["business_model"] or d["ownership"]):
        out.append(f"\nResearch didn't finish: {line(d['error'])}\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def build(db: Session, user_id: int, now: Optional[datetime] = None) -> tuple[io.BytesIO, int, str]:
    """The zip (an in-memory file), how many files it holds and its file name, for ``user_id``'s own data. Must run
    in that person's key scope; reads only their own workspaces. Refuses (413) before opening any upload when
    the uploads alone are over ``MAX_EXPORT_BYTES``."""
    stamp = now or datetime.utcnow()
    user = db.get(User, user_id)
    root = f"JobHuntPA export {stamp.strftime('%Y-%m-%d')}"
    workspaces = db.query(Workspace).filter(Workspace.owner_id == user_id).order_by(Workspace.id).all()
    stored = 0
    for (name,) in db.query(Document.stored_name).filter(Document.workspace_id.in_([w.id for w in workspaces])):
        try:
            stored += os.path.getsize(UPLOAD_DIR / name) if name and os.path.basename(name) == name else 0
        except OSError:
            pass
    if stored > MAX_EXPORT_BYTES:
        raise too_large()
    arc = Archive(root, stamp)  # (after the size check: a refused export builds nothing)
    try:
        ws_names = Folder()
        plans = []
        for ws in workspaces:
            base = f"Workspaces/{ws_names.name(ws.name, 'Workspace')}"
            entries, filtered = _plan_jobs(db, ws, base)
            plans.append((ws, base, entries, filtered))
        # Company research for the companies of the jobs that have a file
        wanted: dict[str, str] = {}
        for _ws, _base, entries, _f in plans:
            for e in entries.values():
                if e.path and e.company:
                    wanted.setdefault(company_key(e.company), e.company)
        companies: dict[str, str] = {}
        profiles = {p.key: p for p in db.query(CompanyProfile).filter(CompanyProfile.key.in_(list(wanted))).all()} if wanted else {}
        folder = Folder()
        for key, p in sorted(profiles.items()):
            companies[key] = f"Companies/{folder.name(p.official_name or p.name, 'Company', '.md')}"
        jobs_total = 0
        for ws, base, entries, filtered in plans:
            arc.add(f"{base}/Search setup.md", _setup_md(db, ws))
            arc.add(f"{base}/Sources.md", _sources_md(db, ws))
            _documents(db, ws, base, arc)
            _jobs(db, ws, base, arc, companies, entries, filtered)
            jobs_total += sum(1 for e in entries.values() if e.path)
            _chats(db, ws, base, arc, entries)
            arc.add(f"{base}/Search history.md", _history_md(db, ws))
        for key, path in companies.items():
            arc.add(path, _company_md(profiles[key]))
        arc.add("Account.md", _account_md(db, user, workspaces))
        arc.add("README.md", _readme(stamp, {"workspaces": len(workspaces), "jobs": jobs_total}))
        count = arc.files
    except BaseException:
        arc.abort()
        raise
    return arc.finish(), count, f"JobHuntPA-export-{stamp.strftime('%Y-%m-%d')}.zip"


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

_running: set[int] = set()
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_AT_ONCE)


class _Hold:
    """An account's place (its one-at-a-time mark and one of the app's slots) for as long as its zip is in memory: from
    the start of the build until the last byte is sent, the client goes away or ``MAX_STREAM_SECONDS`` pass. Giving
    the place back at the end of the build would let one account start another export while the earlier buffers were
    still held by slow readers. ``release`` can run more than once."""

    def __init__(self, user_id: int) -> None:
        self.user_id, self.buffer, self._done = user_id, None, False
        self._guard = threading.Lock()

    def release(self) -> None:
        with self._guard:
            if self._done:
                return
            self._done = True
        if self.buffer is not None:
            self.buffer.close()
        _slots.release()
        with _lock:
            _running.discard(self.user_id)


def _chunks(buffer: io.BytesIO, hold: Optional[_Hold] = None):
    deadline = time.monotonic() + MAX_STREAM_SECONDS
    try:
        with buffer:
            while chunk := buffer.read(64 * 1024):
                if time.monotonic() > deadline:  # a reader too slow to matter must not keep the memory for ever
                    log.warning("an export download was cut off after %d seconds", MAX_STREAM_SECONDS)
                    return
                yield chunk
    finally:
        if hold is not None:
            hold.release()


@router.post("/export")
def export_data(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """The account's data as a zip of Markdown files. Recent password required; one at a time per account, and the
    place is kept until the download has finished (the zip stays in memory until then)."""
    require_recent_auth(user)
    with _lock:
        if user.id in _running:
            raise AuthProblem(429, "export_running", "Your export is still being made. Wait for it to finish.")
        _running.add(user.id)
    if not _slots.acquire(blocking=False):
        with _lock:
            _running.discard(user.id)
        raise AuthProblem(503, "export_busy", "Other exports are being made. Try again in a minute.")
    hold = _Hold(user.id)
    try:
        buffer, files, name = build(db, user.id)
        hold.buffer = buffer
        size = buffer.seek(0, os.SEEK_END)
        buffer.seek(0)
    except BaseException:
        hold.release()
        raise
    log.info("user %s exported their data: %d files, %d bytes", user.id, files, size)
    # The generator gives the place back when it ends; the background task covers a response that is never iterated.
    return StreamingResponse(_chunks(buffer, hold), media_type="application/zip", background=BackgroundTask(hold.release),
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "Content-Length": str(size)})
