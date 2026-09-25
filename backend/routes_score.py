"""FastAPI router for the Muse Spark fit scorer.

Mount in your app factory::

    from backend.routes_score import router
    app.include_router(router)

Endpoints:
    POST /api/score/{job_id}  – rescore a single job (persist to fit_results).
        Optional JSON body: a raw string or {"profile_docs_text": "..."}.
        When omitted, profile text is assembled from documents/search_profile.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from fastapi import APIRouter, Body, Depends  # type: ignore

    from backend.scorer import rescore_single

    router = APIRouter(prefix="/api/score", tags=["score"])

    @router.post("/test")
    def test_scorer_endpoint() -> dict:
        """Smoke-test scorer config + API reachability (never echoes the key)."""
        from backend.scorer import get_config, has_api_key, load_profile_text, score_job

        cfg = get_config()
        safe_cfg = {"base_url": cfg.get("base_url", ""), "model": cfg.get("model", "")}
        # Best-effort scorer-spec extras (never break the core shape).
        try:
            key_source = str(cfg.get("key_source") or "none") or "none"
        except Exception:
            key_source = "none"
        try:
            from backend import scorer as _scorer_mod

            _fn = getattr(_scorer_mod, "is_valid_model", None)
            _models = getattr(_scorer_mod, "VALID_MODELS", None)
            if _models is None:
                _models = (
                    "muse-spark-1.1",
                    "muse-spark-1.2",
                    "muse-spark-1.2-contributor",
                    "muse-spark-1.3",
                    "muse-spark-1.3-contributor",
                )
            _model_name = str(cfg.get("model") or "")
            if callable(_fn):
                try:
                    model_valid = bool(_fn(_model_name))
                except Exception:
                    model_valid = _model_name in list(_models or [])
            else:
                model_valid = (_model_name in list(_models or [])) if _models else True
        except Exception:
            model_valid = True
        extras = {"key_source": key_source, "model_valid": model_valid}
        if not has_api_key():
            return {
                "ok": False,
                "has_key": False,
                **safe_cfg,
                **extras,
                "score": 0,
                "summary": "NO_KEY: MUSE_SPARK_API_KEY not set – scoring skipped.",
                "result": None,
            }
        try:
            profile_text = load_profile_text()
        except Exception:
            profile_text = ""
        result = score_job(
            "Test job: Senior Python Engineer with 5 years experience.",
            profile_text or "Test candidate profile.",
            timeout_s=30.0,
        )
        summary = str(result.get("summary", "") or "")
        ok = not summary.startswith("ERROR:") and not summary.startswith("NO_KEY")
        return {
            "ok": ok,
            "has_key": True,
            **safe_cfg,
            **extras,
            "score": int(result.get("score", 0) or 0),
            "summary": summary[:2000],
            "result": result,
        }

    @router.post("/{job_id}")
    def rescore_endpoint(
        job_id: int,
        body: Any = Body(default=None),
    ) -> dict:
        profile_text: Optional[str] = None
        if isinstance(body, str):
            profile_text = body
        elif isinstance(body, dict) and isinstance(body.get("profile_docs_text"), str):
            profile_text = body["profile_docs_text"]
        return rescore_single(job_id, profile_text)

except Exception:  # FastAPI not installed — import stays safe, functions in scorer still work
    router = None  # type: ignore[assignment]
