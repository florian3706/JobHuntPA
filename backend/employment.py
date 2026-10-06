"""Employment type vocabulary and the one normaliser every source goes through.

Stored as ``jobs.employment_type`` (None = unknown). Never filter or sort on it
in SQL: the accounts design seals most job columns in Phase D.
"""
from __future__ import annotations

import re
from typing import Any, Optional

EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "casual", "temporary", "internship")

# Checked in order, so "Contract/Temp" is contract and "Part time casual" is casual.
_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("internship", re.compile(r"\b(intern|interns|internship|internships|trainee|traineeship|apprentice|apprenticeship)\b")),
    ("casual", re.compile(r"\bcasual\b")),
    ("part_time", re.compile(r"\bpart ?time\b")),
    ("contract", re.compile(r"\b(contract|contractor|contractors|contracting|freelance|fixed ?term)\b")),
    ("temporary", re.compile(r"\b(temp|temps|temporary|seasonal)\b")),
    ("full_time", re.compile(r"\bfull ?time\b")),
)


def normalise_employment_type(value: Any) -> Optional[str]:
    """Map a source's label (string, list of strings, or {"label": ...}) to the vocabulary, else None.

    Accepts "Full time", "FULL_TIME", "full-time", "Contract/Temp", ["PART_TIME", "CONTRACTOR"]
    and the like. A list yields its first recognised entry.
    """
    if isinstance(value, (list, tuple, set)):
        for item in value:
            hit = normalise_employment_type(item)
            if hit:
                return hit
        return None
    if isinstance(value, dict):
        return normalise_employment_type(value.get("label")) or normalise_employment_type(value.get("id"))
    if not isinstance(value, str):
        return None
    text = re.sub(r"[_\-/,;|]+", " ", value.lower())
    text = re.sub(r"\s+", " ", text).strip()
    for name, rx in _RULES:
        if rx.search(text):
            return name
    return None
