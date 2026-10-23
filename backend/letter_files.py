"""Cover letters as files: letter templates, the plain built-in layout, and the letter model the PDF is drawn from.

A **letter template** is a .docx or .odt document of the user's own (a letterhead: header, footer, signature image,
fonts) with a ``{{letter}}`` marker in a paragraph of its own. Filling it replaces that paragraph with one paragraph
per letter paragraph, each a copy of the marker paragraph's paragraph style and of the run formatting the marker
had, so the font, size, alignment and spacing carry over; everything else in the file is left as it is. The
optional markers ``{{date}}``, ``{{company}}``, ``{{job_title}}`` and ``{{name}}`` are filled wherever they appear
(body, tables, headers, footers). The template's bytes are what matters: they are the uploaded file, sealed like
every upload (backend/docs.py), and never sent to the AI.

Without a template, the plain layout builds a .docx and an .odt of the same shape (A4, one inch margins, one sans
font the computer has).

The PDF is not a third layout: ``letter_model`` reads the *filled* .docx or .odt back into a small model
(``Letter``: page, margins, header, footer and body paragraphs with their runs and pictures) and
backend/letter_pdf.py draws that model, so the PDF shows what the editable file holds.

Nothing here logs: a template, a letter and a file name are personal.
"""
from __future__ import annotations

import copy
import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Optional

LETTER_MARKER = "letter"
MARKER_NAMES = ("letter", "date", "company", "job_title", "name")
MARKER_RE = re.compile(r"\{\{\s*(" + "|".join(MARKER_NAMES) + r")\s*\}\}")
TEMPLATE_SUFFIXES = (".docx", ".odt")

A4 = (595.28, 841.89)          # points
US_LETTER = (612.0, 792.0)
INCH = 72.0
DEFAULT_SIZE = 11.0
DEFAULT_LINE_SPACING = 1.15    # Word's "single" is about 1.17 lines of the font size; LibreOffice's about 1.15


class TemplateError(ValueError):
    """The template can't be used; the message is for the user (it names no content of the file)."""


# --------------------------------------------------------------------------
# The letter model
# --------------------------------------------------------------------------

@dataclass
class Picture:
    data: bytes
    width: float    # pt
    height: float   # pt


@dataclass
class Run:
    text: str = ""
    font: str = "Helvetica"
    size: float = DEFAULT_SIZE
    bold: bool = False
    italic: bool = False
    underline: bool = False
    picture: Optional[Picture] = None   # a picture instead of text


@dataclass
class Para:
    runs: list[Run] = field(default_factory=list)
    font: str = "Helvetica"             # the paragraph's own font and size: what an empty paragraph takes up
    size: float = DEFAULT_SIZE
    align: str = "left"                 # left | center | right | justify
    space_before: float = 0.0           # pt
    space_after: float = 0.0
    line_spacing: float = DEFAULT_LINE_SPACING  # a multiple of the font size

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    @property
    def pictures(self) -> list[Picture]:
        return [r.picture for r in self.runs if r.picture is not None]


@dataclass
class Letter:
    page_width: float = A4[0]
    page_height: float = A4[1]
    margin_top: float = INCH
    margin_right: float = INCH
    margin_bottom: float = INCH
    margin_left: float = INCH
    header_distance: float = INCH / 2   # top of the page to the header, bottom of the page to the footer
    footer_distance: float = INCH / 2
    header: list[Para] = field(default_factory=list)
    footer: list[Para] = field(default_factory=list)
    body: list[Para] = field(default_factory=list)

    def families(self) -> list[str]:
        """Every font family the letter uses, first use first."""
        seen: list[str] = []
        for p in self.header + self.body + self.footer:
            for name in [r.font for r in p.runs if r.text.strip()]:
                if name and name not in seen:
                    seen.append(name)
        return seen


# --------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------

def split_paragraphs(text: str) -> list[str]:
    """The letter's paragraphs: blank lines separate them; single line breaks stay (an address block)."""
    return [p.strip("\n").strip() for p in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip()) if p.strip()]


