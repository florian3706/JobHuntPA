"""The browser extensions' server side (backend/extension.py, the key checks in backend/sessions.py and the page reader
backend/scraping/page_job.py; accounts design §19).

Run:  python3 -m unittest tests.test_extension -v
"""
from __future__ import annotations

import json
import re
import unittest
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend import crypto, extension, ratelimit, sessions  # noqa: E402
from backend.adapters.base import Posting  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import AuthSession, Job, SessionLocal, engine, utcnow  # noqa: E402
from backend.scraping import page_job  # noqa: E402

KEY_RE = re.compile(r"^jhpa_[A-Za-z0-9_-]{43}$")


@contextmanager
def offline():
    """No geocoder (no network)."""
    with ExitStack() as stack:
        stack.enter_context(mock.patch("backend.pipeline.geocode", return_value=None))
        stack.enter_context(mock.patch("backend.offices.geocode", return_value=None))
        yield


def jsonld_page(url="https://jobs.example.com/roles/42", title="Senior Data Analyst", company="Examplo",
                description=None, extra_nodes=()):
    description = description or ("<p>You will analyse data for the planning team. " * 8) + "</p>"
    node = {"@context": "https://schema.org", "@type": "JobPosting", "title": title, "url": url,
            "hiringOrganization": {"@type": "Organization", "name": company},
            "jobLocation": {"address": {"addressLocality": "Sydney", "addressRegion": "NSW", "addressCountry": "AU"}},
            "employmentType": "FULL_TIME", "datePosted": "2026-10-01", "description": description}
    nodes = [*extra_nodes, node]
    return ("<html><head><title>Senior Data Analyst | Examplo Careers</title>"
            f'<script type="application/ld+json">{json.dumps(nodes)}</script></head>'
            "<body><h1>Senior Data Analyst</h1></body></html>")


PLAIN_PAGE = """<!doctype html><html><head><title>Product Owner, Payments | Acme Jobs</title>
<meta property="og:title" content="Product Owner, Payments | Acme">
<meta property="og:site_name" content="Acme"></head>
<body><nav><a href="/">Home</a> <a href="/jobs">All jobs</a></nav>
<h1>Product Owner, Payments</h1><div class="job-location">Melbourne VIC</div>
<p class="salary">$140,000 - $160,000 + super</p>
<div class="job-description"><h2>About the role</h2><p>""" + ("You will own the payments roadmap with the team. " * 8) + """</p>
<ul><li>Run discovery</li><li>Write clear stories</li></ul></div>
<footer>Copyright Acme</footer></body></html>"""


def seek_page(job_id="81234567"):
    data = {"jobdetails": {"result": {
        "workArrangements": {"arrangements": [{"type": "HYBRID", "label": "Hybrid"}]},
        "job": {"id": job_id, "title": "Delivery Lead", "content": "<p>" + "Lead delivery of the program. " * 10 + "</p>",
                "location": {"label": "Parramatta, Sydney NSW"}, "advertiser": {"name": "Seekco"},
                "salary": {"label": "$150k"}, "listedAt": {"dateTimeUtc": "2026-10-02"},
                "classifications": [{"label": "Delivery"}]}}}}
    return f"<html><script>\n    window.SEEK_REDUX_DATA = {json.dumps(data)};\n</script><h1>Delivery Lead</h1></html>"


# ---------------------------------------------------------------------------
# Reading a page (no database)
# ---------------------------------------------------------------------------

