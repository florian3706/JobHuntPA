"""Signs that a job ad may not be a real, current vacancy (docs/design/ghost-jobs.md).

An ad can't be proved to be a "ghost job", so this only reports signs, each with a plain reason, and a level:
``likely`` (strong evidence), ``possible``, or None. It never hides, scores or down-ranks a job.

A signal is ``{code, weight, text, detail?}``. The pure functions here turn facts into signals; the rest reads and
stores them:

- Worked out from the ad's own text when a posting changes (``stamp``, called by ``pool``): ``evergreen_wording``,
  ``recruiter_no_employer``. Stored on the posting as ``ghost_json["text"]``.
- Worked out from the postings around one (``refresh``, at the end of a search run): ``reposted``, ``many_similar``.
  Plaintext postings only (public ads), indexed lookups, in batches. Stored as ``ghost_json["pool"]``.
- Worked out when the job is read (``assess``): ``long_open``, ``date_refreshed``, ``company_news_cuts``, and the
  LLM's ``ad_signs`` (stored by the scorer with ``pool.share_ad_fact``).

Nothing here filters, sorts or groups on a sealed column. A job's own text (a private upload, or a private job) is
read in Python after the batch load in ``assess_jobs``.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, NamedTuple, Optional

from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from backend import duplicates
from backend.db import CompanyProfile, Job, Posting, company_key
from backend.scraping.text import description_hash

# --------------------------------------------------------------------------
# Thresholds and weights (the reasons are in docs/design/ghost-jobs.md)
# --------------------------------------------------------------------------

LONG_OPEN_DAYS = ((90, 5), (60, 3), (45, 1))   # (days open, weight): the first row that fits
STILL_LISTED = timedelta(days=14)               # "still listed": shown by a listing this recently
REFRESHED_AFTER = timedelta(days=21)            # posted date this long after the pool first saw the ad
REPOST_WINDOW = timedelta(days=180)             # earlier ads of the role count if they ended within this long
REPOST_GAP = timedelta(days=1)                  # an earlier ad ended at least this long before: not the same ad on two boards
REOPENED_AFTER = timedelta(days=2)              # listed_since this long after first_seen: it closed and came back
MANY_SIMILAR = 5                                # open ads with one title at one company
MIN_HASH_CHARS = 400                            # a description this short isn't evidence that two ads are one
RECHECK_AFTER = timedelta(days=1)               # the pool half is worked out again this often
MAX_REFRESH = 3000                              # postings worked out per run (the rest follow next run)
NEWS_WINDOW = timedelta(days=365)
HEAD_CHARS, SHORT_AD_CHARS = 600, 1500          # wording counts in full in the start of an ad, or in a short one
LIKELY_WEIGHT = 5                               # one signal this strong makes "likely"
LIKELY_TOTAL, LIKELY_SIGNALS = 6, 2             # ... or this much from at least this many moderate signals
POSSIBLE_TOTAL = 3
MAX_AD_PHRASES, MAX_PHRASE_CHARS = 4, 100

ORDER = ("evergreen_wording", "long_open", "reposted", "ad_signs", "company_news_cuts", "date_refreshed",
         "many_similar", "recruiter_no_employer")
EMPTY_HASH = description_hash("")


def signal(code: str, weight: int, text: str, detail: Optional[str] = None) -> dict:
    out = {"code": code, "weight": weight, "text": text}
    if detail:
        out["detail"] = detail
    return out


def level_of(signals: Iterable[dict]) -> Optional[str]:
    """"likely" for one strong signal, or enough from several moderate ones (weak signs alone never reach it),
    "possible" for a little more than one weak sign, else None."""
    weights = [s["weight"] for s in signals]
    total = sum(weights)
    if any(w >= LIKELY_WEIGHT for w in weights):
        return "likely"
    if total >= LIKELY_TOTAL and sum(1 for w in weights if w >= 2) >= LIKELY_SIGNALS:
        return "likely"
    return "possible" if total >= POSSIBLE_TOTAL else None


def strength_of(weight: int) -> str:
    """A sign's weight in words, for the job page (the weights themselves stay internal)."""
    return "strong" if weight >= LIKELY_WEIGHT else "moderate" if weight >= 2 else "weak"


