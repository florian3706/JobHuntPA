"""Text helpers: HTML -> readable plain text, whitespace cleanup, hashing."""
from __future__ import annotations

import hashlib
import html as html_lib
import re

from bs4 import BeautifulSoup, CData, NavigableString, Tag

_WS_RE = re.compile(r"[ \t\r\f\v ]+")
_BLANKS_RE = re.compile(r"\n{3,}")

BLOCK_TAGS = ("p", "div", "section", "article", "br", "h1", "h2", "h3", "h4", "h5", "h6",
              "ul", "ol", "table", "tr", "header", "footer")


def clean(value: object) -> str:
    """Collapse whitespace to single spaces."""
    if value is None:
        return ""
    return " ".join(str(value).split())


_LEFT_OUT = ("script", "style", "noscript", "svg", "template")
_TEXT = (NavigableString, CData)  # the strings get_text() reads (not comments, nor the text of <rt> and the like)
_AFTER = object()


def html_to_text(markup: str) -> str:
    """Convert an HTML fragment to plain text keeping paragraphs and bullets: "- " before each list item, a line
    break before and after each block, scripts, styles and the like left out. One pass over the parsed fragment,
    without changing it: inserting the breaks into the tree took time in proportion to the square of a long list of
    siblings, and the fragment can come from any page (a job's JSON-LD description)."""
    if not markup:
        return ""
    if "<" not in markup and "&" in markup:
        markup = html_lib.unescape(markup)
    if "<" not in markup:
        return normalize_block(markup)
    soup = BeautifulSoup(markup, "lxml")
    out: list[str] = []
    stack: list = [soup]
    while stack:
        node = stack.pop()
        if node is _AFTER:
            out.append("\n")
        elif isinstance(node, Tag):
            if node.name in _LEFT_OUT:
                continue
            if node.name == "li":
                out.append("\n- ")
            elif node.name in BLOCK_TAGS:
                out.append("\n")
                stack.append(_AFTER)
            stack.extend(reversed(node.contents))
        elif type(node) in _TEXT:
            out.append(node)
    return normalize_block("".join(out))


def normalize_block(text: str) -> str:
    lines = [_WS_RE.sub(" ", line).strip() for line in (text or "").splitlines()]
    out = "\n".join(lines)
    return _BLANKS_RE.sub("\n\n", out).strip()


def description_hash(description: str) -> str:
    norm = " ".join((description or "").split()).lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
