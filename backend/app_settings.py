"""App-wide settings made in the dashboard's Settings tab and kept in .env: the LLM (provider address, model, API
key) and the API keys for commute times.

    GET  /api/settings/keys        {"tfnsw": {"set": bool, "hint": "…abcd" | null}, "tomtom": {...}}  (never a key)
    PUT  /api/settings/keys        {"tfnsw"?: "<key>", "tomtom"?: "<key>"}: "" clears a key, an omitted one is unchanged
    POST /api/settings/keys/test   one cheap real request per saved key: {"tfnsw": {"ok": true}, "tomtom": {"ok": false, "error": "…"}}

    GET  /api/settings/llm         {provider, base_url, model, key: {set, hint}, presets, capabilities}  (never the key)
    PUT  /api/settings/llm         {"base_url"?, "model"?, "api_key"?}: same omitted / "" semantics; see put_llm for the
                                   rule that moving to another server needs the key typed again

A saved value takes effect at once (os.environ is updated; backend.commute.keys() and backend.scorer.get_config()
read it on every call) and is written to .env (backend.config.ENV_PATH) so it survives a restart. That file holds the
LLM key, which costs the owner money, and the app can be reachable by other devices, so writing it is locked down:
  * only the names in ENV_RULES can be written, whatever a request says, and each has its own strict value rule
    (keys: KEY_RE; model: MODEL_RE; address: normalise_base_url): no whitespace, newline, "=", "#" or quote, so a
    value can't start a new line or a second setting;
  * the LLM address can't be pointed at another server without the API key typed again in the same request, so a
    saved key can't be redirected to a server someone else controls;
  * the file is replaced atomically (temp file in the same folder, then os.replace), keeping every other line
    and comment, and its permissions (a new file is owner-only).
"""
from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional, Union
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from backend import commute, config, llm_providers, scorer

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

KEY_VARS = {"tfnsw": "TFNSW_API_KEY", "tomtom": "TOMTOM_API_KEY"}
LABELS = {"tfnsw": "Transport for NSW", "tomtom": "TomTom"}
# API keys: TfNSW's are long JWT-like strings (dots), TomTom's short and alphanumeric, OpenAI's "sk-proj-…" and
# Anthropic's "sk-ant-api03-…" long with - and _, Gemini's "AIza…" letters, digits, - and _.
KEY_RE = re.compile(r"[A-Za-z0-9._~+/-]{8,4000}")
# Model ids: OpenRouter's contain "/" (anthropic/claude-sonnet-5.5), Ollama's ":" (llama3.2:3b).
MODEL_RE = re.compile(r"[A-Za-z0-9._:/@+-]{1,200}")
# An API address: letters, digits and . _ ~ : / [ ] + - only (no whitespace, quotes, "#", "?", "@" or "%").
URL_RE = re.compile(r"[A-Za-z0-9._~:/\[\]+-]{1,300}")
HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
URL_HELP = ("The API address must look like https://api.example.com/v1 (up to 300 characters, no spaces, quotes, # or "
            "login details). http:// is only for localhost.")


def _is_ip(host: str) -> Union[ipaddress.IPv4Address, ipaddress.IPv6Address, None]:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def normalise_base_url(text: str) -> str:
    """The API address as it is stored (trailing slashes removed), or ValueError(why not): https with a real host,
    or http only for localhost, 127.0.0.0/8 and [::1] (a local Ollama); no login details, query or fragment."""
    if not URL_RE.fullmatch(text):
        raise ValueError(URL_HELP)
    try:
        parts = urlsplit(text)
        host, _port = parts.hostname, parts.port
    except ValueError:
        raise ValueError(URL_HELP) from None
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        raise ValueError(URL_HELP)
    ip = _is_ip(host or "")
    if not host or not (ip or (len(host) <= 253 and HOST_RE.fullmatch(host))):
        raise ValueError(URL_HELP)
    if parts.scheme == "http" and not (host == "localhost" or (ip is not None and ip.is_loopback)):
        raise ValueError("http:// is only allowed for localhost. Other addresses must start with https://.")
    return text.rstrip("/")


