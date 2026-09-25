"""Muse Spark fit scorer (Meta Model API, OpenAI-compatible chat completions).

Config (``.env``):
    MODEL_API_KEY                 API key (MUSE_SPARK_API_KEY also accepted)
    MUSE_SPARK_BASE_URL           default https://api.meta.ai/v1
    MUSE_SPARK_MODEL              default muse-spark-1.3-contributor
    MUSE_SPARK_REASONING_EFFORT   minimal | low | medium | high | xhigh (default low)
    MUSE_SPARK_TIMEOUT_S          per request, default 180
    MUSE_SPARK_CONCURRENCY        parallel requests, default 3

Results live in ``fit_results``: ``status="ok"`` rows hold a score,
``status="error"`` rows hold the error and are retried on the next
scoring pass. Each row records the hash of the profile text it was
scored against, so jobs scored before the documents changed show as
stale.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Optional

import httpx
from sqlalchemy.orm import Session

from backend import config  # noqa: F401  (loads .env)
from backend.db import Document, FitResult, Job, SessionLocal

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3-contributor"
KNOWN_MODELS = {"muse-spark-1.3", "muse-spark-1.3-contributor", "muse-spark-1.2",
                "muse-spark-1.2-contributor", "muse-spark-1.1"}
MAX_RETRIES = 3


class ScorerError(Exception):
    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message)
        self.auth = auth


def get_config() -> dict[str, Any]:
    key = (os.getenv("MODEL_API_KEY") or os.getenv("MUSE_SPARK_API_KEY") or "").strip()
    return {
        "api_key": key,
        "key_source": "MODEL_API_KEY" if os.getenv("MODEL_API_KEY") else ("MUSE_SPARK_API_KEY" if key else "none"),
        "base_url": (os.getenv("MUSE_SPARK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        "model": (os.getenv("MUSE_SPARK_MODEL") or DEFAULT_MODEL).strip(),
        "reasoning_effort": (os.getenv("MUSE_SPARK_REASONING_EFFORT") or "low").strip(),
        "timeout_s": float(os.getenv("MUSE_SPARK_TIMEOUT_S") or 180),
        "concurrency": max(1, int(os.getenv("MUSE_SPARK_CONCURRENCY") or 3)),
    }


def public_config() -> dict[str, Any]:
    cfg = get_config()
    return {
        "has_key": bool(cfg["api_key"]),
        "key_source": cfg["key_source"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "model_known": cfg["model"] in KNOWN_MODELS,
        "reasoning_effort": cfg["reasoning_effort"],
    }


# --------------------------------------------------------------------------
# Profile text
# --------------------------------------------------------------------------

_KIND_ORDER = {"resume": 0, "cover_letter": 1, "other": 2}
_KIND_LABEL = {"resume": "RESUME / CV", "cover_letter": "COVER LETTER", "other": "SUPPORTING DOCUMENT"}


def build_profile(db: Session) -> tuple[str, str]:
    """(profile text, hash). Resume(s) first, then cover letters, then other docs."""
    from backend.profile import get_profile

    docs = [d for d in db.query(Document).all() if d.use_for_scoring and (d.text or "").strip()]
    docs.sort(key=lambda d: (_KIND_ORDER.get(d.kind or "other", 2), d.id))
    parts = []
    prof = get_profile(db)
    if prof["titles"]:
        parts.append("TARGET ROLES: " + ", ".join(prof["titles"]))
    for d in docs:
        parts.append(f"=== {_KIND_LABEL.get(d.kind or 'other', 'DOCUMENT')}: {d.filename} ===\n{d.text.strip()}")
    text = "\n\n".join(parts)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a strict, evidence-based job-fit assessor. You compare ONE job posting against ONE candidate's documents.

Return ONLY a JSON object, no markdown, with exactly this shape:
{
  "score": 0,
  "requirements": [
    {"point": "string", "must_have": true, "matched": true,
     "evidence": [{"bullet": "string", "sub_bullets": ["string"]}]}
  ],
  "gaps": ["string"],
  "summary": "string"
}

Rules:
1. Extract every pertinent requirement from the job (skills, years of experience, domain, seniority, tools, qualifications, languages, certifications). One entry each. Mark "must_have" true for stated requirements, false for nice-to-haves.
2. "matched": true only when the candidate documents contain evidence. Each matched entry needs at least one evidence item: "bullet" quotes or closely paraphrases the candidate document; "sub_bullets" add supporting specifics (employer, years, outcomes) from the documents. Unmatched entries use "evidence": [].
3. Never invent employers, dates, skills, numbers or credentials. If the documents don't show it, it is not matched.
4. "gaps": short strings naming missing or weak must-haves.
5. "score": integer 0-100. Must-haves dominate; 90+ means every must-have is evidenced; below 40 means major must-haves are missing.
6. "summary": 2-4 sentences: overall fit, strongest evidence, biggest gaps.
7. The resume is the primary source. Other documents (cover letters, interview answers, transcripts) are supporting evidence only.
8. If the job text is only a short listing summary, assess what is stated and say in the summary that the full description was not available."""


