"""The sign-up walkthrough (accounts design 6.1, Phase K): progress in ``users.onboarding_step``, the sealed theme in
``users.prefs_enc``, the places lookup, the routing rule in common.js, and the first account's short path.

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

from backend import account, geo  # noqa: E402
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
        # Scheduled searches (background access, Phase G) are offered; there are no digest emails.
        self.assertTrue(info["features"]["scheduled_searches"])
        self.assertNotIn("digests", info["features"])

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
        exact = {t for t in offered if f"{t}-aa" in offered}
        self.assertEqual(set(account.EXACT_THEME_IDS), exact)

    def test_a_family_theme_saved_before_the_exact_palettes_keeps_meaning_its_aa_version(self):
        # Saved before (no "themes" mark): the plain family id was the AA palette, so it reads as "<id>-aa" ...
        for old, now in (("catppuccin-mocha", "catppuccin-mocha-aa"), ("nord", "nord-aa"),
                         ("vanilla-dark", "vanilla-dark-aa"), ("dyslexia", "dyslexia"), ("dark", "dark"),
                         ("system", "system")):
            self.assertEqual(account._migrated({"theme": old})["theme"], now, old)
        self.assertEqual(account._migrated({}), {"themes": account.THEMES_VERSION})
        # ... and once marked, a plain id is the exact palette.
        marked = {"theme": "nord", "themes": account.THEMES_VERSION}
        self.assertEqual(account._migrated(marked)["theme"], "nord")
        # Through the API: a save writes the mark, so choosing an exact palette stays exact.
        for theme in ("nord", "nord-aa", "catppuccin-latte", "vanilla-dark-aa"):
            self.assertEqual(self.c.put("/api/account/prefs", json={"theme": theme}).json(), {"theme": theme})
            self.assertEqual(self.c.get("/api/account/prefs").json(), {"theme": theme})


class FirstAdminShortPathTest(unittest.TestCase):
    """The first account's existing search settings moved into it: AI provider, theme, then keep or review."""

    def make(self, created_via: str, titles: list[str]):
        email = f"{created_via}-{len(titles)}-{os.urandom(3).hex()}@example.invalid"
        helpers.extra_account(email, helpers.USER_PASSWORD, created_via=created_via)
        c = helpers.client(email)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        if titles:
            c.put("/api/profile", json={**c.get("/api/profile").json(), "titles": titles})
        return c

    def test_the_first_account_with_a_search_gets_the_short_path(self):
        c = self.make("first_run", ["Product Manager"])
        self.assertEqual(c.get("/api/account/onboarding").json()["path"], "short")

    def test_the_bootstrap_account_gets_it_too(self):
        c = self.make("bootstrap", ["Analyst"])
        self.assertEqual(c.get("/api/account/onboarding").json()["path"], "short")

    def test_reviewing_moves_to_the_full_path_and_it_stays_full(self):
        c = self.make("first_run", ["Product Manager"])
        self.assertEqual(c.put("/api/account/onboarding", json={"step": 3}).json()["path"], "short")
        self.assertEqual(c.put("/api/account/onboarding", json={"step": 4}).json()["path"], "full")

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

    def test_first_run_page_is_not_sent_to_itself(self):
        """Its password dialog calls loadSession(); redirecting first-run.html to first-run.html reloaded it for ever."""
        self.assertIsNone(self.route({"onboarding_step": 1, "recovery_codes_pending": True}, "first-run"))
        self.assertIsNone(self.route({"onboarding_step": 1, "recovery_codes_pending": False}, "first-run"))

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


@unittest.skipUnless(shutil.which("node"), "needs node to run the page's script")
class AutomaticSearchesStepTest(unittest.TestCase):
    """The wizard's "Automatic searches" step (Phase G): offered when ``features.scheduled_searches`` is true, it turns
    background access on (the server must hold a key to search while you're away) and the schedule on, or leaves both
    alone. welcome.js runs in node with small stand-ins for the page (``$``, ``api``) that record every call."""

    HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [welcome, common, scenario] = [process.argv[1], process.argv[2], JSON.parse(process.argv[3])];
