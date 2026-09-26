"""Company research: one LLM web-search agent per company.

Each company gets its own request to the provider's Responses API
(``{LLM_BASE_URL}/responses``) with the built-in ``web_search`` tool. The agent returns a JSON
profile: business model, ownership, headquarters and controversies with
news links.

Link integrity: the response lists every page the search tool returned
(``web_search_call.results``) plus the citations the model attached
(``url_citation``). Any URL in the profile that is not among those is
dropped, and a controversy left without a source is dropped with it. So
every link shown in the UI was actually retrieved by the search tool;
none are written from the model's memory.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import Session

from backend.db import CompanyProfile, CompanySource, Job, SessionLocal, company_key
from backend.scorer import ScorerError, config_problem, get_config

log = logging.getLogger(__name__)

REFRESH_AFTER = timedelta(days=90)
POLL_INTERVAL_S = 3
MAX_WAIT_S = 600
NOT_COMPANIES = {"private advertiser", "confidential", "confidential company", "undisclosed", ""}
# Bump when the profile gains fields: older profiles count as missing.
RESEARCH_VERSION = 2

INSTRUCTIONS = """You are a company research agent. Research ONE organisation using web search and return a factual profile.

Search the web before answering (company website, "about" pages, annual reports, Wikipedia, LinkedIn, business press, news coverage of controversies, and employee review sites such as Glassdoor, Indeed company reviews, Seek company reviews, Reddit and Blind). Use the job context given to identify the right organisation when the name is ambiguous.

Return ONLY a JSON object, no markdown:
{
  "official_name": "string",
  "is_company": true,
  "is_recruiter": false,
  "business_model": "2-3 sentences: what they sell, to whom, and how they make money (main revenue streams).",
  "ownership": "1-2 sentences: public (exchange + ticker), private (founders / major shareholders / VC or private-equity backers), subsidiary (of whom), government-owned, or non-profit.",
  "headquarters": "City, Country",
  "employee_count": "approximate headcount with year and basis, e.g. 'about 5,000 (2025, annual report)' or '51-200 (LinkedIn)'",
  "glassdoor_url": "URL of the company's Glassdoor overview or reviews page, if your search returned one, else empty",
  "employee_sentiment": {
    "summary": "2-4 sentences: what current and former employees say overall, and how consistent that is",
    "rating": "e.g. '3.9/5 on Glassdoor (1,200 reviews)'; empty if not found",
    "positives": ["short recurring theme"],
    "negatives": ["short recurring theme"],
    "sources": [{"title": "page title", "url": "https://..."}]
  },
  "controversies": [
    {"title": "short headline", "year": "YYYY", "summary": "1-2 neutral sentences; say 'alleged' where nothing was proven",
     "sources": [{"title": "article title", "url": "https://..."}]}
  ],
  "controversy_note": "one sentence summarising the controversy picture, e.g. 'No significant controversies found in news coverage.'",
  "sources": [{"title": "page title", "url": "https://..."}]
}

Rules:
- Controversies: lawsuits, regulatory fines or investigations, data breaches, workplace/labour or discrimination issues, mass layoffs handled badly, ethics, environmental or safety scandals, fraud. Maximum 5, most significant first. Only include items reported by news outlets you found in your search, and cite those articles' URLs. Never invent a URL: every URL must be a page your search returned.
- If you find no controversies, return "controversies": [] and say so in controversy_note. Don't pad the list with minor items.
- "sources" backs the business model / ownership / headquarters / employee count facts.
- Employee count: prefer the company's own figures, annual reports or LinkedIn; give a range when that is all you find.
- Employee sentiment: base it on review sites and forums you actually found (up to 4 themes each way), note if reviews are few or dated, and cite the pages. If you find nothing, say so in summary and leave the lists empty.
- is_recruiter: true for recruitment/staffing agencies (they advertise roles for undisclosed clients).
- is_company: false if the name is not a real organisation (e.g. "Private Advertiser") - then leave the other fields empty.
- If a fact can't be established, write "Unknown" rather than guessing."""


