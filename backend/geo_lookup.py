"""GET /api/geo/lookup?q=: "where is this place?" for the sign-up walkthrough's home suburb (accounts design 6.1).

It goes through backend/geo.geocode: the cached, rate-limited Nominatim client (one request a second, our honest
User-Agent) the rest of the app already uses to place jobs. Nothing is fetched from anywhere else.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from backend import geo
from backend.sessions import AuthUser, current_user

router = APIRouter(prefix="/api/geo", tags=["geo"])


@router.get("/lookup")
def lookup_place(q: str = Query("", max_length=200), _user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    """{found, label, lat, lng, kind}. ``found`` is false for text that isn't a place, for one that can't be
    measured (a whole country or state), and when the geocoder can't be reached right now."""
    text = " ".join((q or "").split())
    hit = geo.geocode(text) if len(text) >= 2 else None
    if not hit:
        return {"found": False}
    return {"found": True, "label": text, "lat": hit["lat"], "lng": hit["lng"], "kind": hit.get("type") or ""}
