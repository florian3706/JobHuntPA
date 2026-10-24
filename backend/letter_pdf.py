"""The cover letter as PDF: LibreOffice's conversion of the filled .docx/.odt when LibreOffice is installed, else drawn
from the letter model (backend/letter_files.py) with reportlab. ``pdf_backend()`` says which ("libreoffice" |
"reportlab"); the Documents tab and the cover letter window show it (``GET /api/documents/letter-template``).

**LibreOffice** (``to_pdf_libreoffice``): found once per process (``soffice`` or ``libreoffice`` on the PATH, the macOS
app bundle, the Windows Program Files folders). The conversion is the only place a letter is written to disk: into a
new folder of its own inside the data folder (``.letter-pdf-*``, readable by this user only), with a LibreOffice
profile of its own, removed again when the conversion ends however it ends; a folder a crash left is removed by the
next conversion (and by the reset tool, ``remove_temp_folders``). ``PdfError`` says only how it failed (an exit
status, a time-out), never a name, a path or any text.

**reportlab** (``render``): a template asks for fonts by name (Garamond, Calibri...). ``catalogue()`` scans this computer's font folders
once (the TrueType ``name`` table, read here without any library: reportlab embeds only TrueType outlines, so
OpenType CFF fonts and collections are left out) and ``pick()`` finds the family, or a stand-in: Liberation Serif,
Sans or Mono when the computer has them (the same metrics as Times New Roman, Arial and Courier New), else the PDF
core fonts (Times, Helvetica, Courier), which need no file. ``render`` reports every substitution, so the window can
say "the template asks for Garamond, which this computer does not have; the PDF uses Liberation Serif". The font
folders are scanned once, in the background from start-up (``warm_up``): a request never waits for the scan, and until
it ends the PDF uses the core fonts.

Nothing here logs: font names come from the user's template, the text is the letter (backend/cover_letters.py logs a
failed conversion by its ``PdfError`` alone).
"""
from __future__ import annotations

import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
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
_names: dict[str, str] = {}  # family (lower-case) -> the name its files give (for the font list)
_lock = threading.Lock()
_warming: Optional[Future] = None
_warm_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jobhunt-fonts")
# Fonts installed while the app runs: the folders' modification times are compared, at most every CHECK_EVERY_S, with
# those at the last scan, and a change starts a scan in the background (requests keep the old list meanwhile).
_signature: Optional[tuple] = None   # the folders at the last scan
_checked_at = 0.0
_refreshing: Optional[Future] = None
CHECK_EVERY_S = 30.0


def catalogue(refresh: bool = False, wait: bool = True) -> dict[str, dict[tuple[bool, bool], Path]]:
    """Every TrueType family on this computer: {family (lower-case): {(bold, italic): file}}. Scanned once (a Raspberry
    Pi with many fonts takes seconds) and again in the background when a font folder changes. ``wait=False``
    (requests): before the scan has ended, starts it in the background if it isn't running and answers {} (the core
    fonts) instead of waiting."""
    global _catalogue, _signature
    if _catalogue is not None and not refresh:
        if not wait:
            _refresh_if_changed()
        return _catalogue
    if not wait and not refresh:
        warm_up()
        return {}
    with _lock:
        if _catalogue is not None and not refresh:
            return _catalogue
        signature = _dir_signature()
        _catalogue = _scan()
        _signature = signature
        return _catalogue


def _dir_signature() -> tuple:
    """(folder, modification time) for every font folder and sub-folder: what changes when fonts are added."""
    found = []
    for base in font_dirs():
        if not base.is_dir():
            continue
        for root, _dirs, _files in os.walk(base):
            try:
                found.append((root, os.stat(root).st_mtime_ns))
            except OSError:
                continue
    return tuple(found)


def _refresh_if_changed() -> None:
    """Starts a background rescan when a font folder changed since the last scan (checked at most every
    CHECK_EVERY_S, so a request costs a stat per folder at most that often)."""
    from backend import threads

    global _checked_at, _refreshing
    now = time.monotonic()
    if now - _checked_at < CHECK_EVERY_S:
        return
    with _warm_lock:
        if now - _checked_at < CHECK_EVERY_S or (_refreshing is not None and not _refreshing.done()):
            return
        _checked_at = now
        if _dir_signature() != _signature:
            _refreshing = threads.submit(_pool, catalogue, True)


def warm_up() -> None:
    """Scans the font folders in the background (once; at start-up, backend/app.py)."""
    from backend import threads

    global _warming
    with _warm_lock:
        if _warming is None and _catalogue is None:
            _warming = threads.submit(_pool, catalogue)


