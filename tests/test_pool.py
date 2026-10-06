"""Tests for the shared job pool and the company-profile cache (docs/design/accounts.md §14, Phase A2):
one posting per ad for every workspace, one detail fetch, pool matching, private uploads, per-workspace fit
results, the closing rule, the migration from per-workspace copies, the unchanged job JSON, and company-profile
freshness. The network and the LLM are mocked throughout.

Run:  python3 -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["JOBHUNT_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["JOBHUNT_SCHEDULER"] = "off"
os.environ["JOBHUNT_ENV_PATH"] = os.path.join(_TMP, "test.env")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend.adapters.base import Adapter, Posting as Scraped  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import (AppSetting, ChatThread, CompanyProfile, CoverLetter, Document, FitResult, Job, Posting,  # noqa: E402
                        SessionLocal, company_key, engine, init_db)
from backend.scraping.http import FetchError  # noqa: E402

init_db()

LLM_ENV = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://api.openai.com/v1", "LLM_MODEL": "m"}
FULL_AD = ("About the role. You will run the delivery program for our enterprise customers, coordinate engineering "
           "and product teams, and report to the executive team. We offer hybrid working with 3 days in the office. ")
TEASER = "Short teaser: lead delivery."


class FakeAdapter(Adapter):
    """A source with fixed listing and detail answers; it counts the detail pages it was asked for."""

    def __init__(self, source: str, postings: list[Scraped], *, complete: bool = True, has_details: bool = True,
                 details: dict | None = None):
        self.source, self.method = source, "test"
        self.complete_listing, self.has_details = complete, has_details
        self._postings, self.details, self.fetched = postings, details or {}, []

    def list_postings(self):
        return [copy.copy(p) for p in self._postings]  # the pipeline fills in fields on what it's given

    def fetch_detail(self, posting):
        self.fetched.append(posting.url)
        answer = self.details.get(posting.url, "full")
        if isinstance(answer, Exception):
            raise answer
        if answer == "full":
            return replace(posting, description=FULL_AD, detail_status="full")
        return answer


_ROLES = itertools.count(1)


def ad(n: int, **kw) -> Scraped:
    base = dict(url=f"https://jobs.pool.example/{n}", title="Program Manager", company="Poolco",
                location_text="Sydney NSW", country="AU", external_id=str(n))
    return Scraped(**{**base, **kw})


@contextmanager
def offline():
    """No geocoder, no geocode repair (no network)."""
    with ExitStack() as stack:
        stack.enter_context(mock.patch("backend.pipeline.geocode", return_value=None))
        stack.enter_context(mock.patch("backend.offices.geocode", return_value=None))
        stack.enter_context(mock.patch("backend.pipeline.repair_geocodes"))
        yield


def search(ws: int, *adapters: Adapter) -> dict:
    from backend.pipeline import run_search

    with offline(), mock.patch("backend.pipeline.build_adapters", return_value=[(None, a) for a in adapters]):
        return run_search(ws)


class PoolTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        # A job title of this test's own: pool matching links every open posting that matches a workspace's
        # titles, so other tests' postings must not match.
        self.role = f"Program Manager {next(_ROLES)}{os.getpid()}"

    def ad(self, n: int, **kw) -> Scraped:
        return ad(n, **{"title": self.role, **kw})

    def workspace(self, titles=None, **profile) -> int:
        titles = (self.role,) if titles is None else titles
        ws = self.client.post("/api/workspaces", json={"name": self._testMethodName[:60]}).json()["id"]
        self.addCleanup(lambda: self.client.delete(f"/api/workspaces/{ws}"))
        self.client.put("/api/profile", headers=self.h(ws), json={"titles": list(titles), "seek_enabled": False,
                                                                   **profile})
        return ws

    @staticmethod
    def h(ws: int) -> dict:
        return {"X-Workspace": str(ws)}

    def jobs(self, ws: int, include: str = "") -> dict[str, dict]:
        r = self.client.get(f"/api/jobs?include={include}", headers=self.h(ws))
        self.assertEqual(r.status_code, 200, r.text)
        return {j["url"]: j for j in r.json()}

    def wait(self, run: dict, timeout: float = 10) -> dict:
        end = time.monotonic() + timeout
        while run["state"] in ("queued", "running") and time.monotonic() < end:
            time.sleep(0.02)
            run = self.client.get(f"/api/runs/{run['id']}", headers=self.h(run["workspace_id"])).json()
        self.assertEqual(run["state"], "done", run)
        return run


class SharedPoolTest(PoolTestCase):
    def test_same_posting_one_row_two_links_one_detail_fetch(self):
        a, b = self.workspace(), self.workspace()
        url = "https://jobs.pool.example/shared-1"
        first = FakeAdapter("greenhouse:poolco", [self.ad(1, url=url)])
        out_a = search(a, first)
        self.assertEqual(first.fetched, [url])
        self.assertEqual(out_a["totals"]["details_fetched"], 1)

        second = FakeAdapter("greenhouse:poolco", [self.ad(1, url=url)])
        out_b = search(b, second)
        self.assertEqual(second.fetched, [])  # the pool already has the full ad: no second fetch
        self.assertEqual(out_b["totals"]["new"], 1)

        db = SessionLocal()
        self.assertEqual(db.query(Posting).filter(Posting.url == url).count(), 1)
        links = db.query(Job).filter(Job.url == url, Job.workspace_id.in_([a, b])).all()
        self.assertEqual(len(links), 2)
        self.assertEqual(len({j.posting_id for j in links}), 1)
        self.assertEqual({j.origin for j in links}, {"pool"})
        self.assertTrue(all(j._description is None for j in links))  # content lives on the posting only
        db.close()
        for ws in (a, b):
            job = self.jobs(ws)[url]
            self.assertEqual((job["title"], job["detail_status"]), (self.role, "full"))
        self.assertNotEqual(self.jobs(a)[url]["id"], self.jobs(b)[url]["id"])

        # A stale full description is fetched again, once.
        db = SessionLocal()
        db.query(Posting).filter(Posting.url == url).update({Posting.detail_fetched_at: datetime.utcnow() - timedelta(days=31)})
        db.commit()
        db.close()
        third = FakeAdapter("greenhouse:poolco", [self.ad(1, url=url)])
        search(a, third)
        self.assertEqual(third.fetched, [url])
        fourth = FakeAdapter("greenhouse:poolco", [self.ad(1, url=url)])
        search(b, fourth)
        self.assertEqual(fourth.fetched, [])

    def test_personal_state_stays_per_workspace(self):
        a, b = self.workspace(), self.workspace()
        url = "https://jobs.pool.example/personal"
        search(a, FakeAdapter("ashby:poolco", [self.ad(2, url=url)]))
        search(b, FakeAdapter("ashby:poolco", [self.ad(2, url=url)]))
        ida, idb = self.jobs(a)[url]["id"], self.jobs(b)[url]["id"]
        self.client.patch(f"/api/jobs/{ida}/status", headers=self.h(a), json={"status": "applied"})
        self.client.patch("/api/jobs/hidden", headers=self.h(b), json={"ids": [idb], "hidden": True})
        self.assertEqual(self.jobs(a)[url]["status"], "applied")
        self.assertNotIn(url, self.jobs(b))  # hidden in B only
        self.assertEqual(self.jobs(b, "hidden")[url]["status"], "to_review")
        self.assertEqual(self.client.get(f"/api/jobs/{ida}", headers=self.h(b)).status_code, 404)

    def test_a_source_label_the_user_typed_stays_private(self):
        from backend.db import CompanySource

        a, b = self.workspace(), self.workspace()
        db = SessionLocal()
        src = CompanySource(workspace_id=a, careers_url="https://boards.example/labelco", label="My dream employer")
        db.add(src)
        db.commit()
        db.refresh(src)
        db.expunge(src)
        db.close()
        url = "https://boards.example/labelco/1"
        from backend.pipeline import run_search

        adapter = FakeAdapter("lever:labelco", [self.ad(3, url=url, company="")], has_details=False)
        with offline(), mock.patch("backend.pipeline.build_adapters", return_value=[(src, adapter)]):
            run_search(a)
        self.assertEqual(self.jobs(a)[url]["company"], "My dream employer")
        db = SessionLocal()
        self.assertEqual(db.query(Posting).filter(Posting.url == url).one().company, "Labelco")  # from the URL
        db.close()


class PoolMatchTest(PoolTestCase):
    def test_b_gets_a_posting_found_by_as_search(self):
        a = self.workspace()
        url = "https://jobs.pool.example/match-1"
        search(a, FakeAdapter("workable:poolco", [self.ad(10, url=url), ad(11, title="Barista")]))
        b = self.workspace()
        out = search(b)  # B has no sources of its own
        self.assertEqual(out["totals"]["pool_matched"], 1)
        jobs = self.jobs(b, "excluded,hidden")
        self.assertIn(url, jobs)
        self.assertEqual(jobs[url]["detail_status"], "full")
        self.assertNotIn("https://jobs.pool.example/11", jobs)  # A found it, but it doesn't match B's titles
        self.assertEqual(search(b)["totals"]["pool_matched"], 0)  # linked once

    def test_no_match_without_own_titles_or_with_a_dealbreaker(self):
        a = self.workspace()
        search(a, FakeAdapter("workable:poolco2", [self.ad(12, company="Gambling Pty Ltd")]))
        nothing = self.workspace(titles=())
        self.assertEqual(search(nothing)["totals"]["pool_matched"], 0)
        self.assertEqual(self.jobs(nothing, "excluded,hidden"), {})
        picky = self.workspace()
        self.client.put("/api/dealbreakers", headers=self.h(picky), json={"industries": ["gambling"], "keywords": []})
        self.assertEqual(search(picky)["totals"]["pool_matched"], 0)
        self.assertEqual(self.jobs(picky, "excluded,hidden"), {})

    def test_closed_postings_are_not_matched(self):
        a = self.workspace()
        url = "https://jobs.pool.example/closed-match"
        search(a, FakeAdapter("workable:poolco3", [self.ad(13, url=url)]))
        search(a, FakeAdapter("workable:poolco3", [self.ad(14)]))  # a complete listing without it
        b = self.workspace()
        search(b)
        self.assertNotIn(url, self.jobs(b, "excluded,hidden"))


SEEK_ID = "88880001"
SEEK_URL = f"https://www.seek.com.au/job/{SEEK_ID}"


def seek_page(title: str) -> bytes:
    page = {"jobdetails": {"result": {
        "workArrangements": {"arrangements": [{"type": "HYBRID", "label": "Hybrid"}]},
        "job": {"id": SEEK_ID, "title": title, "content": "<p>" + FULL_AD + "Private full ad text.</p>",
                "location": {"label": "Sydney NSW"}, "advertiser": {"name": "Poolco"},
                "salary": {"label": ""}, "listedAt": {"dateTimeUtc": "2026-10-01"},
                "classifications": [{"label": "Program & Project Management"}]}}}}
    return f"<html><script>\n    window.SEEK_REDUX_DATA = {json.dumps(page)};\n</script></html>".encode()


class PrivateUploadTest(PoolTestCase):
    def test_full_ad_is_private_and_each_workspace_scores_its_own_ad(self):
        import backend.scorer as sc

        a, b = self.workspace(), self.workspace()
        listing = [Scraped(url=SEEK_URL, title=self.role, company="Poolco", external_id=SEEK_ID,
                           location_text="Sydney NSW", country="AU", description=TEASER, detail_status="summary")]
        for ws in (a, b):
            search(ws, FakeAdapter("seek", listing, complete=False, has_details=False))
        ida, idb = self.jobs(a)[SEEK_URL]["id"], self.jobs(b)[SEEK_URL]["id"]
        with offline():
            r = self.client.post(f"/api/jobs/{ida}/import-page", headers=self.h(a),
                                 files={"file": ("ad.html", seek_page(self.role), "text/html")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Private full ad text.", r.json()["description"])
        self.assertEqual(r.json()["detail_status"], "full")
        job_b = self.client.get(f"/api/jobs/{idb}", headers=self.h(b)).json()
        self.assertEqual((job_b["description"], job_b["detail_status"]), (TEASER, "summary"))
        db = SessionLocal()
        posting = db.query(Posting).filter(Posting.url == SEEK_URL).one()
        self.assertEqual((posting.description, posting.detail_status), (TEASER, "summary"))
        db.close()

        # Each workspace's scorer reads the ad that workspace sees, and gets its own fit result.
        db = SessionLocal()
        db.add_all([Document(workspace_id=a, filename="a.pdf", kind="resume", text="Candidate Alpha resume."),
                    Document(workspace_id=b, filename="b.pdf", kind="resume", text="Candidate Beta resume.")])
        db.commit()
        db.close()
        sent: dict[str, str] = {}

        def fake_call(messages, cfg, json_mode=True):
            prompt = json.dumps(messages)
            who = "a" if "Candidate Alpha" in prompt else "b"
            sent[who] = prompt
            return json.dumps({"score": 81 if who == "a" else 42, "requirements": [], "gaps": [], "summary": who})

        from backend.scorer import score_jobs
        with mock.patch.dict(os.environ, LLM_ENV), mock.patch.object(sc, "call_model", side_effect=fake_call), offline():
            for ws, job_id in ((a, ida), (b, idb)):
                self.assertEqual(score_jobs(ws, [job_id])["scored"], 1)
        self.assertIn("Private full ad text.", sent["a"])
        self.assertNotIn("Private full ad text.", sent["b"])
        self.assertIn(TEASER, sent["b"])
        self.assertIn("only the job board's listing summary", sent["b"])
        self.assertEqual(self.jobs(a)[SEEK_URL]["fit"]["score"], 81)
        self.assertEqual(self.jobs(b)[SEEK_URL]["fit"]["score"], 42)
        db = SessionLocal()
        self.assertEqual({f.job_id: f.score for f in db.query(FitResult).filter(FitResult.job_id.in_([ida, idb]))},
                         {ida: 81, idb: 42})
        db.close()

        # A later search keeps A's private ad and B's summary.
        for ws in (a, b):
            search(ws, FakeAdapter("seek", listing, complete=False, has_details=False))
        self.assertIn("Private full ad text.", self.client.get(f"/api/jobs/{ida}", headers=self.h(a)).json()["description"])
        self.assertEqual(self.client.get(f"/api/jobs/{idb}", headers=self.h(b)).json()["description"], TEASER)

    def test_imported_page_jobs_unknown_to_the_pool_stay_private(self):
        a, b = self.workspace(), self.workspace()
        node = {"@type": "JobPosting", "title": self.role, "url": "https://private.example/imported/1",
                "hiringOrganization": {"name": "Secretco"}, "description": "<p>" + FULL_AD + "</p>",
                "jobLocation": {"address": {"addressLocality": "Sydney", "addressRegion": "NSW", "addressCountry": "AU"}}}
        html = f'<script type="application/ld+json">{json.dumps(node)}</script>'.encode()
        with offline():
            r = self.client.post("/api/import/page", headers=self.h(a), files={"file": ("p.html", html, "text/html")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("https://private.example/imported/1", self.jobs(a))
        db = SessionLocal()
        self.assertEqual(db.query(Posting).filter(Posting.url == "https://private.example/imported/1").count(), 0)
        job = db.query(Job).filter(Job.workspace_id == a, Job.url == "https://private.example/imported/1").one()
        self.assertEqual((job.origin, job.posting_id, job.company), ("private", None, "Secretco"))
        db.close()
        search(b)  # pool matching never sees it
        self.assertNotIn("https://private.example/imported/1", self.jobs(b, "excluded,hidden"))


class ClosingRuleTest(PoolTestCase):
    def posting(self, url: str) -> Posting:
        db = SessionLocal()
        try:
            p = db.query(Posting).filter(Posting.url == url).one()
            db.expunge(p)
            return p
        finally:
            db.close()

    def test_unlisted_gone_stale_and_reopen(self):
        a, b = self.workspace(), self.workspace()
        one, two = self.ad(21, url="https://close.example/1"), self.ad(22, url="https://close.example/2")
        search(a, FakeAdapter("greenhouse:closeco", [one, two]))
        # B's own results lack both: one user's results never close anything.
        search(b, FakeAdapter("seek", [], complete=False, has_details=False))
        search(b, FakeAdapter("lever:otherco", [self.ad(23, url="https://close.example/other")]))
        self.assertIsNone(self.posting(one.url).closed_at)
        # A partial listing of the same source doesn't close either.
        search(a, FakeAdapter("greenhouse:closeco", [one], complete=False))
        self.assertIsNone(self.posting(two.url).closed_at)
        # The complete listing of the same listing no longer shows it: closed for everyone.
        out = search(a, FakeAdapter("greenhouse:closeco", [one]))
        self.assertEqual(out["totals"]["closed"], 1)
        self.assertEqual(self.posting(two.url).close_reason, "unlisted")
        self.assertTrue(self.jobs(a)[two.url]["closed"])
        # Listed again: open again.
        search(a, FakeAdapter("greenhouse:closeco", [one, two]))
        self.assertIsNone(self.posting(two.url).closed_at)

        # Its page is gone (404): closed.
        gone = FakeAdapter("greenhouse:closeco", [one, two], details={one.url: FetchError("HTTP 404", 404)})
        db = SessionLocal()
        db.query(Posting).filter(Posting.url == one.url).update({Posting.detail_status: "summary"})
        db.commit()
        db.close()
        search(a, gone)
        self.assertEqual(self.posting(one.url).close_reason, "gone")

        # Not listed anywhere for 45 days: closed as stale (SEEK lists are partial, so this is how they close).
        seek = Scraped(url="https://www.seek.com.au/job/99990001", title=self.role, company="Poolco",
                       description=TEASER, detail_status="summary", external_id="99990001")
        search(a, FakeAdapter("seek", [seek], complete=False, has_details=False))
        db = SessionLocal()
        db.query(Posting).filter(Posting.url == seek.url).update(
            {Posting.last_listed_at: datetime.utcnow() - timedelta(days=46)})
        db.commit()
        db.close()
        out = search(b)
        self.assertEqual(self.posting(seek.url).close_reason, "stale")
        self.assertEqual(out["totals"]["closed"], 0)  # B doesn't have it: not counted for B
        search(a, FakeAdapter("seek", [seek], complete=False, has_details=False))
        self.assertIsNone(self.posting(seek.url).closed_at)

    def test_crawled_pages_close_only_their_own_listing(self):
        from backend.adapters.generic import GenericAdapter
        from backend.pipeline import listing_key

        x, y = GenericAdapter(None, "https://acme.example/careers"), GenericAdapter(None, "https://acme.example/jobs")
        self.assertEqual(x.source, y.source)
        self.assertNotEqual(listing_key(x), listing_key(y))
        x.delegate, x.source = object(), "greenhouse:acme"
        self.assertEqual(listing_key(x), "greenhouse:acme")


class JobJsonShapeTest(PoolTestCase):
    KEYS = {"id", "source", "company", "company_key", "title", "location_text", "country", "lat", "lng", "work_mode",
            "employment_type", "office_days", "salary_text", "salary_min", "salary_max", "url", "distance_km",
            "status", "excluded_reason", "detail_status", "posted_at", "first_seen", "last_seen", "closed", "hidden",
            "fit", "has_cover_letter", "office_text", "office_source", "office_placed", "office_unknown",
            "location_verdict", "recruiter", "also_on"}

    def test_list_jobs_shape_unchanged(self):
        ws = self.workspace()
        url = "https://shape.example/1"
        search(ws, FakeAdapter("workable:shapeco", [self.ad(30, url=url, salary_text="$150k", employment_type="contract")]))
        db = SessionLocal()
        db.add(Job(workspace_id=ws, url="https://shape.example/private", title=self.role, company="Own Co",
                   detail_status="full", description=FULL_AD, status="to_review"))
        db.commit()
        db.close()
        jobs = self.jobs(ws)
        for job in jobs.values():
            self.assertEqual(set(job), self.KEYS)
        pooled = jobs[url]
        self.assertEqual((pooled["source"], pooled["title"], pooled["salary_text"], pooled["employment_type"],
                          pooled["detail_status"], pooled["closed"], pooled["work_mode"]),
                         ("workable:shapeco", self.role, "$150k", "contract", "full", False, "hybrid"))
        self.assertEqual(pooled["office_days"], 3)
        full = self.client.get(f"/api/jobs/{pooled['id']}", headers=self.h(ws)).json()
        self.assertEqual(set(full), self.KEYS | {"description", "office_candidates"})
        self.assertEqual(full["description"], FULL_AD)


class MigrationTest(unittest.TestCase):
    """Per-workspace copies from before the pool, moved into it."""

    COLS = ("workspace_id", "url", "source", "external_id", "company", "title", "location_text", "country",
            "work_mode", "salary_text", "description", "detail_status", "status", "hidden", "excluded_reason",
            "first_seen", "last_seen", "closed_at", "office_text", "office_source", "office_checked_at",
            "office_days", "description_hash", "source_id")

    def legacy(self, conn, **kw) -> int:
        row = {c: None for c in self.COLS}
        row.update(status="to_review", hidden=0, detail_status="none", country="AU",
                   first_seen="2026-09-01 10:00:00.000000", last_seen="2026-10-04 10:00:00.000000")
        row.update(kw)
        conn.execute(text(f"INSERT INTO jobs ({', '.join(self.COLS)}) VALUES ({', '.join(':' + c for c in self.COLS)})"),
                     row)
        return conn.execute(text("SELECT last_insert_rowid()")).scalar()

    def setUp(self):
        with engine.begin() as conn:
            for name in ("Legacy A", "Legacy B"):
                conn.execute(text("INSERT INTO workspaces (name, created_at) VALUES (:n, CURRENT_TIMESTAMP)"), {"n": name})
            self.a, self.b = [i for (i,) in conn.execute(text("SELECT id FROM workspaces ORDER BY id DESC LIMIT 2"))][::-1]
            a, b = self.a, self.b
            conn.execute(text("INSERT INTO company_sources (workspace_id, careers_url, label, enabled) "
                              "VALUES (:w, 'https://jobs.lever.co/legacyco', 'Our pet name', 1)"), {"w": a})
            source_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
            self.ids = {
                "shared_a": self.legacy(conn, workspace_id=a, url="https://legacy.example/1", source="greenhouse:leg",
                                        title="Program Manager", company="Legacyco", description=FULL_AD,
                                        detail_status="full", status="applied", office_days=3),
                "shared_b": self.legacy(conn, workspace_id=b, url="https://legacy.example/1", source="greenhouse:leg",
                                        title="Program Manager", company="Legacyco", description=FULL_AD,
                                        detail_status="full", hidden=1, excluded_reason="salary below floor",
                                        last_seen="2026-10-05 10:00:00.000000", office_days=3),
                "seek_a": self.legacy(conn, workspace_id=a, url="https://www.seek.com.au/job/77001", source="seek",
                                      external_id="77001", title="Delivery Lead", company="Seekco",
                                      description=FULL_AD + "Uploaded by A.", detail_status="full"),
                "seek_b": self.legacy(conn, workspace_id=b, url="https://www.seek.com.au/job/77001", source="seek",
                                      external_id="77001", title="Delivery Lead", company="Seekco",
                                      description=TEASER, detail_status="summary"),
                "only_upload": self.legacy(conn, workspace_id=a, url="https://www.seek.com.au/job/77002", source="seek",
                                           external_id="77002", title="Only Uploaded", company="Seekco",
                                           description=FULL_AD + "Nobody else.", detail_status="full"),
                "imported": self.legacy(conn, workspace_id=a, url="https://legacy-import.example/9", source="import",
                                        title="Imported", company="Privateco", description=FULL_AD, detail_status="full"),
                "closed_both_a": self.legacy(conn, workspace_id=a, url="https://legacy.example/closed", source="lever:leg",
                                             title="Closed", closed_at="2026-10-01 00:00:00.000000"),
                "closed_both_b": self.legacy(conn, workspace_id=b, url="https://legacy.example/closed", source="lever:leg",
                                             title="Closed", closed_at="2026-10-02 00:00:00.000000"),
                "office_a": self.legacy(conn, workspace_id=a, url="https://legacy.example/office", source="lever:leg",
                                        title="Office", description=FULL_AD, detail_status="full",
                                        office_text="1 Denison St, North Sydney", office_source="ad",
                                        office_checked_at="2026-10-03 00:00:00.000000"),
                "labelled": self.legacy(conn, workspace_id=a, url="https://jobs.lever.co/legacyco/1", source="lever:legacyco",
                                        source_id=source_id, title="Labelled", company="Our pet name"),
            }
            ids = self.ids
            conn.execute(text("INSERT INTO fit_results (job_id, status, score, evidence_json, profile_hash) "
                              "VALUES (:a, 'ok', 77, '{}', 'h'), (:b, 'ok', 33, '{}', 'h')"),
                         {"a": ids["shared_a"], "b": ids["shared_b"]})
            conn.execute(text("INSERT INTO cover_letters (job_id, text, edited) VALUES (:j, 'Dear Hiring Manager', 0)"),
                         {"j": ids["seek_a"]})
            conn.execute(text("INSERT INTO chat_threads (workspace_id, job_ids_json, title) VALUES (:w, :j, 'Chat')"),
                         {"w": a, "j": json.dumps([ids["shared_a"], ids["seek_a"]])})
            conn.execute(text("DELETE FROM app_settings WHERE key = 'job_pool_migration'"))

    def snapshot(self):
        with engine.connect() as conn:
            jobs = conn.execute(text("SELECT * FROM jobs WHERE workspace_id IN (:a, :b) ORDER BY id"),
                                {"a": self.a, "b": self.b}).fetchall()
            postings = conn.execute(text("SELECT * FROM postings WHERE url LIKE '%legacy%' OR url LIKE '%7700%' "
                                         "ORDER BY id")).fetchall()
        return [tuple(r) for r in jobs], [tuple(r) for r in postings]

    def check(self):
        from backend import pool_migration

        ids, a, b = self.ids, self.a, self.b
        db = SessionLocal()
        get = lambda key: db.get(Job, ids[key])  # noqa: E731
        # One posting for the shared ad, both links kept with their ids and personal state.
        self.assertEqual(db.query(Posting).filter(Posting.url == "https://legacy.example/1").count(), 1)
        sa, sb = get("shared_a"), get("shared_b")
        self.assertEqual(sa.posting_id, sb.posting_id)
        self.assertEqual((sa.origin, sa.status, sa.hidden, sa.excluded_reason), ("pool", "applied", False, None))
        self.assertEqual((sb.status, sb.hidden, sb.excluded_reason), ("to_review", True, "salary below floor"))
        self.assertEqual((sa.title, sa.description, sa.detail_status), ("Program Manager", FULL_AD, "full"))
        self.assertIsNone(sa._description)  # the content moved to the posting
        self.assertEqual(sa.posting.listing_key, "greenhouse:leg")
        # The uploaded full ad stays A's; B and the posting keep the summary.
        ua, ub = get("seek_a"), get("seek_b")
        self.assertEqual(ua.posting_id, ub.posting_id)
        self.assertIn("Uploaded by A.", ua.description)
        self.assertEqual((ub.description, ub.detail_status), (TEASER, "summary"))
        self.assertEqual((ua.posting.description, ua.posting.detail_status), (TEASER, "summary"))
        only = get("only_upload")
        self.assertIn("Nobody else.", only.description)
        self.assertEqual((only.posting.description, only.posting.detail_status, only.posting.title),
                         (None, "none", "Only Uploaded"))
        # A page imported from the browser stays a private job.
        imp = get("imported")
        self.assertEqual((imp.origin, imp.posting_id, imp.title), ("private", None, "Imported"))
        self.assertEqual(db.query(Posting).filter(Posting.url == "https://legacy-import.example/9").count(), 0)
        # Closed only when every copy was.
        self.assertIsNotNone(get("closed_both_a").closed_at)
        # The office read from the public ad is shared.
        self.assertEqual(get("office_a").posting.ad_office_text, "1 Denison St, North Sydney")
        self.assertEqual(get("office_a").office_text, "1 Denison St, North Sydney")
        # A source label the user typed stays theirs; the posting gets the name from the URL.
        labelled = get("labelled")
        self.assertEqual((labelled.company, labelled.posting.company), ("Our pet name", "Legacyco"))
        # Fit results, letters and chats point at the same job ids.
        self.assertEqual({f.job_id: f.score for f in db.query(FitResult).filter(FitResult.job_id.in_([sa.id, sb.id]))},
                         {sa.id: 77, sb.id: 33})
        self.assertEqual(db.query(CoverLetter).filter(CoverLetter.job_id == ua.id).one().text, "Dear Hiring Manager")
        thread = db.query(ChatThread).filter(ChatThread.workspace_id == a).one()
        db.close()
        with TestClient(app) as c:
            chat = c.get(f"/api/chats/{thread.id}", headers={"X-Workspace": str(a)}).json()
            self.assertEqual([j["title"] for j in chat["jobs"]], ["Program Manager", "Delivery Lead"])
            listed = {j["id"]: j for j in c.get("/api/jobs?include=excluded,hidden", headers={"X-Workspace": str(b)}).json()}
            self.assertEqual(listed[sb.id]["fit"]["score"], 33)
            self.assertTrue(listed[sb.id]["hidden"])
            c.delete(f"/api/workspaces/{a}")
            c.delete(f"/api/workspaces/{b}")
        self.assertTrue(pool_migration.backup_path().exists())

    def test_migration_idempotent(self):
        from backend import pool_migration

        out = pool_migration.migrate(batch_urls=2)
        self.assertEqual(out["state"], "done")
        self.assertGreaterEqual(out["rows"], len(self.ids))
        before = self.snapshot()
        self.assertEqual(pool_migration.migrate(), {"state": "done", "rows": 0})
        init_db()  # what every start does
        self.assertEqual(self.snapshot(), before)
        self.check()

    def test_migration_resumes_after_an_interruption(self):
        from backend import pool_migration

        real, calls = pool_migration.migrate_url, []

        def flaky(conn, url, careers):
            calls.append(url)
            if len(calls) == 4:
                raise RuntimeError("power cut")
            return real(conn, url, careers)

        unmigrated = text("SELECT COUNT(*) FROM jobs WHERE origin IS NULL")
        with engine.connect() as conn:
            total = conn.execute(unmigrated).scalar()
        with mock.patch.object(pool_migration, "migrate_url", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                pool_migration.migrate(batch_urls=2)
        with engine.connect() as conn:
            state = json.loads(conn.execute(text("SELECT value_json FROM app_settings WHERE key = 'job_pool_migration'")).scalar())
            left = conn.execute(unmigrated).scalar()
        self.assertEqual(state["state"], "running")
        self.assertGreater(left, 0)  # the failed batch was rolled back, the first one kept
        self.assertLess(left, total)
        self.assertEqual(pool_migration.migrate()["state"], "done")
        self.check()


class CompanyProfileFreshnessTest(PoolTestCase):
    NAME = "Freshco Pty Ltd"

    def response(self, payload: dict) -> dict:
        return {"status": "completed", "output": [
            {"type": "web_search_call", "results": [{"title": "News", "url": "https://news.example/freshco"},
                                                     {"title": "Reviews", "url": "https://www.glassdoor.com.au/Reviews/Freshco-E1.htm"}]},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload), "annotations": []}]}]}

    def full_payload(self, tag: str) -> dict:
        return {"official_name": "Freshco", "is_company": True, "business_model": f"Business {tag}.",
                "ownership": "Private.", "headquarters": "Sydney", "employee_count": "50",
                "employee_sentiment": {"summary": f"Sentiment {tag}.", "rating": "", "positives": [], "negatives": [],
                                       "sources": [{"title": "Reviews", "url": "https://www.glassdoor.com.au/Reviews/Freshco-E1.htm"}]},
                "controversies": [{"title": f"News {tag}", "year": "2026", "summary": "x",
                                   "sources": [{"title": "News", "url": "https://news.example/freshco"}]}],
                "controversy_note": f"Note {tag}.", "sources": [], "offices": []}

    def setUp(self):
        super().setUp()
        self.calls: list[dict] = []
        db = SessionLocal()
        row = db.get(CompanyProfile, company_key(self.NAME))
        if row is not None:
            db.delete(row)
            db.commit()
        db.close()

    def fake_search(self, cfg, *, instructions, input, **kw):
        self.calls.append({"instructions": instructions, "input": input})
        return self.response(self.full_payload(f"call{len(self.calls)}"))

    def research(self, ws: int, **payload) -> dict:
        with mock.patch.dict(os.environ, LLM_ENV), \
                mock.patch("backend.research.llm_providers.web_search", side_effect=self.fake_search), \
                mock.patch("backend.scorer.call_model", side_effect=AssertionError("no LLM chat expected")), offline():
            run = self.client.post("/api/companies/research", headers=self.h(ws), json=payload).json()
            return self.wait(run)["summary"]["research"]

    def profile(self) -> CompanyProfile:
        db = SessionLocal()
        try:
            row = db.get(CompanyProfile, company_key(self.NAME))
            db.expunge(row)
            return row
        finally:
            db.close()

    def age(self, **days) -> None:
        db = SessionLocal()
        row = db.get(CompanyProfile, company_key(self.NAME))
        for field, n in days.items():
            setattr(row, field, datetime.utcnow() - timedelta(days=n))
        db.commit()
        db.close()

    def test_base_kept_sections_refreshed_on_request_and_shared(self):
        from backend.research import INSTRUCTIONS

        a, b = self.workspace(), self.workspace()
        listing = [self.ad(40, url="https://fresh.example/1", company=self.NAME, work_mode="remote")]
        search(a, FakeAdapter("workable:freshco", listing))

        out = self.research(a, name=self.NAME)  # nothing cached: one full research
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["instructions"], INSTRUCTIONS)
        self.assertEqual(out["refreshed"], {self.NAME: ["base", "sentiment", "news"]})
        p = self.profile()
        self.assertEqual(p.business_model, "Business call1.")
        self.assertTrue(p.researched_at == p.sentiment_researched_at == p.news_researched_at)

        # B gets the shared profile once it has the company's job (a pool match), without researching.
        search(b)
        keyed = self.client.get("/api/companies", headers=self.h(b)).json()[company_key(self.NAME)]
        for field in ("researched_at", "sentiment_researched_at", "news_researched_at"):
            self.assertIsNotNone(keyed[field])
        self.assertEqual(keyed["business_model"], "Business call1.")
        out = self.research(b, name=self.NAME)  # everything is under a day old
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((out["refreshed"], out["up_to_date"], out["researched"]), ({}, [self.NAME], 1))

        # Ten days on: bulk "Fill missing info" leaves it (the base is under 30 days old)...
        self.age(researched_at=10, sentiment_researched_at=10, news_researched_at=10)
        before = self.profile()
        out = self.research(b, mode="missing")
        self.assertEqual((len(self.calls), out["requested"]), (1, 0))
        # ...and "research this company" refreshes only sentiment and news, with a narrower request.
        out = self.research(b, name=self.NAME)
        self.assertEqual(len(self.calls), 2)
        narrow = self.calls[1]["instructions"]
        self.assertNotEqual(narrow, INSTRUCTIONS)
        self.assertIn("employee_sentiment", narrow)
        self.assertIn("controversies", narrow)
        self.assertNotIn("business_model", narrow)
        self.assertEqual(out["refreshed"], {self.NAME: ["sentiment", "news"]})
        p = self.profile()
        self.assertEqual((p.business_model, p.researched_at), ("Business call1.", before.researched_at))
        self.assertEqual(json.loads(p.sentiment_json)["summary"], "Sentiment call2.")
        self.assertEqual(json.loads(p.controversies_json)[0]["title"], "News call2")
        self.assertGreater(p.sentiment_researched_at, before.sentiment_researched_at)
        self.assertGreater(p.news_researched_at, before.news_researched_at)
        # A sees B's refresh: the profile is shared.
        self.assertEqual(self.client.get("/api/companies", headers=self.h(a)).json()[company_key(self.NAME)]
                         ["sentiment"]["summary"], "Sentiment call2.")

        # Only news is stale: only news is asked for.
        self.age(news_researched_at=2)
        out = self.research(a, name=self.NAME)
        self.assertEqual(out["refreshed"], {self.NAME: ["news"]})
        self.assertNotIn("employee_sentiment", self.calls[2]["instructions"])
        self.assertEqual(json.loads(self.profile().sentiment_json)["summary"], "Sentiment call2.")

        # The base older than 30 days: a full research again.
        self.age(researched_at=31)
        out = self.research(a, name=self.NAME)
        self.assertEqual(self.calls[3]["instructions"], INSTRUCTIONS)
        self.assertEqual(out["refreshed"], {self.NAME: ["base", "sentiment", "news"]})
        self.assertEqual(self.profile().business_model, "Business call4.")

    def test_plan(self):
        from backend.research import plan

        now = datetime.utcnow()
        fresh = CompanyProfile(status="done", research_version=3, researched_at=now - timedelta(days=29),
                               sentiment_researched_at=now - timedelta(hours=2), news_researched_at=now - timedelta(days=3))
        self.assertEqual(plan(fresh), [])
        self.assertEqual(plan(fresh, on_request=True), ["news"])
        self.assertEqual(plan(None), ["base", "sentiment", "news"])
        self.assertEqual(plan(CompanyProfile(status="error")), ["base", "sentiment", "news"])
        self.assertEqual(plan(CompanyProfile(status="skipped"), on_request=True), [])
        old = CompanyProfile(status="done", research_version=2, researched_at=now)
        self.assertEqual(plan(old), ["base", "sentiment", "news"])

    def test_old_profiles_get_section_dates_from_their_research(self):
        from backend.db import _fix_legacy_data

        db = SessionLocal()
        when = datetime(2026, 9, 1, 12, 0)
        db.add(CompanyProfile(key=company_key(self.NAME), name=self.NAME, status="done", researched_at=when,
                              sentiment_json=json.dumps({"summary": "old"})))
        db.add(CompanyProfile(key="oldnosentimentco", name="Old No Sentiment Co", status="done", researched_at=when))
        db.commit()
        db.close()
        _fix_legacy_data()
        out = CompanyProfile.to_dict(self.profile())
        self.assertEqual((out["researched_at"], out["sentiment_researched_at"], out["news_researched_at"]),
                         (when.isoformat(),) * 3)
        db = SessionLocal()
        other = db.get(CompanyProfile, "oldnosentimentco")
        self.assertEqual((other.sentiment_researched_at, other.news_researched_at), (None, when))
        db.delete(other)
        db.commit()
        db.close()


class AppSettingCleanupTest(unittest.TestCase):
    def test_migration_state_is_recorded(self):
        db = SessionLocal()
        self.assertIn('"done"', db.get(AppSetting, "job_pool_migration").value_json)
        db.close()


if __name__ == "__main__":
    unittest.main()
