"""Recovery codes, password changes, signing out elsewhere and deleting an account (accounts
design §3, §6.2 to §6.8; Phase E). Everything runs on the shared test database through the app's own endpoints.
"""
from __future__ import annotations

import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

from backend import crypto, docs, lockout, migrate_encryption, ratelimit, users  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import (  # noqa: E402
    UPLOAD_DIR,
    AuthSession,
    ChatMessage,
    ChatThread,
    CompanyProfile,
    CompanySource,
    CoverLetter,
    DealbreakerSet,
    Document,
    FitResult,
    Job,
    JobDuplicate,
    LlmUsage,
    Pin,
    Posting,
    SearchRun,
    SearchSchedule,
    SessionLocal,
    User,
    UserKeys,
    UserProfile,
    Workspace,
    engine,
    utcnow,
)

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new password"


class AccountTestCase(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)

    # -- clients --------------------------------------------------------------------------------------------------

    def anonymous(self, **kwargs) -> TestClient:
        c = TestClient(app, **kwargs)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def signed_in(self, account: helpers.Account) -> helpers.SignedInClient:
        c = helpers.client(account.email)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def login(self, account: helpers.Account, password: str | None = None, **extra) -> tuple[TestClient, object]:
        ratelimit.AUTH.reset()  # (the wrong tries of an account are not forgotten: only the per-address request count)
        c = self.anonymous()
        r = c.post("/api/auth/login", json={"email": account.email, "password": password or account.password, **extra})
        if r.status_code == 200 and "csrf" in r.json():
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
        return c, r

    def make_stale(self, c: TestClient) -> None:
        """This client's session last had its password entered 20 minutes ago."""
        sid = crypto.session_id(crypto.b64d(c.cookies.get("jh_session")))
        db = SessionLocal()
        try:
            db.get(AuthSession, sid).reauth_at = utcnow() - timedelta(minutes=20)
            db.commit()
        finally:
            db.close()

    def row(self, account: helpers.Account) -> dict:
        db = SessionLocal()
        try:
            u = db.get(User, account.id)
            return {"failed": helpers.wrong_tries(account.id), "locked": ratelimit.guesses.waiting(account.id) or None,
                    "background": db.get(UserKeys, u.id).server_wrapped_dek is not None}
        finally:
            db.close()

    def sessions_of(self, account: helpers.Account) -> list[AuthSession]:
        db = SessionLocal()
        try:
            return db.query(AuthSession).filter(AuthSession.user_id == account.id).all()
        finally:
            db.close()

    def write_pin(self, account: helpers.Account, label: str) -> None:
        db = SessionLocal()
        try:
            with helpers.as_account(account):
                db.add(Pin(workspace_id=account.workspace_id, label=label, kind="home", lat=-33.8, lng=151.2,
                           radius_km=10))
                db.commit()
        finally:
            db.close()

    def pin_labels(self, c: TestClient) -> list[str]:
        return [p["label"] for p in c.get("/api/pins").json()]


# ---------------------------------------------------------------------------
# Confirming the password, and where recent sign-in is needed
# ---------------------------------------------------------------------------

