"""The browser extensions' server side (backend/extension.py and the key checks in backend/sessions.py; accounts design
§19).

Run:  python3 -m unittest tests.test_extension -v
"""
from __future__ import annotations

import json
import re
import threading
import time
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

KEY_RE = re.compile(r"^jhpa_[A-Za-z0-9_-]{43}$")
PAGE_URL = "https://careers.acme.example/jobs/77"
AD = "About the role\n\n" + " ".join(["You will own the payments roadmap with the team."] * 8) + "\n\n- Run discovery\n- Write stories"


@contextmanager
def offline():
    """No geocoder (no network)."""
    with ExitStack() as stack:
        stack.enter_context(mock.patch("backend.pipeline.geocode", return_value=None))
        stack.enter_context(mock.patch("backend.offices.geocode", return_value=None))
        yield


def page(address: str = PAGE_URL, canonical_url: str = "", **job) -> dict:
    """A request as the extension sends it: the job it read in the browser (these values, over a typical job)."""
    values = {"title": "Product Owner, Payments", "company": "Acme", "location_text": "Melbourne VIC",
              "salary_text": "$140,000 - $160,000 + super", "employment_type": "Full time", "work_mode": "",
              "posted_at": "2026-10-01", "url": "", "description": AD, "description_from": "site", "method": "site"}
    values.update(job)
    return {"url": address, "canonical_url": canonical_url, "job": values}


# ---------------------------------------------------------------------------
# The address a job is stored under (no database)
# ---------------------------------------------------------------------------

