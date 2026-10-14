"""The admin panel's API (accounts design §7 and §14.8). Every route needs an admin (403 otherwise).

    GET    /api/admin/stats?days=30                       people and LLM usage figures
    GET    /api/admin/users                               everyone, with the details only admins can open
    POST   /api/admin/users/{id}/role                     {role: admin|user}  (password entered within 15 minutes)
    POST   /api/admin/users/{id}/regrant                  share the admin key with an admin again (recent password)
    POST   /api/admin/users/{id}/disable | enable
    DELETE /api/admin/users/{id}                          removes the account and all its data (recent password)
    GET    /api/admin/signup   PUT /api/admin/signup      {mode: invite|open|closed}
    POST   /api/admin/invites {email?}                    -> {link, ...}; GET /api/admin/invites; DELETE .../{id}
    GET    /api/admin/audit?limit=&before_id=             what admins did
    GET    /api/admin/plaintext-audit                     plaintext left from before encryption
    POST   /api/admin/plaintext-files/delete              (recent password)
    POST   /api/admin/keys/rotate                         replace the server key (recent password)

**Admin routes never use ``current_workspace`` and never read a user's own sealed data.** They read plaintext columns
(role, status, dates, counts) and values sealed to the admin details key (backend/admin_key.py), which each request
opens for itself through the signed-in admin's own grant and keeps nowhere. The request's key scope is the admin's
own, so a slip that tried to open another person's sealed row would fail with KeyMismatch (a 404), not leak it.

**Every action writes an ``admin_audit`` row** in the same transaction, its detail sealed to the admin details key.
Details say what was done (role, dates, counts), never anyone's content.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

from backend import admin_key, crypto, key_rotation, keys, lockout, migrate_encryption, public_url, signup, tokens, users
from backend.admin_key import Apk, ApkUnavailable
from backend.db import (
    AdminAudit,
    AdminKeyGrant,
    AuthSession,
    AuthToken,
    LlmUsage,
    User,
    UserKeys,
    Workspace,
    get_db,
    scrub_wal,
    utcnow,
)
from backend.sessions import AuthProblem, AuthUser, current_user, require_admin, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

DETAILS_UNAVAILABLE = "Details unavailable until this person signs in"
MAX_INVITES = 200          # open invitations at once


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(when: Optional[datetime]) -> Optional[str]:
    """A database time (UTC, without a zone) as ISO text with Z, so a browser reads it as UTC."""
    return when.replace(microsecond=0).isoformat() + "Z" if when else None


def _problem(status: int, code: str, detail: str, field: Optional[str] = None) -> AuthProblem:
    return AuthProblem(status, code, detail, field=field)


def _exclusive(db: Session) -> None:
    """Take the database's write lock now, before reading what the change depends on (who the admins are, which
    key is current), so two admins' changes can't both pass the same check."""
    db.execute(text("BEGIN IMMEDIATE"))


def _target(db: Session, user_id: int) -> User:
    row = db.get(User, user_id)
    if row is None:
        raise _problem(404, "not_found", "There is no such person.")
    return row


def _open_apk(db: Session, admin: AuthUser) -> Optional[Apk]:
    """The admin details key as this admin can open it right now, or None (no usable grant)."""
    try:
        return admin_key.open_apk(db, admin.id)
    except ApkUnavailable:
        return None


SIGN_IN_AGAIN = "Sign in again to open people's details."


def _key_missing() -> AuthProblem:
    if not crypto.has_ik():  # a session from before the identity key: signing in again is the fix
        return _problem(409, "admin_key_missing", SIGN_IN_AGAIN)
    return _problem(409, "admin_key_missing", "You don't have the admin key, so this can't be done. Ask another "
                                              "admin to share it with you again.")


def _need_apk(db: Session, admin: AuthUser) -> Apk:
    apk = _open_apk(db, admin)
    if apk is None:
        raise _key_missing()
    return apk


def _public(db: Session) -> bytes:
    current = admin_key.current_public(db)
    if current is None:
        raise _problem(409, "admin_key_missing", "There is no admin key yet.")
    return current[0]


def _active_admins(db: Session, *, besides: Optional[int] = None) -> list[int]:
    q = db.query(User.id).filter(User.role == "admin", User.status == "active")
    if besides is not None:
        q = q.filter(User.id != besides)
    return [i for (i,) in q]


def audit(db: Session, admin: AuthUser, action: str, target: Optional[int] = None,
          detail: Optional[dict] = None) -> None:
    admin_key.audit(db, admin.id, action, target, detail)


# ---------------------------------------------------------------------------
# What a person's row says (plaintext columns, and details opened with the admin key)
# ---------------------------------------------------------------------------

def _details_of(user: User, apk: Optional[Apk]) -> Optional[dict]:
    """{email, name} from ``pii_admin``, or None when this admin can't open it or it isn't sealed to the current
    key (stale: it heals when the person next signs in)."""
    if apk is None or user.pii_admin is None or user.pii_admin_version != apk.version:
        return None
    info = admin_key.open_json(apk, user.pii_admin, admin_key.pii_label(user.id))
    return info if isinstance(info, dict) else None


def _summaries(db: Session, apk: Optional[Apk], only: Optional[list[int]] = None) -> list[dict]:
    q = db.query(User).order_by(User.id)
    if only is not None:
        q = q.filter(User.id.in_(only))
    rows = q.all()
    ids = [u.id for u in rows]
    if not ids:
        return []
    workspaces = dict(db.query(Workspace.owner_id, func.count(Workspace.id))
                      .filter(Workspace.owner_id.in_(ids)).group_by(Workspace.owner_id).all())
    background = {i for (i,) in db.query(UserKeys.user_id).filter(UserKeys.user_id.in_(ids),
                                                                  UserKeys.server_wrapped_dek.isnot(None))}
    shared = {g.user_id: g.apk_version for g in db.query(AdminKeyGrant)}
    current = admin_key.current_public(db)
    out = []
    for user in rows:
        info = _details_of(user, apk)
        item = {
            "id": user.id,
            "email": info.get("email") if info else None,
            "name": info.get("name") if info else None,
            "details_available": info is not None,
            "details_note": None if info is not None else DETAILS_UNAVAILABLE,
            "role": user.role,
            "status": user.status,
            "created_at": _iso(user.created_at),
            "created_via": user.created_via,
            "last_active_on": user.last_active_on.isoformat() if user.last_active_on else None,
            "locked": lockout.waiting(user.id) > 0,  # some place waits after wrong passwords (backend/lockout.py)
            "background": user.id in background,
            "workspaces": workspaces.get(user.id, 0),
        }
        if user.role == "admin":
            item["admin_key_shared"] = current is not None and shared.get(user.id) == current[1]
        out.append(item)
    return out


def _labels(db: Session, apk: Optional[Apk]) -> dict[int, dict]:
    """{user id: {email, name}} for everyone whose details this admin can open."""
    out: dict[int, dict] = {}
    if apk is None:
        return out
    for user in db.query(User).filter(User.pii_admin.isnot(None)):
        info = _details_of(user, apk)
        if info is not None:
            out[user.id] = {"email": info.get("email"), "name": info.get("name")}
    return out


def _person(labels: dict[int, dict], user_id: Optional[int]) -> Optional[dict]:
    if user_id is None:
        return None
    return {"id": user_id, **labels.get(user_id, {"email": None, "name": None})}


# ---------------------------------------------------------------------------
# Stats and the list of people
# ---------------------------------------------------------------------------

def _hit_rate(cached: int, prompt: int) -> Optional[float]:
    return round(cached / prompt, 4) if prompt else None


@router.get("/stats")
def stats(days: int = Query(30, ge=1, le=365), admin: AuthUser = Depends(current_user),
          db: Session = Depends(get_db)):
    """People (total, admins, active in 7 and 30 days, new in 30 days) and the LLM usage of the last ``days`` days:
    by day, provider, model and purpose, and per person, with cached tokens and the cache hit rate (cached tokens
    over input tokens)."""
    now = utcnow()
    today = now.date()
    in_days = lambda n: func.sum(case((User.last_active_on >= today - timedelta(days=n - 1), 1), else_=0))  # noqa: E731
    total, admins, active_7d, active_30d, new_30d = db.query(
        func.count(User.id), func.sum(case((User.role == "admin", 1), else_=0)), in_days(7), in_days(30),
        func.sum(case((User.created_at >= now - timedelta(days=30), 1), else_=0))).one()
    since = now - timedelta(days=days)
    tokens_in, tokens_out = func.coalesce(LlmUsage.input_tokens, 0), func.coalesce(LlmUsage.output_tokens, 0)
    cached, written = func.coalesce(LlmUsage.cached_tokens, 0), func.coalesce(LlmUsage.cache_write_tokens, 0)
    day = func.date(LlmUsage.created_at)
    by_day = [
        {"day": d, "provider": provider, "model": model, "purpose": purpose, "requests": n, "input": i,
         "output": o, "cached": c, "cache_write": w, "cache_hit_rate": _hit_rate(c, i)}
        for d, provider, model, purpose, n, i, o, c, w in
        db.query(day, LlmUsage.provider, LlmUsage.model, LlmUsage.purpose, func.count(LlmUsage.id),
                 func.sum(tokens_in), func.sum(tokens_out), func.sum(cached), func.sum(written))
        .filter(LlmUsage.created_at >= since).group_by(day, LlmUsage.provider, LlmUsage.model, LlmUsage.purpose)
        .order_by(day)]
    owner = func.coalesce(LlmUsage.user_id, Workspace.owner_id)  # older rows have no user_id: the workspace's owner
    per_user = (db.query(owner, LlmUsage.model, func.count(LlmUsage.id), func.sum(tokens_in), func.sum(tokens_out),
                         func.sum(cached))
                .outerjoin(Workspace, Workspace.id == LlmUsage.workspace_id)
                .filter(LlmUsage.created_at >= since).group_by(owner, LlmUsage.model).order_by(owner).all())
    labels = _labels(db, _open_apk(db, admin))
    by_user = [{"user": _person(labels, uid), "model": model, "requests": n, "input": i, "output": o, "cached": c,
                "cache_hit_rate": _hit_rate(c, i)} for uid, model, n, i, o, c in per_user]
    sums = [sum(row[k] for row in by_day) for k in ("requests", "input", "output", "cached", "cache_write")]
    return {
        "days": days,
        "since": _iso(since),
        "users": {"total": total or 0, "admins": admins or 0, "active_7d": active_7d or 0,
                  "active_30d": active_30d or 0, "new_30d": new_30d or 0},
        "llm": {"totals": {"requests": sums[0], "input": sums[1], "output": sums[2], "cached": sums[3],
                           "cache_write": sums[4], "cache_hit_rate": _hit_rate(sums[3], sums[1])},
                "by_day": by_day, "by_user": by_user},
    }


@router.get("/users")
def list_users(admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Everyone: id, email and name (opened with the admin key; null with ``details_note`` while stale), role,
    status, dates, background access and the number of workspaces. Nothing
    from their own data."""
    apk = _open_apk(db, admin)
    people = _summaries(db, apk)
    return {
        "users": people,
        "admin_key_available": apk is not None,  # false: this admin can't open details (ask for a re-grant)
        "admin_key_needs_sign_in": apk is None and not crypto.has_ik(),  # ... or this session has no identity key
        "admins": sum(1 for p in people if p["role"] == "admin"),
        "only_one_admin": sum(1 for p in people if p["role"] == "admin" and p["status"] == "active") <= 1,
        "details_unavailable": sum(1 for p in people if not p["details_available"]),
    }


