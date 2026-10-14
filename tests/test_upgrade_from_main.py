"""An install made by the code before the open-source version (origin/main at cb5d0fe) starts with today's code and
keeps everything (docs/design/open-source.md, Part 4 "Existing installs must survive", and Phase A).

The fixture in tests/fixtures/upgrade_from_main/ (its README says how it was made) has two accounts, both with
two-step sign-in on, a membership marked as paid at Stripe, a free period with a sealed note, a job digest, unused
emailed links, a sign-in waiting for its two-step code, jobs, a document, a pin, dealbreakers, background access and
an AI provider in .env (with the old SMTP_* and STRIPE_* lines beside it). A fresh process opens a copy of it: the app
must start, both accounts sign in with their password alone and find their data, and what the removed features left
must be gone: the two-step secrets, the waiting sign-in, the emailed links and the retired tables, with no copy of
those secrets left in the database file or its write-ahead log.
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401  (sets the test environment first)
from tests.helpers import remove_scratch_folders, scenario, scratch_folder

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "upgrade_from_main"
OWNER = ("owner@example.invalid", "fixture-owner-password")
MEMBER = ("member@example.invalid", "fixture-member-password")

UPGRADE = '''
import sqlite3
from pathlib import Path

DB = Path(FOLDER) / "jobhunt.db"


def raw(sql):
    con = sqlite3.connect(DB)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# What the removed features left, read before the new code touches the database (on the second start there is
# nothing of it left, so only the first run reads it).
first = bool(raw("SELECT name FROM sqlite_master WHERE name = 'memberships'"))
secrets = []
if first:
    secrets = [bytes(b) for (b,) in raw("SELECT totp_secret_enc FROM users WHERE totp_secret_enc IS NOT NULL")]
    secrets += [bytes(b) for (b,) in raw("SELECT payload_enc FROM auth_tokens WHERE payload_enc IS NOT NULL")]
    secrets += [bytes(b) for (b,) in raw("SELECT note_admin FROM membership_grants WHERE note_admin IS NOT NULL")]
    out["before"] = {"two_step": raw("SELECT COUNT(*) FROM users WHERE mfa_enabled = 1")[0][0],
                     "secrets": len(secrets),
                     "pending": raw("SELECT COUNT(*) FROM sessions WHERE state = 'pending_mfa'")[0][0],
                     "links": sorted(k for (k,) in raw("SELECT kind FROM auth_tokens")),
                     "memberships": raw("SELECT COUNT(*) FROM memberships")[0][0],
                     "grants": raw("SELECT COUNT(*) FROM membership_grants")[0][0],
                     "digests": raw("SELECT COUNT(*) FROM email_prefs WHERE digest_enabled = 1")[0][0]}

from fastapi.testclient import TestClient
from backend.app import app

ACCOUNTS = %r
with TestClient(app, base_url="https://jobs.example.invalid") as c:
    for email, password in ACCOUNTS:
        r = c.post("/api/auth/login", json={"email": email, "password": password})
        got = {"login": r.status_code, "answer": sorted(r.json())}
        if r.status_code == 200:
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            ws = c.get("/api/workspaces").json()
            c.headers["X-Workspace"] = str(ws[0]["id"])
            jobs = c.get("/api/jobs?include=excluded,hidden").json()
            got["jobs"] = sorted((j["title"], j["status"]) for j in jobs)
            docs = c.get("/api/documents").json()
            got["documents"] = [c.get(f"/api/documents/{d['id']}/text").json().get("text") for d in docs]
            got["pins"] = [p["label"] for p in c.get("/api/pins").json()]
            got["dealbreakers"] = c.get("/api/dealbreakers").json()
            got["security"] = c.get("/api/auth/security").json()
            got["background"] = c.get("/api/account/background").json()["enabled"]
            got["me"] = c.get("/api/auth/me").json()["user"]["role"]
            if got["me"] == "admin":
                got["llm_model"] = c.get("/api/settings/llm").json().get("model")
            # The routes of the removed features are gone (404 with a session; nothing answers them).
            got["gone"] = {path: c.post(path, json={}).status_code for path in (
                "/api/auth/login/mfa", "/api/auth/mfa/setup", "/api/auth/mfa/disable", "/api/auth/forgot",
                "/api/auth/reset", "/api/auth/verify-email", "/api/billing/checkout", "/api/billing/webhook")}
            got["gone"]["/api/account/digest"] = c.get("/api/account/digest").status_code
            got["gone"]["/api/billing/status"] = c.get("/api/billing/status").status_code
            got["gone"]["/api/settings/email"] = c.get("/api/settings/email").status_code
            c.post("/api/auth/logout")
            c.headers.pop("X-Workspace", None)
            c.headers.pop("X-CSRF-Token", None)
        out[email] = got

out["tables"] = sorted(t for (t,) in raw("SELECT name FROM sqlite_master WHERE type = 'table'"))
out["after"] = {"two_step": raw("SELECT COUNT(*) FROM users WHERE mfa_enabled != 0 OR totp_secret_enc IS NOT NULL "
                                "OR totp_pending_enc IS NOT NULL OR totp_last_step IS NOT NULL")[0][0],
                "pending": raw("SELECT COUNT(*) FROM sessions WHERE state = 'pending_mfa'")[0][0],
                "links": sorted(k for (k,) in raw("SELECT kind FROM auth_tokens")),
                "settings": sorted(k for (k,) in raw("SELECT key FROM app_settings"))}
files = [p for p in (DB, DB.with_name(DB.name + "-wal")) if p.exists()]
out["secrets_left"] = sum(1 for p in files for s in secrets if s in p.read_bytes())
'''


class UpgradeFromMainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = scratch_folder()
        for name in ("jobhunt.db", "server-keys.json"):
            shutil.copy(FIXTURE / name, Path(cls.folder) / name)
        shutil.copy(FIXTURE / "env.txt", Path(cls.folder) / ".env")
        shutil.copytree(FIXTURE / "uploads", Path(cls.folder) / "uploads")
        cls.out = scenario(UPGRADE % ([OWNER, MEMBER],), cls.folder)
        cls.again = scenario(UPGRADE % ([OWNER, MEMBER],), cls.folder)  # a second start: nothing left to do, all kept

    @classmethod
    def tearDownClass(cls):
        remove_scratch_folders()

    def test_the_fixture_holds_what_the_removed_features_left(self):
        before = self.out["before"]
        self.assertEqual(before["two_step"], 2)
        self.assertGreaterEqual(before["secrets"], 6)  # 2 two-step secrets, 3 emailed links, 1 free period's note
        self.assertEqual(before["pending"], 1)
        self.assertEqual(before["links"], ["email_change", "reset", "verify_email"])
        self.assertEqual((before["memberships"], before["grants"], before["digests"]), (2, 1, 1))

    def test_both_accounts_sign_in_with_their_password_alone(self):
        for run in (self.out, self.again):
            for email, _ in (OWNER, MEMBER):
                got = run[email]
                self.assertEqual(got["login"], 200, got)
                self.assertNotIn("mfa_required", got["answer"])
                self.assertIn("csrf", got["answer"])
                self.assertNotIn("mfa_enabled", got["security"])

    def test_every_job_document_and_setting_is_kept(self):
        for run in (self.out, self.again):
            owner, member = run[OWNER[0]], run[MEMBER[0]]
            self.assertEqual(owner["jobs"], [["Fixture Delivery Lead", "shortlisted"], ["Fixture Private Role", "applied"]])
            self.assertEqual(owner["documents"], ["Fixture CV: ten years of delivery."])
            self.assertEqual(owner["pins"], ["Fixture home"])
            self.assertEqual(owner["dealbreakers"], {"industries": ["Gambling"], "keywords": ["fixture-dealbreaker"]})
            self.assertTrue(owner["background"], "background access (scheduled searches) stays on")
            self.assertEqual(owner["me"], "admin")
            self.assertEqual(owner["llm_model"], "claude-fixture-model", "the AI settings in .env still work")
            self.assertEqual(owner["security"]["recovery_codes_left"], 8)
            self.assertEqual(member["jobs"], [["Fixture Delivery Lead", "shortlisted"], ["Fixture Private Role", "applied"]])
            self.assertEqual(member["me"], "user")

    def test_the_routes_of_the_removed_features_are_gone(self):
        for path, status in self.out[OWNER[0]]["gone"].items():
            self.assertIn(status, (404, 405), path)

    def test_two_step_secrets_waiting_sign_ins_and_emailed_links_are_deleted(self):
        after = self.out["after"]
        self.assertEqual(after["two_step"], 0)
        self.assertEqual(after["pending"], 0)
        self.assertEqual(after["links"], [])
        self.assertNotIn("free_searches", after["settings"])
        self.assertEqual(self.out["secrets_left"], 0, "a removed secret is still in the database or its log")

    def test_the_retired_tables_are_dropped_and_the_rest_kept(self):
        for gone in ("memberships", "membership_grants", "email_prefs"):
            self.assertNotIn(gone, self.out["tables"])
        for kept in ("users", "user_keys", "recovery_codes", "sessions", "jobs", "postings", "documents", "workspaces",
                     "auth_tokens", "app_settings", "admin_key_grants"):
            self.assertIn(kept, self.out["tables"])


if __name__ == "__main__":
    unittest.main()
