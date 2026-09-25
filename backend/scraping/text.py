"""Text helpers: HTML -> readable plain text, whitespace cleanup, hashing."""
from __future__ import annotations

import hashlib
import html as html_lib
import re

from bs4 import BeautifulSoup

_WS_RE = re.compile(r"[ \t\r\f\v ]+")
_BLANKS_RE = re.compile(r"\n{3,}")

BLOCK_TAGS = ("p", "div", "section", "article", "br", "h1", "h2", "h3", "h4", "h5", "h6",
              "ul", "ol", "table", "tr", "header", "footer")


def clean(value: object) -> str:
    """Collapse whitespace to single spaces."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def html_to_text(markup: str) -> str:
    """Convert an HTML fragment to plain text keeping paragraphs and bullets."""
    if not markup:
        return ""
    if "<" not in markup and "&" in markup:
        markup = html_lib.unescape(markup)
    if "<" not in markup:
        return normalize_block(markup)
    soup = BeautifulSoup(markup, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return normalize_block(soup.get_text())


def normalize_block(text: str) -> str:
    lines = [_WS_RE.sub(" ", line).strip() for line in (text or "").splitlines()]
    out = "\n".join(lines)
    return _BLANKS_RE.sub("\n\n", out).strip()


def description_hash(description: str) -> str:
    norm = " ".join((description or "").split()).lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