def sign_off_name(text: str) -> str:
    """The name the letter signs off with (its last short line), for a ``{{name}}`` marker; "" when unsure."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    last = lines[-1]
    if len(last) <= 40 and 1 <= len(last.split()) <= 5 and not last.endswith((".", ",", ":", "!", "?")):
        return last
    return ""


def long_date(day: Optional[date] = None) -> str:
    day = day or date.today()
    return f"{day.day} {day.strftime('%B %Y')}"


def marker_values(letter_text: str, company: str = "", job_title: str = "", when: Optional[date] = None) -> dict:
    return {"date": long_date(when), "company": company or "", "job_title": job_title or "",
            "name": sign_off_name(letter_text)}


def _replace_in_texts(texts: list[str], repl: Callable[[re.Match], Optional[str]]) -> tuple[list[str], bool]:
    """Replaces the markers in a paragraph given as a list of its runs' texts (a marker may be split over runs:
    Word often does that). The replacement goes into the run where the marker starts, which keeps that run's
    formatting; the rest of the marker is cut out of the runs after it. ``repl`` answers None to leave a marker."""
    texts = list(texts)
    matches = list(MARKER_RE.finditer("".join(texts)))
    changed = False
    for m in reversed(matches):  # back to front: the earlier positions stay valid
        replacement = repl(m)
        if replacement is None:
            continue
        start, end = m.span()
        pos = 0
        for i, t in enumerate(texts):
            run_start, run_end = pos, pos + len(t)
            pos = run_end
            if run_end <= start or run_start >= end:
                continue
            a, b = max(run_start, start) - run_start, min(run_end, end) - run_start
            texts[i] = t[:a] + (replacement if run_start <= start < run_end else "") + t[b:]
        changed = True
    return texts, changed


def _values_repl(values: dict) -> Callable[[re.Match], Optional[str]]:
    def repl(m: re.Match) -> Optional[str]:
        name = m.group(1)
        return None if name == LETTER_MARKER else str(values.get(name, "") or "")
    return repl


def _is_letter_marker(m: re.Match) -> bool:
    return m.group(1) == LETTER_MARKER


def parse_length(value: Optional[str], default: float = 0.0) -> float:
    """"12pt", "2.54cm", "1in", "10mm", "720twip" -> points. A bare number is points."""
    if value is None:
        return default
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(pt|cm|mm|in|inch|pc|px|twip|emu)?\s*", str(value))
    if not m:
        return default
    n, unit = float(m.group(1)), (m.group(2) or "pt")
    return n * {"pt": 1.0, "cm": INCH / 2.54, "mm": INCH / 25.4, "in": INCH, "inch": INCH, "pc": 12.0, "px": 0.75,
                "twip": 1 / 20, "emu": 1 / 12700}[unit]


def _page_size_name(width: float, height: float) -> tuple[float, float]:
    """Snaps a page size within a few points of A4 or US Letter to the exact size (files round)."""
    for size in (A4, US_LETTER):
        if abs(width - size[0]) < 4 and abs(height - size[1]) < 4:
            return size
    return width, height


# --------------------------------------------------------------------------
# .docx (python-docx)
# --------------------------------------------------------------------------

def _docx_blocks(container) -> Iterable:
    """Paragraphs of a body, cell, header or footer in order, tables flattened (each cell once)."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    items = container.iter_inner_content() if hasattr(container, "iter_inner_content") else container.paragraphs
    for item in items:
        if isinstance(item, Paragraph):
            yield item
        elif isinstance(item, Table):
            seen = set()
            for row in item.rows:
                for cell in row.cells:
                    if id(cell._tc) in seen:
                        continue
                    seen.add(id(cell._tc))
                    yield from _docx_blocks(cell)


def _docx_headers_footers(doc) -> list:
    parts = []
    for section in doc.sections:
        for name in ("header", "first_page_header", "even_page_header", "footer", "first_page_footer", "even_page_footer"):
            part = getattr(section, name, None)
            if part is not None and not part.is_linked_to_previous:
                parts.append((name, part))
    return parts


def _docx_replace_markers(paragraph, values: dict) -> None:
    runs = paragraph.runs
    if not runs:
        return
    texts, changed = _replace_in_texts([r.text for r in runs], _values_repl(values))
    if changed:
        for run, text in zip(runs, texts):
            if run.text != text:
                run.text = text


def _docx_marker_run(paragraph):
    """The run the {{letter}} marker starts in (its formatting is what the letter takes)."""
    pos = 0
    full = paragraph.text
    m = next((m for m in MARKER_RE.finditer(full) if _is_letter_marker(m)), None)
    start = m.start() if m else 0
    for run in paragraph.runs:
        if pos <= start < pos + len(run.text):
            return run
        pos += len(run.text)
    return paragraph.runs[0] if paragraph.runs else None