# ---------------------------------------------------------------------------
# Roles and the admin key
# ---------------------------------------------------------------------------

NEEDS_SIGN_IN = "Ask them to sign in once first, then share the key."


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["admin", "user"]


@router.post("/users/{user_id}/role")
def set_role(user_id: int, body: RoleIn, admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Make someone an admin (the admin key is sealed to their public key: a grant) or an ordinary user again (the
    admin key is replaced, so the person who loses it can't open anything written afterwards). The last admin
    can't be demoted. Recent password required."""
    require_recent_auth(admin)
    _exclusive(db)
    target = _target(db, user_id)
    if target.role == body.role:
        return {"ok": True, "changed": False, "apk_rotated": False, "user": _summaries(db, _open_apk(db, admin),
                                                                                     [target.id])[0]}
    apk = _need_apk(db, admin)
    if body.role == "admin":
        if target.status != "active":
            raise _problem(409, "disabled", "Turn this account on before making it an admin.")
        try:
            admin_key.grant_to(db, apk, target.id, admin.id)
        except admin_key.NeedsSignIn:
            raise _problem(409, "needs_sign_in", NEEDS_SIGN_IN) from None
        except ApkUnavailable:
            raise _problem(409, "no_keys", "This account has no keys, so it can't be made an admin.") from None
        target.role = "admin"
        audit(db, admin, "role_changed", target.id, {"from": "user", "to": "admin"})
        db.commit()
        return {"ok": True, "changed": True, "apk_rotated": False, "user": _summaries(db, apk, [target.id])[0]}
    if not _active_admins(db, besides=target.id):
        raise _problem(409, "last_admin", "This is the last admin, so they can't be made an ordinary user. Make "
                                          "someone else an admin first.")
    target.role = "user"
    db.flush()
    remaining = admin_key.active_admin_ids(db)
    rotation = admin_key.new_key(db, apk, remaining, admin.id)
    audit_detail = {"from": "admin", "to": "user", "apk_version": rotation.apk.version,
                    "resealed": rotation.resealed, "stale": rotation.stale, "dropped": rotation.dropped}
    admin_key.audit(db, admin.id, "role_changed", target.id, audit_detail, public=rotation.apk.public)
    db.commit()
    scrub_wal()  # the old key's grants must not linger in the write-ahead log
    keep = rotation.apk if admin.id in remaining else None
    return {"ok": True, "changed": True, "apk_rotated": True, "user": _summaries(db, keep, [target.id])[0]}


@router.post("/users/{user_id}/regrant")
def regrant(user_id: int, admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Share the admin key with an admin again (for example after a password reset without a recovery code made
    them a new key pair, so their old grant no longer opens). Recent password required."""
    require_recent_auth(admin)
    _exclusive(db)
    target = _target(db, user_id)
    if target.role != "admin":
        raise _problem(409, "not_admin", "Only an admin holds the admin key. Make them an admin first.")
    if target.status != "active":
        raise _problem(409, "disabled", "Turn this account on before sharing the admin key with it.")
    apk = _need_apk(db, admin)
    try:
        admin_key.grant_to(db, apk, target.id, admin.id)
    except admin_key.NeedsSignIn:
        raise _problem(409, "needs_sign_in", NEEDS_SIGN_IN) from None
    except ApkUnavailable:
        raise _problem(409, "no_keys", "This account has no keys.") from None
    audit(db, admin, "admin_key_shared", target.id, {"apk_version": apk.version})
    db.commit()
    return {"ok": True, "user": _summaries(db, apk, [target.id])[0]}


# ---------------------------------------------------------------------------
# Turning accounts off and on, deleting them
# ---------------------------------------------------------------------------

@router.post("/users/{user_id}/disable")
def disable_user(user_id: int, admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Turn an account off: they are signed out everywhere and can't sign in. Not your own account, and not the last
    admin. Turning off an admin who holds the admin key replaces the key, as demoting does (they keep no copy of anything
    written afterwards); turning them on again does not share it with them. Only that replacement needs your own copy
    of the key: without it they are still turned off and signed out (the emergency brake works for any admin) and their
    copy is deleted, but the key is not replaced (``apk_rotated`` false, with a note); an admin who has the key replaces
    it, or ``new-admin-key`` does. Recent password required."""
    require_recent_auth(admin)
    _exclusive(db)
    target = _target(db, user_id)
    if target.id == admin.id:
        raise _problem(409, "self", "You can't turn off your own account.")
    if target.role == "admin" and target.status == "active" and not _active_admins(db, besides=target.id):
        raise _problem(409, "last_admin", "This is the last admin, so their account can't be turned off.")
    holds_key = target.role == "admin" and db.get(AdminKeyGrant, target.id) is not None
    apk = _open_apk(db, admin) if holds_key else None
    not_replaced = holds_key and apk is None  # turned off by an admin who has no copy of the key
    changed = target.status != "disabled"
    target.status = "disabled"
    db.query(AuthSession).filter(AuthSession.user_id == target.id).delete(synchronize_session=False)
    rotation = None
    if apk is not None:
        db.flush()
        rotation = admin_key.new_key(db, apk, admin_key.active_admin_ids(db), admin.id)
    elif not_replaced:  # they hold nothing if they are turned on again; the key is replaced by an admin who has it
        db.query(AdminKeyGrant).filter(AdminKeyGrant.user_id == target.id).delete(synchronize_session=False)
    if changed or rotation is not None or not_replaced:
        action = "user_disabled" if changed else "admin_key_replaced"  # (a disabled admin who still held a copy)
        detail: dict = {}
        if not_replaced:
            detail["admin_key_not_replaced"] = True
        if rotation is not None:
            detail.update(apk_version=rotation.apk.version, resealed=rotation.resealed, stale=rotation.stale,
                          dropped=rotation.dropped)
            admin_key.audit(db, admin.id, action, target.id, detail, public=rotation.apk.public)
        else:
            audit(db, admin, action, target.id, detail)
    db.commit()
    if rotation is not None or not_replaced:
        scrub_wal()  # the old key's grants must not linger in the write-ahead log
    out = {"ok": True, "changed": changed, "apk_rotated": rotation is not None,
           "user": _summaries(db, rotation.apk if rotation else _open_apk(db, admin), [target.id])[0]}
    if not_replaced and not crypto.has_ik():  # a session from before the identity key: signing in again is the fix
        out["note"] = ("They are off, but the admin key was not replaced because this session can't open it. Sign in "
                       "again, then replace it from an admin who has it, or run new-admin-key.")
    elif not_replaced:
        out["note"] = ("They are off, but the admin key was not replaced because you don't have it. Replace it from "
                       "an admin who has it, or run new-admin-key.")
    return out


@router.post("/users/{user_id}/enable")
def enable_user(user_id: int, admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Turn an account on again, or end the wrong-password wait of an account that is on (unlock)."""
    target = _target(db, user_id)
    changed = target.status != "active"
    was_locked = lockout.waiting(target.id) > 0
    target.status = "active"
    lockout.forget(target.id)
    if changed:
        audit(db, admin, "user_enabled", target.id)
    elif was_locked:
        audit(db, admin, "user_unlocked", target.id)
    db.commit()
    return {"ok": True, "changed": changed, "user": _summaries(db, _open_apk(db, admin), [target.id])[0]}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Remove an account and everything of theirs (their sealed upload files too; the work is
    ``users.delete_account``). Not your own account, not the last admin, and not while one of their searches is
    running. Removing an admin replaces the admin key, as demoting does, so the key they held opens nothing written
    afterwards. The audit entry holds no personal details. Recent password required."""
    require_recent_auth(admin)
    target = _target(db, user_id)
    if target.id == admin.id:
        raise _problem(409, "self", "You can't delete your own account here. Use Settings.")
    try:
        result = users.delete_account(db, target.id, actor_id=admin.id)
    except users.AccountProtected:
        raise _problem(409, "last_admin", "This is the last admin, so their account can't be deleted. Make someone "
                                          "else an admin first.") from None
    except users.AccountBusy:
        raise _problem(409, "busy", "A search of theirs is still running. Try again when it has finished.") from None
    except ApkUnavailable:
        raise _key_missing() from None
    return {"ok": True, "apk_rotated": result["apk_rotated"]}


# ---------------------------------------------------------------------------
# Sign-up mode and invitations
# ---------------------------------------------------------------------------

class SignupSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Optional[Literal["invite", "open", "closed"]] = None


def _signup_settings(db: Session) -> dict:
    return {"mode": signup.mode(db)}


@router.get("/signup")
def get_signup(db: Session = Depends(get_db)):
    return _signup_settings(db)


@router.put("/signup")
def put_signup(body: SignupSettingsIn, admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Who may sign up (invite: only with an invitation link, the default; open: anyone; closed: nobody)."""
    if body.mode is None:
        raise _problem(422, "invalid", "Send mode.")
    signup.set_mode(db, body.mode)
    detail = {"mode": body.mode}
    audit(db, admin, "signup_changed", None, detail)
    db.commit()
    return _signup_settings(db)


class InviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: Optional[str] = Field(None, max_length=1000)


def _base_url(request: Request) -> str:
    return public_url.public_base_url() or str(request.base_url).rstrip("/")


def _invite_status(row: AuthToken, now: datetime) -> str:
    return "used" if row.used_at else "expired" if row.expires_at <= now else "pending"


@router.post("/invites", status_code=201)
def create_invite(body: InviteIn, request: Request, admin: AuthUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    """An invitation link, good for 7 days and one sign-up. Only the link's hash is stored, so this is the one
    time it can be shown: the admin copies it and sends it themselves. ``email`` (optional) pre-fills the sign-up
    form."""
    now = utcnow()
    db.query(AuthToken).filter(AuthToken.kind == signup.INVITE_KIND,  # old ones that expired a month ago
                               AuthToken.expires_at < now - timedelta(days=30)).delete(synchronize_session=False)
    open_ones = db.query(AuthToken).filter(AuthToken.kind == signup.INVITE_KIND, AuthToken.used_at.is_(None),
                                           AuthToken.expires_at > now).count()
    if open_ones >= MAX_INVITES:
        raise _problem(409, "too_many_invites", "There are a lot of invitations waiting. Delete some you no longer "
                                                "need first.")
    email = None
    if body.email:
        try:
            email = users.clean_email(body.email)
        except users.AccountError as exc:
            raise _problem(422, "invalid", exc.message, "email") from None
    token, row = tokens.create(db, signup.INVITE_KIND, ttl=signup.INVITE_TTL, created_by=admin.id,
                               payload={"email": email or ""})
    audit(db, admin, "invite_created", None, {"invite_id": row.id, "email": email})
    db.commit()
    return {"ok": True, "id": row.id, "link": f"{_base_url(request)}/signup.html?invite={token}",
            "email": email, "expires_at": _iso(row.expires_at)}


@router.get("/invites")
def list_invites(admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Invitations, newest first (the address an admin gave is shown while this admin can open the audit detail
    that kept it; the links themselves can't be shown again)."""
    now = utcnow()
    apk = _open_apk(db, admin)
    emails: dict[int, Optional[str]] = {}
    if apk is not None:
        for entry in db.query(AdminAudit).filter(AdminAudit.action == "invite_created"):
            info = admin_key.open_json(apk, entry.detail_admin, admin_key.audit_label(entry.id))
            if isinstance(info, dict) and isinstance(info.get("invite_id"), int):
                emails[info["invite_id"]] = info.get("email")
    rows = db.query(AuthToken).filter(AuthToken.kind == signup.INVITE_KIND).order_by(AuthToken.id.desc()).all()
    return {"invites": [{"id": r.id, "status": _invite_status(r, now), "created_by": r.created_by,
                         "created_at": _iso(r.created_at), "expires_at": _iso(r.expires_at),
                         "used_at": _iso(r.used_at), "used_by": r.user_id, "email": emails.get(r.id)}
                        for r in rows]}


@router.delete("/invites/{invite_id}")
def delete_invite(invite_id: int, admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Cancel an invitation (its link stops working) or clear a used or expired one from the list."""
    row = db.get(AuthToken, invite_id)
    if row is None or row.kind != signup.INVITE_KIND:
        raise _problem(404, "not_found", "There is no such invitation.")
    db.delete(row)
    audit(db, admin, "invite_deleted", None, {"invite_id": invite_id})
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

@router.get("/audit")
def get_audit(limit: int = Query(100, ge=1, le=500), before_id: Optional[int] = Query(None, ge=1),
              admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """What admins did, newest first: ``{entries: [{id, at, action, admin, target, detail}], next_before_id}``.
    ``detail`` is ``{}`` for an action with nothing to say, and null when this admin can't open it (the admin key
    was replaced since)."""
    q = db.query(AdminAudit).order_by(AdminAudit.id.desc())
    if before_id is not None:
        q = q.filter(AdminAudit.id < before_id)
    rows = q.limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    apk = _open_apk(db, admin)
    labels = _labels(db, apk)
    return {"entries": [{"id": r.id, "at": _iso(r.at), "action": r.action, "admin": _person(labels, r.admin_id),
                         "target": _person(labels, r.target_user_id),
                         "detail": {} if r.detail_admin is None
                         else admin_key.open_json(apk, r.detail_admin, admin_key.audit_label(r.id))}
                        for r in rows],
            "next_before_id": rows[-1].id if more and rows else None}


# ---------------------------------------------------------------------------
# Plaintext left from before encryption, and the server key
# ---------------------------------------------------------------------------

def _leftovers() -> list[dict]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in migrate_encryption.leftover_files()]


@router.get("/plaintext-audit")
def plaintext_audit():
    """Where plaintext is still left: values in sealed columns that aren't sealed (``columns``: zero once the
    encryption has run), uploads that aren't sealed (``files``), and the files outside the database that hold
    plaintext from before encryption (``leftovers``: backups and the old page cache). Types and sizes only, never a
    value."""
    progress = migrate_encryption.progress()
    done = progress["state"] == "done"
    found = migrate_encryption.plaintext_audit()
    leftovers = _leftovers() if done else []
    return {"encryption": progress, "columns": found["columns"], "files": found["files"], "leftovers": leftovers,
            "clean": done and not found["columns"] and not found["files"] and not leftovers}


@router.post("/plaintext-files/delete")
def delete_plaintext_files(admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Delete the leftover plaintext files (backups, old page cache), after overwriting them. Recent password
    required."""
    require_recent_auth(admin)
    if not migrate_encryption.is_done():
        raise _problem(409, "encrypting", "Wait until your data is encrypted.")
    deleted = migrate_encryption.delete_leftovers()
    audit(db, admin, "plaintext_files_deleted", None, {"count": len(deleted)})
    db.commit()
    return {"ok": True, "deleted": deleted, "leftovers": _leftovers()}


@router.post("/keys/rotate")
def rotate_server_key(admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Replace the server key (the one that opens background-access keys with nobody signed
    in) and re-wrap everything that uses it. Safe to interrupt: start-up finishes it. Recent password required."""
    require_recent_auth(admin)
    server = users.server_keys()
    try:
        moved = key_rotation.rotate(server)
    except keys.RotationIncomplete as exc:
        log.error("Server key rotation incomplete: %d value(s) left", len(exc.refs))
        raise _problem(500, "rotation_incomplete", f"{len(exc.refs)} value(s) couldn't be moved to the new server "
                                                   "key, so the old one was kept. Nothing was lost; check the log.") \
            from None
    except keys.KeyFileError as exc:  # the file was replaced by another process: nothing was written
        raise _problem(409, "key_file_changed", str(exc)) from None
    audit(db, admin, "server_key_rotated", None, {"rewrapped": moved, "key": server.current_kek_id})
    db.commit()
    return {"ok": True, "rewrapped": moved, "key": server.current_kek_id}
