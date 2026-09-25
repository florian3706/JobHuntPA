"""Muse Spark fit scorer.

Scores a job description against profile/CV text using a Muse Spark
OpenAI-compatible chat-completions endpoint.

Env vars (``.env`` supported — see ``.env.example``):
    MODEL_API_KEY        – API key (Bearer, primary). If unset, falls back to
                           MUSE_SPARK_API_KEY. If neither is set, scoring
                           degrades gracefully to ``score=0`` +
                           ``summary='NO_KEY…'``.
    MUSE_SPARK_API_KEY   – API key fallback (Bearer).
    MUSE_SPARK_BASE_URL  – API base URL, e.g. ``https://api.meta.ai/v1``.
                           Default: ``https://api.meta.ai/v1``.
                           The client POSTs to ``{base}/chat/completions``.
                           Trailing slashes are stripped. Override this for
                           self-hosted proxies/mocks or region endpoints.
    MUSE_SPARK_MODEL     – Model name, e.g. ``muse-spark-1.3-contributor``.
                           Default: ``muse-spark-1.3-contributor``. Valid:
                           muse-spark-1.3/1.2/1.1 (standard),
                           muse-spark-1.3-contributor/1.2-contributor
                           (contributor). Context: 1048576 tokens.
    JOBHUNT_DB_PATH      – Optional path to the sqlite DB (shared with
                           ``backend.db``). Defaults to
                           ``<project_root>/data/jobhunt.db``.

Typical usage::

    from backend.scorer import score_job, score_new_jobs, rescore_single

    result = score_job(job_description, profile_docs_text)
    batch = score_new_jobs([{"id": 1, "description": "..."}], profile_text)
    fresh = rescore_single(job_id=1, profile_docs_text=profile_text)

FastAPI wiring (in your app factory)::

    from backend.routes_score import router as score_router
    app.include_router(score_router)   # POST /api/score/{job_id}

Ingest wiring (score new hashes only)::

    from backend.scorer import maybe_score_new_job
    # inside ingest loop, after inserting a job with a NEW description_hash:
    maybe_score_new_job(job_id, desc_hash, description, profile_text,
                        seen_hashes=already_scored_hashes)

Storage: results are upserted into the ``fit_results`` table
(``job_id`` FK → ``jobs.id``, ``score``, ``evidence_json``, ``created_at``).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

# ---------------------------------------------------------------------------
# Config / .env
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3-contributor"
VALID_MODELS = {
    "muse-spark-1.3",
    "muse-spark-1.2",
    "muse-spark-1.1",
    "muse-spark-1.3-contributor",
    "muse-spark-1.2-contributor",
}
NO_KEY_SUMMARY = "NO_KEY: MUSE_SPARK_API_KEY not set – scoring skipped."

# Token budget (1M-token context window).
MAX_CONTEXT_TOKENS = 1048576
RESERVED_TOKENS = 2000  # prompt overhead + response headroom
CHARS_PER_TOKEN = 4  # rough estimate for budget math


def is_valid_model(model: str) -> bool:
    """Return True if ``model`` is a known Muse Spark model name."""
    return (model or "").strip() in VALID_MODELS

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal ``.env`` loader (no dependency).

    Tries ``python-dotenv`` first if installed, otherwise parses a ``.env``
    file manually. Search order: CWD, project root, backend/ dir. Only sets
    vars that are not already present in the environment.
    """
    try:
        from dotenv import load_dotenv as _ld  # type: ignore

        _ld(PROJECT_ROOT / ".env", override=False)
        _ld(Path.cwd() / ".env", override=False)
        return
    except Exception:
        pass
    for candidate in (Path.cwd() / ".env", PROJECT_ROOT / ".env"):
        try:
            if not candidate.is_file():
                continue
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            continue


_load_dotenv()


def get_config() -> Dict[str, str]:
    """Read Muse Spark config from the environment."""
    base = (os.getenv("MUSE_SPARK_BASE_URL") or DEFAULT_BASE_URL).strip()
    base = base.rstrip("/") or DEFAULT_BASE_URL
    model_key = (os.getenv("MODEL_API_KEY") or "").strip()
    muse_key = (os.getenv("MUSE_SPARK_API_KEY") or "").strip()
    if model_key:
        api_key, key_source = model_key, "model"
    elif muse_key:
        api_key, key_source = muse_key, "muse"
    else:
        api_key, key_source = "", "none"
    return {
        "api_key": api_key,
        "key_source": key_source,
        "base_url": base,
        "model": (os.getenv("MUSE_SPARK_MODEL") or DEFAULT_MODEL).strip()
        or DEFAULT_MODEL,
    }