class NoSecondStepTest(AccountTestCase):
    """Two-step sign-in is gone: the password is the whole sign-in, and its routes and fields are gone with it."""

    def test_sign_in_is_one_step_and_state_says_nothing_of_a_second(self):
        alice = helpers.make_user(password=PASSWORD)
        c, r = self.login(alice)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("mfa_required", r.json())
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        state = self.anonymous().get("/api/auth/state").json()
        self.assertEqual(set(state), {"setup_required", "folder_required", "signed_in"})
        self.assertNotIn("mfa_enabled", c.get("/api/auth/security").json())

    def test_the_two_step_routes_are_gone_signed_in_or_not(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        anonymous = self.anonymous()
        for path in ("/api/auth/login/mfa", "/api/auth/mfa/setup", "/api/auth/mfa/enable", "/api/auth/mfa/disable"):
            self.assertIn(c.post(path, json={}).status_code, (404, 405), path)
            self.assertEqual(anonymous.post(path, json={}).status_code, 401, path)  # (not an open path any more)

    def test_recover_takes_no_second_proof(self):
        alice = helpers.make_user(password=PASSWORD)
        code = helpers.pending_codes(alice)[0]
        body = {"email": alice.email, "recovery_code": code, "new_password": NEW_PASSWORD}
        for extra in ({"code": "123456"}, {"second_recovery_code": code}):
            r = self.anonymous().post("/api/auth/recover", json={**body, **extra})
            self.assertEqual(r.status_code, 422, extra)
        self.assertEqual(users.recovery_codes_left(SessionLocal(), alice.id), 8)  # nothing was used up
        r = self.anonymous().post("/api/auth/recover", json=body)
        self.assertEqual((r.status_code, r.json()["recovery_codes_left"]), (200, 7))


class RecentSignInTest(AccountTestCase):
    def test_reauth_with_the_password_starts_fifteen_more_minutes(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        self.assertTrue(c.get("/api/auth/security").json()["recent_auth"])
        self.make_stale(c)
        self.assertFalse(c.get("/api/auth/security").json()["recent_auth"])
        r = c.post("/api/account/background", json={"enabled": True})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        r = c.post("/api/auth/reauth", json={"password": "not the password"})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_password", "password"))
        self.assertFalse(c.get("/api/auth/security").json()["recent_auth"])
        r = c.post("/api/auth/reauth", json={"password": PASSWORD})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "minutes": 15}))
        self.assertTrue(c.get("/api/auth/security").json()["recent_auth"])
        self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)

    def test_wrong_passwords_are_counted_and_lock_like_sign_in(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        for _ in range(4):
            self.assertEqual(c.post("/api/auth/reauth", json={"password": "nope nope nope"}).status_code, 422)
        r = c.post("/api/auth/reauth", json={"password": "nope nope nope"})
        self.assertEqual(r.status_code, 422)
        r = c.post("/api/auth/reauth", json={"password": PASSWORD})  # even the right one waits
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        self.assertIn("Retry-After", r.headers)
        # The wait is for the browser that kept guessing: the owner, signing in from somewhere else, isn't held up.
        _, login = self.login(alice)
        self.assertEqual(login.status_code, 200)

    def test_every_sensitive_action_asks_for_recent_sign_in(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        self.make_stale(c)
        for method, path, body in (("post", "/api/account/background", {"enabled": True}),
                                   ("post", "/api/auth/recovery-codes", None),
                                   ("post", "/api/account/delete", {"password": PASSWORD})):
            r = getattr(c, method)(path, **({"json": body} if body else {}))
            self.assertEqual((r.status_code, r.json().get("code")), (403, "reauth_required"), f"{path}: {r.text}")
        self.assertFalse(self.row(alice)["background"])  # nothing happened
        self.assertEqual(c.get("/api/auth/me").status_code, 200)

    def test_deleting_the_leftover_files_asks_for_the_password_not_a_new_sign_in(self):
        first = self.signed_in(helpers.Account(helpers.admin_id(), helpers.ADMIN_EMAIL, helpers.ADMIN_PASSWORD, 0))
        self.make_stale(first)
        from backend import migrate_encryption
        with mock.patch.object(migrate_encryption, "delete_leftovers", return_value=[]) as deleted:
            r = first.post("/api/settings/leftovers/delete")
            self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
            deleted.assert_not_called()
            self.assertEqual(first.post("/api/auth/reauth", json={"password": helpers.ADMIN_PASSWORD}).status_code, 200)
            self.assertEqual(first.post("/api/settings/leftovers/delete").status_code, 200)
            deleted.assert_called_once()


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------

class RecoveryCodesTest(AccountTestCase):
    def test_every_code_resets_the_password_once_and_keeps_the_data(self):
        alice = helpers.make_user(password=PASSWORD)
        self.write_pin(alice, "Alice's home MARKER")
        codes = helpers.show_codes(self.signed_in(alice)).json()["codes"]
        self.assertEqual(len(codes), 8)
        password = PASSWORD
        for n, code in enumerate(codes):
            ratelimit.reset_all()
            c = self.anonymous()
            new = f"{NEW_PASSWORD} number {n}"
            r = c.post("/api/auth/recover", json={"email": alice.email.upper(), "recovery_code": code.lower(),
                                                  "new_password": new})
            self.assertEqual(r.status_code, 200, f"code {n}: {r.text}")
            self.assertEqual(r.json()["recovery_codes_left"], 7 - n)
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            self.assertEqual(self.pin_labels(c), ["Alice's home MARKER"])  # signed in, and the data opens
            ratelimit.reset_all()
            _, old = self.login(alice, password)
            self.assertEqual(old.status_code, 401)  # the old password no longer works
            self.assertEqual(self.login(alice, new)[1].status_code, 200)
            again = self.anonymous().post("/api/auth/recover", json={
                "email": alice.email, "recovery_code": code, "new_password": "another good password"})
            self.assertEqual((again.status_code, again.json()["code"]), (401, "wrong_recovery"), f"reused {n}")
            password = new
        c = self.anonymous()
        r = c.post("/api/auth/recover", json={"email": alice.email, "recovery_code": codes[0],
                                              "new_password": "yet another password"})
        self.assertEqual(r.status_code, 401)

    def test_the_data_key_is_the_same_after_a_reset(self):
        alice = helpers.make_user(password=PASSWORD)
        code = helpers.show_codes(self.signed_in(alice)).json()["codes"][0]
        r = self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": code,
                                                            "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200)
        db = SessionLocal()
        try:
            user = users.find_by_name(db, alice.email)
            self.assertEqual(users.unlock_with_password(db, user, NEW_PASSWORD), helpers.key_of(alice.email)[1])
            self.assertIsNone(users.unlock_with_password(db, user, PASSWORD))
        finally:
            db.close()

    def test_other_sessions_end_and_the_person_is_signed_in(self):
        alice = helpers.make_user(password=PASSWORD)
        old = self.signed_in(alice)
        code = helpers.show_codes(old).json()["codes"][0]
        c = self.anonymous()
        r = c.post("/api/auth/recover", json={"email": alice.email, "recovery_code": code,
                                              "new_password": NEW_PASSWORD, "remember": True})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Max-Age", r.headers["set-cookie"])
        self.assertEqual(old.get("/api/auth/me").status_code, 401)
        self.assertEqual(len(self.sessions_of(alice)), 1)
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertTrue(c.get("/api/auth/security").json()["recent_auth"])  # a reset enters the password

    def test_refusals_are_the_same_for_a_wrong_code_and_an_unknown_email(self):
        alice = helpers.make_user(password=PASSWORD)
        good = helpers.show_codes(self.signed_in(alice)).json()["codes"][0]
        wrong = "AAAA-BBBB-CCCC-DDDD"
        a = self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": wrong,
                                                            "new_password": NEW_PASSWORD})
        b = self.anonymous().post("/api/auth/recover", json={"email": "nobody@example.invalid",
                                                            "recovery_code": good, "new_password": NEW_PASSWORD})
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
        self.assertEqual(a.status_code, 401)
        self.assertEqual(self.sessions_of(alice)[0].state, "active")  # nothing changed
        self.assertEqual(self.login(alice)[1].status_code, 200)

    def test_what_the_form_must_have(self):
        alice = helpers.make_user(password=PASSWORD)
        good = helpers.show_codes(self.signed_in(alice)).json()["codes"][0]
        c = self.anonymous()
        r = c.post("/api/auth/recover", json={"email": alice.email, "recovery_code": good, "new_password": "short"})
        self.assertEqual((r.status_code, r.json()["field"]), (422, "new_password"))
        r = c.post("/api/auth/recover", json={"email": alice.email, "recovery_code": "nonsense",
                                              "new_password": NEW_PASSWORD})
        self.assertEqual((r.status_code, r.json()["field"]), (422, "recovery_code"))
        self.assertEqual(c.post("/api/auth/recover", data={"email": alice.email}).status_code, 415)
        self.assertEqual(c.post("/api/auth/recover", json={"email": alice.email, "recovery_code": good,
                                                           "new_password": NEW_PASSWORD},
                                headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.login(alice)[1].status_code, 200)  # still the old password

    def test_wrong_recovery_codes_lock_the_account_like_wrong_passwords(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.anonymous()
        for _ in range(5):
            ratelimit.AUTH.reset()
            c.post("/api/auth/recover", json={"email": alice.email, "recovery_code": "AAAA-BBBB-CCCC-DDDD",
                                              "new_password": NEW_PASSWORD})
        ratelimit.AUTH.reset()
        r = c.post("/api/auth/recover", json={"email": alice.email, "recovery_code": "AAAA-BBBB-CCCC-DDDD",
                                              "new_password": NEW_PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))

    def test_the_recover_endpoint_is_rate_limited_per_address(self):
        c = self.anonymous()
        body = {"recovery_code": "AAAA-BBBB-CCCC-DDDD", "new_password": NEW_PASSWORD}
        codes = [c.post("/api/auth/recover", json={**body, "email": f"x{n}@example.invalid"}).status_code
                 for n in range(11)]
        self.assertEqual(codes[-1], 429)
        self.assertNotIn(429, codes[:10])

    def test_a_disabled_account_cannot_reset(self):
        alice = helpers.make_user(password=PASSWORD)
        code = helpers.show_codes(self.signed_in(alice)).json()["codes"][0]
        db = SessionLocal()
        db.get(User, alice.id).status = "disabled"
        db.commit()
        db.close()
        r = self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": code,
                                                            "new_password": NEW_PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "disabled"))
        self.assertEqual(users.recovery_codes_left(SessionLocal(), alice.id), 8)

    def test_new_codes_replace_all_eight(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        old = helpers.show_codes(c).json()["codes"]
        c.post("/api/auth/recovery-codes/ack")
        self.assertEqual(helpers.show_codes(c).status_code, 404)
        self.assertFalse(c.get("/api/auth/security").json()["recovery_codes_pending"])
        r = c.post("/api/auth/recovery-codes", json={"password": PASSWORD})
        self.assertEqual((r.status_code, {k: v for k, v in r.json().items() if k != "recovery_codes"}),
                         (200, {"ok": True, "recovery_codes_left": 8, "recovery_codes_pending": True}))
        self.assertEqual(len(r.json()["recovery_codes"]), 8)  # (shown at once)
        self.assertTrue(c.get("/api/auth/me").json()["recovery_codes_pending"])
        new = helpers.show_codes(c).json()["codes"]
        self.assertEqual(len(new), 8)
        self.assertFalse(set(old) & set(new))
        for code in old[:2]:  # the old ones are dead
            r = self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": code,
                                                                "new_password": NEW_PASSWORD})
            self.assertEqual(r.status_code, 401)
        self.assertEqual(c.post("/api/auth/recovery-codes/ack").status_code, 200)
        self.assertEqual(helpers.show_codes(c).status_code, 404)  # shown once
        db = SessionLocal()
        try:
            self.assertEqual(db.query(users.RecoveryCode).filter_by(user_id=alice.id).count(), 8)
            self.assertIsNone(db.get(UserKeys, alice.id).pending_codes_enc)
        finally:
            db.close()
        r = self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": new[5],
                                                            "new_password": NEW_PASSWORD})
        self.assertEqual((r.status_code, r.json()["recovery_codes_left"]), (200, 7))

    def test_used_codes_are_replaced_too(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        code = helpers.show_codes(c).json()["codes"][0]
        self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": code,
                                                         "new_password": NEW_PASSWORD})
        c = helpers.client(alice.email, NEW_PASSWORD)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        self.assertEqual(c.get("/api/auth/security").json()["recovery_codes_left"], 7)
        c.post("/api/auth/recovery-codes", json={"password": NEW_PASSWORD})
        self.assertEqual(c.get("/api/auth/security").json()["recovery_codes_left"], 8)


