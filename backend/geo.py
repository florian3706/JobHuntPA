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
    """{lat, lng, country, type} for a location string (cached), or None.
    ``type`` is Nominatim's addresstype ("suburb", "city", ...) when known."""
    query = _geocode_query(location_text)
    if not query or _NOT_A_PLACE.match(query):
        return None
    key = query.lower()
    db = SessionLocal()
    try:
        row = db.get(GeocodeCache, key)
        if row is not None:
            return None if row.lat is None else {"lat": row.lat, "lng": row.lng, "country": row.country or "",
                                                 "type": row.place_type}
        result = _lookup(query)
        if result is False:  # transient failure: don't cache
            return None
        row = GeocodeCache(query=key)
        if result:
            row.lat, row.lng, row.country, row.display_name, row.place_type = (
                result["lat"], result["lng"], result["country"], result["display_name"], result["type"])
        db.add(row)
        db.commit()
        return None if not result else {"lat": result["lat"], "lng": result["lng"], "country": result["country"],
                                        "type": result["type"]}
    finally:
        db.close()


_STATE_IN = re.compile(r"\b(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\b")


# SEEK's areas of a city, for ads that name no suburb: placed at the area's main centre.
# Areas not listed here are placed at the city's centre; either way the office is unknown.
SEEK_AREAS = {
    "sydney": {
        "cbd, inner west & eastern suburbs": "Sydney",
        "north shore & northern beaches": "Chatswood",
        "north west & hills district": "Castle Hill",
        "parramatta & western suburbs": "Parramatta",
        "ryde & macquarie park": "Macquarie Park",
        "south west & m5 corridor": "Liverpool",
        "southern suburbs & sutherland shire": "Hurstville",
    },
    "melbourne": {
        "cbd & inner suburbs": "Melbourne",
        "bayside & south eastern suburbs": "Moorabbin",
        "eastern suburbs": "Box Hill",
        "northern suburbs": "Preston",
        "western suburbs": "Footscray",
    },
}
_AREA_WORDS = re.compile(r"&|\b(suburbs|district|corridor|beaches|inner)\b", re.I)
_SEEK_LAST = re.compile(r"(.*?)\s*\b(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)", re.I)  # cached queries are lower case


def _seek_location(query: str) -> Optional[tuple[str, str, str]]:
    """SEEK's "Terrigal, Gosford & Central Coast NSW" as ("Terrigal", "Gosford & Central
    Coast", "NSW"); "North West & Hills District, Sydney NSW" as ("North West & Hills
    District", "Sydney", "NSW"); "Gosford & Central Coast NSW" as ("", "Gosford & Central
    Coast", "NSW"). None for other forms."""
    parts = [p.strip() for p in query.split(",") if p.strip()]
    last = _SEEK_LAST.fullmatch(parts[-1]) if parts else None
    if not last or not last.group(1):
        return None
    if len(parts) == 1:
        return ("", last.group(1), last.group(2).upper()) if "&" in last.group(1) else None
    return ", ".join(parts[:-1]), last.group(1), last.group(2).upper()


def city_area(query: str) -> Optional[tuple[str, str]]:
    """(city, area) for an ad that names only an area of a big city ("North West &
    Hills District, Sydney NSW"), so the office could be anywhere in it."""
    seek = _seek_location(query)
    if not seek or seek[1].lower() not in _METRO:
        return None
    place, city, _ = seek
    known = SEEK_AREAS.get(city.lower(), {})
    return (city, place) if place.lower() in known or _AREA_WORDS.search(place) else None


def _settlement(query: str):
    """Nominatim's answer only if it's a town, suburb or the like: not a building,
    road or council area that merely shares words with the query."""
    hit = _nominatim(query)
    return hit if not hit or hit["type"] in _SETTLEMENTS else None


def _lookup(query: str):
    """The place a cleaned location means, or None (False: the geocoder failed,
    try again later)."""
    seek = _seek_location(query)
    if seek:
        place, region, state = seek
        area = city_area(query)
        if area:  # "North West & Hills District, Sydney NSW": the area's centre, or the city's
            centre = SEEK_AREAS.get(region.lower(), {}).get(place.lower(), region)
            hit = _settlement(f"{centre}, {state}")
            if hit is not None:
                return hit
        elif region.lower() not in _METRO:
            # "Woy Woy, Gosford & Central Coast NSW": the region's words find the Central
            # Coast Highway, so look for the suburb (or the region's first town) in its state.
            hit = _settlement(f"{place or region.split('&')[0].strip()}, {state}")
            if hit is not None:
                return hit
    result = _nominatim(query)
    if result is None or (result and result["area"]):
        # "Hornsby, Sydney NSW" only finds Hornsby Shire, whose centre is
        # 13 km from Hornsby; "Hornsby, NSW" finds the suburb itself.
        alt = _without_city(query)
        better = _settlement(alt) if alt else None
        if better is False:
            return False
        if better:
            result = better
    return result


