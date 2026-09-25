"""Search-profile + pins API router.

Coordination note: ``backend/db.py`` and ``backend/app.py`` are owned by the
scaffolder agent. This module only *imports* from ``backend.db`` (``Base``,
``get_db``) and registers its tables on the same ``Base`` metadata, so the
scaffolder's ``init_db()`` (``Base.metadata.create_all``) creates them
automatically. It never modifies ``db.py`` / ``app.py`` / ``frontend/`` /
``backend/adapters/`` / ``backend/scorer.py``.

Wiring (for the owner of ``backend/app.py``)::

    from backend.profile import router as profile_router
    app.include_router(profile_router)

Endpoints (all JSON):
    GET    /api/profile          search profile (titles, keywords, salary, remote flags)
    PUT    /api/profile          full replace, instant-save semantics
    GET    /api/dealbreakers     tag arrays {industries[], keywords[]}
    PUT    /api/dealbreakers     full replace, instant-save semantics
    GET    /api/pins             list all pins
    POST   /api/pins             create pin -> 201
    PUT    /api/pins/{id}        partial update, merge onto existing
    DELETE /api/pins/{id}        -> 204

Pure helpers (stdlib only, unit-testable without FastAPI/SQLAlchemy/geopy):
    distance_km(lat1, lng1, lat2, lng2)
    classify_work_mode(job_text, job_lat, job_lng, pins)
    geocode_location(query, timeout=10)  (needs geopy; returns None if unavailable)
"""
from __future__ import annotations

import math
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, Float, Integer, JSON, String
from sqlalchemy.orm import Session

from backend.db import Base, get_db

router = APIRouter()

PIN_KINDS = ("home", "hybrid", "onsite")
PinKind = Literal["home", "hybrid", "onsite"]
WorkMode = Literal["remote_aus", "remote_global", "hybrid", "onsite", "unknown"]

GEOCODER_USER_AGENT = "JobHuntPA"
DEFAULT_PIN_RADIUS_KM = 25.0
EARTH_RADIUS_KM = 6371.0088


# ---------------------------------------------------------------------------
# DB models (registered on backend.db.Base -> created by scaffolder init_db())
# ---------------------------------------------------------------------------

class UserProfile(Base):
    """Single-row table (id=1): search profile."""

    __tablename__ = "user_profile"

    id = Column(Integer, primary_key=True)
    titles = Column(JSON, default=list, nullable=False)
    keywords_include = Column(JSON, default=list, nullable=False)
    keywords_exclude = Column(JSON, default=list, nullable=False)
    salary_floor = Column(Integer, nullable=True)
    remote_aus_ok = Column(Boolean, default=True, nullable=False)
    remote_global_ok = Column(Boolean, default=False, nullable=False)


class DealbreakerSet(Base):
    """Single-row table (id=1): exclusion tag arrays."""

    __tablename__ = "dealbreakers"

    id = Column(Integer, primary_key=True)
    industries = Column(JSON, default=list, nullable=False)
    keywords = Column(JSON, default=list, nullable=False)


class Pin(Base):
    """Location pins: home base + acceptable hybrid/onsite workplaces."""

    __tablename__ = "pins"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # home | hybrid | onsite
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    radius_km = Column(Float, default=DEFAULT_PIN_RADIUS_KM, nullable=False)


def init_profile_tables() -> None:
    """Ensure profile/pins tables exist (imports models, delegates to db.init_db)."""
    from backend.db import init_db

    init_db()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ProfileSchema(BaseModel):
    titles: list[str] = Field(default_factory=list)
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)
    salary_floor: Optional[int] = Field(default=None, ge=0)
    remote_aus_ok: bool = True
    remote_global_ok: bool = False


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


# ---------------------------------------------------------------------------
# Pure helpers (no third-party imports -> trivially unit-testable)
# ---------------------------------------------------------------------------

def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two WGS84 points (haversine), in km."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


_GLOBAL_PATTERNS = (
    "work from anywhere",
    "remote global",
    "global remote",
    "remote worldwide",
    "worldwide remote",
    "remote - global",
    "remote (global)",
    "anywhere in the world",
    "fully remote worldwide",
    "remote first global",
)
_AUS_PATTERNS = (
    "remote australia",
    "remote aus",
    "australia remote",
    "australia-based remote",
    "remote within australia",
    "remote role australia",
    "wfh australia",
)
_BARE_REMOTE_PATTERNS = (
    "fully remote",
    "100% remote",
    "100 % remote",
    "work from home",
    "wfh",
    "remote work",
    "remote position",
    "remote role",
    "remote job",
    "remote opportunity",
    "remote ",
    " remote",
)
_HYBRID_PATTERNS = ("hybrid", "hybrid working", "flexible hybrid", "2 days in office",
                    "3 days in office", "days in office")