# ---------------------------------------------------------------------------
# Changing the password, and signing out elsewhere
# ---------------------------------------------------------------------------

def on_disk(blob: bytes) -> list[str]:
    """Which of the database file and its write-ahead log hold ``blob``, read as raw bytes (no checkpoint first)."""
    found = []
    for suffix in ("", "-wal"):
        path = Path(str(engine.url.database) + suffix)
        if path.exists() and bytes(blob) in path.read_bytes():
            found.append(path.name)
    return found


class PasswordChangeTest(AccountTestCase):
    def test_the_old_passwords_copy_of_the_data_key_does_not_linger_in_the_log(self):
        alice = helpers.make_user(password=PASSWORD)
        old = bytes(self.row_keys(alice.id).pw_wrapped_dek)
        r = self.signed_in(alice).post("/api/auth/password", json={"current_password": PASSWORD,
                                                                  "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(bytes(self.row_keys(alice.id).pw_wrapped_dek), old)
        self.assertEqual(on_disk(old), [])

    def row_keys(self, user_id: int):
        db = SessionLocal()
        try:
            return db.get(UserKeys, user_id)
        finally:
            db.close()

    def test_the_data_stays_readable_and_the_new_password_opens_it(self):
        alice = helpers.make_user(password=PASSWORD)
        self.write_pin(alice, "Kept through a password change MARKER")
        c = self.signed_in(alice)
        r = c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        self.assertEqual((r.status_code, r.json()["ok"]), (200, True))
        self.assertEqual(self.pin_labels(c), ["Kept through a password change MARKER"])  # this session carries on
        self.assertEqual(self.login(alice, PASSWORD)[1].status_code, 401)
        fresh, r = self.login(alice, NEW_PASSWORD)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pin_labels(fresh), ["Kept through a password change MARKER"])
        db = SessionLocal()
        try:
            user = users.find_by_name(db, alice.email)
            self.assertEqual(users.unlock_with_password(db, user, NEW_PASSWORD), helpers.key_of(alice.email)[1])
        finally:
            db.close()

    def test_nothing_is_re_encrypted(self):
        alice = helpers.make_user(password=PASSWORD)
        self.write_pin(alice, "Stays as it is")
        with engine.connect() as conn:
            before = conn.execute(text("SELECT label FROM pins WHERE workspace_id = :w"),
                                  {"w": alice.workspace_id}).scalar()
        self.signed_in(alice).post("/api/auth/password", json={"current_password": PASSWORD,
                                                              "new_password": NEW_PASSWORD})
        with engine.connect() as conn:
            after = conn.execute(text("SELECT label FROM pins WHERE workspace_id = :w"),
                                 {"w": alice.workspace_id}).scalar()
        self.assertEqual(bytes(before), bytes(after))

    def test_refusals(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        r = c.post("/api/auth/password", json={"current_password": "not it at all", "new_password": NEW_PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_password",
                                                                                "current_password"))
        r = c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"})
        self.assertEqual((r.status_code, r.json()["field"]), (422, "new_password"))
        self.assertEqual(self.login(alice, PASSWORD)[1].status_code, 200)  # still the old one

    def test_other_sessions_end_unless_asked_to_stay(self):
        alice = helpers.make_user(password=PASSWORD)
        here, other = self.signed_in(alice), self.signed_in(alice)
        third = self.signed_in(alice)  # signing in again as the same account made three sessions
        self.assertEqual(here.get("/api/auth/security").json()["other_sessions"], 2)
        r = here.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD,
                                                  "sign_out_others": False})
        self.assertEqual(r.json()["signed_out_others"], 0)
        self.assertEqual(other.get("/api/auth/me").status_code, 200)
        r = here.post("/api/auth/password", json={"current_password": NEW_PASSWORD, "new_password": PASSWORD})
        self.assertEqual(r.json()["signed_out_others"], 2)
        self.assertEqual(other.get("/api/auth/me").status_code, 401)
        self.assertEqual(third.get("/api/auth/me").status_code, 401)
        self.assertEqual(here.get("/api/auth/me").status_code, 200)

    def test_it_counts_as_recent_sign_in(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        self.make_stale(c)
        self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 403)
        c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)

