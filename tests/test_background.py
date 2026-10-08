"""Background access (accounts design 1.2-1.3, 6.7, 13 item 1; Phase G): the switch and its API, the scheduler
running searches under an owner's key (or passing the slot over with a reason), the "keep data" password reset, and
``python -m backend.keys recover-lost``.

Run:  python3 -m unittest tests.test_background -v
"""
from __future__ import annotations

import io
import os
import json
import re
import shutil
import stat
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402

from backend import crypto, schedule, tasks, users  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import AuthSession, SearchSchedule, SessionLocal, User, UserKeys, engine, utcnow  # noqa: E402
from backend.profile import get_profile  # noqa: E402
from sqlalchemy import text  # noqa: E402

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
FRIDAY_1020 = datetime(2026, 10, 2, 0, 20, tzinfo=timezone.utc)  # Fri 10:20 Sydney: the 10:00 slot is due
FRIDAY_1220 = FRIDAY_1020 + timedelta(hours=2)
FRIDAY_1420 = FRIDAY_1020 + timedelta(hours=4)
SLOT_1000 = "2026-10-02T00:00:00+00:00"


def tearDownModule():
    helpers.remove_scratch_folders()


def signed_in(account):
    c = helpers.client(account.email)
    c.__enter__()
    return c


class Base(unittest.TestCase):
    def new_user(self, background: bool = False, titles=("Product Manager",)):
        """An account with a search to run (a title with SEEK on) and a signed-in client."""
        acct = helpers.make_user()
        c = signed_in(acct)
        self.addCleanup(c.__exit__, None, None, None)
        profile = c.get("/api/profile").json()
        self.assertEqual(c.put("/api/profile", json={**profile, "titles": list(titles), "seek_enabled": True})
                         .status_code, 200)
        if background:
            self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)
        return acct, c

    def row(self, user_id: int) -> UserKeys:
        db = SessionLocal()
        try:
            row = db.get(UserKeys, user_id)
            db.expunge(row)
            return row
        finally:
            db.close()

    def age_sign_in(self, user_id: int, minutes: int) -> None:
        db = SessionLocal()
        try:
            db.query(AuthSession).filter(AuthSession.user_id == user_id).update(
                {AuthSession.reauth_at: utcnow() - timedelta(minutes=minutes)})
            db.commit()
        finally:
            db.close()


class SwitchApiTest(Base):
    def test_a_new_account_has_it_off_and_the_first_admin_has_it_on(self):
        acct, c = self.new_user()
        self.assertEqual(c.get("/api/account/background").json(), {"enabled": False, "since": None})
        with helpers.client() as admin:
            self.assertTrue(admin.get("/api/account/background").json()["enabled"])

    def test_turning_it_on_wraps_the_data_key_under_the_server_key(self):
        acct, c = self.new_user()
        r = c.post("/api/account/background", json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["enabled"])
        self.assertRegex(body["since"], r"^\d{4}-\d\d-\d\dT[\d:.]+Z$")
        self.assertEqual(c.get("/api/account/background").json(), body)
        row = self.row(acct.id)
        self.assertEqual(row.server_kek_id, users.server_keys().current_kek_id)
        self.assertIsNotNone(row.background_since)
        self.assertNotIn(helpers.key_of(acct.email)[1], bytes(row.server_wrapped_dek))  # a wrap, not the key itself
        db = SessionLocal()
        try:
            self.assertEqual(users.background_key(db, acct.id), helpers.key_of(acct.email)[1])
        finally:
            db.close()

    def test_turning_it_on_needs_a_recent_password(self):
        acct, c = self.new_user()
        self.age_sign_in(acct.id, 20)
        r = c.post("/api/account/background", json={"enabled": True})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        self.assertFalse(c.get("/api/account/background").json()["enabled"])
        self.assertIsNone(self.row(acct.id).server_wrapped_dek)
        self.age_sign_in(acct.id, 5)
        self.assertTrue(c.post("/api/account/background", json={"enabled": True}).json()["enabled"])

    def test_turning_it_off_deletes_the_servers_copy_and_needs_no_new_password(self):
        acct, c = self.new_user(background=True)
        blob = bytes(self.row(acct.id).server_wrapped_dek)
        self.age_sign_in(acct.id, 120)  # a long time since the password was typed
        r = c.post("/api/account/background", json={"enabled": False})
        self.assertEqual(r.json(), {"enabled": False, "since": None})
        row = self.row(acct.id)
        self.assertEqual((row.server_wrapped_dek, row.server_kek_id, row.background_since), (None, None, None))
        db = SessionLocal()
        try:
            self.assertIsNone(users.background_key(db, acct.id))
        finally:
            db.close()
        # no checkpoint here: turning it off must have emptied the write-ahead log itself
        for suffix in ("", "-wal"):
            path = Path(str(engine.url.database) + suffix)
            self.assertNotIn(blob, path.read_bytes() if path.exists() else b"", f"the old copy is gone from {path.name}")
        self.assertEqual(c.post("/api/account/background", json={"enabled": False}).status_code, 200)  # idempotent

    def test_only_this_accounts_own_switch_can_be_changed(self):
        acct, c = self.new_user()
        other, _ = self.new_user()
        for body in ({"enabled": True, "user_id": other.id}, {}, {"enabled": "maybe"}, {"enabled": True, "x": 1}):
            with self.subTest(body=body):
                self.assertEqual(c.post("/api/account/background", json=body).status_code, 422)
        self.assertIsNone(self.row(other.id).server_wrapped_dek)
        anonymous = TestClient(app)
        self.assertEqual(anonymous.get("/api/account/background").status_code, 401)
        self.assertEqual(anonymous.post("/api/account/background", json={"enabled": False}).status_code, 401)
        # a signed-in request without the CSRF token is refused
        no_csrf = helpers.client(acct.email)
        no_csrf.__enter__()
        self.addCleanup(no_csrf.__exit__, None, None, None)
        del no_csrf.headers["X-CSRF-Token"]
        self.assertEqual(no_csrf.post("/api/account/background", json={"enabled": True}).status_code, 403)
        self.assertIsNone(self.row(acct.id).server_wrapped_dek)

    def test_a_key_that_isnt_the_accounts_is_refused(self):
        acct, _ = self.new_user()
        other = helpers.make_user()
        db = SessionLocal()
        try:
            with self.assertRaises(ValueError):
                users.enable_background(db, acct.id, helpers.key_of(other.email)[1])
            with self.assertRaises(ValueError):
                users.enable_background(db, acct.id, os.urandom(32))
        finally:
            db.close()
        self.assertIsNone(self.row(acct.id).server_wrapped_dek)

    def test_a_wrap_copied_from_another_account_opens_nothing(self):
        alice, _ = self.new_user(background=True)
        bob, _ = self.new_user()
        db = SessionLocal()
        try:
            a = db.get(UserKeys, alice.id)
            b = db.get(UserKeys, bob.id)
            b.server_wrapped_dek, b.server_kek_id = a.server_wrapped_dek, a.server_kek_id
            db.commit()
            self.assertIsNone(users.background_key(db, bob.id))  # the label names alice
            self.assertEqual(users.background_block(db, bob.id), "key_unusable")
        finally:
            db.close()

    def test_background_access_is_on_the_features_list_the_wizard_reads(self):
        acct, c = self.new_user()
        self.assertTrue(c.get("/api/account/onboarding").json()["features"]["scheduled_searches"])
        self.assertTrue(c.get("/api/auth/me").json()["features"]["scheduled_searches"])


