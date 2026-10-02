"""LLM settings made in the app (as opposed to .env): reasoning level per task.

Tasks: ``scoring`` (fit scores), ``research`` (company profiles),
``cover_letter`` and ``chat`` (questions about jobs). Each task's level is one of the levels the configured
model accepts, or "" for the model's default (the parameter isn't sent;
``LLM_REASONING_EFFORT`` in .env, if set, is used instead).

Which levels a model accepts differs by provider and model, so they are
detected: one tiny request per candidate level; the ones the API accepts
are stored per base URL + model. The candidates depend on the provider
(Claude takes low, medium, high, xhigh and max; see backend/llm_providers.py).

    GET  /api/llm                 config, detected levels, level per task
    POST /api/llm/detect-levels   probe the configured model
    PUT  /api/llm/reasoning       {scoring?, research?, cover_letter?, chat?}
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx  # noqa: F401  (tests patch llm_settings.httpx; the probes are sent by backend/llm_providers.py)
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import llm_providers
from backend.db import AppSetting, SessionLocal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm", tags=["llm"])

TASKS = ("scoring", "research", "cover_letter", "chat")
CANDIDATE_LEVELS = llm_providers.LEVELS_OPENAI
ALL_LEVELS = tuple(dict.fromkeys(llm_providers.LEVELS_OPENAI + llm_providers.LEVELS_ANTHROPIC))  # incl. Claude's max
DEFAULT_LEVELS = {"scoring": "low", "research": "low", "cover_letter": "medium", "chat": "low"}


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
    # Tasks added after the levels were saved start at their default.
    return {t: saved.get(t, DEFAULT_LEVELS[t]) for t in TASKS}


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
    accepted, rejected = [], {}
    for level in llm_providers.candidate_levels(cfg):
        try:
            why_not = llm_providers.probe_level(cfg, level)
        except llm_providers.ScorerError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        if why_not is None:
            accepted.append(level)
        else:
            rejected[level] = why_not
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
        "provider": llm_providers.provider_for(cfg["base_url"]),
        "candidates": list(llm_providers.candidate_levels(cfg)),  # what "Detect levels" will try
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
    chat: Optional[str] = None


@router.put("/reasoning")
def update_reasoning(payload: ReasoningUpdate):
    current = task_levels()
    for task in TASKS:
        value = getattr(payload, task)
        if value is None:
            continue
        if value and value not in ALL_LEVELS:
            raise HTTPException(status_code=422, detail=f"Unknown reasoning level {value!r}")
        current[task] = value
    _set("reasoning", current)
    return _state()
