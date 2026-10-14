"""One account per install (open-source spec §1.2): once an account exists, nothing makes another, whatever the way
in; an install from before that keeps every account it has; the account signs in with a name, which it can change in
Settings with a recent password; the admin panel and sign-up are gone.

Most tests run on the shared test database, whose account the app made from the environment (tests/__init__.py); the
extra accounts helpers.make_user writes stand in for an older install's. The tests that need an empty install, or a
restart, run in a fresh process (helpers.scenario).
"""
from __future__ import annotations

import os
import threading
import unittest
from datetime import timedelta

from tests import helpers  # noqa: F401  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402

from backend import crypto, keys, ratelimit, users  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import AuthSession, SessionLocal, User, utcnow  # noqa: E402
from tests.helpers import remove_scratch_folders, scenario  # noqa: E402

PASSWORD = "user-password-123"

# Every route of the admin panel, sign-up and invitations before the open-source version.
REMOVED = (
    ("get", "/api/admin/stats"), ("get", "/api/admin/users"), ("get", "/api/admin/users/1"),
    ("post", "/api/admin/users/2/role"), ("post", "/api/admin/users/2/disable"), ("post", "/api/admin/users/2/enable"),
    ("delete", "/api/admin/users/2"), ("post", "/api/admin/users/2/regrant"), ("get", "/api/admin/audit"),
    ("get", "/api/admin/signup"), ("put", "/api/admin/signup"), ("get", "/api/admin/invites"),
    ("post", "/api/admin/invites"), ("delete", "/api/admin/invites/1"), ("post", "/api/admin/keys/rotate"),
    ("post", "/api/admin/admin-key/new"), ("get", "/api/admin/plaintext-audit"),
    ("post", "/api/admin/plaintext-files/delete"), ("get", "/api/auth/signup"), ("post", "/api/auth/signup"),
    ("get", "/api/auth/signup/state"), ("get", "/api/auth/invite"),
)


class Base(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)

    def anonymous(self) -> TestClient:
        c = TestClient(app)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def signed_in(self, account: helpers.Account | None = None) -> helpers.SignedInClient:
        c = helpers.client(account.email) if account else helpers.client()
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    @staticmethod
    def accounts() -> int:
        db = SessionLocal()
        try:
            return db.query(User).count()
        finally:
            db.close()

    @staticmethod
    def make_stale(c: TestClient) -> None:
        """This client's session last had its password entered 20 minutes ago."""
        sid = crypto.session_id(crypto.b64d(c.cookies.get("jh_session")))
        db = SessionLocal()
        try:
            db.get(AuthSession, sid).reauth_at = utcnow() - timedelta(minutes=20)
            db.commit()
        finally:
            db.close()


