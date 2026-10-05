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

A message's content is a string, or a list of text parts. ``cached_text(...)`` marks the part that ends a prefix
later requests repeat byte for byte (the instructions and the candidate's documents), so providers can cache it;
see "Prompt caching" below for what each provider does with the mark. ``cfg`` may also carry ``purpose`` and
``workspace_id`` (recorded with the request's token usage in ``llm_usage``) and ``long_output`` (a long text that
must not be cut off).

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

import hashlib
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
    "prompt_cache_key": ("prompt_cache_key",),
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
# Prompt caching
# --------------------------------------------------------------------------
#
# Scoring sends the same instructions and candidate documents (often 40-60k tokens) with every job, and the chat
# sends them with every question. Call sites put that stable part first, end it with a ``cached_text`` part and
# put what changes (the job, the question) after it; nothing per-request (time, ids) goes before the mark. What
# each provider does with it (checked against their documentation, October 2026):
#
#   Anthropic   cache_control breakpoints on the marked blocks (backend/llm_anthropic.py). Cache reads cost a
#               tenth of the input price or less; writes cost 1.25x (5-minute entries) or 2x (1-hour entries).
#               Prefixes under the model's minimum (512 tokens on current models, 4096 on Haiku 4.5) aren't
#               cached, silently.
#   OpenAI      automatic for prompts of 1024+ tokens; ``prompt_cache_key`` (Chat Completions) routes requests
#               with the same key to the same cache. GPT-5.6+ bills cache writes at 1.25x, reads at 0.1x.
#   Meta        automatic; optional ``prompt_cache_key`` (its Prompt Caching cookbook).
#   Mistral     only with ``prompt_cache_key``; cached tokens billed at 10%.
#   xAI         automatic; the ``x-grok-conv-id`` header sends requests to the server holding the cache.
#   OpenRouter  passes caching through to the model's provider (automatic for OpenAI, DeepSeek, Gemini, Grok,
#               Groq); Claude models need ``cache_control`` on content parts, so those keep their parts.
#   Gemini      implicit caching on Gemini 2.5+ (4096+ tokens on 3.x Flash), nothing to send.
#   DeepSeek    automatic disk cache, nothing to send; reported as ``prompt_cache_hit_tokens``.
#   Groq        automatic on the GPT-OSS models (50% off cached tokens), nothing to send.
#   Ollama      runs locally: llama.cpp reuses the computed prefix while the model stays loaded (nothing billed).
#   custom      unknown host: nothing extra is sent, the prompt is flattened as before.
#
# Caching doesn't mean "sent once": the documents still travel with every request, the provider just processes
# (and bills) a repeated prefix for less. Only explicit caches that hold content by reference store it once
# (Gemini's cachedContents on its native API, not built here). Every provider except Anthropic and OpenRouter-Claude
# gets the parts flattened into one string per message, byte for byte what was sent before parts existed.

CACHE_RUN = "run"                    # the next request with this prefix follows within minutes (a scoring run)
CACHE_CONVERSATION = "conversation"  # it waits for the user's next question (the job chat), often 5-60 minutes
MAX_BREAKPOINTS = 4                  # Anthropic's limit, through OpenRouter too
_CACHE_KEY_PROVIDERS = {"openai", "meta", "mistral"}  # take prompt_cache_key on /chat/completions
_RUN_TTL_LIMIT_S = 270               # a request that may run longer than this outlives a 5-minute entry


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def cached_text(text: str, reuse: str = CACHE_RUN) -> dict:
    """A content part ending a prefix that later requests repeat byte for byte. ``reuse`` says when the next one
    comes: CACHE_RUN (within minutes) or CACHE_CONVERSATION (when the user asks again)."""
    return {"type": "text", "text": text, "cache": reuse}


def _parts(content: Any) -> list[dict]:
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    return [text_part("" if content is None else str(content))]


def text_of(content: Any) -> str:
    """A message's content as one string (parts are joined as they are, with nothing in between)."""
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in _parts(content))
    return "" if content is None else str(content)


def flat_messages(messages: list[dict]) -> list[dict]:
    """The messages with string content (how every OpenAI-style provider gets them)."""
    return [{**m, "content": text_of(m.get("content"))} if isinstance(m.get("content"), list) else m
            for m in messages]


def cache_breakpoints(messages: list[dict]) -> set[tuple[int, int]]:
    """(message index, part index) of the marked parts that get a breakpoint: the last MAX_BREAKPOINTS."""
    marks = [(i, j) for i, m in enumerate(messages) if isinstance(m.get("content"), list)
             for j, p in enumerate(_parts(m["content"])) if p.get("cache") and p.get("text")]
    return set(marks[-MAX_BREAKPOINTS:])