_ONSITE_PATTERNS = ("onsite", "on-site", "on site", "in office", "in-office",
                    "office based", "office-based", "5 days in office")


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(p in text for p in patterns)


def classify_work_mode(
    job_text: Optional[str] = None,
    job_lat: Optional[float] = None,
    job_lng: Optional[float] = None,
    pins: Optional[list[Any]] = None,
) -> WorkMode:
    """Infer a job's work mode.

    Precedence (deterministic, text beats location):
      1. Explicit global-remote phrasing  -> ``remote_global``.
      2. Explicit Australia-remote phrasing, or bare remote/WFH phrasing
         (product scope is the Australian market, so unqualified "remote"
         defaults to ``remote_aus``)      -> ``remote_aus``.
      3. Hybrid phrasing                   -> ``hybrid``.
      4. Onsite phrasing                   -> ``onsite``.
      5. Location fallback: job coords within a pin's ``radius_km`` resolve
         to that pin's kind, with ``home`` mapping to ``onsite`` (a job at
         the candidate's doorstep with no remote flag is commutable onsite
         work). Priority on overlap: onsite > hybrid > home.
      6. Otherwise                          -> ``unknown``.

    ``pins`` accepts dicts or objects with ``kind``/``lat``/``lng``/
    ``radius_km`` attributes. Entries missing coords are skipped.
    """
    text = (job_text or "").lower()

    if text:
        if _matches(text, _GLOBAL_PATTERNS):
            return "remote_global"
        if _matches(text, _AUS_PATTERNS) or _matches(text, _BARE_REMOTE_PATTERNS):
            return "remote_aus"
        if _matches(text, _HYBRID_PATTERNS):
            return "hybrid"
        if _matches(text, _ONSITE_PATTERNS):
            return "onsite"

    if job_lat is not None and job_lng is not None and pins:
        in_range: set[str] = set()
        for pin in pins:
            if isinstance(pin, dict):
                kind, plat, plng = pin.get("kind"), pin.get("lat"), pin.get("lng")
                prad = pin.get("radius_km", DEFAULT_PIN_RADIUS_KM)
            else:
                kind, plat, plng = getattr(pin, "kind", None), getattr(pin, "lat", None), getattr(
                    pin, "lng", None)
                prad = getattr(pin, "radius_km", DEFAULT_PIN_RADIUS_KM)
            if kind not in PIN_KINDS or plat is None or plng is None:
                continue
            try:
                dist = distance_km(job_lat, job_lng, float(plat), float(plng))
            except (TypeError, ValueError):
                continue
            if dist <= float(prad or 0):
                in_range.add(kind)
        if "onsite" in in_range or "home" in in_range:
            return "onsite"
        if "hybrid" in in_range:
            return "hybrid"

    return "unknown"


def home_distance_km(
    job_lat: float, job_lng: float, pins: Optional[list[Any]] = None
) -> Optional[float]:
    """Distance from a job to the nearest ``home`` pin (None if no home pin)."""
    best: Optional[float] = None
    for pin in pins or []:
        if isinstance(pin, dict):
            kind, plat, plng = pin.get("kind"), pin.get("lat"), pin.get("lng")
        else:
            kind, plat, plng = getattr(pin, "kind", None), getattr(pin, "lat", None), getattr(
                pin, "lng", None)
        if kind != "home" or plat is None or plng is None:
            continue
        dist = distance_km(job_lat, job_lng, float(plat), float(plng))
        if best is None or dist < best:
            best = dist
    return best


def geocode_location(query: str, timeout: int = 10) -> Optional[dict[str, Any]]:
    """Geocode a free-text location via geopy Nominatim.

    Returns ``{"lat": float, "lng": float, "display_name": str}`` or ``None``
    when geopy is unavailable, the query is empty, or the lookup fails.
    """
    if not query or not query.strip():
        return None
    try:
        from geopy.geocoders import Nominatim
    except ImportError:
        return None
    try:
        geolocator = Nominatim(user_agent=GEOCODER_USER_AGENT, timeout=timeout)
        location = geolocator.geocode(query.strip())
    except Exception:
        return None
    if location is None:
        return None
    return {"lat": location.latitude, "lng": location.longitude,
            "display_name": location.address}


# ---------------------------------------------------------------------------
# Store helpers (single-row get-or-create)
# ---------------------------------------------------------------------------