def _without_city(query: str) -> str:
    """"Hornsby, Sydney NSW" -> "Hornsby, NSW" ('' if there's no city part)."""
    parts = [p.strip() for p in query.split(",") if p.strip()]
    state = _STATE_IN.search(parts[-1]) if len(parts) >= 2 else None
    return f"{parts[0]}, {state.group(1)}" if state else ""


# Places a commute can be measured to; plus buildings and addresses (rank 26+).
_SETTLEMENTS = {"city", "town", "suburb", "village", "hamlet", "neighbourhood", "quarter", "city_district",
                "borough", "locality", "isolated_dwelling"}
_TOO_BROAD = {"country", "state", "region", "continent"}


def _pick(hits: list[dict]) -> Optional[dict]:
    """The place meant: a town, suburb or address before a council area named
    after it, then the most important one ("Melbourne, Australia" is the city,
    not Melbourne Point, WA). "Australia" or "Victoria" is not a place you can
    measure a commute to."""
    hits = [h for h in hits if h.get("addresstype") not in _TOO_BROAD]
    precise = [h for h in hits if h.get("addresstype") in _SETTLEMENTS or int(h.get("place_rank") or 0) >= 26]
    pool = precise or hits
    return max(pool, key=lambda h: float(h.get("importance") or 0)) if pool else None


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
    hit = _pick(hits or [])
    if hit is None:
        return None
    kind = hit.get("addresstype") or ""
    return {
        "lat": float(hit["lat"]),
        "lng": float(hit["lon"]),
        "country": ((hit.get("address") or {}).get("country_code") or "").upper(),
        "display_name": hit.get("display_name") or "",
        "type": kind,
        # A council or district area rather than a place in it.
        "area": kind not in _SETTLEMENTS and int(hit.get("place_rank") or 30) < 26,
    }


_AREA_NAME = re.compile(r"^(the )?(council|city|municipality|shire|district)\b.*\bof\b|\b(council|shire)$", re.I)
_NAMES_AREA = re.compile(r"\b(council|shire|city of|municipality|district of)\b", re.I)


def purge_seek_geocodes() -> set[str]:
    """Forget cached results for SEEK-style locations that found nothing, or found
    something else that shares words with them ("Woy Woy, Gosford & Central Coast
    NSW" was a bus stop on the Central Coast Highway), from before such places were
    looked up by suburb. Returns the dropped queries so their jobs can be placed again."""
    db = SessionLocal()
    try:
        dropped = set()
        for r in db.query(GeocodeCache).all():
            seek = _seek_location(r.query)
            if not seek:
                continue
            place = (seek[0] or seek[1].split("&")[0]).strip().lower()
            if r.lat is None or not (r.display_name or "").lower().startswith(place) or city_area(r.query):
                dropped.add(r.query)
        if dropped:
            db.query(GeocodeCache).filter(GeocodeCache.query.in_(dropped)).delete(synchronize_session=False)
            db.commit()
        return dropped
    finally:
        db.close()


def purge_area_geocodes() -> set[str]:
    """Forget cached results that are council areas (stored before suburbs
    were preferred), unless the query itself asked for the council. Returns
    the dropped queries so their jobs can be placed again."""
    db = SessionLocal()
    try:
        rows = db.query(GeocodeCache).filter(GeocodeCache.display_name.isnot(None)).all()
        dropped = {r.query for r in rows
                   if _AREA_NAME.search(r.display_name.split(",")[0].strip()) and not _NAMES_AREA.search(r.query)}
        if dropped:
            db.query(GeocodeCache).filter(GeocodeCache.query.in_(dropped)).delete(synchronize_session=False)
            db.commit()
        return dropped
    finally:
        db.close()


# Big cities: an ad that only names one of these doesn't say where the office is.
_METRO = {"sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra", "hobart", "darwin", "gold coast",
          "newcastle", "wollongong", "central coast", "sunshine coast", "geelong", "greater sydney",
          "greater melbourne", "greater brisbane", "auckland", "wellington", "london", "singapore"}
_LOCATION_NOISE = re.compile(
    r"\b(nsw|vic|qld|wa|sa|tas|act|nt|new south wales|victoria|queensland|western australia|south australia|"
    r"tasmania|australian capital territory|northern territory|australia|au|metro|metropolitan|region|area|"
    r"hybrid|on-?site|in-?office|in-person|office|based)\b|[^a-z ]", re.I)


def is_city_only(location_text: str) -> bool:
    """True when the location names only a city ("Sydney NSW", "Australia -
    Sydney - New South Wales"), several cities, or an area of a city ("North
    West & Hills District, Sydney NSW"), so the office could be anywhere in it.
    "Sydney CBD" or a suburb is specific enough."""
    found = False
    for text in re.split(r"\s*;\s*", location_text or ""):
        words = " ".join(_LOCATION_NOISE.sub(" ", text.lower()).split())
        if not words or set(words.split()) == {"remote"}:  # "Remote - Remote" alongside a city
            continue
        if not ({words, " ".join(dict.fromkeys(words.split()))} & _METRO or city_area(_geocode_query(text))):
            return False  # ("sydney sydney" -> "sydney")
        found = True
    return found


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