# --------------------------------------------------------------------------
# Which companies need research
# --------------------------------------------------------------------------

def _board_labels(db: Session) -> set[str]:
    """Labels of sources that are job boards (their jobs name many employers).
    Jobs from those boards whose employer is unknown carry the board's name."""
    out = set()
    for src in db.query(CompanySource).all():
        companies = {c for (c,) in db.query(Job.company).filter(Job.source_id == src.id).distinct()}
        if len(companies) >= 3:
            out.add(company_key(src.label or ""))
    return out


def companies_to_research(db: Session, mode: str = "missing") -> list[dict]:
    """[{name, key, context}] for companies of jobs that pass your filters.

    mode: missing (no profile, failed, older than 90 days, or made before the
    current RESEARCH_VERSION) | all
    """
    skip = NOT_COMPANIES | _board_labels(db)
    rows = (db.query(Job.company, Job.title, Job.location_text, Job.url)
            .filter((Job.excluded_reason.is_(None)) | (Job.excluded_reason == ""))
            .filter(Job.closed_at.is_(None)).all())
    by_key: dict[str, dict] = {}
    for company, title, location, url in rows:
        key = company_key(company or "")
        if key in skip:
            continue
        entry = by_key.setdefault(key, {"name": company.strip(), "key": key, "jobs": []})
        if len(entry["jobs"]) < 3:
            entry["jobs"].append({"title": title, "location": location, "url": url})
    profiles = {p.key: p for p in db.query(CompanyProfile).all()}
    stale_before = datetime.utcnow() - REFRESH_AFTER
    out = []
    for key, entry in by_key.items():
        p = profiles.get(key)
        if mode == "all" or p is None or p.status == "error" or (
                p.status == "done" and ((p.researched_at and p.researched_at < stale_before)
                                        or (p.research_version or 1) < RESEARCH_VERSION)):
            out.append(entry)
    return sorted(out, key=lambda e: e["name"].lower())


def job_context(db: Session, name: str) -> dict:
    """{name, key, jobs} for researching one company on demand."""
    key = company_key(name)
    jobs = [
        {"title": t, "location": l, "url": u}
        for (t, l, u, c) in db.query(Job.title, Job.location_text, Job.url, Job.company).all()
        if company_key(c or "") == key
    ][:3]
    return {"name": name.strip(), "key": key, "jobs": jobs}


# --------------------------------------------------------------------------
# Responses API with web search
# --------------------------------------------------------------------------

def _post(url: str, payload: dict, cfg: dict) -> dict:
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    for attempt in range(1, 4):
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=120)
        except httpx.HTTPError as exc:
            if attempt == 3:
                raise ScorerError(f"connection error: {exc}") from exc
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (401, 403):
            raise ScorerError(f"HTTP {resp.status_code}: API key rejected by {cfg['base_url']} ({resp.text[:300]})", auth=True)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(5 * attempt)
            continue
        if resp.status_code >= 400:
            raise ScorerError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()
    raise ScorerError("gave up after retries")


def _get(url: str, cfg: dict) -> dict:
    resp = httpx.get(url, headers={"Authorization": f"Bearer {cfg['api_key']}"}, timeout=60)
    if resp.status_code >= 400:
        raise ScorerError(f"HTTP {resp.status_code} polling research: {resp.text[:300]}")
    return resp.json()


def run_agent(company: dict, cfg: dict) -> dict:
    """One research agent: web-search grounded response for one company."""
    context = "\n".join(
        f"- {j['title']} ({j.get('location') or 'location n/a'}) {j.get('url') or ''}" for j in company["jobs"]
    )
    payload = {
        "model": cfg["model"],
        "instructions": INSTRUCTIONS,
        "input": f"Organisation: {company['name']}\nJob ads we have from it (for disambiguation):\n{context or '- none'}",
        "tools": [{"type": "web_search", "search_context_size": "medium",
                   "user_location": {"type": "approximate", "country": "AU"}}],
        "include": ["web_search_call.results"],
        "background": True,
    }
    if cfg.get("reasoning_effort"):
        payload["reasoning"] = {"effort": cfg["reasoning_effort"]}
    data = _post(f"{cfg['base_url']}/responses", payload, cfg)
    waited = 0
    while data.get("status") in ("queued", "in_progress") and waited < MAX_WAIT_S:
        time.sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S
        data = _get(f"{cfg['base_url']}/responses/{data['id']}", cfg)
    if data.get("status") != "completed":
        err = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        raise ScorerError(f"research agent ended with status {data.get('status')}: {err or 'no details'}")
    return data