def _docx_pour(paragraph, paragraphs: list[str]) -> None:
    """Replaces the marker paragraph with one paragraph per letter paragraph, same style and run formatting."""
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    run = _docx_marker_run(paragraph)
    rPr = copy.deepcopy(run._r.rPr) if run is not None and run._r.rPr is not None else None
    anchor = paragraph._p
    for text in paragraphs:
        p_el = copy.deepcopy(anchor)
        for child in list(p_el):  # keep the paragraph properties, drop the marker's runs and everything else
            if child.tag != qn("w:pPr"):
                p_el.remove(child)
        new = Paragraph(p_el, paragraph._parent)
        new_run = new.add_run(text)  # line breaks and tabs become w:br / w:tab
        if rPr is not None:
            new_run._r.insert(0, copy.deepcopy(rPr))
        anchor.addprevious(p_el)
    anchor.getparent().remove(anchor)


def fill_docx(data: bytes, letter_text: str, values: dict) -> bytes:
    import docx

    doc = docx.Document(io.BytesIO(data))
    body = list(_docx_blocks(doc))
    targets = [p for p in body if any(_is_letter_marker(m) for m in MARKER_RE.finditer(p.text))]
    if not targets:
        raise TemplateError("The template has no {{letter}} marker. Put {{letter}} in a paragraph of its own where "
                            "the letter should go.")
    for p in body + [p for _, part in _docx_headers_footers(doc) for p in _docx_blocks(part)]:
        _docx_replace_markers(p, values)
    _docx_pour(targets[0], split_paragraphs(letter_text))
    for extra in targets[1:]:  # a second {{letter}} marker: emptied rather than filled twice
        _docx_pour(extra, [""])
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


class _DocxStyles:
    """Resolves fonts, sizes and spacing through the style chain, the theme fonts and the document defaults."""

    def __init__(self, doc):
        from docx.oxml.ns import qn

        self.qn = qn
        self.doc = doc
        styles_el = doc.styles.element
        self.default_rpr = next(iter(styles_el.xpath("w:docDefaults/w:rPrDefault/w:rPr")), None)
        self.default_ppr = next(iter(styles_el.xpath("w:docDefaults/w:pPrDefault/w:pPr")), None)
        self.theme = self._theme_fonts()

    def _theme_fonts(self) -> dict:
        found = {}
        try:
            for part in self.doc.part.package.iter_parts():
                if "theme" in str(part.partname) and str(part.partname).endswith(".xml"):
                    xml = part.blob.decode("utf-8", errors="replace")
                    for kind in ("major", "minor"):
                        m = re.search(rf"<a:{kind}Font>.*?<a:latin[^>]*typeface=\"([^\"]*)\"", xml, re.S)
                        if m and m.group(1):
                            found[kind] = m.group(1)
                    break
        except Exception:  # a theme is a nicety
            pass
        return found

    # -- the chains --

    def para_styles(self, paragraph) -> list:
        chain, style = [], paragraph.style
        while style is not None and style not in chain:
            chain.append(style)
            style = style.base_style
        return chain

    def run_styles(self, run) -> list:
        chain, style = [], getattr(run, "style", None)
        while style is not None and style not in chain:
            chain.append(style)
            style = style.base_style
        return chain

    # -- fonts --

    def _rfonts_name(self, rpr) -> Optional[str]:
        if rpr is None:
            return None
        rfonts = rpr.find(self.qn("w:rFonts"))
        if rfonts is None:
            return None
        name = rfonts.get(self.qn("w:ascii")) or rfonts.get(self.qn("w:hAnsi"))
        if name:
            return name
        theme = rfonts.get(self.qn("w:asciiTheme")) or rfonts.get(self.qn("w:hAnsiTheme"))
        if theme:
            return self.theme.get("major" if "major" in theme else "minor")
        return None

    def _size(self, rpr) -> Optional[float]:
        if rpr is None:
            return None
        sz = rpr.find(self.qn("w:sz"))
        try:
            return int(sz.get(self.qn("w:val"))) / 2 if sz is not None else None
        except (TypeError, ValueError):
            return None

    def _flag(self, rpr, tag: str) -> Optional[bool]:
        if rpr is None:
            return None
        el = rpr.find(self.qn(tag))
        if el is None:
            return None
        val = el.get(self.qn("w:val"))
        return val not in ("0", "false", "off")

    def _rprs(self, run, paragraph) -> list:
        """Run properties, most specific first: the run, its character style, the paragraph style chain, the
        document defaults."""
        rprs = [run._r.rPr] if run is not None else []
        for style in (self.run_styles(run) if run is not None else []) + self.para_styles(paragraph):
            rprs.append(style.element.find(self.qn("w:rPr")))
        rprs.append(self.default_rpr)
        return rprs

    def font(self, run, paragraph) -> tuple[str, float, bool, bool, bool]:
        rprs = self._rprs(run, paragraph)
        name = next((n for n in (self._rfonts_name(r) for r in rprs) if n), None) or self.theme.get("minor") or "Calibri"
        size = next((s for s in (self._size(r) for r in rprs) if s), None) or DEFAULT_SIZE
        bold = next((f for f in (self._flag(r, "w:b") for r in rprs) if f is not None), False)
        italic = next((f for f in (self._flag(r, "w:i") for r in rprs) if f is not None), False)
        underline = next((f for f in (self._flag(r, "w:u") for r in rprs) if f is not None), False)
        return name, size, bold, italic, underline

    # -- paragraph layout --

    def _pprs(self, paragraph) -> list:
        pprs = [paragraph._p.pPr]
        for style in self.para_styles(paragraph):
            pprs.append(style.element.find(self.qn("w:pPr")))
        pprs.append(self.default_ppr)
        return [p for p in pprs if p is not None]

    def layout(self, paragraph, size: float) -> tuple[str, float, float, float]:
        align, before, after, spacing = None, None, None, None
        for ppr in self._pprs(paragraph):
            jc = ppr.find(self.qn("w:jc"))
            if align is None and jc is not None:
                align = {"center": "center", "right": "right", "end": "right", "both": "justify",
                         "distribute": "justify"}.get(jc.get(self.qn("w:val")), "left")
            sp = ppr.find(self.qn("w:spacing"))
            if sp is not None:
                if before is None and sp.get(self.qn("w:before")) is not None:
                    before = parse_length(sp.get(self.qn("w:before")) + "twip")
                if after is None and sp.get(self.qn("w:after")) is not None:
                    after = parse_length(sp.get(self.qn("w:after")) + "twip")
                if spacing is None and sp.get(self.qn("w:line")) is not None:
                    line, rule = sp.get(self.qn("w:line")), sp.get(self.qn("w:lineRule")) or "auto"
                    try:
                        spacing = int(line) / 240 if rule == "auto" else max(int(line) / 20 / size, 1.0)
                    except ValueError:
                        spacing = None
        return align or "left", before or 0.0, after or 0.0, spacing or DEFAULT_LINE_SPACING


