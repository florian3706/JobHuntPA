"""LLM providers: which API each ``LLM_BASE_URL`` speaks, and every call that differs between them.

The settings stay the three provider-neutral variables (``LLM_API_KEY``, ``LLM_BASE_URL``, ``LLM_MODEL``).
The base URL's host picks the API style:

    api.anthropic.com                      Anthropic's own Messages API through the official SDK
                                           (backend/llm_anthropic.py); never its OpenAI-compatible endpoint
    generativelanguage.googleapis.com      Gemini: plain chat through its OpenAI-compatible endpoint, web
                                           search through the native generateContent API (google_search tool)
    anything else                          OpenAI-compatible: ``{base}/chat/completions``; web search through
                                           ``{base}/responses`` with the web_search tool (OpenAI, Meta, xAI,
                                           any custom host), or OpenRouter's web plugin

Call sites use two functions and never build a provider's request themselves:

    chat(messages, cfg, json_mode=True)  -> str    (backend.scorer.call_model is this)
    web_search(cfg, instructions=..., input=...)   -> dict (below)

``web_search`` returns the app's normalised search response, in the shape of an OpenAI Responses answer, so the
link-integrity code (backend.research.extract / parse_profile, backend.job_chat.web_sources) works unchanged
for every provider:

    {"status": "completed", "output": [
        {"type": "web_search_call", "results": [{"url", "title"}, ...]},          # pages the search returned
        {"type": "message", "content": [{"type": "output_text", "text": "...",
            "annotations": [{"type": "url_citation", "url", "title"}, ...]}]}]}   # pages the answer cites

Providers with no supported web search (Mistral, Groq, DeepSeek, Ollama) fail fast with a user-facing message
(``web_search_problem``) instead of sending a request that cannot work.

``PRESETS``, ``provider_for`` and ``capabilities`` feed the Settings tab and the installer. This module imports
only the standard library until a request is made (httpx is the one exception, and optional here), so the
installer can import it before the app's packages exist.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, Union
from urllib.parse import quote, urljoin, urlsplit

try:
    import httpx
except ModuleNotFoundError:  # the installer reads PRESETS before the app's packages are installed
    httpx = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

MAX_RETRIES = 3
COUNTRY = "AU"  # where web searches are located (the app targets Australian job boards)


class ScorerError(Exception):
    """An LLM call failed. ``auth`` marks failures that retrying other requests cannot fix (key rejected,
    nothing configured): batch runs stop at the first one."""

    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message)
        self.auth = auth


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------

STYLE_OPENAI = "openai"
STYLE_ANTHROPIC = "anthropic"
STYLE_GEMINI = "gemini"

# How a provider's web search is reached (None = the app has no supported way).
SEARCH_RESPONSES = "responses"            # {base}/responses + web_search tool, background mode + polling
SEARCH_RESPONSES_SYNC = "responses_sync"  # same tool, but a plain request (xAI rejects background mode)
SEARCH_ANTHROPIC = "anthropic"            # web_search server tool through the SDK
SEARCH_GEMINI = "gemini"                  # generateContent + google_search
SEARCH_OPENROUTER = "openrouter"          # chat/completions + the web plugin

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Reasoning levels worth probing per style (llm_settings.detect_levels sends one tiny request for each).
LEVELS_OPENAI = ("none", "minimal", "low", "medium", "high", "xhigh")
LEVELS_ANTHROPIC = ("low", "medium", "high", "xhigh", "max")

# Suggested model ids, checked against each provider's own documentation or model list (October 2026).
# A list left empty means the ids could not be confirmed; the model field is free text anyway.
PRESETS: list[dict[str, Any]] = [
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1",
     "models": ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra"],
     "key_url": "https://platform.openai.com/api-keys", "web_search": True, "key_required": True,
     "note": "gpt-6-astra is the most capable and costs the most; gpt-6-luna is the cheapest."},
    {"id": "anthropic", "label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com",
     "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-fable-5-1"],
     "key_url": "https://platform.claude.com/settings/keys", "web_search": True, "key_required": True,
     "note": "Uses Anthropic's own API. claude-opus-5-5 is the default; claude-fable-5-1 is the most capable "
             "and costs more; claude-haiku-4-5 is the cheapest and has no reasoning levels."},
    {"id": "gemini", "label": "Google Gemini", "base_url": GEMINI_OPENAI_BASE,
     "models": ["gemini-3.8-flash", "gemini-3.5-flash-lite"],
     "key_url": "https://aistudio.google.com/apikey", "web_search": True, "key_required": True,
     "note": "Web search uses Google Search grounding, which tends to list fewer sources than other providers."},
    {"id": "meta", "label": "Meta (Muse Spark)", "base_url": "https://api.meta.ai/v1",
     "models": ["muse-spark-1.3", "muse-spark-1.3-contributor"],
     "key_url": "https://dev.meta.ai/", "web_search": True, "key_required": True,
     "note": "The -contributor model is the discounted tier."},
    {"id": "mistral", "label": "Mistral", "base_url": "https://api.mistral.ai/v1",
     "models": [], "key_url": "https://console.mistral.ai/api-keys", "web_search": False, "key_required": True,
     "note": "Type a model id from Mistral's model list. Company research, finding offices and the chat's "
             "web search aren't available; everything else works."},
    {"id": "groq", "label": "Groq", "base_url": "https://api.groq.com/openai/v1",
     "models": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
     "key_url": "https://console.groq.com/keys", "web_search": False, "key_required": True,
     "note": "Company research, finding offices and the chat's web search aren't available; "
             "everything else works."},
    {"id": "xai", "label": "xAI (Grok)", "base_url": "https://api.x.ai/v1",
     "models": ["grok-4.7", "grok-4.6"],
     "key_url": "https://console.x.ai", "web_search": True, "key_required": True, "note": ""},
    {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com",
     "models": ["deepseek-flash", "deepseek-v4-pro"],
     "key_url": "https://platform.deepseek.com/api_keys", "web_search": False, "key_required": True,
     "note": "Company research, finding offices and the chat's web search aren't available; "
             "everything else works."},
    {"id": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
     "models": ["anthropic/claude-sonnet-5.5", "openai/gpt-6.1-sol", "google/gemini-3.8-flash"],
     "key_url": "https://openrouter.ai/keys", "web_search": True, "key_required": True,
     "note": "One key for many providers. Model ids look like provider/model. Web search uses OpenRouter's "
             "web plugin and is billed extra."},
    {"id": "ollama", "label": "Ollama (local)", "base_url": "http://localhost:11434/v1",
     "models": [], "key_url": "https://ollama.com/download", "web_search": False, "key_required": False,
     "note": "Runs models on your own computer: install Ollama and pull a model first, then type its name. "
             "No key needed. Company research, finding offices and the chat's web search aren't available."},
    {"id": "custom", "label": "Custom (OpenAI-compatible)", "base_url": "",
     "models": [], "key_url": "", "web_search": True, "key_required": True,
     "note": "Any service with an OpenAI-style API. Web search works only if it also supports the Responses "
             "API with the web_search tool."},
]
_BY_ID = {p["id"]: p for p in PRESETS}

_HOSTS = {
    "openai": {"api.openai.com"},
    "anthropic": {"api.anthropic.com"},
    "gemini": {"generativelanguage.googleapis.com"},
    "meta": {"api.meta.ai"},
    "mistral": {"api.mistral.ai"},
    "groq": {"api.groq.com"},
    "xai": {"api.x.ai"},
    "deepseek": {"api.deepseek.com"},
    "openrouter": {"openrouter.ai"},
}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
OLLAMA_PORT = 11434

_SEARCH: dict[str, Optional[str]] = {
    "openai": SEARCH_RESPONSES, "anthropic": SEARCH_ANTHROPIC, "gemini": SEARCH_GEMINI,
    "meta": SEARCH_RESPONSES, "mistral": None, "groq": None, "xai": SEARCH_RESPONSES_SYNC,
    "deepseek": None, "openrouter": SEARCH_OPENROUTER, "ollama": None, "custom": SEARCH_RESPONSES,
}


def _host_port(base_url: str) -> tuple[str, Optional[int]]:
    text = (base_url or "").strip()
    try:
        parts = urlsplit(text if "//" in text else "//" + text)
        return (parts.hostname or "").lower(), parts.port
    except ValueError:
        return "", None


def provider_for(base_url: str) -> str:
    """The preset id for a base URL ("custom" when the host isn't one of the known providers)."""
    host, port = _host_port(base_url)
    for provider, hosts in _HOSTS.items():
        if host in hosts:
            return provider
    if host in _LOCAL_HOSTS and port == OLLAMA_PORT:
        return "ollama"
    return "custom"


def style_for(base_url: str) -> str:
    provider = provider_for(base_url)
    return STYLE_ANTHROPIC if provider == "anthropic" else STYLE_GEMINI if provider == "gemini" else STYLE_OPENAI


def search_kind(cfg: dict) -> Optional[str]:
    return _SEARCH[provider_for(cfg.get("base_url", ""))]


def candidate_levels(cfg: dict) -> tuple[str, ...]:
    """Reasoning levels to probe for this provider."""
    return LEVELS_ANTHROPIC if style_for(cfg.get("base_url", "")) == STYLE_ANTHROPIC else LEVELS_OPENAI


def capabilities(cfg: dict) -> dict[str, Any]:
    """What the configured provider can do, for the Settings tab and for call sites.

    ``web_search``: company research, finding offices and the chat's "Search the web" work.
    ``key_required``: False for providers that run locally (Ollama).
    ``reasoning_levels``: the levels worth probing (llm_settings.detect_levels).
    """
    provider = provider_for(cfg.get("base_url", ""))
    return {
        "provider": provider,
        "label": _BY_ID[provider]["label"],
        "style": style_for(cfg.get("base_url", "")),
        "web_search": _SEARCH[provider] is not None,
        "key_required": _BY_ID[provider]["key_required"],
        "reasoning_levels": list(candidate_levels(cfg)),
    }


def web_search_problem(cfg: dict, feature: str = "Web search") -> Optional[str]:
    """None when the provider supports web search, else a message for the user."""
    provider = provider_for(cfg.get("base_url", ""))
    if _SEARCH[provider] is not None:
        return None
    ok = ", ".join(p["label"] for p in PRESETS if p["web_search"] and p["id"] != "custom")
    return (f"{feature} needs web search, which {_BY_ID[provider]['label']} doesn't offer through this app. "
            f"Choose one of {ok} in Settings > LLM to use it; everything else works with "
            f"{_BY_ID[provider]['label']}.")


# --------------------------------------------------------------------------
# Shared HTTP helpers (every non-Anthropic provider is plain httpx)
# --------------------------------------------------------------------------

def _bearer(cfg: dict) -> dict[str, str]:
    key = cfg.get("api_key") or ("ollama" if provider_for(cfg.get("base_url", "")) == "ollama" else "")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _auth_failed(cfg: dict, status: int, body: str) -> bool:
    if status in (401, 403):
        return True
    # Google answers a wrong key with HTTP 400 ("API key not valid"), not 401.
    return (status == 400 and style_for(cfg.get("base_url", "")) == STYLE_GEMINI
            and re.search(r"api key not valid|api_key_invalid|valid api key", body, re.I) is not None)


# Parameters some providers reject. After a 400 names one, it is dropped and the request resent; the model is
# remembered (until the app restarts) so later requests don't send it again.
_PARAM_HINTS = {
    "response_format": ("response_format", "json_object", "json mode", "json_schema"),
    "reasoning_effort": ("reasoning_effort", "reasoning effort", "reasoning"),
}
_rejected: dict[str, set[str]] = {}


def _model_key(cfg: dict) -> str:
    return f"{cfg.get('base_url', '')}|{cfg.get('model', '')}"


def param_rejected(cfg: dict, param: str) -> bool:
    return param in _rejected.get(_model_key(cfg), ())


def remember_rejected(cfg: dict, param: str) -> None:
    _rejected.setdefault(_model_key(cfg), set()).add(param)


def _drop_rejected_param(payload: dict, body: str, cfg: dict) -> bool:
    low = body.lower()
    for param, hints in _PARAM_HINTS.items():
        if param in payload and any(h in low for h in hints):
            payload.pop(param)
            remember_rejected(cfg, param)
            return True
    return False


def _post_json(url: str, payload: dict, headers: dict, cfg: dict, timeout: float, *,
               drop: Optional[tuple[str, str]] = None) -> dict:
    """POST a JSON request with the app's retry rules and return the JSON answer. ``drop=(key, hint)``: when a
    400 mentions ``hint``, resend once without ``payload[key]``."""
    attempt = 0
    while attempt < MAX_RETRIES:
        attempt += 1
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            if attempt == MAX_RETRIES:
                raise ScorerError(f"connection error: {exc.__class__.__name__}: {exc}") from exc
            time.sleep(2 ** attempt)
            continue
        body = resp.text[:600]
        if _auth_failed(cfg, resp.status_code, body):
            raise ScorerError(f"HTTP {resp.status_code}: API key rejected by {cfg['base_url']} ({body})", auth=True)
        if resp.status_code == 400 and drop and drop[0] in payload and drop[1] in body.lower():
            payload.pop(drop[0])
            attempt -= 1
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            time.sleep(5 * attempt)
            continue
        if resp.status_code >= 400:
            raise ScorerError(f"HTTP {resp.status_code}: {body}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ScorerError(f"unexpected response: {body}") from exc
    raise ScorerError("gave up after retries")


def search_timeout(cfg: dict) -> float:
    """Searches that answer in one request take a while (the Responses API's background mode avoids this)."""
    return max(float(cfg.get("timeout_s") or 180), 300.0)


def _turns(value: Union[str, list]) -> list[dict]:
    return [{"role": "user", "content": value}] if isinstance(value, str) else list(value)


# --------------------------------------------------------------------------
# Plain chat
# --------------------------------------------------------------------------

def chat_url(cfg: dict) -> str:
    base = GEMINI_OPENAI_BASE if style_for(cfg["base_url"]) == STYLE_GEMINI else cfg["base_url"]
    return f"{base}/chat/completions"


def _anthropic():
    try:
        from backend import llm_anthropic
    except ModuleNotFoundError as exc:
        if exc.name != "anthropic":
            raise
        raise ScorerError("Claude needs the 'anthropic' Python package, which isn't installed. Run the setup "
                          "again, or: pip install \"anthropic>=1.0,<2\"", auth=True) from exc
    return llm_anthropic


def chat(messages: list[dict], cfg: dict, *, json_mode: bool = True) -> str:
    """One chat answer. ``messages`` use the OpenAI roles (system/user/assistant); ``json_mode`` asks for a
    JSON object where the API has a switch for it (Claude has none: the prompts ask for JSON)."""
    if style_for(cfg["base_url"]) == STYLE_ANTHROPIC:
        return _anthropic().chat(messages, cfg, json_mode=json_mode)
    return _chat_openai(messages, cfg, json_mode)


def _chat_openai(messages: list[dict], cfg: dict, json_mode: bool) -> str:
    payload: dict[str, Any] = {"model": cfg["model"], "messages": messages}
    if json_mode and not param_rejected(cfg, "response_format"):
        payload["response_format"] = {"type": "json_object"}
    if cfg.get("reasoning_effort") and not param_rejected(cfg, "reasoning_effort"):
        payload["reasoning_effort"] = cfg["reasoning_effort"]
    headers = _bearer(cfg)
    url = chat_url(cfg)
    attempt = 0
    while attempt < MAX_RETRIES:
        attempt += 1
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=cfg["timeout_s"])
        except httpx.HTTPError as exc:
            if attempt == MAX_RETRIES:
                raise ScorerError(f"connection error: {exc.__class__.__name__}: {exc}") from exc
            time.sleep(2 ** attempt)
            continue
        body = resp.text[:600]
        if _auth_failed(cfg, resp.status_code, body):
            raise ScorerError(f"HTTP {resp.status_code}: API key rejected by {cfg['base_url']} ({body})", auth=True)
        if resp.status_code == 400 and _drop_rejected_param(payload, body, cfg):
            attempt -= 1  # a provider that doesn't take response_format / reasoning_effort: resend without it
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            retry_after = resp.headers.get("retry-after", "")
            time.sleep(float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 5 * attempt)
            continue
        if resp.status_code >= 400:
            raise ScorerError(f"HTTP {resp.status_code}: {body}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ScorerError(f"unexpected response shape: {body}") from exc
        if isinstance(content, list):
            content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        return str(content or "")
    raise ScorerError("gave up after retries")


# --------------------------------------------------------------------------
# Reasoning-level detection
# --------------------------------------------------------------------------

def probe_level(cfg: dict, level: str) -> Optional[str]:
    """Send one tiny request with this reasoning level. None = accepted, else why not. Raises ScorerError
    when the provider can't be reached or rejects the key."""
    if style_for(cfg["base_url"]) == STYLE_ANTHROPIC:
        return _anthropic().probe_level(cfg, level)
    payload = {"model": cfg["model"], "reasoning_effort": level,
               "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    try:
        resp = httpx.post(chat_url(cfg), json=payload, headers=_bearer(cfg), timeout=90)
    except httpx.HTTPError as exc:
        raise ScorerError(f"Could not reach {cfg['base_url']}: {exc}") from exc
    if _auth_failed(cfg, resp.status_code, resp.text[:600]):
        raise ScorerError(f"API key rejected (HTTP {resp.status_code}).", auth=True)
    if resp.status_code == 200:
        return None
    return f"HTTP {resp.status_code}: {resp.text[:160]}"


# --------------------------------------------------------------------------
# Web search
# --------------------------------------------------------------------------

def web_search(cfg: dict, *, instructions: str, input: Union[str, list], context_size: str = "medium",
               what: str = "web search", feature: str = "Web search") -> dict:
    """One web-search-grounded answer, in the normalised shape described in the module docstring.

    ``instructions``: the system prompt; ``input``: the question (text) or the conversation (OpenAI-style
    turns); ``context_size``: low | medium | high, how much the search may read; ``what``: names the request
    in error messages; ``feature``: names the feature in the "no web search" message.
    """
    kind = search_kind(cfg)
    if kind is None:
        raise ScorerError(web_search_problem(cfg, feature) or "web search is not available")
    if kind in (SEARCH_RESPONSES, SEARCH_RESPONSES_SYNC):
        return _search_responses(cfg, instructions, input, context_size, what, kind)
    if kind == SEARCH_ANTHROPIC:
        return _anthropic().web_search(cfg, instructions, _turns(input), context_size, what)
    if kind == SEARCH_GEMINI:
        return _search_gemini(cfg, instructions, _turns(input), what)
    return _search_openrouter(cfg, instructions, _turns(input), context_size, what)


# -- OpenAI Responses API (OpenAI, Meta, xAI, custom hosts) ------------------

def _responses_payload(cfg: dict, instructions: str, input: Union[str, list], context_size: str, kind: str) -> dict:
    location = {"type": "approximate", "country": COUNTRY}
    if kind == SEARCH_RESPONSES_SYNC:
        # xAI documents neither background mode nor search_context_size, and answers 400 to both.
        return {"model": cfg["model"], "instructions": instructions, "input": input,
                "tools": [{"type": "web_search", "user_location": location}],
                "include": ["web_search_call.action.sources"]}
    include = ["web_search_call.results"]
    if provider_for(cfg["base_url"]) == "openai":
        include.append("web_search_call.action.sources")  # where OpenAI lists the pages it searched
    payload = {"model": cfg["model"], "instructions": instructions, "input": input,
               "tools": [{"type": "web_search", "search_context_size": context_size, "user_location": location}],
               "include": include, "background": True}
    if cfg.get("reasoning_effort"):
        payload["reasoning"] = {"effort": cfg["reasoning_effort"]}
    return payload


def _search_responses(cfg: dict, instructions: str, input: Union[str, list], context_size: str, what: str,
                      kind: str) -> dict:
    from backend import research  # the Responses transport (start, poll) lives there

    payload = _responses_payload(cfg, instructions, input, context_size, kind)
    run_cfg = {**cfg, "search_timeout_s": search_timeout(cfg)} if kind == SEARCH_RESPONSES_SYNC else cfg
    try:
        response = research.run_response(payload, run_cfg, what)
    except ScorerError as exc:
        if "reasoning" not in payload or not str(exc).startswith("HTTP 400") or "reasoning" not in str(exc).lower():
            raise
        payload.pop("reasoning")  # a model that takes no reasoning level
        response = research.run_response(payload, run_cfg, what)
    return _with_listed_sources(response) if provider_for(cfg["base_url"]) in ("openai", "xai") else response


def _with_listed_sources(response: dict) -> dict:
    """Make the pages a provider lists apart from ``web_search_call.results`` visible to research.extract:
    ``web_search_call.action.sources`` (OpenAI, xAI) and a top-level ``citations`` list (xAI)."""
    found: dict[str, str] = {}
    for item in response.get("output") or []:
        if item.get("type") == "web_search_call":
            for source in (item.get("action") or {}).get("sources") or []:
                if isinstance(source, dict) and source.get("url"):
                    found.setdefault(source["url"], source.get("title") or "")
    for cite in response.get("citations") or []:
        url, title = (cite.get("url"), cite.get("title")) if isinstance(cite, dict) else (cite, "")
        if isinstance(url, str) and url:
            found.setdefault(url, title or "")
    if not found:
        return response
    listed = {"type": "web_search_call", "results": [{"url": u, "title": t} for u, t in found.items()]}
    return {**response, "output": [*(response.get("output") or []), listed]}


# -- Google Gemini: generateContent with Google Search grounding -------------

GEMINI3_LEVELS = ("minimal", "low", "medium", "high")
_GOOGLE_REDIRECT_HOST = "vertexaisearch.cloud.google.com"


def _search_gemini(cfg: dict, instructions: str, turns: list[dict], what: str) -> dict:
    model = cfg["model"].removeprefix("models/")
    body: dict[str, Any] = {
        "contents": [{"role": "model" if t.get("role") == "assistant" else "user",
                      "parts": [{"text": str(t.get("content") or "")}]} for t in turns if t.get("role") != "system"],
        "tools": [{"google_search": {}}],
    }
    if instructions:
        body["systemInstruction"] = {"parts": [{"text": instructions}]}
    if model.startswith("gemini-3") and cfg.get("reasoning_effort") in GEMINI3_LEVELS:
        body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": cfg["reasoning_effort"]}}
    headers = {"x-goog-api-key": cfg["api_key"], "Content-Type": "application/json"}
    data = _post_json(f"{GEMINI_NATIVE_BASE}/models/{quote(model, safe='')}:generateContent", body, headers, cfg,
                      search_timeout(cfg), drop=("generationConfig", "thinking"))
    return gemini_to_response(data, what)


def _resolve_google_redirect(uri: str) -> str:
    """Grounding links are Google redirects to the real page; follow one hop so the app can show (and
    verify) the page's own address. The redirect itself is kept when that fails."""
    if urlsplit(uri).hostname != _GOOGLE_REDIRECT_HOST:
        return uri
    try:
        resp = httpx.get(uri, follow_redirects=False, timeout=10)
    except httpx.HTTPError:
        return uri
    location = resp.headers.get("location") if 300 <= resp.status_code < 400 else None
    return urljoin(uri, location) if location else uri


def gemini_to_response(data: dict, what: str = "web search") -> dict:
    """A generateContent answer with grounding, in the normalised shape."""
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise ScorerError(f"{what} ended with status failed: Google returned no answer"
                          + (f" (blocked: {reason})" if reason else ""))
    cand = candidates[0]
    text = "".join(p.get("text") or "" for p in (cand.get("content") or {}).get("parts") or []
                   if isinstance(p, dict) and not p.get("thought"))
    finish = cand.get("finishReason")
    if finish == "MAX_TOKENS":
        raise ScorerError(f"{what}: the answer was cut off at the model's length limit; try a lower reasoning level.")
    if not text.strip():
        raise ScorerError(f"{what} ended with status failed: Google returned no text (finish reason {finish or 'unknown'}).")

    grounding = cand.get("groundingMetadata") or {}
    chunks = [(c.get("web") or {}) for c in grounding.get("groundingChunks") or [] if isinstance(c, dict)]
    uris = [w.get("uri") or "" for w in chunks]
    with ThreadPoolExecutor(max_workers=8) as pool:
        urls = list(pool.map(_resolve_google_redirect, uris))
    pages = []  # (url, title) per chunk, in the order Google lists them
    for web, url in zip(chunks, urls):
        if url:
            pages.append((url, web.get("title") or urlsplit(url).hostname or url))
    cited_idx = {i for s in grounding.get("groundingSupports") or [] if isinstance(s, dict)
                 for i in s.get("groundingChunkIndices") or []}
    cited = {}
    for i, (web, url) in enumerate(zip(chunks, urls)):
        if url and i in cited_idx:
            cited.setdefault(url, web.get("title") or urlsplit(url).hostname or url)
    return _normalised(text, dict(pages), cited)


def _normalised(text: str, searched: dict[str, str], cited: dict[str, str]) -> dict:
    """The search response shape every provider adapter returns (see the module docstring)."""
    return {"status": "completed", "output": [
        {"type": "web_search_call", "results": [{"url": u, "title": t} for u, t in searched.items()]},
        {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": [
            {"type": "url_citation", "url": u, "title": t} for u, t in cited.items()]}]}]}


