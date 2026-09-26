"""LLM settings made in the app (as opposed to .env): reasoning level per task.

Tasks: ``scoring`` (fit scores), ``research`` (company profiles) and
``cover_letter``. Each task's level is one of the levels the configured
model accepts, or "" for the model's default (the parameter isn't sent;
``LLM_REASONING_EFFORT`` in .env, if set, is used instead).

Which levels a model accepts differs by provider and model, so they are
detected: one tiny request per candidate level; the ones the API accepts
are stored per base URL + model.

    GET  /api/llm                 config, detected levels, level per task
    POST /api/llm/detect-levels   probe the configured model
    PUT  /api/llm/reasoning       {scoring?, research?, cover_letter?}
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db import AppSetting, SessionLocal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm", tags=["llm"])

TASKS = ("scoring", "research", "cover_letter")
CANDIDATE_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh")
DEFAULT_LEVELS = {"scoring": "low", "research": "low", "cover_letter": "medium"}


def _get(key: str, default: Any = None) -> Any:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        return json.loads(row.value_json) if row else default
    finally:
        db.close()


def _set(key: str, value: Any) -> None:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key) or AppSetting(key=key)
        row.value_json = json.dumps(value)
        db.merge(row)
        db.commit()
    finally:
        db.close()


def model_key(base_url: str, model: str) -> str:
    return f"{base_url.rstrip('/')}|{model}"


def supported_levels(base_url: str, model: str) -> Optional[list[str]]:
    """Levels detected for this model, or None if never detected."""
    return (_get("reasoning_levels", {}) or {}).get(model_key(base_url, model))


def task_levels() -> dict[str, str]:
    saved = _get("reasoning", None)
    if saved is None:
        return dict(DEFAULT_LEVELS)
    return {t: saved.get(t, "") for t in TASKS}


def effort_for(task: Optional[str], base_url: str, model: str) -> str:
    """Reasoning effort to send for a task ("" = don't send)."""
    env_default = (os.getenv("LLM_REASONING_EFFORT") or "").strip()
    if task is None:
        return env_default
    level = task_levels().get(task, "")
    levels = supported_levels(base_url, model)
    if level and (levels is None or level in levels):
        return level
    # No level chosen, or the chosen one isn't accepted by this model.
    return env_default if (levels is None or env_default in levels) else ""


def detect_levels(cfg: dict) -> dict:
    """Send one tiny request per candidate level; keep the accepted ones."""
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    url = f"{cfg['base_url']}/chat/completions"
    accepted, rejected = [], {}
    for level in CANDIDATE_LEVELS:
        payload = {"model": cfg["model"], "reasoning_effort": level,
                   "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=90)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Could not reach {cfg['base_url']}: {exc}")
        if resp.status_code in (401, 403):
            raise HTTPException(status_code=502, detail=f"API key rejected (HTTP {resp.status_code}).")
        if resp.status_code == 200:
            accepted.append(level)
        else:
            rejected[level] = f"HTTP {resp.status_code}: {resp.text[:160]}"
    stored = _get("reasoning_levels", {}) or {}
    stored[model_key(cfg["base_url"], cfg["model"])] = accepted
    _set("reasoning_levels", stored)
    return {"levels": accepted, "rejected": rejected}


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def _state() -> dict:
    from backend.scorer import config_problem, get_config

    cfg = get_config()
    return {
        "configured": config_problem(cfg) is None,
        "problem": config_problem(cfg),
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "env_default": (os.getenv("LLM_REASONING_EFFORT") or "").strip(),
        "levels": supported_levels(cfg["base_url"], cfg["model"]),
        "reasoning": task_levels(),
        "effective": {t: effort_for(t, cfg["base_url"], cfg["model"]) for t in TASKS},
    }


@router.get("")
def read_llm_settings():
    return _state()


@router.post("/detect-levels")
def detect_llm_levels():
    from backend.scorer import config_problem, get_config

    cfg = get_config()
    if config_problem(cfg):
        raise HTTPException(status_code=400, detail=config_problem(cfg))
    result = detect_levels(cfg)
    return {**_state(), "rejected": result["rejected"]}


class ReasoningUpdate(BaseModel):
    scoring: Optional[str] = None
    research: Optional[str] = None
    cover_letter: Optional[str] = None


@router.put("/reasoning")
def update_reasoning(payload: ReasoningUpdate):
    current = task_levels()
    for task in TASKS:
        value = getattr(payload, task)
        if value is None:
            continue
        if value and value not in CANDIDATE_LEVELS:
            raise HTTPException(status_code=422, detail=f"Unknown reasoning level {value!r}")
        current[task] = value
    _set("reasoning", current)
    return _state()
