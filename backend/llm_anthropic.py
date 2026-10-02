"""Claude through Anthropic's own Messages API, using the official ``anthropic`` SDK (1.x).

Only backend/llm_providers.py imports this module, and only when ``LLM_BASE_URL`` is api.anthropic.com. The
OpenAI-compatible endpoint is never used for Claude, and the SDK is given its default base URL.

    chat(messages, cfg, json_mode=True) -> str
    web_search(cfg, instructions, turns, context_size, what) -> dict   (the normalised search response of
                                                                        llm_providers, see its docstring)
    probe_level(cfg, level) -> None | str                               (reasoning-level detection)

How the request differs from the OpenAI-style ones:
  * the system prompt is the top-level ``system``; there is no ``response_format`` (the prompts ask for JSON and
    the app's tolerant parser does the rest) and no assistant prefill (a 400 on current models);
  * the reasoning level is ``output_config={"effort": ...}``, sent only when a level is set. No ``thinking``
    parameter is sent: ``budget_tokens`` and ``disabled`` are 400s on current models;
  * Claude Fable 5.1, Opus 5.5, Opus 5 and Sonnet 5.5 go through the beta endpoint with
    ``fallbacks="default"``, so the API reruns a refused request on a fallback model;
  * web search is the server tool ``web_search``: results, errors and citations come back in the same response,
    and a long search ends with ``stop_reason == "pause_turn"`` until the request is sent again.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

import anthropic

from backend.llm_providers import COUNTRY, ScorerError, search_timeout, param_rejected, remember_rejected

log = logging.getLogger(__name__)

MAX_TOKENS = 16000
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_CONTINUATIONS = 5
SEARCH_USES = {"low": 5, "medium": 10, "high": 20}  # web_search max_uses per request
# Models that take the newer web search tool (dynamic filtering); older ones, e.g. claude-haiku-4-5, the basic one.
_NEW_SEARCH_MODELS = ("claude-fable-", "claude-mythos-", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
                      "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6")


def search_tool_type(model: str) -> str:
    return "web_search_20260209" if (model or "").startswith(_NEW_SEARCH_MODELS) else "web_search_20250305"


def effort_param(level: Optional[str]) -> Optional[str]:
    """The ``effort`` for an app reasoning level; levels Claude has no equivalent of (none, minimal) send nothing."""
    return level if level in EFFORTS else None


# --------------------------------------------------------------------------
# Client and errors
# --------------------------------------------------------------------------

_clients: dict[tuple[str, float], "anthropic.Anthropic"] = {}
_lock = threading.Lock()


def _client(cfg: dict) -> "anthropic.Anthropic":
    """The SDK client for this key and timeout (reused, so connections are)."""
    key = (cfg.get("api_key") or "", float(cfg.get("timeout_s") or 180))
    with _lock:
        if key not in _clients:
            _clients[key] = anthropic.Anthropic(api_key=key[0], timeout=key[1])
        return _clients[key]


def _create(cfg: dict, kwargs: dict) -> Any:
    client = _client(cfg)
    if (cfg.get("model") or "").strip() in FALLBACK_MODELS:
        return client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
    return client.messages.create(**kwargs)


def _status_error(exc: "anthropic.APIStatusError") -> ScorerError:
    message = str(getattr(exc, "message", "") or exc)[:600]
    rejected = isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError))
    if rejected:
        return ScorerError(f"HTTP {exc.status_code}: API key rejected by Anthropic ({message})", auth=True)
    # Nothing else will work until the account is topped up: stop batch runs at the first one.
    return ScorerError(f"HTTP {exc.status_code}: {message}", auth="credit balance" in message.lower())


def _guard(call: Callable[[], Any]) -> Any:
    """Run an SDK call; its errors become ScorerError."""
    try:
        return call()
    except anthropic.APIStatusError as exc:
        raise _status_error(exc) from exc
    except anthropic.APIConnectionError as exc:  # includes timeouts
        raise ScorerError(f"connection error: {exc.__class__.__name__}: {exc}") from exc
    except anthropic.AnthropicError as exc:
        raise ScorerError(str(exc)) from exc


def split_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """(system prompt, conversation) from OpenAI-style messages."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system").strip()
    turns = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("role") in ("user", "assistant")]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)  # a conversation starts with the user
    if not turns:
        raise ScorerError("nothing to send: the conversation has no user message")
    return system, turns


def _send(cfg: dict, system: str, messages: list[dict], *, tools: Optional[list[dict]] = None) -> Any:
    kwargs: dict[str, Any] = {"model": cfg["model"], "max_tokens": MAX_TOKENS, "messages": messages}
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    effort = effort_param(cfg.get("reasoning_effort"))
    if effort and not param_rejected(cfg, "reasoning_effort"):
        kwargs["output_config"] = {"effort": effort}
    try:
        return _guard(lambda: _create(cfg, kwargs))
    except ScorerError as exc:
        if "output_config" not in kwargs or not str(exc).startswith("HTTP 400") or "effort" not in str(exc).lower():
            raise
        kwargs.pop("output_config")  # a model without effort (Haiku 4.5): send without, and don't try again
        remember_rejected(cfg, "reasoning_effort")
        return _guard(lambda: _create(cfg, kwargs))


