"""The sign-up walkthrough (accounts design 6.1, Phase K): progress in ``users.onboarding_step``, the sealed theme in
``users.prefs_enc``, the places lookup, the routing rule in common.js, and the first admin's short path.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import text

from tests import helpers  # noqa: E402  (sets the environment first)

from backend import account, geo, users  # noqa: E402
from backend.db import SessionLocal, engine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def signed_in(account_):
    c = helpers.client(account_.email)
    c.__enter__()
    return c


class WizardApiTest(unittest.TestCase):
    def setUp(self):
        self.bob = helpers.make_user()
        self.c = helpers.client(self.bob.email)
        self.c.__enter__()
        self.addCleanup(self.c.__exit__, None, None, None)

    def test_a_new_account_starts_at_step_1_on_the_full_path(self):
        me = self.c.get("/api/auth/me").json()
        self.assertEqual(me["onboarding_step"], 1)
        self.assertEqual(me["prefs"], {"theme": None})
        info = self.c.get("/api/account/onboarding").json()
        self.assertEqual((info["step"], info["path"]), (1, "full"))
        # Scheduled searches and digests are hidden until Phases G and J ship them.
        self.assertFalse(info["features"]["scheduled_searches"])
        self.assertFalse(info["features"]["digests"])

    def test_each_step_saves_through_the_search_endpoints_and_progress_resumes(self):
        c = self.c
        # Step 2: titles, as the page does it (the whole profile, merged)
        profile = c.get("/api/profile").json()
        r = c.put("/api/profile", json={**profile, "titles": ["Product Manager", "Business Analyst"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.put("/api/account/onboarding", json={"step": 3}).json()["step"], 3)
        # Step 3: places, work types, office days and the home pin
        profile = c.get("/api/profile").json()
        c.put("/api/profile", json={**profile, "seek_locations": ["All Sydney NSW"], "allow_onsite": False,
                                    "max_office_days": 2})
        pin = c.post("/api/pins", json={"label": "Chatswood NSW", "kind": "home", "lat": -33.79, "lng": 151.18,
                                         "radius_km": 20})
        self.assertEqual(pin.status_code, 201)
        c.put("/api/account/onboarding", json={"step": 4})
        # Step 4: salary, keywords, industries
        profile = c.get("/api/profile").json()
        c.put("/api/profile", json={**profile, "salary_floor": 140000, "keywords_exclude": ["graduate"]})
        c.put("/api/dealbreakers", json={"industries": ["Gambling"], "keywords": []})

        saved = c.get("/api/profile").json()
        self.assertEqual(saved["titles"], ["Product Manager", "Business Analyst"])
        self.assertEqual(saved["seek_locations"], ["All Sydney NSW"])
        self.assertEqual((saved["salary_floor"], saved["max_office_days"], saved["allow_onsite"]), (140000, 2, False))
        self.assertEqual(c.get("/api/dealbreakers").json()["industries"], ["Gambling"])
        self.assertEqual([p["label"] for p in c.get("/api/pins").json()], ["Chatswood NSW"])

        # Sign in again, as if the person had closed the tab: the walkthrough is at step 4.
        with helpers.client(self.bob.email) as again:
            self.assertEqual(again.get("/api/auth/me").json()["onboarding_step"], 4)
            self.assertEqual(again.get("/api/account/onboarding").json()["step"], 4)
            done = again.put("/api/account/onboarding", json={"done": True}).json()
            self.assertIsNone(done["step"])
            self.assertIsNone(again.get("/api/auth/me").json()["onboarding_step"])

    def test_step_values_are_checked(self):
        for body in ({}, {"step": 0}, {"step": 99}, {"step": "two"}, {"step": 2, "other": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.c.put("/api/account/onboarding", json=body).status_code, 422)
        self.assertEqual(self.c.get("/api/account/onboarding").json()["step"], 1)

    def test_progress_is_per_account_and_needs_a_session(self):
        self.c.put("/api/account/onboarding", json={"step": 5})
        other = helpers.make_user()
        with helpers.client(other.email) as c2:
            self.assertEqual(c2.get("/api/account/onboarding").json()["step"], 1)
        from fastapi.testclient import TestClient
        from backend.app import app
        anonymous = TestClient(app)
        self.assertEqual(anonymous.get("/api/account/onboarding").status_code, 401)
        self.assertEqual(anonymous.get("/api/account/prefs").status_code, 401)
        self.assertEqual(anonymous.get("/api/geo/lookup?q=Sydney").status_code, 401)


class PrefsTest(unittest.TestCase):
    def setUp(self):
        self.bob = helpers.make_user()
        self.c = helpers.client(self.bob.email)
        self.c.__enter__()
        self.addCleanup(self.c.__exit__, None, None, None)

    def test_the_theme_is_saved_sealed_for_the_account(self):
        self.assertEqual(self.c.get("/api/account/prefs").json(), {"theme": None})
        r = self.c.put("/api/account/prefs", json={"theme": "dyslexia-dark"})
        self.assertEqual(r.json(), {"theme": "dyslexia-dark"})
        self.assertEqual(self.c.get("/api/account/prefs").json(), {"theme": "dyslexia-dark"})
        self.assertEqual(self.c.get("/api/auth/me").json()["prefs"], {"theme": "dyslexia-dark"})
        # Another sign-in (another device) gets the same theme; another account does not.
        with helpers.client(self.bob.email) as again:
            self.assertEqual(again.get("/api/account/prefs").json()["theme"], "dyslexia-dark")
        with helpers.client(helpers.make_user().email) as other:
            self.assertEqual(other.get("/api/account/prefs").json()["theme"], None)

    def test_the_raw_database_never_holds_the_theme_name(self):
        self.c.put("/api/account/prefs", json={"theme": "monochromacy-dark"})
        db = SessionLocal()
        try:
            blob = db.execute(text("SELECT prefs_enc FROM users WHERE id = :i"), {"i": self.bob.id}).scalar()
        finally:
            db.close()
        self.assertIsInstance(blob, bytes)
        self.assertNotIn(b"monochromacy", blob)
        self.assertNotIn(b"theme", blob)
        # ... nor does the database file (checkpointed, so the main file holds every committed page)
        db = SessionLocal()
        try:
            db.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        finally:
            db.close()
        raw = Path(engine.url.database).read_bytes()
        self.assertNotIn(b"monochromacy", raw)
        # and it opens only under this user's key: the stored bytes aren't readable as JSON
        with self.assertRaises(ValueError):
            json.loads(blob)

    def test_unknown_themes_and_extra_fields_are_refused(self):
        for body in ({"theme": "neon"}, {"theme": ""}, {}, {"theme": "dark", "x": 1}, {"theme": "<script>"}):
            with self.subTest(body=body):
                self.assertEqual(self.c.put("/api/account/prefs", json=body).status_code, 422)
        self.assertEqual(self.c.get("/api/account/prefs").json(), {"theme": None})

    def test_the_servers_theme_list_matches_the_pages(self):
        offered = set(re.findall(r"\{ id: '([\w-]+)'", (FRONTEND / "common.js").read_text(encoding="utf-8")))
        self.assertEqual(set(account.THEME_IDS), offered)


class FirstAdminShortPathTest(unittest.TestCase):
    """The first admin's existing search settings moved into the account: theme, then keep or review."""

    def make(self, created_via: str, titles: list[str]):
        email = f"{created_via}-{len(titles)}-{os.urandom(3).hex()}@example.invalid"
        acct = users.create_user(email, helpers.USER_PASSWORD, role="user", created_via=created_via)
        helpers._passwords[email] = helpers.USER_PASSWORD
        c = helpers.client(email)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        if titles:
            c.put("/api/profile", json={**c.get("/api/profile").json(), "titles": titles})
        return c

    def test_the_first_admin_with_a_search_gets_the_short_path(self):
        c = self.make("first_run", ["Product Manager"])
        self.assertEqual(c.get("/api/account/onboarding").json()["path"], "short")

    def test_the_bootstrap_admin_gets_it_too(self):
        c = self.make("bootstrap", ["Analyst"])
        self.assertEqual(c.get("/api/account/onboarding").json()["path"], "short")

    def test_reviewing_moves_to_the_full_path_and_it_stays_full(self):
        c = self.make("first_run", ["Product Manager"])
        self.assertEqual(c.put("/api/account/onboarding", json={"step": 2}).json()["path"], "short")
        self.assertEqual(c.put("/api/account/onboarding", json={"step": 3}).json()["path"], "full")

    def test_no_existing_settings_or_another_kind_of_account_gets_the_full_path(self):
        self.assertEqual(self.make("first_run", []).get("/api/account/onboarding").json()["path"], "full")
        self.assertEqual(self.make("open", ["Product Manager"]).get("/api/account/onboarding").json()["path"], "full")
        self.assertEqual(self.make("invite", ["Product Manager"]).get("/api/account/onboarding").json()["path"], "full")

    def test_keeping_the_settings_finishes_without_changing_them(self):
        c = self.make("first_run", ["Product Manager"])
        before = c.get("/api/profile").json()
        c.put("/api/account/onboarding", json={"done": True})
        self.assertEqual(c.get("/api/profile").json(), before)
        self.assertIsNone(c.get("/api/auth/me").json()["onboarding_step"])

    def test_accounts_from_before_the_walkthrough_have_none(self):
        """The column is NULL for them (the migration adds it empty), and NULL means done."""
        bob = helpers.make_user()
        db = SessionLocal()
        try:
            db.execute(text("UPDATE users SET onboarding_step = NULL WHERE id = :i"), {"i": bob.id})
            db.commit()
        finally:
            db.close()
        with helpers.client(bob.email) as c:
            self.assertIsNone(c.get("/api/auth/me").json()["onboarding_step"])
            self.assertIsNone(c.get("/api/account/onboarding").json()["step"])


