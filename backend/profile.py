"""Search profile, dealbreakers and location pins (API + store helpers).

Endpoints (signed in, for the request's workspace):
    GET/PUT /api/profile        titles, keywords, salary floor, work modes, SEEK settings, commute starting points
    GET/PUT /api/dealbreakers   {industries[], keywords[]}
    GET/POST /api/pins, PUT/DELETE /api/pins/{id}
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from backend import commute
from backend.db import DealbreakerSet, Pin, UserProfile, get_db
from backend.workspaces import current_workspace, owned

router = APIRouter()

PinKind = Literal["home", "hybrid", "onsite"]
DEFAULT_PIN_RADIUS_KM = 25.0
# A technical bound on one PUT, far above any real use (spec §1.16: no product limit on starting points).
STARTING_POINTS_MAX = 500


class StartingPoint(BaseModel):
    """One place the commute links start from: one of the workspace's pins, or a typed place (an address, a station,
    a bus stop), with what it is for. An empty label shows the place's (or the pin's) name."""

    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=commute.LABEL_MAX)
    pin_id: Optional[int] = Field(default=None, ge=1)
    place: Optional[str] = Field(default=None, max_length=commute.PLACE_MAX)
    use: Literal["driving", "transit", "both"] = "both"

    @model_validator(mode="after")
    def _one_place(self) -> "StartingPoint":
        typed = bool((self.place or "").strip())
        if (self.pin_id is None) == (not typed):
            raise ValueError("A starting point is either a pin (pin_id) or a typed place, not both or neither.")
        return self


class ProfileSchema(BaseModel):
    titles: list[str] = Field(default_factory=list)
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)
    salary_floor: Optional[int] = Field(default=None, ge=0)
    remote_aus_ok: bool = True
    remote_global_ok: bool = False
    allow_hybrid: bool = True
    max_office_days: Optional[int] = Field(default=None, ge=0, le=5)
    allow_onsite: bool = True
    seek_enabled: bool = True
    seek_locations: list[str] = Field(default_factory=list)
    # Where the job page's Google Maps links start (backend.commute), besides the home pins. Left out of a PUT: kept
    # as it is.
    starting_points: Optional[list[StartingPoint]] = Field(default=None, max_length=STARTING_POINTS_MAX)


class DealbreakersSchema(BaseModel):
    industries: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class PinCreate(BaseModel):
    label: str = Field(min_length=1)
    kind: PinKind
    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)
    radius_km: float = Field(default=DEFAULT_PIN_RADIUS_KM, gt=0)


class PinUpdate(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1)
    kind: Optional[PinKind] = None
    lat: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    lng: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    radius_km: Optional[float] = Field(default=None, gt=0)


class PinOut(BaseModel):
    id: int
    label: str
    kind: PinKind
    lat: float
    lng: float
    radius_km: float


def _clean_tags(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))


def get_profile(db: Session, ws: int) -> dict[str, Any]:
    row = db.get(UserProfile, ws)
    if row is None:
        return {**ProfileSchema().model_dump(), "starting_points": []}
    return {
        "titles": list(row.titles or []),
        "keywords_include": list(row.keywords_include or []),
        "keywords_exclude": list(row.keywords_exclude or []),
        "salary_floor": row.salary_floor,
        "remote_aus_ok": bool(row.remote_aus_ok),
        "remote_global_ok": bool(row.remote_global_ok),
        "allow_hybrid": bool(row.allow_hybrid),
        "max_office_days": row.max_office_days,
        "allow_onsite": bool(row.allow_onsite),
        "seek_enabled": bool(row.seek_enabled),
        "seek_locations": list(row.seek_locations or []),
        "starting_points": commute.saved_starting_points(row),
    }


def save_profile(db: Session, ws: int, data: ProfileSchema) -> dict[str, Any]:
    row = db.get(UserProfile, ws)
    if row is None:
        row = UserProfile(id=ws)
        db.add(row)
    row.titles = _clean_tags(data.titles)
    row.keywords_include = _clean_tags(data.keywords_include)
    row.keywords_exclude = _clean_tags(data.keywords_exclude)
    row.salary_floor = data.salary_floor
    row.remote_aus_ok = data.remote_aus_ok
    row.remote_global_ok = data.remote_global_ok
    row.allow_hybrid = data.allow_hybrid
    row.max_office_days = data.max_office_days
    row.allow_onsite = data.allow_onsite
    row.seek_enabled = data.seek_enabled
    row.seek_locations = _clean_tags(data.seek_locations)
    if data.starting_points is not None:
        points = commute.clean_starting_points(p.model_dump() for p in data.starting_points)
        # A pin start must name one of this workspace's pins (plaintext Pin.id/workspace_id). One whose pin is gone
        # is kept only if it was already saved, so the page can still show it as deleted; any other pin id (a pin
        # of another search, a made-up number) is dropped rather than refusing the whole save. Looked up only
        # within this workspace (commute.starts_for, export), a kept id never reaches another workspace's pin.
        pins = {pin_id for (pin_id,) in db.query(Pin.id).filter(Pin.workspace_id == ws)}
        stored = {p["pin_id"] for p in commute.clean_starting_points(row.starting_points or [])
                  if p["pin_id"] is not None}
        row.starting_points = [p for p in points if p["pin_id"] is None or p["pin_id"] in pins
                               or p["pin_id"] in stored]
        # The older search setup's stations are now starting points, and its times are gone (spec §1.8).
        row.commute_from, row.commute_arrive_by, row.commute_leave_at = None, "", ""
    db.commit()
    return get_profile(db, ws)


def get_dealbreakers(db: Session, ws: int) -> dict[str, Any]:
    row = db.get(DealbreakerSet, ws)
    if row is None:
        return {"industries": [], "keywords": []}
    return {"industries": list(row.industries or []), "keywords": list(row.keywords or [])}


def save_dealbreakers(db: Session, ws: int, data: DealbreakersSchema) -> dict[str, Any]:
    row = db.get(DealbreakerSet, ws)
    if row is None:
        row = DealbreakerSet(id=ws)
        db.add(row)
    row.industries = _clean_tags(data.industries)
    row.keywords = _clean_tags(data.keywords)
    db.commit()
    return get_dealbreakers(db, ws)


def _pin_to_dict(pin: Pin) -> dict[str, Any]:
    return {"id": pin.id, "label": pin.label, "kind": pin.kind,
            "lat": pin.lat, "lng": pin.lng, "radius_km": pin.radius_km}


def list_pins(db: Session, ws: int) -> list[dict[str, Any]]:
    return [_pin_to_dict(p) for p in db.query(Pin).filter(Pin.workspace_id == ws).order_by(Pin.id).all()]


@router.get("/api/profile")
def read_profile(db: Session = Depends(get_db), ws: int = Depends(current_workspace)) -> dict[str, Any]:
    return get_profile(db, ws)


@router.put("/api/profile")
def write_profile(payload: ProfileSchema, db: Session = Depends(get_db),
                  ws: int = Depends(current_workspace)) -> dict[str, Any]:
    return save_profile(db, ws, payload)


@router.get("/api/dealbreakers")
def read_dealbreakers(db: Session = Depends(get_db), ws: int = Depends(current_workspace)) -> dict[str, Any]:
    return get_dealbreakers(db, ws)


@router.put("/api/dealbreakers")
def write_dealbreakers(payload: DealbreakersSchema, db: Session = Depends(get_db),
                       ws: int = Depends(current_workspace)) -> dict[str, Any]:
    return save_dealbreakers(db, ws, payload)


@router.get("/api/pins", response_model=list[PinOut])
def read_pins(db: Session = Depends(get_db), ws: int = Depends(current_workspace)) -> list[dict[str, Any]]:
    return list_pins(db, ws)


@router.post("/api/pins", response_model=PinOut, status_code=status.HTTP_201_CREATED)
def add_pin(payload: PinCreate, db: Session = Depends(get_db), ws: int = Depends(current_workspace)) -> dict[str, Any]:
    pin = Pin(workspace_id=ws, label=payload.label.strip(), kind=payload.kind, lat=payload.lat,
              lng=payload.lng, radius_km=payload.radius_km)
    db.add(pin)
    db.commit()
    return _pin_to_dict(pin)


@router.put("/api/pins/{pin_id}", response_model=PinOut)
def edit_pin(pin_id: int, payload: PinUpdate, db: Session = Depends(get_db),
             ws: int = Depends(current_workspace)) -> dict[str, Any]:
    pin = owned(db, Pin, pin_id, ws, "Pin")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(pin, field, value.strip() if field == "label" else value)
    db.commit()
    return _pin_to_dict(pin)


@router.delete("/api/pins/{pin_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_pin(pin_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)) -> None:
    pin = owned(db, Pin, pin_id, ws, "Pin")
    db.delete(pin)
    forget_pin_starts(db, ws, pin_id)
    db.commit()


def forget_pin_starts(db: Session, ws: int, pin_id: int) -> None:
    """Drop the workspace's starting points that use this pin (decided in Python: the column is sealed). The caller
    commits, so the pin and its starting points go together."""
    row = db.get(UserProfile, ws)
    if row is None or row.starting_points is None:
        return
    points = commute.clean_starting_points(row.starting_points)
    kept = [p for p in points if p["pin_id"] != pin_id]
    if len(kept) != len(points):
        row.starting_points = kept
