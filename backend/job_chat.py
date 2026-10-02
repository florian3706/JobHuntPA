"""Chat about one or more jobs with the configured LLM.

The model gets the candidate's documents and, per job: the ad, the fit
assessment, the researched company profile and any cover-letter draft,
then the conversation so far. With "Search the web" on, the question goes
to the provider's web search (the Responses API's web_search tool, Claude's,
Gemini's or OpenRouter's; like company research), which isn't offered by every
provider; only links the search actually returned are kept, and they're listed as
the answer's sources. Without it, links that aren't in the material are
removed, so no link comes from the model's memory.

    GET    /api/chats?job_ids=1,2     chats about exactly these jobs, newest first
    POST   /api/chats                 {job_ids, message, web_search}: start a chat
    GET    /api/chats/{id}            one chat with its messages
    POST   /api/chats/{id}/messages   {message, web_search}: ask a follow-up
    DELETE /api/chats/{id}

Merged duplicate ads count as the job they were merged into.
Reasoning level: task "chat".
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.db import (ChatMessage, ChatThread, CompanyProfile, CoverLetter, FitResult, Job, company_key,
                        get_db)
from backend.workspaces import current_workspace, owned

router = APIRouter(prefix="/api/chats", tags=["job chat"])

MAX_JOBS = 12
HISTORY_MESSAGES = 30

SYSTEM_PROMPT = """You are a job-search adviser inside a private job-hunting app. The candidate is asking about the job ad(s) below. You have their documents (resume first), each ad, an earlier automated fit assessment, researched facts about the employer and any cover-letter draft.

