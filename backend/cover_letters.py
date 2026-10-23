"""Cover-letter drafts per job, written by the LLM from the user's documents.

    GET  /api/jobs/{id}/cover-letter        saved draft (404 if none)
    POST /api/jobs/{id}/cover-letter        draft a new one {instructions?}
    PUT  /api/jobs/{id}/cover-letter        save edits {text} (the model's draft stays in ``draft_text``)
    GET  /api/jobs/{id}/cover-letter.pdf    download as PDF (X-Font-Substitutions says which fonts stood in)
    GET  /api/jobs/{id}/cover-letter.odt    download as OpenDocument
    GET  /api/jobs/{id}/cover-letter.docx   download as Word

All of them: signed in, the job's workspace. Downloads use the workspace's letter template when it has one
(backend/letter_files.py; chosen on the Documents tab, kept in ``workspaces.letter_template_id``): a .docx template
gives .docx and PDF, an .odt template .odt and PDF, and a format the template can't give answers 409. Without a
template every format comes from the plain built-in layout. The PDF is drawn from the filled file
(backend/letter_pdf.py).

The prompt gets the resume(s) first, earlier cover letters as style
samples, the job ad, the fit result (matched evidence and gaps) and the
company profile when there is one. Reasoning level: task "cover_letter".

Preference documents are never sent: they are the candidate's private wishes
(pay, place, employers to avoid) and must not end up in a letter to an employer.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import crypto, letter_files, letter_pdf, llm_providers, tasks
from backend.db import (
    LETTER_TEMPLATE_KIND,
    CompanyProfile,
    CoverLetter,
    Document,
    FitResult,
    Job,
    Workspace,
    company_key,
    get_db,
)
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
        "draft_text": cl.draft_text,  # the model's draft as written (None for a letter from before this was kept)
        "instructions": cl.instructions,
        "edited": bool(cl.edited),
        "model": cl.model,
        "reasoning_effort": cl.reasoning_effort,
        "created_at": cl.created_at.isoformat() if cl.created_at else None,
        "updated_at": cl.updated_at.isoformat() if cl.updated_at else None,
    }


def build_prompt(db: Session, job: Job, instructions: str = "") -> str:
    from backend.scorer import document_heading

    docs = [d for d in (db.query(Document)
                        .filter(Document.workspace_id == job.workspace_id, Document.use_for_scoring.is_(True))
                        .order_by(Document.id))
            if d.kind not in ("preferences", LETTER_TEMPLATE_KIND)]  # private wishes; a letterhead is not a document
    resumes = [d for d in docs if d.kind == "resume" and d.text_for_llm()]
    letters = [d for d in docs if d.kind == "cover_letter" and (d.text or "").strip()]
    others = [d for d in docs if d.kind not in ("resume", "cover_letter") and d.text_for_llm()]
    if not resumes and not others:
        raise HTTPException(status_code=400, detail="Upload your resume on the Documents tab first.")
    parts = []
    for d in resumes + others:  # each as chosen on the Documents tab (original or concise)
        parts.append(f"{document_heading(d)}\n{d.text_for_llm()}")
    for d in letters[:2]:  # style samples: the letter as written, as a concise version loses the voice
        parts.append(f"=== EARLIER COVER LETTER (style sample only): {d.filename} ===\n{d.text.strip()[:6000]}")

    job_lines = [f"Title: {job.title}", f"Company: {job.company}", f"Location: {job.location_text or 'not stated'}"]
    if job.detail_status == "summary":
        job_lines.append("Note: only the job board's listing summary is available.")
    job_lines += ["", job.description or "(no description)"]
    parts.append("=== JOB AD ===\n" + "\n".join(job_lines))

    fit = db.query(FitResult).filter(FitResult.job_id == job.id).first()
    if fit and fit.status == "ok":  # checked here: the status is sealed (accounts design §14.6)
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
    cfg = {**get_config("cover_letter"), "workspace_id": ws}
    if config_problem(cfg):
        raise HTTPException(status_code=400, detail=config_problem(cfg))
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(db, job, payload.instructions or "")}]
    try:
        with tasks.track("cover_letter", ws, jobs=[job_id]):
            text = _clean_letter(call_model(messages, {**cfg, "timeout_s": max(cfg["timeout_s"], 300)},
                                            json_mode=False))
    except ScorerError as exc:
        raise HTTPException(status_code=502, detail=llm_providers.user_message(exc))
    if not text:
        raise HTTPException(status_code=502, detail="The model returned an empty letter; try again.")
    cl = db.query(CoverLetter).filter(CoverLetter.job_id == job_id).first() or CoverLetter(job_id=job_id)
    cl.text, cl.draft_text, cl.instructions, cl.edited = text, text, payload.instructions, False
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
    # Saving the model's draft back word for word ("Discard my edits") makes the letter the draft again.
    cl.text, cl.edited, cl.updated_at = payload.text, payload.text != (cl.draft_text or object()), datetime.utcnow()
    db.commit()
    return _letter_dict(cl)


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------

FORMATS = {
    "pdf": ("application/pdf", "PDF"),
    "odt": ("application/vnd.oasis.opendocument.text", "OpenDocument (.odt)"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word (.docx)"),
}


def current_template(db: Session, ws: int) -> Optional[Document]:
    """The workspace's letter template, or None for the plain layout. Checked through plaintext columns before the
    row is loaded; a pointer to a document that is gone or no longer a template counts as none."""
    template_id = db.query(Workspace.letter_template_id).filter(Workspace.id == ws).scalar()
    if not template_id:
        return None
    row = db.query(Document.workspace_id, Document.kind, Document.stored_name).filter(Document.id == template_id).first()
    if row is None or row[0] != ws or row[1] != LETTER_TEMPLATE_KIND or not row[2]:
        return None
    return db.get(Document, template_id)


def template_suffix(template: Document) -> str:
    return (template.filetype or "").lower()


def format_problem(fmt: str, template: Optional[Document]) -> Optional[str]:
    """Why ``fmt`` can't be made with the current template (None: it can)."""
    if template is None or fmt == "pdf":
        return None
    suffix = template_suffix(template)
    if fmt == "docx" and suffix == ".odt":
        return ("Your letter template is an OpenDocument (.odt) file, which gives .odt and PDF. For a Word file, upload "
                "a .docx template or switch to the plain layout on the Documents tab.")
    if fmt == "odt" and suffix == ".docx":
        return ("Your letter template is a Word (.docx) file, which gives .docx and PDF. For an OpenDocument file, "
                "upload an .odt template or switch to the plain layout on the Documents tab.")
    return None