def _clean_tags(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def get_profile(db: Session) -> dict[str, Any]:
    row = db.query(UserProfile).filter(UserProfile.id == 1).first()
    if row is None:
        return {"titles": [], "keywords_include": [], "keywords_exclude": [],
                "salary_floor": None, "remote_aus_ok": True, "remote_global_ok": False}
    return {"titles": list(row.titles or []), "keywords_include": list(row.keywords_include or []),
            "keywords_exclude": list(row.keywords_exclude or []), "salary_floor": row.salary_floor,
            "remote_aus_ok": bool(row.remote_aus_ok),
            "remote_global_ok": bool(row.remote_global_ok)}


def save_profile(db: Session, data: ProfileSchema) -> dict[str, Any]:
    row = db.query(UserProfile).filter(UserProfile.id == 1).first()
    if row is None:
        row = UserProfile(id=1)
        db.add(row)
    row.titles = _clean_tags(data.titles)
    row.keywords_include = _clean_tags(data.keywords_include)
    row.keywords_exclude = _clean_tags(data.keywords_exclude)
    row.salary_floor = data.salary_floor
    row.remote_aus_ok = data.remote_aus_ok
    row.remote_global_ok = data.remote_global_ok
    db.commit()
    db.refresh(row)
    return get_profile(db)


def get_dealbreakers(db: Session) -> dict[str, Any]:
    row = db.query(DealbreakerSet).filter(DealbreakerSet.id == 1).first()
    if row is None:
        return {"industries": [], "keywords": []}
    return {"industries": list(row.industries or []), "keywords": list(row.keywords or [])}


def save_dealbreakers(db: Session, data: DealbreakersSchema) -> dict[str, Any]:
    row = db.query(DealbreakerSet).filter(DealbreakerSet.id == 1).first()
    if row is None:
        row = DealbreakerSet(id=1)
        db.add(row)
    row.industries = _clean_tags(data.industries)
    row.keywords = _clean_tags(data.keywords)
    db.commit()
    db.refresh(row)
    return get_dealbreakers(db)


def _pin_to_dict(pin: Pin) -> dict[str, Any]:
    return {"id": pin.id, "label": pin.label, "kind": pin.kind,
            "lat": pin.lat, "lng": pin.lng, "radius_km": pin.radius_km}


def list_pins(db: Session) -> list[dict[str, Any]]:
    return [_pin_to_dict(p) for p in db.query(Pin).order_by(Pin.id).all()]


def create_pin(db: Session, data: PinCreate) -> dict[str, Any]:
    pin = Pin(label=data.label.strip(), kind=data.kind, lat=data.lat, lng=data.lng,
              radius_km=data.radius_km)
    db.add(pin)
    db.commit()
    db.refresh(pin)
    return _pin_to_dict(pin)


def update_pin(db: Session, pin_id: int, data: PinUpdate) -> dict[str, Any]:
    pin = db.query(Pin).filter(Pin.id == pin_id).first()
    if pin is None:
        raise HTTPException(status_code=404, detail=f"Pin {pin_id} not found")
    patch = data.model_dump(exclude_unset=True)
    if "label" in patch and patch["label"] is not None:
        pin.label = patch["label"].strip()
    for field in ("kind", "lat", "lng", "radius_km"):
        if patch.get(field) is not None:
            setattr(pin, field, patch[field])
    db.commit()
    db.refresh(pin)
    return _pin_to_dict(pin)


def delete_pin(db: Session, pin_id: int) -> None:
    pin = db.query(Pin).filter(Pin.id == pin_id).first()
    if pin is None:
        raise HTTPException(status_code=404, detail=f"Pin {pin_id} not found")
    db.delete(pin)
    db.commit()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/profile")
def read_profile(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_profile(db)


@router.put("/api/profile")
def write_profile(payload: ProfileSchema, db: Session = Depends(get_db)) -> dict[str, Any]:
    return save_profile(db, payload)


@router.get("/api/dealbreakers")
def read_dealbreakers(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_dealbreakers(db)


@router.put("/api/dealbreakers")
def write_dealbreakers(payload: DealbreakersSchema,
                       db: Session = Depends(get_db)) -> dict[str, Any]:
    return save_dealbreakers(db, payload)


@router.get("/api/pins", response_model=list[PinOut])
def read_pins(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return list_pins(db)


@router.post("/api/pins", response_model=PinOut, status_code=status.HTTP_201_CREATED)
def add_pin(payload: PinCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    return create_pin(db, payload)


@router.put("/api/pins/{pin_id}", response_model=PinOut)
def edit_pin(pin_id: int, payload: PinUpdate,
             db: Session = Depends(get_db)) -> dict[str, Any]:
    return update_pin(db, pin_id, payload)


@router.delete("/api/pins/{pin_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_pin(pin_id: int, db: Session = Depends(get_db)) -> None:
    delete_pin(db, pin_id)
    return None