class SchedulerTest(Base):
    """Each workspace's slot runs under its owner's key, unwrapped with the server key, or is passed over with a
    reason the person sees. Other workspaces in the shared test database are told they have nothing to search."""

    def tick(self, ws_id: int, now: datetime, start):
        real = schedule.nothing_to_search
        with mock.patch.object(schedule, "nothing_to_search",
                               side_effect=lambda d, w: real(d, w) if w == ws_id else True), \
                mock.patch.object(schedule.tasks, "active_run", return_value=None):
            return schedule.tick(start, now)

    def starter(self, started: list):
        def start(ws):
            started.append((ws, crypto.current().user_id))
            return {"id": 4000 + len(started)}
        return start

    def test_a_locked_users_slot_is_passed_over_with_a_reason_they_can_see(self):
        acct, c = self.new_user(background=False)
        started: list = []
        self.assertIsNone(self.tick(acct.workspace_id, FRIDAY_1020, self.starter(started)))
        self.assertEqual(started, [])
        info = c.get("/api/schedule").json()
        self.assertFalse(info["background"])
        self.assertEqual(info["skipped"], {"reason": "background_off", "slot": SLOT_1000,
                                           "text": "Scheduled search skipped: background access is off"})
        db = SessionLocal()
        try:
            row = db.get(SearchSchedule, acct.workspace_id)
            self.assertEqual(row.last_slot, datetime(2026, 10, 2, 0, 0))  # handled: it isn't tried again
        finally:
            db.close()
        # nothing is attempted for the same slot a second time
        self.assertIsNone(self.tick(acct.workspace_id, FRIDAY_1020 + timedelta(minutes=5), self.starter(started)))
        self.assertEqual(started, [])

    def test_turning_it_on_clears_the_reason_and_the_next_slot_runs(self):
        acct, c = self.new_user()
        started: list = []
        self.tick(acct.workspace_id, FRIDAY_1020, self.starter(started))
        self.assertIsNotNone(c.get("/api/schedule").json()["skipped"])
        c.post("/api/account/background", json={"enabled": True})
        info = c.get("/api/schedule").json()
        self.assertEqual((info["background"], info["skipped"]), (True, None))
        run = self.tick(acct.workspace_id, FRIDAY_1220, self.starter(started))
        self.assertEqual(run["id"], 4001)
        self.assertEqual(started, [(acct.workspace_id, acct.id)])  # under this owner's key
        self.assertIsNone(c.get("/api/schedule").json()["skipped"])

    def test_an_opted_in_users_scheduled_run_reads_their_sealed_data(self):
        acct, c = self.new_user(background=True, titles=("Sealed Title For The Scheduler",))
        ws = acct.workspace_id

        def start(workspace):
            def job(progress):
                db = SessionLocal()
                try:
                    return {"user": crypto.current().user_id, "titles": get_profile(db, workspace)["titles"]}
                finally:
                    db.close()
            return tasks.start("search", job, workspace, scheduled=True)

        run = self.tick(ws, FRIDAY_1020, start)
        self.assertIsNotNone(run)
        self.assertTrue(run["scheduled"])
        done = None
        for _ in range(400):  # the single worker may be finishing another test's run first
            done = c.get(f"/api/runs/{run['id']}").json()
            if done["state"] not in ("queued", "running"):
                break
            import time
            time.sleep(0.05)
        self.assertEqual(done["state"], "done", done)
        self.assertEqual(done["summary"], {"user": acct.id, "titles": ["Sealed Title For The Scheduler"]})
        self.assertFalse(crypto.has_scope())  # nothing leaked into this thread

    def test_turning_it_off_stops_the_runs(self):
        acct, c = self.new_user(background=True)
        started: list = []
        self.assertEqual(self.tick(acct.workspace_id, FRIDAY_1020, self.starter(started))["id"], 4001)
        c.post("/api/account/background", json={"enabled": False})
        self.assertIsNone(self.tick(acct.workspace_id, FRIDAY_1220, self.starter(started)))
        self.assertEqual(len(started), 1)
        self.assertEqual(c.get("/api/schedule").json()["skipped"]["reason"], "background_off")
        self.assertIsNone(self.row(acct.id).server_wrapped_dek)

    def test_a_turned_off_account_is_passed_over_too(self):
        acct, c = self.new_user(background=True)
        db = SessionLocal()
        try:
            db.query(User).filter(User.id == acct.id).update({User.status: "disabled"})
            db.commit()
            started: list = []
            self.assertIsNone(self.tick(acct.workspace_id, FRIDAY_1020, self.starter(started)))
            self.assertEqual(started, [])
            row = db.get(SearchSchedule, acct.workspace_id)
            self.assertEqual(row.skipped_reason, "account_off")
            self.assertEqual(schedule.skipped_info(db, acct.workspace_id)["text"],
                             "Scheduled search skipped: this account is turned off")
        finally:
            db.close()

    def test_a_key_the_server_cant_open_says_so(self):
        acct, c = self.new_user(background=True)
        db = SessionLocal()
        try:
            db.query(UserKeys).filter(UserKeys.user_id == acct.id).update({UserKeys.server_wrapped_dek: os.urandom(80)})
            db.commit()
        finally:
            db.close()
        started: list = []
        self.assertIsNone(self.tick(acct.workspace_id, FRIDAY_1020, self.starter(started)))
        self.assertEqual(started, [])
        self.assertEqual(c.get("/api/schedule").json()["skipped"]["reason"], "key_unusable")
        c.post("/api/account/background", json={"enabled": False})  # off and on again fixes it
        c.post("/api/account/background", json={"enabled": True})
        self.assertEqual(self.tick(acct.workspace_id, FRIDAY_1220, self.starter(started))["id"], 4001)

    def test_a_skip_for_another_reason_is_not_blamed_on_background_access(self):
        """No titles and no sources: "nothing to search" is shown by its own line, not as a missing key."""
        acct, c = self.new_user(background=True)
        c.put("/api/profile", json={**c.get("/api/profile").json(), "titles": [], "seek_enabled": False})
        started: list = []
        self.assertIsNone(self.tick(acct.workspace_id, FRIDAY_1020, self.starter(started)))
        info = c.get("/api/schedule").json()
        self.assertEqual((info["skipped"], info["nothing_to_search"], started), (None, True, []))

    def test_workspaces_are_never_run_with_another_owners_key(self):
        alice, ca = self.new_user(background=True)
        bob, cb = self.new_user(background=True)
        started: list = []

        def start(ws):
            started.append((ws, crypto.current().user_id))
            return {"id": 5000 + len(started)}

        real = schedule.nothing_to_search
        with mock.patch.object(schedule, "nothing_to_search",
                               side_effect=lambda d, w: real(d, w) if w in (alice.workspace_id, bob.workspace_id) else True), \
                mock.patch.object(schedule.tasks, "active_run", return_value=None):
            schedule.tick(start, FRIDAY_1020)
            schedule.tick(start, FRIDAY_1020)
        self.assertEqual(sorted(started), sorted([(alice.workspace_id, alice.id), (bob.workspace_id, bob.id)]))