class GeoLookupTest(unittest.TestCase):
    def setUp(self):
        self.c = helpers.client(helpers.make_user().email)
        self.c.__enter__()
        self.addCleanup(self.c.__exit__, None, None, None)

    def test_a_place_is_found_through_the_cached_geocoder(self):
        with mock.patch.object(geo, "geocode", return_value={"lat": -33.8, "lng": 151.18, "country": "AU",
                                                             "type": "suburb"}) as g:
            r = self.c.get("/api/geo/lookup", params={"q": "  Chatswood   NSW "})
        self.assertEqual(r.json(), {"found": True, "label": "Chatswood NSW", "lat": -33.8, "lng": 151.18,
                                    "kind": "suburb"})
        g.assert_called_once_with("Chatswood NSW")

    def test_unknown_or_too_short_text_is_not_found_and_does_not_call_out(self):
        with mock.patch.object(geo, "geocode", return_value=None) as g:
            self.assertEqual(self.c.get("/api/geo/lookup", params={"q": "zzzz nowhere"}).json(), {"found": False})
            self.assertEqual(self.c.get("/api/geo/lookup", params={"q": "a"}).json(), {"found": False})
            self.assertEqual(self.c.get("/api/geo/lookup").json(), {"found": False})
        self.assertEqual(g.call_count, 1)

    def test_long_queries_are_refused(self):
        self.assertEqual(self.c.get("/api/geo/lookup", params={"q": "x" * 201}).status_code, 422)


