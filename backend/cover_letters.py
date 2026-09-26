"""Cover-letter drafts per job, written by the LLM from the user's documents.

    GET  /api/jobs/{id}/cover-letter        saved draft (404 if none)
    POST /api/jobs/{id}/cover-letter        draft a new one {instructions?}
    PUT  /api/jobs/{id}/cover-letter        save edits {text}
    GET  /api/jobs/{id}/cover-letter.docx   download as Word

The prompt gets the resume(s) first, earlier cover letters as style
samples, the job ad, the fit result (matched evidence and gaps) and the
company profile when there is one. Reasoning level: task "cover_letter".
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.db import CompanyProfile, CoverLetter, Document, FitResult, Job, company_key, get_db
from backend.workspaces import current_workspace, owned

router = APIRouter(prefix="/api/jobs", tags=["cover letters"])

SYSTEM_PROMPT = """You write job application cover letters for one candidate.

Write a cover letter for the job below, using ONLY facts from the candidate's documents.

Rules:
- Never invent experience, employers, dates, numbers, qualifications or skills. If the documents don't show something the job asks for, don't claim it; where it helps, point to the closest genuine, transferable experience.
- Lead with the candidate's strongest evidence for the job's most important requirements (the fit analysis lists them when available).
- Show real interest in this employer using the facts provided about it; don't flatter or guess.
- 250-350 words in total (never more than 400), 3-4 short paragraphs of 2-4 sentences each. Plain, confident, professional tone; no bullet points.
- Pick the 2-3 most relevant achievements and tell them concretely (what, scale, result) instead of listing every role and date. Mention a role's dates only when they matter.
- Avoid cliches ("I am writing to express my interest", "team player", "passionate", "I believe I would be a great fit"). Vary sentence openings; don't start paragraphs with job titles.
- Every sentence must be grammatically complete; re-read before answering.
- Use the spelling conventions of the job ad (e.g. Australian/British vs American English).
- Address the hiring manager by name only if the ad names one, otherwise "Dear Hiring Manager,".
- Sign off with the candidate's name as it appears in their resume.
- If earlier cover letters are provided, match their voice, but don't copy sentences.
- Follow the candidate's extra instructions when given, unless they conflict with the no-invention rule.