class KeepDataResetTest(Base):
    """``users.reset_password_keeping_data``: the hook Phase H calls for a password reset by email."""

    NEW = "a brand new password 99"

    def sign_in_fails(self, acct, password) -> bool:
        from backend import ratelimit
        ratelimit.reset_all()
        r = TestClient(app).post("/api/auth/login", json={"email": acct.email, "password": password})
        return r.status_code == 401

    def test_data_stays_readable_with_the_new_password(self):
        acct, c = self.new_user(background=True, titles=("Kept Through The Reset",))
        old_dek = helpers.key_of(acct.email)[1]
        before = self.snapshot(acct.id)
        db = SessionLocal()
        try:
            self.assertTrue(users.reset_password_keeping_data(db, acct.id, self.NEW))
        finally:
            db.close()
        after = self.snapshot(acct.id)
        self.assertNotEqual(after["salt"], before["salt"])
        self.assertNotEqual(after["verifier"], before["verifier"])
        self.assertNotEqual(after["wrap"], before["wrap"])
        self.assertEqual(after["server_wrap"], before["server_wrap"])  # background access carries on
        # every session ended, the old password is gone
        self.assertEqual(c.get("/api/auth/me").status_code, 401)
        self.assertTrue(self.sign_in_fails(acct, acct.password))
        helpers._passwords[acct.email] = self.NEW
        with helpers.client(acct.email, self.NEW) as fresh:
            self.assertEqual(fresh.get("/api/profile").json()["titles"], ["Kept Through The Reset"])
        db = SessionLocal()
        try:
            self.assertEqual(users.unlock_with_password(db, db.get(User, acct.id), self.NEW), old_dek)
            self.assertEqual(db.query(AuthSession).filter(AuthSession.user_id == acct.id).count(), 1)  # only the new one
        finally:
            db.close()

    def test_a_locked_out_account_can_sign_in_with_the_new_password(self):
        acct, _ = self.new_user(background=True)
        from backend import ratelimit
        helpers.lock_out(acct.id, failures=9)
        self.assertGreater(ratelimit.guesses.waiting(acct.id), 0)
        db = SessionLocal()
        try:
            self.assertTrue(users.reset_password_keeping_data(db, acct.id, self.NEW))
        finally:
            db.close()
        self.assertEqual((ratelimit.guesses.waiting(acct.id), helpers.wrong_tries(acct.id)), (0, 0))

    def test_recovery_codes_are_replaced_and_the_old_ones_open_nothing(self):
        """Each old code opens the data key, so it must stop working: whoever held the account before the person who
        proved the mailbox may have kept them. The new ones wrap the same key."""
        from backend.db import RecoveryCode
        email = f"keepdata-codes-{os.getpid()}@example.invalid"
        account = users.create_user(email, helpers.USER_PASSWORD, background=True)
        db = SessionLocal()
        try:
            new_codes = users.reset_password_keeping_data(db, account.user_id, self.NEW)
            self.assertEqual(len(new_codes), 8)
            self.assertFalse(set(new_codes) & set(account.recovery_codes))
            for code in account.recovery_codes:
                self.assertIsNone(users.open_recovery_code(db, account.user_id, code))
            self.assertEqual(db.query(RecoveryCode).filter(RecoveryCode.user_id == account.user_id).count(), 8)
            for code in new_codes:
                self.assertEqual(users.open_recovery_code(db, account.user_id, code)[1], account.dek)
            with crypto.as_user(account.user_id, account.dek):
                self.assertEqual(users.pending_recovery_codes(db, account.user_id), new_codes)
        finally:
            db.close()

    def test_without_background_access_it_returns_false_and_changes_nothing(self):
        acct, c = self.new_user(background=False, titles=("Still Locked",))
        before = self.snapshot(acct.id)
        db = SessionLocal()
        try:
            self.assertFalse(users.reset_password_keeping_data(db, acct.id, self.NEW))
        finally:
            db.close()
        self.assertEqual(self.snapshot(acct.id), before)
        self.assertEqual(c.get("/api/auth/me").status_code, 200)  # still signed in
        self.assertFalse(self.sign_in_fails(acct, acct.password))  # the old password still works
        self.assertTrue(self.sign_in_fails(acct, self.NEW))

    def test_after_turning_it_off_it_returns_false(self):
        acct, c = self.new_user(background=True)
        c.post("/api/account/background", json={"enabled": False})
        db = SessionLocal()
        try:
            self.assertFalse(users.reset_password_keeping_data(db, acct.id, self.NEW))
        finally:
            db.close()

    def test_a_bad_password_is_refused_before_anything_changes(self):
        acct, c = self.new_user(background=True)
        before = self.snapshot(acct.id)
        db = SessionLocal()
        try:
            for bad in ("short", "", None, "x" * 2000):
                with self.subTest(password=bad), self.assertRaises(users.AccountError):
                    users.reset_password_keeping_data(db, acct.id, bad)
        finally:
            db.close()
        self.assertEqual(self.snapshot(acct.id), before)
        self.assertEqual(c.get("/api/auth/me").status_code, 200)

    def test_a_key_the_server_cant_open_returns_false_and_changes_nothing(self):
        acct, c = self.new_user(background=True)
        db = SessionLocal()
        try:
            db.query(UserKeys).filter(UserKeys.user_id == acct.id).update({UserKeys.server_wrapped_dek: os.urandom(80)})
            db.commit()
            before = self.snapshot(acct.id)
            self.assertFalse(users.reset_password_keeping_data(db, acct.id, self.NEW))
        finally:
            db.close()
        self.assertEqual(self.snapshot(acct.id), before)

    def test_an_unknown_account_returns_false(self):
        db = SessionLocal()
        try:
            self.assertFalse(users.reset_password_keeping_data(db, 10 ** 9, self.NEW))
        finally:
            db.close()

    def test_other_accounts_are_untouched(self):
        acct, _ = self.new_user(background=True)
        other, oc = self.new_user(background=True)
        before = self.snapshot(other.id)
        db = SessionLocal()
        try:
            users.reset_password_keeping_data(db, acct.id, self.NEW)
        finally:
            db.close()
        self.assertEqual(self.snapshot(other.id), before)
        self.assertEqual(oc.get("/api/auth/me").status_code, 200)

    def snapshot(self, user_id: int) -> dict:
        db = SessionLocal()
        try:
            user, row = db.get(User, user_id), db.get(UserKeys, user_id)
            return {"salt": bytes(user.kdf_salt), "verifier": bytes(user.pw_verifier), "params": user.kdf_params,
                    "wrap": bytes(row.pw_wrapped_dek), "server_wrap": row.server_wrapped_dek,
                    "sessions": db.query(AuthSession).filter(AuthSession.user_id == user_id).count()}
        finally:
            db.close()


