"""Suggest job titles to search for, from the workspace's documents.

    POST /api/documents/suggest-titles   -> {titles: [{title, fit, reason}]}

fit is strong | good | stretch. Titles already in the search settings are
flagged ``in_search``. The UI merges picked titles into the current search
or starts a new workspace with them (existing profile/workspace endpoints).
Reasoning level: the "scoring" task's.
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import Document, get_db
from backend.profile import get_profile
from backend.workspaces import current_workspace

router = APIRouter(prefix="/api/documents", tags=["documents"])

SYSTEM_PROMPT = """You are a careers adviser. From the candidate's documents, suggest job titles they should search job boards for.

Return ONLY a JSON object:
{"titles": [{"title": "Technical Program Manager", "fit": "strong", "reason": "one sentence citing their experience"}]}

Rules:
- 10 to 15 titles, most suitable first. fit: "strong" (experience matches directly), "good" (clearly transferable), or "stretch" (plausible next step or pivot).
- Use the titles employers actually advertise (e.g. "Implementation Manager", not "Implementation Wizard"). Include common variants only when both are widely used (e.g. "Program Manager" and "Programme Manager").
- Keep seniority realistic for their years of experience; include a level up only as "stretch".
- Each reason must point to something in the documents; never invent experience.
- Don't suggest titles already in their search list (given below) unless it's the best-fitting title overall."""


def _parse(raw: str) -> list[dict]:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise HTTPException(status_code=502, detail="The model didn't return a list of titles; try again.")
    try:
        data = json.loads(re.sub(r",\s*([}\]])", r"\1", text[start:end + 1]))
    except ValueError:
        raise HTTPException(status_code=502, detail="The model returned malformed JSON; try again.")
    out, seen = [], set()
    for t in data.get("titles") or []:
        if not isinstance(t, dict):
            continue
        title = " ".join(str(t.get("title") or "").split())
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        fit = str(t.get("fit") or "").lower()
        out.append({"title": title, "fit": fit if fit in ("strong", "good", "stretch") else "good",
                    "reason": str(t.get("reason") or "").strip()})
    return out


@router.post("/suggest-titles")
def suggest_titles(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    from backend.scorer import ScorerError, build_profile, call_model, config_problem, get_config

    cfg = get_config("scoring")
    if config_problem(cfg):
        raise HTTPException(status_code=400, detail=config_problem(cfg))
    if not db.query(Document).filter(Document.workspace_id == ws, Document.use_for_scoring.is_(True)).count():
        raise HTTPException(status_code=400, detail="Upload your resume first (and tick 'use for scoring').")
    documents, _ = build_profile(db, ws)
    current = get_profile(db, ws)["titles"]
    user = ("CANDIDATE DOCUMENTS:\n<<<\n" + documents + "\n>>>\n\n"
            "TITLES ALREADY IN THEIR SEARCH: " + (", ".join(current) or "none") + "\n\nReturn only the JSON object.")
    try:
        raw = call_model([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}], cfg)
    except ScorerError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    current_l = {c.lower() for c in current}
    titles = [{**t, "in_search": t["title"].lower() in current_l} for t in _parse(raw)]
    return {"titles": titles, "current_titles": current}