def _docx_pictures(run, part) -> list[Picture]:
    pictures = []
    try:
        for drawing in run._r.xpath(".//w:drawing"):
            extents = drawing.xpath(".//wp:extent")
            blips = drawing.xpath(".//a:blip")
            if not blips or not extents:
                continue
            rid = blips[0].get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
            if not rid or rid not in part.related_parts:
                continue
            cx, cy = int(extents[0].get("cx")), int(extents[0].get("cy"))
            pictures.append(Picture(part.related_parts[rid].blob, cx / 12700, cy / 12700))
    except Exception:  # a picture it can't read is left out, the text stays
        pass
    return pictures


def _docx_para(paragraph, styles: _DocxStyles, part) -> Para:
    font, size, *_ = styles.font(None, paragraph)
    align, before, after, spacing = styles.layout(paragraph, size)
    para = Para(font=font, size=size, align=align, space_before=before, space_after=after, line_spacing=spacing)
    for run in paragraph.runs:
        name, rsize, bold, italic, underline = styles.font(run, paragraph)
        for picture in _docx_pictures(run, part):
            para.runs.append(Run(font=name, size=rsize, picture=picture))
        if run.text:
            para.runs.append(Run(run.text, name, rsize, bold, italic, underline))
    return para


def docx_letter(data: bytes) -> Letter:
    """The model of a (filled) .docx: its first section's page, header and footer, and every body paragraph."""
    import docx

    doc = docx.Document(io.BytesIO(data))
    styles = _DocxStyles(doc)
    section = doc.sections[0]
    letter = Letter()

    def pt(value, default):
        return value.pt if value is not None else default
    letter.page_width, letter.page_height = _page_size_name(pt(section.page_width, A4[0]), pt(section.page_height, A4[1]))
    letter.margin_top, letter.margin_bottom = pt(section.top_margin, INCH), pt(section.bottom_margin, INCH)
    letter.margin_left, letter.margin_right = pt(section.left_margin, INCH), pt(section.right_margin, INCH)
    letter.header_distance, letter.footer_distance = pt(section.header_distance, INCH / 2), pt(section.footer_distance, INCH / 2)
    letter.body = [_docx_para(p, styles, doc.part) for p in _docx_blocks(doc)]
    for name, part in _docx_headers_footers(doc):
        paras = [_docx_para(p, styles, part.part) for p in _docx_blocks(part)]
        if name == "header" and paras:
            letter.header = paras
        elif name == "footer" and paras:
            letter.footer = paras
        elif name == "first_page_header" and section.different_first_page_header_footer and paras:
            letter.header = paras   # a letter is one page: the first page's header is the one that shows
        elif name == "first_page_footer" and section.different_first_page_header_footer and paras:
            letter.footer = paras
    return letter