RECOVER = '''
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from fastapi.testclient import TestClient
from backend import crypto, keys, users
from backend.app import app
from backend.db import DB_PATH, AppSetting, SessionLocal, User, UserKeys, init_db

ADMIN_PW = "root-password-123"
init_db()
users.startup()                                   # the admin, from the environment
alice = users.create_user("alice@example.invalid", "alice-password-1", name="Alice", background=True)
bob = users.create_user("bob@example.invalid", "bob-password-12", name="Bob")
carol = users.create_user("carol@example.invalid", "carol-password-1", name="Carol")
db = SessionLocal()
db.query(User).filter(User.id == alice.user_id).update({User.mfa_enabled: True, User.totp_secret_enc: b"sealed-secret",
                                                       User.totp_last_step: 5})
db.query(User).filter(User.id == carol.user_id).update({User.pii_admin: None})   # her details can't be worked out
db.commit()
old_hash = {u.id: bytes(u.email_hash) for u in db.query(User)}
old_fp = db.get(AppSetting, "server_keys_fingerprint").value_json
old_blob = bytes(db.get(UserKeys, alice.user_id).server_wrapped_dek)
db.close()
key_path = Path(FOLDER) / "server-keys.json"
old_file = key_path.read_bytes()


def recover(email, password):
    out_, err_ = io.StringIO(), io.StringIO()
    with redirect_stdout(out_), redirect_stderr(err_):
        status = keys.main(["recover-lost", "--admin-email", email], ask_password=lambda prompt: password)
    return {"status": status, "out": out_.getvalue(), "err": err_.getvalue()}


def state():
    db = SessionLocal()
    rows = {u.id: (bytes(u.email_hash), u.mfa_enabled, u.totp_secret_enc) for u in db.query(User)}
    bg = [r.user_id for r in db.query(UserKeys).filter(UserKeys.server_wrapped_dek.isnot(None))]
    fp = db.get(AppSetting, "server_keys_fingerprint").value_json
    db.close()
    return rows, bg, fp


before = state()
out["with_file"] = recover("root@example.invalid", ADMIN_PW)            # a key file exists: refused
out["file_kept"] = key_path.read_bytes() == old_file
key_path.unlink()
run_file = Path(FOLDER) / "running.json"                                # the app is running (a live process)
run_file.write_text(json.dumps({"port": 8000, "pid": os.getppid()}))
out["while_running"] = recover("root@example.invalid", ADMIN_PW)
out["still_no_file"] = not key_path.exists() and not (key_path.parent / "server-keys.json.new").exists()
run_file.unlink()
db = SessionLocal()
db.query(User).filter(User.id == 1).update({User.status: "disabled"})   # a turned-off admin, right password
db.commit()
db.close()
out["disabled_admin"] = recover("root@example.invalid", ADMIN_PW)
out["after_disabled"] = state() == before and not key_path.exists()
db = SessionLocal()
db.query(User).filter(User.id == 1).update({User.status: "active"})
db.commit()
db.close()
out["wrong_password"] = recover("root@example.invalid", "not the password")
out["wrong_email"] = recover("nobody@example.invalid", ADMIN_PW)
out["empty_password"] = recover("root@example.invalid", "")
non_admin = recover("alice@example.invalid", "alice-password-1")      # right password, but not an admin
out["not_admin"] = non_admin
out["nothing_changed"] = state() == before and not key_path.exists() and not (key_path.parent / "server-keys.json.new").exists()

out["recover"] = recover("Root@Example.invalid", ADMIN_PW)              # the email is matched whatever its case
out["file_exists"] = key_path.exists()
db_files = [Path(str(DB_PATH) + suffix) for suffix in ("", "-wal")]
out["old_copies_left"] = [f.name for f in db_files if f.exists() and (old_blob in f.read_bytes() or b"sealed-secret" in f.read_bytes())]
out["file_mode"] = oct(key_path.stat().st_mode & 0o777)
out["new_file_differs"] = key_path.read_bytes() != old_file
out["leftover"] = sorted(p.name for p in key_path.parent.iterdir() if p.name.startswith("server-keys") and p.name != "server-keys.json")

rows, bg, fp = state()
out["background_left"] = bg
out["mfa_left"] = [uid for uid, (_, mfa, secret) in rows.items() if mfa or secret]
out["fingerprint_changed"] = fp != old_fp

users.startup()                                                           # a restart: the new file matches the database
db = SessionLocal()
found = {name: users.find_by_email(db, f"{name}@example.invalid") for name in ("root", "alice", "bob", "carol")}
out["found"] = {name: (u.id if u else None) for name, u in found.items()}
out["ids"] = {"root": 1, "alice": alice.user_id, "bob": bob.user_id, "carol": carol.user_id}
out["hash_changed"] = {name: (bytes(u.email_hash) != old_hash[u.id]) for name, u in found.items() if u}
out["carol_hash_kept"] = bytes(db.get(User, carol.user_id).email_hash) == old_hash[carol.user_id]
out["alice_background_key"] = users.background_key(db, alice.user_id) is None
out["alice_dek_same"] = users.unlock_with_password(db, found["alice"], "alice-password-1") == alice.dek
out["root_signs_in"] = users.unlock_with_password(db, found["root"], ADMIN_PW) is not None
with crypto.as_user(alice.user_id, alice.dek):
    out["alice_details"] = users.details(db, alice.user_id)
db.close()

with TestClient(app) as c:
    r = c.post("/api/auth/login", json={"email": "alice@example.invalid", "password": "alice-password-1"})
    out["alice_login"] = r.status_code
    out["alice_background_state"] = c.get("/api/account/background").json()
    # and background access can be turned on again with the new key file
    out["turn_on_again"] = c.post("/api/account/background", json={"enabled": True}, headers={"X-CSRF-Token": r.json()["csrf"]}).json()["enabled"]
'''


class RecoverLostKeyFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = helpers.scenario(RECOVER, env={"JOBHUNT_ADMIN_EMAIL": "root@example.invalid",
                                                 "JOBHUNT_ADMIN_PASSWORD": "root-password-123"})

    def test_it_refuses_without_changing_anything(self):
        out = self.out
        self.assertEqual(out["with_file"]["status"], 1)
        self.assertIn("A key file already exists", out["with_file"]["err"])
        self.assertTrue(out["file_kept"])
        for case in ("wrong_password", "wrong_email", "not_admin"):
            with self.subTest(case=case):
                self.assertEqual(out[case]["status"], 1)
                self.assertIn("No admin account matches that email address and password", out[case]["err"])
        self.assertEqual(out["empty_password"]["status"], 1)
        self.assertIn("No password was entered", out["empty_password"]["err"])
        self.assertTrue(out["nothing_changed"], "no key file, no half-written file, no row changed")

    def test_it_refuses_while_the_app_is_running(self):
        out = self.out
        self.assertEqual(out["while_running"]["status"], 1)
        self.assertIn("JobHuntPA is running", out["while_running"]["err"])
        self.assertTrue(out["still_no_file"])

    def test_it_refuses_a_turned_off_admin_without_saying_why(self):
        out = self.out
        self.assertEqual(out["disabled_admin"]["status"], 1)
        self.assertIn("No admin account matches that email address and password", out["disabled_admin"]["err"])
        self.assertTrue(out["after_disabled"], "no key file and nothing changed")

    def test_it_writes_a_new_private_key_file_and_nothing_is_left_over(self):
        out = self.out
        self.assertEqual(out["recover"]["status"], 0, out["recover"])
        self.assertTrue(out["file_exists"])
        self.assertTrue(out["new_file_differs"])
        if os.name == "posix":
            self.assertEqual(out["file_mode"], oct(stat.S_IRUSR | stat.S_IWUSR))
        self.assertEqual(out["leftover"], [])
        self.assertTrue(out["fingerprint_changed"])

    def test_every_email_is_found_again_with_the_new_pepper(self):
        out = self.out
        for name in ("root", "alice", "bob"):
            self.assertEqual(out["found"][name], out["ids"][name], name)
            self.assertTrue(out["hash_changed"][name], f"{name}'s email hash was recomputed")
        # carol's details couldn't be opened: her old hash stays, no email address finds her any more
        self.assertIsNone(out["found"]["carol"])
        self.assertTrue(out["carol_hash_kept"])
        self.assertIn("1 could not be worked out", out["recover"]["out"])
        self.assertIn("Email lookup was rebuilt for 3 account(s)", out["recover"]["out"])

    def test_background_access_and_two_step_sign_in_are_switched_off(self):
        out = self.out
        self.assertEqual(out["background_left"], [])
        self.assertEqual(out["old_copies_left"], [], "the deleted key copies and TOTP secret are in neither the database nor its log")
        self.assertEqual(out["mfa_left"], [])
        self.assertTrue(out["alice_background_key"])
        self.assertIn("Background access was switched off for 2 account(s) and two-step sign-in for 1", out["recover"]["out"])

    def test_nobodys_data_is_lost_and_nobody_is_named(self):
        out = self.out
        self.assertTrue(out["alice_dek_same"])
        self.assertTrue(out["root_signs_in"])
        self.assertEqual(out["alice_details"], {"email": "alice@example.invalid", "name": "Alice"})
        self.assertEqual(out["alice_login"], 200)
        self.assertEqual(out["alice_background_state"], {"enabled": False, "since": None})
        self.assertTrue(out["turn_on_again"])
        for text_ in (out["recover"]["out"], out["recover"]["err"], out["wrong_password"]["err"]):
            self.assertNotRegex(text_, r"example\.invalid|alice|bob|carol|password-")


