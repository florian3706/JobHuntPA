"""Background runs (search + scoring) on a single worker thread, and the tasks tray's registry of running AI work.

The UI starts a run, gets its id back immediately, and polls
``GET /api/runs/{id}`` for stage/progress/summary.

The tasks tray (spec §1.13): every piece of AI work registers itself while it runs, with ``track``::

    with tasks.track("cover_letter", ws, jobs=[job_id]):
        letter = ...  # the AI call

and ``GET /api/tasks`` lists the signed-in account's (``for_user``). The registry is in memory: it holds kinds,
ids and numbers (a workspace, job or document id, done/total, the start time), never text: no job title, company
name, question or document. The page turns the ids into names itself. A task ends when its block does, whether the
work finished or raised. A task started inside another one's block (scoring inside a search run, office look-ups
inside company research) is that one's step (``parent_id``) and isn't counted on its own. Nothing cancels a task
(spec §1.16). A new kind is one line in ``KINDS``.
"""
from __future__ import annotations

import contextvars
import itertools
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Iterator, Optional

from sqlalchemy.orm import load_only

from backend import crypto, llm_providers
from backend.db import SearchRun, SessionLocal, Workspace
from backend.threads import submit

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jobhunt-run")
_start_lock = threading.Lock()

Progress = Callable[[str, dict], None]

# The run this thread (and the threads it starts through backend.threads.submit) is working for, or None outside a
# run.
current_run_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("current_run_id", default=None)


