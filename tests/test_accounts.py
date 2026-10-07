"""Accounts and sessions (accounts design, Phase C): first run, the environment bootstrap, sign-in and lockout,
sessions across a restart, CSRF and same-site checks, workspace ownership, admin-only global settings, recovery
codes and the request's key scope.

The first-run, bootstrap and restart cases need a database with no account, or a fresh process, so they run in a
subprocess with throwaway paths of their own (``scenario``). Everything else uses the shared test database, where
the admin from tests/__init__.py exists.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import config, crypto, ratelimit, sessions, users  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import AuthSession, RecoveryCode, SessionLocal, User, UserKeys, Workspace, engine, utcnow  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "correct horse battery"


def tearDownModule():
    helpers.remove_scratch_folders()


scratch_folder = helpers.scratch_folder
scenario = helpers.scenario


FIRST_RUN = '''
from pathlib import Path
from sqlalchemy import text
from backend.db import Job, SessionLocal, Workspace, engine, init_db

# An install from before accounts: two workspaces, each with a job, in plaintext as the old version wrote them.
init_db()
with engine.begin() as conn:
    conn.execute(text("INSERT INTO workspaces (name, created_at) VALUES ('Contract roles', CURRENT_TIMESTAMP)"))
    second = conn.execute(text("SELECT MAX(id) FROM workspaces")).scalar()
    for ws, url, title, status in ((1, "https://legacy.example/1", "Old job one", "applied"),
                                   (second, "https://legacy.example/2", "Old job two", "to_review")):
        conn.execute(text("INSERT INTO jobs (workspace_id, url, title, source, status, hidden, detail_status, "
                          "first_seen, last_seen) VALUES (:w, :u, :t, 'lever:legacy', :s, 0, 'none', "
                          "'2026-09-01 10:00:00.000000', '2026-10-01 10:00:00.000000')"),
                     {"w": ws, "u": url, "t": title, "s": status})
    conn.execute(text("DELETE FROM app_settings WHERE key = 'job_pool_migration'"))  # (from before the pool too)

from fastapi.testclient import TestClient
from backend import crypto, users
from backend.app import app
from backend.db import AdminKeyGrant, AppSetting, EmailPrefs, Membership, RecoveryCode, User, UserKeys

with TestClient(app) as c:
    out["state"] = c.get("/api/auth/state").json()
    out["health"] = c.get("/api/health").status_code
    for path in ("/api/workspaces", "/api/jobs", "/api/settings/llm", "/api/auth/me"):
        r = c.get(path)
        out.setdefault("before", {})[path] = [r.status_code, r.json().get("code")]
    token_file = Path(FOLDER) / "first-run.txt"
    token = token_file.read_text().strip()
    out["token_file_mode"] = oct(token_file.stat().st_mode & 0o777)
    good = {"email": "Owner@Example.com", "password": "correct horse battery", "name": "Pat Owner"}
    out["no_token"] = c.post("/api/auth/first-run", json=good).json()
    out["wrong_token"] = c.post("/api/auth/first-run", json={**good, "token": "x" * 43}).status_code
    out["form_post"] = c.post("/api/auth/first-run", data={**good, "token": token}).status_code
    out["short_password"] = c.post("/api/auth/first-run", json={**good, "token": token, "password": "short"}).json()
    out["bad_email"] = c.post("/api/auth/first-run", json={**good, "token": token, "email": "nope"}).json()
    r = c.post("/api/auth/first-run", json={**good, "token": token, "background": True})
    out["created"] = r.status_code
    out["cookie"] = r.headers.get("set-cookie", "")
    body = r.json()
    out["codes"] = body["recovery_codes"]
    csrf = body["csrf"]
    out["token_file_left"] = token_file.exists()
    out["again"] = c.post("/api/auth/first-run", json={**good, "token": token}).status_code
    out["workspaces"] = sorted(w["name"] for w in c.get("/api/workspaces").json())
    jobs = []
    for w in c.get("/api/workspaces").json():
        jobs += [j["title"] for j in c.get("/api/jobs", headers={"X-Workspace": str(w["id"])}).json()]
    out["jobs"] = sorted(jobs)
    out["me"] = c.get("/api/auth/me").json()
    out["settings"] = c.get("/api/settings/llm").status_code
    out["ack"] = c.post("/api/auth/recovery-codes/ack", headers={"X-CSRF-Token": csrf}).status_code
    out["pending_after_ack"] = c.get("/api/auth/me").json()["recovery_codes_pending"]
    c.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    out["login"] = c.post("/api/auth/login", json={"email": "owner@example.com", "password": good["password"]}).status_code

db = SessionLocal()
user = db.query(User).one()
keys_row = db.get(UserKeys, user.id)
out["user"] = {"role": user.role, "created_via": user.created_via, "pii_admin": user.pii_admin is not None,
               "pii_admin_version": user.pii_admin_version, "background": keys_row.server_wrapped_dek is not None}
out["grant"] = db.query(AdminKeyGrant).filter(AdminKeyGrant.user_id == user.id).count()
out["membership"] = db.query(Membership).filter(Membership.user_id == user.id).count()
out["email_prefs"] = db.query(EmailPrefs).filter(EmailPrefs.user_id == user.id).count()
out["migration"] = db.get(AppSetting, "encryption_migration").value_json
out["owners"] = sorted({o for (o,) in db.query(Workspace.owner_id)})
with engine.connect() as conn:  # the old rows are sealed now (the first run encrypted them with the admin's key)
    out["stored"] = [list(r) for r in conn.execute(text("SELECT typeof(name) FROM workspaces"))] + \
                    [list(r) for r in conn.execute(text("SELECT typeof(status), typeof(hidden), url FROM jobs"))]
out["user_id"] = user.id
# Every recovery code opens the same data key as the password, and so does the admin's grant of the APK.
dek = users.unlock_with_password(db, user, "correct horse battery")
opened = []
for row, code in zip(db.query(RecoveryCode).order_by(RecoveryCode.id), out["codes"]):
    kek = crypto.recovery_kek(users.normalise_recovery_code(code.lower()), keys_row.recovery_salt)
    opened.append(crypto.unwrap(kek, row.wrapped_dek, f"rc|{user.id}|{row.id}") == dek)
out["codes_open_dek"] = opened
with crypto.as_user(user.id, dek):
    priv = crypto.unseal(keys_row.x25519_priv_enc, f"user_keys.x25519_priv|{user.id}")
grant = db.query(AdminKeyGrant).one()
apk_priv = crypto.open_sealed(priv, grant.sealed_priv, f"grant|{user.id}|{grant.apk_version}")
out["pii_admin"] = json.loads(crypto.open_sealed(apk_priv, user.pii_admin, f"users.pii_admin|{user.id}"))
db.close()
'''


class FirstRunTest(unittest.TestCase):
    """A database from before accounts, with no account yet."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario(FIRST_RUN)

    def test_everything_but_setup_waits_for_the_first_account(self):
        out = self.out
        self.assertEqual(out["state"], {"setup_required": True, "signed_in": False})
        self.assertEqual(out["health"], 200)
        for path, (status, code) in out["before"].items():
            self.assertEqual((status, code), (401, "setup_required"), path)

    def test_the_first_run_token_is_needed(self):
        out = self.out
        self.assertEqual(out["token_file_mode"], "0o600")
        self.assertEqual((out["no_token"]["code"], out["no_token"]["field"]), ("bad_token", "token"))
        self.assertEqual(out["wrong_token"], 403)
        self.assertEqual(out["form_post"], 415)  # a form on another site can't send JSON
        self.assertFalse(out["token_file_left"])
        self.assertEqual(out["again"], 409)  # a second first run is refused, token or not

    def test_values_are_checked_field_by_field(self):
        self.assertEqual(self.out["short_password"]["field"], "password")
        self.assertIn("10 characters", self.out["short_password"]["detail"])
        self.assertEqual(self.out["bad_email"]["field"], "email")

    def test_the_first_account_is_an_admin_with_every_key(self):
        out = self.out
        self.assertEqual(out["created"], 201)
        self.assertIn("jh_session=", out["cookie"])
        self.assertIn("HttpOnly", out["cookie"])
        self.assertEqual(out["user"], {"role": "admin", "created_via": "first_run", "pii_admin": True,
                                       "pii_admin_version": 1, "background": True})
        self.assertEqual((out["grant"], out["membership"], out["email_prefs"]), (1, 1, 1))
        migration = json.loads(out["migration"])  # a small database is encrypted within the first-run request
        self.assertEqual((migration["state"], migration["owner"]), ("done", out["user_id"]))
        self.assertEqual(out["stored"], [["blob"], ["blob"], ["blob", "blob", None], ["blob", "blob", None]])
        self.assertEqual(out["pii_admin"], {"email": "Owner@Example.com", "name": "Pat Owner"})
        self.assertEqual(out["me"]["user"]["role"], "admin")
        self.assertEqual(out["me"]["user"]["email"], "Owner@Example.com")
        self.assertEqual(out["settings"], 200)
        self.assertEqual(out["login"], 200)  # the email address is matched whatever its case

    def test_existing_workspaces_and_jobs_are_claimed(self):
        out = self.out
        self.assertEqual(out["workspaces"], ["Contract roles", "My job search"])
        self.assertEqual(out["jobs"], ["Old job one", "Old job two"])
        self.assertEqual(out["owners"], [out["user_id"]])

    def test_recovery_codes_are_shown_once_and_each_opens_the_data_key(self):
        out = self.out
        self.assertEqual(len(out["codes"]), 8)
        self.assertEqual(len(set(out["codes"])), 8)
        for code in out["codes"]:
            self.assertRegex(code, r"^[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}$")
        self.assertEqual(out["codes_open_dek"], [True] * 8)
        self.assertTrue(out["me"]["recovery_codes_pending"])
        self.assertEqual(out["ack"], 200)
        self.assertFalse(out["pending_after_ack"])


