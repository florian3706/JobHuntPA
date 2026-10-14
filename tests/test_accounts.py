"""Accounts and sessions (accounts design, Phase C): first run, the environment bootstrap, sign-in and lockout,
sessions across a restart, CSRF and same-site checks, workspace ownership, the install's settings (any signed-in
account's), recovery codes and the request's key scope.

The first-run, bootstrap and restart cases need a database with no account, or a fresh process, so they run in a
subprocess with throwaway paths of their own (``scenario``). Everything else uses the shared test database, where
the account from tests/__init__.py exists (and the extra accounts tests/helpers.py writes, as an older install has).
"""
from __future__ import annotations

import json
import os
import threading
import time
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend import config, crypto, lockout, ratelimit, sessions, users  # noqa: E402
from backend import db as db_module  # noqa: E402
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
from backend.db import AppSetting, RecoveryCode, User, UserKeys
from sqlalchemy import inspect

with TestClient(app) as c:
    out["state"] = c.get("/api/auth/state").json()
    out["health"] = c.get("/api/health").status_code
    for path in ("/api/workspaces", "/api/jobs", "/api/settings/llm", "/api/auth/me"):
        r = c.get(path)
        out.setdefault("before", {})[path] = [r.status_code, r.json().get("code")]
    token_file = Path(FOLDER) / "first-run.txt"
    token = token_file.read_text().strip()
    out["token_file_mode"] = oct(token_file.stat().st_mode & 0o777)
    good = {"name": "Pat Owner", "password": "correct horse battery"}
    out["no_token"] = c.post("/api/auth/first-run", json=good).json()
    out["wrong_token"] = c.post("/api/auth/first-run", json={**good, "token": "x" * 43}).status_code
    out["form_post"] = c.post("/api/auth/first-run", data={**good, "token": token}).status_code
    out["short_password"] = c.post("/api/auth/first-run", json={**good, "token": token, "password": "short"}).json()
    out["bad_name"] = c.post("/api/auth/first-run", json={**good, "token": token, "name": "two\\nlines"}).json()
    out["no_name"] = c.post("/api/auth/first-run", json={**good, "token": token, "name": "  "}).json()
    out["old_fields"] = c.post("/api/auth/first-run", json={**good, "token": token, "email": "a@b.example"}).status_code
    r = c.post("/api/auth/first-run", json={**good, "token": token, "background": True})
    out["created"] = r.status_code
    out["cookie"] = r.headers.get("set-cookie", "")
    body = r.json()
    out["codes"] = body["recovery_codes"]
    csrf = body["csrf"]
    out["token_file_left"] = token_file.exists()
    out["again"] = c.post("/api/auth/first-run", json={**good, "token": token}).status_code
    out["again_other"] = c.post("/api/auth/first-run", json={"token": token, "name": "Someone Else",
                                                             "password": "another password 123"}).status_code
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
    out["login"] = c.post("/api/auth/login", json={"name": "  pat OWNER ", "password": good["password"]}).status_code
    from backend import ratelimit
    ratelimit.AUTH.reset()  # (the tries above count against this address)
    out["login_other"] = c.post("/api/auth/login", json={"name": "Someone Else",
                                                          "password": "another password 123"}).status_code

db = SessionLocal()
user = db.query(User).one()
keys_row = db.get(UserKeys, user.id)
out["user"] = {"created_via": user.created_via, "background": keys_row.server_wrapped_dek is not None}
out["accounts"] = db.query(User).count()
out["tables"] = sorted(inspect(db.get_bind()).get_table_names())
out["user_columns"] = sorted(c["name"] for c in inspect(db.get_bind()).get_columns("users"))
out["migration"] = db.get(AppSetting, "encryption_migration").value_json
out["owners"] = sorted({o for (o,) in db.query(Workspace.owner_id)})
with engine.connect() as conn:  # the old rows are sealed now (the first run encrypted them with the account's key)
    out["stored"] = [list(r) for r in conn.execute(text("SELECT typeof(name) FROM workspaces"))] + \
                    [list(r) for r in conn.execute(text("SELECT typeof(status), typeof(hidden), url FROM jobs"))]
