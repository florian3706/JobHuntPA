"""Tests for cover letter blocks (backend.letter_blocks) and backend.textclean. No network.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace

_TMP = tempfile.mkdtemp()
os.environ.setdefault("JOBHUNT_DB_PATH", os.path.join(_TMP, "test.db"))  # never the real database
os.environ.setdefault("JOBHUNT_SCHEDULER", "off")
os.environ.setdefault("JOBHUNT_ENV_PATH", os.path.join(_TMP, "test.env"))

from tests import helpers  # noqa: E402  (signed-in clients; sets the test environment)

from backend.db import init_db  # noqa: E402
from backend.letter_blocks import assemble_letter, placeholders_in  # noqa: E402
from backend.textclean import plain_text  # noqa: E402

init_db()


def setUpModule():
    helpers.enter_admin_scope()


def job(**kw):
    base = dict(company="Acme", title="Program Manager", location_text="Sydney")
    return SimpleNamespace(**{**base, **kw})


def block(text):
    return SimpleNamespace(text=text)


class BlockApiTest(unittest.TestCase):
    def test_crud_order_and_validation(self):
        with helpers.client() as c:
            for b in c.get("/api/letter-blocks").json():
                c.delete(f"/api/letter-blocks/{b['id']}")
            body1 = c.post("/api/letter-blocks", json={"kind": "body", "title": " B1 ", "text": "Body one {company}",
                                                       "tags": [" SQL ", "sql", "", "Budget"]}).json()
            closing = c.post("/api/letter-blocks", json={"kind": "closing", "title": "C", "text": "Bye"}).json()
            opening = c.post("/api/letter-blocks", json={"kind": "opening", "title": "O", "text": "Hi"}).json()
            body2 = c.post("/api/letter-blocks", json={"kind": "body", "title": "B2", "text": "Two {compny}"}).json()
            self.assertEqual(body1["title"], "B1")
            self.assertEqual(body1["tags"], ["SQL", "Budget"])
            self.assertEqual(body1["unknown_placeholders"], [])
            self.assertEqual(body2["unknown_placeholders"], ["compny"])
            self.assertEqual([body1["position"], closing["position"], opening["position"], body2["position"]],
                             [body1["position"] + i for i in range(4)])
            ids = [b["id"] for b in c.get("/api/letter-blocks").json()]
            self.assertEqual(ids, [opening["id"], body1["id"], body2["id"], closing["id"]])

            r = c.put("/api/letter-blocks/order", json={"ids": [body2["id"], body1["id"]]})
            self.assertEqual(r.status_code, 200, r.text)
            ids = [b["id"] for b in c.get("/api/letter-blocks").json()]
            self.assertEqual(ids, [opening["id"], body2["id"], body1["id"], closing["id"]])

            self.assertEqual(c.post("/api/letter-blocks", json={"kind": "middle", "text": "x"}).status_code, 422)
            self.assertEqual(c.patch(f"/api/letter-blocks/{body1['id']}", json={"kind": "nope"}).status_code, 422)
            self.assertEqual(c.post("/api/letter-blocks", json={"text": "x" * 8001}).status_code, 422)
            self.assertEqual(c.post("/api/letter-blocks", json={"tags": ["t" * 61]}).status_code, 422)

            r = c.patch(f"/api/letter-blocks/{body1['id']}", json={"text": "New {role} {x}", "kind": "closing",
                                                                  "tags": ["a", "A"]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["text"], "New {role} {x}")
            self.assertEqual(r.json()["kind"], "closing")
            self.assertEqual(r.json()["tags"], ["a"])
            self.assertEqual(r.json()["unknown_placeholders"], ["x"])
            self.assertEqual(r.json()["title"], "B1")

            self.assertEqual(c.delete(f"/api/letter-blocks/{body1['id']}").status_code, 204)
            self.assertEqual(c.delete(f"/api/letter-blocks/{body1['id']}").status_code, 404)
            for i in (opening["id"], body2["id"], closing["id"]):
                c.delete(f"/api/letter-blocks/{i}")

    def test_other_workspace(self):
        with helpers.client() as c:
            mine = c.post("/api/letter-blocks", json={"title": "Mine", "text": "secret"}).json()
            ws2 = c.post("/api/workspaces", json={"name": "Other"}).json()["id"]
            other = {"X-Workspace": str(ws2)}
            try:
                self.assertEqual(c.get("/api/letter-blocks", headers=other).json(), [])
                self.assertEqual(c.patch(f"/api/letter-blocks/{mine['id']}", json={"title": "x"},
                                         headers=other).status_code, 404)
                self.assertEqual(c.delete(f"/api/letter-blocks/{mine['id']}", headers=other).status_code, 404)
                self.assertEqual(c.put("/api/letter-blocks/order", json={"ids": [mine["id"]]},
                                       headers=other).status_code, 404)
                theirs = c.post("/api/letter-blocks", json={"title": "T", "text": "t"}, headers=other).json()
                self.assertEqual(c.put("/api/letter-blocks/order", json={"ids": [mine["id"], theirs["id"]]}
                                       ).status_code, 404)
            finally:
                c.delete(f"/api/workspaces/{ws2}")
                c.delete(f"/api/letter-blocks/{mine['id']}")


class AssembleTest(unittest.TestCase):
    def test_filled_and_joined(self):
        text, unfilled = assemble_letter([block(" Dear {company},\n\n\n\nRole: {role} "), block("In {location} on {date}.")],
                                         job(), today=date(2026, 10, 3))
        self.assertEqual(text, "Dear Acme,\n\nRole: Program Manager\n\nIn Sydney on 3 October 2026.")
        self.assertEqual(unfilled, [])

    def test_unknown_and_empty_left_and_reported(self):
        text, unfilled = assemble_letter([block("{hiring_manager} {location} {hiring_manager} {compny}")],
                                         job(location_text=None))
        self.assertEqual(text, "{hiring_manager} {location} {hiring_manager} {compny}")
        self.assertEqual(unfilled, ["hiring_manager", "location", "compny"])

    def test_stray_braces_untouched(self):
        text, unfilled = assemble_letter([block("Use {} and {0} and { x } and } { of {company}")], job())
        self.assertEqual(text, "Use {} and {0} and { x } and } { of Acme")
        self.assertEqual(unfilled, ["0"])

    def test_date_default_is_today(self):
        text, _ = assemble_letter([block("{date}")], job())
        self.assertEqual(text, f"{date.today().day} {date.today().strftime('%B %Y')}")

    def test_placeholders_in(self):
        self.assertEqual(placeholders_in("{a} {b} {a} {}"), ["a", "b"])


class PlainTextTest(unittest.TestCase):
    def test_invisible_removed(self):
        self.assertEqual(plain_text("﻿a​b‏c‮d­e\x07f"), "abcdef")

    def test_newlines_tabs(self):
        self.assertEqual(plain_text("a\r\nb\rc\td"), "a\nb\nc\td")
        self.assertEqual(plain_text("a\n\n\n\n\nb  \nc"), "a\n\nb\nc")

    def test_lookalikes(self):
        self.assertEqual(plain_text("“Hi” ‘x’ a–b—c−d wait… a b c • d"),
                         "\"Hi\" 'x' a-b-c-d wait... a b c - d")

    def test_transliteration(self):
        self.assertEqual(plain_text("café Müller Straße Æon Łódź"), "cafe Muller Strasse AEon Lodz")

    def test_dropped(self):
        self.assertEqual(plain_text("a{b}<c>+d=e~f \U0001F600 g"), "abcdef  g")
        self.assertEqual(plain_text("ok: (1) a_b-c|d \"q\" 'r' 50% #1 $2 a/b \\ *!?;,."),
                         "ok: (1) a_b-c|d \"q\" 'r' 50% #1 $2 a/b \\ *!?;,.")



LLM_ENV = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://x/v1", "LLM_MODEL": "m"}


class _Workspace:
    """A fresh workspace of the test account with one job (and a resume unless ``resume=False``), deleted after."""

    def __init__(self, c, resume=True):
        from backend.db import Document, Job, SessionLocal

        self.c = c
        self.id = c.post("/api/workspaces", json={"name": "Blocks"}).json()["id"]
        self.h = {"X-Workspace": str(self.id)}
        db = SessionLocal()
        j = Job(workspace_id=self.id, url=f"https://blocks.example/job/{self.id}", title="Program Manager",
                company="Acme", location_text="Sydney", description="Lead programs. Budgets required.",
                status="to_review", detail_status="full")
        db.add(j)
        if resume:
            db.add(Document(workspace_id=self.id, filename="cv.pdf", kind="resume",
                            text="Jane Doe. Ran a 2M budget at Foo."))
        db.commit()
        self.job_id = j.id
        db.close()

    def block(self, kind, title, text, tags=()):
        r = self.c.post("/api/letter-blocks", json={"kind": kind, "title": title, "text": text, "tags": list(tags)},
                        headers=self.h)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def close(self):
        self.c.delete(f"/api/workspaces/{self.id}")


class SuggestTest(unittest.TestCase):
    def suggest(self, c, ws, reply):
        from unittest import mock
        import backend.scorer as sc

        sent = {}

        def fake_call(messages, cfg, json_mode=True):
            sent.update(messages=messages, json_mode=json_mode, cfg=cfg)
            return reply
        with mock.patch.object(sc, "call_model", side_effect=fake_call), mock.patch.dict(os.environ, LLM_ENV):
            r = c.post(f"/api/jobs/{ws.job_id}/cover-letter/blocks/suggest", headers=ws.h)
        return r, sent

    def test_filtered_ordered_and_cleaned(self):
        import json

        with helpers.client() as c:
            ws, other = _Workspace(c), _Workspace(c)
            try:
                foreign = other.block("body", "Theirs", "not mine")
                o1 = ws.block("opening", "Hello", "Dear {company},")
                o2 = ws.block("opening", "Hello again", "Hi")
                b1 = ws.block("body", "Budget", "I ran a 2M budget.", ["budget", "finance"])
                b2 = ws.block("body", "SQL", "I write SQL.")
                b3 = ws.block("body", "Unused", "Something else.")
                cl = ws.block("closing", "Bye", "Kind regards")
                reply = json.dumps({"blocks": [
                    {"id": cl, "why": "closes"}, {"id": b2, "why": "“SQL” fits​"}, {"id": foreign, "why": "x"},
                    {"id": b2, "why": "again"}, "junk", {"id": str(b1)}, {"id": True}, {"id": o1, "why": "w" * 300},
                    {"id": b1}, {"id": o2, "why": "second opening"}],
                    "missing": [{"requirement": "no title"}, "junk",
                                {"title": "Stakeholders", "requirement": "Manage “stakeholders”",
                                 "bullets": ["Led a​ steering group of 12 – weekly", "", 5, "  "]}]})
                r, sent = self.suggest(c, ws, reply)
            finally:
                ws.close()
                other.close()
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertIs(sent["json_mode"], True)
        self.assertGreaterEqual(sent["cfg"]["timeout_s"], 300)
        prompt = sent["messages"][1]["content"]
        self.assertIn(f"[{b1}] (body) Budget - tags: budget, finance\nI ran a 2M budget.", prompt)
        self.assertNotIn("not mine", prompt)
        self.assertIn("=== JOB AD ===", prompt)
        self.assertIn("RESUME", prompt)
        self.assertEqual([p["id"] for p in out["picked"]], [o1, b2, b1, cl])
        self.assertEqual(out["picked"][0]["why"], "w" * 200)
        self.assertEqual(out["picked"][1]["why"], '"SQL" fits')
        self.assertEqual(out["picked"][2]["why"], "")
        self.assertEqual(out["unpicked"], [o2, b3])
        self.assertEqual(out["missing"], [{"title": "Stakeholders", "requirement": 'Manage "stakeholders"',
                                           "bullets": ["Led a steering group of 12 - weekly"]}])
        self.assertEqual([b["id"] for b in out["blocks"]], [o1, o2, b1, b2, b3, cl])
        self.assertEqual(out["blocks"][2]["tags"], ["budget", "finance"])

    def test_zero_blocks(self):
        import json

        with helpers.client() as c:
            ws = _Workspace(c)
            try:
                r, sent = self.suggest(c, ws, json.dumps({"blocks": [{"id": 1}], "missing": [
                    {"title": "Budgets", "requirement": "Budgets", "bullets": ["2M budget at Foo"]}]}))
            finally:
                ws.close()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["picked"], [])
        self.assertEqual(r.json()["unpicked"], [])
        self.assertEqual(r.json()["blocks"], [])
        self.assertEqual(r.json()["missing"][0]["bullets"], ["2M budget at Foo"])
        self.assertIn("=== BLOCKS ===\n(none yet)", sent["messages"][1]["content"])

    def test_fenced_answer_and_unusable_answers(self):
        with helpers.client() as c:
            ws = _Workspace(c)
            try:
                b = ws.block("body", "B", "Text")
                fenced, _ = self.suggest(c, ws, f'```json\n{{"blocks": [{{"id": {b}, "why": "ok"}},], "missing": []}}\n```')
                listed, _ = self.suggest(c, ws, f'[{{"id": {b}}}]')
                prose, _ = self.suggest(c, ws, "I can't decide.")
            finally:
                ws.close()
        self.assertEqual(fenced.status_code, 200, fenced.text)
        self.assertEqual(fenced.json()["picked"], [{"id": b, "why": "ok"}])
        for r in (listed, prose):
            self.assertEqual(r.status_code, 502)
            self.assertEqual(r.json()["detail"], "The model's answer was unusable; pick the blocks yourself.")

    def test_refusals(self):
        from unittest import mock
        import backend.scorer as sc

        with helpers.client() as c:
            ws, bare = _Workspace(c), _Workspace(c, resume=False)
            try:
                ws.block("body", "B", "Text")
                with mock.patch.object(sc, "call_model") as call, \
                        mock.patch.dict(os.environ, {"LLM_API_KEY": "", "LLM_BASE_URL": "https://x/v1", "LLM_MODEL": ""}):
                    no_llm = c.post(f"/api/jobs/{ws.job_id}/cover-letter/blocks/suggest", headers=ws.h)
                with mock.patch.object(sc, "call_model") as call2, mock.patch.dict(os.environ, LLM_ENV):
                    nothing = c.post(f"/api/jobs/{bare.job_id}/cover-letter/blocks/suggest", headers=bare.h)
                    foreign_job = c.post(f"/api/jobs/{ws.job_id}/cover-letter/blocks/suggest", headers=bare.h)
                with mock.patch.object(sc, "call_model", side_effect=sc.ScorerError("HTTP 500")), \
                        mock.patch.dict(os.environ, LLM_ENV):
                    failed = c.post(f"/api/jobs/{ws.job_id}/cover-letter/blocks/suggest", headers=ws.h)
            finally:
                ws.close()
                bare.close()
        self.assertEqual(no_llm.status_code, 400)
        self.assertEqual(nothing.status_code, 400)
        self.assertEqual(foreign_job.status_code, 404)
        self.assertEqual(failed.status_code, 502)
        call.assert_not_called()
        call2.assert_not_called()


class AssembleHttpTest(unittest.TestCase):
    def test_assemble_save_and_download(self):
        with helpers.client() as c:
            ws, other = _Workspace(c), _Workspace(c)
            try:
                foreign = other.block("body", "Theirs", "not mine")
                o = ws.block("opening", "O", "Dear {hiring_manager},")
                b = ws.block("body", "B", "I want to work at {company} as {role}.  “Mine” ​kept")
                cl = ws.block("closing", "C", "Kind regards")
                url = f"/api/jobs/{ws.job_id}/cover-letter/blocks/assemble"
                r = c.post(url, json={"block_ids": [o, b, o, cl]}, headers=ws.h)
                got = c.get(f"/api/jobs/{ws.job_id}/cover-letter", headers=ws.h).json()
                jobs = c.get("/api/jobs", headers=ws.h).json()
                docx = c.get(f"/api/jobs/{ws.job_id}/cover-letter.docx?ws={ws.id}")
                foreign_r = c.post(url, json={"block_ids": [b, foreign]}, headers=ws.h)
                empty = c.post(url, json={"block_ids": []}, headers=ws.h)
                too_many = c.post(url, json={"block_ids": list(range(41))}, headers=ws.h)
                after = c.get(f"/api/jobs/{ws.job_id}/cover-letter", headers=ws.h).json()
            finally:
                ws.close()
                other.close()
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["text"], "Dear {hiring_manager},\n\nI want to work at Acme as Program Manager.  "
                                      "“Mine” ​kept\n\nKind regards")  # the user's words exactly
        self.assertEqual(out["unfilled"], ["hiring_manager"])
        self.assertEqual(out["source"], "blocks")
        self.assertFalse(out["edited"])
        self.assertIsNone(out["model"])
        self.assertIsNone(out["instructions"])
        self.assertEqual(got["text"], out["text"])
        self.assertEqual(got["source"], "blocks")
        self.assertTrue([j for j in jobs if j["id"] == ws.job_id][0]["has_cover_letter"])
        self.assertEqual(docx.status_code, 200)
        self.assertEqual(docx.content[:2], b"PK")
        self.assertEqual(foreign_r.status_code, 404)
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(too_many.status_code, 422)
        self.assertEqual(after["text"], out["text"])  # the refused calls changed nothing


class DraftWithBlocksTest(unittest.TestCase):
    def test_prompt_rules_and_clean_draft(self):
        from unittest import mock
        from backend.cover_letters import SYSTEM_PROMPT, build_prompt
        from backend.db import Document, Job, SessionLocal
        import backend.scorer as sc

        self.assertIn("Write in the candidate's own voice", SYSTEM_PROMPT)
        self.assertIn("never introduce vocabulary they wouldn't use", SYSTEM_PROMPT)
        self.assertIn("When the candidate's own paragraphs are provided, build the letter from them", SYSTEM_PROMPT)
        self.assertNotIn("match their voice", SYSTEM_PROMPT)
        with helpers.client() as c:
            ws = _Workspace(c)
            try:
                db = SessionLocal()
                db.add(Document(workspace_id=ws.id, filename="My note", kind="other", filetype="note",
                                text="I like short sentences. " * 100))
                db.commit()
                without = build_prompt(db, db.get(Job, ws.job_id))
                ws.block("body", "Budget para", "I ran a 2M budget at {company}.")
                ws.block("opening", "Hi para", "Dear team,")
                db.expire_all()
                with_blocks = build_prompt(db, db.get(Job, ws.job_id))
                db.close()
                sent = {}

                def fake_call(messages, cfg, json_mode=True):
                    sent.update(messages=messages)
                    return "```\nDear Hiring Manager,\n\nI said “yes” – twice​.\n\nJane\n```"
                with mock.patch.object(sc, "call_model", side_effect=fake_call), mock.patch.dict(os.environ, LLM_ENV):
                    r = c.post(f"/api/jobs/{ws.job_id}/cover-letter", json={}, headers=ws.h)
            finally:
                ws.close()
        self.assertNotIn("CANDIDATE'S OWN PARAGRAPHS", without)
        self.assertIn("=== CANDIDATE'S OWN PARAGRAPHS ===\n[opening] Hi para\nDear team,\n\n[body] Budget para\n"
                      "I ran a 2M budget at {company}.", with_blocks)
        self.assertLess(with_blocks.index("RESUME"), with_blocks.index("OWN PARAGRAPHS"))
        self.assertLess(with_blocks.index("OWN PARAGRAPHS"), with_blocks.index("=== JOB AD ==="))
        style = "=== HOW THE CANDIDATE WRITES (style only): My note ===\n"
        self.assertIn(style, without)  # notes stand in for earlier cover letters
        self.assertEqual(len(without.split(style)[1].split("\n\n")[0]), 1500)  # the first 1500 characters
        self.assertIn("CANDIDATE'S OWN PARAGRAPHS", sent["messages"][1]["content"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["text"], "Dear Hiring Manager,\n\nI said \"yes\" - twice.\n\nJane")
        self.assertEqual(r.json()["source"], "ai")


if __name__ == "__main__":
    unittest.main()
