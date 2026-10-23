"""The cover letter as PDF, drawn from the letter model (backend/letter_files.py) with reportlab.

Fonts: a template asks for fonts by name (Garamond, Calibri...). ``catalogue()`` scans this computer's font folders
once (the TrueType ``name`` table, read here without any library: reportlab embeds only TrueType outlines, so
OpenType CFF fonts and collections are left out) and ``pick()`` finds the family, or a stand-in: Liberation Serif,
Sans or Mono when the computer has them (the same metrics as Times New Roman, Arial and Courier New), else the PDF
core fonts (Times, Helvetica, Courier), which need no file. ``render`` reports every substitution, so the window can
say "the template asks for Garamond, which this computer does not have; the PDF uses Liberation Serif".

Nothing here logs: font names come from the user's template, the text is the letter.
"""
from __future__ import annotations

import io
import os
import re
import struct
import sys
import threading
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

from backend.letter_files import Letter, Para, Picture

# Serif and monospace families people ask for; everything else is taken as sans-serif.
SERIF_HINTS = ("garamond", "georgia", "times", "cambria", "antiqua", "palatino", "baskerville", "century", "constantia",
               "caladea", "bookman", "minion", "merriweather", "libre", "serif", "charter", "didot", "bodoni", "roman",
               "tinos", "liberation serif", "dejavu serif", "noto serif", "source serif", "pt serif", "lora",
               "crimson", "playfair", "cormorant", "eb ", "spectral")
MONO_HINTS = ("mono", "courier", "consolas", "menlo", "monaco", "code")
# What stands in for a missing family: (families to look for on this computer, then the core font family).
FALLBACKS = {"serif": (("Liberation Serif", "Tinos", "DejaVu Serif", "FreeSerif"), "Times-Roman"),
             "sans": (("Liberation Sans", "Arimo", "DejaVu Sans", "FreeSans"), "Helvetica"),
             "mono": (("Liberation Mono", "Cousine", "DejaVu Sans Mono", "FreeMono"), "Courier")}
CORE = {"Times-Roman": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
        "Helvetica": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"),
        "Courier": ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique")}
CORE_ALIASES = {"times new roman": "Times-Roman", "times": "Times-Roman", "arial": "Helvetica", "helvetica": "Helvetica",
                "courier new": "Courier", "courier": "Courier"}
# The plain layout's font: the first of these the computer has, else Helvetica (core).
PLAIN_FONTS = ("Liberation Sans", "Arial", "Arimo", "DejaVu Sans", "Helvetica")


def font_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
                Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Microsoft" / "Windows" / "Fonts"]
    elif sys.platform == "darwin":
        dirs = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), home / "Library" / "Fonts"]
    else:
        dirs = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".fonts", home / ".local/share/fonts"]
    extra = os.environ.get("JOBHUNT_FONT_DIRS")  # tests, and a computer that keeps fonts elsewhere
    if extra:
        dirs = [Path(p) for p in extra.split(os.pathsep) if p] + dirs
    return dirs