class ReadPageTest(unittest.TestCase):
    def test_json_ld_takes_the_posting_about_this_page(self):
        other = {"@type": "JobPosting", "title": "Another job", "url": "https://jobs.example.com/roles/7",
                 "hiringOrganization": {"name": "Other"}}
        read = page_job.read_page("https://jobs.example.com/roles/42?utm_source=x", "",
                                  jsonld_page(extra_nodes=[other]))
        p = read.posting
        self.assertEqual(read.method, "jsonld")
        self.assertEqual((p.title, p.company, p.employment_type), ("Senior Data Analyst", "Examplo", "full_time"))
        self.assertEqual(p.url, "https://jobs.example.com/roles/42")  # the canonical address, without the tracking
        self.assertEqual(p.location_text, "Sydney, NSW, AU")
        self.assertEqual((read.description_from, p.detail_status), ("structured", "full"))
        self.assertIn("analyse data", p.description)

    def test_json_ld_without_a_matching_url_takes_the_first(self):
        read = page_job.read_page("https://jobs.example.com/view?id=9", "", jsonld_page(url="/roles/42"))
        self.assertEqual(read.posting.title, "Senior Data Analyst")
        self.assertEqual(read.posting.url, "https://jobs.example.com/roles/42")

    def test_a_json_ld_address_on_another_site_is_not_used(self):
        read = page_job.read_page("https://jobs.example.com/roles/42", "",
                                  jsonld_page(url="https://aggregator.example.net/x/1"))
        self.assertEqual(read.posting.url, "https://jobs.example.com/roles/42")

    def test_any_job_page_by_heuristics(self):
        read = page_job.read_page("https://careers.acme.example/jobs/77", "Product Owner, Payments | Acme Jobs",
                                  PLAIN_PAGE, "Home All jobs Product Owner ...", "")
        p = read.posting
        self.assertEqual(read.method, "page")
        self.assertEqual((p.title, p.company), ("Product Owner, Payments", "Acme"))
        self.assertEqual((p.location_text, p.salary_text), ("Melbourne VIC", "$140,000 - $160,000 + super"))
        self.assertEqual(read.description_from, "page")
        self.assertIn("payments roadmap", p.description)
        self.assertIn("- Run discovery", p.description)
        self.assertNotIn("Copyright", p.description)
        self.assertNotIn("All jobs", p.description)
        self.assertEqual(p.detail_status, "full")

    def test_titles_lose_the_site_name_and_the_company_falls_back_to_the_host(self):
        html = "<html><head><title>Graduate Engineer – Widgetworks Careers</title></head><body><p>Hi</p></body></html>"
        read = page_job.read_page("https://www.widgetworks.example.com/careers/1", "", html, "", "")
        self.assertEqual(read.posting.title, "Graduate Engineer")
        self.assertEqual(read.posting.company, "Widgetworks")
        for raw, want in (("Senior Analyst | Example", "Senior Analyst"), ("Example - Senior Analyst", "Senior Analyst"),
                          ("Engineer - Backend - Example Careers", "Engineer - Backend"),
                          ("Job Application for Data Lead at Example", "Data Lead"), ("Example Careers", ""),
                          ("Recruitment Coordinator", "Recruitment Coordinator")):
            self.assertEqual(page_job.clean_title(raw, {"example"}, "Example"), want, raw)

    def test_the_visible_text_is_the_description_when_the_page_has_no_main_content(self):
        text = "Barista wanted. " * 30
        read = page_job.read_page("https://cafe.example/jobs", "Barista | Cafe", "<html><body><div>x</div></body></html>",
                                  text, "")
        self.assertEqual(read.description_from, "page")
        self.assertTrue(read.posting.description.startswith("Barista wanted."))
        self.assertTrue(any("whole page" in n for n in read.notes))

    def test_selected_text_wins(self):
        read = page_job.read_page("https://careers.acme.example/jobs/77", "", PLAIN_PAGE, "", "  Only this part.\n\nAnd this. ")
        self.assertEqual(read.description_from, "selection")
        self.assertEqual(read.posting.description, "Only this part.\n\nAnd this.")
        self.assertEqual(read.posting.detail_status, "summary")
        self.assertIn("Selected text will be used as the description.", read.notes)
        read = page_job.read_page("https://jobs.example.com/roles/42", "", jsonld_page(), "", "Picked text")
        self.assertEqual((read.method, read.description_from, read.posting.description),
                         ("jsonld", "selection", "Picked text"))

    def test_the_users_fields_win_and_empty_ones_change_nothing(self):
        p = page_job.read_page("https://careers.acme.example/jobs/77", "", PLAIN_PAGE).posting
        page_job.apply_fields(p, {"title": " Lead Product Owner ", "company": "", "salary_text": "$170k",
                                  "employment_type": "Contract", "work_mode": "remote", "location_text": ""})
        self.assertEqual((p.title, p.company, p.salary_text, p.employment_type, p.work_mode, p.location_text),
                         ("Lead Product Owner", "Acme", "$170k", "contract", "remote", "Melbourne VIC"))

    def test_a_seek_job_page(self):
        read = page_job.read_page("https://www.seek.com.au/jobs?jobId=81234567", "", seek_page())
        p = read.posting
        self.assertEqual(read.method, "seek")
        self.assertEqual((p.title, p.company, p.work_mode, p.url),
                         ("Delivery Lead", "Seekco", "hybrid", "https://www.seek.com.au/job/81234567"))
        self.assertEqual(read.description_from, "structured")

    def test_a_page_with_nothing_has_no_title(self):
        read = page_job.read_page("https://blank.example/", "", "", "", "")
        self.assertEqual(read.posting.title, "")
        self.assertEqual(read.description_from, "")


# ---------------------------------------------------------------------------
# Keys and the API
# ---------------------------------------------------------------------------