class RecoverLostCommandLineTest(unittest.TestCase):
    def test_the_command_is_wired_into_the_cli_and_asks_for_a_password(self):
        from backend import keys
        prompts: list[str] = []
        err = io.StringIO()
        # A key file exists in the test environment, so it is refused before any password is asked for.
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            status = keys.main(["recover-lost", "--admin-email", "a@b.c"],
                               ask_password=lambda prompt: prompts.append(prompt) or "x")
        self.assertEqual(status, 1)
        self.assertEqual(prompts, [])
        self.assertIn("A key file already exists", err.getvalue())
        self.assertNotIn("isn't available yet", err.getvalue())

    def test_it_asks_with_getpass_by_default(self):
        from backend import key_recovery
        import inspect
        self.assertIn("getpass.getpass", inspect.getsource(key_recovery.run))


@unittest.skipUnless(shutil.which("node"), "needs node to run the page's script")
class BackgroundCardScriptTest(unittest.TestCase):
    """frontend/background.js in node, with small stand-ins for the page that record what it does."""

    HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [script, common, scenario] = [process.argv[1], process.argv[2], JSON.parse(process.argv[3])];
const calls = [], said = [], scheduleLoads = [];
const el = (extra = {}) => ({ textContent: '', hidden: false, checked: false, disabled: false, ...extra });
const els = { '#background-switch': el({ disabled: true }), '#background-explain': el({ textContent: 'Loading…' }), '#background-more': el(),
              '#background-status': el(), '#background-error': el({ hidden: true }), '#background-retry': el({ hidden: true }) };