def has_api_key() -> bool:
    return bool(get_config()["api_key"])


def _completions_url(base_url: str) -> str:
    return f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/chat/completions"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# Sketch of the prompt contract (returned to callers on request):
#   system: role + strict-JSON schema + grounding rules (no hallucination)
#   user:   JOB DESCRIPTION block + PROFILE/CV block + "return ONLY JSON"
SYSTEM_PROMPT = """You are Muse Spark, a strict job-fit scorer. Compare the JOB DESCRIPTION against the CANDIDATE PROFILE (resume/CV + documents).

Return ONLY valid JSON — no markdown, no commentary — with exactly this shape:
{
  "score": 0,
  "requirements": [
    {"point": "string", "matched": true,
     "evidence": [{"bullet": "string", "sub_bullets": ["string"]}]}
  ],
  "gaps": ["string"],
  "summary": "string"
}

Rules:
1. Extract ALL pertinent points from the job description (skills, experience years, education, tools, languages, work mode, domain). One entry per point in "requirements".
2. For each requirement set "matched" true/false. Every "matched": true entry MUST include at least one evidence item; "matched": false MUST use "evidence": [].
3. Evidence must be GROUNDED in the candidate profile text: "bullet" is a short quote or close paraphrase of a resume fact proving the match; "sub_bullets" are supporting details (years, context, outcomes). Never invent employers, years, skills, or metrics not present in the profile text.
4. List anything required but missing/weak in "gaps" (short strings).
5. "score" is an integer 0-100 weighting all requirements (must-haves weigh more). 0 = no fit, 100 = fully evidenced fit.
6. "summary" is 2-4 sentences: overall fit, strongest matches, biggest gaps.
7. If the profile text is empty, score 0, all matched=false, explain in summary. No hallucination."""


def build_user_prompt(job_description: str, profile_docs_text: str) -> str:
    job = (job_description or "").strip() or "(no job description provided)"
    profile = (profile_docs_text or "").strip() or "(no candidate profile provided)"
    return (
        "JOB DESCRIPTION:\n<<<\n" + job + "\n>>>\n\n"
        "CANDIDATE PROFILE (resume/CV + documents):\n<<<\n" + profile + "\n>>>\n\n"
        "Return ONLY the JSON object described in the system prompt."
    )


def prompt_sketch() -> Dict[str, str]:
    """Return the system/user prompt template (for docs/debugging)."""
    return {
        "system": SYSTEM_PROMPT,
        "user_template": "JOB DESCRIPTION:\\n<<<\\n{job_description}\\n>>>\\n\\n"
        "CANDIDATE PROFILE (resume/CV + documents):\\n<<<\\n"
        "{profile_docs_text}\\n>>>\\n\\nReturn ONLY the JSON object.",
    }


def truncate_to_budget(
    job_description: str, profile_docs_text: str
) -> tuple[str, str, bool]:
    """Truncate inputs to fit ``MAX_CONTEXT_TOKENS`` (chars/4 estimate).

    Truncates the profile first from the FRONT (keeping the tail, since
    the most recent docs are appended last), then the job description
    from the end. Returns ``(job, profile, truncated)``.
    """
    job = job_description or ""
    profile = profile_docs_text or ""
    overhead_chars = len(SYSTEM_PROMPT) + 200  # user-template wrapper
    budget_chars = (MAX_CONTEXT_TOKENS - RESERVED_TOKENS) * CHARS_PER_TOKEN
    allowed = budget_chars - overhead_chars
    if allowed < 0:
        allowed = 0
    if len(job) + len(profile) <= allowed:
        return job, profile, False
    if len(job) >= allowed:
        # Even the JD alone overflows: drop profile, keep JD head.
        return job[:allowed], "", True
    # Keep JD whole; keep profile tail to fit.
    keep_profile = allowed - len(job)
    return job, profile[len(profile) - keep_profile :], True


# ---------------------------------------------------------------------------
# HTTP (OpenAI-compatible) — httpx if available, else urllib stdlib
# ---------------------------------------------------------------------------


