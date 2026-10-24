"""Cover letters per job: drafted by the LLM from the user's documents, or put together from the user's own
paragraphs (backend.letter_blocks).

    GET  /api/jobs/{id}/cover-letter                  saved letter (404 if none)
    POST /api/jobs/{id}/cover-letter                  draft a new one {instructions?}
    PUT  /api/jobs/{id}/cover-letter                  save edits {text} (the model's draft stays in ``draft_text``)
    GET  /api/jobs/{id}/cover-letter.pdf              download as PDF (X-Font-Substitutions says which fonts stood in)
    GET  /api/jobs/{id}/cover-letter.odt              download as OpenDocument
    GET  /api/jobs/{id}/cover-letter.docx             download as Word
    POST /api/jobs/{id}/cover-letter/blocks/suggest   the AI picks and orders blocks, and names blocks worth writing
    POST /api/jobs/{id}/cover-letter/blocks/assemble  {block_ids}: the blocks' text verbatim, placeholders filled

All of them: signed in, the job's workspace. Downloads use the workspace's letter template when it has one
(backend/letter_files.py; chosen on the Documents tab, kept in ``workspaces.letter_template_id``): a .docx template
gives .docx and PDF, an .odt template .odt and PDF, and a format the template can't give answers 409. Without a
template every format comes from the plain built-in layout. The PDF is LibreOffice's conversion of the filled file
when LibreOffice is installed, else drawn from it with reportlab (backend/letter_pdf.py ``pdf_backend``).

The draft prompt gets the resume(s) first, the candidate's own paragraphs (blocks) as material when there are any,
earlier cover letters (or else notes) as style samples, the job ad, the fit result (matched evidence and gaps) and the
company profile when there is one. Reasoning level: task "cover_letter". Everything taken from the model goes through
``textclean.plain_text``; the user's block text never does (an assembled letter keeps their words exactly).

Preference documents are never sent: they are the candidate's private wishes
(pay, place, employers to avoid) and must not end up in a letter to an employer.
"""
from __future__ import annotations

import json
import logging
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
    NOTE_FILETYPE,
    CompanyProfile,
    CoverLetter,
    Document,
    FitResult,
    Job,
    LetterBlock,
    Workspace,
    company_key,
    get_db,
)
from backend.letter_blocks import BLOCK_KINDS, _block_dict, _ordered, assemble_letter
from backend.textclean import plain_text
from backend.workspaces import current_workspace, owned

log = logging.getLogger(__name__)

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
- Write in the candidate's own voice: study their paragraphs, earlier cover letters and notes for sentence length, rhythm, word choice, how formal they are and how they talk about results, and imitate that as closely as you can; prefer their phrasings to your own; never introduce vocabulary they wouldn't use.
- When the candidate's own paragraphs are provided, build the letter from them: use their sentences as written where they fit the job, fill their placeholders ({company} etc.), write only the transitions and anything the paragraphs don't cover (still only from the documents), and keep their voice.
- Follow the candidate's extra instructions when given, unless they conflict with the no-invention rule.

Return only the letter text, starting with the salutation. No title, notes or markdown."""

BLOCKS_SYSTEM_PROMPT = """You choose which of the candidate's pre-written paragraphs ("blocks") fit this job; you never rewrite them.