def _update(run_id: int, **fields) -> None:
    """Write these fields of a run without loading it (sealed ones are sealed under the current scope)."""
    db = SessionLocal()
    try:
        db.query(SearchRun).filter(SearchRun.id == run_id).update(
            {getattr(SearchRun, k): v for k, v in fields.items()}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def active_run(ws: Optional[int] = None) -> Optional[dict]:
    """The newest queued or running run (of workspace ``ws`` when given, else of any), plaintext fields only: the
    scheduler asks with no key (accounts design §2.3)."""
    db = SessionLocal()
    try:
        q = db.query(SearchRun).options(load_only(SearchRun.id, SearchRun.workspace_id, SearchRun.kind,
                                                  SearchRun.scheduled, SearchRun.state, SearchRun.started_at,
                                                  SearchRun.finished_at))
        q = q.filter(SearchRun.state.in_(["queued", "running"]))
        if ws is not None:
            q = q.filter(SearchRun.workspace_id == ws)
        run = q.order_by(SearchRun.id.desc()).first()
        return run.plain_dict() if run else None
    finally:
        db.close()


def start_lock() -> threading.Lock:
    """Held while a run is queued. ``users.delete_account`` holds it while it checks for active runs and removes the
    account's workspaces, so no run can be queued for them in between."""
    return _start_lock


def mark_interrupted() -> None:
    """Runs left running by a previous server process can never finish. Written without anyone's key: the error
    is a code, which ``SearchRun.to_dict`` turns into words."""
    db = SessionLocal()
    try:
        db.query(SearchRun).filter(SearchRun.state.in_(["queued", "running"])).update(
            {SearchRun.state: "failed", SearchRun.error_code: "interrupted", SearchRun.finished_at: datetime.utcnow()},
            synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _full(run_id: int) -> dict:
    db = SessionLocal()
    try:
        return db.get(SearchRun, run_id).to_dict()
    finally:
        db.close()


def start(kind: str, job: Callable[[Progress], dict], ws: int, scheduled: bool = False) -> dict:
    """Queue ``job(progress)`` for workspace ``ws``; refuses when that workspace already has an active
    run. Runs of other workspaces wait their turn: the single worker takes them first in, first out
    (one scraper/LLM run at a time keeps crawl delays and rate limits simple). Called inside the owner's key
    scope (a request, or the scheduler's), which the worker thread inherits."""
    with _start_lock:
        current = active_run(ws)
        if current is not None:
            return {**_full(current["id"]), "already_running": True}
        db = SessionLocal()
        try:
            run = SearchRun(workspace_id=ws, kind=kind, scheduled=scheduled, state="queued", progress_json="{}")
            db.add(run)
            db.commit()
            run_id = run.id
            out = run.to_dict()
        finally:
            db.close()
        # Inside the lock, so the queue order is the order of the run ids. In this request's context,
        # so a crypto scope reaches the worker thread. The scope is made again without the request's identity-key
        # opener (it holds the session key): a run can wait in the queue long after the request is over, and a
        # background run must not be able to open the identity key (accounts design §1.2).
        if crypto.has_scope():
            scope = crypto.current()
            with crypto.as_user(scope.user_id, scope.dek):
                submit(_executor, _execute, run_id, job)
        else:
            submit(_executor, _execute, run_id, job)
    return out


def _execute(run_id: int, job: Callable[[Progress], dict]) -> None:
    token = current_run_id.set(run_id)
    try:
        _run(run_id, job)
    finally:
        current_run_id.reset(token)


def _run(run_id: int, job: Callable[[Progress], dict]) -> None:
    _update(run_id, state="running")
    last = [0.0]

    def progress(stage: str, info: dict) -> None:
        report_progress(info)  # the tasks tray's numbers (the task the caller works for), never throttled
        now = time.monotonic()
        if now - last[0] < 0.4:
            return
        last[0] = now
        _update(run_id, stage=stage, progress_json=json.dumps(info))

    try:
        summary = job(progress)
    except Exception as exc:
        log.error("run %s failed: %s", run_id, exc.__class__.__name__)  # the message may hold user data
        # An AI error is shown as the person may see it (not the provider's address or answer).
        text = llm_providers.user_message(exc) if isinstance(exc, llm_providers.ScorerError) else f"{exc.__class__.__name__}: {exc}"
        try:
            _update(run_id, state="failed", error=text, finished_at=datetime.utcnow())
        except crypto.CryptoError:  # no key to seal the error with: record that it failed all the same
            _update(run_id, state="failed", error_code="failed", finished_at=datetime.utcnow())
        return
    _update(run_id, state="done", stage="done", summary_json=json.dumps(summary, default=str),
            finished_at=datetime.utcnow())


# --------------------------------------------------------------------------
# The tasks tray: running AI work (spec §1.13)
# --------------------------------------------------------------------------

# kind -> what the tray calls it. Registering a new kind of AI work is one line here (and a ``track`` around it).
KINDS = {
    "search": "Search run",
    "scoring": "Scoring new jobs",
    "rescore": "Scoring",
    "research": "Company research",
    "offices": "Finding offices",
    "cover_letter": "Cover letter",
    "chat": "Chat answer",
    "summary": "Document summary",
    "concise": "Concise version",
    "titles": "Title suggestions",
    "mapping_job": "Working out a job site",
    "mapping_careers": "Working out a careers page",
}
# A task's ``part`` (which piece of the work, where a kind has several) -> words.
PARTS = {"profile": "profile", "sentiment": "what employees say", "news": "news"}
# What a task is for, when it is for something: the page names it from the id.
SUBJECTS = ("job", "company", "document")


@dataclass
class Task:
    """One piece of running AI work. Ids and numbers only, never text (module docstring)."""
    id: int
    kind: str
    workspace_id: int
    started_at: datetime
    subject: Optional[str] = None       # "job" | "company" (named by one of its jobs) | "document" | None
    job_ids: tuple[int, ...] = ()
    document_id: Optional[int] = None
    part: Optional[str] = None
    parent_id: Optional[int] = None
    done: Optional[int] = None
    total: Optional[int] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def progress(self, done: Optional[int], total: Optional[int]) -> None:
        """How far it is (both numbers, or neither when it isn't known)."""
        ok = isinstance(done, int) and isinstance(total, int) and not isinstance(done, bool) and total > 0
        with self._lock:
            self.done, self.total = (max(0, min(done, total)), total) if ok else (None, None)

    def to_dict(self) -> dict:
        with self._lock:
            progress = {"done": self.done, "total": self.total} if self.total else None
        return {"id": self.id, "kind": self.kind, "what": KINDS[self.kind], "part": self.part,
                "part_label": PARTS.get(self.part or ""), "workspace_id": self.workspace_id, "subject": self.subject,
                "job_ids": list(self.job_ids), "document_id": self.document_id, "parent_id": self.parent_id,
                "started_at": self.started_at.isoformat() + "Z", "progress": progress}


_registry: dict[int, Task] = {}
_registry_lock = threading.Lock()
_task_ids = itertools.count(1)
# The innermost task this thread (and the threads it starts through backend.threads.submit) works for.
current_task: contextvars.ContextVar[Optional[Task]] = contextvars.ContextVar("current_task", default=None)


@contextmanager
def track(kind: str, ws: int, *, jobs: Iterable[int] = (), company_job: Optional[int] = None,
          document: Optional[int] = None, part: Optional[str] = None) -> Iterator[Task]:
    """Registers AI work of ``kind`` for workspace ``ws`` while the block runs; it ends with the block, whether the
    work finished or raised. What it is for: ``jobs`` (job ids), ``company_job`` (a company, named by one of its jobs
    in the workspace), or ``document`` (a document id). ``part``: a key of ``PARTS``. Inside another task's block
    it is that task's step."""
    if kind not in KINDS:
        raise ValueError(f"unknown task kind {kind!r}: add it to tasks.KINDS")
    if part is not None and part not in PARTS:
        raise ValueError(f"unknown task part {part!r}")
    jobs = tuple(int(j) for j in jobs)
    subject = ("company" if company_job is not None else "job" if jobs else
               "document" if document is not None else None)
    if company_job is not None:
        jobs = (int(company_job),)
    parent = current_task.get()
    task = Task(id=next(_task_ids), kind=kind, workspace_id=int(ws), started_at=datetime.utcnow(), subject=subject,
                job_ids=jobs, document_id=int(document) if document is not None else None, part=part,
                parent_id=parent.id if parent is not None else None)
    with _registry_lock:
        _registry[task.id] = task
    token = current_task.set(task)
    try:
        yield task
    finally:
        current_task.reset(token)
        with _registry_lock:
            _registry.pop(task.id, None)


def report_progress(info: dict) -> None:
    """A run's progress (``{"done", "total"}``) on the task this thread works for, if any."""
    task = current_task.get()
    if task is not None and isinstance(info, dict):
        task.progress(info.get("done"), info.get("total"))


def running(workspaces: Iterable[int]) -> list[dict]:
    """The running tasks of these workspaces, oldest first, each step after the task it belongs to."""
    wanted = set(workspaces)
    with _registry_lock:
        mine = [t for t in _registry.values() if t.workspace_id in wanted]
    ids = {t.id for t in mine}
    top = sorted((t for t in mine if t.parent_id not in ids), key=lambda t: t.id)
    out: list[dict] = []

    def add(task: Task, parent_id: Optional[int]) -> None:
        out.append({**task.to_dict(), "parent_id": parent_id})
        for child in sorted((t for t in mine if t.parent_id == task.id), key=lambda t: t.id):
            add(child, task.id)

    for task in top:
        add(task, None)
    return out


def for_user(user_id: int) -> dict:
    """``GET /api/tasks``: the running AI work of this account's workspaces (another account's never shows: a
    workspace belongs to one account). ``count`` is what the header's button shows: tasks, not their steps."""
    db = SessionLocal()
    try:
        workspaces = [ws for (ws,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id)]
    finally:
        db.close()
    items = running(workspaces)
    return {"count": sum(1 for t in items if t["parent_id"] is None), "tasks": items}