out["user_id"] = user.id
# Every recovery code opens the same data key as the password.
unlocked = users.unlock(db, user, "correct horse battery")
dek, ik = unlocked.dek, unlocked.ik
opened, opened_ik = [], []
for row, code in zip(db.query(RecoveryCode).order_by(RecoveryCode.id), out["codes"]):
    kek = crypto.recovery_kek(users.normalise_recovery_code(code.lower()), keys_row.recovery_salt)
    opened.append(crypto.unwrap(kek, row.wrapped_dek, f"rc|{user.id}|{row.id}") == dek)
    opened_ik.append(crypto.unwrap(kek, row.wrapped_ik, f"rc-ik|{user.id}|{row.id}") == ik)
out["codes_open_dek"] = opened
out["codes_open_ik"] = opened_ik
# The private key is under the identity key, which only the password and the codes open (and a session's cookie).
priv = crypto.unwrap(ik, keys_row.x25519_priv_enc, f"ik-priv|{user.id}")
out["priv_needs_ik"] = True
try:
    with crypto.as_user(user.id, dek):
        crypto.unseal(keys_row.x25519_priv_enc, f"user_keys.x25519_priv|{user.id}")
    out["priv_needs_ik"] = False
except crypto.CryptoError:
    pass
with crypto.as_user(user.id, dek):
    out["pii_self"] = json.loads(crypto.unseal(user.pii_self, f"users.pii_self|{user.id}"))
