"""Scheduled searches: run the search by itself at set times (per workspace).

Preset: 10:00, 12:00, 14:00 and 16:00, Monday to Friday, skipping public
holidays (including substitute days, e.g. "Boxing Day (observed)") of the
chosen Australian state. The state also sets the time zone of the times, so
the schedule is right even when the server's clock runs on UTC (the Pi).

A slot that passes while another run is busy waits for it, for up to an
hour; so does a slot missed because the app was off, if the app starts
within the hour. A slot is passed over when a search of the workspace was
started since its time (Run search clicked), when there's nothing to
search, or when the workspace's owner hasn't allowed background access
(the server then has no key to their data). Workspaces due at the same
time run one after another.

A slot passed over because the owner hasn't allowed background access (or the account is turned off) is recorded
with a reason the person sees under their schedule (``skipped``, from SKIP_TEXT); it goes away when they turn
background access on, or when a later slot runs.

Endpoints:
    GET/PUT /api/schedule      the settings, plus next_run, last_run, ``background`` (whether the owner allowed
                               background access) and ``skipped`` ({reason, text, slot} or null)
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Annotated, Any, Callable, Literal, Optional
from zoneinfo import ZoneInfo

import holidays
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import crypto, tasks
from backend.db import CompanySource, SearchRun, SearchSchedule, SessionLocal, Workspace, get_db, scrub_wal_again
from backend.workspaces import current_workspace

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schedule")

STATES = {  # code: (name, time zone)
    "ACT": ("Australian Capital Territory", "Australia/Sydney"),
    "NSW": ("New South Wales", "Australia/Sydney"),
    "NT": ("Northern Territory", "Australia/Darwin"),
    "QLD": ("Queensland", "Australia/Brisbane"),
    "SA": ("South Australia", "Australia/Adelaide"),
    "TAS": ("Tasmania", "Australia/Hobart"),
    "VIC": ("Victoria", "Australia/Melbourne"),
    "WA": ("Western Australia", "Australia/Perth"),
}
State = Literal["ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"]
DEFAULT_TIMES = ["10:00", "12:00", "14:00", "16:00"]
DEFAULT_DAYS = [0, 1, 2, 3, 4]  # Monday to Friday
GRACE = timedelta(hours=1)      # how long a due slot may wait for a busy worker
TICK_SECONDS = 30
# What a person is told when a slot is passed over for lack of a key (SearchSchedule.skipped_reason).
SKIP_TEXT = {
    "background_off": "Scheduled search skipped: background access is off",
    "account_off": "Scheduled search skipped: this account is turned off",
    "key_unusable": "Scheduled search skipped: the server can't open your background key. Turn background access "
                    "off and on again",
}


class ScheduleSchema(BaseModel):
    enabled: bool = True
    times: list[Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]] = Field(
        default_factory=lambda: list(DEFAULT_TIMES), max_length=48)
    days: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=lambda: list(DEFAULT_DAYS))
    skip_holidays: bool = True
    state: State = "NSW"


def get_schedule(db: Session, ws: int) -> ScheduleSchema:
    row = db.get(SearchSchedule, ws)
    if row is None:
        return ScheduleSchema()
    return ScheduleSchema(enabled=bool(row.enabled),
                          times=list(DEFAULT_TIMES if row.times is None else row.times),
                          days=list(DEFAULT_DAYS if row.days is None else row.days),
                          skip_holidays=bool(row.skip_holidays), state=row.state or "NSW")


def start_off(db: Session, ws: int) -> None:
    """A new workspace's schedule: the preset times and days, switched off (open-source spec §1.12). A workspace
    with no row (one from before this) keeps the preset schedule, which is on, so existing workspaces keep theirs."""
    db.flush()  # (a schedule just copied from another workspace is found)
    if db.get(SearchSchedule, ws) is None:
        db.add(SearchSchedule(id=ws, enabled=False, times=list(DEFAULT_TIMES), days=list(DEFAULT_DAYS)))


def _row(db: Session, ws: int) -> SearchSchedule:
    row = db.get(SearchSchedule, ws)
    if row is None:
        row = SearchSchedule(id=ws, times=list(DEFAULT_TIMES), days=list(DEFAULT_DAYS))
        db.add(row)
    return row


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def public_holidays(state: str, year: int) -> dict[date, str]:
    """The state's public holidays, national ones and substitute days included."""
    return dict(holidays.Australia(subdiv=state, years=year))


def holiday_name(sched: ScheduleSchema, day: date) -> Optional[str]:
    return public_holidays(sched.state, day.year).get(day) if sched.skip_holidays else None


def _tz(sched: ScheduleSchema) -> ZoneInfo:
    return ZoneInfo(STATES[sched.state][1])


