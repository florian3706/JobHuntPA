"""Outgoing email through the standard library's smtplib. Design: docs/design/accounts.md, section 9.

Settings (read from the environment on every call, so a change saved in Admin takes effect at once):

    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM ("Name <address>" or an address),
    SMTP_SECURITY (starttls | ssl | none), PUBLIC_BASE_URL (the base of every link in an email).

``send`` never raises and never logs an address, a subject or a body: the log only says whether a send worked, was
skipped (not configured) or failed, plus the exception's class name. Tests capture messages with ``capture()``.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Iterator, Mapping, Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

SECURITY_MODES = ("starttls", "ssl", "none")
DEFAULT_SECURITY = "starttls"
TIMEOUT_S = 20

HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
# No quotes, whitespace or control characters: backend.config.load_dotenv strips quotes, and a space or newline could
# only be an accident or an injection. Any other printable ASCII character is fine in a password.
PASSWORD_RE = re.compile(r"[\x21\x23-\x26\x28-\x7e]{1,256}")
USER_RE = re.compile(r"[A-Za-z0-9._%+@\\-]{1,254}")
ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@" + HOST_RE.pattern + r"\.[A-Za-z0-9-]{2,63}")
NAME_RE = re.compile(r"[\w .'-]{1,60}")
_NO_NEWLINE = re.compile(r"[\r\n\x00]")


# -- validation (used by backend.app_settings.ENV_RULES) -----------------------------------------------------------

def valid_host(value: str) -> bool:
    return isinstance(value, str) and len(value) <= 253 and HOST_RE.fullmatch(value) is not None


def valid_port(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value) is not None and 1 <= int(value) <= 65535


def valid_user(value: str) -> bool:
    return isinstance(value, str) and USER_RE.fullmatch(value) is not None


def valid_password(value: str) -> bool:
    return isinstance(value, str) and PASSWORD_RE.fullmatch(value) is not None


def valid_security(value: str) -> bool:
    return value in SECURITY_MODES


def split_from(value: str) -> Optional[tuple[str, str]]:
    """``(display name, address)`` of an SMTP_FROM value ("JobHuntPA <jobs@example.com>" or a bare address), or None
    when it isn't valid. The name may be empty."""
    if not isinstance(value, str) or len(value) > 200 or _NO_NEWLINE.search(value):
        return None
    name, address = parseaddr(value)
    if not address or ADDRESS_RE.fullmatch(address) is None or len(address) > 254:
        return None
    if name and NAME_RE.fullmatch(name) is None:
        return None
    # parseaddr is forgiving; insist the whole value is exactly one name and address
    expected = f"{name} <{address}>" if name else address
    if value.strip() != expected:
        return None
    return name, address


def valid_from(value: str) -> bool:
    return split_from(value) is not None


def valid_public_base_url(value: str) -> bool:
    """http(s) with a host (and optional port and path); no login details, query or fragment, no trailing slash."""
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
    return valid_host(host) or ":" in host  # a name, an IPv4 address or an IPv6 literal


# -- configuration -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    from_name: str
    from_address: str
    security: str

    def __repr__(self) -> str:  # never the password
        return f"<SmtpConfig {self.host}:{self.port} {self.security}>"


def get_config(env: Optional[Mapping[str, str]] = None) -> Optional[SmtpConfig]:
    """The SMTP settings, or None when mail isn't configured (no host, no valid From, or an unusable value)."""
    env = os.environ if env is None else env

    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    host, sender = get("SMTP_HOST"), split_from(get("SMTP_FROM"))
    security = get("SMTP_SECURITY").lower() or DEFAULT_SECURITY
    if not host or not valid_host(host) or sender is None or security not in SECURITY_MODES:
        return None
    port_text = get("SMTP_PORT") or {"ssl": "465", "starttls": "587", "none": "25"}[security]
    if not valid_port(port_text):
        return None
    return SmtpConfig(host, int(port_text), get("SMTP_USER"), get("SMTP_PASSWORD"), sender[0], sender[1], security)


def public_base_url(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """PUBLIC_BASE_URL without a trailing slash, or None when unset or invalid."""
    value = ((os.environ if env is None else env).get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return value if value and valid_public_base_url(value) else None


def is_configured() -> bool:
    return get_config() is not None


# -- messages and sending ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SendResult:
    status: str  # "sent", "not_configured" or "failed"
    error: str = ""  # what went wrong, without any address or content

    @property
    def ok(self) -> bool:
        return self.status == "sent"

    @property
    def configured(self) -> bool:
        return self.status != "not_configured"


_outbox: Optional[list] = None


@contextlib.contextmanager
def capture() -> Iterator[list]:
    """Test hook: inside the block, ``send`` appends each EmailMessage to the yielded list instead of using SMTP
    (and counts as sent even when SMTP isn't configured)."""
    global _outbox
    previous, box = _outbox, []
    _outbox = box
    try:
        yield box
    finally:
        _outbox = previous


def build_message(to: str, subject: str, text: str, html: str, *, sender: tuple[str, str],
                  headers: Optional[Mapping[str, str]] = None) -> EmailMessage:
    """A multipart/alternative message: the text part first, the HTML part last (clients show the last one they
    can). Raises ValueError for a line break in any header value."""
    extra = dict(headers or {})
    for value in (to, subject, *extra.keys(), *extra.values()):
        if not isinstance(value, str) or _NO_NEWLINE.search(value):
            raise ValueError("A header value can't contain a line break")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(sender) if sender[0] else sender[1]
    msg["To"] = to
    for name, value in extra.items():
        msg[name] = value
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send(to: str, subject: str, text: str, html: str, headers: Optional[Mapping[str, str]] = None) -> SendResult:
    """Send one email. Never raises. Not configured -> SendResult("not_configured"); a refused or failed
    delivery -> SendResult("failed", "<ExceptionClass>")."""
    cfg = get_config()
    if cfg is None and _outbox is None:
        log.info("Email not sent: SMTP is not configured")
        return SendResult("not_configured")
    sender = (cfg.from_name, cfg.from_address) if cfg else ("JobHuntPA", "jobhuntpa@localhost.invalid")
    try:
        msg = build_message(to, subject, text, html, sender=sender, headers=headers)
    except (ValueError, TypeError):
        log.warning("Email not sent: invalid message")
        return SendResult("failed", "invalid message")
    if _outbox is not None:
        _outbox.append(msg)
        return SendResult("sent")
    try:
        _deliver(cfg, msg)
    except (OSError, smtplib.SMTPException) as exc:  # ssl.SSLError is an OSError
        log.warning("Email not sent: %s", exc.__class__.__name__)
        return SendResult("failed", exc.__class__.__name__)
    log.info("Email sent")
    return SendResult("sent")


def _deliver(cfg: SmtpConfig, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if cfg.security == "ssl":
        server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=TIMEOUT_S, context=context)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=TIMEOUT_S)
    with server as conn:
        if cfg.security == "starttls":
            conn.starttls(context=context)
        if cfg.user:
            conn.login(cfg.user, cfg.password)
        conn.send_message(msg)
