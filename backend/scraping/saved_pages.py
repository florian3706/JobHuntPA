"""Read web pages saved from the user's own browser.

Browsers save either plain HTML ("Webpage, HTML only" / "complete") or a
single MHTML file ("Webpage, Single File", .mhtml/.mht), which wraps the
HTML in a MIME message with quoted-printable encoding. Both come back as
the page's HTML text.

``read_saved_page`` also says where the page was saved from
(``page_address``): an MHTML file's own header, Chrome's "saved from url"
note at the top of a saved HTML page, or the address the page gives itself
(its canonical link, or ``og:url``).
"""
from __future__ import annotations

import email
import re
from email import policy
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, SoupStrainer

URL_MAX = 2048
# Chrome (and Edge) start a saved page with <!-- saved from url=(0047)https://... -->: the number is the address's length.
_SAVED_FROM = re.compile(r"<!--\s*saved from url=\(\d{1,5}\)([^\s>]{1,2048}?)\s*-->", re.I)


def _read(data: bytes) -> tuple[str, str]:
    """(The page's HTML, the address an MHTML file says it was saved from, or "")."""
    head = data[:2048].lstrip().lower()
    if head.startswith(b"from:") or head.startswith(b"mime-version") or b"multipart/related" in head:
        msg = email.message_from_bytes(data, policy=policy.default)
        saved = str(msg.get("Snapshot-Content-Location") or "")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                saved = saved or str(part.get("Content-Location") or "")
                try:
                    return part.get_content(), saved
                except (LookupError, ValueError):
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace"), saved
    return data.decode("utf-8", errors="replace"), ""


def decode_saved_page(data: bytes) -> str:
    return _read(data)[0]


def web_address(value: Optional[str]) -> str:
    """``value`` when it is an http(s) address with a host and no user name in it, else ""."""
    value = (value or "").strip()
    if not value or len(value) > URL_MAX:
        return ""
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return ""
    return value if parts.scheme in ("http", "https") and host and "@" not in parts.netloc else ""


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _own_address(html: str) -> str:
    """The address the page gives itself: its canonical link, else its ``og:url``."""
    soup = BeautifulSoup(html, "lxml", parse_only=SoupStrainer(["link", "meta"]))
    canonical = next((web_address(el.get("href")) for el in soup.find_all("link", href=True)
                      if "canonical" in [r.lower() for r in (el.get("rel") or [])]), "")
    og = next((web_address(el.get("content")) for el in soup.find_all("meta", content=True)
               if (el.get("property") or "").lower() == "og:url"), "")
    return canonical or og


def site_name(html: str) -> str:
    """The name the page gives its site (``og:site_name``), or ""."""
    soup = BeautifulSoup(html, "lxml", parse_only=SoupStrainer("meta"))
    return next((" ".join(el["content"].split())[:200] for el in soup.find_all("meta", content=True)
                 if (el.get("property") or "").lower() == "og:site_name" and el["content"].strip()), "")


def read_saved_page(data: bytes) -> tuple[str, str]:
    """(The page's HTML, ``page_address``)."""
    html, saved = _read(data)
    return html, page_address(html, saved)


def page_address(html: str, saved: str = "") -> str:
    """Where a saved page was saved from, or "" when it doesn't say: the browser's own note of it (``saved``, an
    MHTML file's header, or Chrome's "saved from url" comment), with the address the page gives itself preferred when
    that is on the same site (it seldom carries tracking parameters); else the page's own address."""
    saved = web_address(saved)
    if not saved:
        m = _SAVED_FROM.search(html[:4096])
        saved = web_address(m.group(1)) if m else ""
    own = _own_address(html)
    if saved and own and _host(own) != _host(saved):
        return saved
    return own or saved