def _check_stop(resp: Any) -> None:
    """Refusals and cut-off answers are errors; pause_turn is handled by the caller."""
    if resp.stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        why = ": ".join(str(x) for x in (getattr(details, "category", None), getattr(details, "explanation", None)) if x)
        raise ScorerError("Claude declined this request" + (f" ({why})" if why else "") + ".")
    if resp.stop_reason == "max_tokens":
        raise ScorerError("Claude's answer was cut off at the length limit; try a lower reasoning level.")


def _text(content: list) -> str:
    return "".join(b.text for b in content if getattr(b, "type", "") == "text")


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------

def chat(messages: list[dict], cfg: dict, *, json_mode: bool = True) -> str:
    """One answer. ``json_mode`` has no switch in this API: the prompt asks for JSON."""
    system, turns = split_messages(messages)
    resp = _send(cfg, system, turns)
    _check_stop(resp)
    return _text(resp.content)


def probe_level(cfg: dict, level: str) -> Optional[str]:
    """One tiny request at this effort. None = accepted; a 400 means this model doesn't take it (Haiku 4.5
    takes none)."""
    kwargs = {"model": cfg["model"], "max_tokens": 512, "output_config": {"effort": level},
              "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    try:
        _create(cfg, kwargs)
    except anthropic.APIStatusError as exc:
        err = _status_error(exc)
        if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            raise ScorerError(f"API key rejected (HTTP {exc.status_code}).", auth=True) from exc
        if isinstance(exc, anthropic.NotFoundError) or err.auth:
            raise err from exc
        return str(err)[:200]
    except anthropic.APIConnectionError as exc:
        raise ScorerError(f"Could not reach Anthropic: {exc}") from exc
    except anthropic.AnthropicError as exc:
        raise ScorerError(str(exc)) from exc
    return None


# --------------------------------------------------------------------------
# Web search
# --------------------------------------------------------------------------

def web_search(cfg: dict, instructions: str, turns: list[dict], context_size: str, what: str) -> dict:
    """A search-grounded answer. The server tool runs inside the request; when it pauses the turn
    (``pause_turn``) the request is sent again with the paused turn appended, up to MAX_CONTINUATIONS times."""
    cfg = {**cfg, "timeout_s": search_timeout(cfg)}  # a search with many lookups takes minutes
    system, base = split_messages([{"role": "system", "content": instructions}, *turns])
    tool = {"type": search_tool_type(cfg["model"]), "name": "web_search",
            "max_uses": SEARCH_USES.get(context_size, SEARCH_USES["medium"]),
            "user_location": {"type": "approximate", "country": COUNTRY}}
    blocks: list = []
    resp = _send(cfg, system, base, tools=[tool])
    for continuation in range(MAX_CONTINUATIONS + 1):
        _check_stop(resp)
        blocks += list(resp.content)
        if resp.stop_reason != "pause_turn" or continuation == MAX_CONTINUATIONS:
            break
        resp = _send(cfg, system, [*base, {"role": "assistant", "content": _echo(blocks)}], tools=[tool])
    return to_response(blocks, getattr(resp, "model", None) or cfg["model"], what)


def _echo(blocks: list) -> list:
    """The turn so far, as sent back after a pause. If a fallback model took over mid-turn, Anthropic's rule is
    to leave out what came before the last ``fallback`` block and only the model could read: thinking, tool_use,
    and server tool calls without a result."""
    kinds = [getattr(b, "type", "") for b in blocks]
    boundary = max((i for i, k in enumerate(kinds) if k == "fallback"), default=None)
    if boundary is None:
        return list(blocks)
    answered = {b.tool_use_id for b, k in zip(blocks, kinds) if k.endswith("_tool_result")}
    return [b for i, (b, k) in enumerate(zip(blocks, kinds))
            if not (i < boundary and (k in ("thinking", "redacted_thinking", "tool_use")
                                      or (k == "server_tool_use" and b.id not in answered)))]


def to_response(blocks: list, model: str, what: str = "web search") -> dict:
    """The normalised search response for the content blocks of a finished (possibly resumed) turn.

    Pages come from ``web_search_tool_result`` blocks: a list of results on success, but a single error object
    on failure (errors don't raise). The answer is the text after the last tool call, so narration like "Let me
    search" isn't mistaken for it. Citations on any text block count as cited pages."""
    searched: dict[str, str] = {}
    cited: dict[str, str] = {}
    errors: list[str] = []
    text = ""
    for block in blocks:
        kind = getattr(block, "type", "")
        if kind == "web_search_tool_result":
            content = block.content
            if isinstance(content, list):
                for result in content:
                    if getattr(result, "url", None):
                        searched.setdefault(result.url, getattr(result, "title", None) or "")
            else:
                errors.append(str(getattr(content, "error_code", None) or "unknown error"))
        if kind == "server_tool_use" or kind.endswith("_tool_result"):
            text = ""
        elif kind == "text":
            text += block.text
            for cite in getattr(block, "citations", None) or []:
                if getattr(cite, "url", None):
                    cited.setdefault(cite.url, getattr(cite, "title", None) or "")
    if errors and not searched:
        raise ScorerError(f"{what} ended with status failed: web search error ({', '.join(errors)})")
    if not text.strip():
        raise ScorerError(f"{what} ended with status failed: Claude gave no answer after searching.")
    return {"status": "completed", "model": model, "output": [
        {"type": "web_search_call", "results": [{"url": u, "title": t} for u, t in searched.items()]},
        {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": [
            {"type": "url_citation", "url": u, "title": t} for u, t in cited.items()]}]}]}