def origin_of(base_url: str) -> Optional[tuple]:
    """(scheme, host, port) of an address: what makes it "the same server". None for an empty address; an address
    that can't be read is never equal to anything else."""
    if not base_url:
        return None
    try:
        parts = urlsplit(base_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return ("?", base_url)
    if not host:
        return ("?", base_url)
    scheme = parts.scheme.lower()
    return (scheme, host, port or (443 if scheme == "https" else 80))


def _plain_url(value: str) -> bool:
    try:
        return normalise_base_url(value) == value
    except ValueError:
        return False


# The only variables this module may write, each with the rule its (non-empty) value must meet in full.
ENV_RULES: dict[str, Callable[[str], bool]] = {
    "TFNSW_API_KEY": lambda v: KEY_RE.fullmatch(v) is not None,
    "TOMTOM_API_KEY": lambda v: KEY_RE.fullmatch(v) is not None,
    "LLM_API_KEY": lambda v: KEY_RE.fullmatch(v) is not None,
    "LLM_BASE_URL": _plain_url,
    "LLM_MODEL": lambda v: MODEL_RE.fullmatch(v) is not None,
}

_write_lock = threading.Lock()


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------

def _assigned_name(line: str) -> Optional[str]:
    """The variable a .env line sets (read the way backend.config.load_dotenv reads it), else None."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    return line.partition("=")[0].strip()


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set ``NAME=value`` in the .env file at ``path``: in place when the file already assigns NAME (every
    line that does), appended when it doesn't. An empty value clears: the line stays, as ``NAME=``. All other
    lines and comments are kept. The file is created when missing. Raises ValueError for a name outside
    ENV_RULES or a value that isn't empty and doesn't meet that name's rule in full; nothing is written then."""
    for name, value in updates.items():
        rule = ENV_RULES.get(name)
        if rule is None:
            raise ValueError(f"{name} can't be changed here")
        if not isinstance(value, str) or (value and not rule(value)):
            raise ValueError(f"The value for {name} isn't allowed")
    with _write_lock:
        real = Path(os.path.realpath(path))  # a symlinked .env is updated where it lives
        try:
            lines = real.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            lines = []
        out, done = [], set()
        for line in lines:
            name = _assigned_name(line)
            if name in updates:
                out.append(f"{name}={updates[name]}")
                done.add(name)
            else:
                out.append(line)
        out += [f"{name}={value}" for name, value in updates.items() if name not in done]
        _replace_file(real, "\n".join(out) + "\n")


def _replace_file(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env-", suffix=".tmp")  # owner-only (0600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(FileNotFoundError):
            shutil.copymode(path, tmp)  # keep an existing file's permissions
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def _hint(key: str) -> Optional[str]:
    """The end of a key ("…abcd"): at most 4 characters and a quarter of it, never the whole key."""
    n = min(4, len(key) // 4)
    return f"…{key[-n:]}" if n else None


def key_status() -> dict:
    keys = commute.keys()
    return {name: {"set": bool(keys[name]), "hint": _hint(keys[name])} for name in KEY_VARS}


class KeysIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown field is a 422, not silently dropped
    tfnsw: Optional[str] = None
    tomtom: Optional[str] = None


@router.get("/keys")
def get_keys():
    return key_status()


def _save(updates: dict[str, str]) -> None:
    """Write ``updates`` to .env, and only then to os.environ, so what runs matches what a restart would run."""
    try:
        update_env_file(config.ENV_PATH, updates)
    except (OSError, UnicodeError) as exc:
        log.warning("Could not update %s: %s", config.ENV_PATH, exc.__class__.__name__)
        raise HTTPException(status_code=500, detail="Could not save to the .env file (is it writable?). "
                                                    "Nothing was changed.") from exc
    for var, value in updates.items():
        if value:
            os.environ[var] = value
        else:
            os.environ.pop(var, None)


@router.put("/keys")
def put_keys(body: KeysIn):
    updates: dict[str, str] = {}
    for name, var in KEY_VARS.items():
        value = getattr(body, name)
        if value is None:
            continue
        if value and not KEY_RE.fullmatch(value):  # no echo of the value in the message
            raise HTTPException(status_code=422, detail=f"The {LABELS[name]} key must be 8 to 4000 letters, digits "
                                                        "or . _ ~ + / - characters, with no spaces or line breaks.")
        updates[var] = value
    if updates:
        _save(updates)
    return key_status()


def _probe_tfnsw(key: str) -> None:
    data = commute._tfnsw("stop_finder", {"type_sf": "any", "name_sf": "Central Station", "TfNSWSF": "true"}, key)
    if not data.get("locations"):
        raise commute.CommuteError("Transport for NSW: no answer to a stop lookup")


def _probe_tomtom(key: str) -> None:
    url = f"{commute.TOMTOM}-33.87320,151.20690:-33.88300,151.20630/json"  # Town Hall to Central, Sydney
    if not commute._get(url, {"key": key, "traffic": "false"}, what="TomTom").get("routes"):
        raise commute.CommuteError("TomTom: no route found")


@router.post("/keys/test")
def test_keys():
    """One cheap real request per saved key. Keys that aren't set are left out of the answer."""
    keys, out = commute.keys(), {}
    for name, probe in (("tfnsw", _probe_tfnsw), ("tomtom", _probe_tomtom)):
        if not keys[name]:
            continue
        try:
            probe(keys[name])
            out[name] = {"ok": True}
        except commute.CommuteError as exc:
            out[name] = {"ok": False, "error": str(exc).replace(keys[name], "…")}
    return out


# --------------------------------------------------------------------------
# LLM: provider address, model, API key
# --------------------------------------------------------------------------

def llm_state() -> dict:
    cfg = scorer.get_config()
    return {
        "provider": llm_providers.provider_for(cfg["base_url"]),
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "key": {"set": bool(cfg["api_key"]), "hint": _hint(cfg["api_key"])},
        "presets": llm_providers.PRESETS,
        "capabilities": llm_providers.capabilities(cfg),
    }


class LlmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None


@router.get("/llm")
def get_llm():
    return llm_state()


@router.put("/llm")
def put_llm(body: LlmIn):
    """Save the LLM address, model and/or key. ``""`` clears a value, an omitted (or null) one is unchanged.

    Moving to a different server (another scheme, host or port than the saved address) needs ``api_key`` in the same
    request, so a saved key can't be redirected to a server someone else controls: without it the request is a 422.
    Moving to a provider that needs no key (a local Ollama) drops the saved key instead of keeping it."""
    updates: dict[str, str] = {}
    key = body.api_key
    if key and not KEY_RE.fullmatch(key):  # no echo of the value in any message
        raise HTTPException(status_code=422, detail="The API key must be 8 to 4000 letters, digits or . _ ~ + / - "
                                                    "characters, with no spaces or line breaks.")
    if body.model and not MODEL_RE.fullmatch(body.model):
        raise HTTPException(status_code=422, detail="The model name may contain only letters, digits and . _ : / @ + - "
                                                    "(up to 200 characters), with no spaces.")
    if body.model is not None:
        updates["LLM_MODEL"] = body.model
    if body.base_url is not None:
        try:
            base = normalise_base_url(body.base_url) if body.base_url else ""
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        saved = (os.getenv("LLM_BASE_URL") or "").strip().rstrip("/")
        if base and origin_of(base) != origin_of(saved):
            preset = next(p for p in llm_providers.PRESETS if p["id"] == llm_providers.provider_for(base))
            if not preset["key_required"]:
                key = key or ""  # nothing to send, and the old key must not stay around for a later address change
            elif not key:
                raise HTTPException(status_code=422, detail=(
                    f"This changes the API address to a different server ({preset['label']}), so enter its API key "
                    "again in the same save. The key saved for the old address is never sent anywhere else."))
        updates["LLM_BASE_URL"] = base
    if key is not None:
        updates["LLM_API_KEY"] = key
    if updates:
        _save(updates)
    return llm_state()