def _post_chat_completions(
    system: str,
    user: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout_s: float = 60.0,
) -> str:
    """POST ``{base}/chat/completions`` and return the assistant text."""
    url = _completions_url(base_url)
    payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Prefer httpx (declared in requirements.txt) when importable.
    try:
        import httpx  # type: ignore

        def _httpx_post(pl: Dict[str, Any]) -> Any:
            resp = httpx.post(url, json=pl, headers=headers, timeout=timeout_s)
            resp.raise_for_status()
            return resp.json()

        try:
            data = _httpx_post(payload)
            return _extract_content_from_response(data)
        except Exception as exc:
            resp_obj = getattr(exc, "response", None)
            status = getattr(resp_obj, "status_code", None)
            try:
                body_txt = getattr(resp_obj, "text", "") or str(exc)
            except Exception:
                body_txt = str(exc)
            body_txt = str(body_txt or "")
            if status == 400 and "response_format" in body_txt.lower():
                # Retry once WITHOUT response_format (e.g. api.meta.ai/v1).
                retry_payload = {k: v for k, v in payload.items() if k != "response_format"}
                try:
                    data2 = _httpx_post(retry_payload)
                    return _extract_content_from_response(data2)
                except Exception as exc2:
                    resp2 = getattr(exc2, "response", None)
                    status2 = getattr(resp2, "status_code", status)
                    try:
                        body2 = getattr(resp2, "text", "") or str(exc2)
                    except Exception:
                        body2 = str(exc2)
                    raise RuntimeError(
                        f"Muse Spark API HTTP {status2}: {str(body2 or '')[:500]}"
                    ) from exc2
            if status is not None:
                raise RuntimeError(
                    f"Muse Spark API HTTP {status}: {body_txt[:500]}"
                ) from exc
            # Transport / connection error (no HTTP response).
            if isinstance(exc, RuntimeError):
                raise
            try:
                import httpx as _hx  # type: ignore

                if isinstance(exc, _hx.HTTPError):
                    raise RuntimeError(f"Muse Spark API connection error: {exc}") from exc
            except RuntimeError:
                raise
            except Exception:
                pass
            raise
    except ImportError:
        pass

    # stdlib fallback — no extra dependency required.
    def _urllib_post(pl: Dict[str, Any]) -> Any:
        body = json.dumps(pl).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as fh:  # noqa: S310
                raw = fh.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Muse Spark API HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Muse Spark API connection error: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Muse Spark API returned non-JSON: {raw[:500]}") from exc

    try:
        data = _urllib_post(payload)
    except RuntimeError as exc:
        msg = str(exc)
        # Retry once WITHOUT response_format on 400 mentioning response_format.
        if "HTTP 400" in msg and "response_format" in msg.lower():
            retry_payload = {k: v for k, v in payload.items() if k != "response_format"}
            data = _urllib_post(retry_payload)
        else:
            raise
    return _extract_content_from_response(data)


def _extract_content_from_response(data: Mapping[str, Any]) -> str:
    try:
        choices = data.get("choices") or []  # type: ignore[union-attr]
        content = choices[0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError(f"Unexpected chat-completions shape: {str(data)[:500]}") from exc
    if isinstance(content, list):  # some providers return content blocks
        content = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return str(content)


# ---------------------------------------------------------------------------
# JSON validation + repair
# ---------------------------------------------------------------------------


def _extract_json_text(raw: str) -> str:
    """Strip markdown fences and slice first ``{`` … last ``}``."""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return text.strip()


def _local_json_repair(text: str) -> str:
    """Cheap repairs: trailing commas, single-quote objects, fences."""
    fixed = _extract_json_text(text)
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)  # trailing commas
    return fixed