class SignOutEverywhereTest(AccountTestCase):
    def test_every_other_session_ends_and_this_one_stays(self):
        alice, bob = helpers.make_user(password=PASSWORD), helpers.make_user(password=PASSWORD)
        here, phone, laptop = self.signed_in(alice), self.signed_in(alice), self.signed_in(alice)
        bobs = self.signed_in(bob)
        self.assertEqual(here.get("/api/auth/security").json()["other_sessions"], 2)
        r = here.post("/api/auth/sessions/end-others")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "ended": 2}))
        self.assertEqual(phone.get("/api/auth/me").status_code, 401)
        self.assertEqual(laptop.get("/api/auth/me").status_code, 401)
        self.assertEqual(here.get("/api/auth/me").status_code, 200)
        self.assertEqual(bobs.get("/api/auth/me").status_code, 200)  # someone else's sessions are not touched
        self.assertEqual(here.get("/api/auth/security").json()["other_sessions"], 0)

# ---------------------------------------------------------------------------
# Deleting an account
# ---------------------------------------------------------------------------

WORKSPACE_TABLES = {
    "jobs": "workspace_id IN ({ws})",
    "job_duplicates": "workspace_id IN ({ws})",
    "fit_results": "job_id IN (SELECT id FROM jobs WHERE workspace_id IN ({ws}))",
    "cover_letters": "job_id IN (SELECT id FROM jobs WHERE workspace_id IN ({ws}))",
    "documents": "workspace_id IN ({ws})",
    "pins": "workspace_id IN ({ws})",
    "company_sources": "workspace_id IN ({ws})",
    "chat_threads": "workspace_id IN ({ws})",
    "chat_messages": "thread_id IN (SELECT id FROM chat_threads WHERE workspace_id IN ({ws}))",
    "search_runs": "workspace_id IN ({ws})",
    "search_schedules": "id IN ({ws})",
    "user_profile": "id IN ({ws})",
    "dealbreakers": "id IN ({ws})",
    "workspaces": "id IN ({ws})",
}
ACCOUNT_TABLES = ("sessions", "recovery_codes", "user_keys")


