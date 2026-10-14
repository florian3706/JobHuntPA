"""Commute links: Google Maps directions from the user's starting points to a job's office (open-source spec §1.8).

**Starting points** are set in Search setup > Commute and kept in ``user_profile.starting_points`` (sealed): any
number, each ``{"label", "pin_id" | "place", "use"}``, where the place is one of the workspace's pins or typed text
(an address, a station, a bus stop) and the use is driving, public transport or both. Every home pin is a starting
point for both without being added; a starting point made from a home pin takes its place (so its use can be
narrowed). A search setup saved before starting points came in has ``starting_points`` NULL: its "Commute from"
stations (``commute_from``) are read as public-transport starting points until the search setup is saved again, and
its arrive-by and leave-at times are not used any more.

**The app sends nothing anywhere for a commute.** ``GET /api/jobs/{id}/commute`` answers with the places only, and
the job page builds each link itself (``https://www.google.com/maps/dir/?api=1&origin=…&destination=…&travelmode=…``,
with ``encodeURIComponent``); opening one is the user's own visit to Google, which then sees both places. There are no
API keys, no cache and no outside request here.

    GET /api/jobs/{id}/commute   signed in; the job must be in the request's workspace (else 404)
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import Job, Pin, UserProfile, get_db
from backend.workspaces import current_workspace, owned

router = APIRouter(prefix="/api/jobs", tags=["commute"])

USES = ("driving", "transit", "both")
# Google Maps travel modes for each use, in the order the links are listed.
MODES = {"driving": ("driving",), "transit": ("transit",), "both": ("driving", "transit")}
LABEL_MAX, PLACE_MAX = 80, 200


def _text(value: Any, limit: int) -> str:
    """One line of plain text: whitespace collapsed, control characters dropped, cut to ``limit``."""
    return " ".join("".join(ch for ch in str(value or "") if ch.isprintable() or ch.isspace()).split())[:limit]


def legacy_starting_points(stations: Iterable[Any]) -> list[dict]:
    """An older search setup's "Commute from" stations, as public-transport starting points."""
    out, seen = [], set()
    for station in stations or []:
        name = _text(station, PLACE_MAX)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"label": name[:LABEL_MAX], "pin_id": None, "place": name, "use": "transit"})
    return out


def clean_starting_points(items: Iterable[Any]) -> list[dict]:
    """Stored starting points, each reduced to its four fields; malformed ones are dropped."""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        use = item.get("use") if item.get("use") in USES else "both"
        pin_id = item.get("pin_id")
        place = _text(item.get("place"), PLACE_MAX)
        if isinstance(pin_id, int) and not isinstance(pin_id, bool):
            place = ""
        elif place:
            pin_id = None
        else:
            continue
        out.append({"label": _text(item.get("label"), LABEL_MAX), "pin_id": pin_id, "place": place or None,
                    "use": use})
    return out


def saved_starting_points(row: Optional[UserProfile]) -> list[dict]:
    """The workspace's own starting points (not the automatic home pins), from its search-setup row."""
    if row is None:
        return []
    if row.starting_points is None:
        return legacy_starting_points(row.commute_from or [])
    return clean_starting_points(row.starting_points)


def _pin_origin(pin: Pin) -> str:
    return f"{pin.lat:.6f},{pin.lng:.6f}"


def starts_for(db: Session, ws: int) -> tuple[list[dict], int]:
    """([{label, origin, modes, kind}], how many starting points name a pin that is gone). ``origin`` is what goes into
    the link: a pin's coordinates, or the typed place. Home pins first, in pin order, then the saved ones in theirs."""
    pins = {p.id: p for p in db.query(Pin).filter(Pin.workspace_id == ws).order_by(Pin.id)}
    saved = saved_starting_points(db.get(UserProfile, ws))
    chosen = {s["pin_id"] for s in saved if s["pin_id"] is not None}
    out = [{"label": p.label or "Home", "origin": _pin_origin(p), "modes": list(MODES["both"]), "kind": "home"}
           for p in pins.values() if p.kind == "home" and p.id not in chosen]
    missing = 0
    for s in saved:
        if s["pin_id"] is not None:
            pin = pins.get(s["pin_id"])
            if pin is None:
                missing += 1
                continue
            out.append({"label": s["label"] or pin.label or "Pin", "origin": _pin_origin(pin),
                        "modes": list(MODES[s["use"]]), "kind": "pin"})
        else:
            out.append({"label": s["label"] or s["place"], "origin": s["place"], "modes": list(MODES[s["use"]]),
                        "kind": "place"})
    return out, missing


def destination_for(job: Job) -> Optional[dict]:
    """{text, office}: the office picked or found for the job (its address, as Google is given it), else the ad's
    place (the first one an ad lists). None when the job names no place at all."""
    if job.office_text and job.office_text.strip():
        return {"text": _text(job.office_text, PLACE_MAX), "office": True}
    first = _text((job.location_text or "").split(";")[0], PLACE_MAX)
    if first:
        return {"text": first, "office": False}
    if job.lat is not None and job.lng is not None:
        return {"text": f"{job.lat:.6f},{job.lng:.6f}", "office": False}
    return None


def commute_for(db: Session, job: Job) -> dict:
    from backend.offices import office_unknown

    destination = destination_for(job)
    if destination is None:
        raise HTTPException(status_code=409, detail="The ad names no place, so there is nowhere to link to. "
                                                    "Set the office on this page to get the links.")
    starts, missing = starts_for(db, job.workspace_id)
    notes: list[str] = []
    if job.work_mode == "remote":
        notes.append("The ad says this job is remote.")
    elif not destination["office"] and office_unknown(job):
        notes.append("The ad names only a city, so the links go to the city. Set the office for exact directions.")
    if missing:
        notes.append(f"{missing} of your starting points use a pin that has been deleted: change "
                     f"{'it' if missing == 1 else 'them'} under Search setup > Commute.")
    return {"destination": destination["text"], "to_office": destination["office"], "starts": starts,
            "notes": notes}


@router.get("/{job_id}/commute")
def job_commute(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    """The places for the job page's Google Maps links. Nothing is requested from anywhere."""
    job = owned(db, Job, job_id, ws, "Job")
    if job.duplicate_of:
        job = db.get(Job, job.duplicate_of) or job
    return commute_for(db, job)