def plain_docx(letter_text: str, font: str, size: float = DEFAULT_SIZE) -> bytes:
    """The built-in layout as .docx: A4, one inch margins, ``font`` at ``size`` points, a little space between
    paragraphs."""
    import docx
    from docx.enum.text import WD_LINE_SPACING
    from docx.shared import Mm, Pt

    doc = docx.Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    for side in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(section, side, Mm(25.4))
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = font, Pt(size)
    normal.element.rPr.rFonts.set(docx.oxml.ns.qn("w:hAnsi"), font)
    normal.paragraph_format.space_after = Pt(size * 0.8)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    normal.paragraph_format.line_spacing = DEFAULT_LINE_SPACING
    for text in split_paragraphs(letter_text) or [""]:
        doc.add_paragraph(text)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# --------------------------------------------------------------------------
# .odt (odfpy)
# --------------------------------------------------------------------------

_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_STYLE_NS = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
_FO_NS = "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
_SVG_NS = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
_DRAW_NS = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
_XLINK_NS = "http://www.w3.org/1999/xlink"


def _odt_paragraphs(element) -> Iterable:
    """text:p and text:h elements under ``element`` in order (tables, lists and sections included; not the
    paragraphs inside a frame's text box, which belong to the drawing)."""
    from odf.element import Node

    for child in element.childNodes:
        if child.nodeType != Node.ELEMENT_NODE:
            continue
        if child.qname in ((_TEXT_NS, "p"), (_TEXT_NS, "h")):
            yield child
        elif child.qname[0] != _DRAW_NS:
            yield from _odt_paragraphs(child)


def _odt_text_nodes(element) -> list:
    """The text nodes of a paragraph in order (spans and links descended, frames skipped)."""
    from odf.element import Node

    out = []
    for child in element.childNodes:
        if child.nodeType == Node.TEXT_NODE:
            out.append(child)
        elif child.nodeType == Node.ELEMENT_NODE and child.qname[0] != _DRAW_NS:
            out.extend(_odt_text_nodes(child))
    return out


def _odt_text(element) -> str:
    from odf import teletype

    return teletype.extractText(element)


def _odt_replace_markers(paragraph, values: dict) -> None:
    nodes = _odt_text_nodes(paragraph)
    if not nodes:
        return
    texts, changed = _replace_in_texts([n.data for n in nodes], _values_repl(values))
    if changed:
        for node, text in zip(nodes, texts):
            node.data = text


def _odt_marker_span(paragraph):
    """The text:span the {{letter}} marker starts in (None: the marker is directly in the paragraph)."""
    pos = 0
    m = next((m for m in MARKER_RE.finditer("".join(n.data for n in _odt_text_nodes(paragraph))) if _is_letter_marker(m)), None)
    start = m.start() if m else 0
    for node in _odt_text_nodes(paragraph):
        if pos <= start < pos + len(node.data):
            parent = node.parentNode
            return parent if parent is not None and parent.qname == (_TEXT_NS, "span") else None
        pos += len(node.data)
    return None


def _odt_pour(paragraph, paragraphs: list[str]) -> None:
    from odf import teletype
    from odf.element import Element
    from odf.text import Span

    span = _odt_marker_span(paragraph)
    span_style = span.getAttrNS(_TEXT_NS, "style-name") if span is not None else None
    parent = paragraph.parentNode
    for text in paragraphs:
        new = Element(qname=paragraph.qname)
        for (ns, local), value in paragraph.attributes.items():
            if (ns, local) != (_TEXT_NS, "id"):
                new.setAttrNS(ns, local, value)
        holder = new
        if span_style:
            holder = Span(stylename=span_style)
            new.addElement(holder)
        teletype.addTextToElement(holder, text)  # line breaks, tabs and runs of spaces become their elements
        parent.insertBefore(new, paragraph)
    parent.removeChild(paragraph)