BOOT = '''
from fastapi.testclient import TestClient
from backend.app import app
from backend.db import SessionLocal, User
from pathlib import Path

with TestClient(app) as c:
    out["state"] = c.get("/api/auth/state").json()
    out["token_file"] = (Path(FOLDER) / "first-run.txt").exists()
    r = c.post("/api/auth/login", json={"email": "ci@example.invalid", "password": "ci-test-password-123",
                                        "remember": True})
    out["login"] = r.status_code
    if r.status_code == 200:
        out["cookie"] = c.cookies.get("jh_session")
        out["csrf"] = r.json().get("csrf")
        out["workspaces"] = [w["name"] for w in c.get("/api/workspaces").json()]
        out["me"] = c.get("/api/auth/me").json()
db = SessionLocal()
out["users"] = [(u.role, u.created_via) for u in db.query(User)]
db.close()
'''

RESTARTED = '''
from fastapi.testclient import TestClient
from backend.app import app

with TestClient(app) as c:
    c.cookies.set("jh_session", os.environ["SESSION_COOKIE"])
    out["me"] = c.get("/api/auth/me").status_code
    out["write"] = c.post("/api/workspaces", json={"name": "After restart"},
                          headers={"X-CSRF-Token": os.environ["SESSION_CSRF"]}).status_code
    out["workspaces"] = [w["name"] for w in c.get("/api/workspaces").json()]
'''

