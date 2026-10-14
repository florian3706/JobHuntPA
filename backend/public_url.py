"""The address people use to reach this JobHuntPA from other devices: ``PUBLIC_BASE_URL`` in .env.

It is optional. When it is set (for example the ``https://`` name ``tailscale serve`` gives the computer), three
things read it:

* ``backend.sessions.is_secure``: the sign-in cookie is marked ``Secure`` when the address is ``https``, even though
  the app itself is reached over plain http through the proxy on the same computer;
* the same-site check in ``backend.sessions`` accepts requests whose ``Origin`` is this address;
* the Host allowlist (``backend.hosts``) lets its host name through.

The value is checked by ``valid_public_base_url`` before it is written (``backend.app_settings.ENV_RULES``) and again
whenever it is read, so a hand-edited bad value counts as unset.
"""
from __future__ import annotations

import ipaddress
import os
import re
from typing import Mapping, Optional
from urllib.parse import urlsplit

HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")


def valid_host(value: str) -> bool:
    return isinstance(value, str) and len(value) <= 253 and HOST_RE.fullmatch(value) is not None


def is_local_host(host: str) -> bool:
    """``localhost`` or a loopback address (127.x.x.x, ::1): this computer."""
    host = (host or "").lower().strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def valid_public_base_url(value: str) -> bool:
    """http(s) with a host (and optional port and path); no login details, query or fragment, no trailing slash.
    Plain http only for this computer: anyone on the network could read a sign-in over http."""
    if not isinstance(value, str) or not 1 <= len(value) <= 300 or not re.fullmatch(r"[A-Za-z0-9._~:/\[\]+-]+", value):
        return False
    try:
        parts = urlsplit(value)
        host, _port = parts.hostname, parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return False
    if parts.query or parts.fragment or value.endswith("/"):
        return False
    if parts.scheme == "http" and not is_local_host(host):
        return False
    return valid_host(host) or ":" in host  # a name, an IPv4 address or an IPv6 literal


def public_base_url(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """PUBLIC_BASE_URL without a trailing slash, or None when unset or invalid."""
    value = ((os.environ if env is None else env).get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return value if value and valid_public_base_url(value) else None
