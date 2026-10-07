"""The Host allowlist: a defence against DNS rebinding (accounts design, Phase L).

A web page on another site can make its own name point at this computer (``evil.example`` answering 192.168.1.20
after the page has loaded), then read the app's answers as if they were its own site. The browser still sends
``Host: evil.example``, so the app refuses any Host it doesn't know, before anything else runs: 400, with a plain
message. The session cookie, the Origin check and the CSRF token don't help here, because to the browser the request
is same-site.

Allowed (the port never matters):

* any IP address (an address can't be rebound; people reach the app by its LAN or Tailscale address),
* ``localhost`` and ``*.localhost``,
* this computer's own names: ``socket.gethostname()``, its first label, either with ``.local``, and
  ``socket.getfqdn()``,
* the host of PUBLIC_BASE_URL (so the admin's Email settings can open the app to its public name),
* the names in JOBHUNT_ALLOWED_HOSTS (comma-separated, for example the name ``tailscale serve`` passes on),
* ``testserver``, only while JOBHUNT_TESTING=1 (Starlette's TestClient).

Behind uvicorn's proxy headers the Host is still what the browser sent: ``--proxy-headers`` changes the client
address and the scheme, not the Host header.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from functools import lru_cache
from typing import Optional
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

log = logging.getLogger(__name__)

# A host name as people type it: letters, digits and hyphens in dot-separated labels (underscores turn up in
# machine names). No wildcards.
_LABEL = r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?"
NAME_RE = re.compile(rf"{_LABEL}(\.{_LABEL})*")
_PORT_RE = re.compile(r"[0-9]{1,5}")

_warned: set[str] = set()


def parse_host(value: str) -> Optional[str]:
    """The lowercase host of a Host header value, without port, brackets or a trailing dot; None if it isn't a
    plain host name or IP address (userinfo, a path, spaces, a broken bracket or port)."""
    value = (value or "").strip().lower()
    if not value or len(value) > 300:
        return None
    if value.startswith("["):  # [::1] or [::1]:8040
        end = value.find("]")
        rest = value[end + 1:] if end > 0 else "x"
        if rest and not (rest.startswith(":") and _PORT_RE.fullmatch(rest[1:])):
            return None
        name = value[1:end]
        try:
            return str(ipaddress.IPv6Address(name))
        except ValueError:
            return None
    name, colon, port = value.partition(":")
    if colon and not _PORT_RE.fullmatch(port):
        return None
    name = name.rstrip(".")
    return name if name and len(name) <= 253 and NAME_RE.fullmatch(name) else None


def is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


@lru_cache(maxsize=1)
def machine_names() -> frozenset[str]:
    """This computer's own names. Looked up once: getfqdn() can ask the network."""
    names: set[str] = set()
    try:
        host = socket.gethostname().lower().rstrip(".")
        short = host.split(".")[0]
        names.update({host, short, f"{short}.local", f"{host}.local"})
        names.add(socket.getfqdn().lower().rstrip("."))
    except OSError:
        pass
    return frozenset(n for n in names if n and NAME_RE.fullmatch(n))


def configured_names(env=None) -> set[str]:
    """The names the admin allowed: PUBLIC_BASE_URL's host and JOBHUNT_ALLOWED_HOSTS. Read at each request, because
    the public address can be changed in the app while it runs. A bad entry is ignored (and said once)."""
    env = os.environ if env is None else env
    names: set[str] = set()
    base = (env.get("PUBLIC_BASE_URL") or "").strip()
    if base:
        try:
            host = urlsplit(base).hostname
        except ValueError:
            host = None
        if host:
            names.add(host.lower().rstrip("."))
    for item in (env.get("JOBHUNT_ALLOWED_HOSTS") or "").split(","):
        item = item.strip().lower().rstrip(".")
        if not item or is_ip(item):  # addresses are always allowed
            continue
        if NAME_RE.fullmatch(item):
            names.add(item)
        elif item not in _warned and len(_warned) < 20:
            _warned.add(item)
            log.warning("JOBHUNT_ALLOWED_HOSTS: ignoring an entry that isn't a host name (use names only, "
                        "like jobs.example.com: no http://, port or path)")
    return names


def is_allowed(host_header: str, env=None) -> bool:
    name = parse_host(host_header)
    if name is None:
        return False
    if is_ip(name) or name == "localhost" or name.endswith(".localhost"):
        return True
    if name == "testserver" and (os.environ if env is None else env).get("JOBHUNT_TESTING") == "1":
        return True
    return name in machine_names() or name in configured_names(env)


class HostGuard:
    """Answers 400 to a request whose Host isn't allowed, before anything else (sessions, routes, files) runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        host = Headers(scope=scope).get("host", "")
        if is_allowed(host):
            return await self.app(scope, receive, send)
        name = parse_host(host)
        if name and name not in _warned and len(_warned) < 20:  # once per name: a rebinding page can ask a lot
            _warned.add(name)
            log.warning("Refused a request for the host %s: it isn't allowed (see JOBHUNT_ALLOWED_HOSTS)", name)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        shown = f" ({name})" if name else ""
        response = PlainTextResponse(
            f"This address{shown} isn't allowed. Open JobHuntPA from the address it printed when it started, "
            "or ask the person who runs it to add this name to JOBHUNT_ALLOWED_HOSTS or the public address.",
            status_code=400, headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)
