"""SQLite storage via SQLAlchemy.

Tables: jobs, documents, search_profile.
DB file: <project_root>/data/jobhunt.db
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.getenv("JOBHUNT_DB_PATH", DATA_DIR / "jobhunt.db"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String, nullable=True)
    external_id = Column(String, nullable=True)
    company = Column(String, nullable=True)
    title = Column(String, nullable=True)
    location_text = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    work_mode = Column(String, nullable=True)
    salary_min = Column(Integer, nullable=True)
    salary_max = Column(Integer, nullable=True)
    salary_text = Column(String, nullable=True)
    url = Column(String, unique=True, nullable=True)
    description = Column(Text, nullable=True)
    description_hash = Column(String, nullable=True, index=True)
    industry = Column(String, nullable=True)
    distance_km = Column(Float, nullable=True)
    status = Column(String, default="to_review", nullable=False)
    excluded_reason = Column(Text, nullable=True)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    filetype = Column(String, nullable=True)
    text = Column(Text, nullable=True)
    kind = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SearchProfile(Base):
    __tablename__ = "search_profile"

    id = Column(Integer, primary_key=True, index=True)
    cv_text = Column(Text, nullable=True)
    wishes_text = Column(Text, nullable=True)
    home_location = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    radius_km = Column(Float, nullable=True)
    work_modes = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FitResult(Base):
    """Muse Spark fit score per job (written by backend.scorer)."""

    __tablename__ = "fit_results"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    score = Column(Integer, nullable=False, default=0)
    evidence_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