def fill_odt(data: bytes, letter_text: str, values: dict) -> bytes:
    from odf.opendocument import load

    doc = load(io.BytesIO(data))
    body = list(_odt_paragraphs(doc.text))
    targets = [p for p in body if any(_is_letter_marker(m) for m in MARKER_RE.finditer(_odt_text(p)))]
    if not targets:
        raise TemplateError("The template has no {{letter}} marker. Put {{letter}} in a paragraph of its own where "
                            "the letter should go.")
    for p in body + list(_odt_paragraphs(doc.masterstyles)):
        _odt_replace_markers(p, values)
    _odt_pour(targets[0], split_paragraphs(letter_text))
    for extra in targets[1:]:
        _odt_pour(extra, [""])
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


class _OdtStyles:
    def __init__(self, doc):
        from odf import style

        self.doc = doc
        self.styles = {}
        for el in doc.getElementsByType(style.Style):
            self.styles.setdefault((el.getAttrNS(_STYLE_NS, "family"), el.getAttrNS(_STYLE_NS, "name")), el)
        self.defaults = {el.getAttrNS(_STYLE_NS, "family"): el for el in doc.getElementsByType(style.DefaultStyle)}
        self.fonts = {}
        for el in doc.getElementsByType(style.FontFace):
            family = el.getAttrNS(_SVG_NS, "font-family") or el.getAttrNS(_STYLE_NS, "name") or ""
            self.fonts[el.getAttrNS(_STYLE_NS, "name")] = family.strip("'\"")

    def chain(self, family: str, name: Optional[str]) -> list:
        """A style and its parents, then the family's default style."""
        out, seen = [], set()
        while name and name not in seen:
            seen.add(name)
            el = self.styles.get((family, name))
            if el is None:
                break
            out.append(el)
            name = el.getAttrNS(_STYLE_NS, "parent-style-name")
        if family in self.defaults:
            out.append(self.defaults[family])
        return out

    @staticmethod
    def _props(el, local: str):
        from odf.element import Node

        for child in el.childNodes:
            if child.nodeType == Node.ELEMENT_NODE and child.qname == (_STYLE_NS, local):
                return child
        return None

    def _text_attr(self, chain: list, ns: str, local: str) -> Optional[str]:
        for el in chain:
            props = self._props(el, "text-properties")
            if props is not None:
                value = props.getAttrNS(ns, local)
                if value:
                    return value
        return None

    def font(self, chain: list) -> tuple[str, float, bool, bool, bool]:
        name = self._text_attr(chain, _STYLE_NS, "font-name")
        family = self.fonts.get(name) if name else None
        family = family or (self._text_attr(chain, _FO_NS, "font-family") or "").strip("'\"") or "Liberation Serif"
        size_text = self._text_attr(chain, _FO_NS, "font-size")
        size = parse_length(size_text, DEFAULT_SIZE) if size_text and not size_text.endswith("%") else DEFAULT_SIZE
        bold = (self._text_attr(chain, _FO_NS, "font-weight") or "normal") not in ("normal", "")
        italic = (self._text_attr(chain, _FO_NS, "font-style") or "normal") not in ("normal", "")
        underline = (self._text_attr(chain, _STYLE_NS, "text-underline-style") or "none") not in ("none", "")
        return family, size, bold, italic, underline

    def layout(self, chain: list, size: float) -> tuple[str, float, float, float]:
        align, before, after, spacing = None, None, None, None
        for el in chain:
            props = self._props(el, "paragraph-properties")
            if props is None:
                continue
            if align is None and props.getAttrNS(_FO_NS, "text-align"):
                align = {"center": "center", "end": "right", "right": "right", "justify": "justify"}.get(
                    props.getAttrNS(_FO_NS, "text-align"), "left")
            if before is None and props.getAttrNS(_FO_NS, "margin-top"):
                before = parse_length(props.getAttrNS(_FO_NS, "margin-top"))
            if after is None and props.getAttrNS(_FO_NS, "margin-bottom"):
                after = parse_length(props.getAttrNS(_FO_NS, "margin-bottom"))
            lh = props.getAttrNS(_FO_NS, "line-height")
            if spacing is None and lh and lh != "normal":
                spacing = float(lh[:-1]) / 100 if lh.endswith("%") else max(parse_length(lh, size) / size, 1.0)
        return align or "left", before or 0.0, after or 0.0, spacing or DEFAULT_LINE_SPACING


