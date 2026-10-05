"""Tests for documents: notes, the preferences kind, concise versions and the summary document. No network: the LLM
is mocked at backend.scorer.call_model.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("JOBHUNT_DB_PATH", os.path.join(_TMP, "test.db"))  # never the real database
os.environ.setdefault("JOBHUNT_SCHEDULER", "off")
os.environ.setdefault("JOBHUNT_ENV_PATH", os.path.join(_TMP, "test.env"))

from fastapi.testclient import TestClient  # noqa: E402

import backend.scorer as sc  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import Document, Job, SessionLocal, init_db  # noqa: E402

init_db()

LLM_ENV = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "m"}


class DocsTestCase(unittest.TestCase):
    """Each test gets a workspace of its own (deleted afterwards), so documents from other tests don't mix in."""

    def setUp(self):
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.ws = self.client.post("/api/workspaces", json={"name": self._testMethodName[:80]}).json()["id"]
        self.h = {"X-Workspace": str(self.ws)}
        self.addCleanup(lambda: self.client.delete(f"/api/workspaces/{self.ws}"))

    def add_doc(self, filename, kind, text, **extra) -> int:
        db = SessionLocal()
        doc = Document(workspace_id=self.ws, filename=filename, filetype=extra.pop("filetype", ".pdf"), kind=kind,
                       text=text, **extra)
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()
        return doc_id

    def profile(self) -> tuple[str, str]:
        db = SessionLocal()
        try:
            return sc.build_profile(db, self.ws)
        finally:
            db.close()