def ordered(signals: Iterable[dict]) -> list[dict]:
    """Strongest first, then in a fixed order."""
    signals = list(signals)
    if len(signals) < 2:
        return signals
    return sorted(signals, key=lambda s: (-s["weight"], ORDER.index(s["code"]) if s["code"] in ORDER else len(ORDER)))


# --------------------------------------------------------------------------
# Wording in the ad
# --------------------------------------------------------------------------

def _phrases(*patterns: str) -> re.Pattern:
    """A case-insensitive, word-bounded alternation. A space matches any run of spaces or hyphens."""
    return re.compile(r"\b(?:" + "|".join(p.replace(" ", r"[\s\-]+") for p in patterns) + r")\b", re.I)


# Wording that says the ad is not for one current vacancy. Strong: it says so outright. Ambiguous: also the duties of a
# recruiter, HR or sales ad ("build a pipeline of candidates") and normal wording for pooled teaching and nursing roles
# ("ongoing recruitment"). Moderate: also found in real ads ("lodge an expression of interest" for an acting role,
# "we'll keep your details on file"). Left out because real ads use them: "rolling basis", "ongoing role/opportunity"
# (permanent), "talented people", "future needs", a bare "always looking".
STRONG = _phrases(
    "talent community", "talent network", "talent bench",
    r"general applications?(?![\s\-]+(?:development|developer|support|software|design|engineer\w*|architect\w*|"
    r"testing|security|maintenance|programming))",
    r"evergreen (?:role|position|requisition|vacancy|advert|ad|posting)", "not a current vacancy",
    r"no current vacanc(?:y|ies)",
    r"not (?:currently )?(?:an )?(?:active|open|actual|advertised) (?:vacancy|role|position|opening)",
    r"not currently (?:hiring|recruiting) for (?:a|this) (?:specific|particular)")
AMBIGUOUS = _phrases(
    "talent pool", "talent pipeline", r"pipeline of (?:\w+ ){0,2}(?:candidates|applicants|talent\w*|people|professionals)",
    "ongoing recruitment")
MODERATE = _phrases(
    r"register(?:ing)? (?:your |an )?interest", "registrations? of interest",
    r"future (?:opportunit(?:y|ies)|vacanc(?:y|ies)|openings?|roles?|positions?) (?:that|which) may (?:arise|become available)",
    r"should (?:they|any) arise", r"always (?:hiring|recruiting)", "ongoing hiring",
    r"continuous(?:ly)? (?:hiring|recruiting|recruitment)", r"year round (?:hiring|recruiting|recruitment)",
    r"keep (?:your )?(?:cv|resume|details) on file")
# "Expression of interest" is the usual title of a real acting or secondment role in the public service.
INTEREST = _phrases("expressions? of interest")
ACTING = re.compile(r"\bacting\b|higher duties|second(?:ed|ment)|closing date|applications? clos(?:e|es|ing)\b", re.I)
# A recruiter, HR or sales ad names these as its duties, so the ambiguous phrases say nothing there.
DUTIES_TITLE = re.compile(r"recruit|talent|sourc|\bHR\b|human resources|sales|business development", re.I)
APPLY_CUE = re.compile(r"\bapply\b|\bsubmit\b|\bregister\b|\bjoin\b|not hiring for a specific|not for a specific role", re.I)
APPLY_NEAR = 80                                 # an apply cue this close to an ambiguous phrase makes it a call for applications
# A recruiter's ad that doesn't name who the job is with.
NO_EMPLOYER = _phrases(
    r"(?:our|my) clients?", "client of ours", r"(?:confidential|undisclosed) (?:client|company|employer|organi[sz]ation|business)")


def _best_hit(pattern: re.Pattern, title: str, text: str, in_title: int, in_head: int, in_tail: int) -> tuple[int, str]:
    """(weight, phrase) of the strongest hit: in the title, in the start of the ad (or anywhere in a short one), or
    only further down a long one (usually a footer: "join our talent community")."""
    best = (0, "")
    short = len(text) < SHORT_AD_CHARS
    hits = [(m, in_title) for m in pattern.finditer(title)]
    hits += [(m, in_head if short or m.start() < HEAD_CHARS else in_tail) for m in pattern.finditer(text)]
    for m, weight in hits:
        if weight > best[0]:
            best = (weight, " ".join(m.group(0).lower().replace("-", " ").split()))
    return best