class DeleteAccountTest(AccountTestCase):
    def populate(self, account: helpers.Account, tag: str) -> dict:
        """A full set of rows in two workspaces of ``account``, with a sealed upload file each; the file names."""
        db = SessionLocal()
        files = []
        try:
            with helpers.as_account(account):
                second = Workspace(name=f"{tag} second", owner_id=account.id)
                db.add(second)
                db.flush()
                for ws in (account.workspace_id, second.id):
                    first = Job(workspace_id=ws, url=f"https://{tag}.example/{ws}/1", title=f"{tag} job", status="applied")
                    copy = Job(workspace_id=ws, url=f"https://{tag}.example/{ws}/2", title=f"{tag} job", status="to_review")
                    db.add_all([first, copy])
                    db.flush()
                    copy.duplicate_of = first.id
                    db.add_all([
                        FitResult(job_id=first.id, status="ok", score=80, evidence_json="{}"),
                        CoverLetter(job_id=first.id, text=f"Dear {tag}"),
                        JobDuplicate(workspace_id=ws, job_a=first.id, job_b=copy.id, status="merged",
                                     reasons_json="[]"),
                        Pin(workspace_id=ws, label=f"{tag} home", kind="home", lat=-33.8, lng=151.2, radius_km=5),
                        CompanySource(workspace_id=ws, label=f"{tag} co", careers_url=f"https://{tag}.example/c/{ws}"),
                        SearchRun(workspace_id=ws, kind="search", state="done", progress_json="{}"),
                        SearchSchedule(id=ws), UserProfile(id=ws, titles=[f"{tag} title"]), DealbreakerSet(id=ws),
                        LlmUsage(workspace_id=ws, user_id=account.id, purpose="chat"),
                    ])
                    stored = docs.save_upload(f"{tag} resume bytes {ws}".encode())
                    files.append(stored)
                    db.add(Document(workspace_id=ws, filename=f"{tag}.pdf", filetype=".pdf", stored_name=stored,
                                    text=f"{tag} resume text", kind="resume"))
                    stored = docs.save_upload(f"{tag} template bytes {ws}".encode())
                    files.append(stored)
                    db.add(Document(workspace_id=ws, filename=f"{tag}.docx", filetype=".docx", stored_name=stored,
                                    text="", kind="letter_template"))
                    thread = ChatThread(workspace_id=ws, job_ids_json=f"[{first.id}]", title=f"{tag} chat")
                    db.add(thread)
                    db.flush()
                    db.add(ChatMessage(thread_id=thread.id, role="user", content=f"{tag} question"))
                db.commit()
                ids = [account.workspace_id, second.id]
        finally:
            db.close()
        return {"ws": ids, "files": files}

    def counts(self, ws_ids: list[int], user_id: int) -> dict:
        marks = ", ".join(str(i) for i in ws_ids)
        out = {}
        with engine.connect() as conn:
            for table, where in WORKSPACE_TABLES.items():
                out[table] = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where.format(ws=marks)}")).scalar()
            for table in ACCOUNT_TABLES:
                out[table] = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE user_id = :u"), {"u": user_id}).scalar()
            for name, where in (("usage_of_user", "SELECT COUNT(*) FROM llm_usage WHERE user_id = :u"),
                                ("users", "SELECT COUNT(*) FROM users WHERE id = :u")):
                out[name] = conn.execute(text(where), {"u": user_id}).scalar()
        return out

    def test_it_removes_every_row_and_file_and_leaves_other_people_alone(self):
        bob, carol = helpers.make_user(password=PASSWORD), helpers.make_user(password=PASSWORD)
        mine, theirs = self.populate(bob, "bobtag"), self.populate(carol, "caroltag")
        db = SessionLocal()
        db.add(Posting(url="https://shared.example/posting-that-stays", title="Shared ad"))
        db.add(CompanyProfile(key="shared co", name="Shared Co", status="done"))
        db.commit()
        db.close()
        c, carols = self.signed_in(bob), self.signed_in(carol)
        before_bob, before_carol = self.counts(mine["ws"], bob.id), self.counts(theirs["ws"], carol.id)
        self.assertEqual(before_bob["jobs"], 4)
        self.assertTrue(all(v >= 1 for v in before_bob.values()), before_bob)
        self.assertTrue(all((UPLOAD_DIR / f).is_file() for f in mine["files"] + theirs["files"]))
        r = c.post("/api/account/delete", json={"password": PASSWORD})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}), r.text)
        self.assertEqual(r.headers["clear-site-data"], '"cache"')
        self.assertIn("Max-Age=0", r.headers["set-cookie"])
        # Bob: nothing left.
        self.assertEqual({k: v for k, v in self.counts(mine["ws"], bob.id).items() if v}, {})
        self.assertFalse(any((UPLOAD_DIR / f).exists() for f in mine["files"]))
        self.assertEqual(c.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.login(bob)[1].status_code, 401)
        # Carol: exactly as before, and she can still use it.
        self.assertEqual(self.counts(theirs["ws"], carol.id), before_carol)
        self.assertTrue(all((UPLOAD_DIR / f).is_file() for f in theirs["files"]))
        self.assertEqual(self.pin_labels(carols), ["caroltag home"])
        self.assertEqual(self.login(carol)[1].status_code, 200)
        # Shared things stay; usage counts stay without a workspace.
        db = SessionLocal()
        try:
            self.assertIsNotNone(db.query(Posting).filter_by(url="https://shared.example/posting-that-stays").first())
            self.assertIsNotNone(db.get(CompanyProfile, "shared co"))
            self.assertEqual(db.query(LlmUsage).filter(LlmUsage.workspace_id.in_(mine["ws"])).count(), 0)
            self.assertGreaterEqual(db.query(LlmUsage).filter(LlmUsage.workspace_id.is_(None),
                                                              LlmUsage.purpose == "chat").count(), 2)
        finally:
            db.close()

    def test_the_email_address_can_be_used_again(self):
        bob = helpers.make_user(password=PASSWORD)
        self.signed_in(bob).post("/api/account/delete", json={"password": PASSWORD})
        again = helpers.make_user(bob.email, password=PASSWORD)  # (SQLite may even reuse the id)
        self.assertEqual(self.login(again)[1].status_code, 200)

    def test_it_needs_the_password(self):
        bob = helpers.make_user(password=PASSWORD)
        mine = self.populate(bob, "kept")
        c = self.signed_in(bob)
        r = c.post("/api/account/delete", json={"password": "the wrong one!!"})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_password", "password"))
        self.assertEqual(c.post("/api/account/delete", json={"password": ""}).status_code, 422)
        self.assertEqual(self.counts(mine["ws"], bob.id)["users"], 1)
        self.assertEqual(c.post("/api/account/delete", json={}).status_code, 422)  # no password at all: still no

    def test_it_needs_the_csrf_token(self):
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        del c.headers["X-CSRF-Token"]
        r = c.post("/api/account/delete", json={"password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "csrf"))

    def test_a_running_search_stops_it(self):
        bob = helpers.make_user(password=PASSWORD)
        mine = self.populate(bob, "busy")
        c = self.signed_in(bob)  # (starting the app marks runs left running as interrupted: plant ours after)
        db = SessionLocal()
        with helpers.as_account(bob):
            run = SearchRun(workspace_id=bob.workspace_id, kind="search", state="running", progress_json="{}")
            db.add(run)
            db.commit()
            run_id = run.id
        db.close()
        r = c.post("/api/account/delete", json={"password": PASSWORD})
        self.assertEqual((r.status_code, r.json().get("code")), (409, "run_active"), r.text)
        self.assertEqual(self.counts(mine["ws"], bob.id)["jobs"], 4)  # nothing was removed
        self.assertTrue(all((UPLOAD_DIR / f).is_file() for f in mine["files"]))
        db = SessionLocal()
        # (an update by id: the run's sealed columns aren't opened)
        db.query(SearchRun).filter(SearchRun.id == run_id).update({SearchRun.state: "done"})
        db.commit()
        db.close()
        self.assertEqual(c.post("/api/account/delete", json={"password": PASSWORD}).status_code, 200)

    def test_the_only_account_is_never_deleted_here(self):
        """Deleting the install's only account would put JobHuntPA back at its first run: only the reset tool, run on
        the computer itself, does that (a real install with one account: tests/test_one_account.py)."""
        bob = helpers.make_user(password=PASSWORD)
        mine = self.populate(bob, "onlyone")
        c = self.signed_in(bob)
        self.assertEqual(c.get("/api/account/delete").json(), {"allowed": True})  # (other accounts exist here)
        with mock.patch.object(users, "is_only_account", return_value=True):
            self.assertEqual(c.get("/api/account/delete").json(), {"allowed": False})
            r = c.post("/api/account/delete", json={"password": PASSWORD})
            self.assertEqual((r.status_code, r.json()["code"]), (409, "only_account"))
            self.assertIn("reset tool", r.json()["detail"])
            self.assertEqual(c.post("/api/account/delete", json={"password": "wrong"}).status_code, 409)  # no oracle
            db = SessionLocal()
            try:
                with self.assertRaises(users.AccountProtected):
                    users.delete_account(db, bob.id)
            finally:
                db.close()
        self.assertEqual(self.counts(mine["ws"], bob.id)["jobs"], 4)
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.anonymous().get("/api/account/delete").status_code, 401)

    def test_a_failure_midway_leaves_the_account_whole(self):
        bob = helpers.make_user(password=PASSWORD)
        mine = self.populate(bob, "midfail")
        before = self.counts(mine["ws"], bob.id)  # (sessions come and go with the clients: left out below)
        c = self.signed_in(bob)
        with mock.patch.object(migrate_encryption, "owner_of", side_effect=RuntimeError("boom")):  # (after many deletes)
            with self.assertRaises(RuntimeError):
                c.post("/api/account/delete", json={"password": PASSWORD})
        self.assertEqual(self.counts(mine["ws"], bob.id) | {"sessions": 0}, before | {"sessions": 0})
        self.assertTrue(all((UPLOAD_DIR / f).is_file() for f in mine["files"]))
        self.assertEqual(c.post("/api/account/delete", json={"password": PASSWORD}).status_code, 200)  # (lock let go)
        self.assertEqual(self.counts(mine["ws"], bob.id)["users"], 0)

    def test_a_file_that_cannot_be_deleted_never_leaves_a_half_deleted_account(self):
        bob = helpers.make_user(password=PASSWORD)
        mine = self.populate(bob, "stuck")
        db = SessionLocal()
        try:
            with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("no")):
                result = users.delete_account(db, bob.id)
        finally:
            db.close()
        self.assertEqual((result["workspaces"], result["files"]), (2, 0))  # the rows are gone; the files are not counted
        self.assertEqual({k: v for k, v in self.counts(mine["ws"], bob.id).items() if v}, {})
        for name in mine["files"]:  # sealed, so unreadable without anyone's key
            (UPLOAD_DIR / name).unlink(missing_ok=True)

    def test_an_upload_that_lands_while_the_account_is_being_deleted_is_removed_too(self):
        bob = helpers.make_user(password=PASSWORD)
        mine = self.populate(bob, "racetag")
        late = {}
        armed = [True]

        def upload_now(conn, cursor, statement, parameters, context, executemany):
            # The moment the delete takes the write lock: another tab's upload commits just before it.
            if armed[0] and statement.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                armed[0] = False
                other = SessionLocal()
                try:
                    with helpers.as_account(bob):
                        stored = docs.save_upload(b"a late upload")
                        late["file"] = stored
                        other.add(Document(workspace_id=bob.workspace_id, filename="late.pdf", filetype=".pdf",
                                           stored_name=stored, text="late", kind="resume"))
                        other.commit()
                finally:
                    other.close()

        db = SessionLocal()
        event.listen(engine, "before_cursor_execute", upload_now)
        try:
            users.delete_account(db, bob.id)
        finally:
            event.remove(engine, "before_cursor_execute", upload_now)
            db.close()
        self.assertIn("file", late, "the late upload happened")
        for name in mine["files"] + [late["file"]]:
            self.assertFalse((UPLOAD_DIR / name).exists(), f"{name} should be gone")

    def test_delete_account_works_without_the_users_key_and_reports_what_it_did(self):
        bob = helpers.make_user(password=PASSWORD)
        mine = self.populate(bob, "nokey-path")
        db = SessionLocal()
        try:
            self.assertFalse(crypto.has_scope())
            result = users.delete_account(db, bob.id)  # no key scope of the user's: it never opens a sealed value
        finally:
            db.close()
        self.assertEqual(result, {"workspaces": 2, "files": 4})  # a document and a letter template in each
        self.assertEqual({k: v for k, v in self.counts(mine["ws"], bob.id).items() if v}, {})

    def test_deleting_the_account_ends_every_session(self):
        bob = helpers.make_user(password=PASSWORD)
        c, _ = self.login(bob)
        other, _ = self.login(bob)
        self.assertEqual(len(self.sessions_of(bob)), 2)
        self.assertEqual(c.post("/api/account/delete", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(self.sessions_of(bob), [])
        self.assertEqual(other.get("/api/auth/me").status_code, 401)

    def test_the_wrong_tries_and_limits_of_a_deleted_account_are_forgotten(self):
        """SQLite may give the id of the newest account to the next one, which must not inherit these."""
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        for _ in range(6):
            self.login(bob, "the wrong one!!")
        self.assertGreater(lockout.waiting(bob.id), 0)
        self.assertEqual(c.post("/api/account/delete", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(lockout.waiting(bob.id), 0)


# ---------------------------------------------------------------------------
# What the logs and answers must not hold
# ---------------------------------------------------------------------------

class NoSecretsInLogsTest(AccountTestCase):
    def test_nothing_secret_reaches_the_log(self):
        alice = helpers.make_user(f"Alice Loggable {os.urandom(3).hex()}", password=PASSWORD)
        with self.assertLogs(level="DEBUG") as logged:
            self.signed_in(alice)
            codes = helpers.pending_codes(alice)
            c, _ = self.login(alice, "a wrong password")
            c, _ = self.login(alice)
            c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
            self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": "AAAA-BBBB-CCCC-DDDD",
                                                             "new_password": "a wrong try at recovery"})
            c = self.anonymous()
            r = c.post("/api/auth/recover", json={"email": alice.email, "recovery_code": codes[0],
                                                  "new_password": "another good password"})
            self.assertEqual(r.status_code, 200, r.text)
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            self.assertEqual(c.post("/api/account/delete", json={"password": "another good password"}).status_code, 200)
        text_out = "\n".join(logged.output)
        for secret_text in (alice.email, PASSWORD, NEW_PASSWORD,
                            "another good password", codes[0], codes[1], codes[0].replace("-", "")):
            self.assertNotIn(secret_text, text_out)


# ---------------------------------------------------------------------------
# Review fixes: what a stolen session (or a stolen session plus the password) can and can't do
# ---------------------------------------------------------------------------

class StolenSessionTest(AccountTestCase):
    def test_unsaved_recovery_codes_need_the_password_again_even_in_a_fresh_session(self):
        """A cookie taken in the 15 minutes after sign-in counts as a recent sign-in, and each code opens the data key and
        survives a later password change: reading them takes the password in the request."""
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        self.assertTrue(c.get("/api/auth/security").json()["recent_auth"])
        self.assertIn(c.get("/api/auth/recovery-codes").status_code, (404, 405))  # (a GET no longer shows them)
        for body in (None, {}, {"password": ""}):
            r = c.post("/api/auth/recovery-codes/show", **({"json": body} if body is not None else {}))
            self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "invalid", "password"), body)
        r = helpers.show_codes(c, "not the password")
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_password", "password"))
        self.assertNotIn("codes", r.json())
        self.make_stale(c)  # the password in the request is the proof: no separate "recent sign-in" step
        r = helpers.show_codes(c)
        self.assertEqual((r.status_code, r.json()["codes"]), (200, helpers.pending_codes(alice)))
        self.assertEqual(len(r.json()["codes"]), 8)
        self.assertEqual(helpers.show_codes(self.login(alice)[0], PASSWORD).status_code, 200)
        self.assertEqual(c.post("/api/auth/recovery-codes/ack").status_code, 200)
        self.assertEqual(helpers.show_codes(c).status_code, 404)

    def test_wrong_passwords_when_showing_the_codes_add_up_to_a_wait(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        for _ in range(5):
            self.assertEqual(helpers.show_codes(c, "not the password").status_code, 422)
        r = helpers.show_codes(c)  # even the right one waits now
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        self.assertNotIn("codes", r.json())

    def test_new_codes_come_in_the_answer_that_made_them(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        r = c.post("/api/auth/recovery-codes", json={"password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["recovery_codes"], helpers.pending_codes(alice))

    def test_new_codes_take_the_password_and_nothing_else(self):
        """There is no second step any more: the fields that carried its code are refused, not ignored."""
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        self.assertEqual(c.post("/api/auth/recovery-codes", json={"password": PASSWORD}).status_code, 200)
        for extra in ({"code": "123456"}, {"recovery_code": ""}):
            for path in ("/api/auth/recovery-codes", "/api/auth/recovery-codes/show"):
                r = c.post(path, json={"password": PASSWORD, **extra})
                self.assertEqual(r.status_code, 422, (path, extra))

    def test_a_session_alone_cannot_make_codes_even_right_after_sign_in(self):
        """A cookie taken in the 15 minutes after sign-in is a recent sign-in. New codes open the data key and can set a
        new password, so the request must carry the password itself."""
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        before = helpers.show_codes(c).json()["codes"]
        self.assertTrue(c.get("/api/auth/security").json()["recent_auth"])
        for body in (None, {}, {"password": ""}):
            r = c.post("/api/auth/recovery-codes", **({"json": body} if body is not None else {}))
            self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "invalid", "password"), body)
        r = c.post("/api/auth/recovery-codes", json={"password": "not the password"})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_password", "password"))
        self.assertEqual(helpers.show_codes(c).json()["codes"], before)  # nothing was replaced
        self.assertEqual(c.post("/api/auth/recovery-codes", json={"password": PASSWORD}).status_code, 200)
        self.assertFalse(set(before) & set(helpers.show_codes(c).json()["codes"]))

    def test_a_wrong_password_still_counts_and_a_right_one_still_clears_elsewhere(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        c.post("/api/auth/reauth", json={"password": "wrong wrong wrong"})
        self.assertEqual(self.row(alice)["failed"], 1)
        self.assertEqual(c.post("/api/auth/reauth", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(self.row(alice)["failed"], 0)

if __name__ == "__main__":
    unittest.main()