class NoteTest(DocsTestCase):
    def test_create_edit_rename_retag_and_delete(self):
        c, h = self.client, self.h
        r = c.post("/api/documents/notes", headers=h, json={"title": "  Project Atlas  ", "text": "Led the Atlas rollout to 40 schools.\n"})
        self.assertEqual(r.status_code, 201, r.text)
        note = r.json()
        self.assertEqual((note["filename"], note["filetype"], note["source"], note["kind"]), ("Project Atlas", "note", "note", "other"))
        self.assertTrue(note["editable"] and note["use_for_scoring"])
        self.assertEqual(note["text_len"], len("Led the Atlas rollout to 40 schools."))
        text, first_hash = self.profile()
        self.assertIn("=== SUPPORTING DOCUMENT (candidate's note): Project Atlas ===\nLed the Atlas rollout to 40 schools.", text)

        r = c.patch(f"/api/documents/{note['id']}", headers=h, json={"title": "Atlas rollout", "text": "Led the Atlas rollout to 42 schools.", "kind": "resume"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["filename"], r.json()["kind"]), ("Atlas rollout", "resume"))
        self.assertIsNotNone(r.json()["updated_at"])
        body = c.get(f"/api/documents/{note['id']}/text", headers=h).json()
        self.assertEqual(body["text"], "Led the Atlas rollout to 42 schools.")
        text, second_hash = self.profile()
        self.assertIn("=== RESUME / CV (candidate's note): Atlas rollout ===", text)
        self.assertNotEqual(first_hash, second_hash)  # editing marks scores stale

        self.assertEqual(c.patch(f"/api/documents/{note['id']}", headers=h, json={"text": "   "}).status_code, 422)
        self.assertEqual(c.patch(f"/api/documents/{note['id']}", headers=h, json={"title": ""}).status_code, 422)
        self.assertEqual(c.post("/api/documents/notes", headers=h, json={"title": "x", "text": "  "}).status_code, 422)
        self.assertEqual(c.post("/api/documents/notes", headers=h, json={"title": "x", "text": "y", "kind": "diary"}).status_code, 422)

        # A note has no file: deleting it must not touch the uploads folder.
        with mock.patch("pathlib.Path.unlink", side_effect=AssertionError("a note has no file to delete")):
            self.assertEqual(c.delete(f"/api/documents/{note['id']}", headers=h).status_code, 200)
        self.assertEqual(c.get("/api/documents", headers=h).json(), [])

    def test_notes_belong_to_their_workspace(self):
        c = self.client
        note = c.post("/api/documents/notes", headers=self.h, json={"title": "Mine", "text": "private"}).json()
        other = c.post("/api/workspaces", json={"name": "Someone else"}).json()["id"]
        self.addCleanup(lambda: c.delete(f"/api/workspaces/{other}"))
        h2 = {"X-Workspace": str(other)}
        self.assertEqual(c.get("/api/documents", headers=h2).json(), [])
        self.assertEqual(c.get(f"/api/documents/{note['id']}/text", headers=h2).status_code, 404)
        self.assertEqual(c.patch(f"/api/documents/{note['id']}", headers=h2, json={"text": "hijacked"}).status_code, 404)
        self.assertEqual(c.post(f"/api/documents/{note['id']}/concise", headers=h2).status_code, 404)
        self.assertEqual(c.delete(f"/api/documents/{note['id']}", headers=h2).status_code, 404)
        self.assertEqual(c.get(f"/api/documents/{note['id']}/text", headers=self.h).json()["text"], "private")

    def test_uploaded_text_is_read_only_but_can_be_renamed(self):
        doc_id = self.add_doc("cv.pdf", "resume", "Jane Doe.")
        self.assertEqual(self.client.patch(f"/api/documents/{doc_id}", headers=self.h, json={"text": "Better"}).status_code, 400)
        r = self.client.patch(f"/api/documents/{doc_id}", headers=self.h, json={"title": "Jane's CV"})
        self.assertEqual((r.status_code, r.json()["filename"], r.json()["editable"]), (200, "Jane's CV", False))

    def test_copying_a_workspace_copies_notes_concise_versions_and_summary_sources(self):
        c = self.client
        cv = self.add_doc("cv.pdf", "resume", "Jane Doe. Ran programs.", concise_text="Jane Doe: programs.", use_concise=True)
        summary = self.add_doc("Summary of 1 document", "resume", "Jane runs programs.", filetype="summary", summary_of=[cv])
        copy = c.post("/api/workspaces", json={"name": "Copy", "copy_from": self.ws, "copy_documents": True}).json()["id"]
        self.addCleanup(lambda: c.delete(f"/api/workspaces/{copy}"))
        docs = {d["filename"]: d for d in c.get("/api/documents", headers={"X-Workspace": str(copy)}).json()}
        self.assertTrue(docs["cv.pdf"]["use_concise"])
        self.assertEqual(docs["Summary of 1 document"]["summary_of"], [docs["cv.pdf"]["id"]])
        self.assertNotEqual(docs["cv.pdf"]["id"], cv)
        self.assertNotEqual(docs["Summary of 1 document"]["id"], summary)