# -- OpenRouter: chat/completions with the web plugin --------------------------

_OPENROUTER_RESULTS = {"low": 5, "medium": 8, "high": 10}


def _search_openrouter(cfg: dict, instructions: str, turns: list[dict], context_size: str, what: str) -> dict:
    payload = {"model": cfg["model"],
               "messages": [{"role": "system", "content": instructions}, *turns],
               "plugins": [{"id": "web", "max_results": _OPENROUTER_RESULTS.get(context_size, 5)}]}
    data = _post_json(f"{cfg['base_url']}/chat/completions", payload, _bearer(cfg), cfg, search_timeout(cfg))
    try:
        message = data["choices"][0]["message"]
        text = message.get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ScorerError(f"unexpected response shape: {str(data)[:300]}") from exc
    if isinstance(text, list):
        text = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in text)
    if not str(text).strip():
        raise ScorerError(f"{what} ended with status failed: the model returned no text.")
    cited: dict[str, str] = {}
    for ann in message.get("annotations") or []:
        if isinstance(ann, dict) and ann.get("type") == "url_citation":
            cite = ann.get("url_citation") or ann
            if cite.get("url"):
                cited.setdefault(cite["url"], cite.get("title") or "")
    return _normalised(str(text), dict(cited), cited)  # OpenRouter lists only the pages it cites
