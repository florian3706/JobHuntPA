"""Read web pages saved from the user's own browser.

Browsers save either plain HTML ("Webpage, HTML only" / "complete") or a
single MHTML file ("Webpage, Single File", .mhtml/.mht), which wraps the
HTML in a MIME message with quoted-printable encoding. Both come back as
the page's HTML text.
"""
from __future__ import annotations

import email
from email import policy


def decode_saved_page(data: bytes) -> str:
    head = data[:2048].lstrip().lower()
    if head.startswith(b"from:") or head.startswith(b"mime-version") or b"multipart/related" in head:
        msg = email.message_from_bytes(data, policy=policy.default)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_content()
                except (LookupError, ValueError):
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")