NO_KEY_FILE = '''
from fastapi.testclient import TestClient
from backend.app import app

os.remove(os.path.join(FOLDER, "server-keys.json"))
try:
    with TestClient(app) as c:
        out["started"] = True
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"
'''

ADMIN_ENV = {"JOBHUNT_ADMIN_EMAIL": "ci@example.invalid", "JOBHUNT_ADMIN_PASSWORD": "ci-test-password-123"}


class BootstrapAndRestartTest(unittest.TestCase):
    def test_environment_bootstrap_then_a_session_survives_a_restart(self):
        boot = scenario(BOOT, env=ADMIN_ENV)
        self.assertEqual(boot["state"], {"setup_required": False, "signed_in": False})
        self.assertFalse(boot["token_file"])
        self.assertEqual(boot["login"], 200)
        self.assertEqual(boot["users"], [["admin", "bootstrap"]])
        self.assertEqual(boot["workspaces"], ["My job search"])
        self.assertTrue(boot["me"]["recovery_codes_pending"])  # shown at the first sign-in instead
        # A new process (no admin variables now): the cookie from before still works, CSRF token included.
        again = scenario(RESTARTED, folder=boot["_folder"],
                         env={"SESSION_COOKIE": boot["cookie"], "SESSION_CSRF": boot["csrf"]})
        self.assertEqual((again["me"], again["write"]), (200, 201))
        self.assertEqual(again["workspaces"], ["My job search", "After restart"])
        # Without the key file that belongs to this database, start-up stops and says what to do.
        lost = scenario(NO_KEY_FILE, folder=boot["_folder"])
        self.assertIn("KeyFileMissing", lost["error"])
        self.assertIn("Restore data/server-keys.json from your backup", lost["error"])

    def test_admin_variables_in_dotenv_are_ignored(self):
        folder = scratch_folder()
        Path(folder, ".env").write_text("JOBHUNT_ADMIN_EMAIL=ci@example.invalid\n"
                                        "JOBHUNT_ADMIN_PASSWORD=ci-test-password-123\n")
        out = scenario(BOOT, folder=folder)
        self.assertEqual(out["state"]["setup_required"], True)  # process environment only
        self.assertTrue(out["token_file"])
        self.assertEqual(out["users"], [])

    def test_a_bad_bootstrap_password_falls_back_to_the_first_run_page(self):
        out = scenario(BOOT, env={"JOBHUNT_ADMIN_EMAIL": "ci@example.invalid", "JOBHUNT_ADMIN_PASSWORD": "short"})
        self.assertTrue(out["state"]["setup_required"])
        self.assertTrue(out["token_file"])


class SignInTestCase(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)

    def anonymous(self, **kwargs) -> TestClient:
        c = TestClient(app, **kwargs)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def signed_in(self, account: helpers.Account | None = None, **kwargs) -> helpers.SignedInClient:
        c = helpers.client(account.email) if account else helpers.client(**kwargs)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c