def _norm_url(url: str) -> str:
    p = urlsplit((url or "").strip())
    if p.scheme not in ("http", "https"):
        return ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), p.query, ""))


def extract(response: dict) -> tuple[str, dict[str, str]]:
    """(final answer text, {normalised url: title} of every page the search returned or cited)."""
    seen: dict[str, str] = {}
    text = ""
    for item in response.get("output") or []:
        if item.get("type") == "web_search_call":
            for r in item.get("results") or []:
                if r.get("url"):
                    seen[_norm_url(r["url"])] = r.get("title") or ""
        elif item.get("type") == "message":
            for block in item.get("content") or []:
                if block.get("type") != "output_text":
                    continue
                text = block.get("text") or text  # last message wins (final answer)
                for ann in block.get("annotations") or []:
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        seen[_norm_url(ann["url"])] = ann.get("title") or ""
    seen.pop("", None)
    return text or response.get("output_text") or "", seen


def parse_profile(text: str, seen: dict[str, str]) -> tuple[dict, int]:
    """Validated profile + number of claims dropped for citing unseen URLs."""
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ScorerError(f"research agent did not return JSON: {text[:200]}")
    try:
        obj = json.loads(re.sub(r",\s*([}\]])", r"\1", raw[start:end + 1]))
    except ValueError as exc:
        raise ScorerError(f"research agent returned invalid JSON: {exc}") from exc

    dropped = 0

    def verified(sources: Any) -> list[dict]:
        nonlocal dropped
        out = []
        for s in sources if isinstance(sources, list) else []:
            url = s.get("url") if isinstance(s, dict) else None
            key = _norm_url(url or "")
            if key and key in seen:
                out.append({"title": str(s.get("title") or seen[key] or url).strip(), "url": url})
            elif url:
                dropped += 1
        return out

    controversies = []
    for c in obj.get("controversies") or []:
        if not isinstance(c, dict):
            continue
        srcs = verified(c.get("sources"))
        if not srcs:
            continue  # unverifiable claim: drop it
        controversies.append({
            "title": str(c.get("title") or "").strip(),
            "year": str(c.get("year") or "").strip(),
            "summary": str(c.get("summary") or "").strip(),
            "sources": srcs,
        })
    profile = {
        "official_name": str(obj.get("official_name") or "").strip(),
        "is_company": obj.get("is_company") is not False,
        "is_recruiter": obj.get("is_recruiter") is True,
        "business_model": str(obj.get("business_model") or "").strip(),
        "ownership": str(obj.get("ownership") or "").strip(),
        "headquarters": str(obj.get("headquarters") or "").strip(),
        "controversies": controversies[:5],
        "controversy_note": str(obj.get("controversy_note") or "").strip(),
        "sources": verified(obj.get("sources")),
        "employee_count": str(obj.get("employee_count") or "").strip(),
        "glassdoor_url": _verified_glassdoor(obj.get("glassdoor_url"), seen),
        "sentiment": _sentiment(obj.get("employee_sentiment"), verified),
    }
    # The rating usually comes from Glassdoor: cite the verified page with it.
    gd, sent = profile["glassdoor_url"], profile["sentiment"]
    if gd and not any("glassdoor." in urlsplit(x["url"]).netloc for x in sent["sources"]):
        sent["sources"].insert(0, {"title": "Glassdoor reviews", "url": gd})
    return profile, dropped