class Base(unittest.TestCase):
    def setUp(self):
        ratelimit.BAD_KEY.reset()

    def account(self):
        acct = helpers.make_user()
        c = helpers.client(acct.email)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return acct, c

    def connect(self, c, label="Firefox on laptop") -> dict:
        r = c.post("/api/extension/keys", json={"label": label})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def key_client(self, key: str, **headers) -> TestClient:
        """A client that sends only the key: no cookie, no CSRF token."""
        k = TestClient(app)
        k.__enter__()
        self.addCleanup(k.__exit__, None, None, None)
        k.headers.update({"Authorization": f"Bearer {key}", **headers})
        return k

    def rows(self, user_id: int) -> list[AuthSession]:
        db = SessionLocal()
        try:
            out = db.query(AuthSession).filter(AuthSession.user_id == user_id,
                                               AuthSession.state == sessions.KEY_STATE).all()
            for row in out:
                db.expunge(row)
            return out
        finally:
            db.close()

    def age_sign_in(self, user_id: int, minutes: int) -> None:
        db = SessionLocal()
        try:
            db.query(AuthSession).filter(AuthSession.user_id == user_id, AuthSession.state == "active").update(
                {AuthSession.reauth_at: utcnow() - timedelta(minutes=minutes)})
            db.commit()
        finally:
            db.close()

    def job_count(self, ws: int) -> int:
        with engine.connect() as conn:
            return conn.execute(text("SELECT count(*) FROM jobs WHERE workspace_id = :w"), {"w": ws}).scalar()