def _ambiguous_hit(title: str, text: str) -> tuple[int, str]:
    """(weight, phrase) for AMBIGUOUS wording: nothing in a recruiter, HR or sales ad (it is the job's duties); 5 in the
    title, or in the body next to a cue to apply ("join our talent pool"); else 3, or 1 in the footer of a long ad."""
    if DUTIES_TITLE.search(AMBIGUOUS.sub(" ", title)):  # (without the phrase itself: "Talent Pool" is no recruiter's title)
        return 0, ""
    weight, phrase = _best_hit(AMBIGUOUS, title, text, 5, 3, 1)
    if weight == 3 and any(APPLY_CUE.search(text[max(0, m.start() - APPLY_NEAR):m.end() + APPLY_NEAR])
                           for m in AMBIGUOUS.finditer(text) if len(text) < SHORT_AD_CHARS or m.start() < HEAD_CHARS):
        weight = 5
    return weight, phrase


def text_signals(title: str, description: str) -> list[dict]:
    """Signals from the ad's own words. ``recruiter_no_employer`` is stored with ``when: "recruiter"``: it counts
    only if the company turns out to be a recruiter (``assess``), which depends on the company research."""
    title, text = title or "", description or ""
    out = []
    moderate = _best_hit(MODERATE, title, text, 2, 2, 1)
    if not ACTING.search(f"{title} {text}"):
        moderate = max(moderate, _best_hit(INTEREST, title, text, 2, 2, 1))
    weight, phrase = max(_best_hit(STRONG, title, text, 5, 5, 1), _ambiguous_hit(title, text), moderate)
    if weight:
        out.append(signal("evergreen_wording", weight, f'Mentions "{phrase}"',
                          "Ads like this are sometimes used to collect applications for future roles, "
                          "not to fill a current one."))
    if NO_EMPLOYER.search(text):
        item = signal("recruiter_no_employer", 1, "Recruiter ad: the employer may not be named",
                      "Ask the recruiter who the employer is and whether the role is open now.")
        item["when"] = "recruiter"
        out.append(item)
    return out


ACCUSING = re.compile(r"scam|fake|fraud|ghost|bogus|phishing", re.I)
_WORD = re.compile(r"[a-z0-9']+")
_SMALL_WORDS = frozenset("the and for with that this are was were has have not any all but you your our can will from "
                         "into out its their they them who what when where which how may also than then".split())
QUOTED_SHARE = 0.8                              # of a phrase's content words that must be in the ad it is said to quote


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _SMALL_WORDS]


def clean_phrases(value: Any, description: Optional[str] = None) -> list[str]:
    """The LLM's ``ghost_signs`` as a short list of plain phrases (anything else is dropped), without an accusation
    (the ad is untrusted text and the phrases are shown to everyone who sees the posting). With ``description``,
    only phrases made of the ad's own words are kept: not a phrase from the candidate's documents."""
    if not isinstance(value, list):
        return []
    ad_words = set(_words(description)) if description is not None else None
    out: list[str] = []
    for item in value:
        phrase = " ".join(item.split()).strip(" .;") if isinstance(item, str) else ""
        if not phrase or ACCUSING.search(phrase):
            continue
        if ad_words is not None:
            words = _words(phrase)
            if not words or sum(1 for w in words if w in ad_words) < QUOTED_SHARE * len(words):
                continue
        if phrase.lower() not in (o.lower() for o in out):
            out.append(phrase[:MAX_PHRASE_CHARS].rstrip())
    return out[:MAX_AD_PHRASES]


def ad_signs_signal(raw: Optional[str]) -> Optional[dict]:
    """From the stored JSON list of what an LLM read in the ad. Weak alone (one phrase is not even "possible") and
    never "likely": an LLM can be wrong."""
    try:
        phrases = clean_phrases(json.loads(raw)) if raw else []
    except ValueError:
        return None
    if not phrases:
        return None
    return signal("ad_signs", 2 if len(phrases) == 1 else 3, "AI read of the ad: " + "; ".join(phrases[:2]),
                  "Read from the ad by the AI: " + "; ".join(phrases) + ". It can be wrong.")


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y")
_SLASH_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
# Sources whose posted_at is the ad's last-edit date, not its posting date: it moves on every edit.
UPDATED_DATE_SOURCES = frozenset({"greenhouse", "atlassian"})