class StoredUrlTest(unittest.TestCase):
    def test_a_seek_job_is_stored_under_seeks_own_address_with_its_id(self):
        want = ("https://www.seek.com.au/job/81234567", "81234567")
        for url in ("https://www.seek.com.au/job/81234567", "https://www.seek.com.au/job/81234567?type=standard#sol=a",
                    "https://www.seek.com.au/job/81234567/apply", "https://seek.com.au/jobs?keywords=x&jobId=81234567",
                    "https://www.seek.co.nz/job/81234567", "https://nz.seek.co.nz/jobs?jobId=81234567"):
            self.assertEqual(extension.stored_url(url), want, url)
        # Judged from the page's own address only: the job's or canonical address can't make it a SEEK job
        self.assertEqual(extension.stored_url("https://www.seek.com.au/job/81234567", "https://www.seek.com.au/job/9",
                                              "https://www.seek.com.au/job/8"), want)

    def test_only_seeks_own_sites_and_only_all_digit_ids(self):
        for url in ("https://evil.example/job/81234567", "https://seek.com.au.evil.example/job/1",
                    "https://notseek.com.au/job/1", "https://www.seek.com.au@evil.example/job/1",
                    "https://www.seek.com.au/job/12a", "https://www.seek.com.au/job/١٢٣",
                    "https://www.seek.com.au/jobs?jobId=1x", "https://www.seek.com.au/jobs/in-Sydney",
                    "https://www.seek.com.au/x/job/1", "https://www.seek.com.au/job/" + "1" * 21):
            self.assertEqual(extension.stored_url(url), (url, ""), url)
        page_url = "https://evil.example/x"
        self.assertEqual(extension.stored_url(page_url, "https://www.seek.com.au/job/1",
                                              "https://www.seek.com.au/job/1"), (page_url, ""))

    def test_the_jobs_own_then_the_canonical_address_on_the_very_same_host(self):
        url = "https://jobs.example.com/roles/42?utm_source=x"
        own, canonical = "https://jobs.example.com/roles/42", "https://jobs.example.com/c/42"
        self.assertEqual(extension.stored_url(url, own, canonical), (own, ""))
        self.assertEqual(extension.stored_url(url, "", canonical), (canonical, ""))
        self.assertEqual(extension.stored_url(url, "/roles/42"), (own, ""), "a relative address is the page's site")
        self.assertEqual(extension.stored_url(url, "https://jobs.example.com:443/roles/42")[0],
                         "https://jobs.example.com:443/roles/42", "the scheme's own port is the same port")
        for other in ("https://jobs.example.com@www.seek.com.au/job/1", "https://jobs.example.com:8443/roles/42",
                      "https://evil.jobs.example.com/roles/42", "https://www.jobs.example.com/roles/42",
                      "https://www.seek.com.au/job/1", "http://user:pw@jobs.example.com/roles/42",
                      "//aggregator.example.net/x/1", "javascript:alert(1)", "ftp://jobs.example.com/x"):
            self.assertEqual(extension.stored_url(url, other), (url, ""), other)
            self.assertEqual(extension.stored_url(url, "", other), (url, ""), other)
            self.assertEqual(extension.stored_url(url, other, canonical), (canonical, ""), other)
        self.assertEqual(extension.stored_url("https://u:p@jobs.example.com/1", own), ("https://u:p@jobs.example.com/1", ""))

    def test_the_posting_is_the_browsers_values_trimmed(self):
        body = extension.PageIn.model_validate(page(
            title="  Product   Owner ", employment_type="Contract / Temp", work_mode="", description="  a  \n\n\n\nb "))
        p = extension.posting_of(body)
        self.assertEqual((p.url, p.title, p.employment_type, p.work_mode, p.description, p.detail_status),
                         (PAGE_URL, "Product Owner", "contract", "unknown", "a\n\nb", "summary"))
        p = extension.posting_of(extension.PageIn.model_validate(page(work_mode="remote", employment_type="what")))
        self.assertEqual((p.work_mode, p.employment_type, p.detail_status, p.posted_at), ("remote", None, "full",
                                                                                        "2026-10-01"))
        p = extension.posting_of(extension.PageIn.model_validate(page(description="")))
        self.assertEqual(p.detail_status, "none")


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

    def test_the_limit_holds_for_requests_at_the_same_time(self):
        acct, c = self.account()
        counted = threading.Barrier(2)
        live_keys = extension._live_keys

        def slow_count(db, user_id):
            out = live_keys(db, user_id)
            try:
                counted.wait(timeout=1)  # both requests counted before either adds its key (if they can)
            except threading.BrokenBarrierError:
                pass
            return out

        codes = []
        with mock.patch.object(extension, "MAX_KEYS", 1), mock.patch.object(extension, "_live_keys", slow_count):
            threads = [threading.Thread(target=lambda: codes.append(
                c.post("/api/extension/keys", json={"label": "at once"}).status_code)) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sorted(codes), [201, 409])
        self.assertEqual(len(self.rows(acct.id)), 1)


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
            for bad in ("jhpa_", "jhpa_short", made["key"][:-1], made["key"] + "A", "jhpa_" + "*" * 43):
                c.headers["Authorization"] = f"Bearer {bad}"
                self.assertEqual(c.get("/api/extension/status").status_code, 401, bad)
            # Not a key (a proxy's sign-in, another scheme): the cookie is used
            for other in ("Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig",
                          "Bearer " + made["key"].replace("jhpa_", "jhpb_")):
                c.headers["Authorization"] = other
                self.assertEqual(c.get("/api/auth/me").status_code, 200, other)
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
            for path, body in (("/api/extension/jobs", page("https://x.example/1")),
                               ("/api/extension/preview", {"url": "https://x.example/1"}),
                               ("/api/sources", {"url": "https://careers.example.org/y"})):
                r = c.post(path, json=body)
                self.assertEqual((r.status_code, r.json()["code"]), (403, "csrf"), path)
            r = c.post("/api/sources", json={"url": "https://careers.example.org/y"},
                       headers={"X-CSRF-Token": token, "Origin": "https://evil.example"})
            self.assertEqual((r.status_code, r.json()["code"]), (403, "bad_origin"))
        finally:
            c.headers["X-CSRF-Token"] = token
        r = c.post("/api/extension/preview", json=page())
        self.assertEqual(r.status_code, 200, "a session may use it with its token")

    def test_workspaces_are_the_accounts_own(self):
        acct, c = self.account()
        other, _ = self.account()
        k = self.key_client(self.connect(c)["key"])
        body = page()
        for path in ("/api/extension/preview", "/api/extension/jobs"):
            r = k.post(path, json=body, headers={"X-Workspace": str(other.workspace_id)})
            self.assertEqual(r.status_code, 404, path)
        r = k.get("/api/sources", headers={"X-Workspace": str(other.workspace_id)})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.job_count(other.workspace_id), 0)
        second = c.post("/api/workspaces", json={"name": "Plan B"}).json()["id"]
        with offline():
            r = k.post("/api/extension/jobs", json=body, headers={"X-Workspace": str(second)})
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

    def test_bad_keys_dont_lock_out_a_good_key_from_the_same_address(self):
        acct, c = self.account()
        good = self.key_client(self.connect(c)["key"])
        bad = self.key_client("jhpa_" + "B" * 43)
        for _ in range(20):
            self.assertEqual(bad.get("/api/extension/status").status_code, 401)
        self.assertEqual(bad.get("/api/extension/status").status_code, 429)
        r = good.get("/api/extension/status")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(good.post("/api/extension/preview", json=page()).status_code, 200)
        r = bad.get("/api/extension/status")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "rate_limited"), "the bad one is still refused")
        self.assertIn("Retry-After", r.headers)
        ratelimit.BAD_KEY.reset()

    def test_a_sign_in_proxys_bearer_token_goes_the_cookie_way(self):
        """Auth proxies (oauth2-proxy, Authelia, ...) pass their own ``Authorization: Bearer <JWT>`` on: it isn't an
        extension key, so the session cookie works as without it, with its Origin and CSRF checks, and nothing is
        counted as a bad key."""
        jwt = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.c2lnbmF0dXJl"
        acct, c = self.account()
        c.headers["Authorization"] = jwt
        try:
            for _ in range(25):
                self.assertEqual(c.get("/api/jobs").status_code, 200)
            r = c.post("/api/sources", json={"url": "https://careers.example.org/proxy"})
            self.assertEqual(r.status_code, 201, r.text)
            token = c.headers.pop("X-CSRF-Token")
            try:
                r = c.post("/api/sources", json={"url": "https://careers.example.org/proxy2"})
                self.assertEqual((r.status_code, r.json()["code"]), (403, "csrf"))
                r = c.post("/api/sources", json={"url": "https://careers.example.org/proxy2"},
                           headers={"X-CSRF-Token": token, "Origin": "https://evil.example"})
                self.assertEqual((r.status_code, r.json()["code"]), (403, "bad_origin"))
            finally:
                c.headers["X-CSRF-Token"] = token
        finally:
            c.headers.pop("Authorization", None)
        with TestClient(app) as anon:
            anon.headers["Authorization"] = jwt
            self.assertEqual(anon.get("/api/health").status_code, 200)
            r = anon.get("/api/jobs")
            self.assertEqual((r.status_code, r.json()["code"]), (401, "signed_out"))
        bad = self.key_client("jhpa_" + "B" * 43)
        self.assertEqual(bad.get("/api/extension/status").status_code, 401, "none of those counted as bad keys")


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

    def stored(self, job_id: int) -> Job:
        with helpers.as_account(self.acct):
            db = SessionLocal()
            try:
                job = db.get(Job, job_id)
                db.expunge(job)
                return job
            finally:
                db.close()

    def test_preview_says_the_address_and_the_existing_job_and_stores_nothing(self):
        for body in (page(), {"url": PAGE_URL}, {"url": PAGE_URL, "job": {}}, page(title="")):
            r = self.k.post("/api/extension/preview", json=body)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), {"url": PAGE_URL, "existing": None})
        r = self.k.post("/api/extension/preview", json=page("https://www.seek.com.au/jobs?jobId=81234567"))
        self.assertEqual(r.json(), {"url": "https://www.seek.com.au/job/81234567", "existing": None})
        r = self.k.post("/api/extension/preview", json=page(PAGE_URL + "?ref=x", url=PAGE_URL))
        self.assertEqual(r.json()["url"], PAGE_URL)
        self.assertEqual(self.job_count(self.acct.workspace_id), 0)

    def test_saving_needs_a_title(self):
        for title in ("", "   "):
            r = self.k.post("/api/extension/jobs", json=page(title=title))
            self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "invalid", "title"))
        r = self.k.post("/api/extension/jobs", json={"url": PAGE_URL})
        self.assertEqual((r.status_code, r.json()["field"]), (422, "title"))
        self.assertEqual(self.job_count(self.acct.workspace_id), 0)

    def test_saving_makes_a_private_job_then_updates_it(self):
        r = self.k.post("/api/extension/jobs", json=page(work_mode="hybrid"))
        self.assertEqual(r.status_code, 201, r.text)
        saved = r.json()
        self.assertEqual(set(saved), {"job_id", "created", "title", "company", "link", "excluded_reason"})
        self.assertEqual((saved["created"], saved["title"], saved["company"], saved["excluded_reason"]),
                         (True, "Product Owner, Payments", "Acme", None))
        self.assertEqual(saved["link"], f"/job.html?id={saved['job_id']}&ws={self.acct.workspace_id}")
        job = self.stored(saved["job_id"])
        self.assertEqual((job.origin, job.posting_id, job.source, job.work_mode, job.employment_type,
                          job.detail_status, job.posted_at, job.url),
                         ("private", None, "browser", "hybrid", "full_time", "full", "2026-10-01", PAGE_URL))
        self.assertIsNone(job.duplicate_of)
        full = self.c.get(f"/api/jobs/{saved['job_id']}").json()  # the session reads what the key sealed
        self.assertEqual(full["description"], AD)
        self.assertEqual((full["salary_text"], full["location_text"]), ("$140,000 - $160,000 + super", "Melbourne VIC"))

        preview = self.k.post("/api/extension/preview", json=page()).json()
        self.assertEqual(preview["existing"], {"id": saved["job_id"], "title": "Product Owner, Payments",
                                               "status": "to_review", "link": saved["link"]})
        r = self.k.post("/api/extension/jobs", json=page(salary_text="$175,000", title="Lead Product Owner"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["job_id"], r.json()["created"], r.json()["title"]),
                         (saved["job_id"], False, "Lead Product Owner"))
        self.assertEqual(self.c.get(f"/api/jobs/{saved['job_id']}").json()["salary_text"], "$175,000")
        self.assertEqual(self.job_count(self.acct.workspace_id), 1)

    def test_a_job_saved_under_the_pages_address_is_found_again_by_it(self):
        first = self.k.post("/api/extension/jobs", json=page(PAGE_URL + "?ref=x")).json()
        r = self.k.post("/api/extension/preview", json=page(PAGE_URL + "?ref=x", url=PAGE_URL))
        self.assertEqual((r.json()["url"], r.json()["existing"]["id"]), (PAGE_URL, first["job_id"]))
        r = self.k.post("/api/extension/jobs", json=page(PAGE_URL + "?ref=x", url=PAGE_URL, salary_text="$1"))
        self.assertEqual((r.status_code, r.json()["job_id"]), (200, first["job_id"]))
        self.assertEqual(self.job_count(self.acct.workspace_id), 1)

    def test_a_seek_job_is_stored_under_seeks_address_and_links_the_pools_posting(self):
        from backend import pool
        from backend.adapters.base import Posting as Scraped

        with helpers.as_account(self.acct):
            db = SessionLocal()
            try:
                pool.upsert_scraped(db, Scraped(url="https://www.seek.com.au/job/81234567", title="Delivery Lead",
                                                company="Seekco", external_id="81234567", detail_status="summary",
                                                description="Lead delivery."), "seek", None, utcnow())
                db.commit()
            finally:
                db.close()
        r = self.k.post("/api/extension/jobs", json=page("https://www.seek.com.au/jobs?keywords=x&jobId=81234567",
                                                         title="Delivery Lead", company="Seekco"))
        self.assertEqual(r.status_code, 201, r.text)
        job = self.stored(r.json()["job_id"])
        self.assertEqual((job.url, job.external_id, job.origin), ("https://www.seek.com.au/job/81234567", "81234567",
                                                                  "pool"))
        self.assertIsNotNone(job.posting_id)
        # Another site's page never lands on a SEEK address
        r = self.k.post("/api/extension/jobs", json=page("https://evil.example/x", url="https://www.seek.com.au/job/1"))
        self.assertEqual(self.stored(r.json()["job_id"]).url, "https://evil.example/x")

    def test_a_saved_job_is_filtered_and_waits_for_a_score(self):
        from backend.scorer import select_jobs

        r = self.c.put("/api/dealbreakers", json={"industries": [], "keywords": ["blockchain"]})
        self.assertEqual(r.status_code, 200, r.text)
        bad = self.k.post("/api/extension/jobs", json=page("https://jobs.example.com/roles/42", title="Chain Analyst",
                                                           description="A blockchain job. " * 20))
        good = self.k.post("/api/extension/jobs", json=page())
        self.assertEqual((bad.status_code, good.status_code), (201, 201))
        listed = {j["id"]: j for j in self.jobs()}
        self.assertTrue(listed[bad.json()["job_id"]]["excluded_reason"])
        self.assertIsNone(listed[good.json()["job_id"]]["excluded_reason"])
        # The answer says why the job won't show in the Jobs list, as the list says it
        self.assertEqual(bad.json()["excluded_reason"], listed[bad.json()["job_id"]]["excluded_reason"])
        self.assertIsNone(good.json()["excluded_reason"])
        self.assertNotIn(bad.json()["job_id"], [j["id"] for j in self.jobs(include="")])
        with helpers.as_account(self.acct):
            db = SessionLocal()
            try:
                waiting = select_jobs(db, "pending", "x", self.acct.workspace_id)
            finally:
                db.close()
        self.assertIn(good.json()["job_id"], waiting)
        self.assertNotIn(bad.json()["job_id"], waiting)

    def test_the_same_job_on_another_site_is_merged_and_the_listed_one_is_linked(self):
        first = self.k.post("/api/extension/jobs", json=page())
        again = self.k.post("/api/extension/jobs", json=page("https://board.example.net/acme/77"))
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(again.json()["job_id"], first.json()["job_id"], "the copy is merged into the first")
        self.assertEqual([j["id"] for j in self.jobs()], [first.json()["job_id"]])
        preview = self.k.post("/api/extension/preview", json=page("https://board.example.net/acme/77")).json()
        self.assertEqual(preview["existing"]["id"], first.json()["job_id"])

    def test_saving_a_merged_copys_page_updates_the_listed_job(self):
        first = self.k.post("/api/extension/jobs", json=page())
        copy_url = "https://board.example.net/acme/77"
        again = self.k.post("/api/extension/jobs", json=page(copy_url))
        listed_id = first.json()["job_id"]
        self.assertEqual(again.json()["job_id"], listed_id)
        r = self.k.post("/api/extension/jobs", json=page(copy_url, salary_text="$999,999", title="Renamed title"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["job_id"], r.json()["created"], r.json()["title"]),
                         (listed_id, False, "Renamed title"))
        shown = self.c.get(f"/api/jobs/{listed_id}").json()
        self.assertEqual((shown["title"], shown["salary_text"]), ("Renamed title", "$999,999"), "the edits are seen")
        self.assertEqual(shown["url"], PAGE_URL)
        copies = shown["also_on"]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0]["title"], "Product Owner, Payments", "the hidden copy is left alone")
        self.assertEqual([j["id"] for j in self.jobs()], [listed_id])

    def test_bad_input_is_422_with_the_field(self):
        for body, field in (({"job": {"title": "T"}}, "url"), ({"url": "ftp://files.example/x"}, "url"),
                            ({"url": "javascript:alert(1)"}, "url"), ({"url": 5}, "url"),
                            ({"url": PAGE_URL, "canonical_url": "data:text/html,x"}, "canonical_url"),
                            ({"url": PAGE_URL, "canonical_url": ["x"]}, "canonical_url"),
                            ({"url": PAGE_URL, "job": "Analyst"}, "job"), ({"url": PAGE_URL, "job": []}, "job"),
                            (page(title=5), "title"), (page(company={"name": "Acme"}), "company"),
                            (page(description=["a", "b"]), "description"), (page(work_mode="moon"), "work_mode"),
                            (page(work_mode="Remote"), "work_mode"), (page(description_from="html"), "description_from"),
                            (page(method="seek"), "method"), (page(posted_at="3 days ago"), "posted_at"),
                            (page(posted_at=20261001), "posted_at")):
            for path in ("/api/extension/preview", "/api/extension/jobs"):
                r = self.k.post(path, json=body)
                self.assertEqual((r.status_code, r.json().get("code"), r.json().get("field")), (422, "invalid", field),
                                 (path, body))
        for raw in (b"not json", b"[1]", b'"x"', b"[" * 200_000 + b"]" * 200_000, b'{"url": ' * 50_000):
            r = self.k.post("/api/extension/jobs", content=raw, headers={"Content-Type": "application/json"})
            self.assertEqual((r.status_code, r.json()["code"]), (422, "invalid"), raw[:20])
        self.assertEqual(self.job_count(self.acct.workspace_id), 0)

    def test_unknown_fields_are_refused_so_a_stale_extension_fails_loudly(self):
        old = {"url": PAGE_URL, "page_title": "Product Owner | Acme", "html": "<html></html>", "text": "...",
               "selection": "", "fields": {"title": "Product Owner"}}
        for body, field in ((old, "page_title"), ({"url": PAGE_URL, "html": "<p>x</p>"}, "html"),
                            ({**page(), "selection": "x"}, "selection"),
                            ({**page(), "job": {**page()["job"], "location": "Sydney"}}, "location")):
            for path in ("/api/extension/preview", "/api/extension/jobs"):
                r = self.k.post(path, json=body)
                self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "invalid", field), path)
                self.assertIn("Update the extension", r.json()["detail"])
        self.assertEqual(self.job_count(self.acct.workspace_id), 0)

    def test_values_over_their_limits_are_413_with_the_field(self):
        for body, field in (({"url": "https://x.example/" + "a" * 2050}, "url"),
                            ({"url": PAGE_URL, "canonical_url": "https://x.example/" + "a" * 2050}, "canonical_url"),
                            (page(title="t" * 501), "title"), (page(company="c" * 501), "company"),
                            (page(location_text="l" * 501), "location_text"), (page(salary_text="s" * 501), "salary_text"),
                            (page(employment_type="e" * 101), "employment_type"), (page(posted_at="2" * 41), "posted_at"),
                            (page(url="https://x.example/" + "a" * 2050), "url"),
                            (page(description="d" * 100_001), "description")):
            for path in ("/api/extension/preview", "/api/extension/jobs"):
                r = self.k.post(path, json=body)
                self.assertEqual((r.status_code, r.json()["code"], r.json().get("field")), (413, "too_large", field),
                                 (path, field))
        for limit in ("t" * 500, "😀" * 500):  # characters, not bytes: at the limit is fine
            self.assertIn(self.k.post("/api/extension/jobs", json=page(title=limit)).status_code, (200, 201))
        r = self.k.post("/api/extension/jobs", json=page(description="d" * 100_000, employment_type="e" * 100))
        self.assertIn(r.status_code, (200, 201), r.text)

    def test_a_body_over_512_kb_is_refused_before_it_is_parsed(self):
        with mock.patch.object(extension, "json", mock.Mock(loads=mock.Mock(side_effect=AssertionError("parsed")))) as parsed:
            r = self.k.post("/api/extension/jobs", json={"url": PAGE_URL, "padding": "a" * extension.MAX_BODY})
            self.assertEqual((r.status_code, r.json()["code"]), (413, "too_large"))
            r = self.k.post("/api/extension/jobs", content=b"x" * 20, headers={"Content-Length": "999999999"})
            self.assertEqual(r.status_code, 413)

            def stream():
                for _ in range(20):
                    yield b" " * 64 * 1024

            r = self.k.post("/api/extension/preview", content=stream(), headers={"Content-Type": "application/json"})
            self.assertEqual((r.status_code, r.json()["code"]), (413, "too_large"), "no Content-Length: while reading")
            parsed.loads.assert_not_called()
        self.assertEqual(extension.MAX_BODY, 512 * 1024)
        self.assertEqual(self.job_count(self.acct.workspace_id), 0)

    def test_text_utf8_cant_store_and_nulls(self):
        body = json.dumps(page(title="Analyst \ud800 lead", company=None, description="Text \udfff here " * 20))
        r = self.k.post("/api/extension/jobs", content=body.encode(), headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["title"], "Analyst ? lead")
        self.assertEqual(self.k.post("/api/extension/preview", json={"url": PAGE_URL, "canonical_url": None,
                                                                     "job": None}).status_code, 200)

    def test_the_largest_request_is_quick(self):
        """Nothing is parsed but the JSON: the largest request the limits allow is stored in well under a second."""
        body = page(**{name: "x" * limit for name, limit in extension.LIMITS.items()
                       if name not in ("url", "canonical_url", "posted_at", "description")},
                    description=("A line of the ad. " * 6 + "\n") * 900)
        body["job"]["description"] = body["job"]["description"][:100_000]
        start = time.perf_counter()
        for path in ("/api/extension/preview", "/api/extension/jobs"):
            self.assertIn(self.k.post(path, json=body).status_code, (200, 201))
        self.assertLess(time.perf_counter() - start, 5.0)


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