def cached_prefix(messages: list[dict]) -> Optional[str]:
    """Everything up to and including the last marked part, or None when nothing is marked."""
    marks = cache_breakpoints(messages)
    if not marks:
        return None
    last_i, last_j = max(marks)
    pieces = []
    for i, m in enumerate(messages[:last_i + 1]):
        parts = _parts(m.get("content"))
        if i == last_i:
            parts = parts[:last_j + 1]
        pieces.append(f"{m.get('role')}\x00" + "".join(str(p.get("text") or "") for p in parts))
    return "\x00".join(pieces)


def cache_key(prefix: str) -> str:
    """A stable, non-identifying key for requests sharing this prefix (a hash, never the content)."""
    return "jobhuntpa-" + hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:32]


def cache_ttl(cfg: dict, messages: list[dict]) -> Optional[str]:
    """"1h" or None (the provider's 5-minute default) for the breakpoints of this request.

    A cache read refreshes the entry, and its lifetime counts from the start of the request that wrote or read it.
    In a scoring run each request starts at most one request's duration (LLM_TIMEOUT_S, 180 s by default) after
    the previous one, so 5 minutes is enough and is the cheaper write (1.25x the input price instead of 2x). The
    1-hour entry pays for itself when the next request may start later than that: a chat waiting for the user,
    or requests allowed to run longer than about 4.5 minutes."""
    reuse = {p.get("cache") for m in messages if isinstance(m.get("content"), list)
             for p in _parts(m["content"]) if p.get("cache")}
    if CACHE_CONVERSATION in reuse or (reuse and float(cfg.get("timeout_s") or 180) > _RUN_TTL_LIMIT_S):
        return "1h"
    return None


def cache_control(ttl: Optional[str]) -> dict:
    return {"type": "ephemeral", "ttl": ttl} if ttl else {"type": "ephemeral"}


def _openrouter_claude(cfg: dict) -> bool:
    return provider_for(cfg.get("base_url", "")) == "openrouter" and str(cfg.get("model", "")).startswith("anthropic/")


def _wire_messages(messages: list[dict], cfg: dict) -> list[dict]:
    """The messages as sent to an OpenAI-style provider: flattened, except that Claude through OpenRouter keeps
    text parts with ``cache_control`` on the marked ones (OpenRouter's documented way to cache them)."""
    if not _openrouter_claude(cfg):
        return flat_messages(messages)
    marks, ttl = cache_breakpoints(messages), cache_ttl(cfg, messages)
    out = []
    for i, m in enumerate(messages):
        if not isinstance(m.get("content"), list):
            out.append(m)
            continue
        parts = []
        for j, p in enumerate(_parts(m["content"])):
            part = text_part(str(p.get("text") or ""))
            if (i, j) in marks:
                part["cache_control"] = cache_control(ttl)
            if part["text"]:
                parts.append(part)
        out.append({**m, "content": parts})
    return out


# --------------------------------------------------------------------------
# Usage records (llm_usage)
# --------------------------------------------------------------------------

def as_int(value: Any) -> Optional[int]:
    try:
        return None if value is None or isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


def field(obj: Any, key: str) -> Any:
    """A field of a dict or an SDK object (None when missing)."""
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def usage_from_chat(data: Any) -> dict:
    """Token counts from a /chat/completions answer (OpenAI and every OpenAI-compatible provider)."""
    usage = field(data, "usage") or {}
    details = field(usage, "prompt_tokens_details") or {}
    cached = field(details, "cached_tokens")
    if cached is None:
        cached = field(usage, "prompt_cache_hit_tokens")  # DeepSeek
    return {"input_tokens": as_int(field(usage, "prompt_tokens")),
            "output_tokens": as_int(field(usage, "completion_tokens")),
            "cached_tokens": as_int(cached), "cache_write_tokens": as_int(field(details, "cache_write_tokens"))}


def usage_from_responses(data: Any) -> dict:
    """Token counts from a Responses API answer (OpenAI, Meta, xAI, custom hosts)."""
    usage = field(data, "usage") or {}
    details = field(usage, "input_tokens_details") or {}
    return {"input_tokens": as_int(field(usage, "input_tokens")),
            "output_tokens": as_int(field(usage, "output_tokens")),
            "cached_tokens": as_int(field(details, "cached_tokens")),
            "cache_write_tokens": as_int(field(details, "cache_write_tokens"))}


def usage_from_gemini(data: Any) -> dict:
    """Token counts from a native generateContent answer (thinking tokens count as output)."""
    meta = field(data, "usageMetadata") or {}
    out, thoughts = as_int(field(meta, "candidatesTokenCount")), as_int(field(meta, "thoughtsTokenCount"))
    return {"input_tokens": as_int(field(meta, "promptTokenCount")),
            "output_tokens": None if out is None and thoughts is None else (out or 0) + (thoughts or 0),
            "cached_tokens": as_int(field(meta, "cachedContentTokenCount")), "cache_write_tokens": None}