def slots_on(sched: ScheduleSchema, day: date) -> list[datetime]:
    """The day's run times as aware datetimes, or [] when it's off."""
    if not sched.enabled or day.weekday() not in sched.days or holiday_name(sched, day):
        return []
    tz = _tz(sched)
    return [datetime.combine(day, time.fromisoformat(t), tzinfo=tz) for t in sorted(set(sched.times))]


def latest_slot(sched: ScheduleSchema, now: datetime) -> Optional[datetime]:
    """The most recent slot at or before ``now`` (aware), today or yesterday."""
    today = now.astimezone(_tz(sched)).date()
    for day in (today, today - timedelta(days=1)):
        past = [s for s in slots_on(sched, day) if s <= now]
        if past:
            return past[-1]
    return None


def due_slot(sched: ScheduleSchema, now: datetime, last_slot: Optional[datetime]) -> Optional[datetime]:
    """The slot to run now: the latest one, if it's under an hour old and
    not handled yet (``last_slot`` is aware UTC). An earlier slot still
    waiting is superseded by a later one."""
    slot = latest_slot(sched, now)
    if slot is None or now - slot > GRACE or (last_slot is not None and slot <= last_slot):
        return None
    return slot


def next_run(sched: ScheduleSchema, now: datetime) -> Optional[datetime]:
    today = now.astimezone(_tz(sched)).date()
    for n in range(370):
        later = [s for s in slots_on(sched, today + timedelta(days=n)) if s > now]
        if later:
            return later[0]
    return None


def skipped_holidays(sched: ScheduleSchema, today: date, limit: int = 3) -> list[dict[str, str]]:
    """The next public holidays that fall on a scheduled day."""
    if not (sched.enabled and sched.skip_holidays and sched.times):
        return []
    found = {**public_holidays(sched.state, today.year), **public_holidays(sched.state, today.year + 1)}
    days = sorted(d for d in found if d >= today and d.weekday() in sched.days)
    return [{"date": d.isoformat(), "name": found[d]} for d in days[:limit]]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def nothing_to_search(db: Session, ws: int) -> bool:
    from backend.profile import get_profile

    prof = get_profile(db, ws)
    if prof["seek_enabled"] and prof["titles"]:
        return False
    return not (db.query(CompanySource.id)
                .filter(CompanySource.workspace_id == ws, CompanySource.enabled.is_(True)).first())


def _utc(naive: Optional[datetime]) -> Optional[datetime]:
    return naive.replace(tzinfo=timezone.utc) if naive else None


def _naive_utc(aware: datetime) -> datetime:
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def tick(start_search: Callable[[int], dict], now: Optional[datetime] = None) -> Optional[dict]:
    """Start at most one due scheduled search. ``start_search(ws)`` queues
    the run; returns the run it started, if any.

    A workspace's data is sealed under its owner's key, which the server holds only for an owner who allowed
    background access (``users.background_key``): the search runs inside that key's scope, and a slot of an owner
    who didn't is passed over."""
    from backend import migrate_encryption, users

    now = now or datetime.now(timezone.utc)
    if not migrate_encryption.is_done():
        return None  # the data is being encrypted (or waits for its account to sign in)
    if tasks.active_run() is not None:
        return None  # due slots wait (up to GRACE) for the worker
    db = SessionLocal()
    try:
        # A workspace with no owner (an install from before accounts, until its first account is created) waits.
        for ws, owner in (db.query(Workspace.id, Workspace.owner_id).filter(Workspace.owner_id.isnot(None))
                          .order_by(Workspace.id).all()):
            row = db.get(SearchSchedule, ws)
            slot = due_slot(get_schedule(db, ws), now, _utc(row.last_slot) if row else None)
            if slot is None:
                continue
            reason, run, skipped = None, None, None
            key = users.background_key(db, owner)
            if key is None:
                skipped = users.background_block(db, owner)
                reason = f"no background key ({skipped})"
            else:
                with crypto.as_user(owner, key):
                    if nothing_to_search(db, ws):
                        reason = "nothing to search"
                    elif (db.query(SearchRun.id).filter(SearchRun.workspace_id == ws, SearchRun.kind == "search",
                                                        SearchRun.started_at >= _naive_utc(slot)).first()):
                        reason = "a search already ran since"
                    else:
                        run = start_search(ws)  # the run's thread inherits this key scope
            if run is not None:
                if run.get("already_running"):
                    return None  # someone clicked a run just now; try again next tick
                log.info("workspace %s: scheduled search for %s started (run %s)", ws, slot.isoformat(), run["id"])
            else:
                log.info("workspace %s: scheduled search for %s passed over: %s", ws, slot.isoformat(), reason)
            sched_row = _row(db, ws)
            sched_row.last_slot = _naive_utc(slot)
            sched_row.skipped_reason, sched_row.skipped_slot = skipped, _naive_utc(slot) if skipped else None
            db.commit()
            if run is not None:
                return run
        return None
    finally:
        db.close()