class PreferencesTest(DocsTestCase):
    def test_kind_is_accepted_everywhere_kinds_are_chosen(self):
        c, h = self.client, self.h
        r = c.post("/api/documents", headers=h, files={"file": ("wishes.txt", b"Remote only, 3 days max in an office.")},
                   data={"kind": "preferences"})
        self.assertEqual((r.status_code, r.json()["kind"]), (200, "preferences"), r.text)
        note = c.post("/api/documents/notes", headers=h, json={"title": "Pay", "text": "At least $150k.", "kind": "preferences"}).json()
        self.assertEqual(note["kind"], "preferences")
        self.assertEqual(c.patch(f"/api/documents/{note['id']}", headers=h, json={"kind": "other"}).json()["kind"], "other")
        self.assertEqual(c.patch(f"/api/documents/{note['id']}", headers=h, json={"kind": "preferences"}).json()["kind"], "preferences")
        c.delete(f"/api/documents/{r.json()['id']}", headers=h)

    def test_build_profile_puts_preferences_last_under_their_own_heading(self):
        self.add_doc("wishes", "preferences", "No gambling companies.", filetype="note")
        self.add_doc("extra.pdf", "other", "Volunteer coach.")
        self.add_doc("cv.pdf", "resume", "Jane Doe. Program lead.")
        self.add_doc("letter.pdf", "cover_letter", "Dear team.")
        text, _ = self.profile()
        order = ["=== RESUME / CV: cv.pdf ===", "=== COVER LETTER: letter.pdf ===", "=== SUPPORTING DOCUMENT: extra.pdf ===",
                 sc.PREFERENCES_HEADING, "=== PREFERENCES (candidate's note): wishes ===\nNo gambling companies."]
        positions = [text.index(part) for part in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Not evidence of skills or experience", sc.PREFERENCES_HEADING)
        self.assertIn("CANDIDATE PREFERENCES", sc.SYSTEM_PROMPT)  # the scorer is told how to treat them

    def test_documents_without_preferences_give_the_same_profile_as_before(self):
        """Upgrading must not mark every existing score stale: plain documents render exactly as they used to."""
        self.add_doc("cv.pdf", "resume", "  Jane Doe.  ")
        self.add_doc("extra.pdf", "other", "Coach.")
        self.assertEqual(self.profile()[0], "=== RESUME / CV: cv.pdf ===\nJane Doe.\n\n=== SUPPORTING DOCUMENT: extra.pdf ===\nCoach.")

    def test_preferences_never_reach_a_cover_letter(self):
        from backend.cover_letters import build_prompt

        self.add_doc("cv.pdf", "resume", "Jane Doe. Program lead at Foo.")
        self.add_doc("wishes", "preferences", "SECRET: I'd leave within a year for more money.", filetype="note")
        self.add_doc("extra", "other", "Volunteer coach.", filetype="note")
        self.add_doc("old.pdf", "cover_letter", "Dear team, I build things.", concise_text="Builds things.", use_concise=True)
        db = SessionLocal()
        job = Job(workspace_id=self.ws, url=f"https://prefs.example/{self.ws}", title="Program Manager", company="Acme",
                  description="Lead programs.", status="to_review", detail_status="full")
        db.add(job)
        db.commit()
        job_id = job.id
        prompt = build_prompt(db, job)
        db.close()
        self.assertNotIn("SECRET", prompt)
        self.assertNotIn("PREFERENCES", prompt)
        self.assertIn("Jane Doe. Program lead at Foo.", prompt)
        self.assertIn("Volunteer coach.", prompt)
        self.assertIn("Dear team, I build things.", prompt)  # style samples stay the letter as written

        sent = []
        with mock.patch.object(sc, "call_model", side_effect=lambda m, cfg, json_mode=True: sent.append(m) or "Dear Hiring Manager,\n\nHi.\n\nJane"), \
                mock.patch.dict(os.environ, LLM_ENV):
            r = self.client.post(f"/api/jobs/{job_id}/cover-letter", headers=self.h, json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("SECRET", repr(sent))
        # ... while the scorer, the chat and title suggestions do get them.
        self.assertIn("SECRET", self.profile()[0])


class ConciseTest(DocsTestCase):
    def test_choice_of_version_drives_the_profile_and_its_hash(self):
        c, h = self.client, self.h
        doc_id = self.add_doc("cv.pdf", "resume", "Jane Doe. Led 12 releases at Foo, 2019-2025. Long story...")
        original_text, original_hash = self.profile()
        self.assertEqual(c.patch(f"/api/documents/{doc_id}", headers=h, json={"use_concise": True}).status_code, 409)  # none yet

        r = c.patch(f"/api/documents/{doc_id}", headers=h, json={"concise_text": "Jane Doe: 12 releases at Foo, 2019-2025."})
        self.assertEqual((r.json()["concise_len"], r.json()["use_concise"]), (40, False))
        self.assertEqual(self.profile(), (original_text, original_hash))  # stored, not sent yet

        r = c.patch(f"/api/documents/{doc_id}", headers=h, json={"use_concise": True})
        self.assertTrue(r.json()["use_concise"])
        self.assertEqual(r.json()["sent_len"], 40)
        concise_text, concise_hash = self.profile()
        self.assertIn("=== RESUME / CV: cv.pdf ===\nJane Doe: 12 releases at Foo, 2019-2025.", concise_text)
        self.assertNotIn("Long story", concise_text)
        self.assertNotEqual(concise_hash, original_hash)

        r = c.patch(f"/api/documents/{doc_id}", headers=h, json={"concise_text": "Jane Doe: 12 releases at Foo (2019-2025)."})
        self.assertNotEqual(self.profile()[1], concise_hash)  # editing the concise text marks scores stale too

        c.patch(f"/api/documents/{doc_id}", headers=h, json={"use_concise": False})
        self.assertEqual(self.profile(), (original_text, original_hash))  # back to the original
        r = c.patch(f"/api/documents/{doc_id}", headers=h, json={"use_concise": True})
        r = c.patch(f"/api/documents/{doc_id}", headers=h, json={"concise_text": ""})  # removing it falls back
        self.assertEqual((r.json()["concise_len"], r.json()["use_concise"]), (0, False))
        self.assertEqual(self.profile(), (original_text, original_hash))

    def test_writing_a_concise_version_with_the_llm(self):
        c, h = self.client, self.h
        doc_id = self.add_doc("transcript.pdf", "other", "Um, so I led the migration of 300 courses, Master's not yet conferred.")
        sent = []

        def fake(messages, cfg, json_mode=True):
            sent.append((messages, cfg, json_mode))
            return "```\nLed the migration of 300 courses. Master's not yet conferred.\n```"
        with mock.patch.object(sc, "call_model", side_effect=fake), mock.patch.dict(os.environ, LLM_ENV):
            r = c.post(f"/api/documents/{doc_id}/concise", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["concise_text"], "Led the migration of 300 courses. Master's not yet conferred.")  # fence removed
        self.assertTrue(body["use_concise"])
        self.assertEqual((body["concise_model"], body["text"][:6]), ("m", "Um, so"))
        messages, cfg, json_mode = sent[0]
        self.assertFalse(json_mode)
        self.assertEqual((cfg["purpose"], cfg["workspace_id"], cfg["long_output"]), ("concise", self.ws, True))
        system = messages[0]["content"]
        for rule in ("Never invent", "not yet conferred", "verbatim", "dates", "numbers"):
            self.assertIn(rule, system)
        self.assertIn("300 courses", messages[1]["content"])
        self.assertIn("Led the migration of 300 courses", self.profile()[0])
        with mock.patch.dict(os.environ, {**LLM_ENV, "LLM_MODEL": ""}):
            self.assertEqual(c.post(f"/api/documents/{doc_id}/concise", headers=h).status_code, 400)  # not configured


class SummaryTest(DocsTestCase):
    def test_summary_document_flow(self):
        c, h = self.client, self.h
        cv = self.add_doc("cv.pdf", "resume", "Jane Doe. Program lead at Foo 2019-2025. PMP.")
        letter = self.add_doc("letter.pdf", "cover_letter", "Dear Acme, I led programs at Foo since 2019.")
        self.add_doc("wishes", "preferences", "SECRET wish: remote only.", filetype="note")
        unticked = self.add_doc("old.pdf", "resume", "Outdated CV.", use_for_scoring=False)
        sent = []

        def fake(messages, cfg, json_mode=True):
            sent.append((messages, cfg))
            return "SUMMARY: Jane Doe, program lead.\nEXPERIENCE:\n- Program lead, Foo, 2019-2025\nEDUCATION, QUALIFICATIONS AND CERTIFICATIONS:\n- PMP"
        with mock.patch.object(sc, "call_model", side_effect=fake), mock.patch.dict(os.environ, LLM_ENV):
            r = c.post("/api/documents/summary", headers=h)
        self.assertEqual(r.status_code, 201, r.text)
        summary = r.json()
        self.assertEqual((summary["filename"], summary["source"], summary["kind"]), ("Summary of 2 documents", "summary", "resume"))
        self.assertTrue(summary["use_for_scoring"] and summary["editable"])
        self.assertEqual(summary["summary_of"], [cv, letter])
        self.assertEqual([s["id"] for s in summary["sources"]], [cv, letter])

        messages, cfg = sent[0]
        system, user = messages[0]["content"], messages[1]["content"]
        for rule in ("Never invent", "not yet conferred", "disagree", "preferences", "verbatim"):
            self.assertIn(rule, system)
        self.assertIn("Jane Doe. Program lead at Foo 2019-2025. PMP.", user)
        self.assertIn("I led programs at Foo since 2019.", user)
        self.assertNotIn("SECRET", user)        # preferences stay separate
        self.assertNotIn("Outdated CV", user)   # only ticked documents
        self.assertEqual((cfg["purpose"], cfg["workspace_id"]), ("summary", self.ws))

        # The originals are left ticked (the UI suggests unticking them): until then their content goes twice.
        docs = {d["id"]: d for d in c.get("/api/documents", headers=h).json()}
        self.assertTrue(docs[cv]["use_for_scoring"] and docs[letter]["use_for_scoring"])
        self.assertFalse(docs[unticked]["use_for_scoring"])
        text, _ = self.profile()
        self.assertIn("=== RESUME / CV: Summary of 2 documents ===\nSUMMARY: Jane Doe", text)
        self.assertIn("Jane Doe. Program lead at Foo", text)

        # Readable and editable like a note.
        r = c.patch(f"/api/documents/{summary['id']}", headers=h, json={"text": "SUMMARY: Jane Doe, program lead (PMP)."})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get(f"/api/documents/{summary['id']}/text", headers=h).json()["text"], "SUMMARY: Jane Doe, program lead (PMP).")
        for doc_id in (cv, letter):
            c.patch(f"/api/documents/{doc_id}", headers=h, json={"use_for_scoring": False})
        text, _ = self.profile()
        self.assertNotIn("Jane Doe. Program lead at Foo", text)
        self.assertIn("SUMMARY: Jane Doe, program lead (PMP).", text)
        self.assertIn("SECRET wish", text)

    def test_nothing_ticked_and_no_llm(self):
        self.add_doc("wishes", "preferences", "Remote only.", filetype="note")
        with mock.patch.object(sc, "call_model", side_effect=AssertionError("no request without documents")), \
                mock.patch.dict(os.environ, LLM_ENV):
            r = self.client.post("/api/documents/summary", headers=self.h)
        self.assertEqual(r.status_code, 400)
        self.assertIn("preferences", r.json()["detail"])


class MigrationTest(unittest.TestCase):
    def test_an_old_documents_table_gets_the_new_columns(self):
        """Databases from before notes and concise versions keep working: init_db adds the missing columns."""
        from sqlalchemy import create_engine, inspect, text

        import backend.db as dbm

        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = create_engine(f"sqlite:///{path}")
        with old.begin() as conn:
            conn.execute(text("CREATE TABLE documents (id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL DEFAULT 1, "
                              "filename VARCHAR NOT NULL, filetype VARCHAR, stored_name VARCHAR, text TEXT, kind VARCHAR, "
                              "use_for_scoring BOOLEAN NOT NULL DEFAULT 1, created_at DATETIME)"))
            conn.execute(text("INSERT INTO documents (filename, filetype, text, kind) VALUES ('cv.pdf', '.pdf', 'Jane', 'resume')"))
        with mock.patch.object(dbm, "engine", old):
            dbm._add_missing_columns()
        columns = {c["name"] for c in inspect(old).get_columns("documents")}
        self.assertTrue({"concise_text", "use_concise", "concise_model", "concise_at", "summary_of", "updated_at"} <= columns)
        with old.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT use_concise, concise_text FROM documents")).one(), (0, None))
        old.dispose()


if __name__ == "__main__":
    unittest.main()