def record_usage(cfg: dict, usage: Optional[dict] = None, *, model: Optional[str] = None) -> None:
    """Store one answered request's token usage. Never raises: missing usage fields are stored as empty, and a
    failure to store is only logged, so a request never fails because of its statistics."""
    try:
        from backend.db import LlmUsage, SessionLocal  # here, so the installer can import this module

        counts = {k: as_int((usage or {}).get(k)) for k in
                  ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")}
        db = SessionLocal()
        try:
            db.add(LlmUsage(workspace_id=as_int(cfg.get("workspace_id")), purpose=cfg.get("purpose") or None,
                            provider=provider_for(cfg.get("base_url", "")), model=model or cfg.get("model"), **counts))
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001  (statistics must never break a request)
        log.warning("could not record LLM usage", exc_info=True)


def record_answer(cfg: dict, parse, data: Any, *, model: Optional[str] = None) -> None:
    """record_usage for a provider's answer, read with ``parse`` (usage_from_chat, ...); never raises."""
    try:
        usage = parse(data)
    except Exception:  # noqa: BLE001
        usage = None
    record_usage(cfg, usage, model=model)


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
    """One chat answer. ``messages`` use the OpenAI roles (system/user/assistant), with string content or text
    parts (``cached_text`` marks a cacheable prefix); ``json_mode`` asks for a JSON object where the API has a
    switch for it (Claude has none: the prompts ask for JSON). ``cfg["long_output"]``: the answer is a long text
    that must not be cut off (a cut-off one is an error)."""
    if style_for(cfg["base_url"]) == STYLE_ANTHROPIC:
        return _anthropic().chat(messages, cfg, json_mode=json_mode)
    return _chat_openai(messages, cfg, json_mode)


def _chat_openai(messages: list[dict], cfg: dict, json_mode: bool) -> str:
    provider = provider_for(cfg["base_url"])
    payload: dict[str, Any] = {"model": cfg["model"], "messages": _wire_messages(messages, cfg)}
    if json_mode and not param_rejected(cfg, "response_format"):
        payload["response_format"] = {"type": "json_object"}
    if cfg.get("reasoning_effort") and not param_rejected(cfg, "reasoning_effort"):
        payload["reasoning_effort"] = cfg["reasoning_effort"]
    headers = _bearer(cfg)
    prefix = cached_prefix(messages)
    if prefix is not None:
        if provider in _CACHE_KEY_PROVIDERS and not param_rejected(cfg, "prompt_cache_key"):
            payload["prompt_cache_key"] = cache_key(prefix)
        if provider == "xai":  # xAI's Chat Completions take the routing key as a header
            headers["x-grok-conv-id"] = cache_key(prefix)
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
            attempt -= 1  # a provider that doesn't take response_format / reasoning_effort / prompt_cache_key
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            retry_after = resp.headers.get("retry-after", "")
            time.sleep(float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 5 * attempt)
            continue
        if resp.status_code >= 400:
            raise ScorerError(f"HTTP {resp.status_code}: {body}")
        try:
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ScorerError(f"unexpected response shape: {body}") from exc
        record_answer(cfg, usage_from_chat, data)
        if isinstance(content, list):
            content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        if cfg.get("long_output") and isinstance(choice, dict) and choice.get("finish_reason") == "length":
            raise ScorerError("The answer was cut off at the model's length limit, so it is incomplete; "
                              "try a lower reasoning level or a model with longer answers.")
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

def web_search(cfg: dict, *, instructions: Union[str, list], input: Union[str, list], context_size: str = "medium",
               what: str = "web search", feature: str = "Web search") -> dict:
    """One web-search-grounded answer, in the normalised shape described in the module docstring.

    ``instructions``: the system prompt (a string, or text parts: Claude caches the marked ones, every other
    provider gets them as one string); ``input``: the question (text) or the conversation (OpenAI-style
    turns); ``context_size``: low | medium | high, how much the search may read; ``what``: names the request
    in error messages; ``feature``: names the feature in the "no web search" message.
    """
    kind = search_kind(cfg)
    if kind is None:
        raise ScorerError(web_search_problem(cfg, feature) or "web search is not available")
    if kind == SEARCH_ANTHROPIC:
        return _anthropic().web_search(cfg, instructions, _turns(input), context_size, what)
    instructions = text_of(instructions)
    if kind in (SEARCH_RESPONSES, SEARCH_RESPONSES_SYNC):
        return _search_responses(cfg, instructions, input, context_size, what, kind)
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
    record_answer(cfg, usage_from_responses, response)
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
    record_answer(cfg, usage_from_gemini, data)
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
    record_answer(cfg, usage_from_chat, data)
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