def read_names(path: Path) -> Optional[tuple[str, str]]:
    """(family, style) from a TrueType file's ``name`` table; None for anything reportlab can't embed."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12:
                return None
            tag, count = head[:4], struct.unpack(">H", head[4:6])[0]
            if tag not in (b"\x00\x01\x00\x00", b"true"):  # 'OTTO' (CFF) and 'ttcf' (collections): not embeddable
                return None
            tables = {}
            for _ in range(min(count, 64)):
                rec = f.read(16)
                if len(rec) < 16:
                    return None
                tables[rec[:4]] = struct.unpack(">II", rec[8:16])
            if b"name" not in tables or b"glyf" not in tables:
                return None
            offset, length = tables[b"name"]
            f.seek(offset)
            data = f.read(min(length, 65536))
        fmt, n, strings = struct.unpack(">HHH", data[:6])
        names: dict[tuple[int, int], str] = {}
        for i in range(min(n, 2000)):
            rec = data[6 + i * 12: 18 + i * 12]
            if len(rec) < 12:
                break
            platform, encoding, language, name_id, slen, soff = struct.unpack(">HHHHHH", rec)
            if name_id not in (1, 2, 16, 17):
                continue
            raw = data[strings + soff: strings + soff + slen]
            if platform == 3 and language == 0x409:
                names[(name_id, 0)] = raw.decode("utf-16-be", errors="replace")
            elif platform == 1 and language == 0 and (name_id, 0) not in names:
                names[(name_id, 1)] = raw.decode("mac-roman", errors="replace")

        def get(name_id: int) -> str:
            return (names.get((name_id, 0)) or names.get((name_id, 1)) or "").strip()
        family, style = get(16) or get(1), get(17) or get(2)
        return (family, style or "Regular") if family else None
    except (OSError, struct.error):
        return None


_catalogue: Optional[dict[str, dict[tuple[bool, bool], Path]]] = None
_lock = threading.Lock()


def catalogue(refresh: bool = False) -> dict[str, dict[tuple[bool, bool], Path]]:
    """Every TrueType family on this computer: {family (lower-case): {(bold, italic): file}}. Scanned once."""
    global _catalogue
    with _lock:
        if _catalogue is not None and not refresh:
            return _catalogue
        found: dict[str, dict[tuple[bool, bool], Path]] = {}
        for base in font_dirs():
            if not base.is_dir():
                continue
            for root, _dirs, files in os.walk(base):
                for name in files:
                    if not name.lower().endswith((".ttf", ".otf")):
                        continue
                    path = Path(root) / name
                    names = read_names(path)
                    if names is None:
                        continue
                    family, style = names
                    style_l = style.lower()
                    key = ("bold" in style_l, "italic" in style_l or "oblique" in style_l)
                    found.setdefault(family.lower(), {}).setdefault(key, path)
        _catalogue = found
        return found


def classify(family: str) -> str:
    low = family.lower()
    if any(h in low for h in MONO_HINTS):
        return "mono"
    if any(h in low for h in SERIF_HINTS):
        return "serif"
    return "sans"


def pick(family: str) -> tuple[str, Optional[dict[tuple[bool, bool], Path]]]:
    """The family to draw ``family`` with: (name used, its files or None for a core font). The name used is the
    family itself when the computer has it."""
    cat = catalogue()
    files = cat.get(family.lower())
    if files:
        return family, files
    kind = classify(family)
    for stand_in in FALLBACKS[kind][0]:
        if cat.get(stand_in.lower()):
            return stand_in, cat[stand_in.lower()]
    return CORE_ALIASES.get(family.lower(), FALLBACKS[kind][1]), None


def plain_font() -> str:
    """The plain layout's font: a sans this computer has, so its PDF and its .docx/.odt agree."""
    cat = catalogue()
    return next((f for f in PLAIN_FONTS if cat.get(f.lower())), "Helvetica")


def font_report(families: list[str]) -> list[dict]:
    """For each family a template asks for: whether the computer has it and what the PDF uses instead."""
    out = []
    for family in families:
        used, files = pick(family)
        out.append({"asked": family, "used": used, "found": used == family})
    return out


# --------------------------------------------------------------------------
# reportlab
# --------------------------------------------------------------------------

_registered: dict[str, tuple[str, str, str, str]] = {}  # family used -> (normal, bold, italic, bold-italic) names