Return only the letter text, starting with the salutation. No title, notes or markdown."""


class DraftRequest(BaseModel):
    instructions: Optional[str] = Field(default=None, max_length=2000)


class SaveRequest(BaseModel):
    text: str = Field(max_length=20000)


def _letter_dict(cl: CoverLetter) -> dict:
    return {
        "job_id": cl.job_id,
        "text": cl.text,
        "instructions": cl.instructions,
        "edited": bool(cl.edited),
        "model": cl.model,
        "reasoning_effort": cl.reasoning_effort,
        "created_at": cl.created_at.isoformat() if cl.created_at else None,
        "updated_at": cl.updated_at.isoformat() if cl.updated_at else None,
    }


def build_prompt(db: Session, job: Job, instructions: str = "") -> str:
    docs = (db.query(Document)
            .filter(Document.workspace_id == job.workspace_id, Document.use_for_scoring.is_(True)).all())
    resumes = [d for d in docs if d.kind == "resume" and (d.text or "").strip()]
    letters = [d for d in docs if d.kind == "cover_letter" and (d.text or "").strip()]
    others = [d for d in docs if d.kind not in ("resume", "cover_letter") and (d.text or "").strip()]
    if not resumes and not others:
        raise HTTPException(status_code=400, detail="Upload your resume on the Documents tab first.")
    parts = []
    for d in resumes:
        parts.append(f"=== RESUME / CV: {d.filename} ===\n{d.text.strip()}")
    for d in others:
        parts.append(f"=== SUPPORTING DOCUMENT: {d.filename} ===\n{d.text.strip()}")
    for d in letters[:2]:
        parts.append(f"=== EARLIER COVER LETTER (style sample only): {d.filename} ===\n{d.text.strip()[:6000]}")

    job_lines = [f"Title: {job.title}", f"Company: {job.company}", f"Location: {job.location_text or 'not stated'}"]
    if job.detail_status == "summary":
        job_lines.append("Note: only the job board's listing summary is available.")
    job_lines += ["", job.description or "(no description)"]
    parts.append("=== JOB AD ===\n" + "\n".join(job_lines))

    fit = db.query(FitResult).filter(FitResult.job_id == job.id, FitResult.status == "ok").first()
    if fit:
        data = json.loads(fit.evidence_json or "{}")
        matched = [f"- {r['point']}: " + "; ".join(e["bullet"] for e in r.get("evidence", []))
                   for r in data.get("requirements", []) if r.get("matched")]
        gaps = [f"- {g}" for g in data.get("gaps", [])]
        parts.append("=== FIT ANALYSIS (from an earlier assessment) ===\nEvidenced requirements:\n"
                     + ("\n".join(matched) or "- none") + "\nGaps:\n" + ("\n".join(gaps) or "- none"))

    profile = db.get(CompanyProfile, company_key(job.company or ""))
    if profile and profile.status == "done" and not profile.is_recruiter:
        parts.append("=== ABOUT THE EMPLOYER (researched) ===\n"
                     f"Business: {profile.business_model or 'unknown'}\nHeadquarters: {profile.headquarters or 'unknown'}")
    if instructions.strip():
        parts.append("=== CANDIDATE'S EXTRA INSTRUCTIONS ===\n" + instructions.strip())
    return "\n\n".join(parts)


def _clean_letter(text: str) -> str:
    text = (text or "").strip()
    fence = re.match(r"^```\w*\n(.*?)\n```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    return text


@router.get("/{job_id}/cover-letter")
def get_cover_letter(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    owned(db, Job, job_id, ws, "Job")
    cl = db.query(CoverLetter).filter(CoverLetter.job_id == job_id).first()
    if cl is None:
        raise HTTPException(status_code=404, detail="No cover letter drafted yet")
    return _letter_dict(cl)


@router.post("/{job_id}/cover-letter")
def draft_cover_letter(job_id: int, payload: DraftRequest, db: Session = Depends(get_db),
                       ws: int = Depends(current_workspace)):
    from backend.scorer import ScorerError, call_model, config_problem, get_config

    job = owned(db, Job, job_id, ws, "Job")
    cfg = get_config("cover_letter")
    if config_problem(cfg):
        raise HTTPException(status_code=400, detail=config_problem(cfg))
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(db, job, payload.instructions or "")}]
    try:
        text = _clean_letter(call_model(messages, {**cfg, "timeout_s": max(cfg["timeout_s"], 300)}, json_mode=False))
    except ScorerError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not text:
        raise HTTPException(status_code=502, detail="The model returned an empty letter; try again.")
    cl = db.query(CoverLetter).filter(CoverLetter.job_id == job_id).first() or CoverLetter(job_id=job_id)
    cl.text, cl.instructions, cl.edited = text, payload.instructions, False
    cl.model, cl.reasoning_effort = cfg["model"], cfg["reasoning_effort"] or None
    cl.created_at = cl.updated_at = datetime.utcnow()
    db.add(cl)
    db.commit()
    return _letter_dict(cl)


@router.put("/{job_id}/cover-letter")
def save_cover_letter(job_id: int, payload: SaveRequest, db: Session = Depends(get_db),
                      ws: int = Depends(current_workspace)):
    owned(db, Job, job_id, ws, "Job")
    cl = db.query(CoverLetter).filter(CoverLetter.job_id == job_id).first()
    if cl is None:
        cl = CoverLetter(job_id=job_id, created_at=datetime.utcnow())
        db.add(cl)
    cl.text, cl.edited, cl.updated_at = payload.text, True, datetime.utcnow()
    db.commit()
    return _letter_dict(cl)


@router.get("/{job_id}/cover-letter.docx")
def download_cover_letter(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    import docx

    job = owned(db, Job, job_id, ws, "Job")
    cl = db.query(CoverLetter).filter(CoverLetter.job_id == job_id).first()
    if cl is None:
        raise HTTPException(status_code=404, detail="No cover letter drafted yet")
    document = docx.Document()
    for para in re.split(r"\n\s*\n", cl.text.strip()):
        document.add_paragraph(para.strip())
    buf = io.BytesIO()
    document.save(buf)
    buf.seek(0)
    name = re.sub(r"[^\w\- ]+", "", f"Cover letter - {job.company} - {job.title}")[:120].strip() + ".docx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
