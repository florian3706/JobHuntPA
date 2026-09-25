"""Location helpers: country guessing, geocoding (cached), distances, work mode."""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from typing import Any, Optional

import httpx

from backend.config import USER_AGENT
from backend.db import GeocodeCache, SessionLocal

log = logging.getLogger(__name__)

HOME_COUNTRY = "AU"
EARTH_RADIUS_KM = 6371.0088

# --------------------------------------------------------------------------
# Country guessing from free text (cheap, used before any geocoding)
# --------------------------------------------------------------------------

_AU_RE = re.compile(
    r"\b(australia|nsw|new south wales|vic|victoria|qld|queensland|tas|tasmania|act|"
    r"sydney|melbourne|brisbane|perth|adelaide|canberra|hobart|darwin|gold coast|"
    r"central coast|newcastle|wollongong|geelong|parramatta|north sydney|chatswood|"
    r"macquarie park|north ryde|gosford)\b",
    re.I,
)
_AU_STATE_SUFFIX_RE = re.compile(r",?\s\b(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\b(\s\d{4})?\s*$")

_COUNTRIES = {
    "united states": "US", "usa": "US", "u.s.": "US", "united kingdom": "GB", "uk": "GB",
    "england": "GB", "scotland": "GB", "ireland": "IE", "germany": "DE", "france": "FR",
    "spain": "ES", "italy": "IT", "netherlands": "NL", "portugal": "PT", "austria": "AT",
    "switzerland": "CH", "sweden": "SE", "denmark": "DK", "norway": "NO", "finland": "FI",
    "poland": "PL", "czechia": "CZ", "czech republic": "CZ", "canada": "CA", "mexico": "MX",
    "brazil": "BR", "argentina": "AR", "chile": "CL", "colombia": "CO", "india": "IN",
    "philippines": "PH", "singapore": "SG", "japan": "JP", "korea": "KR", "south korea": "KR",
    "china": "CN", "hong kong": "HK", "taiwan": "TW", "indonesia": "ID", "malaysia": "MY",
    "vietnam": "VN", "thailand": "TH", "new zealand": "NZ", "south africa": "ZA",
    "united arab emirates": "AE", "uae": "AE", "israel": "IL", "türkiye": "TR", "turkiye": "TR",
    "turkey": "TR", "greece": "GR", "belgium": "BE", "romania": "RO", "hungary": "HU",
    "ukraine": "UA", "nigeria": "NG", "kenya": "KE", "egypt": "EG", "saudi arabia": "SA",
    "pakistan": "PK", "bangladesh": "BD", "peru": "PE", "estonia": "EE", "lithuania": "LT",
    "latvia": "LV", "bulgaria": "BG", "croatia": "HR", "serbia": "RS", "slovakia": "SK",
    "luxembourg": "LU", "iceland": "IS", "sri lanka": "LK", "nepal": "NP", "qatar": "QA",
}
_COUNTRY_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(_COUNTRIES, key=len, reverse=True)) + r")\b", re.I)
_US_STATE_RE = re.compile(r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WV|WI|WY|DC)\b")


_CITIES = {
    "london": "GB", "manchester": "GB", "edinburgh": "GB", "dublin": "IE", "new york": "US",
    "san francisco": "US", "seattle": "US", "austin": "US", "boston": "US", "chicago": "US",
    "toronto": "CA", "vancouver": "CA", "auckland": "NZ", "wellington": "NZ", "berlin": "DE",
    "munich": "DE", "paris": "FR", "amsterdam": "NL", "madrid": "ES", "barcelona": "ES",
    "lisbon": "PT", "bangalore": "IN", "bengaluru": "IN", "manila": "PH", "makati": "PH",
    "tokyo": "JP", "seoul": "KR", "beijing": "CN", "shanghai": "CN", "istanbul": "TR",
}
_CITY_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(_CITIES, key=len, reverse=True)) + r")\b", re.I)
_ISO_SUFFIX_RE = re.compile(r",\s*([A-Z]{2})\s*$")


def guess_country(text: str) -> str:
    """ISO-2 country for a location string, or '' when unsure."""
    t = text or ""
    m = _COUNTRY_RE.search(t)
    if m:
        return _COUNTRIES[m.group(1).lower()]
    if _AU_RE.search(t) or _AU_STATE_SUFFIX_RE.search(t):
        return "AU"
    if _US_STATE_RE.search(t):
        return "US"
    m = _ISO_SUFFIX_RE.search(t)
    if m and m.group(1) in set(_COUNTRIES.values()) | {"AU"}:
        return m.group(1)
    m = _CITY_RE.search(t)
    if m:
        return _CITIES[m.group(1).lower()]
    return ""