def build_job_text(job: Job) -> str:
    lines = [
        f"Title: {job.title or '?'}",
        f"Company: {job.company or '?'}",
        f"Location: {job.location_text or 'not stated'}",
        f"Work mode: {job.work_mode or 'unknown'}",
    ]
    if job.salary_text:
        lines.append(f"Salary: {job.salary_text}")
    if job.detail_status == "summary":
        lines.append("Note: only the job board's listing summary is available, not the full description.")
    lines.append("")
    lines.append(job.description or "(no description)")
    return "\n".join(lines)


def build_messages(job: Job, profile_text: str) -> list[dict]:
    # Stable content (instructions + candidate profile) first, job last.
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            "CANDIDATE DOCUMENTS:\n<<<\n" + (profile_text or "(no documents uploaded)") + "\n>>>\n\n"
            "JOB POSTING:\n<<<\n" + build_job_text(job) + "\n>>>\n\n"
            "Return only the JSON object."
        )},
    ]


# --------------------------------------------------------------------------
# API call
# --------------------------------------------------------------------------

def call_model(messages: list[dict], cfg: dict) -> str:
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "reasoning_effort": cfg["reasoning_effort"],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    url = f"{cfg['base_url']}/chat/completions"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=cfg["timeout_s"])
        except httpx.HTTPError as exc:
            if attempt == MAX_RETRIES:
                raise ScorerError(f"connection error: {exc.__class__.__name__}: {exc}") from exc
            time.sleep(2 ** attempt)
            continue
        body = resp.text[:600]
        if resp.status_code in (401, 403):
            raise ScorerError(f"HTTP {resp.status_code}: API key rejected by {cfg['base_url']} ({body})", auth=True)
        if resp.status_code == 400 and "response_format" in body and "response_format" in payload:
            payload.pop("response_format")
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            retry_after = resp.headers.get("retry-after", "")
            time.sleep(float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 5 * attempt)
            continue
        if resp.status_code >= 400:
            raise ScorerError(f"HTTP {resp.status_code}: {body}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ScorerError(f"unexpected response shape: {body}") from exc
        if isinstance(content, list):
            content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        return str(content or "")
    raise ScorerError("gave up after retries")


# --------------------------------------------------------------------------
# Result parsing
# --------------------------------------------------------------------------

def parse_result(raw: str) -> dict:
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ScorerError(f"model did not return JSON: {raw[:300]}")
    try:
        obj = json.loads(re.sub(r",\s*([}\]])", r"\1", text[start:end + 1]))
    except ValueError as exc:
        raise ScorerError(f"invalid JSON from model: {exc}") from exc
    try:
        score = max(0, min(100, int(round(float(obj.get("score", 0))))))
    except (TypeError, ValueError):
        raise ScorerError(f"no numeric score in model output: {str(obj)[:300]}")
    reqs = []
    for r in obj.get("requirements") or []:
        if not isinstance(r, dict) or not str(r.get("point", "")).strip():
            continue
        matched = r.get("matched") is True or str(r.get("matched")).lower() == "true"
        evidence = []
        if matched:
            for e in r.get("evidence") or []:
                if isinstance(e, dict) and str(e.get("bullet", "")).strip():
                    subs = e.get("sub_bullets") if isinstance(e.get("sub_bullets"), list) else []
                    evidence.append({"bullet": str(e["bullet"]).strip(),
                                     "sub_bullets": [str(s).strip() for s in subs if str(s).strip()]})
        reqs.append({"point": str(r["point"]).strip(), "must_have": r.get("must_have") is not False,
                     "matched": matched, "evidence": evidence})
    gaps = [str(g).strip() for g in obj.get("gaps") or [] if str(g).strip()]
    return {"score": score, "requirements": reqs, "gaps": gaps, "summary": str(obj.get("summary") or "").strip()}


# --------------------------------------------------------------------------
# Scoring jobs
# --------------------------------------------------------------------------

def _store(db: Session, job_id: int, *, result: Optional[dict] = None, error: Optional[str] = None,
           profile_hash: str = "", model: str = "") -> None:
    row = db.query(FitResult).filter(FitResult.job_id == job_id).first()
    if row is None:
        row = FitResult(job_id=job_id)
        db.add(row)
    if result is not None:
        row.status, row.score, row.error = "ok", result["score"], None
        row.evidence_json = json.dumps(result)
        row.profile_hash, row.model = profile_hash, model
    else:
        # Keep a previous good score; just record the failure.
        if row.score is None:
            row.status = "error"
            row.evidence_json = row.evidence_json or "{}"
        row.error = (error or "unknown error")[:2000]
    row.created_at = datetime.utcnow()
    db.commit()


def score_one(job_id: int, profile_text: str, profile_hash: str, cfg: dict) -> dict:
    """Score a single job and persist the outcome. Raises ScorerError on auth failure."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            raise ScorerError(f"job {job_id} not found")
        try:
            result = parse_result(call_model(build_messages(job, profile_text), cfg))
        except ScorerError as exc:
            _store(db, job_id, error=str(exc))
            raise
        _store(db, job_id, result=result, profile_hash=profile_hash, model=cfg["model"])
        return result
    finally:
        db.close()


def select_jobs(db: Session, mode: str, profile_hash: str) -> list[int]:
    """Job ids to score. mode: pending (never scored or errored) | stale (+ scored
    against older documents) | all (every eligible job)."""
    rows = (
        db.query(Job.id, FitResult.status, FitResult.profile_hash, FitResult.score)
        .outerjoin(FitResult, FitResult.job_id == Job.id)
        .filter((Job.excluded_reason.is_(None)) | (Job.excluded_reason == ""))
        .filter(Job.closed_at.is_(None))
        .filter(Job.detail_status.in_(["full", "summary"]))
        .all()
    )
    out = []
    for job_id, status, phash, score in rows:
        unscored = status is None or score is None
        if mode == "all" or unscored or (mode == "stale" and phash != profile_hash):
            out.append(job_id)
    return out


def _duplicate_groups(job_ids: list[int]) -> dict[int, list[int]]:
    """{representative id: [duplicate ids]} for postings with the same company,
    title and description (e.g. one role advertised in several cities)."""
    db = SessionLocal()
    try:
        rows = db.query(Job.id, Job.company, Job.title, Job.description_hash).filter(Job.id.in_(job_ids)).all()
    finally:
        db.close()
    groups: dict[int, list[int]] = {}
    first: dict[tuple, int] = {}
    for job_id, company, title, dhash in rows:
        key = ((company or "").lower(), (title or "").lower(), dhash)
        if dhash and key in first:
            groups[first[key]].append(job_id)
        else:
            first[key] = job_id
            groups[job_id] = []
    return groups


def _copy_to_duplicates(dup_ids: list[int], result: dict, profile_hash: str, model: str) -> None:
    if not dup_ids:
        return
    db = SessionLocal()
    try:
        for job_id in dup_ids:
            _store(db, job_id, result=result, profile_hash=profile_hash, model=model)
    finally:
        db.close()


def score_jobs(job_ids: list[int], progress: Callable[[str, dict], None] = lambda s, i: None) -> dict:
    cfg = get_config()
    if not cfg["api_key"]:
        raise ScorerError("no API key: set MODEL_API_KEY in .env", auth=True)
    db = SessionLocal()
    try:
        profile_text, profile_hash = build_profile(db)
    finally:
        db.close()
    if not job_ids:
        return {"requested": 0, "scored": 0, "errors": 0, "aborted": None}
    groups = _duplicate_groups(job_ids)
    requested = len(job_ids)
    job_ids = list(groups)

    stop = threading.Event()
    lock = threading.Lock()
    counts = {"scored": 0, "errors": 0, "done": 0}
    first_error: list[str] = []
    aborted: list[str] = []

    def work(job_id: int) -> None:
        if stop.is_set():
            return
        try:
            result = score_one(job_id, profile_text, profile_hash, cfg)
            ok = True
            _copy_to_duplicates(groups[job_id], result, profile_hash, cfg["model"])
        except ScorerError as exc:
            ok = False
            if exc.auth:
                stop.set()
                aborted.append(str(exc))
            elif not first_error:
                first_error.append(str(exc))
        with lock:
            counts["done"] += 1
            counts["scored" if ok else "errors"] += 1
            progress(f"scoring {counts['done']}/{len(job_ids)}", {"done": counts["done"], "total": len(job_ids), **counts})

    with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
        for fut in as_completed([pool.submit(work, j) for j in job_ids]):
            fut.result()
    return {
        "requested": requested,
        "unique_postings": len(job_ids),
        "scored": counts["scored"],
        "errors": counts["errors"],
        "skipped": len(job_ids) - counts["done"],
        "first_error": first_error[0] if first_error else None,
        "aborted": aborted[0] if aborted else None,
    }


def test_connection() -> dict:
    """One tiny request to check key, endpoint and model."""
    cfg = get_config()
    out = public_config()
    if not cfg["api_key"]:
        return {**out, "ok": False, "message": "No API key: set MODEL_API_KEY in .env and restart."}
    messages = [{"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}]
    try:
        reply = call_model(messages, {**cfg, "reasoning_effort": "minimal", "timeout_s": 60})
    except ScorerError as exc:
        return {**out, "ok": False, "message": str(exc)}
    return {**out, "ok": True, "message": f"Connected. Model replied: {reply[:200]}"}