class KeysTest(Base):
    def test_connecting_needs_a_session_and_a_recent_sign_in(self):
        anon = TestClient(app)
        with anon:
            r = anon.post("/api/extension/keys", json={"label": "x"})
        self.assertEqual((r.status_code, r.json()["code"]), (401, "signed_out"))
        acct, c = self.account()
        self.age_sign_in(acct.id, 20)
        r = c.post("/api/extension/keys", json={"label": "Chrome"})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        self.assertEqual(self.rows(acct.id), [])
        self.assertEqual(c.post("/api/auth/reauth", json={"password": acct.password}).status_code, 200)
        made = self.connect(c, "  Chrome   at work ")
        self.assertRegex(made["key"], KEY_RE)
        self.assertEqual(made["label"], "Chrome at work")
        self.assertEqual(set(made), {"id", "label", "created_at", "expires_at", "key"})
        self.assertRegex(made["expires_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_the_card_lists_and_revokes_keys(self):
        acct, c = self.account()
        made = self.connect(c)
        listed = c.get("/api/extension/keys").json()
        self.assertEqual(listed, [{"id": made["id"], "label": "Firefox on laptop", "created_at": made["created_at"],
                                   "last_used_at": None, "expires_at": made["expires_at"]}])
        self.assertNotIn("key", json.dumps(listed).replace('"key"', ""))
        k = self.key_client(made["key"])
        self.assertEqual(k.get("/api/extension/status").status_code, 200)
        self.assertIsNotNone(c.get("/api/extension/keys").json()[0]["last_used_at"])
        self.assertEqual(c.delete(f"/api/extension/keys/{made['id']}").json(), {"ok": True})
        self.assertEqual(c.get("/api/extension/keys").json(), [])
        r = k.get("/api/extension/status")
        self.assertEqual((r.status_code, r.json()["code"]), (401, "token_invalid"))
        self.assertEqual(c.delete(f"/api/extension/keys/{made['id']}").status_code, 404)

    def test_a_name_is_needed_and_has_a_limit(self):
        acct, c = self.account()
        for label in ("", "   ", None, "x" * 81):
            r = c.post("/api/extension/keys", json={"label": label})
            self.assertEqual((r.status_code, r.json().get("field")), (422, "label"), label)

    def test_nobody_else_can_see_or_revoke_a_key(self):
        acct, c = self.account()
        made = self.connect(c)
        other, c2 = self.account()
        self.assertEqual(c2.get("/api/extension/keys").json(), [])
        self.assertEqual(c2.delete(f"/api/extension/keys/{made['id']}").status_code, 404)
        self.assertEqual(self.key_client(made["key"]).get("/api/extension/status").status_code, 200)

    def test_what_is_stored_is_a_hash_and_a_wrap_never_the_key(self):
        acct, c = self.account()
        made = self.connect(c, "Laptop")
        secret = crypto.b64d(made["key"][5:])
        (row,) = self.rows(acct.id)
        self.assertEqual(row.id, crypto.extension_key_id(secret))
        self.assertNotEqual(row.id, crypto.session_id(secret))  # never the id a cookie with these bytes would have
        self.assertNotEqual(row.public_id, row.id)
        self.assertIsNone(row.wrapped_ik)
        self.assertIsNone(row.reauth_at)
        for blob in (bytes(row.wrapped_dek), bytes(row.label_enc)):
            self.assertNotIn(secret, blob)
            self.assertNotIn(helpers.key_of(acct.email)[1], blob)
        self.assertNotIn(b"Laptop", bytes(row.label_enc))
        self.assertEqual(crypto.unwrap_extension_dek(secret, row.wrapped_dek), helpers.key_of(acct.email)[1])
        self.assertEqual(row.absolute_expires_at - row.created_at, sessions.KEY_ABSOLUTE)

    def test_a_key_is_not_a_cookie_and_a_cookie_is_not_a_key(self):
        acct, c = self.account()
        made = self.connect(c)
        anon = TestClient(app)
        with anon:
            anon.cookies.set("jh_session", made["key"][5:])
            r = anon.get("/api/extension/status")
            self.assertEqual((r.status_code, r.json()["code"]), (401, "signed_out"))
        cookie = c.cookies.get("jh_session")
        r = self.key_client(sessions.KEY_PREFIX + cookie).get("/api/extension/status")
        self.assertEqual((r.status_code, r.json()["code"]), (401, "token_invalid"))

    def test_too_many_keys(self):
        acct, c = self.account()
        with mock.patch.object(extension, "MAX_KEYS", 2):
            self.connect(c)
            self.connect(c)
            r = c.post("/api/extension/keys", json={"label": "third"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "too_many_keys"))


class KeyAuthTest(Base):
    def test_status_with_the_key_alone(self):
        acct, c = self.account()
        second = c.post("/api/workspaces", json={"name": "Plan B"}).json()["id"]
        made = self.connect(c)
        r = self.key_client(made["key"]).get("/api/extension/status")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual({k: body[k] for k in ("ok", "api", "label", "expires_at", "default_workspace")},
                         {"ok": True, "api": 1, "label": "Firefox on laptop", "expires_at": made["expires_at"],
                          "default_workspace": acct.workspace_id})
        self.assertEqual(body["workspaces"], [{"id": acct.workspace_id, "name": "My job search"},
                                              {"id": second, "name": "Plan B"}])  # sealed names: the key opens them
        self.assertRegex(body["app_version"], r"^\d+\.\d+\.\d+$")

    def test_the_allowed_endpoints_need_no_csrf_token_or_origin(self):
        acct, c = self.account()
        k = self.key_client(self.connect(c)["key"], Origin="chrome-extension://abcdefghijklmnop",
                            **{"Sec-Fetch-Site": "cross-site"})
        r = k.post("/api/sources", json={"url": "https://careers.example.org/jobs", "label": "Example"})
        self.assertEqual(r.status_code, 201, r.text)
        source = r.json()
        self.assertEqual(k.post("/api/sources", json={"url": "https://careers.example.org/jobs"}).status_code, 409)
        self.assertEqual(k.post("/api/sources", json={"url": "ftp://nope"}).status_code, 422)
        self.assertEqual([s["id"] for s in k.get("/api/sources").json()], [source["id"]])
        fake = mock.Mock(method="careers page", notes=[], list_postings=lambda: [
            Posting(url="https://careers.example.org/jobs/1", title="Analyst", location_text="Sydney")])
        with mock.patch("backend.sources.source_adapter", return_value=fake):
            r = k.post(f"/api/sources/{source['id']}/test")
        self.assertEqual(r.json()["count"], 1, r.text)
        self.assertEqual([s["label"] for s in c.get("/api/sources").json()], ["Example"])

    def test_everything_else_is_refused(self):
        acct, c = self.account()
        k = self.key_client(self.connect(c)["key"])
        made_id = c.get("/api/extension/keys").json()[0]["id"]
        source = c.post("/api/sources", json={"url": "https://careers.example.org/x"}).json()["id"]
        for method, path, body in (("get", "/api/jobs", None), ("get", "/api/auth/security", None),
                                   ("get", "/api/auth/me", None), ("get", "/api/workspaces", None),
                                   ("get", "/api/extension/keys", None), ("post", "/api/extension/keys", {"label": "x"}),
                                   ("delete", f"/api/extension/keys/{made_id}", None),
                                   ("post", "/api/auth/password", {"current_password": "x", "new_password": "y" * 12}),
                                   ("post", "/api/auth/sessions/end-others", None), ("post", "/api/auth/logout", None),
                                   ("patch", f"/api/sources/{source}", {"enabled": False}),
                                   ("delete", f"/api/sources/{source}", None), ("get", "/api/health", None),
                                   ("post", "/api/account/background", {"enabled": True}),
                                   ("get", "/api/account/export", None), ("post", "/api/import/page", None),
                                   ("get", "/api/admin/users", None), ("get", "/api/extension/status/x", None)):
            r = getattr(k, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual((r.status_code, r.json().get("code")), (403, "token_scope"), f"{method} {path}")
        self.assertEqual(len(c.get("/api/sources").json()), 1)
        self.assertEqual(len(self.rows(acct.id)), 1)

    def test_the_admins_key_is_refused_by_admin_endpoints_too(self):
        with helpers.client() as admin:
            made = self.connect(admin, "Admin laptop")
            r = self.key_client(made["key"]).get("/api/admin/users")
            self.assertEqual((r.status_code, r.json()["code"]), (403, "token_scope"))
            admin.delete(f"/api/extension/keys/{made['id']}")

    def test_a_bad_key_is_401_and_the_cookie_is_never_used_instead(self):
        acct, c = self.account()
        made = self.connect(c)
        c.headers["Authorization"] = "Bearer jhpa_" + "A" * 43  # a signed-in client, with a wrong key
        try:
            for method, path in (("get", "/api/extension/status"), ("get", "/api/jobs"), ("get", "/api/auth/me")):
                r = getattr(c, method)(path)
                self.assertEqual((r.status_code, r.json()["code"]), (401, "token_invalid"), path)
            for bad in ("", "jhpa_", "jhpa_short", made["key"][:-1], made["key"] + "A", "jhpa_" + "*" * 43,
                        made["key"].replace("jhpa_", "jhpb_")):
                c.headers["Authorization"] = f"Bearer {bad}"
                self.assertEqual(c.get("/api/extension/status").status_code, 401, bad)
            c.headers["Authorization"] = "Basic dXNlcjpwYXNz"  # another scheme (a proxy's): the cookie is used
            self.assertEqual(c.get("/api/auth/me").status_code, 200)
        finally:
            c.headers.pop("Authorization", None)
        self.assertEqual(c.get("/api/auth/me").status_code, 200, "the cookie still works on its own")

    def test_an_expired_key_stops_working(self):
        acct, c = self.account()
        made = self.connect(c)
        k = self.key_client(made["key"])
        self.assertEqual(k.get("/api/extension/status").status_code, 200)
        db = SessionLocal()
        try:
            db.query(AuthSession).filter(AuthSession.public_id == made["id"]).update(
                {AuthSession.idle_expires_at: utcnow() - timedelta(seconds=1)})
            db.commit()
        finally:
            db.close()
        self.assertEqual(k.get("/api/extension/status").status_code, 401)
        self.assertEqual(self.rows(acct.id), [], "an expired key's row is deleted when it is next tried")

    def test_use_moves_the_idle_expiry_on(self):
        acct, c = self.account()
        made = self.connect(c)
        db = SessionLocal()
        try:
            db.query(AuthSession).filter(AuthSession.public_id == made["id"]).update(
                {AuthSession.idle_expires_at: utcnow() + timedelta(days=1)})
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.key_client(made["key"]).get("/api/extension/status").status_code, 200)
        (row,) = self.rows(acct.id)
        self.assertGreater(row.idle_expires_at, utcnow() + timedelta(days=59))

    def test_a_key_never_counts_as_a_recent_sign_in(self):
        auth = sessions.AuthUser(id=1, role="admin", session_id="x", persistent=False, reauth_at=utcnow(), csrf="",
                                 key=True)
        self.assertFalse(sessions.has_recent_auth(auth))
        with self.assertRaises(sessions.AuthProblem):
            sessions.require_recent_auth(auth)

    def test_a_cookie_request_to_the_same_paths_still_needs_its_csrf_token(self):
        acct, c = self.account()
        token = c.headers.pop("X-CSRF-Token")
        try:
            for path, body in (("/api/extension/jobs", {"url": "https://x.example/1", "fields": {"title": "T"}}),
                               ("/api/extension/preview", {"url": "https://x.example/1"}),
                               ("/api/sources", {"url": "https://careers.example.org/y"})):
                r = c.post(path, json=body)
                self.assertEqual((r.status_code, r.json()["code"]), (403, "csrf"), path)
            r = c.post("/api/sources", json={"url": "https://careers.example.org/y"},
                       headers={"X-CSRF-Token": token, "Origin": "https://evil.example"})
            self.assertEqual((r.status_code, r.json()["code"]), (403, "bad_origin"))
        finally:
            c.headers["X-CSRF-Token"] = token
        r = c.post("/api/extension/preview", json={"url": "https://careers.acme.example/jobs/77", "html": PLAIN_PAGE})
        self.assertEqual(r.status_code, 200, "a session may use it with its token")

    def test_workspaces_are_the_accounts_own(self):
        acct, c = self.account()
        other, _ = self.account()
        k = self.key_client(self.connect(c)["key"])
        page = {"url": "https://careers.acme.example/jobs/77", "html": PLAIN_PAGE}
        for path in ("/api/extension/preview", "/api/extension/jobs"):
            r = k.post(path, json=page, headers={"X-Workspace": str(other.workspace_id)})
            self.assertEqual(r.status_code, 404, path)
        r = k.get("/api/sources", headers={"X-Workspace": str(other.workspace_id)})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.job_count(other.workspace_id), 0)
        second = c.post("/api/workspaces", json={"name": "Plan B"}).json()["id"]
        with offline():
            r = k.post("/api/extension/jobs", json=page, headers={"X-Workspace": str(second)})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(r.json()["link"].endswith(f"&ws={second}"))
        self.assertEqual((self.job_count(second), self.job_count(acct.workspace_id)), (1, 0))

    def test_bad_keys_from_one_address_are_slowed_down(self):
        k = self.key_client("jhpa_" + "B" * 43)
        codes = [k.get("/api/extension/status").status_code for _ in range(21)]
        self.assertEqual(codes[:20], [401] * 20)
        self.assertEqual(codes[20], 429)
        self.assertIn("Retry-After", k.get("/api/extension/status").headers)
        ratelimit.BAD_KEY.reset()


class RevokedTogetherTest(Base):
    """A key is a session row: whatever signs an account out everywhere disconnects its extensions."""

    def status(self, key: str) -> int:
        return self.key_client(key).get("/api/extension/status").status_code

    def test_a_password_change_that_signs_out_the_other_devices(self):
        acct, c = self.account()
        key = self.connect(c)["key"]
        r = c.post("/api/auth/password", json={"current_password": acct.password, "new_password": "another-pass-456",
                                               "sign_out_others": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.status(key), 200, "kept when the other devices stay signed in")
        r = c.post("/api/auth/password", json={"current_password": "another-pass-456",
                                               "new_password": "a-third-pass-789", "sign_out_others": True})
        self.assertEqual(r.status_code, 200, r.text)
        helpers._passwords[acct.email] = "a-third-pass-789"
        self.assertEqual(self.status(key), 401)

    def test_sign_out_everywhere_else(self):
        acct, c = self.account()
        key = self.connect(c)["key"]
        self.assertEqual(c.post("/api/auth/sessions/end-others").status_code, 200)
        self.assertEqual(self.status(key), 401)
        self.assertEqual(c.get("/api/auth/me").status_code, 200)

    def test_an_admin_turning_the_account_off(self):
        acct, c = self.account()
        key = self.connect(c)["key"]
        with helpers.client() as admin:
            self.assertEqual(admin.post(f"/api/admin/users/{acct.id}/disable").status_code, 200)
            self.assertEqual(self.status(key), 401)
            self.assertEqual(self.rows(acct.id), [])
            self.assertEqual(admin.post(f"/api/admin/users/{acct.id}/enable").status_code, 200)
        self.assertEqual(self.status(key), 401, "turning it on again doesn't bring the key back")


class PagesTest(Base):
    def setUp(self):
        super().setUp()
        self.acct, self.c = self.account()
        self.k = self.key_client(self.connect(self.c)["key"])
        stack = ExitStack()
        stack.enter_context(offline())
        self.addCleanup(stack.close)

    def jobs(self, include="excluded,hidden") -> list[dict]:
        return self.c.get(f"/api/jobs?include={include}").json()

    def test_preview_reads_the_page_and_stores_nothing(self):
        r = self.k.post("/api/extension/preview", json={
            "url": "https://careers.acme.example/jobs/77", "page_title": "Product Owner, Payments | Acme Jobs",
            "html": PLAIN_PAGE, "text": "...", "selection": "", "fields": {"title": "ignored"}})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(set(body), {"method", "job", "existing", "notes"})
        self.assertEqual(body["method"], "page")
        self.assertIsNone(body["existing"])
        job = body["job"]
        self.assertEqual(set(job), {"title", "company", "location_text", "salary_text", "employment_type", "work_mode",
                                    "posted_at", "url", "description_preview", "description_chars",
                                    "description_from"})
        self.assertEqual((job["title"], job["company"], job["location_text"], job["description_from"]),
                         ("Product Owner, Payments", "Acme", "Melbourne VIC", "page"))
        self.assertIn(job["work_mode"], page_job.WORK_MODES)
        self.assertEqual(job["description_chars"], len(job["description_preview"]))
        self.assertEqual(self.job_count(self.acct.workspace_id), 0)

    def test_preview_long_descriptions_are_cut_for_the_popup(self):
        long_ad = "<p>" + "Plenty to say about this role. " * 200 + "</p>"
        r = self.k.post("/api/extension/preview", json={"url": "https://jobs.example.com/roles/42",
                                                        "html": jsonld_page(description=long_ad)})
        job = r.json()["job"]
        self.assertEqual(len(job["description_preview"]), extension.PREVIEW_CHARS)
        self.assertGreater(job["description_chars"], 6000)
        self.assertEqual((r.json()["method"], job["description_from"]), ("jsonld", "structured"))

    def test_no_title_is_422_unless_one_is_given(self):
        page = {"url": "https://blank.example/x", "html": "<html><body><p>Nothing here</p></body></html>"}
        r = self.k.post("/api/extension/preview", json=page)
        self.assertEqual((r.status_code, r.json()["field"]), (422, "title"))
        self.assertEqual(self.k.post("/api/extension/jobs", json=page).status_code, 422)
        r = self.k.post("/api/extension/preview", json={**page, "fields": {"title": "Typed title"}})
        self.assertEqual(r.status_code, 200)
        r = self.k.post("/api/extension/jobs", json={**page, "fields": {"title": "Typed title"}})
        self.assertEqual((r.status_code, r.json()["title"]), (201, "Typed title"))

    def test_saving_makes_a_private_job_then_updates_it(self):
        page = {"url": "https://careers.acme.example/jobs/77", "html": PLAIN_PAGE,
                "fields": {"title": "", "company": "Acme Payments", "work_mode": "hybrid", "employment_type": "Full time"}}
        r = self.k.post("/api/extension/jobs", json=page)
        self.assertEqual(r.status_code, 201, r.text)
        saved = r.json()
        self.assertEqual(set(saved), {"job_id", "created", "title", "company", "link"})
        self.assertEqual((saved["created"], saved["title"], saved["company"]),
                         (True, "Product Owner, Payments", "Acme Payments"))
        self.assertEqual(saved["link"], f"/job.html?id={saved['job_id']}&ws={self.acct.workspace_id}")
        with helpers.as_account(self.acct):
            db = SessionLocal()
            try:
                job = db.get(Job, saved["job_id"])
                self.assertEqual((job.origin, job.posting_id, job.source, job.work_mode, job.employment_type,
                                  job.detail_status), ("private", None, "browser", "hybrid", "full_time", "full"))
                self.assertIsNone(job.duplicate_of)
            finally:
                db.close()
        full = self.c.get(f"/api/jobs/{saved['job_id']}").json()  # the session reads what the key sealed
        self.assertIn("payments roadmap", full["description"])
        self.assertEqual(full["salary_text"], "$140,000 - $160,000 + super")

        preview = self.k.post("/api/extension/preview", json=page).json()
        self.assertEqual(preview["existing"], {"id": saved["job_id"], "title": "Product Owner, Payments",
                                               "status": "to_review", "link": saved["link"]})
        r = self.k.post("/api/extension/jobs", json={**page, "fields": {"salary_text": "$175,000"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["job_id"], r.json()["created"]), (saved["job_id"], False))
        self.assertEqual(self.c.get(f"/api/jobs/{saved['job_id']}").json()["salary_text"], "$175,000")
        self.assertEqual(self.job_count(self.acct.workspace_id), 1)

    def test_a_saved_job_is_filtered_and_waits_for_a_score(self):
        from backend.scorer import select_jobs

        r = self.c.put("/api/dealbreakers", json={"industries": [], "keywords": ["blockchain"]})
        self.assertEqual(r.status_code, 200, r.text)
        bad = self.k.post("/api/extension/jobs", json={
            "url": "https://jobs.example.com/roles/42", "html": jsonld_page(), "selection": "A blockchain job. " * 20})
        good = self.k.post("/api/extension/jobs", json={"url": "https://careers.acme.example/jobs/77", "html": PLAIN_PAGE})
        self.assertEqual((bad.status_code, good.status_code), (201, 201))
        listed = {j["id"]: j for j in self.jobs()}
        self.assertTrue(listed[bad.json()["job_id"]]["excluded_reason"])
        self.assertIsNone(listed[good.json()["job_id"]]["excluded_reason"])
        with helpers.as_account(self.acct):
            db = SessionLocal()
            try:
                waiting = select_jobs(db, "pending", "x", self.acct.workspace_id)
            finally:
                db.close()
        self.assertIn(good.json()["job_id"], waiting)
        self.assertNotIn(bad.json()["job_id"], waiting)

    def test_the_same_job_on_another_site_is_merged_and_the_listed_one_is_linked(self):
        first = self.k.post("/api/extension/jobs", json={"url": "https://careers.acme.example/jobs/77", "html": PLAIN_PAGE})
        again = self.k.post("/api/extension/jobs", json={"url": "https://board.example.net/acme/77", "html": PLAIN_PAGE,
                                                         "fields": {"company": "Acme"}})
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(again.json()["job_id"], first.json()["job_id"], "the copy is merged into the first")
        listed = [j["id"] for j in self.jobs()]
        self.assertEqual(listed, [first.json()["job_id"]])
        preview = self.k.post("/api/extension/preview", json={"url": "https://board.example.net/acme/77",
                                                              "html": PLAIN_PAGE}).json()
        self.assertEqual(preview["existing"]["id"], first.json()["job_id"])

    def test_a_seek_page_is_stored_under_seeks_address(self):
        r = self.k.post("/api/extension/jobs", json={"url": "https://www.seek.com.au/jobs?jobId=81234567",
                                                     "html": seek_page()})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.c.get(f"/api/jobs/{r.json()['job_id']}").json()["url"],
                         "https://www.seek.com.au/job/81234567")

    def test_bad_input(self):
        for body, field in (({"html": PLAIN_PAGE}, "url"), ({"url": "ftp://files.example/x"}, "url"),
                            ({"url": "javascript:alert(1)"}, "url"), ({"url": "https://x.example/" + "a" * 2050}, "url"),
                            ({"url": "https://x.example/1", "page_title": "t" * 1001}, "page_title"),
                            ({"url": "https://x.example/1", "fields": {"title": "t" * 501}}, "title"),
                            ({"url": "https://x.example/1", "fields": {"work_mode": "moon"}}, "work_mode"),
                            ({"url": "https://x.example/1", "html": 5}, "html")):
            for path in ("/api/extension/preview", "/api/extension/jobs"):
                r = self.k.post(path, json=body)
                self.assertEqual((r.status_code, r.json().get("field")), (422, field), (path, body))
        r = self.k.post("/api/extension/jobs", content=b"not json", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.job_count(self.acct.workspace_id), 0)

    def test_pages_over_the_limits_are_refused_with_413(self):
        r = self.k.post("/api/extension/preview", json={"url": "https://x.example/1", "html": "a" * 3_000_001})
        self.assertEqual((r.status_code, r.json()["code"]), (413, "too_large"))
        r = self.k.post("/api/extension/jobs", json={"url": "https://x.example/1", "selection": "a" * 100_001})
        self.assertEqual(r.status_code, 413)
        with mock.patch.object(extension, "MAX_BODY", 1000), \
                mock.patch("backend.extension.json.loads", side_effect=AssertionError("parsed")) as parsed:
            r = self.k.post("/api/extension/jobs", json={"url": "https://x.example/1", "text": "a" * 2000})
            self.assertEqual(r.status_code, 413)
            r = self.k.post("/api/extension/jobs", content=b"x" * 20, headers={"Content-Length": "999999999"})
            self.assertEqual(r.status_code, 413)
            parsed.assert_not_called()
        self.assertEqual(self.job_count(self.acct.workspace_id), 0)


class SettingsCardTest(unittest.TestCase):
    """frontend/extension.js: the Settings card (no browser needed)."""

    @classmethod
    def setUpClass(cls):
        root = helpers.ROOT / "frontend"
        cls.js = (root / "extension.js").read_text(encoding="utf-8")
        cls.index = (root / "index.html").read_text(encoding="utf-8")

    def test_it_fills_its_own_slot_after_the_shared_scripts(self):
        self.assertIn('<div id="account-extension-slot"></div>', self.index)
        self.assertLess(self.index.index('src="./common.js"'), self.index.index('src="./extension.js"'))
        self.assertIn("getElementById('account-extension-slot')", self.js)

    def test_connecting_asks_for_the_password_and_shows_the_address_and_the_key_once(self):
        line = next(l for l in self.js.splitlines() if "'/api/extension/keys'" in l and "POST" in l)
        self.assertIn("withRecentAuth", line)
        self.assertIn("window.location.origin", self.js)
        for text in ("Copy address", "Copy key", "shown only now", "Disconnect",
                     "https://github.com/florian3706/JobHuntPA-chrome-extension",
                     "https://github.com/florian3706/JobHuntPA-firefox-extension"):
            self.assertIn(text, self.js)

    def test_it_keeps_the_content_security_policy(self):
        self.assertNotRegex(self.js, r"\son[a-z]+=|style=|<script|<style")
        self.assertNotIn("localStorage", self.js, "the key is never kept in the browser")


if __name__ == "__main__":
    unittest.main()