def looks_like_location(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 80:
        return False
    return bool(guess_country(t)) or bool(re.search(r"\bremote\b", t, re.I))


# --------------------------------------------------------------------------
# Geocoding: Nominatim, max 1 request/second, results cached forever
# (https://operations.osmfoundation.org/policies/nominatim/)
# --------------------------------------------------------------------------

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_geo_lock = threading.Lock()
_geo_last = 0.0
_NOT_A_PLACE = re.compile(r"^(remote|anywhere|various|multiple locations?|global|worldwide)\b", re.I)


def _geocode_query(location_text: str) -> str:
    # "Sydney NSW; Melbourne VIC" -> geocode the first place only.
    first = re.split(r"[;|/]| or ", location_text or "")[0]
    first = re.sub(r"\((.*?)\)", "", first)
    return " ".join(first.split()).strip(" ,")


def geocode(location_text: str) -> Optional[dict[str, Any]]:
    """{lat, lng, country} for a location string (cached), or None."""
    query = _geocode_query(location_text)
    if not query or _NOT_A_PLACE.match(query):
        return None
    key = query.lower()
    db = SessionLocal()
    try:
        row = db.get(GeocodeCache, key)
        if row is not None:
            return None if row.lat is None else {"lat": row.lat, "lng": row.lng, "country": row.country or ""}
        result = _nominatim(query)
        if result is False:  # transient failure: don't cache
            return None
        row = GeocodeCache(query=key)
        if result:
            row.lat, row.lng, row.country, row.display_name = (
                result["lat"], result["lng"], result["country"], result["display_name"])
        db.add(row)
        db.commit()
        return None if not result else {"lat": result["lat"], "lng": result["lng"], "country": result["country"]}
    finally:
        db.close()


def _nominatim(query: str):
    global _geo_last
    params = {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1}
    guessed = guess_country(query)
    if guessed:
        params["countrycodes"] = guessed.lower()
    with _geo_lock:
        wait = _geo_last + 1.1 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _geo_last = time.monotonic()
        try:
            resp = httpx.get(_NOMINATIM, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            hits = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("geocode failed for %r: %s", query, exc)
            return False
    if not hits:
        return None
    hit = hits[0]
    return {
        "lat": float(hit["lat"]),
        "lng": float(hit["lon"]),
        "country": ((hit.get("address") or {}).get("country_code") or "").upper(),
        "display_name": hit.get("display_name") or "",
    }


# --------------------------------------------------------------------------
# Distances + pins
# --------------------------------------------------------------------------

def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def home_distance(lat: Optional[float], lng: Optional[float], pins: list[dict]) -> Optional[float]:
    """Distance to the nearest home pin (any pin if there is no home pin)."""
    if lat is None or lng is None or not pins:
        return None
    homes = [p for p in pins if p.get("kind") == "home"] or pins
    return round(min(distance_km(lat, lng, p["lat"], p["lng"]) for p in homes), 1)


def pins_in_range(lat: Optional[float], lng: Optional[float], pins: list[dict]) -> set[str]:
    """Kinds of the pins whose radius covers the point."""
    if lat is None or lng is None:
        return set()
    return {p["kind"] for p in pins if distance_km(lat, lng, p["lat"], p["lng"]) <= float(p.get("radius_km") or 0)}


# --------------------------------------------------------------------------
# Work mode from text (only when the source did not say)
# --------------------------------------------------------------------------

_REMOTE_STRONG = re.compile(
    r"\b(fully remote|100% remote|remote[- ]first|work from anywhere|remote role|remote position|"
    r"this (?:role|position) is remote|remote \(|remote,|remote -|remote within)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b|\b\d\s*days? (?:a week |per week )?in (?:the )?office\b", re.I)
_ONSITE = re.compile(r"\b(on-?site|office[- ]based|in[- ]office (?:role|position)|5 days in (?:the )?office)\b", re.I)


def classify_work_mode(title: str, location_text: str, description: str) -> str:
    head = f"{title}\n{location_text}"
    if re.search(r"\bremote\b", head, re.I):
        return "remote"
    body = (description or "")[:4000]
    if _HYBRID.search(head) or _HYBRID.search(body):
        return "hybrid"
    if _REMOTE_STRONG.search(body):
        return "remote"
    if _ONSITE.search(head) or _ONSITE.search(body):
        return "onsite"
    return "unknown"
