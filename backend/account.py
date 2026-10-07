"""The signed-in user's own account settings (accounts design, section 6.1 and Phase K).

    GET /api/account/prefs         {theme: <id> | null}: the theme chosen for this account, null when none yet
    PUT /api/account/prefs         {theme}: saves it, sealed under the user's key (``users.prefs_enc``)
    GET /api/account/onboarding    {step, path, features}: where the sign-up walkthrough stands
    PUT /api/account/onboarding    {step: n} remembers the step to show next; {done: true} ends the walkthrough

``users.onboarding_step`` is the number (counting from 1) of the walkthrough step to show next, NULL once the
walkthrough is finished or skipped. The page (frontend/welcome.html) builds its list of steps and saves the number
of the next one after each step, so a person who leaves comes back to the same place.

Two paths: the first admin, whose existing search settings moved into the account (``created_via`` first_run or
bootstrap, with titles already saved), gets the short one (choose a theme, then keep or review the settings); every
other new account gets the full one. A short-path person who asks to review moves past step 2, which makes the path
"full" from then on.

The theme is health-related information for some people (a dyslexia or colour-vision theme), so it is never kept as
plaintext; localStorage in the browser stays the fast path for the first paint (design 2.2).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend import crypto
from backend.db import User, UserProfile, Workspace, get_db
from backend.sessions import AuthUser, current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])

# The ids in THEMES (frontend/common.js); tests/test_wizard.py checks the two lists agree.
THEME_IDS = frozenset({
    "system", "dark", "light", "catppuccin-latte", "catppuccin-frappe", "catppuccin-macchiato", "catppuccin-mocha",
    "rose-pine", "rose-pine-moon", "rose-pine-dawn", "nord", "nord-light", "kanagawa-wave", "kanagawa-dragon",
    "kanagawa-lotus", "everforest-dark", "everforest-light", "dyslexia", "dyslexia-dark", "protanomaly",
    "protanomaly-dark", "deuteranomaly", "deuteranomaly-dark", "tritanomaly", "tritanomaly-dark", "dichromacy",
    "dichromacy-dark", "monochromacy", "monochromacy-dark",
})
MAX_STEP = 20
SHORT_PATH_LAST_STEP = 2  # on the short path: 1 theme, 2 "keep your current settings or review them"


def features() -> dict[str, bool]:
    """What this version offers the walkthrough and the account menu. The wizard shows the scheduled-search and
    digest options only when these are true: background access arrives in Phase G, digests in Phase J (each phase
    sets its flag to True when its code ships)."""
    return {"admin_panel": False, "scheduled_searches": False, "digests": False}


def _label(uid: int) -> str:
    return f"users.prefs_enc|{uid}"


def read_prefs(db: Session, user_id: int) -> dict[str, Any]:
    """The user's sealed preferences ({} when none). Needs the user's key scope."""
    user = db.get(User, user_id)
    if user is None or user.prefs_enc is None:
        return {}
    try:
        data = json.loads(crypto.unseal(user.prefs_enc, _label(user_id)))
    except (ValueError, crypto.CryptoError):
        log.warning("user %s: preferences could not be opened", user_id)
        return {}
    return data if isinstance(data, dict) else {}


def write_prefs(db: Session, user_id: int, prefs: dict[str, Any]) -> None:
    user = db.get(User, user_id)
    user.prefs_enc = crypto.seal(json.dumps(prefs, ensure_ascii=False).encode("utf-8"), _label(user_id))
    db.commit()


def public_prefs(db: Session, user_id: int) -> dict[str, Any]:
    theme = read_prefs(db, user_id).get("theme")
    return {"theme": theme if theme in THEME_IDS else None}


class PrefsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: str = Field(max_length=40)


@router.get("/prefs")
def get_prefs(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    return public_prefs(db, user.id)


@router.put("/prefs")
def put_prefs(body: PrefsIn, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    if body.theme not in THEME_IDS:
        raise HTTPException(status_code=422, detail="That isn't one of the themes JobHuntPA offers.")
    prefs = read_prefs(db, user.id)
    prefs["theme"] = body.theme
    write_prefs(db, user.id, prefs)
    return public_prefs(db, user.id)


def _has_titles(db: Session, user_id: int) -> bool:
    ws = db.query(Workspace.id).filter(Workspace.owner_id == user_id).order_by(Workspace.id).first()
    row = db.get(UserProfile, ws[0]) if ws else None
    return bool(row and row.titles)


def onboarding_info(db: Session, user_id: int) -> dict[str, Any]:
    user = db.get(User, user_id)
    step: Optional[int] = user.onboarding_step
    short = (step is not None and step <= SHORT_PATH_LAST_STEP and user.created_via in ("first_run", "bootstrap")
             and _has_titles(db, user_id))
    return {"step": step, "path": "short" if short else "full", "features": features()}


class OnboardingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: Optional[int] = Field(default=None, ge=1, le=MAX_STEP)
    done: bool = False


@router.get("/onboarding")
def get_onboarding(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    return onboarding_info(db, user.id)


@router.put("/onboarding")
def put_onboarding(body: OnboardingIn, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    if not body.done and body.step is None:
        raise HTTPException(status_code=422, detail="Send a step number, or done.")
    row = db.get(User, user.id)
    row.onboarding_step = None if body.done else body.step
    db.commit()
    return onboarding_info(db, user.id)