How to answer:
- Answer the question directly first, then explain. Be candid: point out risks, gaps and red flags as well as strengths.
- Claims about the candidate come only from their documents. Never invent experience, employers, numbers or qualifications; say when the documents don't show something.
- Claims about a job or employer come from the material below{web}. If the answer isn't there, say so and suggest how to find out (e.g. a question for the recruiter).
- With several jobs, say which one you mean ("Job 2, Canva") and compare them side by side when asked.
- The fit score is an automated estimate; disagree with it when the material supports that, and say why.
- Use the spelling of the job ads (Australian English for Australian employers).
- Keep answers focused: short paragraphs or bullet lists. Markdown (bold, lists, links) is fine."""

WEB_NOTE = (", or from web pages you find with web search. Search when the material doesn't cover a question "
            "(company news, salary benchmarks, interview process, culture). Link the pages you used as markdown links "
            "next to the facts they support; never link a page you didn't retrieve")
NO_WEB_NOTE = ". Don't give web links: you can't browse, and links from memory are often wrong"


class StartChat(BaseModel):
    job_ids: list[int] = Field(min_length=1)
    message: str = Field(min_length=1, max_length=8000)
    web_search: bool = False


class FollowUp(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    web_search: bool = False


# --------------------------------------------------------------------------
# Jobs in a chat
# --------------------------------------------------------------------------

def resolve_jobs(db: Session, ws: int, job_ids: list[int]) -> list[Job]:
    """The listed jobs behind these ids (a merged copy counts as the job it
    was merged into), in id order."""
    if len(set(job_ids)) > MAX_JOBS:
        raise HTTPException(status_code=400, detail=f"Chat about up to {MAX_JOBS} jobs at a time; select fewer.")
    jobs: dict[int, Job] = {}
    for job_id in job_ids:
        job = owned(db, Job, job_id, ws, "Job")
        if job.duplicate_of:
            job = db.get(Job, job.duplicate_of) or job
        jobs[job.id] = job
    return [jobs[i] for i in sorted(jobs)]


def _thread_jobs(db: Session, thread: ChatThread) -> list[Job]:
    jobs = {}
    for job_id in json.loads(thread.job_ids_json or "[]"):
        job = db.get(Job, job_id)
        if job is not None and job.duplicate_of:
            job = db.get(Job, job.duplicate_of) or job
        if job is not None:
            jobs[job.id] = job
    return [jobs[i] for i in sorted(jobs)]


def _job_label(job: Job) -> dict:
    return {"id": job.id, "title": job.title, "company": job.company, "url": job.url}


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[... rest of the ad cut for length]"


def job_block(db: Session, n: int, job: Job, ad_limit: int) -> str:
    lines = [f"=== JOB {n}: {job.title or '?'} at {job.company or '?'} ==="]
    facts = [f"Location: {job.location_text or 'not stated'}", f"Work mode: {job.work_mode or 'unknown'}"]
    if job.office_days:
        facts.append(f"{job.office_days} days a week in the office")
    if job.salary_text:
        facts.append(f"Salary: {job.salary_text}")
    if job.distance_km is not None:
        facts.append(f"{job.distance_km:.0f} km from the candidate's home")
    if job.posted_at:
        facts.append(f"Posted: {job.posted_at}")
    lines.append(" | ".join(facts))
    lines.append(f"Ad link: {job.url}")
    copies = db.query(Job).filter(Job.duplicate_of == job.id).all()
    if copies:
        lines.append("Also advertised at: " + ", ".join(c.url for c in copies if c.url))
    lines.append(f"Candidate's status for this job: {job.status.replace('_', ' ')}"
                 + (" (no longer advertised)" if job.closed_at else ""))
    if job.excluded_reason:
        lines.append(f"The candidate's search filters exclude it: {job.excluded_reason}")
    if job.detail_status != "full":
        lines.append("Note: only the job board's short listing summary is stored, not the full ad.")
    lines += ["", "Ad:", _clip(job.description or "(no description stored)", ad_limit)]

    fit = db.query(FitResult).filter(FitResult.job_id == job.id, FitResult.status == "ok").first()
    if fit:
        data = json.loads(fit.evidence_json or "{}")
        matched = [r["point"] for r in data.get("requirements", []) if r.get("matched")]
        missing = [r["point"] for r in data.get("requirements", []) if not r.get("matched")]
        lines += ["", f"Automated fit assessment: {fit.score}/100. {data.get('summary', '')}",
                  "Evidenced requirements: " + ("; ".join(matched) or "none"),
                  "Not evidenced: " + ("; ".join(missing) or "none"),
                  "Gaps: " + ("; ".join(data.get("gaps", [])) or "none")]

    profile = db.get(CompanyProfile, company_key(job.company or ""))
    if profile and profile.status == "done":
        p = profile.to_dict()
        about = [f"Business: {p['business_model'] or 'unknown'}", f"Ownership: {p['ownership'] or 'unknown'}",
                 f"Headquarters: {p['headquarters'] or 'unknown'}", f"Employees: {p['employee_count'] or 'unknown'}"]
        if p["is_recruiter"]:
            about.insert(0, "This is a recruitment agency advertising for an undisclosed client.")
        if p["sentiment"]:
            s = p["sentiment"]
            about.append(f"Employee sentiment: {s.get('rating') or ''} {s.get('summary') or ''}".strip())
        if p["controversies"]:
            about.append("Controversies: " + "; ".join(
                f"{c['title']} ({c.get('year') or 'n.d.'}): {c.get('summary', '')}" for c in p["controversies"]))
        lines += ["", "Researched employer facts:", *about]

    letter = db.query(CoverLetter).filter(CoverLetter.job_id == job.id).first()
    if letter and letter.text.strip():
        lines += ["", "The candidate's cover-letter draft for this job:", letter.text.strip()]
    return "\n".join(lines)


def build_context(db: Session, ws: int, jobs: list[Job], web: bool) -> str:
    from backend.scorer import build_profile

    profile_text, _ = build_profile(db, ws)
    ad_limit = 12000 if len(jobs) <= 2 else 6000 if len(jobs) <= 5 else 3000
    parts = [SYSTEM_PROMPT.replace("{web}", WEB_NOTE if web else NO_WEB_NOTE),
             f"Today's date: {date.today().isoformat()}.",
             "=== CANDIDATE'S DOCUMENTS ===\n" + (profile_text or "(no documents uploaded)")]
    parts += [job_block(db, n, job, ad_limit) for n, job in enumerate(jobs, 1)]
    return "\n\n".join(parts)


def known_links(db: Session, jobs: list[Job]) -> set[str]:
    """Links the material itself contains (ads, copies, research sources)."""
    from backend.research import _norm_url

    urls = set()
    for job in jobs:
        urls.add(job.url or "")
        urls.update(u for (u,) in db.query(Job.url).filter(Job.duplicate_of == job.id))
        urls.update(re.findall(r"https?://[^\s)\]>\"']+", job.description or ""))
        profile = db.get(CompanyProfile, company_key(job.company or ""))
        if profile and profile.status == "done":
            urls.update(re.findall(r"https?://[^\s\"']+", " ".join(
                [profile.sources_json or "", profile.controversies_json or "", profile.sentiment_json or "",
                 profile.glassdoor_url or ""])))
    return {_norm_url(u.rstrip(".,;")) for u in urls} - {""}


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"(?<![(\[])https?://[^\s)\]>]+")


def keep_known_links(text: str, allowed: set[str]) -> str:
    """Unlink URLs that aren't in ``allowed`` (normalised): keep the link
    text of a markdown link, drop a bare URL."""
    from backend.research import _norm_url

    def md(m: re.Match) -> str:
        return m.group(0) if _norm_url(m.group(2)) in allowed else m.group(1)

    def bare(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;:")
        return m.group(0) if _norm_url(url) in allowed else "(link removed: not verified)"

    return _BARE_URL.sub(bare, _MD_LINK.sub(md, text))


def _history(thread: Optional[ChatThread], db: Session) -> list[dict]:
    if thread is None:
        return []
    rows = (db.query(ChatMessage).filter(ChatMessage.thread_id == thread.id)
            .order_by(ChatMessage.id.desc()).limit(HISTORY_MESSAGES).all())
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


def ask(db: Session, ws: int, jobs: list[Job], history: list[dict], message: str, web: bool) -> dict:
    """{"text", "sources", "model"} for the next answer."""
    from backend import llm_providers
    from backend.research import extract
    from backend.scorer import ScorerError, call_model, config_problem, get_config

    cfg = get_config("chat")
    if config_problem(cfg):
        raise HTTPException(status_code=400, detail=config_problem(cfg))
    no_search = llm_providers.web_search_problem(cfg, "The chat's \"Search the web\" option") if web else None
    if no_search:
        raise HTTPException(status_code=400, detail=f"{no_search} Or turn that option off.")
    context = build_context(db, ws, jobs, web)
    turns = history + [{"role": "user", "content": message}]
    try:
        if web:
            response = _with_retry(lambda: llm_providers.web_search(
                cfg, instructions=context, input=turns, context_size="medium", what="web search"))
            text, seen = extract(response)
            text = keep_known_links(text, set(seen) | known_links(db, jobs))
            sources = web_sources(response, text, seen)
        else:
            messages = [{"role": "system", "content": context}, *turns]
            text = call_model(messages, {**cfg, "timeout_s": max(cfg["timeout_s"], 300)}, json_mode=False)
            text, sources = keep_known_links(text, known_links(db, jobs)), []
    except ScorerError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=502, detail="The model returned an empty answer; try again.")
    return {"text": text, "sources": sources, "model": cfg["model"]}


def _with_retry(call):
    """Web-search requests now and then end "failed" on the provider's side
    with no reason given; one retry usually works."""
    from backend.scorer import ScorerError

    try:
        return call()
    except ScorerError as exc:
        if "ended with status failed" not in str(exc):
            raise
        return call()


MAX_SEARCHED_SOURCES = 8


def web_sources(response: dict, text: str, seen: dict[str, str]) -> list[dict]:
    """[{title, url, cited}]: the searched pages the answer cites (citation
    annotations or links in the text). Some providers return neither; then
    the pages the search returned, marked cited=False."""
    from backend.research import _norm_url

    cited, searched = [], []
    for item in response.get("output") or []:
        if item.get("type") == "web_search_call":
            searched += [r["url"] for r in item.get("results") or [] if r.get("url")]
        for block in item.get("content") or [] if item.get("type") == "message" else []:
            cited += [a["url"] for a in block.get("annotations") or [] if a.get("type") == "url_citation" and a.get("url")]
    cited += [m.group(2) for m in _MD_LINK.finditer(text)] + [m.group(0).rstrip(".,;:") for m in _BARE_URL.finditer(text)]
    out: dict[str, dict] = {}
    for url in cited:
        if _norm_url(url) in seen:
            out.setdefault(_norm_url(url), {"title": seen[_norm_url(url)] or url, "url": url, "cited": True})
    if not out:
        for url in searched[:MAX_SEARCHED_SOURCES]:
            out.setdefault(_norm_url(url), {"title": seen.get(_norm_url(url)) or url, "url": url, "cited": False})
    return list(out.values())


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def _message_dict(m: ChatMessage) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "web_search": bool(m.web_search),
            "sources": json.loads(m.sources_json) if m.sources_json else [], "model": m.model,
            "created_at": m.created_at.isoformat() if m.created_at else None}


def _thread_dict(db: Session, t: ChatThread, *, messages: bool = True) -> dict:
    out = {"id": t.id, "title": t.title, "job_ids": json.loads(t.job_ids_json or "[]"),
           "jobs": [_job_label(j) for j in _thread_jobs(db, t)],
           "updated_at": t.updated_at.isoformat() if t.updated_at else None}
    if messages:
        out["messages"] = [_message_dict(m) for m in db.query(ChatMessage)
                           .filter(ChatMessage.thread_id == t.id).order_by(ChatMessage.id)]
    return out


def _answer(db: Session, ws: int, thread: Optional[ChatThread], jobs: list[Job], message: str,
            web: bool) -> ChatThread:
    reply = ask(db, ws, jobs, _history(thread, db), message, web)
    now = datetime.utcnow()
    if thread is None:
        title = " ".join(message.split())
        thread = ChatThread(workspace_id=ws, job_ids_json=json.dumps([j.id for j in jobs]),
                            title=title[:80] + ("…" if len(title) > 80 else ""), created_at=now)
        db.add(thread)
        db.flush()
    thread.updated_at = now
    db.add(ChatMessage(thread_id=thread.id, role="user", content=message, web_search=web, created_at=now))
    db.add(ChatMessage(thread_id=thread.id, role="assistant", content=reply["text"], web_search=web,
                       sources_json=json.dumps(reply["sources"]) if reply["sources"] else None,
                       model=reply["model"], created_at=datetime.utcnow()))
    db.commit()
    return thread


@router.get("")
def list_chats(job_ids: str = Query("", description="comma-separated job ids"), db: Session = Depends(get_db),
               ws: int = Depends(current_workspace)):
    ids = [int(x) for x in job_ids.split(",") if x.strip().isdigit()]
    wanted = [j.id for j in resolve_jobs(db, ws, ids)] if ids else None
    threads = (db.query(ChatThread).filter(ChatThread.workspace_id == ws)
               .order_by(ChatThread.updated_at.desc()).all())
    out = []
    for t in threads:
        if wanted is None or [j.id for j in _thread_jobs(db, t)] == wanted:
            out.append(_thread_dict(db, t, messages=False))
    return out


@router.post("")
def start_chat(payload: StartChat, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    jobs = resolve_jobs(db, ws, payload.job_ids)
    thread = _answer(db, ws, None, jobs, payload.message.strip(), payload.web_search)
    return _thread_dict(db, thread)


@router.get("/{chat_id}")
def get_chat(chat_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    return _thread_dict(db, owned(db, ChatThread, chat_id, ws, "Chat"))


@router.post("/{chat_id}/messages")
def follow_up(chat_id: int, payload: FollowUp, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    thread = owned(db, ChatThread, chat_id, ws, "Chat")
    jobs = _thread_jobs(db, thread)
    if not jobs:
        raise HTTPException(status_code=409, detail="The jobs in this chat no longer exist.")
    thread = _answer(db, ws, thread, jobs, payload.message.strip(), payload.web_search)
    return _thread_dict(db, thread)


@router.delete("/{chat_id}")
def delete_chat(chat_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    db.delete(owned(db, ChatThread, chat_id, ws, "Chat"))
    db.commit()
    return {"ok": True}
