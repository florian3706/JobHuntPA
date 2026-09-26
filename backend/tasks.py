"""Background runs (search + scoring) on a single worker thread.

The UI starts a run, gets its id back immediately, and polls
``GET /api/runs/{id}`` for stage/progress/summary.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Optional

from backend.db import SearchRun, SessionLocal

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jobhunt-run")
_start_lock = threading.Lock()

Progress = Callable[[str, dict], None]


def _update(run_id: int, **fields) -> None:
    db = SessionLocal()
    try:
        run = db.get(SearchRun, run_id)
        for k, v in fields.items():
            setattr(run, k, v)
        db.commit()
    finally:
        db.close()


def active_run() -> Optional[dict]:
    db = SessionLocal()
    try:
        run = (db.query(SearchRun).filter(SearchRun.state.in_(["queued", "running"]))
               .order_by(SearchRun.id.desc()).first())
        return run.to_dict() if run else None
    finally:
        db.close()


def mark_interrupted() -> None:
    """Runs left running by a previous server process can never finish."""
    db = SessionLocal()
    try:
        db.query(SearchRun).filter(SearchRun.state.in_(["queued", "running"])).update(
            {SearchRun.state: "failed", SearchRun.error: "interrupted by server restart",
             SearchRun.finished_at: datetime.utcnow()}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def start(kind: str, job: Callable[[Progress], dict], ws: int) -> dict:
    """Queue ``job(progress)`` for workspace ``ws``; refuses when any run is active
    (one scraper/LLM run at a time keeps crawl delays and rate limits simple)."""
    with _start_lock:
        current = active_run()
        if current is not None:
            return {**current, "already_running": True}
        db = SessionLocal()
        try:
            run = SearchRun(workspace_id=ws, kind=kind, state="queued", progress_json="{}")
            db.add(run)
            db.commit()
            run_id = run.id
            out = run.to_dict()
        finally:
            db.close()
    _executor.submit(_execute, run_id, job)
    return out


def _execute(run_id: int, job: Callable[[Progress], dict]) -> None:
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
        log.exception("run %s failed", run_id)
        _update(run_id, state="failed", error=f"{exc.__class__.__name__}: {exc}", finished_at=datetime.utcnow())
        return
    _update(run_id, state="done", stage="done", summary_json=json.dumps(summary, default=str),
            finished_at=datetime.utcnow())