_stop = threading.Event()


def start_scheduler(start_search: Callable[[int], dict]) -> None:
    """Check for due searches every TICK_SECONDS on a daemon thread.
    ``JOBHUNT_SCHEDULER=off`` (e.g. in .env) turns scheduled searches off, and the housekeeping with them."""
    if os.getenv("JOBHUNT_SCHEDULER", "on").strip().lower() in ("0", "off", "false", "no"):
        log.info("scheduled searches are off (JOBHUNT_SCHEDULER)")
        return
    _stop.clear()

    def loop() -> None:
        while not _stop.wait(TICK_SECONDS):
            try:
                tick(start_search)
            except Exception:
                log.exception("scheduled search check failed")
            housekeeping()

    threading.Thread(target=loop, name="jobhunt-scheduler", daemon=True).start()


CLEANUP_EVERY = timedelta(minutes=10)
_last_cleanup: Optional[datetime] = None


def housekeeping(now: Optional[datetime] = None) -> bool:
    """Every 10 minutes, with the scheduler's loop (so off with JOBHUNT_SCHEDULER=off): delete expired sessions and
    links that can't be used any more (``auth.cleanup_tick``), and empty the write-ahead log again when a scrub after
    deleting a key copy was blocked (``db.scrub_wal_again``). True when it ran. Never raises."""
    global _last_cleanup
    now = now or datetime.now(timezone.utc)
    if _last_cleanup is not None and now - _last_cleanup < CLEANUP_EVERY:
        return False
    _last_cleanup = now
    try:
        from backend import auth  # (auth imports modules that import this one)

        auth.cleanup_tick()
    except Exception as exc:  # noqa: BLE001
        log.warning("Clean-up of expired sessions and links failed: %s", exc.__class__.__name__)
    scrub_wal_again()
    return True


def stop_scheduler() -> None:
    _stop.set()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def skipped_info(db: Session, ws: int) -> Optional[dict[str, Any]]:
    """The last slot passed over for lack of a key, with the words to show, or None."""
    row = db.get(SearchSchedule, ws)
    if row is None or row.skipped_reason not in SKIP_TEXT:
        return None
    slot = _utc(row.skipped_slot)
    return {"reason": row.skipped_reason, "text": SKIP_TEXT[row.skipped_reason],
            "slot": slot.isoformat() if slot else None}


def schedule_info(db: Session, ws: int, now: Optional[datetime] = None) -> dict[str, Any]:
    from backend import users

    now = now or datetime.now(timezone.utc)
    sched = get_schedule(db, ws)
    owner = db.query(Workspace.owner_id).filter(Workspace.id == ws).scalar()
    nxt = next_run(sched, now)
    last = (db.query(SearchRun).filter(SearchRun.workspace_id == ws, SearchRun.scheduled.is_(True))
            .order_by(SearchRun.id.desc()).first())
    return {
        **sched.model_dump(),
        "states": [{"code": code, "name": name, "timezone": tz} for code, (name, tz) in STATES.items()],
        "timezone": STATES[sched.state][1],
        "next_run": nxt.isoformat() if nxt else None,
        "skipped_holidays": skipped_holidays(sched, now.astimezone(_tz(sched)).date()),
        "last_run": last.to_dict() if last else None,
        "nothing_to_search": nothing_to_search(db, ws),
        "background": owner is not None and users.background_enabled(db, owner),
        "skipped": skipped_info(db, ws),
    }


@router.get("")
def read_schedule(db: Session = Depends(get_db), ws: int = Depends(current_workspace)) -> dict[str, Any]:
    return schedule_info(db, ws)


@router.put("")
def write_schedule(payload: ScheduleSchema, db: Session = Depends(get_db),
                   ws: int = Depends(current_workspace)) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    before = latest_slot(get_schedule(db, ws), now)
    row = _row(db, ws)
    row.enabled, row.skip_holidays, row.state = payload.enabled, payload.skip_holidays, payload.state
    row.times = sorted(set(payload.times))
    row.days = sorted(set(payload.days))
    # A change applies from the next time on: switching the schedule on (or
    # adding a time) at 10:30 doesn't run the 10:00 search there and then.
    after = latest_slot(payload, now)
    if after is not None and after != before and (row.last_slot is None or _utc(row.last_slot) < after):
        row.last_slot = _naive_utc(after)
    db.commit()
    return schedule_info(db, ws, now)