class NoSecondAccountTest(Base):
    def test_the_first_run_page_refuses_once_an_account_exists(self):
        before = self.accounts()
        body = {"token": "whatever", "name": "intruder", "password": "intruder-password"}
        for c in (self.anonymous(), self.signed_in()):
            r = c.post("/api/auth/first-run", json=body)
            self.assertEqual((r.status_code, r.json()["code"]), (409, "already_set_up"), r.text)
        self.assertEqual(self.anonymous().get("/api/auth/state").json()["setup_required"], False)
        self.assertEqual(self.accounts(), before)

    def test_the_account_code_itself_refuses(self):
        before = self.accounts()
        for via in ("first_run", "bootstrap", "open", "invite"):
            with self.assertRaises(users.AlreadySetUp):
                users.create_account(f"another-{via}", "another long password", created_via=via)
        self.assertEqual(self.accounts(), before)
        self.assertFalse(hasattr(users, "create_user"), "the old way of adding accounts is gone")

    def test_sign_up_and_invitations_are_gone(self):
        c = self.signed_in()
        for path in ("/api/auth/signup", "/api/admin/invites"):
            self.assertIn(c.post(path, json={"email": "x@example.invalid", "name": "x", "password": "x" * 12})
                          .status_code, (404, 405), path)
        for page in ("/signup.html", "/admin.html", "/signup.js", "/admin.js", "/dialogs.js"):
            self.assertEqual(self.anonymous().get(page).status_code, 404, page)
        self.assertEqual(self.anonymous().get("/welcome.html").status_code, 200, "the first account's walkthrough stays")

    def test_no_command_line_makes_an_account(self):
        import argparse

        commands = set()
        real = argparse._SubParsersAction.add_parser

        def spy(self_, name, **kw):
            commands.add(name)
            return real(self_, name, **kw)

        from unittest import mock
        with mock.patch.object(argparse._SubParsersAction, "add_parser", spy), \
                mock.patch("sys.stderr"), mock.patch("sys.stdout"), self.assertRaises(SystemExit):
            keys.main(["--help"])
        self.assertEqual(commands, {"bench-kdf", "rotate-kek", "recover-lost"})

    def test_the_start_up_path_adds_nobody_to_an_install_that_has_an_account(self):
        got = scenario('''
            from backend import users
            from backend.db import SessionLocal, User, init_db
            init_db()
            users.startup()
            users.create_account("Owner", "the owner's password")
            users._users_exist = False  # (as a new process sees it)
            import os
            os.environ["JOBHUNT_ADMIN_EMAIL"], os.environ["JOBHUNT_ADMIN_PASSWORD"] = "Somebody else", "another password"
            users.startup()  # a restart with the variables set
            db = SessionLocal()
            out["accounts"] = db.query(User).count()
            out["found"] = users.find_by_name(db, "Somebody else") is not None
            out["owner"] = users.find_by_name(db, "owner") is not None
            db.close()
        ''')
        self.assertEqual((got["accounts"], got["found"], got["owner"]), (1, False, True))
        remove_scratch_folders()

    def test_two_first_runs_at_once_make_one_account(self):
        got = scenario('''
            import threading
            from backend import users
            from backend.db import SessionLocal, User, init_db
            init_db()
            users.startup()
            results, ready = [], threading.Barrier(4)

            def make(n):
                ready.wait()
                try:
                    users.create_account(f"person {n}", "a long enough password")
                    results.append("made")
                except users.AlreadySetUp:
                    results.append("refused")

            threads = [threading.Thread(target=make, args=(n,)) for n in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            db = SessionLocal()
            out["accounts"] = db.query(User).count()
            db.close()
            out["results"] = sorted(results)
        ''')
        self.assertEqual(got["accounts"], 1)
        self.assertEqual(got["results"], ["made", "refused", "refused", "refused"])
        remove_scratch_folders()

    def test_the_first_run_token_makes_one_account_and_is_then_spent(self):
        got = scenario('''
            from pathlib import Path
            from fastapi.testclient import TestClient
            from backend.app import app
            with TestClient(app) as c:
                token = (Path(FOLDER) / "first-run.txt").read_text().strip()
                out["state"] = c.get("/api/auth/state").json()["setup_required"]
                out["no_token"] = c.post("/api/auth/first-run", json={"name": "A", "password": "a long password"}).status_code
                out["email_field"] = c.post("/api/auth/first-run", json={"token": token, "email": "a@example.invalid",
                                                                         "password": "a long password"}).status_code
                r = c.post("/api/auth/first-run", json={"token": token, "name": "Alex", "password": "a long password"})
                out["first"] = r.status_code
                c.cookies.clear()
                r = c.post("/api/auth/first-run", json={"token": token, "name": "Blake", "password": "b long password"})
                out["second"] = (r.status_code, r.json().get("code"))
                out["file"] = (Path(FOLDER) / "first-run.txt").exists()
                out["state_after"] = c.get("/api/auth/state").json()["setup_required"]
        ''')
        self.assertTrue(got["state"])
        self.assertEqual(got["no_token"], 403)
        self.assertEqual(got["email_field"], 422, "the first run asks for a name, not an email address")
        self.assertEqual(got["first"], 201)
        self.assertEqual(got["second"], [409, "already_set_up"])
        self.assertFalse(got["file"])
        self.assertFalse(got["state_after"])
        remove_scratch_folders()


class OlderInstallAccountsTest(Base):
    """An install from before the open-source version may hold several accounts: each keeps working."""

    def test_every_account_signs_in_and_keeps_its_own_workspaces(self):
        bob, carol = helpers.make_user(), helpers.make_user()
        b, c = self.signed_in(bob), self.signed_in(carol)
        self.assertEqual([w["id"] for w in b.get("/api/workspaces").json()], [bob.workspace_id])
        self.assertEqual([w["id"] for w in c.get("/api/workspaces").json()], [carol.workspace_id])
        r = b.get("/api/jobs", headers={"X-Workspace": str(carol.workspace_id)})
        self.assertEqual(r.status_code, 404, "another account's workspace stays out of reach")
        self.assertEqual(b.get("/api/auth/me").json()["user"]["name"], bob.email)

    def test_no_account_is_set_apart(self):
        bob = helpers.make_user()
        me = self.signed_in(bob).get("/api/auth/me").json()
        self.assertNotIn("role", me["user"])
        self.assertNotIn("admin_panel", me.get("features", {}))
        self.assertNotIn("role", {c.name for c in User.__table__.columns})


class RemovedAdminRoutesTest(Base):
    def test_every_admin_panel_route_is_gone_signed_in_or_not(self):
        bob = helpers.make_user()
        for c in (self.signed_in(), self.signed_in(bob), self.anonymous()):
            for method, path in REMOVED:
                r = getattr(c, method)(path, **({"json": {}} if method in ("post", "put") else {}))
                # Signed out, every /api route but a few answers 401 first; signed in, nothing is there.
                self.assertIn(r.status_code, (401, 404, 405), f"{method} {path}: {r.status_code}")
                self.assertNotEqual(r.status_code, 200, path)
        paths = {getattr(r, "path", "") for r in app.routes}
        self.assertFalse([p for p in paths if p.startswith("/api/admin") or "signup" in p or "invite" in p])


