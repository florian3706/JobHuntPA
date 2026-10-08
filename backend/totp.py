"""Authenticator-app codes (TOTP, RFC 6238) for two-step sign-in: accounts design §3.6 and §6.3. Nothing here touches
the database; backend/users.py keeps the secrets (under the server key, so a password reset by email can still ask
for a code) and backend/auth.py and backend/security.py use these helpers.

Plain settings, so every authenticator app works: SHA-1, 6 digits, 30-second steps. A code is accepted for the step
before, the step itself and the step after (clocks drift). The caller refuses a step that was already used
(``users.verify_totp`` keeps ``users.totp_last_step``), so a code can't be replayed inside that window.
"""
from __future__ import annotations

import hmac
import re
import time
import unicodedata
from typing import Any, Optional

import pyotp
import segno

ISSUER = "JobHuntPA"
STEP_SECONDS = 30
WINDOW = 1                  # steps either side of now
DIGITS = 6
_CODE_RE = re.compile(r"[0-9]{6}")


def now() -> float:
    """The time codes are checked against (tests replace it)."""
    return time.time()


def new_secret() -> str:
    """A random base32 secret (160 bits)."""
    return pyotp.random_base32()


def grouped(secret: str) -> str:
    """The secret in groups of four, for reading out or typing by hand: ``ABCD EFGH ...``."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def provisioning_uri(secret: str, account_name: str) -> str:
    """The ``otpauth://`` address that authenticator apps read from a QR code (or open as a link on the same device)."""
    return pyotp.TOTP(secret).provisioning_uri(name=account_name or "account", issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    """The address as an inline SVG QR code with a ``<title>`` (drawn here, so the secret never goes to a script on
    another site). Black on white with the standard 4-module quiet zone, whatever the page's theme; it scales with
    its container."""
    svg = segno.make(uri, error="m").svg_inline(
        title="QR code for your authenticator app", desc="Scan this with an authenticator app. The same setup key is "
                                                          "written out below as text.",
        dark="#000000", light="#ffffff", border=4, omitsize=True, svgclass="qr")
    return svg.replace("<svg ", '<svg role="img" ', 1)


def normalise_code(text: Any) -> Optional[str]:
    """The six digits of a typed code (spaces and hyphens ignored), or None if it can't be one."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    code = re.sub(r"[\s\-]", "", unicodedata.normalize("NFKC", text))
    return code if _CODE_RE.fullmatch(code) else None


def code_at(secret: str, step: int) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS).at(step * STEP_SECONDS)


def current_step(at: Optional[float] = None) -> int:
    return int((now() if at is None else at) // STEP_SECONDS)


def match_step(secret: str, code: Any, at: Optional[float] = None) -> Optional[int]:
    """The time step ``code`` is for (the newest, if it fits more than one), within one step of now; None if it
    isn't a code for any of them. Every step is compared, in constant time, whatever the answer."""
    typed = normalise_code(code)
    if typed is None:
        return None
    centre = current_step(at)
    found: Optional[int] = None
    for step in range(centre + WINDOW, centre - WINDOW - 1, -1):  # newest first
        if hmac.compare_digest(code_at(secret, step).encode("ascii"), typed.encode("ascii")) and found is None:
            found = step
    return found