def _scan() -> dict[str, dict[tuple[bool, bool], Path]]:
    """Every TrueType family in the font folders (see ``catalogue``)."""
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
                _names.setdefault(family.lower(), family)
    return found


NOT_FOR_LETTERS = ("emoji", "symbol", "math", "braille", "dingbat", "music", "icons", "awesome")


def families() -> list[str]:
    """The families this computer has that a letter could be set in: those with a regular face, sorted, less the
    symbol and emoji fonts. Empty before the scan has ended (the window then offers the default only)."""
    cat = catalogue(wait=False)
    out = [_names.get(low, low) for low, faces in cat.items()
           if (False, False) in faces and not any(h in low for h in NOT_FOR_LETTERS)]
    return sorted(out, key=str.lower)


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
    cat = catalogue(wait=False)
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
    cat = catalogue(wait=False)
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
        self.used: dict[str, str] = {}
        self.substitutions: list[dict] = []

    def family(self, family: str, record: bool = True) -> tuple[str, str, str, str]:
        """``record``: a stand-in counts as a substitution (not for an empty paragraph's font: nothing shows)."""
        if family not in self.names:
            used, files = pick(family)  # (once: the scan may end during the render)
            self.names[family] = _register(used, files)
            self.used[family] = used
        used = self.used[family]
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


# --------------------------------------------------------------------------
# LibreOffice
# --------------------------------------------------------------------------

PDF_TIMEOUT_S = 120
TEMP_PREFIX = ".letter-pdf-"
STALE_TEMP_S = 15 * 60  # older than any conversion can run: left by a crash


class PdfError(Exception):
    """LibreOffice didn't make the PDF. The message says how (an exit status, a time-out), never a name or text."""


_backend: Optional[tuple[str, Optional[str]]] = None  # ("libreoffice", path) | ("reportlab", None), found once


def _candidates() -> list[str]:
    found = [shutil.which(name) for name in ("soffice", "libreoffice")]
    found.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    for env, default in (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        found.append(os.path.join(os.environ.get(env) or default, "LibreOffice", "program", "soffice.exe"))
    return [f for f in found if f]


def _find() -> tuple[str, Optional[str]]:
    global _backend
    if _backend is None:
        exe = next((c for c in _candidates() if os.path.isfile(c) and os.access(c, os.X_OK)), None)
        _backend = ("libreoffice", exe) if exe else ("reportlab", None)
    return _backend


def reset_pdf_backend() -> None:
    """Look for LibreOffice again on the next PDF (tests; an install made while the app runs needs a restart)."""
    global _backend
    _backend = None


def pdf_backend() -> str:
    """"libreoffice" when LibreOffice is installed (it converts the filled file), else "reportlab" (``render``)."""
    return _find()[0]


def _temp_parent() -> Path:
    from backend.db import DATA_DIR

    return Path(DATA_DIR)


def remove_temp_folders(older_than_s: float = 0) -> int:
    """Removes the conversion folders (``.letter-pdf-*`` in the data folder) older than ``older_than_s`` seconds: the
    ones a crash left. Returns how many."""
    parent, removed = _temp_parent(), 0
    if not parent.is_dir():
        return 0
    now = time.time()
    for path in parent.glob(TEMP_PREFIX + "*"):
        try:
            if path.is_dir() and not path.is_symlink() and now - path.stat().st_mtime >= older_than_s:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def to_pdf_libreoffice(data: bytes, suffix: str) -> bytes:
    """``data`` (a .docx or .odt) as a PDF, converted by LibreOffice in a folder of its own that is removed after."""
    exe = _find()[1]
    if not exe:
        raise PdfError("LibreOffice not found")
    remove_temp_folders(STALE_TEMP_S)
    parent = _temp_parent()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=parent)  # (0700)
    try:
        source = Path(tmp) / f"letter{suffix}"
        source.write_bytes(data)
        command = [exe, "--headless", "--norestore", "--nologo",
                   f"-env:UserInstallation={(Path(tmp) / 'profile').as_uri()}",
                   "--convert-to", "pdf", "--outdir", tmp, str(source)]
        try:
            done = subprocess.run(command, capture_output=True, timeout=PDF_TIMEOUT_S, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise PdfError(f"timed out after {PDF_TIMEOUT_S} s") from None
        except OSError as exc:
            raise PdfError(f"could not start ({exc.__class__.__name__})") from None
        result = Path(tmp) / "letter.pdf"
        if done.returncode != 0:
            raise PdfError(f"exit status {done.returncode}")
        if not result.is_file():
            raise PdfError("no PDF written")
        return result.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