const calls = [];
const els = { '#wz-auto': { checked: !!scenario.ticked } };
const failures = { ...(scenario.fail || {}) };
const ctx = {
  document: { addEventListener() {} },
  $: (sel) => els[sel], $$: () => [],
  esc: (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  announce() {}, setText() {}, confirmPassword: async () => { calls.push(['confirm']); return scenario.confirm !== false; },
  api: async (url, opts = {}) => {
    const method = opts.method || 'GET', body = opts.body ? JSON.parse(opts.body) : null;
    calls.push([method, url, body]);
    const key = `${method} ${url}`;
    if (failures[key] > 0) { failures[key] -= 1; const e = new Error('For your safety, enter your password again to do this.'); e.code = 'reauth_required'; throw e; }
    if (url === '/api/account/background') return { enabled: !!(body && body.enabled), since: '2026-10-07T00:00:00Z' };
    if (url === '/api/schedule') return method === 'PUT' ? { ...body } : { enabled: true, times: ['10:00'], days: [0, 1], skip_holidays: true, state: 'NSW' };
    return {};
  },
};
vm.createContext(ctx);
const m = fs.readFileSync(common, 'utf8').match(/async function withRecentAuth\(fn\) \{.*?\n\}\n/s);
vm.runInContext(m[0], ctx);
vm.runInContext(fs.readFileSync(common, 'utf8').match(/const BACKGROUND_ON_COPY = .*\nconst BACKGROUND_OFF_COPY = .*\n/)[0], ctx);
const page = vm.runInContext(fs.readFileSync(welcome, 'utf8') + '\n;({ wiz, STEPS, buildSteps })', ctx);
Object.assign(page.wiz, scenario.wiz || {});
(async () => {
  const out = { steps: page.buildSteps() };
  if (scenario.html) out.html = page.STEPS.auto.html();
  if (scenario.save) {
    try { out.saved = await page.STEPS.auto.save(); } catch (e) { out.error = { message: e.message, code: e.code }; }
  }
  out.calls = calls;
  console.log(JSON.stringify(out));
})();
"""

    def run_page(self, scenario: dict) -> dict:
        done = subprocess.run(["node", "-e", self.HARNESS, "--", str(FRONTEND / "welcome.js"),
                               str(FRONTEND / "common.js"), json.dumps(scenario)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_step_appears_just_before_the_resume_step_only_when_the_server_offers_it(self):
        on = self.run_page({"wiz": {"features": {"scheduled_searches": True}}})
        self.assertEqual(on["steps"], ["ai", "theme", "titles", "where", "extras", "auto", "resume"])
        off = self.run_page({"wiz": {"features": {"scheduled_searches": False}}})
        self.assertEqual(off["steps"], ["ai", "theme", "titles", "where", "extras", "resume"])
        short = self.run_page({"wiz": {"short": True, "features": {"scheduled_searches": True}}})
        self.assertEqual(short["steps"], ["ai", "theme", "keep"])

    def test_the_server_offers_what_the_page_asks_for(self):
        bob = helpers.make_user()
        with helpers.client(bob.email) as c:
            self.assertTrue(c.get("/api/account/onboarding").json()["features"]["scheduled_searches"])
            # the three calls the step makes exist
            self.assertEqual(c.get("/api/account/background").status_code, 200)
            self.assertEqual(c.get("/api/schedule").status_code, 200)

    def test_the_step_explains_both_choices_in_the_words_of_the_background_card(self):
        html = self.run_page({"html": True, "wiz": {"features": {"scheduled_searches": True}}})["html"]
        self.assertIn('id="wz-auto"', html)
        self.assertNotIn(" checked", html.split("<input")[1].split(">")[0], "off by default for a new account")
        self.assertIn('aria-describedby="wz-auto-hint"', html)
        self.assertIn("JobHuntPA keeps a key on the server so it can run scheduled searches while you&#39;re away.", html)
        self.assertIn("Only you can unlock your data. JobHuntPA can&#39;t run searches while you&#39;re away", html)
        # the same words as the Settings card: one definition, in common.js
        self.assertNotIn("BACKGROUND_ON_COPY =", (FRONTEND / "welcome.js").read_text(encoding="utf-8"))
        self.assertNotIn("BACKGROUND_ON_COPY =", (FRONTEND / "background.js").read_text(encoding="utf-8"))
        self.assertIn("BACKGROUND_ON_COPY =", (FRONTEND / "common.js").read_text(encoding="utf-8"))

    def test_it_starts_ticked_when_searches_already_run_automatically(self):
        html = self.run_page({"html": True, "wiz": {"features": {"scheduled_searches": True},
                                                    "background": {"enabled": True}, "schedule": {"enabled": True}}})["html"]
        self.assertIn(" checked", html.split("<input")[1].split(">")[0])
        html = self.run_page({"html": True, "wiz": {"background": {"enabled": True}, "schedule": {"enabled": False}}})["html"]
        self.assertNotIn(" checked", html.split("<input")[1].split(">")[0])

    def test_ticking_it_turns_background_access_on_then_the_schedule(self):
        out = self.run_page({"save": True, "ticked": True, "wiz": {"background": {"enabled": False}}})
        self.assertTrue(out["saved"])
        self.assertEqual([c[:2] for c in out["calls"]], [["POST", "/api/account/background"], ["GET", "/api/schedule"],
                                                         ["PUT", "/api/schedule"]])
        self.assertEqual(out["calls"][0][2], {"enabled": True})
        self.assertEqual(out["calls"][2][2], {"enabled": True, "times": ["10:00"], "days": [0, 1],
                                              "skip_holidays": True, "state": "NSW"})

    def test_an_old_sign_in_asks_for_the_password_and_tries_again(self):
        out = self.run_page({"save": True, "ticked": True, "wiz": {"background": {"enabled": False}},
                             "fail": {"POST /api/account/background": 1}})
        self.assertTrue(out["saved"])
        self.assertEqual([c[0] for c in out["calls"]], ["POST", "confirm", "POST", "GET", "PUT"])

    def test_without_the_password_nothing_is_switched_on(self):
        out = self.run_page({"save": True, "ticked": True, "wiz": {"background": {"enabled": False}},
                             "fail": {"POST /api/account/background": 1}, "confirm": False})
        self.assertEqual(out["error"]["code"], "reauth_required")
        self.assertNotIn("PUT", [c[0] for c in out["calls"]], "no schedule is saved for a key the server doesn't have")

    def test_leaving_it_empty_only_turns_the_schedule_off(self):
        out = self.run_page({"save": True, "ticked": False, "wiz": {"background": {"enabled": False}}})
        self.assertEqual([c[:2] for c in out["calls"]], [["GET", "/api/schedule"], ["PUT", "/api/schedule"]])
        self.assertFalse(out["calls"][1][2]["enabled"])

    def test_already_on_is_not_asked_again(self):
        out = self.run_page({"save": True, "ticked": True, "wiz": {"background": {"enabled": True},
                                                                  "schedule": {"enabled": False, "times": ["09:00"], "days": [2],
                                                                               "skip_holidays": False, "state": "VIC"}}})
        self.assertEqual([c[:2] for c in out["calls"]], [["PUT", "/api/schedule"]])
        self.assertEqual(out["calls"][0][2], {"enabled": True, "times": ["09:00"], "days": [2], "skip_holidays": False, "state": "VIC"})

    def test_the_calls_work_against_the_real_server(self):
        """What the step sends, in order, with a new account's fresh sign-in."""
        bob = helpers.make_user()
        with helpers.client(bob.email) as c:
            self.assertEqual(c.post("/api/account/background", json={"enabled": True}).json()["enabled"], True)
            current = c.get("/api/schedule").json()
            put = c.put("/api/schedule", json={"enabled": True, "times": current["times"], "days": current["days"],
                                               "skip_holidays": current["skip_holidays"], "state": current["state"]})
            self.assertEqual(put.status_code, 200)
            self.assertTrue(put.json()["enabled"])
            self.assertTrue(put.json()["background"])
            self.assertIsNone(put.json()["skipped"])


@unittest.skipUnless(shutil.which("node"), "needs node to run the page's script")
class AiProviderStepTest(unittest.TestCase):
    """The walkthrough's first step is the first run's step 3 (spec §1.5): the AI provider, model and key, saved with
    PUT /api/settings/llm, or skipped (the step says which features wait for it)."""

    HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [welcome, scenario] = [process.argv[1], JSON.parse(process.argv[2])];
const calls = [], errors = [];
const field = (value) => ({ value, hidden: false, innerHTML: '', addEventListener() {} });
const els = {
  '#wz-ai-provider': field(scenario.provider), '#wz-ai-url': field(scenario.url || ''),
  '#wz-ai-model': field(scenario.model || ''), '#wz-ai-key': field(scenario.key || ''),
};
const ctx = {
  document: { addEventListener() {} }, $: (sel) => els[sel] || field(''), $$: () => [],
  esc: (s) => String(s ?? ''), announce() {}, setText() {}, NEW_TAB: '',
  api: async (url, opts = {}) => { calls.push([opts.method || 'GET', url, opts.body ? JSON.parse(opts.body) : null]); return { saved: true }; },
};
vm.createContext(ctx);
const page = vm.runInContext(fs.readFileSync(welcome, 'utf8') + '\n;({ wiz, STEPS })', ctx);
ctx.showErrors = (list) => errors.push(...list);
page.wiz.llm = scenario.llm;
(async () => {
  const out = { html: page.STEPS.ai.html(), intro: page.STEPS.ai.intro, skippable: page.STEPS.ai.skippable };
  out.saved = await page.STEPS.ai.save();
  out.calls = calls;
  out.errors = errors;
  console.log(JSON.stringify(out));
})();
"""

    @classmethod
    def setUpClass(cls):
        from backend.llm_providers import PRESETS
        cls.presets = PRESETS

    def run_step(self, **scenario) -> dict:
        scenario.setdefault("llm", {"provider": "openai", "base_url": "", "model": "", "key": {"set": False, "hint": None},
                                    "presets": self.presets})
        done = subprocess.run(["node", "-e", self.HARNESS, "--", str(FRONTEND / "welcome.js"), json.dumps(scenario)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_it_can_be_skipped_and_says_what_waits(self):
        out = self.run_step(provider="openai")
        self.assertTrue(out["skippable"])
        for waits in ("fit scores", "cover letters", "company research"):
            self.assertIn(waits, out["intro"])
            self.assertIn(waits, out["html"])
        self.assertIn("Settings > AI provider", out["intro"])

    def test_completed_it_saves_through_the_settings_endpoint(self):
        out = self.run_step(provider="openai", url="https://api.openai.com/v1", model="gpt-6-luna", key=" sk-abc12345 ")
        self.assertTrue(out["saved"])
        self.assertEqual(out["calls"], [["PUT", "/api/settings/llm", {"base_url": "https://api.openai.com/v1",
                                                                       "model": "gpt-6-luna", "api_key": "sk-abc12345"}]])

    def test_a_missing_key_is_asked_for_and_nothing_is_saved(self):
        out = self.run_step(provider="openai", url="https://api.openai.com/v1", model="gpt-6-luna")
        self.assertFalse(out["saved"])
        self.assertEqual(out["calls"], [])
        self.assertEqual([e["field"] for e in out["errors"]], ["wz-ai-key"])

    def test_a_saved_key_is_kept_and_a_keyless_provider_needs_none(self):
        saved = {"provider": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-6-luna",
                 "key": {"set": True, "hint": "…abcd"}, "presets": self.presets}
        out = self.run_step(provider="openai", url="https://api.openai.com/v1", model="gpt-6-astra", llm=saved)
        self.assertEqual(out["calls"][0][2], {"base_url": "https://api.openai.com/v1", "model": "gpt-6-astra"})
        out = self.run_step(provider="ollama", url="http://localhost:11434/v1", model="llama3.2:3b")
        self.assertTrue(out["saved"])
        self.assertNotIn("api_key", out["calls"][0][2])

    def test_the_server_takes_what_the_step_sends(self):
        bob = helpers.make_user()
        with helpers.client(bob.email) as c, mock.patch.dict(os.environ, {}), \
                mock.patch("backend.app_settings.update_env_file"):
            r = c.put("/api/settings/llm", json={"base_url": "https://api.openai.com/v1", "model": "gpt-6-luna",
                                                 "api_key": "sk-abc12345"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["model"], "gpt-6-luna")


if __name__ == "__main__":
    unittest.main()