class LoginTest(SignInTestCase):
    def test_sign_in_me_and_sign_out(self):
        alice = helpers.make_user(name="Alice Example", password=PASSWORD)
        c = self.anonymous()
        self.assertEqual(c.get("/api/auth/state").json(), {"setup_required": False, "signed_in": False})
        r = c.post("/api/auth/login", json={"email": f"  {alice.email.upper()} ", "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        csrf = r.json()["csrf"]
        me = c.get("/api/auth/me").json()
        self.assertEqual(me["user"], {"id": alice.id, "email": alice.email, "name": "Alice Example", "role": "user"})
        self.assertEqual(me["csrf"], csrf)
        self.assertEqual(me["workspaces"], [alice.workspace_id])
        self.assertFalse(me["features"]["admin_panel"])
        self.assertTrue(c.get("/api/auth/state").json()["signed_in"])
        out = c.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        self.assertEqual(out.headers["clear-site-data"], '"cache"')
        self.assertIn("Max-Age=0", out.headers["set-cookie"])
        self.assertEqual(c.get("/api/auth/me").status_code, 401)
        db = SessionLocal()
        self.assertEqual(db.query(AuthSession).filter(AuthSession.user_id == alice.id).count(), 0)
        self.assertEqual(db.get(User, alice.id).last_active_on, datetime.now(timezone.utc).date())
        db.close()

    def test_signing_in_again_replaces_the_old_session(self):
        alice = helpers.make_user()
        c = self.signed_in(alice)
        old = c.cookies.get("jh_session")
        c.sign_in()
        self.assertNotEqual(c.cookies.get("jh_session"), old)
        db = SessionLocal()
        self.assertEqual(db.query(AuthSession).filter(AuthSession.user_id == alice.id).count(), 1)
        db.close()
        stale = self.anonymous()
        stale.cookies.set("jh_session", old)
        self.assertEqual(stale.get("/api/auth/me").status_code, 401)

    def test_wrong_password_and_unknown_email_get_the_same_answer(self):
        alice = helpers.make_user()
        c = self.anonymous()
        wrong = c.post("/api/auth/login", json={"email": alice.email, "password": "not the password"})
        unknown = c.post("/api/auth/login", json={"email": "nobody@example.invalid", "password": "whatever123"})
        self.assertEqual((wrong.status_code, unknown.status_code), (401, 401))
        self.assertEqual(wrong.json(), unknown.json())
        self.assertNotIn("set-cookie", wrong.headers)

    def test_lockout_after_five_failures_doubles_and_ends(self):
        alice = helpers.make_user()
        c = self.anonymous()
        for _ in range(5):
            self.assertEqual(c.post("/api/auth/login", json={"email": alice.email, "password": "wrong-password"}).status_code, 401)
        locked = c.post("/api/auth/login", json={"email": alice.email, "password": alice.password})
        self.assertEqual(locked.status_code, 429)  # even the right password waits
        self.assertEqual(locked.json()["code"], "locked")
        self.assertTrue(25 <= int(locked.headers["retry-after"]) <= 30)
        db = SessionLocal()
        user = db.get(User, alice.id)
        self.assertEqual(user.failed_logins, 5)
        user.locked_until = utcnow() - timedelta(seconds=1)  # the 30 seconds are over
        db.commit()
        # A sixth failure waits twice as long.
        self.assertEqual(c.post("/api/auth/login", json={"email": alice.email, "password": "wrong-password"}).status_code, 401)
        db.refresh(user)
        self.assertAlmostEqual((user.locked_until - utcnow()).total_seconds(), 60, delta=5)
        user.locked_until = utcnow() - timedelta(seconds=1)
        db.commit()
        self.assertEqual(c.post("/api/auth/login", json={"email": alice.email, "password": alice.password}).status_code, 200)
        db.refresh(user)
        self.assertEqual((user.failed_logins, user.locked_until), (0, None))
        db.close()
        self.assertEqual([ratelimit.lockout_seconds(n) for n in (4, 5, 6, 7, 10, 30)], [0, 30, 60, 120, 900, 900])

    def test_an_unknown_email_is_locked_the_same_way(self):
        c = self.anonymous()
        for _ in range(5):
            c.post("/api/auth/login", json={"email": "ghost@example.invalid", "password": "wrong-password"})
        r = c.post("/api/auth/login", json={"email": "ghost@example.invalid", "password": "wrong-password"})
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))

    def test_rate_limit_per_address(self):
        c = self.anonymous()
        for i in range(10):
            self.assertNotEqual(c.post("/api/auth/login", json={"email": f"x{i}@example.invalid", "password": "x"}).status_code, 429)
        r = c.post("/api/auth/login", json={"email": "y@example.invalid", "password": "x"})
        self.assertEqual((r.status_code, r.json()["code"]), (429, "rate_limited"))
        self.assertGreater(int(r.headers["retry-after"]), 0)
        limiter = ratelimit.Limiter([(2, 10), (3, 100)])
        self.assertIsNone(limiter.hit("a", now=0))
        self.assertIsNone(limiter.hit("a", now=1))
        self.assertAlmostEqual(limiter.hit("a", now=2), 8)
        self.assertIsNone(limiter.hit("a", now=10.5))
        self.assertAlmostEqual(limiter.hit("a", now=20), 80)  # three in 100 seconds

    def test_sign_in_takes_json_only(self):
        c = self.anonymous()
        r = c.post("/api/auth/login", data={"email": "a@example.invalid", "password": "x"})
        self.assertEqual((r.status_code, r.json()["code"]), (415, "json_required"))

    def test_disabled_account(self):
        alice = helpers.make_user()
        c = self.signed_in(alice)
        db = SessionLocal()
        db.get(User, alice.id).status = "disabled"
        db.commit()
        self.assertEqual(c.get("/api/workspaces").status_code, 401)  # its sessions stop at once
        self.assertEqual(c.post("/api/auth/login", json={"email": alice.email, "password": "wrong-password"}).status_code, 401)
        r = c.post("/api/auth/login", json={"email": alice.email, "password": alice.password})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "disabled"))
        db.close()

    def test_a_changed_kdf_profile_is_applied_at_the_next_sign_in(self):
        alice = helpers.make_user()
        with mock.patch.dict(os.environ, {"JOBHUNT_KDF_PROFILE": "low"}):
            self.signed_in(alice)
        db = SessionLocal()
        self.assertEqual(crypto.KdfParams.from_json(db.get(User, alice.id).kdf_params), crypto.KDF_PROFILES["low"])
        db.close()
        self.signed_in(alice)  # and back: the password still works

    def test_cookie_names_and_flags(self):
        alice = helpers.make_user()
        plain = self.anonymous().post("/api/auth/login", json={"email": alice.email, "password": alice.password})
        cookie = plain.headers["set-cookie"]
        self.assertTrue(cookie.startswith("jh_session="))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertNotIn("Max-Age", cookie)  # a browser session unless "Keep me signed in"
        https = self.anonymous(base_url="https://testserver")
        r = https.post("/api/auth/login", json={"email": alice.email, "password": alice.password, "remember": True})
        cookie = r.headers["set-cookie"]
        self.assertTrue(cookie.startswith("__Host-jh_session="))
        self.assertIn("Secure", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn(f"Max-Age={90 * 24 * 3600}", cookie)
        self.assertEqual(https.get("/api/auth/me").status_code, 200)
        # Over HTTPS only the __Host- cookie counts: a plain one (which anyone on the network could plant) doesn't.
        value = https.cookies.get("__Host-jh_session")
        https.cookies.clear()
        https.cookies.set("jh_session", value)
        self.assertEqual(https.get("/api/auth/me").status_code, 401)
        # A proxy that ends TLS without saying so: the https public address decides.
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://jobs.example.com"}):
            proxied = self.anonymous(base_url="http://jobs.example.com")
            r = proxied.post("/api/auth/login", json={"email": alice.email, "password": alice.password})
            self.assertTrue(r.headers["set-cookie"].startswith("__Host-jh_session="))

    def test_sessions_expire(self):
        alice = helpers.make_user()
        c = self.signed_in(alice)
        db = SessionLocal()
        row = db.query(AuthSession).filter(AuthSession.user_id == alice.id).one()
        self.assertFalse(row.persistent)
        self.assertAlmostEqual((row.idle_expires_at - row.created_at).total_seconds(), 24 * 3600, delta=5)
        self.assertAlmostEqual((row.absolute_expires_at - row.created_at).total_seconds(), 7 * 24 * 3600, delta=5)
        # Idle time moves on (at most every 5 minutes).
        row.last_seen_at = utcnow() - timedelta(minutes=10)
        row.idle_expires_at = utcnow() + timedelta(hours=1)
        db.commit()
        self.assertEqual(c.get("/api/workspaces").status_code, 200)
        db.refresh(row)
        self.assertGreater(row.idle_expires_at, utcnow() + timedelta(hours=23))
        row.idle_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        r = c.get("/api/workspaces")
        self.assertEqual((r.status_code, r.json()["code"]), (401, "signed_out"))
        self.assertIn("Max-Age=0", r.headers["set-cookie"])
        self.assertEqual(db.query(AuthSession).filter(AuthSession.user_id == alice.id).count(), 0)
        db.close()


class CsrfAndOriginTest(SignInTestCase):
    def test_csrf_token_is_needed_for_changes(self):
        c = self.signed_in()
        token = c.headers.pop("X-CSRF-Token")
        self.assertEqual(c.get("/api/workspaces").status_code, 200)  # reading needs none
        r = c.post("/api/workspaces", json={"name": "No token"})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "csrf"))
        self.assertEqual(c.post("/api/workspaces", json={"name": "Wrong"}, headers={"X-CSRF-Token": "x" * 43}).status_code, 403)
        other = self.signed_in(helpers.make_user())  # another session's token doesn't fit this one
        self.assertEqual(c.post("/api/workspaces", json={"name": "Theirs"},
                                headers={"X-CSRF-Token": other.headers["X-CSRF-Token"]}).status_code, 403)
        self.assertEqual(c.post("/api/auth/logout").status_code, 403)
        r = c.post("/api/workspaces", json={"name": "With token"}, headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 201)
        c.headers["X-CSRF-Token"] = token
        c.delete(f"/api/workspaces/{r.json()['id']}")

    def test_changes_from_another_site_are_refused(self):
        c = self.signed_in()
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Referer": "https://evil.example/page"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Origin": "http://testserver.evil.example"}):
            r = c.post("/api/workspaces", json={"name": "Cross-site"}, headers=headers)
            self.assertEqual((r.status_code, r.json()["code"]), (403, "bad_origin"), headers)
        # Sign-in too (login CSRF), with or without a session.
        r = self.anonymous().post("/api/auth/login", json={"email": helpers.ADMIN_EMAIL, "password": helpers.ADMIN_PASSWORD},
                                  headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        ok = []
        for headers in ({"Origin": "http://testserver"}, {"Origin": "https://testserver"},
                        {"Referer": "http://testserver/index.html"}, {"Sec-Fetch-Site": "same-origin"}):
            r = c.post("/api/workspaces", json={"name": "Same site"}, headers=headers)
            self.assertEqual(r.status_code, 201, headers)
            ok.append(r.json()["id"])
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://jobs.example.com"}):
            r = c.post("/api/workspaces", json={"name": "Public address"}, headers={"Origin": "https://jobs.example.com"})
            self.assertEqual(r.status_code, 201)
            ok.append(r.json()["id"])
        for ws in ok:
            c.delete(f"/api/workspaces/{ws}")

    def test_reading_from_another_site_has_no_cors(self):
        c = self.signed_in()
        r = c.get("/api/workspaces", headers={"Origin": "https://evil.example"})
        self.assertNotIn("access-control-allow-origin", r.headers)


class OwnershipTest(SignInTestCase):
    def test_another_users_workspace_is_not_found(self):
        alice, bob = helpers.make_user(), helpers.make_user()
        a = self.signed_in(alice)
        b = self.signed_in(bob)
        b.put("/api/profile", json={"titles": ["Bob's secret title"]})
        db = SessionLocal()
        from backend.db import Job
        with helpers.as_account(bob):
            job = Job(workspace_id=bob.workspace_id, url="https://bob.example/1", title="Bob's job", status="to_review")
            db.add(job)
            db.commit()
            job_id = job.id
        db.close()
        theirs = {"X-Workspace": str(bob.workspace_id)}
        for method, path, body in (("get", "/api/jobs", None), ("get", "/api/profile", None),
                                   ("put", "/api/profile", {"titles": ["x"]}), ("get", "/api/documents", None),
                                   ("get", "/api/sources", None), ("get", "/api/schedule", None),
                                   ("get", "/api/pins", None), ("get", "/api/runs/latest", None),
                                   ("get", "/api/score/status", None), ("get", f"/api/jobs/{job_id}", None),
                                   ("get", "/api/duplicates", None), ("get", "/api/companies", None)):
            r = getattr(a, method)(path, headers=theirs, **({"json": body} if body else {}))
            self.assertEqual(r.status_code, 404, f"{method} {path}: {r.text}")
            self.assertNotIn("secret", r.text)
        self.assertEqual(a.get(f"/api/jobs?ws={bob.workspace_id}").status_code, 404)  # plain links too
        self.assertEqual(a.get(f"/api/jobs/{job_id}").status_code, 404)  # by id in her own workspace
        self.assertEqual(a.patch(f"/api/workspaces/{bob.workspace_id}", json={"name": "Mine now"}).status_code, 404)
        self.assertEqual(a.delete(f"/api/workspaces/{bob.workspace_id}").status_code, 404)
        self.assertEqual(a.post("/api/workspaces", json={"name": "Copy", "copy_from": bob.workspace_id}).status_code, 404)
        self.assertEqual([w["id"] for w in a.get("/api/workspaces").json()], [alice.workspace_id])
        self.assertEqual(b.get("/api/profile").json()["titles"], ["Bob's secret title"])  # untouched
        self.assertEqual(b.get(f"/api/jobs/{job_id}").json()["title"], "Bob's job")

    def test_each_account_has_its_own_workspaces(self):
        alice = helpers.make_user()
        a = self.signed_in(alice)
        self.assertEqual(a.get("/api/profile").status_code, 200)  # no header: her first workspace
        second = a.post("/api/workspaces", json={"name": "Second", "copy_from": alice.workspace_id}).json()
        self.assertEqual([w["name"] for w in a.get("/api/workspaces").json()], ["My job search", "Second"])
        self.assertEqual(a.delete(f"/api/workspaces/{second['id']}").status_code, 200)
        r = a.delete(f"/api/workspaces/{alice.workspace_id}")  # the admin's workspaces don't count as hers
        self.assertEqual(r.status_code, 409)
        admin = self.signed_in()
        self.assertNotIn(alice.workspace_id, [w["id"] for w in admin.get("/api/workspaces").json()])
        self.assertEqual(admin.get("/api/jobs", headers={"X-Workspace": str(alice.workspace_id)}).status_code, 404)


class AdminOnlySettingsTest(SignInTestCase):
    LLM_ENV = {"LLM_API_KEY": "sk-secret-key-1234", "LLM_BASE_URL": "https://llm.example.com/v1",
               "LLM_MODEL": "model-name-xyz"}

    def test_global_settings_are_refused_to_users(self):
        user = self.signed_in(helpers.make_user())
        env_before = config.ENV_PATH.read_text() if config.ENV_PATH.exists() else None
        for method, path, body in (("get", "/api/settings/llm", None), ("put", "/api/settings/llm", {"model": "x"}),
                                   ("get", "/api/settings/keys", None), ("put", "/api/settings/keys", {"tomtom": "abcdefgh1"}),
                                   ("post", "/api/settings/keys/test", None), ("get", "/api/llm", None),
                                   ("put", "/api/llm/reasoning", {"scoring": "high"}),
                                   ("post", "/api/llm/detect-levels", None), ("post", "/api/score/test", None)):
            r = getattr(user, method)(path, **({"json": body} if body else {}))
            self.assertEqual(r.status_code, 403, f"{method} {path}")
        self.assertEqual(config.ENV_PATH.read_text() if config.ENV_PATH.exists() else None, env_before)
        admin = self.signed_in()
        with mock.patch.dict(os.environ, self.LLM_ENV):
            self.assertEqual(admin.get("/api/settings/llm").json()["model"], "model-name-xyz")
            self.assertEqual(admin.get("/api/llm").status_code, 200)
            self.assertEqual(admin.get("/api/settings/keys").status_code, 200)

    def test_score_status_shows_users_no_model_or_address(self):
        user = self.signed_in(helpers.make_user())
        admin = self.signed_in()
        with mock.patch.dict(os.environ, self.LLM_ENV):
            mine = user.get("/api/score/status")
            self.assertEqual(mine.status_code, 200)
            out = mine.json()
            for hidden in ("base_url", "model", "provider", "reasoning_effort"):
                self.assertNotIn(hidden, out)
            self.assertEqual(out["capabilities"], {"web_search": True})
            self.assertTrue(out["configured"])
            for text in ("model-name-xyz", "llm.example.com", "custom"):
                self.assertNotIn(text, mine.text)
            self.assertEqual(admin.get("/api/score/status").json()["model"], "model-name-xyz")
        with mock.patch.dict(os.environ, {"LLM_MODEL": "", "LLM_BASE_URL": "", "LLM_API_KEY": ""}):
            out = user.get("/api/score/status").json()
            self.assertEqual((out["configured"], out["problem"]), (False, "AI features aren't set up yet. Ask your admin."))

    def test_commute_status_says_only_what_is_available(self):
        user = self.signed_in(helpers.make_user())
        with mock.patch.dict(os.environ, {"TFNSW_API_KEY": "tfnsw-key-123456", "TOMTOM_API_KEY": ""}):
            r = user.get("/api/commute/status")
        self.assertEqual(r.json(), {"transit": True, "traffic": False})
        self.assertNotIn("tfnsw-key", r.text)


class RecoveryCodeTest(SignInTestCase):
    def test_codes_open_the_data_key_however_they_are_typed(self):
        account = users.create_user(f"codes-{os.getpid()}@example.invalid", PASSWORD)
        db = SessionLocal()
        salt = db.get(UserKeys, account.user_id).recovery_salt
        rows = db.query(RecoveryCode).filter(RecoveryCode.user_id == account.user_id).order_by(RecoveryCode.id).all()
        db.close()
        self.assertEqual(len(rows), 8)
        for row, code in zip(rows, account.recovery_codes):
            messy = " " + code.lower().replace("-", " ").replace("0", "o").replace("1", "l") + " "
            kek = crypto.recovery_kek(users.normalise_recovery_code(messy), salt)
            self.assertEqual(crypto.unwrap(kek, row.wrapped_dek, f"rc|{account.user_id}|{row.id}"), account.dek)
        self.assertIsNone(users.normalise_recovery_code("too short"))
        self.assertIsNone(users.normalise_recovery_code("UUUU-UUUU-UUUU-UUUU"))  # not in the alphabet

    def test_pending_codes_until_acknowledged(self):
        email = f"pending-{os.getpid()}@example.invalid"
        account = users.create_user(email, PASSWORD)
        c = helpers.client(email, PASSWORD)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        self.assertTrue(c.get("/api/auth/me").json()["recovery_codes_pending"])
        self.assertEqual(c.get("/api/auth/recovery-codes").json()["codes"], account.recovery_codes)
        self.assertEqual(c.post("/api/auth/recovery-codes/ack").status_code, 200)
        self.assertFalse(c.get("/api/auth/me").json()["recovery_codes_pending"])
        self.assertEqual(c.get("/api/auth/recovery-codes").status_code, 404)


class KeyScopeTest(SignInTestCase):
    """The request's key scope reaches async and sync endpoints, sync dependencies and background runs."""

    def test_scope_reaches_endpoints_dependencies_and_runs(self):
        from backend import tasks
        from backend.sessions import AuthMiddleware

        probe = FastAPI()
        probe.add_middleware(AuthMiddleware)

        def scope_user() -> int:
            return crypto.current().user_id

        @probe.get("/api/probe/async")
        async def async_probe():
            return {"user": crypto.current().user_id}

        @probe.get("/api/probe/sync")
        def sync_probe(dep: int = Depends(scope_user)):
            return {"user": crypto.current().user_id, "dep": dep}

        @probe.get("/api/probe/run")
        def run_probe(request: Request):
            ws = request.query_params["ws"]
            run = tasks.start("probe", lambda progress: {"user": crypto.current().user_id}, int(ws))
            return run

        alice = helpers.make_user()
        main = self.signed_in(alice)
        with TestClient(probe) as c:
            c.cookies.set("jh_session", main.cookies.get("jh_session"))
            self.assertEqual(c.get("/api/probe/async").json(), {"user": alice.id})
            self.assertEqual(c.get("/api/probe/sync").json(), {"user": alice.id, "dep": alice.id})
            run = c.get(f"/api/probe/run?ws={alice.workspace_id}").json()
        import time
        for _ in range(200):
            state = main.get(f"/api/runs/{run['id']}").json()
            if state["state"] not in ("queued", "running"):
                break
            time.sleep(0.02)
        self.assertEqual(state["summary"], {"user": alice.id})
        self.assertFalse(crypto.has_scope())  # nothing leaks into the test's own context

    def test_me_opens_sealed_details_in_a_sync_endpoint(self):
        alice = helpers.make_user(name="Scoped Name")
        self.assertEqual(self.signed_in(alice).get("/api/auth/me").json()["user"]["name"], "Scoped Name")


class StartupAndSchedulerTest(SignInTestCase):
    def test_engine_hides_bound_values_from_error_messages(self):
        self.assertTrue(engine.hide_parameters)

    def test_the_scheduler_skips_workspaces_without_an_owner(self):
        from backend import schedule
        from backend.db import UserProfile

        db = SessionLocal()
        with helpers.as_admin():  # (written as the admin; the scheduler finds its owner's key itself)
            ws = Workspace(name="Ownerless")
            db.add(ws)
            db.commit()
            db.add(UserProfile(id=ws.id, titles=["Product Manager"], seek_enabled=True))
            db.commit()
            ws_id = ws.id
        started = []

        def tick():
            with mock.patch.object(schedule, "nothing_to_search", side_effect=lambda d, w: w != ws_id):
                return schedule.tick(lambda w: started.append(w) or {"id": 1}, datetime(2026, 10, 2, 0, 20, tzinfo=timezone.utc))

        try:
            self.assertIsNone(tick())
            self.assertEqual(started, [])
            db.query(Workspace).filter(Workspace.id == ws_id).update({Workspace.owner_id: helpers.admin_id()})
            db.commit()
            tick()
            self.assertEqual(started, [ws_id])  # with the owner's background-access key
        finally:
            db.query(Workspace).filter(Workspace.id == ws_id).delete()
            db.commit()
            db.close()

    def test_a_restart_in_process_keeps_sessions(self):
        alice = helpers.make_user()
        c = self.signed_in(alice)
        cookie = c.cookies.get("jh_session")
        users.startup()  # what the next start does: the key file again, expired sessions cleared
        fresh = self.anonymous()
        fresh.cookies.set("jh_session", cookie)
        self.assertEqual(fresh.get("/api/auth/me").json()["user"]["id"], alice.id)

    def test_last_active_is_recorded_by_day(self):
        alice = helpers.make_user()
        self.signed_in(alice).get("/api/workspaces")
        db = SessionLocal()
        self.assertIsInstance(db.get(User, alice.id).last_active_on, date)
        db.close()


if __name__ == "__main__":
    unittest.main()
