"""The data export (backend/export.py): a zip of folders of Markdown files, made in memory from the signed-in
account's own data."""
from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
import zipfile
from unittest import mock

from tests import helpers

from backend import crypto, docs, export
from backend.db import (
    ChatMessage,
    ChatThread,
    CompanyProfile,
    CompanySource,
    CoverLetter,
    DealbreakerSet,
    Document,
    FitResult,
    Job,
    Pin,
    SearchRun,
    SessionLocal,
    UserProfile,
)


class SafeNameTest(unittest.TestCase):
    def test_hostile_names_become_safe_names(self):
        for raw in ("../x", "..", "a/b", 'a<b>c:d"e\\f|g?h*i', "CON", "con.txt", "NUL", "COM1", "lpt9", "  .hidden. ",
                    "x" * 300, "tab\there\nnewline", "‮evil‬", "😀 Résumé", "é", ""):
            name = export.safe_name(raw, "Untitled", ".md")
            with self.subTest(raw=raw):
                self.assertLessEqual(len(name), 80)
                self.assertFalse(re.search(r'[<>:"/\\|?*\x00-\x1f]', name), name)
                self.assertFalse(name.startswith((".", " ")) or name.endswith((" ", ".")), name)
                self.assertNotIn(name.split(".")[0].upper(), export.RESERVED)
                self.assertTrue(name.endswith(".md"))
                self.assertEqual(name, __import__("unicodedata").normalize("NFC", name))
        self.assertEqual(export.safe_name("a/b"), "a-b")
        self.assertEqual(export.safe_name("😀 Résumé"), "😀 Résumé")

    def test_a_folder_never_hands_out_a_name_twice(self):
        folder = export.Folder()
        names = [folder.name("Resume", ext=".md") for _ in range(3)] + [folder.name("resume", ext=".md"), folder.name("x" * 300, ext=".md"),
                                                                         folder.name("x" * 300, ext=".md")]
        self.assertEqual(names[:3], ["Resume.md", "Resume (2).md", "Resume (3).md"])
        self.assertEqual(len({n.casefold() for n in names}), len(names))
        self.assertTrue(all(len(n) <= 80 for n in names))


class MarkdownTest(unittest.TestCase):
    def test_other_peoples_text_is_shown_as_text(self):
        hostile = '<script>alert(1)</script> <img src=x onerror=y> ![t](http://tracker/x.png) [a](javascript:alert(1)) &lt;b&gt;'
        out = export.text(hostile)
        self.assertNotIn("<", out)
        self.assertNotIn("![", out)
        self.assertNotIn("](", out)
        self.assertIn("&amp;lt;b&amp;gt;", out)  # an entity typed in the ad stays what was typed
        self.assertEqual(export.text("R&D at 5 < 6"), "R&D at 5 &lt; 6")
        self.assertEqual(export.cell("a | b\nc"), "a \\| b c")

    def test_only_http_addresses_become_links(self):
        self.assertEqual(export.link("Ad", "https://x.example/a(b)"), "[Ad](https://x.example/a%28b%29)")
        for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "ftp://x", "//x.example", "https://a b", ""):
            self.assertNotIn("](", export.link("Ad", bad), bad)

    def test_relative_links(self):
        self.assertEqual(export.rel_link("J", "Workspaces/W/Chats/c.md", "Workspaces/W/Jobs/Applied/A - B.md"),
                         "[J](../Jobs/Applied/A%20-%20B.md)")
        self.assertEqual(export.rel_link("C", "Workspaces/W/Jobs/Applied/a.md", "Companies/Acme.md"), "[C](../../../../Companies/Acme.md)")


class ExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        helpers.started()

    def setUp(self):
        self.bob = helpers.make_user(name="MARK-name-bob")
        self.alice = helpers.make_user(name="MARK-name-alice")
        self.upload = b"%PDF-1.4 MARK-upload-bytes \x00\xff\x10"
        with helpers.as_account(self.alice):
            self.plant(self.alice, "alice")
        with helpers.as_account(self.bob):
            self.plant(self.bob, "bob")
        self.main_id = self.ids["main"]

    def plant(self, who, tag):
        """Rows with a marker in every sealed field, in a workspace of the account's own."""
        db = SessionLocal()
        ws = who.workspace_id
        ids = {}
        try:
            db.add(UserProfile(id=ws, titles=[f"MARK-title-{tag}"], keywords_include=[f"MARK-inc-{tag}"], keywords_exclude=[f"MARK-exc-{tag}"],
                               salary_floor=123456, seek_locations=[f"MARK-seek-{tag}"], commute_from=[f"MARK-station-{tag}"]))
            db.add(DealbreakerSet(id=ws, industries=[f"MARK-industry-{tag}"], keywords=[f"MARK-dealbreaker-{tag}"]))
            db.add(Pin(workspace_id=ws, label=f"MARK-pin-{tag}", kind="home", lat=-33.5, lng=151.2, radius_km=10))
            db.add(CompanySource(workspace_id=ws, label=f"MARK-source-{tag}", careers_url=f"https://careers.example/{tag}",
                                 source_type="generic", enabled=True, last_error=f"MARK-sourceerror-{tag}"))
            stored = docs.save_upload(self.upload)
            db.add(Document(workspace_id=ws, filename="Resume.pdf", filetype=".pdf", stored_name=stored, kind="resume",
                            text=f"MARK-resume-{tag}", concise_text=f"MARK-concise-{tag}", use_concise=True))
            db.add(Document(workspace_id=ws, filename="Resume.pdf", filetype=".pdf", kind="other", text=f"MARK-second-{tag}"))
            db.add(Document(workspace_id=ws, filename="a/b", filetype="note", kind="other", text=f"MARK-note-{tag}"))
            main = Job(workspace_id=ws, url=f"https://ads.example/{tag}/main", title=f"Senior Analyst MARK-jobtitle-{tag}",
                       company="Acme Corp", description=f"MARK-description-{tag} <script>alert('x')</script> ![i](http://t.example/p.png)",
                       status="shortlisted", location_text=f"MARK-location-{tag}", salary_text="100k", work_mode="hybrid", office_days=2,
                       distance_km=12.5, office_text=f"MARK-office-{tag}", office_source="ad", detail_status="full")
            copy = Job(workspace_id=ws, url=f"https://other.example/{tag}/copy", title="Senior Analyst", company="Acme Corp", status="to_review",
                       description="the copy's own text MARK-copydescription", detail_status="full")
            hidden = Job(workspace_id=ws, url=f"https://ads.example/{tag}/hidden", title=f"Hidden job MARK-hidden-{tag}", company="Hidco",
                         status="to_review", hidden=True, detail_status="full", description="hidden text")
            filtered = Job(workspace_id=ws, url=f"https://ads.example/{tag}/filtered", title=f"Filtered job MARK-filtered-{tag}", company="Filtco",
                           status="to_review", excluded_reason=f"MARK-reason-{tag}", detail_status="full", description="MARK-filtereddescription")
            weird = Job(workspace_id=ws, url=f"https://ads.example/{tag}/weird", title="../x", company="CON", status="applied", detail_status="full")
            long_title = Job(workspace_id=ws, url=f"https://ads.example/{tag}/long", title="L" * 300, company="😀 Co", status="applied", detail_status="full")
            same = [Job(workspace_id=ws, url=f"https://ads.example/{tag}/same{i}", title="Same", company="Same", status="interviewing",
                        detail_status="full") for i in range(2)]
            db.add_all([main, copy, hidden, filtered, weird, long_title, *same])
            db.flush()
            copy.duplicate_of = main.id
            db.add(FitResult(job_id=main.id, status="ok", score=87, evidence_json=json.dumps({
                "summary": f"MARK-fitsummary-{tag}", "gaps": [f"MARK-gap-{tag}"],
                "requirements": [{"point": f"MARK-req-{tag}", "must_have": True, "matched": True,
                                  "evidence": [{"bullet": f"MARK-evidence-{tag}", "sub_bullets": [f"MARK-sub-{tag}"]}]}]})))
            db.add(CoverLetter(job_id=main.id, text=f"MARK-letter-{tag}", instructions=f"MARK-instructions-{tag}"))
            thread = ChatThread(workspace_id=ws, job_ids_json=json.dumps([main.id, copy.id, filtered.id]), title=f"MARK-chat-{tag}")
            db.add(thread)
            db.flush()
            db.add(ChatMessage(thread_id=thread.id, role="user", content=f"MARK-question-{tag}"))
            db.add(ChatMessage(thread_id=thread.id, role="assistant", content=f"MARK-answer-{tag} <b>x</b>",
                               sources_json=json.dumps([{"title": "T", "url": "https://src.example/p"}, {"title": "Bad", "url": "javascript:x"}])))
            db.add(SearchRun(workspace_id=ws, kind="search", state="done", summary_json=json.dumps(
                {"search": {"totals": {"new": 7}, "per_source": [{"label": f"MARK-runsource-{tag}", "listed": 3, "new": 2, "kept": 1}]}})))
            if db.get(CompanyProfile, "acme corp") is None:
                db.add(CompanyProfile(key="acme corp", name="Acme Corp", status="done", business_model="MARK-company-business"))
            db.commit()
            ids["main"] = main.id
        finally:
            db.close()
        self.ids = ids

    def export(self, who=None, **kwargs):
        c = helpers.client((who or self.bob).email)
        with c:
            r = c.post("/api/account/export", **kwargs)
        return r

    def open_zip(self, r):
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.headers["content-type"], "application/zip")
        self.assertRegex(r.headers["content-disposition"], r'^attachment; filename="JobHuntPA-export-\d{4}-\d\d-\d\d\.zip"$')
        return zipfile.ZipFile(io.BytesIO(r.content))

    def texts(self, z):
        return {n: z.read(n).decode("utf-8") for n in z.namelist() if n.endswith(".md")}

    def test_folders_names_and_every_sealed_field_comes_out_readable(self):
        z = self.open_zip(self.export())
        names = z.namelist()
        root = names[0].split("/")[0]
        self.assertRegex(root, r"^JobHuntPA export \d{4}-\d\d-\d\d$")
        self.assertTrue(all(n.startswith(root + "/") for n in names))
        rel = {n[len(root) + 1:] for n in names}
        ws = "Workspaces/My job search"
        for expected in ("README.md", "Account.md", f"{ws}/Search setup.md", f"{ws}/Sources.md", f"{ws}/Search history.md", f"{ws}/Jobs/Jobs.md",
                         f"{ws}/Jobs/Filtered out.md", f"{ws}/Documents/Resume.md", f"{ws}/Documents/Resume (2).md",
                         f"{ws}/Documents/a-b.md", f"{ws}/Documents/Original files/Resume.pdf", "Companies/Acme Corp.md",
                         f"{ws}/Jobs/Shortlisted/Acme Corp - Senior Analyst MARK-jobtitle-bob.md",
                         f"{ws}/Jobs/Shortlisted/Acme Corp - Senior Analyst MARK-jobtitle-bob - Cover letter.md"):
            self.assertIn(expected, rel)
        self.assertTrue(any(p.startswith(f"{ws}/Chats/") and "MARK-chat-bob" in p for p in rel))
        self.assertTrue(any(p.startswith(f"{ws}/Jobs/Hidden/") for p in rel))
        self.assertTrue(any(p.startswith(f"{ws}/Jobs/Applied/") for p in rel))
        everything = "\n".join(self.texts(z).values())
        for marker in ("name-bob", "title-bob", "inc-bob", "exc-bob", "seek-bob", "station-bob", "industry-bob", "dealbreaker-bob", "pin-bob",
                       "source-bob", "sourceerror-bob", "resume-bob", "concise-bob", "second-bob", "note-bob", "jobtitle-bob", "description-bob",
                       "location-bob", "office-bob", "fitsummary-bob", "gap-bob", "req-bob", "evidence-bob", "sub-bob", "letter-bob",
                       "instructions-bob", "chat-bob", "question-bob", "answer-bob", "runsource-bob", "hidden-bob", "filtered-bob", "reason-bob",
                       "company-business"):
            self.assertIn(f"MARK-{marker}", everything, marker)
        self.assertIn("bob", z.read(f"{root}/Account.md").decode())
        self.assertIn(self.bob.email, z.read(f"{root}/Account.md").decode())

    def test_another_persons_data_never_appears(self):
        z = self.open_zip(self.export())
        blob = b"".join(z.read(n) for n in z.namelist()) + "\n".join(z.namelist()).encode()
        self.assertNotIn(b"-alice", blob)
        self.assertNotIn(self.alice.email.encode(), blob)
        mine = self.open_zip(self.export(self.alice))
        self.assertNotIn(b"MARK-resume-bob", b"".join(mine.read(n) for n in mine.namelist()))

    def test_merged_copies_excluded_and_hidden_jobs(self):
        z = self.open_zip(self.export())
        root = z.namelist()[0].split("/")[0]
        ws = f"{root}/Workspaces/My job search/Jobs"
        main = z.read(f"{ws}/Shortlisted/Acme Corp - Senior Analyst MARK-jobtitle-bob.md").decode()
        self.assertIn("## Also advertised at", main)
        self.assertIn("https://other.example/bob/copy", main)
        names = "\n".join(z.namelist())
        self.assertNotIn("MARK-copydescription", "\n".join(self.texts(z).values()))  # a copy has no file of its own
        self.assertNotIn("MARK-filtered-bob.md", names)  # a job the filters left out: only in Filtered out.md
        filtered = z.read(f"{ws}/Filtered out.md").decode()
        self.assertIn("MARK-filtered-bob", filtered)
        self.assertIn("MARK-reason-bob", filtered)
        self.assertNotIn("MARK-filtereddescription", "\n".join(self.texts(z).values()))
        hidden = [n for n in z.namelist() if "/Jobs/Hidden/" in n]
        self.assertEqual(len(hidden), 1)
        self.assertIn("MARK-hidden-bob", hidden[0])
        index = z.read(f"{ws}/Jobs.md").decode()
        self.assertIn("MARK-jobtitle-bob", index)
        self.assertNotIn("MARK-filtered-bob", index)

    def test_hostile_names_are_safe_and_unique(self):
        z = self.open_zip(self.export())
        names = z.namelist()
        seen = set()
        for n in names:
            parts = n.split("/")
            self.assertNotIn("..", parts)
            self.assertFalse(n.startswith("/") or "\\" in n, n)
            for part in parts:
                self.assertLessEqual(len(part), 80, part)
                self.assertNotRegex(part, r'[<>:"|?*\x00-\x1f]')
                self.assertNotIn(part.split(".")[0].upper(), export.RESERVED, part)
            self.assertNotIn(n.casefold(), seen)
            seen.add(n.casefold())
        applied = [n for n in names if "/Jobs/Applied/" in n]
        self.assertEqual(len(applied), 2)  # "CON - ../x" and the long emoji one
        self.assertTrue(any("😀 Co" in n for n in applied))
        interviewing = [n for n in names if "/Jobs/Interviewing/" in n]
        self.assertEqual(sorted(p.rsplit("/", 1)[1] for p in interviewing), ["Same - Same (2).md", "Same - Same.md"])

    def test_html_in_ads_and_answers_is_escaped(self):
        z = self.open_zip(self.export())
        everything = "\n".join(self.texts(z).values())
        self.assertNotIn("<script", everything)
        self.assertIn("&lt;script>", everything)
        self.assertNotIn("<b>", everything)
        self.assertNotIn("![", everything)
        self.assertNotIn("javascript:x", everything)  # not linked
        self.assertIn("(https://src.example/p)", everything)
        for body in self.texts(z).values():
            self.assertNotRegex(body, r"<[a-zA-Z/!]")

    def test_links_between_files_are_relative_and_point_at_files_in_the_zip(self):
        z = self.open_zip(self.export())
        names = set(z.namelist())
        root = z.namelist()[0].split("/")[0]
        from urllib.parse import unquote
        import posixpath
        checked = 0
        for name, body in self.texts(z).items():
            for target in re.findall(r"\]\(([^)]+)\)", body):
                if target.startswith("http"):
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(target)))
                self.assertIn(resolved, names, f"{name} links to {target}")
                checked += 1
        self.assertGreater(checked, 5)
        chat = next(b for n, b in self.texts(z).items() if "/Chats/" in n)
        self.assertIn("Jobs/Shortlisted/", chat)  # the chat links the job's file (the merged copy points to its main job)

    def test_an_upload_comes_back_byte_for_byte(self):
        z = self.open_zip(self.export())
        original = next(n for n in z.namelist() if "/Original files/" in n)
        self.assertEqual(z.read(original), self.upload)

    def test_the_other_workspaces_are_exported_and_the_others_left_alone(self):
        with helpers.as_account(self.bob):
            other = helpers.new_workspace("CON", owner=self.bob.id)
            db = SessionLocal()
            db.add(Job(workspace_id=other, url="https://ads.example/bob/other", title="Other ws job", company="X", status="applied", detail_status="full"))
            db.commit()
            db.close()
        z = self.open_zip(self.export())
        folders = {n.split("/")[2] for n in z.namelist() if n.split("/")[1] == "Workspaces"}
        self.assertEqual(folders, {"My job search", "_CON"})

    def test_it_needs_the_password_recently_the_csrf_token_and_is_one_at_a_time(self):
        from sqlalchemy import text as sql
        from backend.db import engine, utcnow
        from datetime import timedelta
        c = helpers.client(self.bob.email)
        with c:
            with engine.begin() as conn:
                conn.execute(sql("UPDATE sessions SET reauth_at = :t WHERE user_id = :u"), {"t": utcnow() - timedelta(minutes=30), "u": self.bob.user_id if hasattr(self.bob, "user_id") else self.bob.id})
            r = c.post("/api/account/export")
            self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        # no CSRF token
        with helpers.client(self.bob.email) as c2:
            token = c2.headers.pop("X-CSRF-Token", None)
            r = c2.post("/api/account/export")
            self.assertEqual(r.status_code, 403, r.text)
            self.assertNotEqual(r.headers.get("content-type"), "application/zip")
            self.assertIn(c2.get("/api/account/export").status_code, (404, 405))  # a link or a prefetch can't start one
        # a second export while one is being built
        export._running.add(self.bob.id)
        try:
            r = self.export()
            self.assertEqual((r.status_code, r.json()["code"]), (429, "export_running"))
        finally:
            export._running.discard(self.bob.id)
        self.assertEqual(self.export().status_code, 200)  # and afterwards it works again
        with mock.patch.object(export, "build", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.export()
        self.assertNotIn(self.bob.id, export._running, "a failed export doesn't block the next one")

    def test_the_whole_app_makes_only_a_few_exports_at_once(self):
        taken = [export._slots.acquire(blocking=False) for _ in range(export.MAX_AT_ONCE)]
        try:
            self.assertTrue(all(taken))
            r = self.export()
            self.assertEqual((r.status_code, r.json()["code"]), (503, "export_busy"))
            self.assertNotIn(self.bob.id, export._running, "a refused export doesn't block the next one")
        finally:
            for _ in taken:
                export._slots.release()
        self.assertEqual(self.export().status_code, 200)

    def test_a_zip_over_the_size_limit_is_refused_before_it_is_built(self):
        with mock.patch.object(export, "MAX_EXPORT_BYTES", 10), mock.patch.object(docs, "read_upload") as read:
            r = self.export()
        self.assertEqual((r.status_code, r.json()["code"]), (413, "export_too_large"))
        read.assert_not_called()  # the stored uploads' sizes were enough to know
        self.assertNotIn(self.bob.id, export._running)

    def test_the_running_total_stops_a_zip_that_grows_past_the_limit(self):
        arc = export.Archive("root", __import__("datetime").datetime(2026, 1, 1))
        with mock.patch.object(export, "MAX_EXPORT_BYTES", 100):
            arc.add("a.md", "x" * 60)
            with self.assertRaises(export.AuthProblem) as caught:
                arc.add("b.md", b"y" * 60)
        self.assertEqual((caught.exception.status, caught.exception.code), (413, "export_too_large"))

    def test_signed_out_is_refused(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        with TestClient(app) as c:
            self.assertEqual(c.post("/api/account/export").status_code, 401)

    def test_nothing_is_written_to_disk_while_exporting_and_logs_hold_no_content(self):
        data_dir = os.environ["JOBHUNT_DATA_DIR"]
        before = {os.path.join(d, f) for d, _s, files in os.walk(data_dir) for f in files}
        with mock.patch.object(tempfile, "SpooledTemporaryFile", side_effect=AssertionError("no spooled files")), \
                mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=AssertionError("no temporary files")), \
                mock.patch.object(tempfile, "TemporaryFile", side_effect=AssertionError("no temporary files")), \
                self.assertLogs("backend.export", level="INFO") as logs:
            r = self.export()
        self.assertEqual(r.status_code, 200)
        after = {os.path.join(d, f) for d, _s, files in os.walk(data_dir) for f in files}
        self.assertEqual({p for p in after - before if not p.endswith(("-wal", "-shm", ".db-journal"))}, set())
        logged = "\n".join(logs.output)
        self.assertRegex(logged, r"user \d+ exported their data: \d+ files, \d+ bytes")
        self.assertNotIn("MARK", logged)
        self.assertNotIn(self.bob.email, logged)

    def test_the_export_source_never_writes_files(self):
        with open(export.__file__, encoding="utf-8") as f:
            src = f.read()
        for forbidden in ("tempfile", "open(", "write_bytes", "write_text", "SpooledTemporaryFile", "mkdtemp"):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
