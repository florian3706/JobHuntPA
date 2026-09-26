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
    first = re.sub(r"[^\w\s,&'.-]", " ", first)  # emoji flags etc.
    first = re.sub(r"\s[-\u2013]\s*remote\b.*$|\bremote\b", " ", first, flags=re.I)
    first = re.sub(r"\((.*?)\)", "", first)
    parts = [p.strip() for p in first.split(",")]
    parts = [p for p in parts if p]  # "Sydney, , Australia"
    # A trailing ISO code after the country name ("Melbourne, Australia, AU")
    # only confuses the search; the country filter covers it.
    if len(parts) > 1 and re.fullmatch(r"[A-Z]{2,3}", parts[-1]) and parts[-1] not in _AU_STATES:
        parts = parts[:-1]
    return ", ".join(" ".join(p.split()) for p in parts)


_AU_STATES = {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"}


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
    params = {"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1}
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
    # The first hit can be an obscure landmark ("Melbourne Point, WA" for
    # "Melbourne, Australia"); the most important place is the one meant.
    hit = max(hits, key=lambda h: float(h.get("importance") or 0))
    # "Australia" or "Victoria" is not a place you can measure a commute to.
    if hit.get("addresstype") in ("country", "state", "region", "continent"):
        return None
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


_NUM = r"(\d|one|two|three|four|five)"
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_OFFICE = r"(?:(?:the|our|your)\s+)?(?:[A-Z][\w-]+\s+){0,2}(?:office|on-?site|onsite|studio|hub|workplace)"
_OFFICE_DAY_PATTERNS = [
    # "3 days in the office", "2-3 days per week in office", "minimum of 3 days onsite"
    re.compile(_NUM + r"(?:\s*(?:-|to|or)\s*" + _NUM + r")?\s*(?:days?|x)\s*(?:a|per|each|/)?\s*(?:week|wk)?\s*"
               r"(?:in|at|from|on)?\s*" + _OFFICE, re.I),
    # "in the office 3 days a week"
    re.compile(r"(?:in|at)\s+" + _OFFICE + r"\s+" + _NUM + r"(?:\s*(?:-|to)\s*" + _NUM + r")?\s*days?", re.I),
    # "3 office days", "2 onsite days"
    re.compile(_NUM + r"(?:\s*(?:-|to)\s*" + _NUM + r")?\s+(?:office|on-?site|onsite|in-office)\s+days?", re.I),
]
_HOME_DAYS = [
    re.compile(_NUM + r"\s*days?\s*(?:a|per)?\s*(?:week\s*)?(?:from\s+home|wfh|working\s+from\s+home|remote(?:ly)?)", re.I),
    re.compile(r"(?:work(?:ing)?\s+from\s+home|wfh|remote(?:ly)?)\s+" + _NUM + r"\s*days?", re.I),
]


def _to_int(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    v = v.lower()
    return _WORDS.get(v) or (int(v) if v.isdigit() else None)


def parse_office_days(*texts: str) -> Optional[int]:
    """Days per week in the office an ad asks for (the upper end of a range), or None."""
    blob = "\n".join(t for t in texts if t)[:6000]
    for rx in _OFFICE_DAY_PATTERNS:
        for m in rx.finditer(blob):
            nums = [n for n in (_to_int(g) for g in m.groups()) if n]
            if nums and 1 <= max(nums) <= 5:
                return max(nums)
    if _HYBRID.search(blob):
        for rx in _HOME_DAYS:
            m = rx.search(blob)
            home = _to_int(m.group(1)) if m else None
            if home and 1 <= home <= 4:
                return 5 - home
    return None


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