let state = scenario.server || { enabled: false, since: null };
const ctx = {
  document: { addEventListener() {} }, $: (sel) => els[sel], $$: () => [],
  setText: (e, t) => { if (e.textContent !== t) e.textContent = t; },
  announce: (msg, urgent) => said.push([msg, !!urgent]),
  loadSchedule: (opts) => scheduleLoads.push(opts),
  scrollBehavior: () => 'auto',
  confirmPassword: async () => scenario.confirm !== false,
  api: async (url, opts = {}) => {
    const method = opts.method || 'GET', body = opts.body ? JSON.parse(opts.body) : null;
    calls.push([method, url, body]);
    if (scenario.needsAuth && body && body.enabled && !ctx.__authed) { ctx.__authed = true; const e = new Error('For your safety, enter your password again to do this.'); e.code = 'reauth_required'; throw e; }
    if (scenario.failWith && (method === 'POST' || scenario.failLoad) && !(scenario.retryWorks && ctx.__retrying)) throw new Error(scenario.failWith);
    if (method === 'POST') state = { enabled: body.enabled, since: body.enabled ? '2026-10-07T01:02:03Z' : null };
    return state;
  },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(common, 'utf8').match(/async function withRecentAuth\(fn\) \{.*?\n\}\n/s)[0], ctx);
