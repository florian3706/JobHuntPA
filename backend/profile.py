"""Search profile, dealbreakers and location pins (API + store helpers).

Endpoints:
    GET/PUT /api/profile        titles, keywords, salary floor, work modes, SEEK settings
    GET/PUT /api/dealbreakers   {industries[], keywords[]}
    GET/POST /api/pins, PUT/DELETE /api/pins/{id}
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.db import DealbreakerSet, Pin, UserProfile, get_db

router = APIRouter()

PinKind = Literal["home", "hybrid", "onsite"]
DEFAULT_PIN_RADIUS_KM = 25.0


class ProfileSchema(BaseModel):
    titles: list[str] = Field(default_factory=list)
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)
    salary_floor: Optional[int] = Field(default=None, ge=0)
    remote_aus_ok: bool = True
    remote_global_ok: bool = False
    allow_hybrid: bool = True
    allow_onsite: bool = True
    seek_enabled: bool = True
    seek_locations: list[str] = Field(default_factory=list)


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


def get_profile(db: Session) -> dict[str, Any]:
    row = db.get(UserProfile, 1)
    if row is None:
        return ProfileSchema().model_dump()
    return {
        "titles": list(row.titles or []),
        "keywords_include": list(row.keywords_include or []),
        "keywords_exclude": list(row.keywords_exclude or []),
        "salary_floor": row.salary_floor,
        "remote_aus_ok": bool(row.remote_aus_ok),
        "remote_global_ok": bool(row.remote_global_ok),
        "allow_hybrid": bool(row.allow_hybrid),
        "allow_onsite": bool(row.allow_onsite),
        "seek_enabled": bool(row.seek_enabled),
        "seek_locations": list(row.seek_locations or []),
    }


def save_profile(db: Session, data: ProfileSchema) -> dict[str, Any]:
    row = db.get(UserProfile, 1)
    if row is None:
        row = UserProfile(id=1)
        db.add(row)
    row.titles = _clean_tags(data.titles)
    row.keywords_include = _clean_tags(data.keywords_include)
    row.keywords_exclude = _clean_tags(data.keywords_exclude)
    row.salary_floor = data.salary_floor
    row.remote_aus_ok = data.remote_aus_ok
    row.remote_global_ok = data.remote_global_ok
    row.allow_hybrid = data.allow_hybrid
    row.allow_onsite = data.allow_onsite
    row.seek_enabled = data.seek_enabled
    row.seek_locations = _clean_tags(data.seek_locations)
    db.commit()
    return get_profile(db)


def get_dealbreakers(db: Session) -> dict[str, Any]:
    row = db.get(DealbreakerSet, 1)
    if row is None:
        return {"industries": [], "keywords": []}
    return {"industries": list(row.industries or []), "keywords": list(row.keywords or [])}


def save_dealbreakers(db: Session, data: DealbreakersSchema) -> dict[str, Any]:
    row = db.get(DealbreakerSet, 1)
    if row is None:
        row = DealbreakerSet(id=1)
        db.add(row)
    row.industries = _clean_tags(data.industries)
    row.keywords = _clean_tags(data.keywords)
    db.commit()
    return get_dealbreakers(db)


def _pin_to_dict(pin: Pin) -> dict[str, Any]:
    return {"id": pin.id, "label": pin.label, "kind": pin.kind,
            "lat": pin.lat, "lng": pin.lng, "radius_km": pin.radius_km}


def list_pins(db: Session) -> list[dict[str, Any]]:
    return [_pin_to_dict(p) for p in db.query(Pin).order_by(Pin.id).all()]


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
def write_dealbreakers(payload: DealbreakersSchema, db: Session = Depends(get_db)) -> dict[str, Any]:
    return save_dealbreakers(db, payload)


@router.get("/api/pins", response_model=list[PinOut])
def read_pins(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return list_pins(db)


@router.post("/api/pins", response_model=PinOut, status_code=status.HTTP_201_CREATED)
def add_pin(payload: PinCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    pin = Pin(label=payload.label.strip(), kind=payload.kind, lat=payload.lat,
              lng=payload.lng, radius_km=payload.radius_km)
    db.add(pin)
    db.commit()
    return _pin_to_dict(pin)


@router.put("/api/pins/{pin_id}", response_model=PinOut)
def edit_pin(pin_id: int, payload: PinUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    pin = db.get(Pin, pin_id)
    if pin is None:
        raise HTTPException(status_code=404, detail=f"Pin {pin_id} not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(pin, field, value.strip() if field == "label" else value)
    db.commit()
    return _pin_to_dict(pin)


@router.delete("/api/pins/{pin_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_pin(pin_id: int, db: Session = Depends(get_db)) -> None:
    pin = db.get(Pin, pin_id)
    if pin is None:
        raise HTTPException(status_code=404, detail=f"Pin {pin_id} not found")
    db.delete(pin)
    db.commit()