def export_options(db: Session, ws: int) -> dict:
    """What the cover-letter window shows about downloads: the template (or the plain layout), which formats it
    gives, the font the letter takes and which of the template's fonts this computer lacks."""
    template = current_template(db, ws)
    out = {"template": None, "formats": {}, "fonts": [], "letter_font": None, "problem": None}
    if template is not None:
        out["template"] = {"id": template.id, "filename": template.filename, "filetype": template_suffix(template)}
        try:
            model = letter_files.check_template(_template_bytes(template), template_suffix(template))
            out["fonts"] = letter_pdf.font_report(model.families())
            font = letter_files.marker_font(model)
            out["letter_font"] = {"family": font[0], "size": font[1]} if font else None
        except HTTPException as exc:
            out["problem"] = exc.detail
    else:
        font = letter_pdf.plain_font()
        out["letter_font"] = {"family": font, "size": letter_files.DEFAULT_SIZE}
    for fmt, (_media, label) in FORMATS.items():
        problem = out["problem"] or format_problem(fmt, template)
        out["formats"][fmt] = {"label": label, "available": problem is None, "reason": problem}
    return out


def _template_bytes(template: Document) -> bytes:
    from backend import docs

    try:
        return docs.read_upload(template.stored_name)
    except (OSError, crypto.CryptoError):
        raise HTTPException(status_code=409, detail="The letter template's file can't be opened. Upload it again on "
                                                    "the Documents tab, or switch to the plain layout.")


def build_file(db: Session, ws: int, job: Job, text: str, fmt: str) -> tuple[bytes, list[dict]]:
    """The letter as ``fmt`` with the workspace's template or the plain layout: (bytes, font substitutions)."""
    template = current_template(db, ws)
    problem = format_problem(fmt, template)
    if problem:
        raise HTTPException(status_code=409, detail=problem)
    values = letter_files.marker_values(text, job.company or "", job.title or "")
    if template is not None:
        suffix = template_suffix(template)
        try:
            filled = letter_files.fill(_template_bytes(template), suffix, text, values)
        except letter_files.TemplateError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception:
            raise HTTPException(status_code=409, detail="The letter template couldn't be filled. Save it again from "
                                                        "your word processor and upload it once more.")
    else:
        suffix = ".odt" if fmt == "odt" else ".docx"
        filled = letter_files.plain(suffix, text, letter_pdf.plain_font())
    if fmt != "pdf":
        return filled, []
    return letter_pdf.render(letter_files.letter_model(filled, suffix))


def _download(job_id: int, fmt: str, db: Session, ws: int) -> Response:
    job = owned(db, Job, job_id, ws, "Job")
    cl = db.query(CoverLetter).filter(CoverLetter.job_id == job_id).first()
    if cl is None:
        raise HTTPException(status_code=404, detail="No cover letter drafted yet")
    data, substitutions = build_file(db, ws, job, cl.text, fmt)
    name = re.sub(r"[^\w\- ]+", "", f"Cover letter - {job.company} - {job.title}")[:120].strip() + f".{fmt}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if fmt == "pdf":
        headers["X-Font-Substitutions"] = json.dumps(substitutions, ensure_ascii=True)
    return Response(content=data, media_type=FORMATS[fmt][0], headers=headers)


@router.get("/{job_id}/cover-letter.pdf")
def download_cover_letter_pdf(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    return _download(job_id, "pdf", db, ws)


@router.get("/{job_id}/cover-letter.odt")
def download_cover_letter_odt(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    return _download(job_id, "odt", db, ws)


@router.get("/{job_id}/cover-letter.docx")
def download_cover_letter(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    return _download(job_id, "docx", db, ws)
