"""Encryption of user data (accounts design, Phase D): sealed columns and uploads, the one-time migration of an
existing install, strict mode, the SQL guard, cross-user isolation and the job-list benchmark.

The migration needs a database from before encryption and fresh processes (it runs once, and a crash is part of
the test), so those cases run in subprocesses on throwaway folders (``helpers.scenario``). Everything else uses
the shared test database, which the admin from tests/__init__.py encrypted when it was created.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from sqlalchemy import func, select, text  # noqa: E402

from backend import crypto, db as db_module, migrate_encryption  # noqa: E402
from backend.db import (CommuteCache, Document, FitResult, HttpCache, Job, Pin, SessionLocal, Workspace,  # noqa: E402
                        engine)

ROOT = Path(__file__).resolve().parent.parent
MARK = re.compile(rb"qzxmark", re.I)  # in every value the migration scenarios plant
PASSWORD = "correct horse battery"


def tearDownModule():
    helpers.remove_scratch_folders()


def marked_files(folder: str) -> list[str]:
    """Files under ``folder`` whose raw bytes hold a planted marker."""
    found = []
    for path in sorted(Path(folder).rglob("*")):
        if path.is_file() and MARK.search(path.read_bytes()):
            found.append(str(path.relative_to(folder)))
    return found


# A database from before encryption: written with raw SQL, as the earlier version left it (and the shared pool's
# migration already done). Every personal value holds the marker "QZXMARK"; postings are public and don't.
LEGACY = '''
import json
from pathlib import Path
from sqlalchemy import text
from backend.db import DATA_DIR, DB_PATH, UPLOAD_DIR, engine, init_db

M = "QZXMARK"
init_db()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
(UPLOAD_DIR / "a1b2c3.txt").write_text(f"My CV, {M}-UPLOAD-FILE")
(DATA_DIR / "cache").mkdir(exist_ok=True)
(DATA_DIR / "cache" / "old.html").write_text(f"<p>{M}-OLD-CACHE</p>")
Path(f"{DB_PATH}.bak-before-job-pool").write_text(f"{M}-OLD-BACKUP")
ts = "2026-09-01 10:00:00.000000"
with engine.begin() as c:
    x = lambda sql, **p: c.execute(text(sql), p)
    x("UPDATE workspaces SET name = :n WHERE id = 1", n=f"{M}-WS-NAME")
    x("INSERT INTO user_profile (id, titles, keywords_include, keywords_exclude, salary_floor, remote_aus_ok, "
      "remote_global_ok, allow_hybrid, max_office_days, allow_onsite, seek_enabled, seek_locations, seek_max_pages, "
      "commute_from, commute_arrive_by, commute_leave_at) VALUES (1, :t, '[]', :kx, 120000, 1, 0, 1, 3, 1, 1, :loc, "
      "1, :cf, '08:30', '17:30')", t=json.dumps([f"{M}-TITLE"]), kx=json.dumps([f"{M}-EXCLUDE"]),
      loc=json.dumps([f"{M}-LOCATION"]), cf=json.dumps([f"{M}-STATION"]))
    x("INSERT INTO dealbreakers (id, industries, keywords) VALUES (1, :i, '[]')", i=json.dumps([f"{M}-INDUSTRY"]))
    x("INSERT INTO pins (workspace_id, label, kind, lat, lng, radius_km) VALUES (1, :l, 'home', -33.87, 151.21, 25.0)",
      l=f"{M}-PIN")
    x("INSERT INTO company_sources (workspace_id, label, careers_url, source_type, adapter_key, enabled, last_count, "
      "last_error, company, url, board, host, tenant, site, base_url) VALUES (1, :l, :u, 'auto', '', 1, 3, :e, :co, "
      ":u, '', '', '', '', '')", l=f"{M}-SOURCE-LABEL", u="https://qzxmark-source.example/careers",
      e=f"{M}-SOURCE-ERROR", co=f"{M}-LEGACY-COLUMN")
    x("INSERT INTO documents (workspace_id, filename, filetype, stored_name, text, kind, use_for_scoring, created_at, "
      "concise_text, use_concise) VALUES (1, :f, '.txt', 'a1b2c3.txt', :t, 'resume', 1, :ts, :c, 0)",
      f=f"{M}-CV.txt", t=f"My CV, {M}-DOC-TEXT", ts=ts, c=f"{M}-CONCISE")
    x("INSERT INTO documents (workspace_id, filename, filetype, stored_name, text, kind, use_for_scoring, created_at, "
      "use_concise) VALUES (1, :f, '.pdf', 'gone99.pdf', :t, 'other', 1, :ts, 0)", f=f"{M}-MISSING.pdf",
      t=f"{M}-MISSING-TEXT", ts=ts)
    x("INSERT INTO postings (url, source, company, title, location_text, country, work_mode, description, "
      "detail_status, not_a_job, first_seen, last_listed_at, rev) VALUES ('https://jobs.example/public/1', 'lever:pub', "
      "'Pubco', 'Public role', 'Sydney NSW', 'AU', 'hybrid', 'A public ad.', 'full', 0, :ts, :ts, 1)", ts=ts)
    pid = x("SELECT id FROM postings WHERE url = 'https://jobs.example/public/1'").scalar()
    x("INSERT INTO jobs (workspace_id, url, posting_id, origin, posting_rev, status, hidden, excluded_reason, "
      "first_seen, last_seen, lat, lng, distance_km, office_text, office_source, location_verdict, detail_status, "
      "description) VALUES (1, 'https://jobs.example/public/1', :p, 'pool', 1, 'applied', 1, :r, :ts, :ts, -33.8, "
      "151.2, 4.2, :o, 'user', 'ok', 'full', :d)", p=pid, r=f"{M}-EXCLUDED", ts=ts, o=f"{M}-OFFICE",
      d=f"{M}-PRIVATE-FULL-AD")
    link = x("SELECT MAX(id) FROM jobs").scalar()
    x("INSERT INTO jobs (workspace_id, url, origin, status, hidden, title, company, description, detail_status, "
      "first_seen, last_seen, source) VALUES (1, 'https://qzxmark-private.example/1', 'private', 'to_review', 0, :t, "
      ":co, :d, 'full', :ts, :ts, 'import')", t=f"{M}-PRIVATE-TITLE", co=f"{M}-PRIVATE-CO", d=f"{M}-PRIVATE-DESC",
      ts=ts)
    private = x("SELECT MAX(id) FROM jobs").scalar()
    x("INSERT INTO fit_results (job_id, status, score, evidence_json, profile_hash, model, created_at) VALUES "
      "(:j, 'ok', 87, :e, 'h', 'm', :ts)", j=link, e=json.dumps({"summary": f"{M}-FIT-SUMMARY"}), ts=ts)
    x("INSERT INTO cover_letters (job_id, text, instructions, edited, created_at, updated_at) VALUES "
      "(:j, :t, :i, 0, :ts, :ts)", j=link, t=f"Dear {M}-LETTER", i=f"{M}-INSTRUCTIONS", ts=ts)
    x("INSERT INTO chat_threads (workspace_id, job_ids_json, title, created_at, updated_at) VALUES "
      "(1, :j, :t, :ts, :ts)", j=json.dumps([link]), t=f"{M}-CHAT-TITLE", ts=ts)
    thread = x("SELECT MAX(id) FROM chat_threads").scalar()
    x("INSERT INTO chat_messages (thread_id, role, content, sources_json, web_search, created_at) VALUES "
      "(:t, 'user', :c, :s, 0, :ts)", t=thread, c=f"{M}-CHAT-MESSAGE",
      s=json.dumps([{"title": f"{M}-CHAT-SOURCE", "url": "https://x.example"}]), ts=ts)
    x("INSERT INTO search_runs (workspace_id, kind, scheduled, state, stage, progress_json, summary_json, error, "
      "started_at) VALUES (1, 'search', 0, 'done', 'done', '{}', :s, :e, :ts)",
      s=json.dumps({"search": {"note": f"{M}-RUN-SUMMARY"}}), e=f"{M}-RUN-ERROR", ts=ts)
    x("INSERT INTO job_duplicates (workspace_id, job_a, job_b, status, score, reasons_json, created_at) VALUES "
      "(1, :a, :b, 'suggested', 70, :r, :ts)", a=link, b=private, r=json.dumps([f"{M}-DUP-REASON"]), ts=ts)
    x("INSERT INTO search_profile (id, cv_text, wishes_text, home_location, lat, lng, radius_km, work_modes, "
      "updated_at) VALUES (1, :cv, :w, :h, -33.9, 151.1, 20, 'hybrid', :ts)", cv=f"{M}-OLD-CV", w=f"{M}-WISHES",
      h=f"{M}-HOME", ts=ts)
    x("INSERT INTO commute_cache (key, result_json, fetched_at) VALUES (:k, :r, :ts)", k="stop|qzxmark-station",
      r=json.dumps({"name": f"{M}-COMMUTE"}), ts=ts)
    x("INSERT INTO http_cache (url, status_code, body, fetched_at) VALUES (:u, 200, :b, :ts)",
      u="https://www.seek.com.au/qzxmark-search", b=f"<html>{M}-HTTP-BODY</html>", ts=ts)
out["planted"] = {"link": link, "private": private}
'''

# The admin is made the way the first run makes them; the run is then started by hand (and made to crash).
MAKE_ADMIN = '''
from backend import users
users.startup()
account = users.create_first_admin("owner@example.com", "correct horse battery", name="Pat Owner")
out["user_id"] = account.user_id
'''

RESTART = '''
from backend import migrate_encryption, users
from backend.db import SessionLocal, init_db
init_db()
users.startup()
migrate_encryption.at_startup()
out["state_after_restart"] = migrate_encryption.state().get("state")
db = SessionLocal()
user = users.find_by_email(db, "owner@example.com")
dek = users.unlock_with_password(db, user, "correct horse battery")
db.close()
'''

# Read everything back through the API, signed in as the owner.
READ_BACK = '''
from fastapi.testclient import TestClient
from backend.app import app
with TestClient(app) as c:
    r = c.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"})
    out["login"] = r.status_code
    csrf = r.json()["csrf"]
    out["api"] = {
        "workspaces": c.get("/api/workspaces").json(),
        "jobs": c.get("/api/jobs?include=excluded,hidden").json(),
        "profile": c.get("/api/profile").json(),
        "dealbreakers": c.get("/api/dealbreakers").json(),
        "pins": c.get("/api/pins").json(),
        "sources": c.get("/api/sources").json(),
        "documents": c.get("/api/documents").json(),
        "chats": c.get("/api/chats").json(),
        "latest_run": c.get("/api/runs/latest").json(),
        "duplicates": c.get("/api/duplicates").json(),
    }
    docs = {d["filename"]: d["id"] for d in out["api"]["documents"]}
    out["api"]["doc_text"] = c.get(f"/api/documents/{docs['QZXMARK-CV.txt']}/text").json()["text"]
    job_ids = {j["url"]: j["id"] for j in out["api"]["jobs"]}
    out["api"]["letter"] = c.get(f"/api/jobs/{job_ids['https://jobs.example/public/1']}/cover-letter").json()
    out["status"] = c.get("/api/encryption/status").json()
    out["delete_without_csrf"] = c.post("/api/encryption/leftovers/delete").status_code
    deleted = c.post("/api/encryption/leftovers/delete", headers={"X-CSRF-Token": csrf})
    out["deleted"] = deleted.json()
'''


class MigrationScenarioTest(unittest.TestCase):
    """An install from before encryption: interrupted twice (once while sealing uploads, once in the tables),
    then finished; nothing it planted is left readable."""

    @classmethod
    def setUpClass(cls):
        folder = helpers.scratch_folder()
        cls.folder = folder
        # 1. Crash while sealing the uploads: the sealed copy is written, the document not yet pointed at it.
        cls.first = helpers.scenario(LEGACY + MAKE_ADMIN + '''
from backend import migrate_encryption
out["pending"] = migrate_encryption.state()
migrate_encryption._point_document_at = lambda *a, **k: os._exit(3)
migrate_encryption.run(account.user_id, account.dek)
''', folder, returncode=3)
        cls.after_crash_1 = sorted(p.name for p in (Path(folder) / "uploads").iterdir())
        # 2. Restart; crash right after the third batch of rows (two rows each) is committed.
        cls.second = helpers.scenario(RESTART + '''
migrate_encryption.BATCH_ROWS = 2
real, calls = migrate_encryption._seal_batch, []
def flaky(*a, **k):
    n = real(*a, **k)
    if n:
        calls.append(n)
    if len(calls) == 3:
        os._exit(4)
    return n
migrate_encryption._seal_batch = flaky
migrate_encryption.run(user.id, dek)
''', folder, returncode=4)
        cls.after_crash_2 = sorted(p.name for p in (Path(folder) / "uploads").iterdir())
        cls.between = helpers.scenario('''
from sqlalchemy import text
from backend import migrate_encryption
from backend.db import engine
out["state"] = migrate_encryption.state()
out["audit"] = migrate_encryption.plaintext_audit()
with engine.connect() as conn:
    pending = {t.name: conn.execute(text(f"SELECT COUNT(*) FROM {t.name} WHERE "
                                         f"{migrate_encryption._pending_where(t, cols)}")).scalar()
               for t, cols in migrate_encryption.sealed_tables()}
out["pending_rows"] = sum(pending.values())
''', folder)
        # 3. Restart and finish; then the audit, the leftovers and every planted value read back through the API.
        cls.final = helpers.scenario(RESTART + '''
final = migrate_encryption.run(user.id, dek)
out["final"] = final
out["again"] = migrate_encryption.run(user.id, dek)["finished_at"] == final["finished_at"]  # a no-op once done
out["audit"] = migrate_encryption.plaintext_audit()
out["leftovers"] = [{k: v for k, v in i.items() if not k.startswith("_")} for i in migrate_encryption.leftover_files()]
out["strict"] = __import__("backend.crypto").crypto.strict()
from backend.db import engine
from sqlalchemy import text
with engine.connect() as conn:
    out["stored_names"] = [r[0] for r in conn.execute(text("SELECT stored_name FROM documents ORDER BY id"))]
    out["pool_link_url"] = [list(r) for r in conn.execute(text(
        "SELECT url, typeof(url_index) FROM jobs WHERE posting_id IS NOT NULL"))]
    out["source_index"] = [list(r) for r in conn.execute(text(
        "SELECT typeof(careers_url_index), typeof(company) FROM company_sources"))]
    out["caches"] = [conn.execute(text("SELECT COUNT(*) FROM commute_cache")).scalar(),
                     conn.execute(text("SELECT COUNT(*) FROM http_cache")).scalar()]
''', folder)
        cls.marked_before_delete = marked_files(folder)
        cls.read = helpers.scenario(READ_BACK, folder)
        cls.marked_after_delete = marked_files(folder)

    def test_a_crash_while_sealing_uploads_leaves_both_copies(self):
        self.assertEqual(self.first["_returncode"], 3)
        self.assertIn("a1b2c3.txt", self.after_crash_1)      # the original is kept until the document points away
        self.assertIn("a1b2c3.sealed", self.after_crash_1)   # its sealed copy, verified, is already there
        self.assertNotIn("a1b2c3.txt", self.after_crash_2)   # the next run pointed the document at it, then deleted it

    def test_an_interrupted_run_resumes_where_it_stopped(self):
        self.assertEqual(self.second["_returncode"], 4)
        self.assertEqual(self.between["state"]["state"], "running")  # (what a crash leaves; start-up resets it)
        self.assertTrue(self.between["audit"]["columns"])            # rows still plaintext ...
        sealed_by_then = self.final["final"]["report"]["rows"]       # ... and the last run sealed only those
        self.assertEqual(sum(sealed_by_then.values()), self.between["pending_rows"])
        self.assertEqual(self.final["state_after_restart"], "pending")
        self.assertEqual(self.final["final"]["state"], "done")
        self.assertTrue(self.final["again"])
        self.assertTrue(self.final["strict"])

    def test_the_plaintext_audit_is_zero(self):
        self.assertEqual(self.final["audit"], {"columns": {}, "files": []})
        report = self.final["final"]["report"]
        self.assertEqual(report["vacuum"], "done")
        self.assertEqual(report["backup"], "jobhunt.db.bak-before-encryption")

    def test_uploads_are_sealed_and_a_missing_file_is_noted(self):
        self.assertEqual(self.final["stored_names"], ["a1b2c3.sealed", None])
        self.assertEqual(sorted(p.name for p in (Path(self.folder) / "uploads").iterdir()), ["a1b2c3.sealed"])

    def test_links_indexes_and_caches(self):
        self.assertEqual(self.final["pool_link_url"], [[None, "blob"]])  # the posting has the URL
        self.assertEqual(self.final["source_index"], [["blob", "blob"]])
        self.assertEqual(self.final["caches"], [0, 0])  # the old shared caches are gone

    def test_no_planted_marker_is_left_in_the_database_its_wal_or_the_uploads(self):
        # Before the leftovers are deleted, only they hold markers: the backups and the old page cache.
        self.assertEqual(sorted(self.marked_before_delete),
                         ["cache/old.html", "jobhunt.db.bak-before-encryption", "jobhunt.db.bak-before-job-pool"])
        self.assertEqual(sorted(i["path"] for i in self.final["leftovers"]),
                         ["cache/old.html", "jobhunt.db.bak-before-encryption", "jobhunt.db.bak-before-job-pool"])
        # After "Delete them now", no file in the data folder holds one (database, WAL, uploads, key file...).
        self.assertEqual(self.marked_after_delete, [])
        self.assertEqual(sorted(self.read["deleted"]["deleted"]),
                         ["cache/old.html", "jobhunt.db.bak-before-encryption", "jobhunt.db.bak-before-job-pool"])
        self.assertEqual(self.read["deleted"]["leftovers"], [])
        self.assertEqual(self.read["delete_without_csrf"], 403)

    def test_everything_reads_back_through_the_api(self):
        api = self.read["api"]
        self.assertEqual(self.read["login"], 200)
        self.assertEqual([w["name"] for w in api["workspaces"]], ["QZXMARK-WS-NAME"])
        jobs = {j["url"]: j for j in api["jobs"]}
        link, private = jobs["https://jobs.example/public/1"], jobs["https://qzxmark-private.example/1"]
        self.assertEqual((link["status"], link["hidden"], link["excluded_reason"], link["office_text"],
                          link["location_verdict"], link["fit"]["summary"], link["title"]),
                         ("applied", True, "QZXMARK-EXCLUDED", "QZXMARK-OFFICE", "ok", "QZXMARK-FIT-SUMMARY",
                          "Public role"))
        self.assertEqual((private["title"], private["company"], private["hidden"], private["excluded_reason"]),
                         ("QZXMARK-PRIVATE-TITLE", "QZXMARK-PRIVATE-CO", False, None))
        self.assertEqual(api["profile"]["titles"], ["QZXMARK-TITLE"])
        self.assertEqual((api["profile"]["salary_floor"], api["profile"]["commute_arrive_by"]), (120000, "08:30"))
        self.assertEqual(api["dealbreakers"]["industries"], ["QZXMARK-INDUSTRY"])
        self.assertEqual(api["pins"][0]["label"], "QZXMARK-PIN")
        self.assertEqual((api["sources"][0]["label"], api["sources"][0]["url"]),
                         ("QZXMARK-SOURCE-LABEL", "https://qzxmark-source.example/careers"))
        self.assertEqual(api["doc_text"], "My CV, QZXMARK-DOC-TEXT")
        self.assertEqual(api["chats"][0]["title"], "QZXMARK-CHAT-TITLE")
        self.assertEqual(api["latest_run"]["summary"]["search"]["note"], "QZXMARK-RUN-SUMMARY")
        self.assertEqual(api["latest_run"]["error"], "QZXMARK-RUN-ERROR")
        self.assertEqual(api["duplicates"]["suggested"][0]["reasons"], ["QZXMARK-DUP-REASON"])
        self.assertEqual(api["letter"]["text"], "Dear QZXMARK-LETTER")
        status = self.read["status"]
        self.assertEqual(status["state"], "done")
        self.assertEqual(len(status["leftovers"]), 3)


PROGRESS = '''
import threading, time
from fastapi.testclient import TestClient
from backend import migrate_encryption
from backend.app import app

gate = threading.Event()
real = migrate_encryption.seal_tables
def held(*a, **k):
    gate.wait(30)
    return real(*a, **k)
migrate_encryption.seal_tables = held
with TestClient(app) as c:
    r = c.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"})
    out["login"] = r.status_code
    r = c.get("/api/jobs")
    out["while"] = [r.status_code, r.json()]
    out["status_while"] = c.get("/api/encryption/status").json()
    out["me_while"] = c.get("/api/auth/me").status_code
    gate.set()
    for _ in range(300):
        if c.get("/api/encryption/status").json()["state"] == "done":
            break
        time.sleep(0.1)
    r = c.get("/api/jobs?include=excluded,hidden")
    out["after"] = [r.status_code, len(r.json())]
'''


class ProgressWhileEncryptingTest(unittest.TestCase):
    def test_data_apis_answer_423_with_the_progress_until_done(self):
        out = helpers.scenario(LEGACY + MAKE_ADMIN + PROGRESS)
        self.assertEqual(out["login"], 200)
        status, body = out["while"]
        self.assertEqual(status, 423)
        self.assertEqual(body["code"], "encrypting")
        self.assertGreater(body["total"], 10)
        self.assertEqual(out["status_while"]["state"], "running")
        self.assertEqual(out["me_while"], 200)  # signing in and out keep working
        self.assertEqual(out["after"], [200, 2])


class SealedAtRestTest(unittest.TestCase):
    """What the app writes through its API is sealed in the shared test database."""

    def setUp(self):
        self.client = helpers.client()
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.ws = helpers.new_workspace("Sealed at rest")
        self.h = {"X-Workspace": str(self.ws)}

    def raw(self, sql, **params):
        with engine.connect() as conn:
            return conn.execute(text(sql), params).all()

    def test_uploads_are_sealed_files_in_the_tests_own_folder(self):
        real_uploads = ROOT / "data" / "uploads"
        before = sorted(p.name for p in real_uploads.iterdir()) if real_uploads.is_dir() else []
        self.assertEqual(Path(db_module.UPLOAD_DIR).parent, Path(os.environ["JOBHUNT_DATA_DIR"]))
        self.assertNotEqual(Path(db_module.UPLOAD_DIR).resolve(), real_uploads.resolve())
        r = self.client.post("/api/documents", headers=self.h, data={"kind": "resume"},
                             files={"file": ("cv.txt", b"Plain CV text QZXMARK-UPLOADED", "text/plain")})
        self.assertEqual(r.status_code, 200, r.text)
        stored = self.raw("SELECT stored_name FROM documents WHERE id = :i", i=r.json()["id"])[0][0]
        self.assertTrue(stored.endswith(".sealed"))
        data = (db_module.UPLOAD_DIR / stored).read_bytes()
        self.assertTrue(crypto.is_sealed(data))
        self.assertIsNone(MARK.search(data))
        self.assertEqual(self.client.get(f"/api/documents/{r.json()['id']}/text", headers=self.h).json()["text"],
                         "Plain CV text QZXMARK-UPLOADED")
        # Copying a workspace with its documents seals the file again under the copy's own name.
        copy = self.client.post("/api/workspaces", json={"name": "Copy", "copy_from": self.ws, "copy_documents": True})
        copied = self.raw("SELECT stored_name FROM documents WHERE workspace_id = :w", w=copy.json()["id"])[0][0]
        self.assertNotEqual(copied, stored)
        self.assertEqual(self.client.get("/api/documents", headers={"X-Workspace": str(copy.json()["id"])}).json()[0]
                         ["filename"], "cv.txt")
        self.client.delete(f"/api/documents/{r.json()['id']}", headers=self.h)
        self.assertFalse((db_module.UPLOAD_DIR / stored).exists())
        self.client.delete(f"/api/workspaces/{copy.json()['id']}")
        self.assertFalse((db_module.UPLOAD_DIR / copied).exists())
        after = sorted(p.name for p in real_uploads.iterdir()) if real_uploads.is_dir() else []
        self.assertEqual(after, before)  # the app's real uploads folder is never touched

    def test_an_excluded_and_a_kept_job_look_the_same_in_the_raw_bytes(self):
        with helpers.as_admin():
            db = SessionLocal()
            kept = Job(workspace_id=self.ws, url="https://raw.example/kept", origin="private", title="Same title",
                       company="Same co", status="to_review", hidden=False, excluded_reason=None)
            excluded = Job(workspace_id=self.ws, url="https://raw.example/excl", origin="private", title="Same title",
                           company="Same co", status="to_review", hidden=True,
                           excluded_reason="hybrid role needs 3 office days a week (your max is 2)")
            db.add_all([kept, excluded])
            db.commit()
            ids = kept.id, excluded.id
            db.close()
        names = [c.name for c in crypto.sealed_columns(Job.__table__)]
        rows = {}
        for job_id in ids:
            sql = ", ".join(f"typeof({n}), length({n})" for n in names)
            rows[job_id] = list(self.raw(f"SELECT {sql} FROM jobs WHERE id = :i", i=job_id)[0])
        self.assertEqual(rows[ids[0]], rows[ids[1]])  # same types and lengths, column by column
        self.assertNotIn("null", rows[ids[0]][names.index("excluded_reason") * 2])

    def test_fit_results_and_profiles_are_sealed(self):
        self.client.put("/api/profile", headers=self.h, json={"titles": ["QZXMARK Analyst"], "salary_floor": 99000})
        with helpers.as_admin():
            db = SessionLocal()
            job = Job(workspace_id=self.ws, url="https://raw.example/fit", origin="private", title="T", status="applied")
            db.add(job)
            db.flush()
            db.add(FitResult(job_id=job.id, status="ok", score=81, evidence_json=json.dumps({"summary": "QZXMARK"})))
            db.commit()
            db.close()
        stored = self.raw("SELECT titles, salary_floor FROM user_profile WHERE id = :i", i=self.ws)[0]
        self.assertTrue(all(crypto.is_sealed(v) for v in stored))
        for (score, status, evidence) in self.raw("SELECT f.score, f.status, f.evidence_json FROM fit_results f "
                                                  "JOIN jobs j ON j.id = f.job_id WHERE j.workspace_id = :w", w=self.ws):
            self.assertTrue(crypto.is_sealed(score) and crypto.is_sealed(status) and crypto.is_sealed(evidence))
        self.assertEqual(self.client.get("/api/profile", headers=self.h).json()["titles"], ["QZXMARK Analyst"])

    def test_the_commute_cache_is_per_user_and_sealed(self):
        from backend import commute

        uid, dek = helpers.key_of()
        with crypto.as_user(uid, dek):
            self.assertEqual(commute._cached("stop|QZXMARK station", commute.CACHE_FOR, lambda: {"id": 7}), {"id": 7})
            self.assertEqual(commute._cached("stop|QZXMARK station", commute.CACHE_FOR, lambda: {"id": 8}), {"id": 7})
            mine = crypto.blind_index("commute", "stop|QZXMARK station").hex()
        bob = helpers.make_user()
        with helpers.as_account(bob):  # another user's index for the same origin is another row
            self.assertEqual(commute._cached("stop|QZXMARK station", commute.CACHE_FOR, lambda: {"id": 9}), {"id": 9})
            theirs = crypto.blind_index("commute", "stop|QZXMARK station").hex()
        rows = self.raw("SELECT key, owner_id, result_json FROM commute_cache WHERE key IN (:a, :b) ORDER BY owner_id",
                        a=mine, b=theirs)
        self.assertEqual([owner for _key, owner, _result in rows], [uid, bob.id])
        for key, _owner, result in self.raw("SELECT key, owner_id, result_json FROM commute_cache"):
            self.assertNotIn("qzxmark", key.lower())
            self.assertTrue(crypto.is_sealed(result))

    def test_the_http_cache_is_keyed_and_sealed_with_a_process_key(self):
        from backend.scraping import http

        http._cache_put("https://www.seek.com.au/qzxmark-jobs?keywords=QZXMARK", 200, "<html>QZXMARK</html>")
        got = http._cache_get("https://www.seek.com.au/qzxmark-jobs?keywords=QZXMARK", http.ROBOTS_TTL)
        self.assertEqual((got.status, got.text, got.url), (200, "<html>QZXMARK</html>",
                                                            "https://www.seek.com.au/qzxmark-jobs?keywords=QZXMARK"))
        for url, body in self.raw("SELECT url, body FROM http_cache"):
            self.assertNotIn("qzxmark", url.lower())
            self.assertIsNone(MARK.search(body))
        # After a restart the process key is new: the row can't be read, and start-up deletes it.
        with mock.patch.object(crypto, "_PROCESS_AEAD", crypto.AESGCM(crypto.new_key())), \
                mock.patch.object(crypto, "_PROCESS_KID", b"\x00\x00\x00\x00"):
            self.assertIsNone(http._cache_get("https://www.seek.com.au/qzxmark-jobs?keywords=QZXMARK",
                                              http.ROBOTS_TTL))
        db_module.purge_http_cache()
        self.assertEqual(self.raw("SELECT COUNT(*) FROM http_cache")[0][0], 0)

    def test_the_whole_test_database_has_no_plaintext_in_sealed_columns(self):
        self.client.post("/api/sources", headers=self.h, json={"url": "https://qzxmark.example/careers"})
        self.client.post("/api/pins", headers=self.h, json={"label": "QZXMARK", "kind": "home", "lat": -33.8,
                                                            "lng": 151.2})
        self.assertEqual(migrate_encryption.plaintext_audit(), {"columns": {}, "files": []})


class IsolationTest(unittest.TestCase):
    """Another user's rows never open under your key: ownership checks come first, and a slip past them is a 404
    (KeyMismatch), not their data."""

    def test_key_mismatch_answers_404_without_the_data(self):
        alice, bob = helpers.make_user(), helpers.make_user()
        with helpers.as_account(bob):
            db = SessionLocal()
            job = Job(workspace_id=bob.workspace_id, url="https://bob.example/secret", origin="private",
                      title="Bob's QZXMARK job", status="applied")
            db.add(job)
            db.add(Pin(workspace_id=bob.workspace_id, label="Bob's QZXMARK home", kind="home", lat=-33.8, lng=151.2))
            db.commit()
            job_id = job.id
            db.close()
        with helpers.client(alice.email) as a:
            self.assertEqual(a.get(f"/api/jobs/{job_id}").status_code, 404)  # the workspace check
            # Someone who can write the database moves Bob's job into Alice's workspace: it still doesn't open.
            with engine.begin() as conn:
                conn.execute(text("UPDATE jobs SET workspace_id = :w WHERE id = :i"),
                             {"w": alice.workspace_id, "i": job_id})
            try:
                with self.assertLogs("backend.app", "WARNING"):
                    r = a.get(f"/api/jobs/{job_id}")
                self.assertEqual(r.status_code, 404)
                self.assertNotIn("QZXMARK", r.text)
                self.assertNotIn("bob", r.text.lower())
            finally:
                with engine.begin() as conn:
                    conn.execute(text("UPDATE jobs SET workspace_id = :w WHERE id = :i"),
                                 {"w": bob.workspace_id, "i": job_id})
        with helpers.client(bob.email) as b:
            self.assertEqual(b.get(f"/api/jobs/{job_id}").json()["title"], "Bob's QZXMARK job")
        with helpers.as_admin(), self.assertRaises(crypto.KeyMismatch):  # the admin's key doesn't open it either
            db = SessionLocal()
            try:
                db.get(Job, job_id)
            finally:
                db.close()

    def test_strict_mode_refuses_plaintext_put_in_a_sealed_column(self):
        self.assertTrue(crypto.strict())  # the test database's migration has run
        with helpers.as_admin():
            db = SessionLocal()
            job = Job(workspace_id=1, url="https://strict.example/1", origin="private", title="T", status="applied")
            db.add(job)
            db.commit()
            job_id = job.id
            db.close()
        with engine.begin() as conn:  # a downgrade: the sealed status swapped for plaintext of someone's choosing
            conn.execute(text("UPDATE jobs SET status = 'rejected' WHERE id = :i"), {"i": job_id})
        try:
            with helpers.client() as c, self.assertLogs("backend.app", "WARNING"):
                self.assertEqual(c.get(f"/api/jobs/{job_id}", headers={"X-Workspace": "1"}).status_code, 404)
            with helpers.as_admin(), self.assertRaises(crypto.Plaintext):
                db = SessionLocal()
                try:
                    db.get(Job, job_id)
                finally:
                    db.close()
            with engine.begin() as conn:  # and an always-sealed column set to NULL
                conn.execute(text("UPDATE jobs SET excluded_reason = NULL WHERE id = :i"), {"i": job_id})
            with helpers.as_admin(), self.assertRaises(crypto.Plaintext):
                db = SessionLocal()
                try:
                    db.query(Job.excluded_reason).filter(Job.id == job_id).scalar()
                finally:
                    db.close()
        finally:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM jobs WHERE id = :i"), {"i": job_id})

    def test_rows_without_a_workspace_are_refused(self):
        with helpers.as_admin():
            db = SessionLocal()
            try:
                db.add(Document(filename="x", text="y"))
                with self.assertRaises(ValueError):
                    db.flush()
            finally:
                db.rollback()
                db.close()
        default = Job.__table__.c.workspace_id
        self.assertIsNone(default.default)
        self.assertIsNone(default.server_default)


class SqlGuardTest(unittest.TestCase):
    """SQL can't sort, group, de-duplicate, compute or compare over a sealed column; always-sealed columns can't
    even be tested for NULL."""

    def test_the_comparator_refuses_comparisons_and_null_tests_on_always_sealed_columns(self):
        for build in (lambda: Job.excluded_reason.is_(None), lambda: Job.hidden == False,  # noqa: E712
                      lambda: Job.excluded_reason.is_not(None), lambda: Job._title == None,  # noqa: E711
                      lambda: Job.status == "applied", lambda: FitResult.score > 50,
                      lambda: Document.filename.like("%cv%")):
            with self.assertRaises(TypeError):
                build()
        Job.lat.is_(None)  # a plain sealed column may still be tested for NULL

    def test_python_only_job_properties_refuse_sql(self):
        for build in (lambda: Job.url == "https://x.example", lambda: Job.title == "x", lambda: Job.company.in_(["x"]),
                      lambda: select(Job.title)):
            with self.assertRaises((TypeError, Exception)):
                build()

    def test_order_group_distinct_and_functions_are_refused_when_compiled(self):
        statements = [select(Job.id).order_by(Job.status), select(Job.id).group_by(FitResult.score),
                      select(Document.filename).distinct(), select(func.lower(Pin.label)),
                      select(Job.id).where(func.length(Job._description) > 5),
                      select(func.max(Job.last_seen)), select(Workspace).distinct()]
        with engine.connect() as conn:
            for statement in statements:
                with self.assertRaises(crypto.SealedSqlError):
                    conn.execute(statement)
            # Plaintext columns are fine, and so is counting rows.
            conn.execute(select(Job.id).order_by(Job.first_seen, Job.id).distinct())
            conn.execute(select(func.count()).select_from(Job))

    def test_every_table_with_sealed_columns_is_covered_by_the_migration(self):
        names = {t.name for t, _cols in migrate_encryption.sealed_tables()}
        self.assertEqual(names - set(migrate_encryption.OWNED), set())
        self.assertIn("search_profile", names)
        self.assertEqual({c.name for c in crypto.sealed_columns(CommuteCache.__table__)}, {"result_json"})
        self.assertIsInstance(HttpCache.__table__.c.body.type, crypto.ProcessSealedText)


class ScopeInThreadsTest(unittest.TestCase):
    def test_the_scorer_thread_pool_works_under_the_request_scope(self):
        from backend import scorer

        ws = helpers.new_workspace("Scored in threads")
        with helpers.as_admin():
            db = SessionLocal()
            jobs = [Job(workspace_id=ws, url=f"https://pool-scope.example/{i}", origin="private", title=f"Role {i}",
                        company=f"Co {i}", description="A full ad. " * 20, detail_status="full", status="to_review")
                    for i in range(5)]
            db.add_all(jobs)
            db.commit()
            ids = [j.id for j in jobs]
            db.close()
        answer = json.dumps({"score": 77, "requirements": [], "gaps": [], "summary": "Fine."})
        env = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://api.openai.com/v1", "LLM_MODEL": "m",
               "LLM_CONCURRENCY": "3"}
        seen = []

        def fake(messages, cfg, json_mode=True):
            seen.append(threading.current_thread().name)
            return answer

        with mock.patch.dict(os.environ, env), mock.patch.object(scorer, "call_model", side_effect=fake), \
                helpers.client() as c:
            run = c.post("/api/score/run", headers={"X-Workspace": str(ws)}, json={"mode": "all"}).json()
            for _ in range(200):
                state = c.get(f"/api/runs/{run['id']}", headers={"X-Workspace": str(ws)}).json()
                if state["state"] in ("done", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(state["state"], "done", state)
        self.assertEqual(state["summary"]["scoring"]["scored"], 5)
        self.assertGreater(len(set(seen)), 1)  # the pool's own threads, each with the scope copied in
        with helpers.as_admin():
            db = SessionLocal()
            self.assertEqual(sorted(f.score for f in db.query(FitResult).filter(FitResult.job_id.in_(ids))), [77] * 5)
            db.close()
        # A plain pool.submit (no copied context) has no key: the reason backend.threads.submit exists.
        with ThreadPoolExecutor(1) as pool, self.assertRaises(crypto.Locked):
            pool.submit(scorer.score_one, ids[0], "", "h", {"model": "m"}).result()


class BenchmarkTest(unittest.TestCase):
    def test_the_job_list_benchmark_runs(self):
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "bench_jobs_list.py"), "--jobs", "300",
                               "--repeat", "2", "--json"], cwd=ROOT, capture_output=True, text=True, timeout=300,
                              env={k: v for k, v in os.environ.items() if not k.startswith("JOBHUNT_")})
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((out["default_list"]["jobs_sent"], out["full_list"]["jobs_sent"]), (60, 300))
        self.assertEqual(out["default_list"]["omitted"], 240)
        self.assertLess(out["default_list"]["median_s"], 1.5)  # (300 jobs here; 5,000 is the real target, on a Pi)
        self.assertLess(out["value"]["open_us"], 100)


if __name__ == "__main__":
    unittest.main()