@lru_cache(maxsize=4096)
def parse_posted(value: Optional[str]) -> Optional[datetime]:
    """The ad's posted date as a naive UTC datetime, when it is an absolute date (ISO, common written forms, an RFC 822
    date as in an RSS feed). A slash date is read only when it can't be day/month or month/day ("10/01/2026" could
    be either, and a wrong guess makes an ad look months old). "3 days ago" and other free text give None."""
    raw = " ".join((value or "").split())
    if not raw:
        return None
    parsed = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = (parsed - parsed.utcoffset()).replace(tzinfo=None)
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(raw[:40], fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            parsed = _slash_date(raw[:40]) or _rfc822_date(raw[:60])
    return parsed if parsed is not None and parsed.year >= 2000 else None


def _slash_date(raw: str) -> Optional[datetime]:
    m = _SLASH_DATE.fullmatch(raw)
    if not m:
        return None
    first, second, year = (int(g) for g in m.groups())
    if first <= 12 and second <= 12 and first != second:
        return None  # day first or month first: can't tell
    day, month = (first, second) if first > 12 or first == second else (second, first)
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _rfc822_date(raw: str) -> Optional[datetime]:
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return when if when.tzinfo is None else (when - when.utcoffset()).replace(tzinfo=None)


def _day(when: datetime, now: datetime) -> str:
    return f"{when.day} {when:%B}" + ("" if when.year == now.year else f" {when.year}")


def reopened(first_seen: Optional[datetime], listed_since: Optional[datetime]) -> bool:
    return bool(first_seen and listed_since and listed_since - first_seen >= REOPENED_AFTER)


def edit_date_source(source: Optional[str]) -> bool:
    return (source or "").partition(":")[0] in UPDATED_DATE_SOURCES


def listing_dates(first_seen: Optional[datetime], listed_since: Optional[datetime], posted_at: Optional[str],
                  now: datetime, source: Optional[str] = None) -> tuple:
    """(start, posted, began): when the current run of listing started for the pool, the ad's own posted date when it
    can be read as one (None for a source that stores the last-edit date, or a date in the future), and the day the
    listing began (``time_signals`` says how the two combine). All None without a start."""
    start = listed_since or first_seen
    if start is None:
        return None, None, None
    posted = None if edit_date_source(source) else parse_posted(posted_at)
    if posted is not None and posted > now + timedelta(days=2):
        posted = None  # a date in the future isn't a posted date
    began = start if posted is None or reopened(first_seen, listed_since) else min(start, posted)
    return start, posted, began


def still_listed(last_listed: Optional[datetime], now: datetime) -> bool:
    return last_listed is None or last_listed >= now - STILL_LISTED


def time_signals(first_seen: Optional[datetime], listed_since: Optional[datetime], last_listed: Optional[datetime],
                 closed: bool, posted_at: Optional[str], now: datetime, source: Optional[str] = None) -> list[dict]:
    """``long_open`` and ``date_refreshed`` from the posting's dates. The listing's start is when the pool first saw
    the ad (or when it came back after closing); a posted date earlier than that moves the start back, unless the ad
    closed and came back (it wasn't open all that time). A source that stores the ad's last-edit date as its posted
    date (``UPDATED_DATE_SOURCES``) says nothing about when it was posted: that date is not read."""
    start, posted, began = listing_dates(first_seen, listed_since, posted_at, now, source)
    if start is None:
        return []
    out = []
    if not closed and still_listed(last_listed, now):
        days = (now - began).days
        for threshold, weight in LONG_OPEN_DAYS:
            if days >= threshold:
                out.append(signal("long_open", weight, f"Open for {days} days",
                                  "Most ads are filled or taken down within a month or two."
                                  + (" SEEK ads run 30 days unless they are renewed." if source == "seek" else "")))
                break
    if posted is not None and posted - start >= REFRESHED_AFTER:
        out.append(signal("date_refreshed", 1, f"Posted date renewed (first listed {_day(start, now)})",
                          "The ad's date was moved forward while it stayed listed. Renewing an ad is common, "
                          "and it also makes an old ad look new."))
    return out


# --------------------------------------------------------------------------
# Company news
# --------------------------------------------------------------------------

CUTS = re.compile(
    r"\b(?:lay[\s-]?offs?|laid[\s-]off|laying[\s-]off|redundanc(?:y|ies)|retrench\w*|job cuts?|hiring freeze|"
    r"recruitment freeze|headcount reduc\w*|downsiz\w*|cut(?:s|ting)? (?:\d[\d,]*\s+|\w+\s+){0,3}"
    r"(?:jobs|roles|positions|staff|workers|employees)|"
    r"(?:axe[sd]?|reduc\w+) (?:\d[\d,]*\s+|\w+\s+){0,2}(?:jobs|roles|positions|staff|workers|employees|workforce))\b",
    re.I)


def news_signal(profile: Optional[CompanyProfile], now: datetime) -> Optional[dict]:
    """Layoffs, redundancies, job cuts or a hiring freeze in the company's researched news from the last 12 months.
    Only the news items are read (their title and summary), never the one-line note."""
    if profile is None or profile.status != "done":
        return None
    try:
        items = json.loads(profile.controversies_json or "[]")
    except ValueError:
        return None
    researched_recently = bool(profile.news_researched_at and profile.news_researched_at >= now - NEWS_WINDOW)
    oldest_year = (now - NEWS_WINDOW).year
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        body = f"{item.get('title') or ''} {item.get('summary') or ''}"
        if not CUTS.search(body):
            continue
        year = re.search(r"\b(19|20)\d{2}\b", str(item.get("year") or ""))
        if (year and int(year.group(0)) < oldest_year) or (not year and not researched_recently):
            continue
        title = " ".join(str(item.get("title") or "").split())[:120]
        return signal("company_news_cuts", 2, "Recent news about job cuts at this company",
                      f"From the company research: {title}" if title else None)
    return None


# --------------------------------------------------------------------------
# The pool around a posting
# --------------------------------------------------------------------------

def company_norm(name: str) -> str:
    """The employer's name without legal suffixes and punctuation ("Acme Pty Ltd" and "ACME" match)."""
    return " ".join(w for w in duplicates._words(name) if w not in duplicates._COMPANY_NOISE)


def pool_key(company: Optional[str], title: Optional[str], location: Optional[str] = None) -> str:
    """Normalised "company|title words" for finding earlier ads of one role ("" when it can't be). The title keeps
    its seniority and drops contract terms, durations and a city appended to it, and its word order."""
    who = company_norm(company or "")
    words = sorted(duplicates.title_words(title or "", location or ""))
    return f"{who}|{' '.join(words)}"[:200] if who and words else ""


def specific(key: str) -> bool:
    """A key with a title of at least two words: "Nurse" at a hospital says nothing, "Nurse Unit Manager" does."""
    return " " in key.partition("|")[2]


class Row(NamedTuple):
    """What the pool signals need from a posting (no description: its length and hash stand in for it)."""
    id: int
    key: str
    hash: str
    area: frozenset
    first_seen: datetime
    listed_since: datetime
    last_listed: datetime
    closed: bool
    length: int
    source: str = ""            # where the pool found it ("seek", "greenhouse:acme"); "" when not known
    reopened_from: str = ""     # why the run before the current one ended ("gone", "unlisted", "stale"), when it came back


def _row(r: tuple) -> Row:
    pid, key, dhash, where, first, since, last, closed, length, source, came_back = r
    first = first or datetime.utcnow()
    return Row(pid, key or "", dhash if dhash and dhash != EMPTY_HASH else "", frozenset(duplicates.place_words(where or "")),
               first, since or first, last or first, closed is not None, length or 0, source or "", came_back or "")


def _same_role(me: Row, other: Row) -> bool:
    by_key = bool(me.key) and other.key == me.key and specific(me.key)
    by_text = bool(me.hash) and other.hash == me.hash and min(me.length, other.length) >= MIN_HASH_CHARS
    return (by_key or by_text) and (not me.area or not other.area or bool(me.area & other.area))


def _other_board(me: Row, other: Row) -> bool:
    """Found by another source: the pool saw it there when it crawled it, which says nothing about when it was posted."""
    return bool(me.source and other.source and me.source != other.source)


def pool_signals(me: Row, others: Iterable[Row], now: datetime) -> list[dict]:
    """``reposted`` and ``many_similar`` for one posting from the other postings around it.

    Reposted: the role (same employer and title, or the same description, in the same place) was listed before and
    had ended by the time this ad appeared. An ad that is still listed (the same ad on two boards, or in two
    places) isn't an earlier appearance, and earlier ads that overlapped count as one. Nor is a copy found by another
    source: first_seen is when the pool crawled it, not when the employer posted it. An ad that closed and came back
    counts as one earlier appearance of itself, unless it was only dropped for not being listed for a while ("stale":
    the search changed)."""
    others = [o for o in others if o.id != me.id]
    spans = [(o.first_seen, o.last_listed) for o in others
             if _same_role(me, o) and not _other_board(me, o) and o.last_listed <= me.first_seen - REPOST_GAP
             and o.last_listed >= now - REPOST_WINDOW]
    if reopened(me.first_seen, me.listed_since) and me.reopened_from in ("gone", "unlisted"):
        spans.append((me.first_seen, me.first_seen))
    out = []
    if spans:
        spans.sort()
        merged = [list(spans[0])]
        for start, end in spans[1:]:
            if start <= merged[-1][1] + REPOST_GAP:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        times = len(merged) + 1
        out.append(signal("reposted", 5 if times >= 3 else 1, f"Posted {times} times since {_day(merged[0][0], now)}",
                          "The same role from this employer was listed before and ended, then listed again. "
                          "Roles that are hard to fill are sometimes re-listed, so it may still be real."))
    if me.key and specific(me.key) and not me.closed:
        open_now = 1 + sum(1 for o in others if o.key == me.key and not o.closed and o.last_listed >= now - STILL_LISTED)
        if open_now >= MANY_SIMILAR:
            out.append(signal("many_similar", 1, f"{open_now} open ads with this title at this company",
                              "Large employers often hire many people into one role, so this is a weak sign."))
    return out


_COLUMNS = (Posting.id, Posting.repost_key, Posting.description_hash, Posting.location_text, Posting.first_seen,
            Posting.listed_since, Posting.last_listed_at, Posting.closed_at, func.length(Posting.description),
            Posting.source, Posting.reopened_from)


def _chunks(items: list, size: int = 400) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _neighbours(db: Session, rows: list[Row]) -> dict[int, list[Row]]:
    """{posting id: the other postings that share its key or its description hash}: two indexed lookups per 400
    keys, and no descriptions loaded."""
    keys = sorted({r.key for r in rows if r.key and specific(r.key)})
    hashes = sorted({r.hash for r in rows if r.hash})
    by_key: dict[str, list[Row]] = {}
    by_hash: dict[str, list[Row]] = {}
    for chunk in _chunks(keys):
        for r in db.query(*_COLUMNS).filter(Posting.repost_key.in_(chunk), Posting.not_a_job.is_(False)):
            by_key.setdefault(r[1], []).append(_row(r))
    for chunk in _chunks(hashes):
        for r in db.query(*_COLUMNS).filter(Posting.description_hash.in_(chunk), Posting.not_a_job.is_(False)):
            by_hash.setdefault(r[2], []).append(_row(r))
    out = {}
    for r in rows:
        around = {o.id: o for o in by_key.get(r.key, []) + by_hash.get(r.hash, [])}
        out[r.id] = list(around.values())  # (one that shares both is one)
    return out


EMPTY_STORED = json.dumps({"text": [], "pool": []})


def _stored(raw: Optional[str]) -> dict:
    if not raw or raw == EMPTY_STORED:  # (most ads: the job list reads thousands)
        return {}
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def stamp(posting: Posting) -> None:
    """Worked out from the posting's own content, when it changes (``pool._changed``): the key that finds earlier
    ads of the role, and the signs in its wording. Keeps the pool half."""
    key = pool_key(posting.company, posting.title, posting.location_text)
    if key != posting.repost_key:
        posting.repost_key, posting.ghost_checked_at = key, None  # a different role: its pool half is worked out again
    data = _stored(posting.ghost_json)
    data["text"] = text_signals(posting.title or "", posting.description or "")
    data.setdefault("pool", [])
    posting.ghost_json = json.dumps(data)


def _backfill(db: Session) -> int:
    """Postings from before this feature (or never stamped): key and wording, in batches of 500, closed ones too
    (they are the history). Done once."""
    done = 0
    while True:
        batch = (db.query(Posting.id, Posting.company, Posting.title, Posting.location_text, Posting.description)
                 .filter(Posting.ghost_json.is_(None)).limit(500).all())
        if not batch:
            return done
        db.execute(update(Posting), [
            {"id": pid, "repost_key": pool_key(company, title, where),
             "ghost_json": json.dumps({"text": text_signals(title or "", description or ""), "pool": []})}
            for pid, company, title, where, description in batch])
        db.flush()
        done += len(batch)


def refresh(db: Session, now: datetime, limit: int = MAX_REFRESH) -> int:
    """Work out the pool half (``reposted``, ``many_similar``) of the open postings that are new or were last worked
    out over RECHECK_AFTER ago, at most ``limit`` (oldest first). Called at the end of a search run. Returns how
    many it did."""
    db.flush()  # (the session doesn't autoflush) this run's postings are in the table
    _backfill(db)
    due = (db.query(*_COLUMNS).filter(Posting.closed_at.is_(None), Posting.not_a_job.is_(False),
                                      or_(Posting.ghost_checked_at.is_(None),
                                          Posting.ghost_checked_at < now - RECHECK_AFTER))
           .order_by(Posting.ghost_checked_at, Posting.id).limit(limit).all())
    rows = [_row(r) for r in due]
    neighbours = _neighbours(db, rows)
    stored: dict[int, Optional[str]] = {}
    for chunk in _chunks([r.id for r in rows]):
        stored.update(db.query(Posting.id, Posting.ghost_json).filter(Posting.id.in_(chunk)))
    updates = []
    for r in rows:
        data = _stored(stored.get(r.id))
        data["pool"] = pool_signals(r, neighbours[r.id], now)
        data.setdefault("text", [])
        updates.append({"id": r.id, "ghost_json": json.dumps(data), "ghost_checked_at": now})
    for chunk in _chunks(updates, 500):
        db.execute(update(Posting), chunk)
    return len(updates)


# --------------------------------------------------------------------------
# A job
# --------------------------------------------------------------------------

NONE = {"level": None, "signals": []}

# Every kind of sign, in the order the job page lists them, with what the app looks for.
CHECKS = (
    ("evergreen_wording", 'Wording that collects applications for future roles, such as "talent pool" or "register your interest"'),
    ("ad_signs", "Wording the AI read in the ad that suggests it is not a current vacancy"),
    ("long_open", f"An ad still listed after {LONG_OPEN_DAYS[-1][0]} days or more"),
    ("reposted", "The same role listed before, ended, and listed again"),
    ("date_refreshed", "A posted date moved forward while the ad stayed listed"),
    ("many_similar", f"{MANY_SIMILAR} or more open ads with this title at this company"),
    ("recruiter_no_employer", "A recruiter's ad that may not name the employer"),
    ("company_news_cuts", "News of job cuts or a hiring freeze at the company in the last 12 months"),
)
NO_HISTORY = "This job did not come from a search, so there is no listing history to check."
NOT_YET = "Worked out at the end of the next search."


def _checks(job: Job, profiles: dict[str, CompanyProfile], found: set, ad_raw: Optional[str], now: datetime) -> list[dict]:
    """For the job page: each kind of sign as ``{code, label, state, note?}``. ``state`` is "found" (it is in the
    signals), "clear" (looked for, not there) or "unknown" (it could not be checked, and the note says why): a green
    rating means less when half the checks could not be made."""
    posting = job.posting
    unknown: dict[str, str] = {}
    notes: dict[str, str] = {}
    if posting is None:
        unknown.update({code: NO_HISTORY for code in ("long_open", "reposted", "date_refreshed", "many_similar")})
    else:
        start, posted, began = listing_dates(posting.first_seen, posting.listed_since, posting.posted_at, now, posting.source)
        if start is None:
            unknown.update({"long_open": NOT_YET, "date_refreshed": NOT_YET})
        elif posting.closed_at is not None:
            notes["long_open"] = "The ad is closed."
        elif not still_listed(posting.last_listed_at, now):
            notes["long_open"] = f"No listing has shown the ad in the last {STILL_LISTED.days} days."
        else:
            days = (now - began).days
            notes["long_open"] = f"Listed for {days} day{'' if days == 1 else 's'}."
        if start is not None and posted is None:
            unknown["date_refreshed"] = ("This job board gives the date of the ad's last edit, not when it was posted."
                                         if edit_date_source(posting.source) else "The ad has no posted date that can be read as a date.")
        if posting.ghost_checked_at is None:
            unknown.update({"reposted": NOT_YET, "many_similar": NOT_YET})
    status = job.detail_status
    if not (job.description or "").strip():
        notes["evergreen_wording"] = "No description is stored, so only the title was read."
    elif status == "summary":
        notes["evergreen_wording"] = "Only the listing summary was read: the full ad is not stored."
    if ad_raw is None:
        unknown["ad_signs"] = "The AI reads the ad when the job is scored, and only a full description."
    key = company_key(job.company or "")
    profile = profiles.get(key) if key else None
    if not key:
        unknown["company_news_cuts"] = "No employer is named."
    elif profile is None or profile.status != "done":
        unknown["company_news_cuts"] = "The company has not been researched yet."
    out = []
    for code, label in CHECKS:
        state = "found" if code in found else "unknown" if code in unknown else "clear"
        item = {"code": code, "label": label, "state": state}
        note = unknown.get(code) if state == "unknown" else notes.get(code)
        if note and state != "found":
            item["note"] = note
        out.append(item)
    return out


def assess(job: Job, profiles: dict[str, CompanyProfile], now: Optional[datetime] = None,
           own: Optional[tuple] = None, news_cache: Optional[dict] = None, explain: bool = False) -> dict:
    """{"level", "signals": [{code, text, detail?, strength}]} for one job, strongest first. ``own`` is
    ``(description, ad_ghost_json)`` of the job's own columns when it has its own ad (``assess_jobs`` loads them for
    many jobs at once). A job without a posting gets only what its own text and the company research give.
    ``explain`` (the job page) adds ``"checks"``: every kind of sign and whether it was found, clear or could not be
    checked (``_checks``)."""
    from backend.offices import looks_like_recruiter

    now = now or datetime.utcnow()
    posting = job.posting
    if own is None and posting is None:
        own = (job.description, job.ad_ghost_json)  # a private job: its own ad (one query, when not preloaded)
    own_ad = own is not None and (posting is None or own[0] is not None)
    found: list[dict] = []
    stored = _stored(posting.ghost_json) if posting is not None else {}
    if posting is not None:
        found += stored.get("pool") or []
        found += time_signals(posting.first_seen, posting.listed_since, posting.last_listed_at,
                              posting.closed_at is not None, posting.posted_at, now, posting.source)
    if own_ad:
        found += text_signals(job.title or "", own[0] or "")
        ad_raw = own[1]
    else:
        found += stored.get("text") or []
        ad_raw = posting.ad_ghost_json if posting is not None else None
    sign = ad_signs_signal(ad_raw)
    if sign:
        found.append(sign)
    key = company_key(job.company or "")
    if key:
        cache = news_cache if news_cache is not None else {}
        if key not in cache:
            cache[key] = news_signal(profiles.get(key), now)
        if cache[key]:
            found.append(cache[key])
    if not found and not explain:
        return dict(NONE, signals=[])
    signals = []
    for item in found:
        if item.get("when") == "recruiter" and not looks_like_recruiter(job.company or "", profiles):
            continue
        signals.append({k: v for k, v in item.items() if k != "when"})
    signals = ordered(signals)
    out = {"level": level_of(signals),
           "signals": [{**{k: v for k, v in s.items() if k != "weight"}, "strength": strength_of(s["weight"])} for s in signals]}
    if explain:
        out["checks"] = _checks(job, profiles, {s["code"] for s in signals}, ad_raw, now)
    return out


def assess_jobs(db: Session, jobs: list[Job], profiles: dict[str, CompanyProfile],
                now: Optional[datetime] = None, explain: bool = False) -> dict[int, dict]:
    """{job id: assess(job)} for many jobs (``explain``: with what was checked, for the job page). A job reads an ad of its own (a private upload, or a private job) when its
    own description is set; that and the LLM's reading of it are deferred in the job list, so they are loaded here in
    one query per 500 jobs (two sealed values a job, nearly all of them the NULL marker)."""
    now = now or datetime.utcnow()
    own: dict[int, tuple] = {}
    for chunk in _chunks([j.id for j in jobs], 500):
        for job_id, description, ad_raw in db.query(Job.id, Job._description, Job.ad_ghost_json).filter(Job.id.in_(chunk)):
            own[job_id] = (description, ad_raw)
    cache: dict = {}
    return {j.id: assess(j, profiles, now, own.get(j.id), cache, explain) for j in jobs}


def first_reason(result: dict) -> Optional[str]:
    """The text of the strongest sign of a flagged job, else None (the export)."""
    return result["signals"][0]["text"] if result.get("level") and result.get("signals") else None
