"""Background runs (search + scoring) on a single worker thread.

The UI starts a run, gets its id back immediately, and polls
``GET /api/runs/{id}`` for stage/progress/summary.
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session, load_only

from backend import crypto, llm_providers
from backend.db import SearchRun, SessionLocal
from backend.threads import submit

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jobhunt-run")
_start_lock = threading.Lock()

Progress = Callable[[str, dict], None]

# The run this thread (and the threads it starts through backend.threads.submit) is working for, or None outside a
# run. backend.pool stamps it on the links it creates (``jobs.first_run_id``), which is how a digest knows what is new.
current_run_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("current_run_id", default=None)


def _update(run_id: int, refund: bool = False, **fields) -> None:
    """Write these fields of a run without loading it (sealed ones are sealed under the current scope). With
    ``refund`` (the run failed) its free search, if it used one, is given back in the same commit."""
    db = SessionLocal()
    try:
        db.query(SearchRun).filter(SearchRun.id == run_id).update(
            {getattr(SearchRun, k): v for k, v in fields.items()}, synchronize_session=False)
        if refund:
            from backend import membership

            membership.refund_runs(db, [run_id])
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
    is a code, which ``SearchRun.to_dict`` turns into words. A free search such a run used is given back."""
    from backend import membership

    db = SessionLocal()
    try:
        stuck = db.query(SearchRun.id).filter(SearchRun.state.in_(["queued", "running"]))
        ids = [i for (i,) in stuck]
        db.query(SearchRun).filter(SearchRun.state.in_(["queued", "running"])).update(
            {SearchRun.state: "failed", SearchRun.error_code: "interrupted", SearchRun.finished_at: datetime.utcnow()},
            synchronize_session=False)
        membership.refund_runs(db, ids)
        db.commit()
    finally:
        db.close()


def _full(run_id: int) -> dict:
    db = SessionLocal()
    try:
        return db.get(SearchRun, run_id).to_dict()
    finally:
        db.close()


def start(kind: str, job: Callable[[Progress], dict], ws: int, scheduled: bool = False,
          charge: Optional[Callable[[Session, SearchRun], None]] = None) -> dict:
    """Queue ``job(progress)`` for workspace ``ws``; refuses when that workspace already has an active
    run. Runs of other workspaces wait their turn: the single worker takes them first in, first out
    (one scraper/LLM run at a time keeps crawl delays and rate limits simple). Called inside the owner's key
    scope (a request, or the scheduler's), which the worker thread inherits. ``charge(db, run)`` runs in the
    transaction that queues the run (a search uses a free search there: backend.membership.charge_search); if it
    raises, nothing is queued."""
    with _start_lock:
        current = active_run(ws)
        if current is not None:
            return {**_full(current["id"]), "already_running": True}
        db = SessionLocal()
        try:
            run = SearchRun(workspace_id=ws, kind=kind, scheduled=scheduled, state="queued", progress_json="{}")
            db.add(run)
            if charge is not None:
                charge(db, run)
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
            _update(run_id, refund=True, state="failed", error=text, finished_at=datetime.utcnow())
        except crypto.CryptoError:  # no key to seal the error with: record that it failed all the same
            _update(run_id, refund=True, state="failed", error_code="failed", finished_at=datetime.utcnow())
        return
    _update(run_id, state="done", stage="done", summary_json=json.dumps(summary, default=str),
            finished_at=datetime.utcnow())