def _odt_picture(doc, frame) -> Optional[Picture]:
    from odf.element import Node

    try:
        image = next((c for c in frame.childNodes if c.nodeType == Node.ELEMENT_NODE and c.qname == (_DRAW_NS, "image")), None)
        href = image.getAttrNS(_XLINK_NS, "href") if image is not None else None
        if not href or href not in doc.Pictures:
            return None
        record = doc.Pictures[href]
        data = record[1]
        if not isinstance(data, (bytes, bytearray)):
            return None
        width, height = parse_length(frame.getAttrNS(_SVG_NS, "width")), parse_length(frame.getAttrNS(_SVG_NS, "height"))
        if width <= 0 or height <= 0:
            return None
        return Picture(bytes(data), width, height)
    except Exception:
        return None


def _odt_runs(doc, element, styles: _OdtStyles, para_chain: list, span_chains: list) -> list[Run]:
    from odf.element import Node

    runs: list[Run] = []
    font, size, bold, italic, underline = styles.font(span_chains + para_chain)

    def add_text(text: str) -> None:
        if runs and runs[-1].picture is None and (runs[-1].font, runs[-1].size, runs[-1].bold, runs[-1].italic, runs[-1].underline) == (font, size, bold, italic, underline):
            runs[-1].text += text
        else:
            runs.append(Run(text, font, size, bold, italic, underline))
    for child in element.childNodes:
        if child.nodeType == Node.TEXT_NODE:
            add_text(child.data)
        elif child.nodeType != Node.ELEMENT_NODE:
            continue
        elif child.qname == (_TEXT_NS, "span"):
            chain = styles.chain("text", child.getAttrNS(_TEXT_NS, "style-name"))
            runs.extend(_odt_runs(doc, child, styles, para_chain, chain + span_chains))
        elif child.qname == (_TEXT_NS, "line-break"):
            add_text("\n")
        elif child.qname == (_TEXT_NS, "tab"):
            add_text("\t")
        elif child.qname == (_TEXT_NS, "s"):
            add_text(" " * int(child.getAttrNS(_TEXT_NS, "c") or 1))
        elif child.qname == (_DRAW_NS, "frame"):
            picture = _odt_picture(doc, child)
            if picture is not None:
                runs.append(Run(font=font, size=size, picture=picture))
        elif child.qname[0] == _TEXT_NS:  # links, bookmarks, fields: their text
            runs.extend(_odt_runs(doc, child, styles, para_chain, span_chains))
    return runs


def _odt_para(doc, p, styles: _OdtStyles) -> Para:
    chain = styles.chain("paragraph", p.getAttrNS(_TEXT_NS, "style-name"))
    font, size, *_ = styles.font(chain)
    align, before, after, spacing = styles.layout(chain, size)
    return Para(_odt_runs(doc, p, styles, chain, []), font, size, align, before, after, spacing)


def odt_letter(data: bytes) -> Letter:
    """The model of a (filled) .odt: the first master page's layout, header and footer, and the body."""
    from odf import style
    from odf.element import Node
    from odf.opendocument import load

    doc = load(io.BytesIO(data))
    styles = _OdtStyles(doc)
    letter = Letter()
    masters = doc.getElementsByType(style.MasterPage)
    master = next((m for m in masters if m.getAttrNS(_STYLE_NS, "name") == "Standard"), masters[0] if masters else None)
    if master is not None:
        layouts = {pl.getAttrNS(_STYLE_NS, "name"): pl for pl in doc.getElementsByType(style.PageLayout)}
        layout = layouts.get(master.getAttrNS(_STYLE_NS, "page-layout-name"))
        props = _OdtStyles._props(layout, "page-layout-properties") if layout is not None else None
        if props is not None:
            letter.page_width, letter.page_height = _page_size_name(
                parse_length(props.getAttrNS(_FO_NS, "page-width"), A4[0]), parse_length(props.getAttrNS(_FO_NS, "page-height"), A4[1]))
            letter.margin_top = parse_length(props.getAttrNS(_FO_NS, "margin-top"), INCH)
            letter.margin_bottom = parse_length(props.getAttrNS(_FO_NS, "margin-bottom"), INCH)
            letter.margin_left = parse_length(props.getAttrNS(_FO_NS, "margin-left"), INCH)
            letter.margin_right = parse_length(props.getAttrNS(_FO_NS, "margin-right"), INCH)
            # In ODF the header sits inside the top margin area: the page's top margin is the header's distance.
            letter.header_distance, letter.footer_distance = letter.margin_top, letter.margin_bottom
        for child in master.childNodes:
            if child.nodeType != Node.ELEMENT_NODE:
                continue
            if child.qname == (_STYLE_NS, "header"):
                letter.header = [_odt_para(doc, p, styles) for p in _odt_paragraphs(child)]
            elif child.qname == (_STYLE_NS, "footer"):
                letter.footer = [_odt_para(doc, p, styles) for p in _odt_paragraphs(child)]
    letter.body = [_odt_para(doc, p, styles) for p in _odt_paragraphs(doc.text)]
    return letter