def _register(used: str, files: Optional[dict[tuple[bool, bool], Path]]) -> tuple[str, str, str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if used in CORE and files is None:
        return CORE[used]
    with _lock:
        if used in _registered:
            return _registered[used]
        base = re.sub(r"[^A-Za-z0-9]+", "", used) or "Font"
        regular = files.get((False, False)) or next(iter(files.values()))
        names = []
        for key, suffix in (((False, False), ""), ((True, False), "-Bold"), ((False, True), "-Italic"), ((True, True), "-BoldItalic")):
            path = files.get(key)
            if path is None:
                names.append(None)
                continue
            try:
                pdfmetrics.registerFont(TTFont(base + suffix, str(path)))
                names.append(base + suffix)
            except Exception:
                names.append(None)
        if names[0] is None:  # no regular file: the first file that loads stands for every style
            try:
                pdfmetrics.registerFont(TTFont(base, str(regular)))
                names[0] = base
            except Exception:
                return CORE[FALLBACKS[classify(used)][1]]
        normal, bold, italic, both = names[0], names[1] or names[0], names[2] or names[0], names[3] or names[1] or names[2] or names[0]
        pdfmetrics.registerFontFamily(normal, normal=normal, bold=bold, italic=italic, boldItalic=both)
        _registered[used] = (normal, bold, italic, both)
        return _registered[used]


class _Fonts:
    """Resolves families once per render and remembers the substitutions."""

    def __init__(self) -> None:
        self.names: dict[str, tuple[str, str, str, str]] = {}
        self.substitutions: list[dict] = []

    def family(self, family: str, record: bool = True) -> tuple[str, str, str, str]:
        """``record``: a stand-in counts as a substitution (not for an empty paragraph's font: nothing shows)."""
        if family not in self.names:
            self.names[family] = _register(*pick(family))
        used = pick(family)[0]
        if record and used != family and not any(s["asked"] == family for s in self.substitutions):
            self.substitutions.append({"asked": family, "used": used})
        return self.names[family]

    def name(self, family: str, bold: bool, italic: bool, record: bool = True) -> str:
        normal, b, i, both = self.family(family, record)
        return both if bold and italic else b if bold else i if italic else normal


def _markup(para: Para, fonts: _Fonts) -> str:
    parts = []
    for run in para.runs:
        if run.picture is not None or not run.text:
            continue
        text = escape(run.text).replace("\n", "<br/>").replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;")
        name = fonts.name(run.font, run.bold, run.italic)
        text = f'<font face="{name}" size="{run.size:g}">{text}</font>'
        if run.underline:
            text = f"<u>{text}</u>"
        parts.append(text)
    return "".join(parts)


def _style(para: Para, fonts: _Fonts, index: int):
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle

    text_runs = [r for r in para.runs if r.text.strip()]
    size = max((r.size for r in text_runs), default=para.size)
    font = text_runs[0].font if text_runs else para.font
    return ParagraphStyle(name=f"p{index}", fontName=fonts.name(font, False, False, record=bool(text_runs)), fontSize=size,
                          leading=size * para.line_spacing, spaceBefore=para.space_before, spaceAfter=para.space_after,
                          alignment={"center": TA_CENTER, "right": TA_RIGHT, "justify": TA_JUSTIFY}.get(para.align, TA_LEFT))


def _image(picture: Picture, max_width: float):
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image

    try:
        reader = ImageReader(io.BytesIO(picture.data))
        reader.getSize()
    except Exception:  # a format reportlab can't draw (EMF, SVG...): left out
        return None
    scale = min(1.0, max_width / picture.width) if picture.width > max_width else 1.0
    return Image(io.BytesIO(picture.data), picture.width * scale, picture.height * scale)


def _flowables(paras: list[Para], fonts: _Fonts, width: float, prefix: str) -> list:
    from reportlab.platypus import Paragraph, Spacer

    out = []
    for i, para in enumerate(paras):
        style = _style(para, fonts, f"{prefix}{i}")
        for picture in para.pictures:
            img = _image(picture, width)
            if img is not None:
                img.hAlign = {"center": "CENTER", "right": "RIGHT"}.get(para.align, "LEFT")
                out.append(img)
        if para.text.strip():
            out.append(Paragraph(_markup(para, fonts), style))
        elif not para.pictures:
            out.append(Spacer(1, style.leading + para.space_before + para.space_after))
    return out


def _height(flowables: list, width: float) -> float:
    total = 0.0
    for f in flowables:
        _w, h = f.wrap(width, 10000)
        total += h + getattr(f, "getSpaceBefore", lambda: 0)() + getattr(f, "getSpaceAfter", lambda: 0)()
    return total


def _draw_block(canvas, flowables: list, x: float, top: float, width: float) -> None:
    """Draws header or footer flowables down from ``top``."""
    y = top
    for f in flowables:
        w, h = f.wrap(width, 10000)
        y -= getattr(f, "getSpaceBefore", lambda: 0)()
        y -= h
        align = getattr(f, "hAlign", "LEFT")
        dx = (width - w) / 2 if align == "CENTER" else width - w if align == "RIGHT" else 0
        f.drawOn(canvas, x + dx, y)
        y -= getattr(f, "getSpaceAfter", lambda: 0)()


def render(letter: Letter) -> tuple[bytes, list[dict]]:
    """(PDF bytes, font substitutions [{asked, used}])."""
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

    fonts = _Fonts()
    width = letter.page_width - letter.margin_left - letter.margin_right
    header = _flowables(letter.header, fonts, width, "h")
    footer = _flowables(letter.footer, fonts, width, "f")
    body = _flowables(letter.body, fonts, width, "b")
    header_h = _height(header, width) if header else 0.0
    footer_h = _height(footer, width) if footer else 0.0
    top = max(letter.margin_top, letter.header_distance + header_h + (6 if header else 0))
    bottom = max(letter.margin_bottom, letter.footer_distance + footer_h + (6 if footer else 0))

    def on_page(canvas, _doc):
        canvas.saveState()
        if header:
            _draw_block(canvas, header, letter.margin_left, letter.page_height - letter.header_distance, width)
        if footer:
            _draw_block(canvas, footer, letter.margin_left, letter.footer_distance + footer_h, width)
        canvas.restoreState()

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=(letter.page_width, letter.page_height), leftMargin=letter.margin_left,
                          rightMargin=letter.margin_right, topMargin=top, bottomMargin=bottom, title="Cover letter",
                          author="", subject="", creator="JobHuntPA")
    frame = Frame(letter.margin_left, bottom, width, letter.page_height - top - bottom, id="body",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="letter", frames=[frame], onPage=on_page)])
    doc.build(body or [_flowables([Para()], fonts, width, "e")[0]])
    return buf.getvalue(), fonts.substitutions
