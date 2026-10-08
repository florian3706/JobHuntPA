"""Single-use links kept in ``auth_tokens`` (accounts design §4): invites, password resets and email checks.

The link carries a random 256-bit token. The database keeps only its SHA-256 (``token_hash``), so a copy of the
database can't be turned into working links, and a payload (an invite carries the address to pre-fill) sealed under a
key derived from the token, so it can only be read by whoever holds the link.

A token is used up atomically: ``consume`` is one UPDATE that only succeeds while the row is unused and unexpired, so
two requests with the same link can't both win.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from backend import crypto
from backend.db import AuthToken, utcnow

TOKEN_MAX = 200  # longer input is never a token we made


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(b"jh auth token v1|" + token.encode("utf-8")).digest()


def _payload_key(token: str) -> bytes:
    return crypto.hkdf(token.encode("utf-8"), b"jh auth token payload v1")


def _payload_label(kind: str) -> str:
    return f"auth_tokens.payload_enc|{kind}"


def seal_payload(token: str, kind: str, data: dict) -> bytes:
    return crypto.wrap(_payload_key(token), json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       _payload_label(kind))


def open_payload(token: str, kind: str, blob: Optional[bytes]) -> Optional[dict]:
    """What ``seal_payload`` sealed for this token, or None (no payload, or not this token's)."""
    if blob is None:
        return None
    try:
        data = json.loads(crypto.unwrap(_payload_key(token), blob, _payload_label(kind)))
    except (crypto.CryptoError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def create(db: Session, kind: str, *, ttl: timedelta, created_by: Optional[int] = None, user_id: Optional[int] = None,
           payload: Optional[dict] = None, token: Optional[str] = None) -> tuple[str, AuthToken]:
    """A new token row in ``db``'s transaction (flushed, not committed): ``(token, row)``. The token is not stored
    anywhere: hand it to the person once. ``token`` is one the caller made (a reset link signs its token)."""
    token = token or new_token()
    row = AuthToken(kind=kind, token_hash=hash_token(token), user_id=user_id, created_by=created_by,
                    expires_at=utcnow() + ttl,
                    payload_enc=seal_payload(token, kind, payload) if payload is not None else None)
    db.add(row)
    db.flush()
    return token, row


def find(db: Session, kind: str, token: Any) -> Optional[AuthToken]:
    """The row for a token of this kind that is unused and not expired, or None. Doesn't use it up."""
    if not isinstance(token, str) or not token or len(token) > TOKEN_MAX:
        return None
    row = db.query(AuthToken).filter(AuthToken.token_hash == hash_token(token), AuthToken.kind == kind).first()
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        return None
    return row


def consume(db: Session, kind: str, token: Any) -> Optional[AuthToken]:
    """Uses the token up (commits) and returns its row, or None when it is unknown, used, expired or of another
    kind. Only one of several simultaneous calls gets the row."""
    row = find(db, kind, token)
    if row is None:
        return None
    now = utcnow()
    done = db.execute(update(AuthToken).where(AuthToken.id == row.id, AuthToken.used_at.is_(None),
                                              AuthToken.expires_at > now).values(used_at=now))
    db.commit()
    if done.rowcount != 1:
        return None
    db.refresh(row)
    return row


def release(db: Session, token_id: int) -> None:
    """Gives a token back after what it was used for failed (the person may try again)."""
    db.execute(update(AuthToken).where(AuthToken.id == token_id).values(used_at=None))
    db.commit()
