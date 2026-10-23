"""Plain text for what the model writes: invisible characters gone, look-alikes turned into ordinary ones, accents
dropped, and only a fixed set of characters kept. Used on model output, never on the user's own words."""
from __future__ import annotations

import re
import unicodedata

_LOOKALIKES = {
    "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "‚": "'",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...", "•": "-", "◦": "-", "▪": "-",
}
# Letters NFKD does not take apart.
_LETTERS = {"ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
            "ł": "l", "Ł": "L", "þ": "th", "Þ": "Th"}
_DISALLOWED = re.compile(r"[^a-zA-Z0-9 ,.!?;:*@#$%^&*()_\-|\"'/\\\n\t]")


def plain_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cf", "Cc"))
    text = "".join(_LOOKALIKES.get(c) or (" " if unicodedata.category(c) == "Zs" else c) for c in text)
    text = "".join(_LETTERS.get(c, c) for c in text)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = _DISALLOWED.sub("", text)
    text = "\n".join(line.rstrip(" ") for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()