Return JSON: {"blocks": [{"id": <the block's number>, "why": "<short reason>"}], "missing": [{"title": "...", "requirement": "...", "bullets": ["..."]}]}

- "blocks", in reading order: one opening, 2-4 body blocks covering the ad's most important requirements, one closing. Skip blocks whose claims the job doesn't call for. Use only ids from the BLOCKS list.
- "missing": requirements of the ad that the candidate's documents (or the fit analysis) show real experience for but that no block covers. For each, a short title for the paragraph to write, the requirement it answers, and 2-5 bullets of the evidence as facts taken from the documents (what, where, scale, result): never suggested wording, never anything the documents don't show. An empty list when every evidenced requirement is covered.
- Plain text only in every string: no markdown."""


class DraftRequest(BaseModel):
    instructions: Optional[str] = Field(default=None, max_length=2000)


class SaveRequest(BaseModel):
    text: str = Field(max_length=20000)


class AssembleRequest(BaseModel):
    block_ids: list[int] = Field(max_length=40)


def _letter_dict(cl: CoverLetter) -> dict:
    return {
        "job_id": cl.job_id,
        "text": cl.text,
        "draft_text": cl.draft_text,  # the model's draft as written (None for a letter from before this was kept)
        "instructions": cl.instructions,
        "edited": bool(cl.edited),
        "model": cl.model,
        "reasoning_effort": cl.reasoning_effort,
        "source": cl.source,
        "created_at": cl.created_at.isoformat() if cl.created_at else None,
        "updated_at": cl.updated_at.isoformat() if cl.updated_at else None,
    }


def _documents_section(db: Session, workspace_id: int, required: bool = True) -> tuple[list[str], list[tuple[str, str]]]:
    """(the documents' prompt parts, style samples as (filename, text)). Resumes first, then other documents, each
    as chosen on the Documents tab; never preferences. The style samples are up to 2 earlier cover letters as written
    (a concise version loses the voice), else up to 2 notes. 400 when ``required`` and there is nothing to go on."""
    from backend.scorer import document_heading

    docs = [d for d in (db.query(Document)
                        .filter(Document.workspace_id == workspace_id, Document.use_for_scoring.is_(True))
                        .order_by(Document.id))
            if d.kind not in ("preferences", LETTER_TEMPLATE_KIND)]  # private wishes; a letterhead is not a document
    resumes = [d for d in docs if d.kind == "resume" and d.text_for_llm()]
    letters = [d for d in docs if d.kind == "cover_letter" and (d.text or "").strip()]
    others = [d for d in docs if d.kind not in ("resume", "cover_letter") and d.text_for_llm()]
    if required and not resumes and not others:
        raise HTTPException(status_code=400, detail="Upload your resume on the Documents tab first.")
    parts = [f"{document_heading(d)}\n{d.text_for_llm()}" for d in resumes + others]
    style = [(d.filename, d.text.strip()[:6000]) for d in letters[:2]]
    if not style:
        notes = [d for d in others if d.kind == "other" and d.filetype == NOTE_FILETYPE and (d.text or "").strip()]
        style = [(d.filename, d.text.strip()[:1500]) for d in notes[:2]]
    return parts, style


def _job_section(job: Job) -> str:
    lines = [f"Title: {job.title}", f"Company: {job.company}", f"Location: {job.location_text or 'not stated'}"]
    if job.detail_status == "summary":
        lines.append("Note: only the job board's listing summary is available.")
    lines += ["", job.description or "(no description)"]
    return "=== JOB AD ===\n" + "\n".join(lines)


def _fit_section(db: Session, job: Job) -> Optional[str]:
    fit = db.query(FitResult).filter(FitResult.job_id == job.id).first()
    if not (fit and fit.status == "ok"):  # checked here: the status is sealed (accounts design §14.6)
        return None
    data = json.loads(fit.evidence_json or "{}")
    matched = [f"- {r['point']}: " + "; ".join(e["bullet"] for e in r.get("evidence", []))
               for r in data.get("requirements", []) if r.get("matched")]
    gaps = [f"- {g}" for g in data.get("gaps", [])]
    return ("=== FIT ANALYSIS (from an earlier assessment) ===\nEvidenced requirements:\n"
            + ("\n".join(matched) or "- none") + "\nGaps:\n" + ("\n".join(gaps) or "- none"))


def _workspace_blocks(db: Session, workspace_id: int) -> list[LetterBlock]:
    """The workspace's blocks, openings first, closings last, each kind in the user's order (sorted in Python)."""
    return _ordered(db.query(LetterBlock).filter(LetterBlock.workspace_id == workspace_id).all())


def build_prompt(db: Session, job: Job, instructions: str = "") -> str:
    parts, style = _documents_section(db, job.workspace_id)
    blocks = [b for b in _workspace_blocks(db, job.workspace_id) if (b.text or "").strip()]
    if blocks:
        parts.append("=== CANDIDATE'S OWN PARAGRAPHS ===\n"
                     + "\n\n".join(f"[{b.kind}] {b.title or '(untitled)'}\n{b.text.strip()}" for b in blocks))
    for filename, text in style:
        parts.append(f"=== HOW THE CANDIDATE WRITES (style only): {filename} ===\n{text}")
    parts.append(_job_section(job))
    fit = _fit_section(db, job)
    if fit:
        parts.append(fit)

    profile = db.get(CompanyProfile, company_key(job.company or ""))
    if profile and profile.status == "done" and not profile.is_recruiter:
        parts.append("=== ABOUT THE EMPLOYER (researched) ===\n"
                     f"Business: {profile.business_model or 'unknown'}\nHeadquarters: {profile.headquarters or 'unknown'}")
    if instructions.strip():
        parts.append("=== CANDIDATE'S EXTRA INSTRUCTIONS ===\n" + instructions.strip())
    return "\n\n".join(parts)


def build_blocks_prompt(db: Session, job: Job, blocks: list[LetterBlock]) -> str:
    """What the model gets to pick blocks: the blocks (or "(none yet)"), the documents, the job ad and the fit."""
    lines = []
    for b in blocks:
        tags = ", ".join(b.tags or [])
        lines.append(f"[{b.id}] ({b.kind}) {b.title or '(untitled)'}" + (f" - tags: {tags}" if tags else "")
                     + f"\n{(b.text or '').strip()}")
    parts = ["=== BLOCKS ===\n" + ("\n\n".join(lines) or "(none yet)")]
    parts += _documents_section(db, job.workspace_id, required=False)[0]
    parts.append(_job_section(job))
    fit = _fit_section(db, job)
    if fit:
        parts.append(fit)
    return "\n\n".join(parts)


def _clean_letter(text: str) -> str:
    text = (text or "").strip()
    fence = re.match(r"^```\w*\n(.*?)\n```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    return plain_text(text)


_UNUSABLE = "The model's answer was unusable; pick the blocks yourself."


def _answer_object(raw: str) -> dict:
    """The JSON object the model answered (a fence and trailing commas allowed); 502 when it isn't one."""
    text = (raw or "").strip()
    fence = re.match(r"^```\w*\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(re.sub(r",\s*([}\]])", r"\1", text))
    except (ValueError, RecursionError):
        start, end = text.find("{"), text.rfind("}")  # prose around the object
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", text[start:end + 1])) if 0 <= start < end else None
        except (ValueError, RecursionError):
            data = None
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=_UNUSABLE)
    return data


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _clip(value, limit: int) -> str:
    return plain_text(value)[:limit].strip() if isinstance(value, str) else ""


def parse_suggestion(data: dict, blocks: list[LetterBlock]) -> dict:
    """The model's pick, checked: only this workspace's blocks, each once, openings first and closings last (at most
    one of each, the first wins), the model's order kept within a kind; ``missing`` cleaned and bounded."""
    by_id = {b.id: b for b in blocks}
    picked: list[tuple[LetterBlock, str]] = []
    seen: set[int] = set()
    for entry in _list(data.get("blocks")):
        if not isinstance(entry, dict):
            continue
        bid = entry.get("id")
        if isinstance(bid, bool) or not isinstance(bid, int) or bid not in by_id or bid in seen:
            continue
        b = by_id[bid]
        if b.kind in ("opening", "closing") and any(p.kind == b.kind for p, _ in picked):
            continue
        seen.add(bid)
        picked.append((b, _clip(entry.get("why"), 200)))
    picked.sort(key=lambda p: BLOCK_KINDS.index(p[0].kind) if p[0].kind in BLOCK_KINDS else 1)  # stable
    missing = []
    for entry in _list(data.get("missing")):
        if not isinstance(entry, dict):
            continue
        title = _clip(entry.get("title"), 120)
        if not title:
            continue
        bullets = [_clip(x, 300) for x in _list(entry.get("bullets"))]  # (anything but a string becomes "")
        missing.append({"title": title, "requirement": _clip(entry.get("requirement"), 120),
                        "bullets": [x for x in bullets if x][:6]})
        if len(missing) == 8:
            break
    return {"picked": [{"id": b.id, "why": why} for b, why in picked],
            "unpicked": [b.id for b in blocks if b.id not in seen],
            "missing": missing}


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
    cl.source = "ai"
    cl.created_at = cl.updated_at = datetime.utcnow()
    db.add(cl)
    db.commit()
    return _letter_dict(cl)


@router.post("/{job_id}/cover-letter/blocks/suggest")
def suggest_blocks(job_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    from backend.scorer import ScorerError, call_model, config_problem, get_config

    job = owned(db, Job, job_id, ws, "Job")
    cfg = {**get_config("cover_letter"), "workspace_id": ws}
    if config_problem(cfg):
        raise HTTPException(status_code=400, detail=config_problem(cfg))
    blocks = _workspace_blocks(db, ws)
    if not blocks and not _documents_section(db, ws, required=False)[0]:
        raise HTTPException(status_code=400, detail="Write a block or upload your resume on the Documents tab first.")
    messages = [{"role": "system", "content": BLOCKS_SYSTEM_PROMPT},
                {"role": "user", "content": build_blocks_prompt(db, job, blocks)}]
    try:
        with tasks.track("cover_letter", ws, jobs=[job_id]):
            raw = call_model(messages, {**cfg, "timeout_s": max(cfg["timeout_s"], 300)}, json_mode=True)
    except ScorerError as exc:
        raise HTTPException(status_code=502, detail=llm_providers.user_message(exc))
    return {**parse_suggestion(_answer_object(raw), blocks), "blocks": [_block_dict(b) for b in blocks]}


@router.post("/{job_id}/cover-letter/blocks/assemble")
def assemble_from_blocks(job_id: int, payload: AssembleRequest, db: Session = Depends(get_db),
                         ws: int = Depends(current_workspace)):
    job = owned(db, Job, job_id, ws, "Job")
    if not payload.block_ids:
        raise HTTPException(status_code=400, detail="Tick at least one block.")
    blocks = [owned(db, LetterBlock, i, ws, "Block") for i in dict.fromkeys(payload.block_ids)]
    text, unfilled = assemble_letter(blocks, job)
    cl = db.query(CoverLetter).filter(CoverLetter.job_id == job_id).first() or CoverLetter(job_id=job_id)
    cl.text, cl.draft_text, cl.instructions, cl.edited = text, None, None, False  # (no model draft to go back to)
    cl.model, cl.reasoning_effort = None, None
    cl.source = "blocks"
    cl.created_at = cl.updated_at = datetime.utcnow()
    db.add(cl)
    db.commit()
    return {**_letter_dict(cl), "unfilled": unfilled}


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
    gives, the font the letter takes, which of the template's fonts this computer lacks (what reportlab stands in
    with), and what makes the PDF (``pdf``: "libreoffice" or "reportlab")."""
    template = current_template(db, ws)
    out = {"template": None, "formats": {}, "fonts": [], "letter_font": None, "problem": None,
           "pdf": letter_pdf.pdf_backend()}
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
    values = letter_files.marker_values(text, job.company or "", job.title or "", location=job.location_text or "")
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
    if letter_pdf.pdf_backend() == "libreoffice":
        try:
            return letter_pdf.to_pdf_libreoffice(filled, suffix), []
        except letter_pdf.PdfError as exc:  # (how it failed, never a name or text; reportlab draws it instead)
            log.warning("LibreOffice could not turn a cover letter into a PDF: %s", exc)
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
