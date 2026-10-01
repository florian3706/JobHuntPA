"""Commute to a job's office at peak times, for the job page.

Public transport: the Transport for NSW Trip Planner API (free key from
https://opendata.transport.nsw.gov.au, ``TFNSW_API_KEY``). Journeys start at
each station set in Search setup ("Commute from"), or at the home pin,
arrive by the morning time and leave at the evening time on the next
Tuesday (a typical office day). Walking, buses, metro and light rail at
the office end are part of the journey.

Car: TomTom Routing with predicted traffic for those times when
``TOMTOM_API_KEY`` is set (free tier), otherwise OSRM's public server,
which knows no traffic (the page says so). Starts at the home pin.

Answers are cached for a week in ``commute_cache``.

    GET /api/jobs/{id}/commute
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.config import USER_AGENT
from backend.db import CommuteCache, Job, Pin, SessionLocal, get_db
from backend.workspaces import current_workspace, owned

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["commute"])
status_router = APIRouter(prefix="/api/commute", tags=["commute"])

TFNSW = "https://api.transport.nsw.gov.au/v1/tp/"
TOMTOM = "https://api.tomtom.com/routing/1/calculateRoute/"
OSRM = "https://router.project-osrm.org/route/v1/driving/"
TZ = ZoneInfo("Australia/Sydney")
CACHE_FOR = timedelta(days=7)
STOP_CACHE_FOR = timedelta(days=90)
# Transport for NSW product classes.
MODES = {1: "Train", 2: "Metro", 4: "Light rail", 5: "Bus", 7: "Coach", 9: "Ferry", 11: "School bus",
         99: "Walk", 100: "Walk", 107: "Cycle"}


class CommuteError(Exception):
    pass


def keys() -> dict[str, str]:
    return {"tfnsw": (os.getenv("TFNSW_API_KEY") or "").strip(), "tomtom": (os.getenv("TOMTOM_API_KEY") or "").strip()}


def peak_day(today: Optional[date] = None) -> date:
    """Next Tuesday: a typical office day, with weekday timetables."""
    today = today or datetime.now(TZ).date()
    return today + timedelta(days=(1 - today.weekday()) % 7 or 7)


def _at(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in (hhmm or "09:00").split(":")[:2])
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _cached(key: str, max_age: timedelta, fetch) -> Any:
    db = SessionLocal()
    try:
        row = db.get(CommuteCache, key)
        if row is not None and row.fetched_at > datetime.utcnow() - max_age:
            return json.loads(row.result_json)
        value = fetch()
        db.merge(CommuteCache(key=key, result_json=json.dumps(value), fetched_at=datetime.utcnow()))
        db.commit()
        return value
    finally:
        db.close()


def _get(url: str, params: dict, headers: Optional[dict] = None, what: str = "") -> dict:
    try:
        resp = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT, **(headers or {})}, timeout=30)
    except httpx.HTTPError as exc:
        raise CommuteError(f"{what}: could not connect ({exc.__class__.__name__})") from exc
    if resp.status_code in (401, 403):
        raise CommuteError(f"{what}: the API key was rejected (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        raise CommuteError(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise CommuteError(f"{what}: not a JSON answer") from exc


# --------------------------------------------------------------------------
# Public transport (Transport for NSW)
# --------------------------------------------------------------------------

def _tfnsw(call: str, params: dict, key: str) -> dict:
    return _get(TFNSW + call, {"outputFormat": "rapidJSON", "coordOutputFormat": "EPSG:4326", **params},
                {"Authorization": f"apikey {key}"}, "Transport for NSW")


def find_stop(name: str, key: str) -> Optional[dict]:
    """{id, name, lat, lng} of a station or stop by name."""
    def fetch():
        data = _tfnsw("stop_finder", {"type_sf": "any", "name_sf": name, "TfNSWSF": "true"}, key)
        stops = [loc for loc in data.get("locations") or [] if loc.get("type") in ("stop", "platform")]
        best = next((s for s in stops if s.get("isBest")), stops[0] if stops else None)
        if not best or not best.get("coord"):
            return None
        return {"id": best["id"], "name": best.get("disassembledName") or best.get("name") or name,
                "lat": best["coord"][0], "lng": best["coord"][1]}
    return _cached(f"stop|{name.lower()}", STOP_CACHE_FOR, fetch)


def _place(p: dict) -> tuple[str, str]:
    """(type, name) for a trip request end: a stop id or a coordinate."""
    if p.get("id"):
        return "any", str(p["id"])
    return "coord", f"{p['lng']:.6f}:{p['lat']:.6f}:EPSG:4326"


def _local(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)


def summarise_journey(journey: dict) -> Optional[dict]:
    legs = []
    for leg in journey.get("legs") or []:
        transport = leg.get("transportation") or {}
        mode = MODES.get((transport.get("product") or {}).get("class"), "Transport")
        o, d = leg.get("origin") or {}, leg.get("destination") or {}
        dep = _local(o.get("departureTimeEstimated") or o.get("departureTimePlanned"))
        arr = _local(d.get("arrivalTimeEstimated") or d.get("arrivalTimePlanned"))
        legs.append({
            "mode": mode,
            "line": "" if mode == "Walk" else (transport.get("disassembledName") or transport.get("number") or ""),
            "line_name": "" if mode == "Walk" else (transport.get("name") or ""),
            "towards": (transport.get("destination") or {}).get("name") or "",
            "from": o.get("disassembledName") or o.get("name") or "",
            "to": d.get("disassembledName") or d.get("name") or "",
            "depart": dep.strftime("%H:%M") if dep else "",
            "arrive": arr.strftime("%H:%M") if arr else "",
            "minutes": round((leg.get("duration") or 0) / 60),
            "coords": [[round(c[0], 5), round(c[1], 5)] for c in leg.get("coords") or []],
            "_dep": dep, "_arr": arr,
        })
    timed = [leg for leg in legs if leg["_dep"] and leg["_arr"]]
    if not timed:
        return None
    start, end = timed[0]["_dep"], timed[-1]["_arr"]
    for leg in legs:
        leg.pop("_dep"), leg.pop("_arr")
    rides = [leg for leg in legs if leg["mode"] != "Walk"]
    return {"depart": start.strftime("%H:%M"), "arrive": end.strftime("%H:%M"),
            "minutes": round((end - start).total_seconds() / 60), "changes": max(0, len(rides) - 1),
            "summary": " → ".join(f"{leg['mode']} {leg['line']}".strip() for leg in rides) or "Walk",
            "legs": legs, "_start": start.isoformat(), "_end": end.isoformat()}


def best_journey(data: dict, arrive_by: Optional[datetime] = None) -> Optional[dict]:
    """Arrive-by: the one leaving latest that's on time (fewest changes on a
    tie). Leave-at: the one arriving first."""
    found = [j for j in (summarise_journey(j) for j in data.get("journeys") or []) if j]
    if arrive_by is not None:
        on_time = [j for j in found if datetime.fromisoformat(j["_end"]) <= arrive_by] or found
        best = max(on_time, key=lambda j: (j["_start"], -j["changes"]), default=None)
    else:
        best = min(found, key=lambda j: (j["_end"], j["changes"]), default=None)
    if best:
        best = {k: v for k, v in best.items() if not k.startswith("_")}
    return best


def transit(origin: dict, dest: dict, when: datetime, arrive: bool, key: str) -> Optional[dict]:
    o_type, o_name = _place(origin)
    d_type, d_name = _place(dest)
    cache_key = f"tfnsw|{o_name}|{d_name}|{'arr' if arrive else 'dep'}|{when.strftime('%a %H%M')}"

    def fetch():
        data = _tfnsw("trip", {
            "depArrMacro": "arr" if arrive else "dep", "itdDate": when.strftime("%Y%m%d"),
            "itdTime": when.strftime("%H%M"), "type_origin": o_type, "name_origin": o_name,
            "type_destination": d_type, "name_destination": d_name, "calcNumberOfTrips": 5, "TfNSWTR": "true",
        }, key)
        return best_journey(data, when if arrive else None)
    return _cached(cache_key, CACHE_FOR, fetch)


# --------------------------------------------------------------------------
# Car
# --------------------------------------------------------------------------

def drive_tomtom(origin: dict, dest: dict, when: datetime, arrive: bool, key: str) -> dict:
    url = f"{TOMTOM}{origin['lat']:.5f},{origin['lng']:.5f}:{dest['lat']:.5f},{dest['lng']:.5f}/json"
    params = {"key": key, "traffic": "true", "travelMode": "car", "routeType": "fastest",
              ("arriveAt" if arrive else "departAt"): when.isoformat(timespec="seconds")}
    data = _get(url, params, what="TomTom")
    route = (data.get("routes") or [None])[0]
    if not route:
        raise CommuteError("TomTom: no route found")
    summary = route["summary"]
    points = [[round(p["latitude"], 5), round(p["longitude"], 5)]
              for leg in route.get("legs") or [] for p in leg.get("points") or []]
    dep, arr = _local(summary.get("departureTime")), _local(summary.get("arrivalTime"))
    return {"minutes": round(summary["travelTimeInSeconds"] / 60), "km": round(summary["lengthInMeters"] / 1000, 1),
            "traffic_minutes": round((summary.get("trafficDelayInSeconds") or 0) / 60),
            "depart": dep.strftime("%H:%M") if dep else "", "arrive": arr.strftime("%H:%M") if arr else "",
            "coords": points[::max(1, len(points) // 300)]}


def drive_osrm(origin: dict, dest: dict) -> dict:
    url = f"{OSRM}{origin['lng']:.5f},{origin['lat']:.5f};{dest['lng']:.5f},{dest['lat']:.5f}"
    data = _get(url, {"overview": "simplified", "geometries": "geojson"}, what="OSRM")
    route = (data.get("routes") or [None])[0]
    if not route:
        raise CommuteError("OSRM: no route found")
    return {"minutes": round(route["duration"] / 60), "km": round(route["distance"] / 1000, 1),
            "coords": [[round(c[1], 5), round(c[0], 5)] for c in route["geometry"]["coordinates"]]}


def drive(origin: dict, dest: dict, when: datetime, arrive: bool, key: str) -> dict:
    if key:
        cache_key = f"tomtom|{origin['lat']:.4f},{origin['lng']:.4f}|{dest['lat']:.4f},{dest['lng']:.4f}|" \
                    f"{'arr' if arrive else 'dep'}|{when.strftime('%a %H%M')}"
        return {**_cached(cache_key, CACHE_FOR, lambda: drive_tomtom(origin, dest, when, arrive, key)),
                "traffic": True}
    cache_key = f"osrm|{origin['lat']:.4f},{origin['lng']:.4f}|{dest['lat']:.4f},{dest['lng']:.4f}"
    return {**_cached(cache_key, CACHE_FOR, lambda: drive_osrm(origin, dest)), "traffic": False}


# --------------------------------------------------------------------------
# A job's commute
# --------------------------------------------------------------------------

def home_pin(db: Session, ws: int) -> Optional[dict]:
    pins = db.query(Pin).filter(Pin.workspace_id == ws).order_by(Pin.id).all()
    pin = (next((p for p in pins if p.kind == "home"), None)
           or next((p for p in pins if "home" in (p.label or "").lower()), None)
           or (pins[0] if pins else None))
    return {"name": pin.label or "Home", "lat": pin.lat, "lng": pin.lng} if pin else None


def _maps_link(origin: dict, dest: dict, mode: str) -> str:
    return (f"https://www.google.com/maps/dir/?api=1&origin={origin['lat']:.5f},{origin['lng']:.5f}"
            f"&destination={dest['lat']:.5f},{dest['lng']:.5f}&travelmode={mode}")


def commute_for(db: Session, job: Job) -> dict:
    from backend.offices import office_unknown
    from backend.profile import get_profile

    if job.lat is None or job.lng is None:
        raise HTTPException(status_code=409, detail="This job isn't on the map: its location couldn't be found. "
                                                    "Set the office on this page to see the commute.")
    prof = get_profile(db, job.workspace_id)
    home = home_pin(db, job.workspace_id)
    if home is None:
        raise HTTPException(status_code=409, detail="Drop a home pin on the Map tab to see commutes.")
    office = {"name": job.office_text or job.location_text or "Office", "lat": job.lat, "lng": job.lng,
              "precise": job.office_lat is not None or not office_unknown(job), "source": job.office_source}
    day = peak_day()
    morning, evening = _at(day, prof["commute_arrive_by"]), _at(day, prof["commute_leave_at"])
    k = keys()
    notes: list[str] = []
    if not office["precise"]:
        notes.append("The ad names only a city, so times are to its centre. Set the office for exact times.")

    starts: list[dict] = []
    if k["tfnsw"]:
        for name in prof["commute_from"] or []:
            try:
                stop = find_stop(name, k["tfnsw"])
            except CommuteError as exc:
                notes.append(str(exc))
                break
            if stop:
                starts.append(stop)
            else:
                notes.append(f'Transport for NSW doesn\'t know a stop called "{name}".')
    from_stations = bool(starts)
    if not starts:
        starts = [{**home, "id": None}]

    trips = []
    if k["tfnsw"]:
        for start in starts:
            row = {"from": start["name"], "start": {"lat": start["lat"], "lng": start["lng"]}}
            try:
                row["there"] = transit(start, office, morning, True, k["tfnsw"])
                row["back"] = transit(office, start, evening, False, k["tfnsw"])
            except CommuteError as exc:
                row["error"] = str(exc)
            trips.append(row)
    else:
        notes.append("Public transport times need a free Transport for NSW API key (TFNSW_API_KEY); "
                     "see the README.")

    car: dict[str, Any] = {}
    try:
        car = {"there": drive(home, office, morning, True, k["tomtom"]),
               "back": drive(office, home, evening, False, k["tomtom"])}
    except CommuteError as exc:
        car = {"error": str(exc)}
    if not k["tomtom"] and "error" not in car:
        notes.append("Driving times are without traffic; peak traffic adds a lot. A free TomTom key "
                     "(TOMTOM_API_KEY) gives peak-hour times.")

    return {
        "day": f"{day:%A} {day.day} {day:%B}",
        "arrive_by": prof["commute_arrive_by"], "leave_at": prof["commute_leave_at"],
        "home": home, "office": office, "transit": trips, "car": car, "notes": notes, "from_stations": from_stations,
        "stations": prof["commute_from"] or [],
        "links": {"transit": _maps_link(starts[0], office, "transit"), "car": _maps_link(home, office, "driving")},
    }


@router.get("/{job_id}/commute")
def job_commute(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    job = owned(db, Job, job_id, ws, "Job")
    if job.duplicate_of:
        job = db.get(Job, job.duplicate_of) or job
    return commute_for(db, job)


@status_router.get("/status")
def commute_status():
    """Which commute services are configured (never the keys themselves)."""
    k = keys()
    return {"transit": bool(k["tfnsw"]), "traffic": bool(k["tomtom"])}