class NameTest(Base):
    def test_a_new_name_needs_a_recent_password_and_then_signs_in(self):
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        new = f"Bob Builder {os.getpid()}"
        self.make_stale(c)
        r = c.put("/api/auth/name", json={"name": new})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        self.assertEqual(c.get("/api/auth/me").json()["user"]["name"], bob.email, "nothing changed")
        self.assertEqual(c.post("/api/auth/reauth", json={"password": PASSWORD}).status_code, 200)
        r = c.put("/api/auth/name", json={"name": f"  {new}  "})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "name": new}))
        self.assertEqual(c.get("/api/auth/me").json()["user"]["name"], new)
        self.assertEqual(c.get("/api/auth/me").status_code, 200, "the session carries on")
        fresh = self.anonymous()
        old = fresh.post("/api/auth/login", json={"name": bob.email, "password": PASSWORD})
        self.assertEqual(old.status_code, 401, "the old name no longer signs in")
        ratelimit.reset_all()
        again = fresh.post("/api/auth/login", json={"name": new.upper(), "password": PASSWORD})
        self.assertEqual(again.status_code, 200, "capitals and spaces don't matter")
        db = SessionLocal()
        try:
            self.assertEqual(users.find_by_name(db, new).id, bob.id)
            self.assertIsNone(users.find_by_name(db, bob.email))
        finally:
            db.close()

    def test_signed_out_it_is_refused(self):
        r = self.anonymous().put("/api/auth/name", json={"name": "Somebody"})
        self.assertEqual(r.status_code, 401)

    def test_a_name_another_account_has_or_a_bad_one_is_refused(self):
        bob, carol = helpers.make_user(password=PASSWORD), helpers.make_user()
        c = self.signed_in(bob)
        r = c.put("/api/auth/name", json={"name": carol.email.upper()})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (409, "name_taken", "name"))
        for bad in ("", "   ", "a\nb", "x" * 255):
            r = c.put("/api/auth/name", json={"name": bad})
            self.assertEqual((r.status_code, r.json()["field"]), (422, "name"), repr(bad))
        self.assertEqual(c.put("/api/auth/name", json={"name": "ok", "role": "admin"}).status_code, 422)
        self.assertEqual(c.get("/api/auth/me").json()["user"]["name"], bob.email)

    def test_the_old_name_leaves_no_copy_in_the_database(self):
        bob = helpers.make_user(email=f"remember-me-{os.getpid()}@example.invalid", password=PASSWORD)
        c = self.signed_in(bob)
        db = SessionLocal()
        try:
            old_sealed = bytes(db.get(User, bob.id).pii_self)
            old_hash = bytes(db.get(User, bob.id).email_hash)
        finally:
            db.close()
        self.assertEqual(c.put("/api/auth/name", json={"name": f"Renamed {os.getpid()}"}).status_code, 200)
        from backend.db import DB_PATH
        for suffix in ("", "-wal"):
            path = DB_PATH.with_name(DB_PATH.name + suffix)
            if path.exists():
                data = path.read_bytes()
                self.assertNotIn(old_sealed, data, path.name)
                self.assertNotIn(old_hash, data, path.name)

    def test_two_renames_at_once_cannot_both_take_a_name(self):
        bob, carol = helpers.make_user(), helpers.make_user()
        wanted = f"Only one {os.getpid()}"
        results, ready = [], threading.Barrier(2)

        def rename(account):
            db = SessionLocal()
            try:
                with helpers.as_account(account):
                    ready.wait()
                    try:
                        users.change_name(db, account.id, wanted)
                        results.append("renamed")
                    except users.NameTaken:
                        results.append("taken")
            finally:
                db.close()

        threads = [threading.Thread(target=rename, args=(a,)) for a in (bob, carol)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(sorted(results), ["renamed", "taken"])


class FirstRunLogTest(unittest.TestCase):
    def test_the_first_run_log_line_names_no_folder(self):
        """The data folder's path can hold the person's name (a home folder): the first-run line names the file only."""
        import tempfile
        from pathlib import Path
        from unittest import mock

        folder = Path(tempfile.mkdtemp(prefix="jobhunt-log-")) / "Jane Citizen" / "JobHuntPA"
        folder.mkdir(parents=True)
        self.addCleanup(__import__("shutil").rmtree, folder.parent.parent, True)
        before = users._first_run_token
        self.addCleanup(setattr, users, "_first_run_token", before)
        with mock.patch.object(users, "first_run_path", return_value=folder / users.FIRST_RUN_FILE), \
                mock.patch.object(users, "_launcher_token", return_value=None), \
                self.assertLogs("backend.users", "WARNING") as logs:
            users._write_first_run_token()
        text = "\n".join(logs.output)
        self.assertIn("first-run.html#token=", text)
        self.assertIn(users.FIRST_RUN_FILE, text)
        self.assertNotIn("Jane Citizen", text)
        self.assertNotIn(str(folder.parent.parent), text)


if __name__ == "__main__":
    unittest.main()