db.close()
'''


class FirstRunTest(unittest.TestCase):
    """A database from before accounts, with no account yet."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario(FIRST_RUN)

    def test_everything_but_setup_waits_for_the_first_account(self):
        out = self.out
        self.assertEqual(out["state"], {"setup_required": True, "folder_required": False, "signed_in": False})
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
        self.assertEqual(out["again_other"], 409)  # ... whoever it is for: one account per install
        self.assertEqual(out["login_other"], 401)
        self.assertEqual(out["accounts"], 1)

    def test_values_are_checked_field_by_field(self):
        self.assertEqual(self.out["short_password"]["field"], "password")
        self.assertIn("10 characters", self.out["short_password"]["detail"])
        self.assertEqual(self.out["bad_name"]["field"], "name")
        self.assertEqual(self.out["no_name"]["field"], "name")
        self.assertEqual(self.out["old_fields"], 422)  # (no email address is asked for any more)

    def test_the_first_account_has_a_name_every_key_and_no_role(self):
        out = self.out
        self.assertEqual(out["created"], 201)
        self.assertIn("jh_session=", out["cookie"])
        self.assertIn("HttpOnly", out["cookie"])
        self.assertEqual(out["user"], {"created_via": "first_run", "background": True})
        for gone in ("memberships", "membership_grants", "email_prefs", "admin_key_grants", "admin_audit",
                     "auth_tokens"):  # removed features make no tables
            self.assertNotIn(gone, out["tables"])
        for gone in ("role", "pii_admin", "pii_admin_version"):  # ... and a new database no such columns
            self.assertNotIn(gone, out["user_columns"])
        migration = json.loads(out["migration"])  # a small database is encrypted within the first-run request
        self.assertEqual((migration["state"], migration["owner"]), ("done", out["user_id"]))
        self.assertEqual(out["stored"], [["blob"], ["blob"], ["blob", "blob", None], ["blob", "blob", None]])
        self.assertEqual(out["pii_self"], {"name": "Pat Owner"})
        self.assertEqual(out["me"]["user"], {"id": out["user_id"], "name": "Pat Owner"})
        self.assertEqual(out["settings"], 200)
        self.assertEqual(out["login"], 200)  # the name is matched whatever its case and the spaces around it

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
        self.assertEqual(out["codes_open_ik"], [True] * 8)  # a code opens the identity key too
        self.assertTrue(out["priv_needs_ik"])  # the data key alone doesn't open the private key
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
    r = c.post("/api/auth/login", json={"name": "ci@example.invalid", "password": "ci-test-password-123",
                                        "remember": True})
    out["login"] = r.status_code
    if r.status_code == 200:
        out["cookie"] = c.cookies.get("jh_session")
        out["csrf"] = r.json().get("csrf")
        out["workspaces"] = [w["name"] for w in c.get("/api/workspaces").json()]
        out["me"] = c.get("/api/auth/me").json()
db = SessionLocal()
out["users"] = [u.created_via for u in db.query(User)]
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
        self.assertEqual({k: boot["state"][k] for k in ("setup_required", "signed_in")},
                         {"setup_required": False, "signed_in": False})
        self.assertFalse(boot["token_file"])
        self.assertEqual(boot["login"], 200)
        self.assertEqual(boot["users"], ["bootstrap"])
        self.assertEqual(boot["workspaces"], ["My job search"])
        self.assertTrue(boot["me"]["recovery_codes_pending"])  # shown at the first sign-in instead
        # A new process (no account variables now): the cookie from before still works, CSRF token included.
        again = scenario(RESTARTED, folder=boot["_folder"],
                         env={"SESSION_COOKIE": boot["cookie"], "SESSION_CSRF": boot["csrf"]})
        self.assertEqual((again["me"], again["write"]), (200, 201))
        self.assertEqual(again["workspaces"], ["My job search", "After restart"])
        # Without the key file that belongs to this database, start-up stops and says what to do.
        lost = scenario(NO_KEY_FILE, folder=boot["_folder"])
        self.assertIn("KeyFileMissing", lost["error"])
        self.assertIn("Restore server-keys.json (in the data folder) from your backup", lost["error"])

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
        alice = helpers.make_user(f"Alice Example {os.urandom(3).hex()}", password=PASSWORD)
        c = self.anonymous()
        state = c.get("/api/auth/state").json()
        self.assertEqual(state, {"setup_required": False, "folder_required": False, "signed_in": False})  # nothing about signing up
        r = c.post("/api/auth/login", json={"name": f"  {alice.email.upper()} ", "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["user"], {"id": alice.id})
        csrf = r.json()["csrf"]
        me = c.get("/api/auth/me").json()
        self.assertEqual(me["user"], {"id": alice.id, "name": alice.email})
        self.assertEqual(me["csrf"], csrf)
        self.assertEqual(me["workspaces"], [alice.workspace_id])
        self.assertEqual(me["features"], {"scheduled_searches": True})
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

    def fake_time(self) -> types.SimpleNamespace:
        """A clock for the limits only (ratelimit's own ``time``; the rest of the process keeps the real one). It starts
        at a reading where ``now + 30 - now`` is 30.000000000000057, which once made Retry-After say 31."""
        clock = types.SimpleNamespace(t=488.432919602)
        patch = mock.patch.object(ratelimit, "time", types.SimpleNamespace(monotonic=lambda: clock.t))
        patch.start()
        self.addCleanup(patch.stop)
        return clock

    def try_login(self, c, alice, password=None):
        ratelimit.AUTH.reset()  # (the per-address request limit is not what is under test)
        return c.post("/api/auth/login", json={"email": alice.email, "password": password or alice.password})

    def test_lockout_after_five_failures_doubles_and_ends(self):
        clock = self.fake_time()
        alice = helpers.make_user()
        c = self.anonymous()
        for _ in range(5):
            self.assertEqual(self.try_login(c, alice, "wrong-password").status_code, 401)
        locked = self.try_login(c, alice)
        self.assertEqual(locked.status_code, 429)  # even the right password waits
        self.assertEqual(locked.json()["code"], "locked")
        self.assertTrue(25 <= int(locked.headers["retry-after"]) <= 30)
        self.assertEqual(helpers.wrong_tries(alice.id), 5)
        clock.t += 31  # the 30 seconds are over
        # A sixth failure waits twice as long.
        self.assertEqual(self.try_login(c, alice, "wrong-password").status_code, 401)
        again = self.try_login(c, alice)
        self.assertEqual(again.status_code, 429)
        self.assertTrue(55 <= int(again.headers["retry-after"]) <= 60)
        clock.t += 61
        self.assertEqual(self.try_login(c, alice).status_code, 200)
        self.assertEqual((helpers.wrong_tries(alice.id), ratelimit.guesses.waiting(alice.id)), (0, 0))
        self.assertEqual([ratelimit.lockout_seconds(n) for n in (4, 5, 6, 7, 10, 30)], [0, 30, 60, 120, 900, 900])
        noisy = 518.4329196020001 - 488.432919602  # 30.000000000000057
        self.assertEqual((ratelimit.retry_after(noisy), ratelimit.wait_text(noisy)), ("30", "30 seconds"))
        self.assertEqual([ratelimit.retry_after(s) for s in (0, 0.2, 29.5, 30.001)], ["1", "1", "30", "31"])
        # No permanent lockout: at the cap the wait is 15 minutes, and then one more try is allowed.
        for _ in range(12):
            clock.t += 901
            self.try_login(c, alice, "wrong-password")
        clock.t += 901
        self.assertEqual(self.try_login(c, alice).status_code, 200)

    def test_an_outsider_who_knows_the_address_cannot_lock_the_owner_out(self):
        alice = helpers.make_user()
        outsider = self.anonymous(client=("203.0.113.9", 4000))
        for _ in range(5):
            self.assertEqual(self.try_login(outsider, alice, "guess-guess-guess").status_code, 401)
        self.assertEqual(self.try_login(outsider, alice).status_code, 429)  # the outsider's address waits ...
        self.assertEqual(ratelimit.guesses.waiting(alice.id) > 0, True)
        owner = self.anonymous(client=("198.51.100.7", 5000))  # ... the owner, anywhere else, signs in at once
        self.assertEqual(self.try_login(owner, alice).status_code, 200)
        # The owner's own browser (it has signed in before) is not held up even from the outsider's address.
        shared = self.anonymous(client=("203.0.113.9", 4001))
        self.assertEqual(self.try_login(shared, alice).status_code, 429)  # no cookie: same address, same wait
        shared.cookies.set("jh_device", owner.cookies.get("jh_device"))
        self.assertEqual(self.try_login(shared, alice).status_code, 200)

    def test_parallel_guesses_are_counted_before_the_check_so_only_five_get_through(self):
        alice = helpers.make_user()
        checked = []
        real = users.unlock

        def counting(db, user, password):
            checked.append(password)
            return real(db, user, password)

        results = []

        def guess(n):
            c = TestClient(app, client=("203.0.113.50", 1000 + n))  # (one network address: the same source)
            results.append(c.post("/api/auth/login", json={"email": alice.email, "password": f"wrong password {n}"}))

        ratelimit.AUTH.reset()
        with mock.patch.object(users, "unlock", counting), \
                mock.patch.object(ratelimit, "AUTH", ratelimit.Limiter([(1000, 60)])):
            starter = TestClient(app)  # (start the app once, outside the race)
            starter.__enter__()
            self.addCleanup(starter.__exit__, None, None, None)
            threads = [threading.Thread(target=guess, args=(n,)) for n in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        codes = sorted(r.status_code for r in results)
        self.assertEqual(codes, [401] * 5 + [429] * 5)
        self.assertEqual(len(checked), 5)  # the other five never got as far as the slow password check

    def test_guesses_from_many_addresses_meet_the_account_wide_ceiling_but_the_owners_browser_does_not(self):
        alice = helpers.make_user()
        owner = self.anonymous(client=("198.51.100.7", 5000))
        self.assertEqual(self.try_login(owner, alice).status_code, 200)  # signs in once: the browser is known
        with mock.patch.object(ratelimit, "ACCOUNT_CEILING", (6, 3600)):
            for n in range(6):  # a different address each time: no address ever reaches its own 5
                guesser = self.anonymous(client=(f"192.0.2.{n + 1}", 1000))
                self.assertEqual(self.try_login(guesser, alice, "guess-guess-guess").status_code, 401)
            new_device = self.anonymous(client=("198.51.100.77", 5000))
            slowed = self.try_login(new_device, alice)
            self.assertEqual((slowed.status_code, slowed.json()["code"]), (429, "locked"))
            self.assertIn("several places", slowed.json()["detail"])
            self.assertGreater(int(slowed.headers["retry-after"]), 0)
            # ... a browser the account has signed in with before is not held up, and signing in clears nothing for others
            self.assertEqual(self.try_login(owner, alice).status_code, 200)
            # an address with no account is counted the same way, so the answers can't tell the two apart
            for n in range(6):
                ghost = self.anonymous(client=(f"192.0.2.{n + 100}", 1000))
                self.assertEqual(self.try_login(ghost, types.SimpleNamespace(email="ghost@example.invalid",
                                                                           password="x" * 12)).status_code, 401)
            r = self.try_login(self.anonymous(client=("198.51.100.99", 1000)),
                               types.SimpleNamespace(email="ghost@example.invalid", password="x" * 12))
            self.assertEqual((r.status_code, r.json()["code"], r.json()["detail"]), (429, "locked", slowed.json()["detail"]))

    def test_the_device_cookie_is_signed_bound_to_the_password_and_to_the_account(self):
        alice, bob = helpers.make_user(), helpers.make_user()
        c = self.anonymous()
        r = self.try_login(c, alice)
        raw = r.headers.get_list("set-cookie")[1]
        self.assertTrue(raw.startswith("jh_device="))
        for flag in ("HttpOnly", "SameSite=strict", "Max-Age=31536000", "Path=/"):
            self.assertIn(flag, raw)
        cookie = c.cookies.get("jh_device")
        scope = {"headers": [(b"cookie", f"jh_device={cookie}".encode())]}
        db = SessionLocal()
        try:
            a, b = db.get(User, alice.id), db.get(User, bob.id)
            device = lockout.device_id(scope, a)
            self.assertTrue(device)
            self.assertIsNone(lockout.device_id(scope, b))  # another account's
            forged = {"headers": [(b"cookie", f"jh_device={cookie[:-3]}abc".encode())]}
            self.assertIsNone(lockout.device_id(forged, a))
            self.assertIsNone(lockout.device_id({"headers": []}, a))
            # signing in again keeps the same browser id; a new password makes every copy useless
            self.assertEqual(self.try_login(c, alice).status_code, 200)
            self.assertEqual(lockout.device_id({"headers": [(b"cookie", f"jh_device={c.cookies.get('jh_device')}".encode())]}, a), device)
            users.apply_new_password(db, a, helpers.key_of(alice.email)[1], helpers.ik_of(alice.email), users.new_password_keys("brand new password"))
            db.commit()
            db.refresh(a)
            self.assertIsNone(lockout.device_id(scope, a))
        finally:
            db.close()

    def test_a_known_browser_counts_its_own_wrong_tries(self):
        alice = helpers.make_user()
        c = self.anonymous(client=("203.0.113.9", 4000))
        self.assertEqual(self.try_login(c, alice).status_code, 200)
        for _ in range(5):
            self.assertEqual(self.try_login(c, alice, "guess-guess-guess").status_code, 401)
        self.assertEqual(self.try_login(c, alice).status_code, 429)  # this browser waits ...
        fresh = self.anonymous(client=("203.0.113.9", 4000))
        self.assertEqual(self.try_login(fresh, alice).status_code, 200)  # ... its address does not

    def test_a_locked_source_is_refused_before_the_slow_work_in_every_sign_in_step(self):
        """Recovery with a code: counted per source too, and open to the owner while an outsider guesses."""
        alice = helpers.make_user()
        outsider = self.anonymous(client=("203.0.113.9", 4000))
        for _ in range(5):
            ratelimit.AUTH.reset()
            r = outsider.post("/api/auth/recover", json={"email": alice.email, "recovery_code": "AAAA-BBBB-CCCC-DDDD",
                                                         "new_password": "some new password"})
            self.assertEqual(r.status_code, 401)
        ratelimit.AUTH.reset()
        r = outsider.post("/api/auth/recover", json={"email": alice.email, "recovery_code": "AAAA-BBBB-CCCC-DDDD",
                                                     "new_password": "some new password"})
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        code = helpers.pending_codes(alice)[0]
        owner = self.anonymous(client=("198.51.100.7", 5000))
        ratelimit.AUTH.reset()
        r = owner.post("/api/auth/recover", json={"email": alice.email, "recovery_code": code,
                                                  "new_password": "some new password"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_refusals_take_as_long_for_an_address_with_no_account_as_for_a_wrong_password(self):
        """A real account used to cost a database write and fsync more on a wrong password (the failure count); the
        counts are in memory now, and every refusal is also held to a floor: one password derivation plus a margin."""
        alice = helpers.make_user()
        c = self.anonymous()

        def timed(email, password):
            ratelimit.reset_all()
            began = time.monotonic()
            r = c.post("/api/auth/login", json={"email": email, "password": password})
            self.assertEqual(r.status_code, 401)
            return time.monotonic() - began

        floor = users.derivation_floor()
        self.assertGreaterEqual(floor, users.PAD_MARGIN_S)
        known = sorted(timed(alice.email, "not-the-password") for _ in range(9))
        unknown = sorted(timed("nobody-at-all@example.invalid", "not-the-password") for _ in range(9))
        self.assertGreaterEqual(known[0], floor - 0.005)
        self.assertGreaterEqual(unknown[0], floor - 0.005)
        self.assertLess(abs(known[4] - unknown[4]), 0.03, (known, unknown))  # the medians

    def test_every_refusal_is_held_to_the_floor(self):
        alice = helpers.make_user()
        c = self.anonymous()
        with mock.patch.object(users, "derivation_floor", return_value=0.3):
            def took(path, body):
                ratelimit.AUTH.reset()
                began = time.monotonic()
                r = c.post(path, json=body)
                return r, time.monotonic() - began

            recover = {"recovery_code": "AAAA-BBBB-CCCC-DDDD", "new_password": "some new password"}
            cases = [("/api/auth/login", {"email": "ghost@example.invalid", "password": "x" * 12}, 401),
                     ("/api/auth/login", {"email": alice.email, "password": "x" * 12}, 401),
                     ("/api/auth/recover", {"email": "ghost@example.invalid", **recover}, 401),
                     ("/api/auth/recover", {"email": alice.email, **recover}, 401)]
            for path, body, status in cases:
                ratelimit.reset_all()
                r, seconds = took(path, body)
                self.assertEqual(r.status_code, status, (path, body))
                self.assertGreaterEqual(seconds, 0.29, (path, body))
            helpers.lock_out(alice.id)  # a refusal for a wait is held to it too
            r, seconds = took("/api/auth/login", {"email": alice.email, "password": alice.password})
            self.assertEqual(r.status_code, 429)
            self.assertGreaterEqual(seconds, 0.29)
            ratelimit.reset_all()
            r, seconds = took("/api/auth/login", {"email": alice.email, "password": alice.password})
            self.assertEqual(r.status_code, 200)  # a right password is not delayed by it
            self.assertLess(seconds, 0.29)

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
        cookie = plain.headers.get_list("set-cookie")[0]
        self.assertTrue(cookie.startswith("jh_session="))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertNotIn("Max-Age", cookie)  # a browser session unless "Keep me signed in"
        https = self.anonymous(base_url="https://testserver")
        r = https.post("/api/auth/login", json={"email": alice.email, "password": alice.password, "remember": True})
        cookie = r.headers.get_list("set-cookie")[0]
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

    def test_the_port_is_part_of_the_cookie_name(self):
        """Browsers keep cookies per host name and ignore the port: a test instance on :8001 next to the live app on
        :8040 would sign each other out if both used one name."""
        alice = helpers.make_user()
        login = {"email": alice.email, "password": alice.password}
        for url, name in (("http://127.0.0.1:8001", "jh_session_8001"), ("http://127.0.0.1:8040", "jh_session_8040"),
                          ("http://[::1]:8001", "jh_session_8001"), ("https://testserver:8443", "__Host-jh_session_8443"),
                          ("http://127.0.0.1", "jh_session")):
            cookie = self.anonymous(base_url=url).post("/api/auth/login", json=login).headers["set-cookie"]
            self.assertTrue(cookie.startswith(name + "="), (url, cookie))
        # One browser, one host name, two ports: each instance has its own cookie and neither ends the other's.
        c = self.anonymous(base_url="http://127.0.0.1:8001")
        self.assertEqual(c.post("/api/auth/login", json=login).status_code, 200)
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        self.assertEqual(c.get("http://127.0.0.1:8040/api/auth/me").status_code, 401)  # (the other instance: signed out)
        self.assertEqual(c.post("http://127.0.0.1:8040/api/auth/login", json=login).status_code, 200)
        self.assertEqual(c.get("http://127.0.0.1:8040/api/auth/me").status_code, 200)
        self.assertEqual(c.get("http://127.0.0.1:8001/api/auth/me").status_code, 200)
        self.assertEqual({"jh_session_8001", "jh_session_8040"} <= set(c.cookies.keys()), True)
        # Signing out of one leaves the other.
        csrf = c.get("http://127.0.0.1:8001/api/auth/me").json()["csrf"]
        self.assertEqual(c.post("http://127.0.0.1:8001/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertEqual(c.get("http://127.0.0.1:8001/api/auth/me").status_code, 401)
        self.assertEqual(c.get("http://127.0.0.1:8040/api/auth/me").status_code, 200)

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


class WorkspaceIdTest(unittest.TestCase):
    """A workspace id is never given out twice (a request from before a wipe or a deletion must not write into the
    next account's workspace)."""

    def test_a_deleted_workspace_id_is_not_reused(self):
        helpers.started()
        first = helpers.make_user()
        with engine.begin() as conn:  # the highest id in the table, deleted: SQLite would hand it out again
            top = conn.execute(text("SELECT MAX(id) FROM workspaces")).scalar()
            conn.execute(text("DELETE FROM workspaces WHERE id = :i"), {"i": top})
        second = helpers.make_user()
        self.assertGreater(second.workspace_id, max(top, first.workspace_id))

    def test_a_database_made_without_autoincrement_is_rebuilt_once(self):
        from sqlalchemy import create_engine, event

        folder = helpers.scratch_folder()
        legacy = create_engine(f"sqlite:///{folder}/legacy.db")
        event.listen(legacy, "connect", lambda conn, _r: conn.execute("PRAGMA foreign_keys=ON"))
        with legacy.begin() as conn:
            for ddl in ("CREATE TABLE users (id INTEGER NOT NULL PRIMARY KEY)",
                        "CREATE TABLE workspaces (id INTEGER NOT NULL, name BLOB, created_at DATETIME, "
                        "owner_id INTEGER, PRIMARY KEY (id))",
                        "CREATE TABLE pins (id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL "
                        "REFERENCES workspaces (id) ON DELETE CASCADE)",
                        "INSERT INTO workspaces (id, name) VALUES (1, 'a'), (2, 'b')",
                        "INSERT INTO pins (id, workspace_id) VALUES (1, 2)"):
                conn.execute(text(ddl))
        with mock.patch.object(db_module, "engine", legacy):
            db_module._rebuild_workspaces_for_autoincrement()
            db_module._rebuild_workspaces_for_autoincrement()  # (a second start changes nothing)
        with legacy.begin() as conn:
            self.assertIn("AUTOINCREMENT", conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'workspaces'")).scalar())
            self.assertEqual([r[0] for r in conn.execute(text("SELECT id FROM workspaces ORDER BY id"))], [1, 2])
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM pins")).scalar(), 1)
            self.assertEqual(conn.execute(text("PRAGMA foreign_keys")).scalar(), 1)
            conn.execute(text("DELETE FROM workspaces WHERE id = 2"))  # the foreign key still cascades
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM pins")).scalar(), 0)
            conn.execute(text("INSERT INTO workspaces (name) VALUES ('c')"))
            self.assertEqual(conn.execute(text("SELECT MAX(id) FROM workspaces")).scalar(), 3)
        legacy.dispose()


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


class InstallSettingsTest(SignInTestCase):
    """The settings that serve the whole install: any signed-in account reads and changes them (there are no roles);
    tests/test_install_settings.py has the rest."""
    LLM_ENV = {"LLM_API_KEY": "sk-secret-key-1234", "LLM_BASE_URL": "https://llm.example.com/v1",
               "LLM_MODEL": "model-name-xyz"}

    def test_any_signed_in_account_reads_them_and_signed_out_nobody_does(self):
        other = self.signed_in(helpers.make_user())
        first = self.signed_in()
        anonymous = self.anonymous()
        with mock.patch.dict(os.environ, self.LLM_ENV):
            for c in (first, other):
                self.assertEqual(c.get("/api/settings/llm").json()["model"], "model-name-xyz")
                self.assertNotIn("sk-secret-key-1234", c.get("/api/settings/llm").text)
                self.assertEqual(c.get("/api/llm").status_code, 200)
            for path in ("/api/settings/llm", "/api/llm"):
                self.assertEqual(anonymous.get(path).status_code, 401, path)

    def test_score_status_shows_the_setup_but_never_the_key(self):
        other = self.signed_in(helpers.make_user())
        with mock.patch.dict(os.environ, self.LLM_ENV):
            out = other.get("/api/score/status")
            self.assertEqual(out.status_code, 200)
            self.assertEqual((out.json()["model"], out.json()["base_url"]), ("model-name-xyz", "https://llm.example.com/v1"))
            self.assertNotIn("sk-secret", out.text)
        with mock.patch.dict(os.environ, {"LLM_MODEL": "", "LLM_BASE_URL": "", "LLM_API_KEY": ""}):
            out = other.get("/api/score/status").json()
            self.assertFalse(out["configured"])
            self.assertIn("in Settings (AI provider)", out["problem"])


class RecoveryCodeTest(SignInTestCase):
    def test_codes_open_the_data_key_however_they_are_typed(self):
        account = helpers.extra_account(f"codes-{os.getpid()}@example.invalid", PASSWORD)
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
        account = helpers.extra_account(email, PASSWORD)
        c = helpers.client(email, PASSWORD)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        self.assertTrue(c.get("/api/auth/me").json()["recovery_codes_pending"])
        self.assertEqual(helpers.show_codes(c).json()["codes"], account.recovery_codes)
        self.assertEqual(c.post("/api/auth/recovery-codes/ack").status_code, 200)
        self.assertFalse(c.get("/api/auth/me").json()["recovery_codes_pending"])
        self.assertEqual(helpers.show_codes(c).status_code, 404)


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
        alice = helpers.make_user(f"Scoped Name {os.urandom(3).hex()}")
        self.assertEqual(self.signed_in(alice).get("/api/auth/me").json()["user"]["name"], alice.email)


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
            db.query(UserProfile).filter(UserProfile.id == ws_id).delete()  # (a reused id mustn't inherit its row)
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
