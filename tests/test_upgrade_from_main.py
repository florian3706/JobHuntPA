"""An install made by the code before the open-source version (origin/main at cb5d0fe) starts with today's code and
keeps everything (docs/design/open-source.md, Part 4 "Existing installs must survive", and Phases A and B).

The fixture in tests/fixtures/upgrade_from_main/ (its README says how it was made) has two accounts, both with
two-step sign-in on, a membership marked as paid at Stripe, a free period with a sealed note, a job digest, unused
emailed links, a sign-in waiting for its two-step code, jobs, a document, a pin, dealbreakers, background access and
an AI provider in .env (with the old SMTP_* and STRIPE_* lines beside it). A fresh process opens a copy of it: the app
must start, both accounts sign in with their password alone and find their data, and what the removed features left
must be gone: the two-step secrets, the waiting sign-in, the emailed links and the retired tables, with no copy of
those secrets left in the database file or its write-ahead log.

Phase B: the admin panel's key material (the admin details key's grant, its public half in the settings, every
account's details sealed to it, the audit log) is gone too, and so is the admin role. Both accounts (the former
admin and the ordinary one) still sign in, with their email address as their name; the former admin can now change
the AI settings (so can the other: there are no roles); the settings survive; no further account can be made; and
the old SMTP_* and STRIPE_* lines leave .env.

Phase D: the jobs get the new applied date column empty, so none shows a date, and the workspaces keep their
scheduled searches on (new workspaces start with them off).
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
    # The admin panel's key material (Phase B)
    secrets += [bytes(b) for (b,) in raw("SELECT sealed_priv FROM admin_key_grants")]
    secrets += [bytes(b) for (b,) in raw("SELECT pii_admin FROM users WHERE pii_admin IS NOT NULL")]
    secrets += [bytes(b) for (b,) in raw("SELECT detail_admin FROM admin_audit WHERE detail_admin IS NOT NULL")]
    out["before"] = {"two_step": raw("SELECT COUNT(*) FROM users WHERE mfa_enabled = 1")[0][0],
                     "secrets": len(secrets),
                     "pending": raw("SELECT COUNT(*) FROM sessions WHERE state = 'pending_mfa'")[0][0],
                     "links": sorted(k for (k,) in raw("SELECT kind FROM auth_tokens")),
                     "memberships": raw("SELECT COUNT(*) FROM memberships")[0][0],
                     "grants": raw("SELECT COUNT(*) FROM membership_grants")[0][0],
                     "digests": raw("SELECT COUNT(*) FROM email_prefs WHERE digest_enabled = 1")[0][0],
                     "grants_of_admin_key": raw("SELECT COUNT(*) FROM admin_key_grants")[0][0],
                     "pii_admin": raw("SELECT COUNT(*) FROM users WHERE pii_admin IS NOT NULL")[0][0],
                     "admins": raw("SELECT COUNT(*) FROM users WHERE role = 'admin'")[0][0],
                     "apk": sorted(k for (k,) in raw("SELECT key FROM app_settings WHERE key LIKE 'apk_%%'"))}
    out["env_before"] = sorted(line.split("=")[0] for line in (Path(FOLDER) / ".env").read_text().splitlines()
                               if "=" in line and not line.startswith("#"))

from fastapi.testclient import TestClient
from backend.app import app

ACCOUNTS = %r
with TestClient(app, base_url="https://jobs.example.invalid") as c:
    for email, password in ACCOUNTS:
        r = c.post("/api/auth/login", json={"name": email, "password": password})
        got = {"login": r.status_code, "answer": sorted(r.json())}
        if r.status_code == 200:
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            ws = c.get("/api/workspaces").json()
            c.headers["X-Workspace"] = str(ws[0]["id"])
            jobs = c.get("/api/jobs?include=excluded,hidden").json()
            got["jobs"] = sorted((j["title"], j["status"]) for j in jobs)
            # Phase D: no job of an older install has an applied date (so none is shown), and its workspace keeps
            # the schedule it had (no row: the preset, which is on).
            got["applied_at"] = sorted((j["title"], j["applied_at"]) for j in jobs)
            got["applied_at_page"] = [c.get(f"/api/jobs/{j['id']}").json()["applied_at"] for j in jobs]
            got["schedule_on"] = c.get("/api/schedule").json()["enabled"]
            docs = c.get("/api/documents").json()
            got["documents"] = [c.get(f"/api/documents/{d['id']}/text").json().get("text") for d in docs]
            got["pins"] = [p["label"] for p in c.get("/api/pins").json()]
            got["dealbreakers"] = c.get("/api/dealbreakers").json()
            got["security"] = c.get("/api/auth/security").json()
            got["background"] = c.get("/api/account/background").json()["enabled"]
            got["me"] = c.get("/api/auth/me").json()["user"]
            # No roles: every account reads the AI settings, and the former admin changes them (and puts them back).
            got["llm_model"] = c.get("/api/settings/llm").json().get("model")
            got["usage"] = c.get("/api/settings/usage").status_code
            got["address"] = c.get("/api/settings/address").json()
            if email == ACCOUNTS[0][0]:
                changed = c.put("/api/settings/llm", json={"model": "fixture-model-changed"})
                got["changed_model"] = (changed.status_code, changed.json().get("model"))
                c.put("/api/settings/llm", json={"model": got["llm_model"]})
            # No further account: the first run is closed and sign-up is gone.
            got["first_run"] = c.post("/api/auth/first-run", json={"token": "x", "name": "intruder",
                                                                    "password": "intruder-password"}).status_code
            # The routes of the removed features are gone (404 with a session; nothing answers them).
            got["gone"] = {path: c.post(path, json={}).status_code for path in (
                "/api/auth/login/mfa", "/api/auth/mfa/setup", "/api/auth/mfa/disable", "/api/auth/forgot",
                "/api/auth/reset", "/api/auth/verify-email", "/api/billing/checkout", "/api/billing/webhook")}
            got["gone"]["/api/account/digest"] = c.get("/api/account/digest").status_code
            got["gone"]["/api/billing/status"] = c.get("/api/billing/status").status_code
            got["gone"]["/api/settings/email"] = c.get("/api/settings/email").status_code
            for path in ("/api/admin/users", "/api/admin/stats", "/api/admin/signup", "/api/admin/audit",
                         "/api/admin/invites"):  # the admin panel (Phase B)
                got["gone"][path] = c.get(path).status_code
            got["gone"]["/api/auth/signup"] = c.post("/api/auth/signup", json={}).status_code
            c.post("/api/auth/logout")
            c.headers.pop("X-Workspace", None)
            c.headers.pop("X-CSRF-Token", None)
        out[email] = got

out["tables"] = sorted(t for (t,) in raw("SELECT name FROM sqlite_master WHERE type = 'table'"))
out["after"] = {"two_step": raw("SELECT COUNT(*) FROM users WHERE mfa_enabled != 0 OR totp_secret_enc IS NOT NULL "
                                "OR totp_pending_enc IS NOT NULL OR totp_last_step IS NOT NULL")[0][0],
                "pending": raw("SELECT COUNT(*) FROM sessions WHERE state = 'pending_mfa'")[0][0],
                "settings": sorted(k for (k,) in raw("SELECT key FROM app_settings")),
                "pii_admin": raw("SELECT COUNT(*) FROM users WHERE pii_admin IS NOT NULL OR pii_admin_version IS NOT NULL")[0][0],
                "roles": sorted(set(r for (r,) in raw("SELECT role FROM users"))),
                "accounts": raw("SELECT COUNT(*) FROM users")[0][0],
                "applied_column": [[r[2], r[4]] for r in raw("PRAGMA table_info(jobs)") if r[1] == "applied_at"],
                "applied_set": raw("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL")[0][0],
                "schedules": raw("SELECT COUNT(*) FROM search_schedules")[0][0]}
out["env_after"] = (Path(FOLDER) / ".env").read_text()
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
        self.assertEqual(before["grants_of_admin_key"], 1)
        self.assertEqual(before["pii_admin"], 2)
        self.assertEqual(before["admins"], 1)
        self.assertEqual(before["apk"], ["apk_public", "apk_version"])
        self.assertIn("STRIPE_SECRET_KEY", self.out["env_before"])
        self.assertIn("SMTP_PASSWORD_B64", self.out["env_before"])
        self.assertEqual(before["two_step"], 2)
        # 2 two-step secrets, 3 emailed links, 1 free period's note, the admin key's grant, 2 details sealed to it
        self.assertGreaterEqual(before["secrets"], 9)
        self.assertEqual(before["pending"], 1)
        self.assertEqual(before["links"], ["email_change", "reset", "verify_email"])
        self.assertEqual((before["memberships"], before["grants"], before["digests"]), (2, 1, 1))

    def test_both_accounts_sign_in_with_their_password_alone(self):
        for run in (self.out, self.again):
            for email, _ in (OWNER, MEMBER):
                got = run[email]
                self.assertEqual(got["login"], 200, got)
                self.assertEqual(got["me"]["name"], email, "the address an account was made with is its name")
                self.assertNotIn("role", got["me"])
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
            self.assertEqual(owner["llm_model"], "claude-fixture-model", "the AI settings in .env still work")
            self.assertEqual(owner["address"]["public_base_url"], "https://jobs.example.invalid")
            self.assertEqual(owner["security"]["recovery_codes_left"], 8)
            self.assertEqual(member["jobs"], [["Fixture Delivery Lead", "shortlisted"], ["Fixture Private Role", "applied"]])
            self.assertEqual(member["llm_model"], "claude-fixture-model", "the former ordinary account reads them too")
            self.assertEqual((owner["usage"], member["usage"]), (200, 200))

    def test_jobs_get_no_applied_date_and_workspaces_keep_their_schedule(self):
        """Phase D: the new sealed applied_at column is added empty (no server default), so no job of an older
        install shows a date, the one in Applied included; and the workspaces keep their scheduled searches on."""
        for run in (self.out, self.again):
            after = run["after"]
            self.assertEqual(len(after["applied_column"]), 1, "the column is added")
            self.assertIsNone(after["applied_column"][0][1], "with no server default")
            self.assertEqual(after["applied_set"], 0)
            self.assertEqual(after["schedules"], 0, "no schedule row is written for an existing workspace")
            for email, _ in (OWNER, MEMBER):
                got = run[email]
                self.assertEqual(got["applied_at"], [["Fixture Delivery Lead", None], ["Fixture Private Role", None]])
                self.assertEqual(got["applied_at_page"], [None, None])
                self.assertTrue(got["schedule_on"], "an existing workspace's scheduled searches stay on")

    def test_the_former_admin_can_change_the_ai_settings(self):
        for run in (self.out, self.again):
            self.assertEqual(run[OWNER[0]]["changed_model"], [200, "fixture-model-changed"])
        self.assertIn("LLM_MODEL=claude-fixture-model", self.again["env_after"])  # (and put back)

    def test_no_further_account_can_be_made(self):
        for run in (self.out, self.again):
            for email, _ in (OWNER, MEMBER):
                self.assertEqual(run[email]["first_run"], 409)
            self.assertEqual(run["after"]["accounts"], 2, "both accounts are kept, none added")

    def test_the_admin_key_material_and_the_role_are_gone(self):
        after = self.out["after"]
        self.assertEqual(after["pii_admin"], 0)
        self.assertEqual(after["roles"], ["user"])
        for key in ("apk_public", "apk_version", "signup_mode"):
            self.assertNotIn(key, after["settings"])
        self.assertIn("server_keys_fingerprint", after["settings"], "the server key stays")
        self.assertEqual(self.out["secrets_left"], 0, "a copy of the admin key material is still in the database")

    def test_the_retired_mail_and_payment_settings_leave_env_and_the_rest_stays(self):
        for run in (self.out, self.again):
            env = run["env_after"]
            for gone in ("SMTP_", "STRIPE_", "sk_test_fixture", "c2VjcmV0"):
                self.assertNotIn(gone, env)
            for kept in ("LLM_BASE_URL=", "LLM_API_KEY=", "LLM_MODEL=", "PUBLIC_BASE_URL=https://jobs.example.invalid"):
                self.assertIn(kept, env)

    def test_the_routes_of_the_removed_features_are_gone(self):
        for path, status in self.out[OWNER[0]]["gone"].items():
            self.assertIn(status, (404, 405), path)

    def test_two_step_secrets_waiting_sign_ins_and_emailed_links_are_deleted(self):
        after = self.out["after"]
        self.assertEqual(after["two_step"], 0)
        self.assertEqual(after["pending"], 0)
        self.assertNotIn("auth_tokens", self.out["tables"])
        self.assertNotIn("free_searches", after["settings"])
        self.assertEqual(self.out["secrets_left"], 0, "a removed secret is still in the database or its log")

    def test_the_retired_tables_are_dropped_and_the_rest_kept(self):
        for gone in ("memberships", "membership_grants", "email_prefs", "admin_key_grants", "admin_audit", "auth_tokens"):
            self.assertNotIn(gone, self.out["tables"])
        for kept in ("users", "user_keys", "recovery_codes", "sessions", "jobs", "postings", "documents", "workspaces",
                     "app_settings", "company_profiles", "llm_usage"):
            self.assertIn(kept, self.out["tables"])


if __name__ == "__main__":
    unittest.main()