def plain_odt(letter_text: str, font: str, size: float = DEFAULT_SIZE) -> bytes:
    """The built-in layout as .odt (the same shape as ``plain_docx``)."""
    from odf import teletype
    from odf.opendocument import OpenDocumentText
    from odf.style import (FontFace, MasterPage, PageLayout, PageLayoutProperties, ParagraphProperties, Style,
                           TextProperties)
    from odf.text import P

    doc = OpenDocumentText()
    doc.fontfacedecls.addElement(FontFace(name=font, fontfamily=font, fontfamilygeneric="swiss", fontpitch="variable"))
    layout = PageLayout(name="LetterPage")
    layout.addElement(PageLayoutProperties(pagewidth="21cm", pageheight="29.7cm", printorientation="portrait",
                                           margintop="2.54cm", marginbottom="2.54cm", marginleft="2.54cm", marginright="2.54cm"))
    doc.automaticstyles.addElement(layout)
    doc.masterstyles.addElement(MasterPage(name="Standard", pagelayoutname="LetterPage"))
    para = Style(name="Letter", family="paragraph")
    para.addElement(ParagraphProperties(margintop="0cm", marginbottom=f"{size * 0.8:.2f}pt", lineheight=f"{int(DEFAULT_LINE_SPACING * 100)}%"))
    para.addElement(TextProperties(fontname=font, fontsize=f"{size:g}pt", fontnameasian=font, fontsizeasian=f"{size:g}pt",
                                   fontnamecomplex=font, fontsizecomplex=f"{size:g}pt"))
    doc.styles.addElement(para)
    for text in split_paragraphs(letter_text) or [""]:
        p = P(stylename=para)
        teletype.addTextToElement(p, text)
        doc.text.addElement(p)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# --------------------------------------------------------------------------
# One door for the rest of the app
# --------------------------------------------------------------------------

def fill(data: bytes, suffix: str, letter_text: str, values: dict) -> bytes:
    """A template filled with the letter: ``suffix`` says which kind it is."""
    if suffix == ".docx":
        return fill_docx(data, letter_text, values)
    if suffix == ".odt":
        return fill_odt(data, letter_text, values)
    raise TemplateError("A letter template is a .docx or .odt file.")


def plain(suffix: str, letter_text: str, font: str) -> bytes:
    return plain_docx(letter_text, font) if suffix == ".docx" else plain_odt(letter_text, font)


def letter_model(data: bytes, suffix: str) -> Letter:
    return docx_letter(data) if suffix == ".docx" else odt_letter(data)


def check_template(data: bytes, suffix: str) -> Letter:
    """Refuses (TemplateError) a file that can't serve as a template; returns the model of it filled with a
    sample, so the caller can see the fonts it asks for and the marker paragraph's font (the body paragraph that
    holds the sample)."""
    if suffix not in TEMPLATE_SUFFIXES:
        raise TemplateError("A letter template is a .docx or .odt file with a {{letter}} marker.")
    try:
        filled = fill(data, suffix, SAMPLE_TEXT, marker_values(SAMPLE_TEXT, "Company", "Job title"))
        return letter_model(filled, suffix)
    except TemplateError:
        raise
    except Exception:
        raise TemplateError("The file couldn't be read as a template. Save it again from your word processor as "
                            f"{suffix} and upload it once more.")


SAMPLE_TEXT = "Sample paragraph of the letter."


def marker_font(model: Letter) -> Optional[tuple[str, float]]:
    """The font the letter takes in a template: (family, size) of the sample paragraph in ``check_template``'s
    model."""
    for p in model.body:
        if p.text.strip() == SAMPLE_TEXT:
            run = next((r for r in p.runs if r.text.strip()), None)
            return (run.font, run.size) if run else (p.font, p.size)
    return None