@unittest.skipUnless(shutil.which("node"), "needs node to run the page's routing rule")
class RoutingTest(unittest.TestCase):
    """common.js sends a signed-in person with an unfinished walkthrough to welcome.html (to first-run.html first when
    their recovery codes aren't confirmed), on every page except welcome.html. The rule is run as the page runs it."""

    @classmethod
    def setUpClass(cls):
        src = (FRONTEND / "common.js").read_text(encoding="utf-8")
        m = re.search(r"function walkthroughPage\(me\) \{.*?\n\}\n", src, re.S)
        assert m, "walkthroughPage() not found in common.js"
        cls.fn = m.group(0)

    def route(self, me, page="dashboard"):
        script = f"global.document = {{ body: {{ dataset: {{ page: {json.dumps(page)} }} }} }};\n{self.fn}\n" \
                 f"console.log(JSON.stringify(walkthroughPage({json.dumps(me)})));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_unfinished_walkthrough_goes_to_welcome(self):
        self.assertEqual(self.route({"onboarding_step": 1, "recovery_codes_pending": False}), "welcome.html")
        self.assertEqual(self.route({"onboarding_step": 4, "recovery_codes_pending": False}, "job"), "welcome.html")

    def test_codes_come_first(self):
        self.assertEqual(self.route({"onboarding_step": 1, "recovery_codes_pending": True}), "first-run.html")

    def test_finished_walkthrough_and_the_welcome_page_itself_stay_put(self):
        self.assertIsNone(self.route({"onboarding_step": None, "recovery_codes_pending": False}))
        self.assertIsNone(self.route({"onboarding_step": 0, "recovery_codes_pending": False}))
        self.assertIsNone(self.route({"onboarding_step": 2, "recovery_codes_pending": False}, "welcome"))

    def test_first_run_ends_at_the_dashboard_which_routes_on(self):
        """first-run.html sends people to './' once the codes are saved, and './' goes through loadSession()."""
        auth = (FRONTEND / "auth.js").read_text(encoding="utf-8")
        self.assertIn("location.replace('./')", auth)
        common = (FRONTEND / "common.js").read_text(encoding="utf-8")
        self.assertRegex(common, r"walkthroughPage\(data\)")
        for page in ("app.js", "job.js"):
            self.assertIn("loadSession()", (FRONTEND / page).read_text(encoding="utf-8"))

    def test_welcome_sends_people_who_have_finished_to_the_dashboard(self):
        js = (FRONTEND / "welcome.js").read_text(encoding="utf-8")
        self.assertIn("if (!me.onboarding_step) { location.replace('./'); return; }", js)
        self.assertIn("location.replace('first-run.html')", js)


if __name__ == "__main__":
    unittest.main()
