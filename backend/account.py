"""The signed-in user's own account settings (accounts design, section 6.1 and Phase K).

Every route needs a signed-in session; deleting also needs recent sign-in.

    GET /api/account/prefs         {theme: <id> | null}: the theme chosen for this account, null when none yet
    PUT /api/account/prefs         {theme}: saves it, sealed under the user's key (``users.prefs_enc``)
    GET /api/account/onboarding    {step, path, features}: where the sign-up walkthrough stands
    PUT /api/account/onboarding    {step: n} remembers the step to show next; {done: true} ends the walkthrough
    GET /api/account/delete        {allowed}: false for the install's only account
    POST /api/account/delete       {password} (recent sign-in): deletes the account and all its data, except the
                                   install's only account (409 only_account: the reset tool, run on the computer
                                   itself, is the way to start over; nothing over the web wipes the install)

``users.onboarding_step`` is the number (counting from 1) of the walkthrough step to show next, NULL once the
walkthrough is finished or skipped. The page (frontend/welcome.html) builds its list of steps and saves the number
of the next one after each step, so a person who leaves comes back to the same place.

Two paths: the first account, whose existing search settings moved into it (``created_via`` first_run or
bootstrap, with titles already saved), gets the short one (the AI provider, a theme, then keep or review the
settings); every other new account gets the full one. Both start with the AI provider (the first run's step 3, spec
§1.5). A short-path person who asks to review moves past step 3, which makes the path "full" from then on.

The theme is health-related information for some people (a dyslexia or colour-vision theme), so it is never kept as
plaintext; localStorage in the browser stays the fast path for the first paint (design 2.2).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend import crypto, sessions, users
from backend.db import User, UserProfile, Workspace, get_db
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])

# The ids in THEMES (frontend/common.js); tests/test_wizard.py checks the two lists agree.
THEME_IDS = frozenset({
    "system", "vanilla-dark", "vanilla-light", "dark", "light", "catppuccin-latte", "catppuccin-frappe",
    "catppuccin-macchiato", "catppuccin-mocha", "rose-pine", "rose-pine-moon", "rose-pine-dawn", "nord", "nord-light",
    "kanagawa-wave", "kanagawa-dragon", "kanagawa-lotus", "everforest-dark", "everforest-light", "dyslexia",
    "dyslexia-dark", "protanomaly", "protanomaly-dark", "deuteranomaly", "deuteranomaly-dark", "tritanomaly",
    "tritanomaly-dark", "dichromacy", "dichromacy-dark", "monochromacy", "monochromacy-dark",
})
MAX_STEP = 20
SHORT_PATH_LAST_STEP = 3  # on the short path: 1 AI provider, 2 theme, 3 "keep your current settings or review them"


def features() -> dict[str, bool]:
    """What this version offers the walkthrough. The wizard shows the scheduled-search option only when
    ``scheduled_searches`` is true (background access came in Phase G)."""
    return {"scheduled_searches": True}


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


class DeleteAccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field("", max_length=4096)


ONLY_ACCOUNT = ("This is the only account on this JobHuntPA, so it can't be deleted here. To start over, download "
                "your data first, then run the reset tool on the computer JobHuntPA runs on (see the README).")


def _only_account() -> AuthProblem:
    return AuthProblem(409, "only_account", ONLY_ACCOUNT)


@router.get("/delete")
def can_delete(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Signed in: whether this account can be deleted here (not the install's only one)."""
    return {"allowed": not users.is_only_account(db, user.id)}


@router.post("/delete")
def delete_my_account(body: DeleteAccountIn, request: Request, user: AuthUser = Depends(current_user),
                      db: Session = Depends(get_db)):
    """Recent sign-in, and the password once more here. Deletes the signed-in account and everything in it (design
    §6.8; the work is ``users.delete_account``): only on an older install that holds other accounts besides it. The
    install's only account is refused (409 ``only_account``)."""
    from backend.security import confirm_password  # (security imports auth, which imports this module)

    require_recent_auth(user)
    if users.is_only_account(db, user.id):
        raise _only_account()
    confirm_password(request, db, user, body.password)
    try:
        users.delete_account(db, user.id)
    except users.AccountBusy:
        raise AuthProblem(409, "run_active", "A search is still running. Wait for it to finish, then try again.") from None
    except users.AccountProtected:
        raise _only_account() from None
    response = JSONResponse({"ok": True})
    sessions.clear_cookie(response, request.scope)
    response.headers["Clear-Site-Data"] = '"cache"'
    return response