def _verified_glassdoor(url: Any, seen: dict[str, str]) -> str:
    """The Glassdoor page URL, only if the search actually returned it."""
    url = str(url or "").strip()
    host = urlsplit(url).netloc.lower()
    if "glassdoor." in host and _norm_url(url) in seen:
        return url
    # Fall back to any Glassdoor company page the search returned.
    for key in seen:
        parts = urlsplit(key)
        if "glassdoor." in parts.netloc and re.search(r"/(Overview|Reviews)/", parts.path, re.I):
            return f"https://www.{parts.netloc}{parts.path}"
    return ""


def _sentiment(obj: Any, verified: Callable[[Any], list[dict]]) -> dict:
    obj = obj if isinstance(obj, dict) else {}

    def themes(v: Any) -> list[str]:
        return [str(t).strip() for t in (v if isinstance(v, list) else []) if str(t).strip()][:4]

    return {
        "summary": str(obj.get("summary") or "").strip(),
        "rating": str(obj.get("rating") or "").strip(),
        "positives": themes(obj.get("positives")),
        "negatives": themes(obj.get("negatives")),
        "sources": verified(obj.get("sources")),
    }


def _save(company: dict, *, profile: Optional[dict] = None, dropped: int = 0,
          error: Optional[str] = None, model: str = "") -> None:
    db = SessionLocal()
    try:
        row = db.get(CompanyProfile, company["key"])
        if row is None:
            row = CompanyProfile(key=company["key"], name=company["name"])
            db.add(row)
        row.name = company["name"]
        if profile is not None:
            row.status = "done" if profile["is_company"] else "skipped"
            row.official_name = profile["official_name"]
            row.is_recruiter = profile["is_recruiter"]
            row.business_model = profile["business_model"]
            row.ownership = profile["ownership"]
            row.headquarters = profile["headquarters"]
            row.controversies_json = json.dumps(profile["controversies"])
            row.controversy_note = profile["controversy_note"]
            row.sources_json = json.dumps(profile["sources"])
            row.employee_count = profile["employee_count"] or None
            row.glassdoor_url = profile["glassdoor_url"] or None
            row.sentiment_json = json.dumps(profile["sentiment"])
            row.research_version = RESEARCH_VERSION
            row.unverified_dropped = dropped
            row.error, row.model, row.researched_at = None, model, datetime.utcnow()
        else:
            if row.status != "done":
                row.status = "error"
            row.error = (error or "unknown error")[:2000]
        db.commit()
    finally:
        db.close()


def research_one(company: dict, cfg: dict) -> dict:
    try:
        text, seen = extract(run_agent(company, cfg))
        profile, dropped = parse_profile(text, seen)
    except ScorerError as exc:
        _save(company, error=str(exc))
        raise
    _save(company, profile=profile, dropped=dropped, model=cfg["model"])
    return profile


def research_companies(companies: list[dict], progress: Callable[[str, dict], None] = lambda s, i: None) -> dict:
    """Run one research agent per company, a few in parallel."""
    cfg = get_config()
    if config_problem(cfg):
        raise ScorerError(config_problem(cfg), auth=True)
    if not companies:
        return {"requested": 0, "researched": 0, "errors": 0, "aborted": None}
    stop = threading.Event()
    lock = threading.Lock()
    counts = {"researched": 0, "errors": 0, "done": 0}
    first_error: list[str] = []
    aborted: list[str] = []

    def work(company: dict) -> None:
        if stop.is_set():
            return
        try:
            research_one(company, cfg)
            ok = True
        except ScorerError as exc:
            ok = False
            if exc.auth:
                stop.set()
                aborted.append(str(exc))
            elif not first_error:
                first_error.append(f"{company['name']}: {exc}")
        with lock:
            counts["done"] += 1
            counts["researched" if ok else "errors"] += 1
            progress(f"researching companies {counts['done']}/{len(companies)} (last: {company['name']})",
                     {"done": counts["done"], "total": len(companies)})

    with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
        for fut in as_completed([pool.submit(work, c) for c in companies]):
            fut.result()
    return {
        "requested": len(companies),
        "researched": counts["researched"],
        "errors": counts["errors"],
        "skipped": len(companies) - counts["done"],
        "first_error": first_error[0] if first_error else None,
        "aborted": aborted[0] if aborted else None,
    }
