"""Cover letters as files: letter templates (.docx and .odt), the plain layout, the PDF and the downloads.

The filler (backend/letter_files.py) is tested on documents built here with python-docx and odfpy; the PDF
(backend/letter_pdf.py) on this computer's fonts, with a made-up font name for the substitution. The endpoints:
signed in and the job's workspace (a signed-out call is refused, another workspace answers 404), 409 for a format
the template can't give. No network: the model is mocked at backend.scorer.call_model.

Run:  python3 -m unittest tests.test_letters -v
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("JOBHUNT_DB_PATH", os.path.join(_TMP, "test.db"))  # never the real database
os.environ.setdefault("JOBHUNT_SCHEDULER", "off")
os.environ.setdefault("JOBHUNT_ENV_PATH", os.path.join(_TMP, "test.env"))

from tests import helpers  # noqa: E402  (signed-in clients; sets the test environment)

from fastapi.testclient import TestClient  # noqa: E402

import backend.scorer as sc  # noqa: E402
from backend import letter_files as lf  # noqa: E402
from backend import letter_pdf as lp  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import Document, Job, SessionLocal, Workspace, init_db  # noqa: E402

init_db()

LETTER = "Dear Hiring Manager,\n\nI led the ZQX launch.\nIt shipped on time.\n\nKind regards,\nJane Doe"
LLM_ENV = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "m"}
MISSING_FONT = "No Such Font Zq"


def setUpModule():
    helpers.enter_admin_scope()


def tearDownModule():
    helpers.exit_admin_scope()


def docx_template(font: str = "Garamond", marker_runs=("{{letter}}",), header: str = "Jane Doe · {{date}}",
                  before: str = "{{company}}: {{job_title}}", after: str = "Signed, {{name}} {{unknown}}",
                  align_right: bool = False) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    d = docx.Document()
    d.sections[0].header.paragraphs[0].text = header
    d.sections[0].footer.paragraphs[0].text = "Footer {{company}}"
    d.add_paragraph(before)
    p = d.add_paragraph()
    if align_right:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.paragraph_format.space_after = Pt(14)
    for i, piece in enumerate(marker_runs):
        run = p.add_run(piece)
        run.font.name, run.font.size, run.italic = font, Pt(12.5), True
    d.add_paragraph(after)
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def odt_template(marker: str = "{{letter}}", span: bool = True) -> bytes:
    from odf import teletype
    from odf.opendocument import OpenDocumentText
    from odf.style import ParagraphProperties, Style, TextProperties
    from odf.text import P, Span

    doc = OpenDocumentText()
    para = Style(name="Body", family="paragraph")
    para.addElement(ParagraphProperties(textalign="justify", marginbottom="0.4cm"))
    para.addElement(TextProperties(fontfamily="Caladea", fontsize="11.5pt"))
    doc.styles.addElement(para)
    bold = Style(name="Strong", family="text")
    bold.addElement(TextProperties(fontweight="bold"))
    doc.styles.addElement(bold)
    p = P(stylename=para)
    teletype.addTextToElement(p, "To {{company}} for the {{job_title}} role")
    doc.text.addElement(p)
    p = P(stylename=para)
    if span:
        holder = Span(stylename=bold)
        p.addElement(holder)
    else:
        holder = p
    teletype.addTextToElement(holder, marker)
    doc.text.addElement(p)
    p = P(stylename=para)
    teletype.addTextToElement(p, "{{name}}")
    doc.text.addElement(p)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


class FillerTest(unittest.TestCase):
    values = lf.marker_values(LETTER, "Acme Pty Ltd", "Program Manager")

    def test_docx_marker_paragraph_becomes_the_letter_in_its_formatting(self):
        model = lf.docx_letter(lf.fill_docx(docx_template(align_right=True), LETTER, self.values))
        texts = [p.text for p in model.body]
        self.assertEqual(texts, ["Acme Pty Ltd: Program Manager", "Dear Hiring Manager,",
                                 "I led the ZQX launch.\nIt shipped on time.", "Kind regards,\nJane Doe",
                                 "Signed, Jane Doe {{unknown}}"])
        for p in model.body[1:4]:  # each letter paragraph: the marker run's font, size and italic, its alignment
            self.assertEqual([(r.font, r.size, r.italic) for r in p.runs], [("Garamond", 12.5, True)])
            self.assertEqual((p.align, p.space_after), ("right", 14.0))
        self.assertEqual(model.body[0].align, "left")
        self.assertEqual([p.text for p in model.header], [f"Jane Doe · {lf.long_date()}"])
        self.assertEqual([p.text for p in model.footer], ["Footer Acme Pty Ltd"])
        self.assertIn("Garamond", model.families())

    def test_docx_marker_split_over_runs_and_a_second_marker(self):
        data = docx_template(marker_runs=("{{", "let", "ter}} {{letter}}"))
        model = lf.docx_letter(lf.fill_docx(data, "One paragraph only.", self.values))
        self.assertEqual([p.text for p in model.body], ["Acme Pty Ltd: Program Manager", "One paragraph only.",
                                                        "Signed, Jane Doe {{unknown}}"])

    def test_docx_without_the_marker_is_refused(self):
        with self.assertRaises(lf.TemplateError) as caught:
            lf.fill_docx(docx_template(marker_runs=("no marker here",)), LETTER, self.values)
        self.assertIn("{{letter}}", str(caught.exception))
        with self.assertRaises(lf.TemplateError):
            lf.check_template(b"not a zip", ".docx")
        with self.assertRaises(lf.TemplateError):
            lf.check_template(docx_template(), ".pdf")

    def test_docx_pictures_header_and_page_size_are_read(self):
        import docx
        from docx.shared import Inches

        d = docx.Document()
        d.sections[0].page_width, d.sections[0].page_height = Inches(8.5), Inches(11)
        d.add_paragraph("{{letter}}")
        from PIL import Image  # (reportlab brings Pillow)

        png_file = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(png_file, "PNG")
        png = png_file.getvalue()
        d.add_paragraph().add_run().add_picture(io.BytesIO(png), width=Inches(1))
        out = io.BytesIO()
        d.save(out)
        model = lf.docx_letter(lf.fill_docx(out.getvalue(), "Hello.", self.values))
        self.assertEqual((model.page_width, model.page_height), lf.US_LETTER)
        self.assertEqual(len(model.body[-1].pictures), 1)
        self.assertAlmostEqual(model.body[-1].pictures[0].width, 72.0, places=1)

    def test_odt_marker_paragraph_becomes_the_letter_in_its_formatting(self):
        model = lf.odt_letter(lf.fill_odt(odt_template(), LETTER, self.values))
        self.assertEqual([p.text for p in model.body], ["To Acme Pty Ltd for the Program Manager role",
                                                        "Dear Hiring Manager,", "I led the ZQX launch.\nIt shipped on time.",
                                                        "Kind regards,\nJane Doe", "Jane Doe"])
        for p in model.body[1:4]:  # the paragraph style (justified, Caladea 11.5) and the marker's span (bold)
            self.assertEqual((p.align, p.font, p.size), ("justify", "Caladea", 11.5))
            self.assertTrue(all(r.bold and r.font == "Caladea" for r in p.runs), p.runs)
            self.assertAlmostEqual(p.space_after, 0.4 * 72 / 2.54, places=2)
        self.assertFalse(model.body[0].runs[0].bold)
        self.assertEqual(model.families(), ["Caladea"])

    def test_odt_without_the_marker_is_refused_and_a_bare_marker_works(self):
        with self.assertRaises(lf.TemplateError):
            lf.fill_odt(odt_template(marker="nothing"), LETTER, self.values)
        model = lf.odt_letter(lf.fill_odt(odt_template(span=False), "Only one.", self.values))
        self.assertEqual([p.text for p in model.body][1], "Only one.")
        self.assertFalse(model.body[1].runs[0].bold)

    def test_plain_layouts_agree(self):
        for suffix in (".docx", ".odt"):
            model = lf.letter_model(lf.plain(suffix, LETTER, "Liberation Sans"), suffix)
            self.assertEqual([p.text for p in model.body], lf.split_paragraphs(LETTER), suffix)
            self.assertEqual((model.page_width, model.page_height), lf.A4, suffix)
            self.assertAlmostEqual(model.margin_left, 72.0, places=0)
            self.assertEqual(model.families(), ["Liberation Sans"], suffix)
            self.assertEqual({r.size for p in model.body for r in p.runs}, {11.0}, suffix)

    def test_helpers(self):
        self.assertEqual(lf.sign_off_name(LETTER), "Jane Doe")
        self.assertEqual(lf.sign_off_name("Dear team,\n\nA long last sentence that ends with a full stop."), "")
        self.assertEqual(lf.split_paragraphs("a\r\n\r\n\n b \n\nc"), ["a", "b", "c"])
        self.assertAlmostEqual(lf.parse_length("2.54cm"), 72.0, places=5)
        self.assertEqual(lf.parse_length("12pt"), 12.0)
        self.assertEqual(lf.parse_length("junk", 7.0), 7.0)
        self.assertEqual(lf.marker_font(lf.check_template(docx_template(), ".docx")), ("Garamond", 12.5))


class PdfTest(unittest.TestCase):
    def test_fonts_on_this_computer_and_stand_ins(self):
        cat = lp.catalogue()
        for family, files in list(cat.items())[:3]:
            self.assertTrue(all(p.is_file() for p in files.values()), family)
        used, files = lp.pick(MISSING_FONT)
        self.assertNotEqual(used, MISSING_FONT)
        self.assertEqual(lp.classify("Garamond"), "serif")
        self.assertEqual(lp.classify("Consolas"), "mono")
        self.assertEqual(lp.classify("Calibri"), "sans")
        self.assertIn(lp.pick("Garamond")[0], ("Garamond",) + lp.FALLBACKS["serif"][0] + ("Times-Roman",))
        self.assertEqual(lp.read_names(__file__), None)  # not a font
        report = lp.font_report([MISSING_FONT, lp.plain_font()])
        self.assertEqual(report[0]["found"], False)
        self.assertEqual(report[1]["found"] or report[1]["used"] == "Helvetica", True)

    def test_render_a_template_reports_substitutions(self):
        from pypdf import PdfReader

        data = lf.fill_docx(docx_template(font=MISSING_FONT), LETTER, lf.marker_values(LETTER, "Acme", "PM"))
        pdf, substitutions = lp.render(lf.docx_letter(data))
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual([s["asked"] for s in substitutions if s["asked"] == MISSING_FONT], [MISSING_FONT])
        text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        for piece in ("Dear Hiring Manager,", "ZQX launch", "Jane Doe", "Footer Acme", lf.long_date()):
            self.assertIn(piece, text)

    def test_render_the_plain_odt(self):
        pdf, substitutions = lp.render(lf.odt_letter(lf.plain_odt(LETTER, lp.plain_font())))
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(substitutions, [])


class EndpointTest(unittest.TestCase):
    """Each test: its own workspace with one job and a saved letter."""

    def setUp(self):
        self.client = helpers.client()
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.ws = self.client.post("/api/workspaces", json={"name": self._testMethodName[:80]}).json()["id"]
        self.h = {"X-Workspace": str(self.ws)}
        self.addCleanup(lambda: self.client.delete(f"/api/workspaces/{self.ws}"))
        db = SessionLocal()
        job = Job(workspace_id=self.ws, url=f"https://letters.example/{self.ws}", title="Program Manager", company="Acme",
                  description="Lead programs.", status="to_review", detail_status="full")
        db.add(job)
        db.commit()
        self.job_id = job.id
        db.close()
        r = self.client.put(f"/api/jobs/{self.job_id}/cover-letter", headers=self.h, json={"text": LETTER})
        self.assertEqual(r.status_code, 200, r.text)

    def upload(self, name: str, data: bytes, kind: str = "letter_template"):
        return self.client.post("/api/documents", headers=self.h, files={"file": (name, data, "application/octet-stream")},
                                data={"kind": kind})

    def download(self, fmt: str):
        return self.client.get(f"/api/jobs/{self.job_id}/cover-letter.{fmt}", headers=self.h)

    def test_plain_layout_gives_every_format(self):
        info = self.client.get("/api/documents/letter-template", headers=self.h).json()
        self.assertIsNone(info["template"])
        self.assertTrue(all(f["available"] for f in info["formats"].values()))
        self.assertEqual(list(info["formats"]), ["pdf", "odt", "docx"])
        for fmt, magic in (("pdf", b"%PDF-"), ("odt", b"PK"), ("docx", b"PK")):
            r = self.download(fmt)
            self.assertEqual(r.status_code, 200, fmt)
            self.assertTrue(r.content.startswith(magic), fmt)
            self.assertIn(f'filename="Cover letter - Acme - Program Manager.{fmt}"', r.headers["content-disposition"])
        self.assertEqual(json.loads(self.download("pdf").headers["x-font-substitutions"]), [])

    def test_docx_template_gives_docx_and_pdf_with_the_substitution_header(self):
        r = self.upload("letterhead.docx", docx_template(font=MISSING_FONT))
        self.assertEqual(r.status_code, 200, r.text)
        doc = r.json()
        self.assertEqual((doc["kind"], doc["use_for_scoring"], doc["current_template"]), ("letter_template", False, True))
        info = self.client.get("/api/documents/letter-template", headers=self.h).json()
        self.assertEqual(info["template"]["id"], doc["id"])
        self.assertEqual((info["formats"]["pdf"]["available"], info["formats"]["docx"]["available"],
                          info["formats"]["odt"]["available"]), (True, True, False))
        self.assertIn(".odt", info["formats"]["odt"]["reason"])
        self.assertEqual(info["letter_font"], {"family": MISSING_FONT, "size": 12.5})
        self.assertIn({"asked": MISSING_FONT, "used": lp.pick(MISSING_FONT)[0], "found": False}, info["fonts"])
        r = self.download("pdf")
        self.assertEqual(r.status_code, 200)
        subs = json.loads(r.headers["x-font-substitutions"])
        self.assertIn(MISSING_FONT, [s["asked"] for s in subs])
        r = self.download("docx")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Dear Hiring Manager,", [p.text for p in lf.docx_letter(r.content).body])
        r = self.download("odt")
        self.assertEqual(r.status_code, 409)
        self.assertIn("Word (.docx) file", r.json()["detail"])

    def test_odt_template_gives_odt_and_pdf(self):
        self.assertEqual(self.upload("letterhead.odt", odt_template()).status_code, 200)
        self.assertEqual(self.download("odt").status_code, 200)
        self.assertEqual(self.download("pdf").status_code, 200)
        r = self.download("docx")
        self.assertEqual(r.status_code, 409)
        self.assertIn("OpenDocument (.odt) file", r.json()["detail"])

    def test_template_uploads_are_checked(self):
        r = self.upload("no-marker.docx", docx_template(marker_runs=("plain text",)))
        self.assertEqual(r.status_code, 400)
        self.assertIn("{{letter}}", r.json()["detail"])
        self.assertEqual(self.upload("cv.pdf", b"%PDF-1.4 junk").status_code, 400)  # a template is .docx or .odt
        self.assertEqual(self.upload("broken.docx", b"PK junk").status_code, 400)
        self.assertEqual(self.client.get("/api/documents/letter-template", headers=self.h).json()["template"], None)

    def test_choosing_among_templates_and_deleting_the_current_one(self):
        first = self.upload("first.docx", docx_template()).json()
        second = self.upload("second.odt", odt_template()).json()
        self.assertEqual((first["current_template"], second["current_template"]), (True, False))
        r = self.client.put("/api/documents/letter-template", headers=self.h, json={"id": second["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["template"]["filename"], "second.odt")
        listed = {d["id"]: d["current_template"] for d in self.client.get("/api/documents", headers=self.h).json()}
        self.assertEqual(listed, {first["id"]: False, second["id"]: True})
        cv = self.client.post("/api/documents/notes", headers=self.h, json={"title": "cv", "text": "Jane.", "kind": "resume"}).json()
        self.assertEqual(self.client.put("/api/documents/letter-template", headers=self.h, json={"id": cv["id"]}).status_code, 400)
        r = self.client.put("/api/documents/letter-template", headers=self.h, json={"id": None})
        self.assertIsNone(r.json()["template"])
        self.client.put("/api/documents/letter-template", headers=self.h, json={"id": second["id"]})
        self.assertEqual(self.client.delete(f"/api/documents/{second['id']}", headers=self.h).status_code, 200)
        self.assertIsNone(self.client.get("/api/documents/letter-template", headers=self.h).json()["template"])
        self.assertEqual(self.download("docx").status_code, 200)  # the plain layout again

    def test_a_template_is_never_sent_to_the_ai(self):
        from backend.cover_letters import build_prompt

        self.upload("letterhead.docx", docx_template(header="SECRET LETTERHEAD {{date}}"))
        tid = self.client.get("/api/documents", headers=self.h).json()[0]["id"]
        self.assertEqual(self.client.patch(f"/api/documents/{tid}", headers=self.h, json={"use_for_scoring": True}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/documents/{tid}", headers=self.h, json={"kind": "resume"}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/documents/{tid}/concise", headers=self.h).status_code, 400)
        cv = self.client.post("/api/documents/notes", headers=self.h, json={"title": "cv", "text": "Jane led things.", "kind": "resume"}).json()
        self.assertEqual(self.client.patch(f"/api/documents/{cv['id']}", headers=self.h, json={"kind": "letter_template"}).status_code, 400)
        db = SessionLocal()
        try:
            db.get(Document, tid).use_for_scoring = True  # even forced on in the database
            db.commit()
            profile, _ = sc.build_profile(db, self.ws)
            prompt = build_prompt(db, db.get(Job, self.job_id))
        finally:
            db.close()
        self.assertNotIn("letterhead", profile.lower())
        self.assertNotIn("letterhead", prompt.lower())
        self.assertIn("Jane led things.", prompt)

    def test_draft_edits_and_discard(self):
        self.client.post("/api/documents/notes", headers=self.h, json={"title": "cv", "text": "Jane led things.", "kind": "resume"})
        with mock.patch.object(sc, "call_model", return_value="Dear Hiring Manager,\n\nDrafted.\n\nJane"), \
                mock.patch.dict(os.environ, LLM_ENV):
            r = self.client.post(f"/api/jobs/{self.job_id}/cover-letter", headers=self.h, json={})
        self.assertEqual(r.status_code, 200, r.text)
        letter = r.json()
        self.assertEqual((letter["draft_text"], letter["edited"]), (letter["text"], False))
        r = self.client.put(f"/api/jobs/{self.job_id}/cover-letter", headers=self.h, json={"text": "Edited by me."})
        self.assertEqual((r.json()["edited"], r.json()["draft_text"]), (True, letter["draft_text"]))
        r = self.client.put(f"/api/jobs/{self.job_id}/cover-letter", headers=self.h, json={"text": letter["draft_text"]})
        self.assertEqual((r.json()["edited"], r.json()["text"]), (False, letter["draft_text"]))  # back to the draft

    def test_access_is_the_job_workspace_signed_in(self):
        other = self.client.post("/api/workspaces", json={"name": "other"}).json()["id"]
        self.addCleanup(lambda: self.client.delete(f"/api/workspaces/{other}"))
        for fmt in ("pdf", "odt", "docx"):
            self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}/cover-letter.{fmt}",
                                             headers={"X-Workspace": str(other)}).status_code, 404, fmt)
        with TestClient(app) as anonymous:
            for fmt in ("pdf", "odt", "docx"):
                self.assertEqual(anonymous.get(f"/api/jobs/{self.job_id}/cover-letter.{fmt}").status_code, 401, fmt)
            self.assertEqual(anonymous.get("/api/documents/letter-template").status_code, 401)
            self.assertEqual(anonymous.put("/api/documents/letter-template", json={"id": None}).status_code, 401)
        template = self.upload("letterhead.docx", docx_template()).json()
        r = self.client.put("/api/documents/letter-template", headers={"X-Workspace": str(other)}, json={"id": template["id"]})
        self.assertEqual(r.status_code, 404)  # another workspace's template can't be chosen
        self.assertIsNone(self.client.get("/api/documents/letter-template", headers={"X-Workspace": str(other)}).json()["template"])

    def test_a_copied_workspace_keeps_its_template_and_the_export_names_it(self):
        from backend import import_file

        template = self.upload("letterhead.docx", docx_template()).json()
        copy = self.client.post("/api/workspaces", json={"name": "copy", "copy_from": self.ws, "copy_documents": True}).json()["id"]
        self.addCleanup(lambda: self.client.delete(f"/api/workspaces/{copy}"))
        info = self.client.get("/api/documents/letter-template", headers={"X-Workspace": str(copy)}).json()
        self.assertEqual(info["template"]["filename"], "letterhead.docx")
        self.assertNotEqual(info["template"]["id"], template["id"])
        db = SessionLocal()
        try:
            data, _ = import_file.workspace_data(db, db.get(Workspace, self.ws), {})
        finally:
            db.close()
        docs = {d["ref"]: d for d in data["documents"]}
        self.assertEqual(docs[data["letter_template"]]["kind"], "letter_template")
        job = next(j for j in data["jobs"] if j["cover_letter"])
        self.assertEqual(job["cover_letter"]["text"], LETTER)
        import_file.WorkspaceIn.model_validate(data)  # what an import reads back


if __name__ == "__main__":
    unittest.main()
