"""Cover letter blocks API: paragraphs the user wrote, put together into a letter by concatenation.

    GET    /api/letter-blocks
    POST   /api/letter-blocks             {kind, title, text, tags}: appended after the workspace's last block
    PUT    /api/letter-blocks/order       {ids}: every id must be the workspace's; ``position`` is rewritten in that order
    PATCH  /api/letter-blocks/{id}        {kind?, title?, text?, tags?}
    DELETE /api/letter-blocks/{id}

All of them need a signed-in user. A block has a ``kind`` (opening, body or closing: openings go first, closings
last) and ``position`` (plaintext; the only thing rows are sorted on, the title, text and tags are sealed). The text
may hold the placeholders {company}, {role}, {location} and {date}; ``unknown_placeholders`` in an answer lists any
other ``{name}`` so the editor can warn about a typo. ``assemble_letter`` joins the texts verbatim: the AI never
rewrites a block.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.db import LetterBlock, get_db
from backend.workspaces import current_workspace, owned

router = APIRouter(prefix="/api/letter-blocks", tags=["letter blocks"])

BLOCK_KINDS = ("opening", "body", "closing")
PLACEHOLDERS = ("company", "role", "location", "date")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


Tag = Annotated[str, Field(max_length=60)]


class BlockCreate(BaseModel):
    kind: Literal["opening", "body", "closing"] = "body"
    title: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=8000)
    tags: list[Tag] = Field(default_factory=list, max_length=30)


class BlockUpdate(BaseModel):
    kind: Optional[Literal["opening", "body", "closing"]] = None
    title: Optional[str] = Field(default=None, max_length=200)
    text: Optional[str] = Field(default=None, max_length=8000)
    tags: Optional[list[Tag]] = Field(default=None, max_length=30)


class OrderBody(BaseModel):
    ids: list[int] = Field(max_length=500)


def clean_tags(tags: list[str]) -> list[str]:
    """Trimmed, empties dropped, duplicates (any capitalisation) dropped keeping the first spelling."""
    seen, out = set(), []
    for tag in tags:
        tag = tag.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def placeholders_in(text: str) -> list[str]:
    """The ``{name}`` tokens in ``text``, each once, in order of appearance."""
    return list(dict.fromkeys(_PLACEHOLDER.findall(text or "")))


def _block_dict(b: LetterBlock) -> dict:
    text = b.text or ""
    return {
        "id": b.id,
        "kind": b.kind,
        "position": b.position,
        "title": b.title or "",
        "text": text,
        "tags": list(b.tags or []),
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
        "unknown_placeholders": [n for n in placeholders_in(text) if n not in PLACEHOLDERS],
    }


def placeholder_values(job, today: Optional[date] = None) -> dict[str, str]:
    """What each of ``PLACEHOLDERS`` stands for in a letter for ``job`` (an empty value: it has none)."""
    return {
        "company": (job.company or "").strip(),
        "role": (job.title or "").strip(),
        "location": (job.location_text or "").strip(),
        "date": _long_date(today or date.today()),
    }


def assemble_letter(blocks, job, today: Optional[date] = None) -> tuple[str, list[str]]:
    """The blocks' texts joined by blank lines in the given order, placeholders filled from ``job`` and the date.
    Returns ``(text, unfilled)``: a placeholder with no value, or an unknown name, stays as written and is listed."""
    values = placeholder_values(job, today)
    unfilled: list[str] = []

    def fill(m: re.Match) -> str:
        name = m.group(1)
        if values.get(name):
            return values[name]
        if name not in unfilled:
            unfilled.append(name)
        return m.group(0)

    joined = "\n\n".join((b.text or "").strip() for b in blocks)
    text = _PLACEHOLDER.sub(fill, joined)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), unfilled


def _long_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"  # "23 October 2026" (English month names, whatever the locale)


def _ordered(blocks: list[LetterBlock]) -> list[LetterBlock]:
    return sorted(blocks, key=lambda b: (BLOCK_KINDS.index(b.kind) if b.kind in BLOCK_KINDS else 1, b.position, b.id))


@router.get("")
def list_blocks(db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    rows = db.query(LetterBlock).filter(LetterBlock.workspace_id == ws).all()
    return [_block_dict(b) for b in _ordered(rows)]


@router.post("")
def create_block(payload: BlockCreate, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    last = db.query(func.max(LetterBlock.position)).filter(LetterBlock.workspace_id == ws).scalar()
    now = datetime.utcnow()
    b = LetterBlock(workspace_id=ws, kind=payload.kind, position=(last if last is not None else -1) + 1,
                    title=payload.title.strip(), text=payload.text, tags=clean_tags(payload.tags),
                    created_at=now, updated_at=now)
    db.add(b)
    db.commit()
    return _block_dict(b)


@router.put("/order")
def order_blocks(payload: OrderBody, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    blocks = [owned(db, LetterBlock, i, ws, "Block") for i in dict.fromkeys(payload.ids)]
    for position, b in enumerate(blocks):
        b.position = position
    db.commit()
    return list_blocks(db, ws)


@router.patch("/{block_id}")
def update_block(block_id: int, payload: BlockUpdate, db: Session = Depends(get_db),
                 ws: int = Depends(current_workspace)):
    b = owned(db, LetterBlock, block_id, ws, "Block")
    if payload.kind is not None:
        b.kind = payload.kind
    if payload.title is not None:
        b.title = payload.title.strip()
    if payload.text is not None:
        b.text = payload.text
    if payload.tags is not None:
        b.tags = clean_tags(payload.tags)
    b.updated_at = datetime.utcnow()
    db.commit()
    return _block_dict(b)


@router.delete("/{block_id}", status_code=204)
def delete_block(block_id: int, db: Session = Depends(get_db), ws: int = Depends(current_workspace)):
    db.delete(owned(db, LetterBlock, block_id, ws, "Block"))
    db.commit()
    return Response(status_code=204)
