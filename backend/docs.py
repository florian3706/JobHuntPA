"""Documents router: upload PDF/DOCX/ODF, extract text, persist."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .db import Document, get_db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".odt", ".ods", ".odp"}

router = APIRouter(prefix="/api/docs", tags=["docs"])


def _extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(parts).strip()


def _extract_docx_text(path: Path) -> str:
    import docx

    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs).strip()


def _extract_odf_text(path: Path) -> str:
    from odf import text as odf_text
    from odf.opendocument import load

    doc = load(str(path))
    parts = []
    for p in doc.getElementsByType(odf_text.P):
        chunks = []
        for node in p.childNodes:
            if node.nodeType == node.TEXT_NODE:
                chunks.append(node.data)
            else:
                # e.g. <text:span> elements
                for sub in node.childNodes:
                    if sub.nodeType == sub.TEXT_NODE:
                        chunks.append(sub.data)
                    elif hasattr(sub, "data"):
                        try:
                            chunks.append(str(sub.data))
                        except Exception:
                            pass
        if chunks:
            parts.append("".join(chunks))
    return "\n".join(parts).strip()


def extract_text(path: Path, suffix: str) -> str:
    suffix = suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    if suffix == ".docx":
        return _extract_docx_text(path)
    if suffix in {".odt", ".ods", ".odp"}:
        return _extract_odf_text(path)
    raise ValueError(f"Unsupported file type: {suffix}")


def _doc_to_dict(d: Document, include_text: bool = False) -> dict:
    data = {
        "id": d.id,
        "filename": d.filename,
        "filetype": d.filetype,
        "kind": d.kind,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "text_len": len(d.text or ""),
    }
    if include_text:
        data["text"] = d.text or ""
    return data


@router.post("")
def upload_doc(
    file: UploadFile = File(...),
    kind: str = Form("cv"),
    db: Session = Depends(get_db),
):
    original_name = file.filename or "upload"
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type {suffix!r}. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / stored_name
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        text = extract_text(dest, suffix)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not extract text: {exc}")

    doc = Document(filename=original_name, filetype=suffix, text=text, kind=kind)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return _doc_to_dict(doc, include_text=False)


@router.get("")
def list_docs(db: Session = Depends(get_db)):
    docs = db.query(Document).order_by(Document.created_at.desc()).all()
    return [_doc_to_dict(d) for d in docs]


@router.get("/{doc_id}/text")
def get_doc_text(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"id": doc.id, "filename": doc.filename, "text": doc.text or ""}


@router.delete("/{doc_id}")
def delete_doc(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    db.delete(doc)
    db.commit()
    # Best-effort cleanup: stored files use uuid names so the original
    # filename is not on disk; uploads dir is left intact. Remove any
    # file that exactly matches the original filename (legacy behavior).
    legacy = UPLOAD_DIR / (doc.filename or "")
    try:
        if legacy.is_file():
            legacy.unlink()
    except Exception:
        pass
    return {"ok": True, "id": doc_id}
