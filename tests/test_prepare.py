"""Tests for the groundwork for accounts and sealed columns (docs/design/accounts.md, Phase A): ownership checks,
the run queue, context propagation, columns that SQL no longer compares, log hygiene and the no-store header.
No network.

Run:  python3 -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["JOBHUNT_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["JOBHUNT_SCHEDULER"] = "off"
os.environ["JOBHUNT_ENV_PATH"] = os.path.join(_TMP, "test.env")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import defer  # noqa: E402

from backend import app as app_module  # noqa: E402
from backend import tasks, threads  # noqa: E402
from backend.adapters.base import Adapter, Posting  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import CompanyProfile, FitResult, Job, SearchRun, SessionLocal, engine, init_db  # noqa: E402

init_db()


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.ws = self.new_workspace()

    def new_workspace(self) -> int:
        ws = self.client.post("/api/workspaces", json={"name": self._testMethodName[:60]}).json()["id"]
        self.addCleanup(lambda: self.client.delete(f"/api/workspaces/{ws}"))
        return ws

    def headers(self, ws=None) -> dict:
        return {"X-Workspace": str(ws or self.ws)}


class RunScopingTest(WorkspaceTestCase):
    def test_runs_are_not_visible_across_workspaces(self):
        other = self.new_workspace()
        db = SessionLocal()
        run = SearchRun(workspace_id=self.ws, kind="search", state="done", progress_json="{}")
        db.add(run)
        db.commit()
        run_id = run.id
        db.close()
        c = self.client
        self.assertEqual(c.get(f"/api/runs/{run_id}", headers=self.headers()).json()["id"], run_id)
        self.assertEqual(c.get(f"/api/runs/{run_id}", headers=self.headers(other)).status_code, 404)
        self.assertEqual(c.get("/api/runs/999999", headers=self.headers()).status_code, 404)
        self.assertIsNone(c.get("/api/runs/latest", headers=self.headers(other)).json())

    def test_owned_checks_the_workspace_before_loading_the_row(self):
        from fastapi import HTTPException
        from backend.workspaces import owned

        db = SessionLocal()
        job = Job(workspace_id=self.ws, url="https://own.example/1", title="Mine")
        db.add(job)
        db.commit()
        job_id = job.id
        db.expire_all()
        with mock.patch.object(db, "get", wraps=db.get) as get:
            with self.assertRaises(HTTPException) as caught:
                owned(db, Job, job_id, self.ws + 1000, "Job")
            self.assertEqual(caught.exception.status_code, 404)
            get.assert_not_called()  # another workspace's row is never loaded
            self.assertEqual(owned(db, Job, job_id, self.ws, "Job").title, "Mine")
        db.close()


class CompaniesTest(WorkspaceTestCase):
    def test_only_the_workspaces_companies(self):
        other = self.new_workspace()
        db = SessionLocal()
        db.add_all([Job(workspace_id=self.ws, url="https://co.example/1", company="Zetacorp Pty Ltd"),
                    Job(workspace_id=other, url="https://co.example/2", company="Omegatech Pty Ltd")])
        for name in ("Zetacorp Pty Ltd", "Omegatech Pty Ltd", "Nobody Else Ltd"):
            from backend.db import company_key
            if db.get(CompanyProfile, company_key(name)) is None:
                db.add(CompanyProfile(key=company_key(name), name=name))
        db.commit()
        db.close()
        from backend.db import company_key
        mine = self.client.get("/api/companies", headers=self.headers()).json()
        theirs = self.client.get("/api/companies", headers=self.headers(other)).json()
        self.assertIn(company_key("Zetacorp Pty Ltd"), mine)
        self.assertNotIn(company_key("Omegatech Pty Ltd"), mine)
        self.assertNotIn(company_key("Nobody Else Ltd"), mine)
        self.assertEqual(set(theirs) & {company_key("Zetacorp Pty Ltd"), company_key("Nobody Else Ltd")}, set())
        self.assertIn(company_key("Omegatech Pty Ltd"), theirs)


def _reference_list_jobs(db, ws, office_slack, include):
    """The job list as it was built before the left-out rows were dropped in SQL: everything loaded, filtered in Python."""
    from backend.app import _profile_hash, _soften, _with_copies, job_to_dict
    from backend.db import CoverLetter
    from backend.filters import softened_job_ids
    from backend.offices import looks_like_recruiter

    phash = _profile_hash(db, ws)
    rows = (db.query(Job, FitResult).outerjoin(FitResult, FitResult.job_id == Job.id)
            .options(defer(Job._description)).filter(Job.workspace_id == ws).all())
    letters = {j for (j,) in db.query(CoverLetter.job_id).join(Job, Job.id == CoverLetter.job_id).filter(Job.workspace_id == ws)}
    softened = softened_job_ids(db, ws, office_slack)
    profiles = {p.key: p for p in db.query(CompanyProfile).all()}
    copies: dict[int, list[Job]] = {}
    for job, _fit in rows:
        if job.duplicate_of:
            copies.setdefault(job.duplicate_of, []).append(job)
    leave_out = {"excluded", "hidden"} - set(include.split(","))

    def omitted(job):
        return (("excluded" in leave_out and bool(job.excluded_reason) and job.id not in softened)
                or ("hidden" in leave_out and bool(job.hidden)))
    everything = [(job, fit) for job, fit in rows if not job.duplicate_of]
    listed = [(job, fit) for job, fit in everything if not omitted(job)]
    return ([_with_copies(_soften(job_to_dict(job, fit, phash, has_letter=job.id in letters,
                                              recruiter=looks_like_recruiter(job.company or "", profiles)), softened),
                          copies.get(job.id, [])) for job, fit in listed],
            len(everything) - len(listed))


class JobListSameOutputTest(WorkspaceTestCase):
    def test_same_output_as_filtering_in_python(self):
        c, h, ws = self.client, self.headers(), self.ws
        c.put("/api/profile", headers=h, json={"max_office_days": 2})
        db = SessionLocal()
        jobs = {}
        for name, kw in {
            "kept": {},
            "kept2": {"company": "Beta Ltd", "closed_at": None},
            "out": {"excluded_reason": "salary below floor ($90,000 < $120,000)"},
            "hybrid": {"work_mode": "hybrid", "office_days": 3,
                       "excluded_reason": "hybrid role needs 3 office days a week (your max is 2)"},
            "hidden": {"hidden": True},
            "hidden_out": {"hidden": True, "excluded_reason": "salary below floor"},
            "blank": {"excluded_reason": ""},
        }.items():
            jobs[name] = Job(workspace_id=ws, url=f"https://same.example/{name}", title=name, status="to_review",
                             detail_status="full", company=kw.pop("company", "Acme"), **kw)
            db.add(jobs[name])
        db.commit()
        for name, parent, kw in (("copy_of_kept", "kept", {}), ("excluded_copy", "kept", {"excluded_reason": "x"}),
                                 ("copy_of_hidden", "hidden", {}), ("copy_of_out", "out", {})):
            db.add(Job(workspace_id=ws, url=f"https://same.example/{name}", title=name, detail_status="full",
                       duplicate_of=jobs[parent].id, **kw))
        db.commit()
        db.add(FitResult(job_id=jobs["kept"].id, status="ok", score=80, profile_hash="old"))
        db.commit()
        db.close()

        shapes = [(slack, inc) for slack in (0, 1, 2) for inc in ("", "excluded", "hidden", "excluded,hidden", "hidden,excluded")]
        seen_sizes = set()
        for slack, inc in shapes:
            r = c.get(f"/api/jobs?office_slack={slack}&include={inc}", headers=h)
            self.assertEqual(r.status_code, 200)
            db = SessionLocal()
            want, omitted = _reference_list_jobs(db, ws, slack, inc)
            db.close()
            self.assertEqual(r.json(), json.loads(json.dumps(want)), (slack, inc))
            self.assertEqual(r.headers["X-Omitted-Count"], str(omitted), (slack, inc))
            seen_sizes.add(len(want))
        self.assertGreater(len(seen_sizes), 2)  # the shapes really do list different sets


class NormalisedReasonTest(WorkspaceTestCase):
    def test_empty_reason_is_stored_as_null(self):
        db = SessionLocal()
        job = Job(workspace_id=self.ws, url="https://norm.example/1", excluded_reason="")
        db.add(job)
        db.commit()
        job_id = job.id
        self.assertIsNone(job.excluded_reason)
        job.excluded_reason = "salary below floor"
        db.commit()
        job.excluded_reason = ""
        db.commit()
        raw = db.execute(text("SELECT excluded_reason FROM jobs WHERE id = :i"), {"i": job_id}).scalar()
        self.assertIsNone(raw)
        db.close()

    def test_one_time_data_fix(self):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO jobs (workspace_id, url, excluded_reason, status, detail_status, hidden) "
                              "VALUES (:w, 'https://norm.example/legacy', '', 'to_review', 'none', 0)"), {"w": self.ws})
        init_db()
        with engine.connect() as conn:
            value = conn.execute(text("SELECT excluded_reason FROM jobs WHERE url = 'https://norm.example/legacy'")).scalar()
        self.assertIsNone(value)

    def test_blank_reason_job_is_eligible_everywhere(self):
        from backend.scorer import select_jobs

        db = SessionLocal()
        db.add(Job(workspace_id=self.ws, url="https://norm.example/2", title="T", detail_status="full", excluded_reason=""))
        db.commit()
        ids = select_jobs(db, "pending", "hash", self.ws)
        db.close()
        self.assertEqual(len(ids), 1)


class PrefixInPythonTest(WorkspaceTestCase):
    def test_softened_ids_match_by_prefix_in_python(self):
        from backend.filters import OFFICE_DAYS_REASON, softened_job_ids

        self.client.put("/api/profile", headers=self.headers(), json={"max_office_days": 2})
        db = SessionLocal()
        yes = Job(workspace_id=self.ws, url="https://pre.example/1", work_mode="hybrid", office_days=3, detail_status="full",
                  excluded_reason=f"{OFFICE_DAYS_REASON}3 office days a week (your max is 2)")
        no = Job(workspace_id=self.ws, url="https://pre.example/2", work_mode="hybrid", office_days=3, detail_status="full",
                 excluded_reason="salary below floor")
        db.add_all([yes, no])
        db.commit()
        self.assertEqual(softened_job_ids(db, self.ws, 1), {yes.id})
        self.assertEqual(softened_job_ids(db, self.ws, 0), set())
        db.close()


class TaskQueueTest(WorkspaceTestCase):
    def wait_for(self, run_id, state, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            db = SessionLocal()
            run = db.get(SearchRun, run_id)
            got = run.state
            db.close()
            if got == state:
                return
            time.sleep(0.02)
        self.fail(f"run {run_id} never reached {state}")

    def test_same_workspace_refused_others_queue_first_in_first_out(self):
        other, third = self.new_workspace(), self.new_workspace()
        gate, order = threading.Event(), []

        def job(name, wait=False):
            def run(progress):
                if wait:
                    gate.wait(10)
                order.append(name)
                return {}
            return run

        first = tasks.start("search", job("a1", wait=True), self.ws)
        self.assertNotIn("already_running", first)
        again = tasks.start("search", job("a2"), self.ws)
        self.assertTrue(again["already_running"])
        self.assertEqual(again["id"], first["id"])
        second = tasks.start("search", job("b"), other)  # another workspace: queued, not refused
        third_run = tasks.start("score", job("c"), third)
        self.assertNotIn("already_running", second)
        self.assertEqual(tasks.active_run(other)["id"], second["id"])
        self.assertEqual(tasks.active_run(other)["state"], "queued")
        self.assertTrue(tasks.start("search", job("b2"), other)["already_running"])
        gate.set()
        for run in (first, second, third_run):
            self.wait_for(run["id"], "done")
        self.assertEqual(order, ["a1", "b", "c"])
        self.assertIsNone(tasks.active_run(self.ws))
        self.assertNotIn("already_running", tasks.start("search", job("a3"), self.ws))  # free again
        last = tasks.active_run(self.ws)
        if last:
            self.wait_for(last["id"], "done")

    def test_mark_interrupted_writes_a_code(self):
        db = SessionLocal()
        run = SearchRun(workspace_id=self.ws, kind="search", state="running", progress_json="{}")
        db.add(run)
        db.commit()
        run_id = run.id
        db.close()
        tasks.mark_interrupted()
        db = SessionLocal()
        run = db.get(SearchRun, run_id)
        self.assertEqual((run.state, run.error_code), ("failed", "interrupted"))
        self.assertEqual(run.error, "interrupted by server restart")  # the visible message is unchanged
        db.close()

    def test_failed_run_logs_only_the_class_name(self):
        marker = "MARKER-secret-term"

        def boom(progress):
            raise ValueError(marker)

        with self.assertLogs("backend.tasks", "ERROR") as logs:
            run = tasks.start("search", boom, self.ws)
            self.wait_for(run["id"], "failed")
        self.assertNotIn(marker, "\n".join(logs.output))
        self.assertIn("ValueError", "\n".join(logs.output))


class ContextPropagationTest(WorkspaceTestCase):
    VAR: contextvars.ContextVar = contextvars.ContextVar("scope_for_test", default=None)

    def test_submit_carries_the_context_into_pool_threads(self):
        token = self.VAR.set("scope-1")
        self.addCleanup(self.VAR.reset, token)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertIsNone(pool.submit(self.VAR.get).result())  # a plain submit loses it
            self.assertEqual(threads.submit(pool, self.VAR.get).result(), "scope-1")
            futures = [threads.submit(pool, lambda i=i: (self.VAR.get(), i)) for i in range(5)]  # one copy per call
            self.assertEqual([f.result() for f in futures], [("scope-1", i) for i in range(5)])
            self.assertEqual(pool.submit(threads.run_in_context(self.VAR.get)).result(), "scope-1")

    def test_task_job_runs_in_the_starters_context(self):
        seen = []
        token = self.VAR.set("scope-2")
        self.addCleanup(self.VAR.reset, token)
        run = tasks.start("search", lambda progress: seen.append(self.VAR.get()) or {}, self.ws)
        end = time.monotonic() + 10
        while tasks.active_run(self.ws) and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(seen, ["scope-2"])
        self.assertEqual(run["state"], "queued")


class _MarkerAdapter(Adapter):
    source = "marker-board"
    method = "test"
    has_details = False

    def __init__(self, postings=None, error=None):
        self._postings, self._error = postings or [], error

    def list_postings(self):
        if self._error:
            raise self._error
        return self._postings


class LogHygieneTest(WorkspaceTestCase):
    COMPANY, URL, TERM, PLACE = "Zorblax Industries", "https://jobs.zorblax.example/careers/12345", "quantum-llama-wrangler", "Zorbville"

    def test_search_logs_contain_no_user_data(self):
        from backend.pipeline import run_search

        posting = Posting(url=self.URL + "/job", title=self.TERM, company=self.COMPANY, location_text=f"{self.PLACE} NSW")
        adapters = [(None, _MarkerAdapter(error=RuntimeError(f"{self.TERM} {self.URL}"))),
                    (None, _MarkerAdapter([posting]))]

        def boom(*args, **kwargs):
            raise httpx.ConnectError(f"cannot reach {self.PLACE} {self.TERM}")

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage() + (logging.Formatter().formatException(record.exc_info) if record.exc_info else ""))

        root, handler = logging.getLogger(), Capture(logging.DEBUG)
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            with mock.patch("backend.pipeline.build_adapters", return_value=adapters), \
                    mock.patch("backend.geo.httpx.get", side_effect=boom), mock.patch("backend.geo.time.sleep"), \
                    mock.patch("backend.pipeline.repair_geocodes"):
                result = run_search(self.ws)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        self.assertEqual(result["totals"]["listed"], 1)
        logged = "\n".join(records)
        self.assertTrue(records)  # the failures were logged...
        for marker in (self.COMPANY, self.URL, self.TERM, self.PLACE, "zorblax"):
            self.assertNotIn(marker.lower(), logged.lower())  # ...without the user's words
        self.assertIn("RuntimeError", logged)
        self.assertIn("ConnectError", logged)

    def test_library_loggers_are_quiet(self):
        for name in ("httpx", "httpcore"):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING)

    def test_access_log_filter_strips_query_strings(self):
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                   ("127.0.0.1:1", "GET", "/api/jobs?token=secret&q=llama", "1.1", 200), None)
        self.assertTrue(app_module.StripQueryString().filter(record))
        self.assertNotIn("secret", record.getMessage())
        self.assertIn("/api/jobs ", record.getMessage())
        self.assertIn(app_module.StripQueryString, {type(f) for f in logging.getLogger("uvicorn.access").filters})


class NoStoreTest(WorkspaceTestCase):
    def test_every_api_response_is_no_store(self):
        for path in ("/api/jobs", "/api/companies", "/api/runs/latest", "/api/workspaces", "/api/runs/999999"):
            r = self.client.get(path, headers=self.headers())
            self.assertEqual(r.headers.get("cache-control"), "no-store", path)
        self.assertEqual(self.client.post("/api/workspaces", json={}).headers.get("cache-control"), "no-store")  # a 422 too
        self.assertNotEqual(self.client.get("/").headers.get("cache-control"), "no-store")  # the pages themselves are cached as before


if __name__ == "__main__":
    unittest.main()