def validate_result(obj: Any) -> Dict[str, Any]:
    """Coerce a parsed object into the strict result shape.

    Always returns ``{score, requirements, gaps, summary}``. Raises
    ``ValueError`` only if the top level is not a JSON object.
    """
    if not isinstance(obj, dict):
        raise ValueError(f"Top-level JSON must be an object, got {type(obj).__name__}")
    # score: int 0..100
    try:
        score = int(obj.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    requirements: List[Dict[str, Any]] = []
    reqs = obj.get("requirements", [])
    if isinstance(reqs, list):
        for r in reqs:
            if not isinstance(r, dict):
                continue
            point = str(r.get("point", "")).strip()
            if not point:
                continue
            matched = r.get("matched")
            matched = bool(matched) if isinstance(matched, bool) else str(matched).lower() in ("1", "true", "yes")
            evidence: List[Dict[str, Any]] = []
            ev = r.get("evidence", [])
            if isinstance(ev, list):
                for e in ev:
                    if not isinstance(e, dict):
                        continue
                    bullet = str(e.get("bullet", "")).strip()
                    if not bullet:
                        continue
                    subs = e.get("sub_bullets", [])
                    if not isinstance(subs, list):
                        subs = []
                    evidence.append(
                        {
                            "bullet": bullet,
                            "sub_bullets": [str(s) for s in subs if str(s).strip()],
                        }
                    )
            if not matched:
                evidence = []
            requirements.append({"point": point, "matched": matched, "evidence": evidence})

    gaps = obj.get("gaps", [])
    gaps = [str(g).strip() for g in gaps if isinstance(g, (str, int, float)) and str(g).strip()] if isinstance(gaps, list) else []

    summary = obj.get("summary", "")
    summary = str(summary).strip() if summary is not None else ""

    return {"score": score, "requirements": requirements, "gaps": gaps, "summary": summary}


def parse_and_validate(raw_text: str) -> Dict[str, Any]:
    """Parse model output (with repair) into the strict shape.

    Raises ``ValueError`` if the text cannot be salvaged as JSON.
    """
    last_err: Optional[Exception] = None
    for candidate in (raw_text, _extract_json_text(raw_text), _local_json_repair(raw_text)):
        try:
            return validate_result(json.loads(candidate))
        except (json.JSONDecodeError, ValueError) as exc:
            last_err = exc
            continue
    raise ValueError(f"Could not parse scorer JSON: {last_err}")


def _no_key_result() -> Dict[str, Any]:
    return {"score": 0, "requirements": [], "gaps": [], "summary": NO_KEY_SUMMARY}


def _error_result(message: str) -> Dict[str, Any]:
    return {"score": 0, "requirements": [], "gaps": [], "summary": f"ERROR: {message}"[:2000]}


# ---------------------------------------------------------------------------
# Public scoring API
# ---------------------------------------------------------------------------


def score_job(
    job_description: str,
    profile_docs_text: str,
    *,
    timeout_s: float = 60.0,
) -> Dict[str, Any]:
    """Score one job against profile text. Degrades gracefully without a key.

    Returns ``{score, requirements, gaps, summary}``. With no
    ``MUSE_SPARK_API_KEY`` returns ``score=0`` + ``summary='NO_KEY…'``
    without any network call. On API/parse failure returns ``score=0``
    with an ``ERROR: …`` summary instead of raising.
    """
    cfg = get_config()
    if not cfg["api_key"]:
        return _no_key_result()
    job_description, profile_docs_text, _truncated = truncate_to_budget(
        job_description, profile_docs_text
    )
    try:
        content = _post_chat_completions(
            SYSTEM_PROMPT,
            build_user_prompt(job_description, profile_docs_text),
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            timeout_s=timeout_s,
        )
    except Exception as exc:  # network / HTTP errors degrade gracefully
        return _error_result(str(exc))
    try:
        return parse_and_validate(content)
    except ValueError as exc:
        return _error_result(str(exc))


def _job_id_and_description(job: Any) -> tuple[Any, str]:
    if isinstance(job, Mapping):
        jid = job.get("id", job.get("job_id"))
        desc = job.get("description", job.get("job_description", ""))
        return jid, str(desc or "")
    jid = getattr(job, "id", None)
    desc = getattr(job, "description", "") or ""
    return jid, str(desc)


def score_new_jobs(
    jobs: Iterable[Any],
    profile_docs_text: str,
    *,
    delay_s: float = 1.0,
    store_results: bool = True,
    timeout_s: float = 60.0,
) -> Dict[Any, Dict[str, Any]]:
    """Score a batch of jobs with rate limiting and optional persistence.

    * Skip-if-no-key: when ``MUSE_SPARK_API_KEY`` is unset every job gets
      ``{score: 0, summary: 'NO_KEY…'}`` with NO network calls and NO delay.
    * Otherwise jobs are scored sequentially with ``delay_s`` seconds
      between API calls (simple rate limiting; no sleep before the first).
    * ``store_results=True`` upserts each non-NO_KEY result into
      ``fit_results``; NO_KEY stubs are returned but never persisted.
    """
    job_list = list(jobs)
    results: Dict[Any, Dict[str, Any]] = {}
    if not has_api_key():
        for job in job_list:
            jid, _ = _job_id_and_description(job)
            results[jid] = _no_key_result()
        return results

    first = True
    for job in job_list:
        jid, desc = _job_id_and_description(job)
        if not first and delay_s > 0:
            time.sleep(delay_s)
        first = False
        result = score_job(desc, profile_docs_text, timeout_s=timeout_s)
        results[jid] = result
        if store_results and result.get("summary") != NO_KEY_SUMMARY:
            try:
                save_fit_result(jid, result)
            except Exception:
                pass  # persistence must never break a batch
    return results


# ---------------------------------------------------------------------------
# Persistence (fit_results table)
# ---------------------------------------------------------------------------


def save_fit_result(job_id: Any, result: Mapping[str, Any]) -> None:
    """Upsert ``result`` into ``fit_results`` for ``job_id``.

    Prefers the SQLAlchemy model in ``backend.db`` when importable,
    otherwise falls back to stdlib ``sqlite3`` against the same DB file.
    """
    score = int(result.get("score", 0)) if isinstance(result, Mapping) else 0
    evidence_json = json.dumps(dict(result) if isinstance(result, Mapping) else {})
    # 1) SQLAlchemy path (normal app runtime).
    try:
        from backend.db import FitResult, SessionLocal, init_db  # type: ignore

        init_db()
        db = SessionLocal()
        try:
            row = db.query(FitResult).filter(FitResult.job_id == int(job_id)).first()  # type: ignore[arg-type]
            if row is None:
                row = FitResult(job_id=int(job_id), score=score, evidence_json=evidence_json)  # type: ignore[call-arg]
                db.add(row)
            else:
                row.score = score
                row.evidence_json = evidence_json
                row.created_at = datetime.utcnow()
            db.commit()
            return
        finally:
            db.close()
    except Exception:
        pass  # fall through to sqlite3 (e.g. deps not installed)
    # 2) stdlib sqlite3 fallback to the same file backend.db uses.
    try:
        import sqlite3

        db_path = os.getenv("JOBHUNT_DB_PATH", str(PROJECT_ROOT / "data" / "jobhunt.db"))
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS fit_results ("
                "job_id INTEGER PRIMARY KEY, score INTEGER NOT NULL DEFAULT 0, "
                "evidence_json TEXT NOT NULL DEFAULT '{}', "
                "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            con.execute(
                "INSERT INTO fit_results (job_id, score, evidence_json, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET score=excluded.score, "
                "evidence_json=excluded.evidence_json, created_at=excluded.created_at",
                (int(job_id), score, evidence_json, datetime.utcnow().isoformat()),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def get_fit_result(job_id: Any) -> Optional[Dict[str, Any]]:
    """Return the stored result dict for ``job_id``, or ``None``."""
    try:
        from backend.db import FitResult, SessionLocal  # type: ignore

        db = SessionLocal()
        try:
            row = db.query(FitResult).filter(FitResult.job_id == int(job_id)).first()  # type: ignore[arg-type]
            if row is None:
                return None
            try:
                return json.loads(row.evidence_json or "{}")
            except (json.JSONDecodeError, TypeError):
                return {"score": row.score, "summary": row.evidence_json}
        finally:
            db.close()
    except Exception:
        pass
    try:
        import sqlite3

        db_path = os.getenv("JOBHUNT_DB_PATH", str(PROJECT_ROOT / "data" / "jobhunt.db"))
        con = sqlite3.connect(db_path)
        try:
            cur = con.execute("SELECT score, evidence_json FROM fit_results WHERE job_id=?", (int(job_id),))
            row = cur.fetchone()
        finally:
            con.close()
        if not row:
            return None
        try:
            return json.loads(row[1] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"score": row[0], "summary": str(row[1])}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Profile text + single rescore + ingest hook (new hashes only)
# ---------------------------------------------------------------------------


def load_profile_text(db: Any = None, extra_text: str = "") -> str:
    """Assemble candidate profile text from the DB (documents + search profile)."""
    parts: List[str] = []
    if extra_text and extra_text.strip():
        parts.append(extra_text.strip())
    # Prepend UserProfile target roles (best-effort, never raise).
    try:
        from backend.profile import get_profile as _get_user_profile  # type: ignore

        _pdb = db
        _own_prof = False
        if _pdb is None:
            try:
                from backend.db import SessionLocal as _SessionLocal  # type: ignore

                _pdb = _SessionLocal()
                _own_prof = True
            except Exception:
                _pdb = None
        if _pdb is not None:
            try:
                _prof = _get_user_profile(_pdb) or {}
                _titles = [str(t).strip() for t in (_prof.get("titles") or []) if str(t).strip()]
                _kws = [
                    str(k).strip()
                    for k in (_prof.get("keywords_include") or [])
                    if str(k).strip()
                ]
                _bits: List[str] = []
                if _titles:
                    _bits.append("titles: " + ", ".join(_titles))
                if _kws:
                    _bits.append("keywords: " + ", ".join(_kws))
                if _bits:
                    parts.insert(0, "Target roles: " + " | ".join(_bits))
            finally:
                if _own_prof:
                    try:
                        _pdb.close()
                    except Exception:
                        pass
    except Exception:
        pass
    try:
        from backend.db import Document, SearchProfile, SessionLocal  # type: ignore

        own = False
        if db is None:
            db = SessionLocal()
            own = True
        try:
            for doc in db.query(Document).all():
                if getattr(doc, "text", None):
                    parts.append(str(doc.text))
            sp = db.query(SearchProfile).first()
            if sp is not None:
                for attr in ("cv_text", "wishes_text"):
                    val = getattr(sp, attr, None)
                    if val:
                        parts.append(str(val))
        finally:
            if own:
                db.close()
    except Exception:
        pass
    return "\n\n---\n\n".join(p for p in parts if p.strip())


def rescore_single(
    job_id: Any,
    profile_docs_text: Optional[str] = None,
    *,
    db: Any = None,
    timeout_s: float = 60.0,
) -> Dict[str, Any]:
    """Rescore one job and persist the result. Used by POST /api/score/{job_id}.

    ``profile_docs_text`` may be passed explicitly; otherwise it is assembled
    from the ``documents``/``search_profile`` tables. Returns the result dict
    (NO_KEY stub when no API key — never raises for missing key).
    """
    description = ""
    if db is None:
        try:
            from backend.db import Job, SessionLocal  # type: ignore

            _db = SessionLocal()
            try:
                row = _db.query(Job).filter(Job.id == int(job_id)).first()  # type: ignore[arg-type]
                description = str(getattr(row, "description", "") or "") if row else ""
            finally:
                _db.close()
        except Exception:
            description = ""
    else:
        try:
            from backend.db import Job  # type: ignore

            row = db.query(Job).filter(Job.id == int(job_id)).first()  # type: ignore[arg-type]
            description = str(getattr(row, "description", "") or "") if row else ""
        except Exception:
            description = ""
    profile = profile_docs_text if profile_docs_text is not None else load_profile_text(db)
    result = score_job(description, profile, timeout_s=timeout_s)
    if result.get("summary") != NO_KEY_SUMMARY:
        try:
            save_fit_result(job_id, result)
        except Exception:
            pass
    return result


def maybe_score_new_job(
    job_id: Any,
    description_hash: Optional[str],
    job_description: str,
    profile_docs_text: str,
    *,
    seen_hashes: Optional[Set[str]] = None,
    timeout_s: float = 60.0,
    store_results: bool = True,
) -> Optional[Dict[str, Any]]:
    """Ingest hook: score ONLY when ``description_hash`` is new.

    Returns ``None`` (no API call, no write) when the hash was already seen;
    otherwise scores the job and optionally persists to ``fit_results``.
    With no API key returns the NO_KEY stub without persisting.
    """
    if description_hash and seen_hashes is not None and description_hash in seen_hashes:
        return None
    result = score_job(job_description, profile_docs_text, timeout_s=timeout_s)
    if store_results and result.get("summary") != NO_KEY_SUMMARY:
        try:
            save_fit_result(job_id, result)
        except Exception:
            pass
    if seen_hashes is not None and description_hash:
        seen_hashes.add(description_hash)
    return result


# ---------------------------------------------------------------------------
# Optional FastAPI router (POST /api/score/{job_id})
# ---------------------------------------------------------------------------


def get_router():  # type: ignore[no-untyped-def]
    """Build a FastAPI router exposing POST /api/score/{job_id}.

    Import FastAPI lazily so this module stays importable (and ``score_job``
    stays usable) in environments without FastAPI installed.
    """
    from fastapi import APIRouter, Body  # type: ignore

    router = APIRouter(prefix="/api/score", tags=["score"])

    @router.post("/{job_id}")
    def rescore_endpoint(job_id: int, profile_docs_text: Optional[str] = Body(default=None)):  # type: ignore[no-untyped-def]
        """Rescore a single job and persist to ``fit_results``."""
        return rescore_single(job_id, profile_docs_text)

    return router


try:  # eager router for ``app.include_router(routes_score.router)`` style
    router = get_router()
except Exception:  # FastAPI not installed — plain functions still work
    router = None  # type: ignore[assignment]