vm.runInContext(fs.readFileSync(common, 'utf8').match(/const BACKGROUND_ON_COPY = .*\nconst BACKGROUND_OFF_COPY = .*\n/)[0], ctx);
const page = vm.runInContext(fs.readFileSync(script, 'utf8') + '\n;({ background, changeBackground, loadBackground })', ctx);
(async () => {
  const snap = () => ({ checked: els['#background-switch'].checked, disabled: els['#background-switch'].disabled,
    explain: els['#background-explain'].textContent, more: els['#background-more'].textContent, status: els['#background-status'].textContent,
    error: els['#background-error'].hidden ? '' : els['#background-error'].textContent, retry: !els['#background-retry'].hidden });
  const out = { before: snap() };
  await page.loadBackground();
  out.loaded = snap();
  if (scenario.retryWorks) { ctx.__retrying = true; await page.loadBackground(); out.retried = snap(); }
  for (const want of scenario.changes || []) {
    els['#background-switch'].checked = want; // what the click does before the handler runs
    if (scenario.twice) { const first = page.changeBackground(want); await page.changeBackground(want); await first; } else await page.changeBackground(want);
    out['after_' + want] = snap();
  }
  out.calls = calls; out.said = said; out.scheduleLoads = scheduleLoads.length;
  console.log(JSON.stringify(out));
})();
"""

    OFF = "Only you can unlock your data. JobHuntPA can't run searches while you're away."
    ON = ("JobHuntPA keeps a key on the server so it can run scheduled searches while you're away. The server's "
          "operator could technically read your data.")

    def run_page(self, scenario: dict) -> dict:
        done = subprocess.run(["node", "-e", self.HARNESS, "--", str(FRONTEND / "background.js"),
                               str(FRONTEND / "common.js"), json.dumps(scenario)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_switch_waits_for_the_server_then_shows_the_state_in_the_designs_words(self):
        out = self.run_page({})
        self.assertTrue(out["before"]["disabled"])
        self.assertEqual(out["calls"], [["GET", "/api/account/background", None]])
        loaded = out["loaded"]
        self.assertEqual((loaded["checked"], loaded["disabled"], loaded["explain"], loaded["status"]),
                         (False, False, self.OFF, "Off"))
        out = self.run_page({"server": {"enabled": True, "since": "2026-10-07T01:02:03Z"}})
        loaded = out["loaded"]
        self.assertEqual((loaded["checked"], loaded["explain"], loaded["status"]), (True, self.ON, "On"))
        self.assertIn("It has been on since", loaded["more"])
        self.assertIn("2026", loaded["more"])

    def test_turning_it_on_posts_updates_the_words_says_so_and_refreshes_the_schedule_card(self):
        out = self.run_page({"changes": [True]})
        self.assertEqual(out["calls"][-1], ["POST", "/api/account/background", {"enabled": True}])
        after = out["after_true"]
        self.assertEqual((after["checked"], after["explain"], after["status"], after["error"]), (True, self.ON, "On", ""))
        self.assertEqual(out["said"], [["Background access is on.", False]])
        self.assertEqual(out["scheduleLoads"], 1)

    def test_an_old_sign_in_asks_for_the_password_and_carries_on(self):
        out = self.run_page({"changes": [True], "needsAuth": True})
        self.assertEqual([c[0] for c in out["calls"]], ["GET", "POST", "POST"])
        self.assertTrue(out["after_true"]["checked"])

    def test_without_the_password_the_switch_goes_back_and_the_reason_is_shown(self):
        out = self.run_page({"changes": [True], "needsAuth": True, "confirm": False})
        after = out["after_true"]
        self.assertFalse(after["checked"], "the switch shows what the server says: still off")
        self.assertEqual(after["explain"], self.OFF)
        self.assertIn("Couldn't turn background access on", after["error"])
        self.assertIn("enter your password again", after["error"])
        self.assertTrue(out["said"][-1][1], "the refusal is announced urgently")
        self.assertEqual(out["scheduleLoads"], 0)

    def test_turning_it_off_says_the_key_is_gone(self):
        out = self.run_page({"server": {"enabled": True, "since": "2026-10-07T01:02:03Z"}, "changes": [False]})
        after = out["after_false"]
        self.assertEqual((after["checked"], after["explain"], after["status"]), (False, self.OFF, "Off"))
        self.assertIn("no longer holds your key", out["said"][-1][0])
        self.assertEqual(out["calls"][-1], ["POST", "/api/account/background", {"enabled": False}])

    def test_a_server_error_puts_the_switch_back_and_shows_it(self):
        out = self.run_page({"server": {"enabled": True, "since": None}, "changes": [False], "failWith": "Server trouble"})
        after = out["after_false"]
        self.assertTrue(after["checked"], "still on: the server didn't change")
        self.assertEqual(after["explain"], self.ON)
        self.assertEqual(after["error"], "Couldn't turn background access off: Server trouble")

    def test_a_card_that_cant_load_says_so_and_stays_disabled(self):
        out = self.run_page({"failWith": "Server trouble", "failLoad": True})
        self.assertIn("Couldn't load this setting: Server trouble", out["loaded"]["error"])  # the alert, not the hint
        self.assertTrue(out["loaded"]["disabled"])
        self.assertTrue(out["loaded"]["retry"])
        self.assertEqual(out["said"], [["Couldn't load background access: Server trouble", True]])

    def test_trying_again_after_a_failed_load_enables_the_switch(self):
        out = self.run_page({"failWith": "Server trouble", "failLoad": True, "retryWorks": True})
        self.assertTrue(out["loaded"]["disabled"])
        self.assertFalse(out["retried"]["disabled"])
        self.assertEqual((out["retried"]["error"], out["retried"]["retry"], out["retried"]["status"]), ("", False, "Off"))

    def test_a_second_change_while_one_is_running_is_ignored(self):
        out = self.run_page({"changes": [True], "twice": True})
        self.assertEqual([c[0] for c in out["calls"]], ["GET", "POST"])


@unittest.skipUnless(shutil.which("node"), "needs node to run the page's script")
class ScheduleStatusScriptTest(unittest.TestCase):
    """renderScheduleStatus in frontend/app.js, with stand-ins for the page: what the schedule card tells the user."""

    HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const sch = JSON.parse(process.argv[2]);
const els = { '#sched-status': { innerHTML: '' }, '#next-scheduled': { hidden: true, textContent: '' } };
const ctx = { $: (sel) => els[sel], esc: (t) => String(t), fmtScheduleTime: (t) => t, zoneLabel: (z) => z,
  scheduledRunLine: () => '', fmtHolidayDate: (d) => d, Intl };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8').match(/function renderScheduleStatus\(sch\) \{.*?\n\}\n/s)[0], ctx);
ctx.renderScheduleStatus(sch);
console.log(JSON.stringify({ status: els['#sched-status'].innerHTML, noteHidden: els['#next-scheduled'].hidden }));
"""

    BASE = {"enabled": True, "background": True, "times": ["10:00"], "days": [1], "next_run": "2026-10-09T10:00:00+10:00",
            "timezone": "Australia/Sydney", "skipped": None, "nothing_to_search": False, "skipped_holidays": [], "last_run": None}

    def render(self, **over) -> dict:
        done = subprocess.run(["node", "-e", self.HARNESS, "--", str(FRONTEND / "app.js"), json.dumps({**self.BASE, **over})],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_working_schedule_shows_the_next_search(self):
        out = self.render()
        self.assertIn("Next search:", out["status"])
        self.assertFalse(out["noteHidden"])

    def test_no_background_access_says_so_and_offers_to_turn_it_on(self):
        out = self.render(background=False, skipped={"reason": "background_off", "slot": None,
                                                     "text": "Scheduled search skipped: background access is off"})
        self.assertIn("Scheduled search skipped: background access is off", out["status"])
        self.assertIn(">Turn on background access</button>", out["status"])
        self.assertNotIn("Next search:", out["status"])
        self.assertTrue(out["noteHidden"])

    def test_a_key_the_server_cant_open_is_shown_even_though_background_access_is_on(self):
        text = schedule.SKIP_TEXT["key_unusable"]
        out = self.render(background=True, skipped={"reason": "key_unusable", "slot": "2026-10-02T10:00:00+10:00", "text": text})
        self.assertIn(text, out["status"])
        self.assertIn("data-focus-background>Turn it off and on again</button>", out["status"])
        self.assertNotIn("Next search:", out["status"])
        self.assertTrue(out["noteHidden"], "the Jobs tab doesn't promise a search that will be passed over")


if __name__ == "__main__":
    unittest.main()
