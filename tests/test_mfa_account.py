"""Two-step sign-in, recovery codes, password changes, signing out elsewhere and deleting an account (accounts
design §3, §6.2 to §6.8; Phase E). Everything runs on the shared test database through the app's own endpoints, with
the clock for authenticator codes under the test's control (``backend.totp.now``).
"""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

from backend import admin_key, crypto, docs, migrate_encryption, ratelimit, sessions, totp, users  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import (  # noqa: E402
    UPLOAD_DIR,
    AdminAudit,
    AdminKeyGrant,
    AuthSession,
    AuthToken,
    ChatMessage,
    ChatThread,
    CommuteCache,
    CompanyProfile,
    CompanySource,
    CoverLetter,
    DealbreakerSet,
    Document,
    FitResult,
    Job,
    JobDuplicate,
    LlmUsage,
    MembershipGrant,
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


class Clock:
    """The time authenticator codes are checked against, moved by hand."""

    def __init__(self):
        self.t = (int(time.time()) // totp.STEP_SECONDS) * totp.STEP_SECONDS + 15.0

    def advance(self, steps: int = 1) -> None:
        self.t += steps * totp.STEP_SECONDS

    @property
    def step(self) -> int:
        return int(self.t // totp.STEP_SECONDS)


class AccountTestCase(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)
        self.clock = Clock()
        patch = mock.patch("backend.totp.now", lambda: self.clock.t)
        patch.start()
        self.addCleanup(patch.stop)

    # -- clients --------------------------------------------------------------------------------------------------

    def anonymous(self) -> TestClient:
        c = TestClient(app)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def signed_in(self, account: helpers.Account) -> helpers.SignedInClient:
        c = helpers.client(account.email)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def login(self, account: helpers.Account, password: str | None = None, **extra) -> tuple[TestClient, object]:
        ratelimit.reset_all()
        c = self.anonymous()
        r = c.post("/api/auth/login", json={"email": account.email, "password": password or account.password, **extra})
        if r.status_code == 200 and "csrf" in r.json():
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
        return c, r

    def mfa_client(self, account: helpers.Account, secret: str) -> TestClient:
        """A client signed in to an account with two-step sign-in (password, then the code of the moment, after which
        the clock moves to the next step)."""
        c, _ = self.login(account)
        self.assertEqual(self.second_step(c, code=self.code(secret)).status_code, 200)
        self.clock.advance()
        return c

    def second_step(self, c: TestClient, **body):
        ratelimit.reset_all()
        r = c.post("/api/auth/login/mfa", json=body)
        if r.status_code == 200:
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
        return r

    # -- accounts ---------------------------------------------------------------------------------------------------

    def with_mfa(self, **kwargs) -> tuple[helpers.Account, str]:
        """An account with two-step sign-in on (set up through the API) and no session; its secret. The clock has moved
        past the code that finished the setup."""
        account = helpers.make_user(password=PASSWORD, **kwargs)
        c = self.signed_in(account)
        secret = c.post("/api/auth/mfa/setup").json()["secret"]
        r = c.post("/api/auth/mfa/enable", json={"code": totp.code_at(secret, self.clock.step)})
        self.assertEqual(r.status_code, 200, r.text)
        c.post("/api/auth/logout")  # leaves no session behind: tests count them
        self.clock.advance(2)  # past the code that finished setup, with room to try the step before now
        return account, secret

    def code(self, secret: str, offset: int = 0) -> str:
        return totp.code_at(secret, self.clock.step + offset)

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
            return {"mfa": u.mfa_enabled, "secret": u.totp_secret_enc, "pending": u.totp_pending_enc,
                    "last_step": u.totp_last_step, "failed": u.failed_logins, "locked": u.locked_until}
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

class RecentSignInTest(AccountTestCase):
    def test_reauth_with_the_password_starts_fifteen_more_minutes(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        self.assertTrue(c.get("/api/auth/security").json()["recent_auth"])
        self.make_stale(c)
        self.assertFalse(c.get("/api/auth/security").json()["recent_auth"])
        r = c.post("/api/auth/mfa/setup")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        r = c.post("/api/auth/reauth", json={"password": "not the password"})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_password", "password"))
        self.assertFalse(c.get("/api/auth/security").json()["recent_auth"])
        r = c.post("/api/auth/reauth", json={"password": PASSWORD})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "minutes": 15}))
        self.assertTrue(c.get("/api/auth/security").json()["recent_auth"])
        self.assertEqual(c.post("/api/auth/mfa/setup").status_code, 200)

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
        _, login = self.login(alice)  # and so does signing in
        self.assertEqual((login.status_code, login.json()["code"]), (429, "locked"))

    def test_every_sensitive_action_asks_for_recent_sign_in(self):
        alice, secret = self.with_mfa()
        c = self.mfa_client(alice, secret)
        self.make_stale(c)
        for method, path, body in (("post", "/api/auth/mfa/setup", None),
                                   ("post", "/api/auth/mfa/enable", {"code": "123456"}),
                                   ("post", "/api/auth/mfa/disable", {"password": PASSWORD, "code": "123456"}),
                                   ("post", "/api/auth/recovery-codes", None),
                                   ("post", "/api/account/delete", {"password": PASSWORD})):
            r = getattr(c, method)(path, **({"json": body} if body else {}))
            self.assertEqual((r.status_code, r.json().get("code")), (403, "reauth_required"), f"{path}: {r.text}")
        self.assertTrue(self.row(alice)["mfa"])  # nothing happened

    def test_deleting_the_leftover_files_asks_for_the_password_not_a_new_sign_in(self):
        admin = self.signed_in(helpers.Account(helpers.admin_id(), helpers.ADMIN_EMAIL, helpers.ADMIN_PASSWORD, 0))
        self.make_stale(admin)
        from backend import migrate_encryption
        with mock.patch.object(migrate_encryption, "delete_leftovers", return_value=[]) as deleted:
            r = admin.post("/api/admin/plaintext-files/delete")
            self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
            deleted.assert_not_called()
            self.assertEqual(admin.post("/api/auth/reauth", json={"password": helpers.ADMIN_PASSWORD}).status_code, 200)
            self.assertEqual(admin.post("/api/admin/plaintext-files/delete").status_code, 200)
            deleted.assert_called_once()


# ---------------------------------------------------------------------------
# Setting up two-step sign-in
# ---------------------------------------------------------------------------

class EnrolmentTest(AccountTestCase):
    def test_setup_gives_the_uri_a_qr_code_and_the_secret_in_groups(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        body = c.post("/api/auth/mfa/setup").json()
        self.assertEqual(set(body), {"otpauth_uri", "qr_svg", "secret", "secret_groups"})
        self.assertTrue(body["otpauth_uri"].startswith("otpauth://totp/JobHuntPA:"))
        self.assertIn(f"secret={body['secret']}", body["otpauth_uri"])
        self.assertIn("issuer=JobHuntPA", body["otpauth_uri"])
        self.assertIn(alice.email.replace("@", "%40"), body["otpauth_uri"])
        self.assertEqual(body["secret_groups"].replace(" ", ""), body["secret"])
        self.assertTrue(all(len(g) == 4 for g in body["secret_groups"].split()))
        svg = body["qr_svg"]
        self.assertTrue(svg.startswith("<svg "))
        self.assertIn("<title>", svg)
        self.assertIn('role="img"', svg)
        self.assertIn("viewBox", svg)
        self.assertNotIn(body["secret"], svg)  # drawn as modules, never as text
        security = c.get("/api/auth/security").json()
        self.assertEqual((security["mfa_enabled"], security["mfa_setup_started"]), (False, True))

    def test_the_pending_secret_is_wrapped_with_the_server_key_under_its_own_label(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        secret = c.post("/api/auth/mfa/setup").json()["secret"]
        pending = self.row(alice)["pending"]
        self.assertNotIn(secret.encode(), pending)
        server = users.server_keys()
        self.assertEqual(server.kek_unwrap(pending, f"totp|{alice.id}"), secret.encode())
        with self.assertRaises(crypto.CryptoError):  # tied to this user
            server.kek_unwrap(pending, f"totp|{alice.id + 1}")
        kek_id = server.kek_id_of(pending)
        self.assertEqual(crypto.unwrap(server._sections["kek"]["keys"][kek_id], pending,
                                       f"totp|{alice.id}|{kek_id}"), secret.encode())

    def test_enabling_checks_the_code_and_moves_the_secret(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        self.assertEqual(c.post("/api/auth/mfa/enable", json={"code": "123456"}).json()["code"], "no_setup")
        secret = c.post("/api/auth/mfa/setup").json()["secret"]
        wrong = totp.code_at(secret, self.clock.step + 5)
        r = c.post("/api/auth/mfa/enable", json={"code": wrong})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_code", "code"))
        self.assertFalse(self.row(alice)["mfa"])
        r = c.post("/api/auth/mfa/enable", json={"code": " " + self.code(secret)[:3] + " " + self.code(secret)[3:]})
        self.assertEqual((r.status_code, r.json()["mfa_enabled"], r.json()["recovery_codes_left"]), (200, True, 8))
        row = self.row(alice)
        self.assertTrue(row["mfa"])
        self.assertIsNone(row["pending"])
        self.assertEqual(users.open_totp(alice.id, row["secret"]), secret)
        self.assertEqual(row["last_step"], self.clock.step)  # the code that finished setup can't be used again
        self.assertEqual(c.post("/api/auth/mfa/setup").json()["code"], "mfa_already_on")
        self.assertEqual(c.get("/api/auth/security").json()["mfa_enabled"], True)

    def test_the_secret_is_not_stored_in_plain_text(self):
        alice, secret = self.with_mfa()
        with engine.connect() as conn:
            raw = conn.execute(text("SELECT totp_secret_enc FROM users WHERE id = :i"), {"i": alice.id}).scalar()
        self.assertNotIn(secret.encode(), bytes(raw))

    def test_a_new_setup_replaces_the_pending_secret(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        first = c.post("/api/auth/mfa/setup").json()["secret"]
        second = c.post("/api/auth/mfa/setup").json()["secret"]
        self.assertNotEqual(first, second)
        r = c.post("/api/auth/mfa/enable", json={"code": totp.code_at(first, self.clock.step)})
        self.assertEqual(r.json()["code"], "wrong_code")
        self.assertEqual(c.post("/api/auth/mfa/enable", json={"code": totp.code_at(second, self.clock.step)}
                                ).status_code, 200)


# ---------------------------------------------------------------------------
# Signing in with the code
# ---------------------------------------------------------------------------

class MfaSignInTest(AccountTestCase):
    def test_the_password_alone_opens_nothing(self):
        alice, secret = self.with_mfa()
        c, r = self.login(alice)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "mfa_required": True})  # no CSRF token, no user
        cookie = c.cookies.get("jh_session")
        self.assertTrue(cookie)
        self.assertNotIn("Max-Age", r.headers["set-cookie"])  # even with "keep me signed in", until the code is in
        state = c.get("/api/auth/state").json()
        self.assertEqual({k: state[k] for k in ("setup_required", "signed_in", "mfa_pending")},
                         {"setup_required": False, "signed_in": False, "mfa_pending": True})
        for path in ("/api/auth/me", "/api/workspaces", "/api/pins", "/api/jobs", "/api/auth/security",
                     "/api/account/prefs", "/api/documents"):
            r = c.get(path)
            self.assertEqual((r.status_code, r.json()["code"]), (401, "mfa_required"), path)
        for method, path in (("post", "/api/auth/recovery-codes"), ("post", "/api/auth/reauth"),
                             ("post", "/api/account/delete"), ("post", "/api/auth/mfa/disable"),
                             ("post", "/api/auth/password"), ("post", "/api/auth/sessions/end-others")):
            r = getattr(c, method)(path, json={})
            self.assertEqual((r.status_code, r.json()["code"]), (401, "mfa_required"), path)
        self.assertEqual(c.cookies.get("jh_session"), cookie)  # the sign-in page can go on
        self.assertEqual(c.get("/api/auth/state").json().get("mfa_pending"), True)

    def test_a_pending_session_holds_the_key_where_a_session_cannot_use_it(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        cookie = crypto.b64d(c.cookies.get("jh_session"))
        self.assertIsNone(sessions.load(cookie))  # the middleware's check: nothing to unwrap
        (row,) = self.sessions_of(alice)
        self.assertEqual(row.state, "pending_mfa")
        with self.assertRaises(crypto.CryptoError):  # not the label an active session uses
            crypto.unwrap_session_dek(cookie, row.wrapped_dek)
        # Even if someone flipped the state in the database, the session still wouldn't open the data.
        db = SessionLocal()
        db.get(AuthSession, row.id).state = "active"
        db.commit()
        db.close()
        self.assertIsNone(sessions.load(cookie))
        self.assertEqual(c.get("/api/pins").status_code, 401)
        self.assertEqual(sessions.pending_dek(cookie, row), helpers.key_of(alice.email)[1])  # only after the code

    def test_the_code_finishes_signing_in(self):
        alice, secret = self.with_mfa(name="Alice Example")
        c, _ = self.login(alice, remember=True)
        pending_cookie = c.cookies.get("jh_session")
        r = self.second_step(c, code=self.code(secret))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["user"], {"id": alice.id, "role": "user"})
        self.assertIn("Max-Age", r.headers["set-cookie"])  # "keep me signed in" was remembered
        self.assertNotEqual(c.cookies.get("jh_session"), pending_cookie)  # a new session, not the pending one
        self.assertEqual(c.get("/api/auth/me").json()["user"]["name"], "Alice Example")
        self.assertEqual([s.state for s in self.sessions_of(alice)].count("pending_mfa"), 0)
        self.assertEqual(self.row(alice)["failed"], 0)
        stale = self.anonymous()
        stale.cookies.set("jh_session", pending_cookie)
        self.assertEqual(stale.post("/api/auth/login/mfa", json={"code": self.code(secret, 1)}).json()["code"],
                         "mfa_expired")

    def test_a_code_cannot_be_used_twice(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        code = self.code(secret)
        self.assertEqual(self.second_step(c, code=code).status_code, 200)
        c2, _ = self.login(alice)
        r = self.second_step(c2, code=code)  # the same code again, a moment later
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_code", "code"))
        self.clock.advance()
        self.assertEqual(self.second_step(c2, code=self.code(secret)).status_code, 200)  # the next one is fine

    def test_one_step_either_side_of_now_is_accepted_and_two_are_not(self):
        alice, secret = self.with_mfa()
        for offset, ok in ((-2, False), (2, False), (-1, True)):
            c, _ = self.login(alice)
            r = self.second_step(c, code=self.code(secret, offset))
            self.assertEqual(r.status_code == 200, ok, f"offset {offset}: {r.text}")
        # -1 was used, so the clock must pass it; a code a step ahead is accepted too
        self.clock.advance(2)
        c, _ = self.login(alice)
        self.assertEqual(self.second_step(c, code=self.code(secret, 1)).status_code, 200)
        self.assertEqual(self.row(alice)["last_step"], self.clock.step + 1)
        # ... and then nothing at or before that step works, even though the clock hasn't reached it
        c, _ = self.login(alice)
        self.assertEqual(self.second_step(c, code=self.code(secret, 0)).status_code, 422)
        self.assertEqual(self.second_step(c, code=self.code(secret, -1)).status_code, 422)

    def test_codes_may_have_spaces_and_hyphens(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        code = self.code(secret)
        self.assertEqual(self.second_step(c, code=f"{code[:3]} - {code[3:]}").status_code, 200)

    def test_five_wrong_codes_drop_the_pending_session(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        wrong = self.code(secret, 7)
        for n in range(4):
            r = self.second_step(c, code=wrong)
            self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"), n)
        self.assertEqual(len(self.sessions_of(alice)), 1)
        r = self.second_step(c, code=wrong)  # the fifth
        self.assertEqual((r.status_code, r.json()["code"]), (401, "mfa_expired"))
        self.assertEqual(self.sessions_of(alice), [])
        r = self.second_step(c, code=self.code(secret))  # even the right code is too late
        self.assertEqual((r.status_code, r.json()["code"]), (401, "mfa_expired"))
        self.assertEqual(self.second_step(c, code=self.code(secret)).json()["code"], "mfa_expired")

    def test_wrong_codes_add_up_across_sign_ins_into_a_wait(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        for _ in range(5):
            self.second_step(c, code=self.code(secret, 7))
        self.assertTrue(self.row(alice)["locked"])
        c, r = self.login(alice)  # the password is right, but the account waits
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))

    def test_the_wrong_code_count_comes_before_the_check_so_parallel_guesses_cannot_pass_it(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        (row,) = self.sessions_of(alice)
        db = SessionLocal()
        db.get(AuthSession, row.id).mfa_failures = sessions.MFA_ATTEMPTS  # five tries are already counted
        db.commit()
        db.close()
        r = self.second_step(c, code=self.code(secret))  # the right code, but one try too many
        self.assertEqual((r.status_code, r.json()["code"]), (401, "mfa_expired"))
        self.assertEqual(self.sessions_of(alice), [])

    def test_a_pending_session_expires_after_ten_minutes(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        (row,) = self.sessions_of(alice)
        self.assertLessEqual(row.idle_expires_at - utcnow(), timedelta(minutes=10))
        self.assertLessEqual(row.absolute_expires_at - utcnow(), timedelta(minutes=10))
        db = SessionLocal()
        db.get(AuthSession, row.id).idle_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        db.close()
        self.assertEqual(c.get("/api/auth/state").json().get("mfa_pending"), None)
        self.assertEqual(self.second_step(c, code=self.code(secret)).json()["code"], "mfa_expired")

    def test_what_the_second_step_needs(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        for body in ({}, {"code": "123456", "recovery_code": "AAAA-BBBB-CCCC-DDDD"}, {"code": " "}):
            r = self.second_step(c, **body)
            self.assertEqual((r.status_code, r.json()["code"]), (422, "invalid"), body)
        self.assertEqual(self.second_step(self.anonymous(), code="123456").json()["code"], "mfa_expired")
        form = c.post("/api/auth/login/mfa", data={"code": self.code(secret)})
        self.assertEqual(form.status_code, 415)
        other_site = c.post("/api/auth/login/mfa", json={"code": self.code(secret)},
                            headers={"Origin": "https://evil.example"})
        self.assertEqual((other_site.status_code, other_site.json()["code"]), (403, "bad_origin"))
        self.assertEqual(len(self.sessions_of(alice)), 1)  # still waiting

    def test_signing_out_ends_a_pending_session(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        r = c.post("/api/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sessions_of(alice), [])
        self.assertIsNone(c.get("/api/auth/state").json().get("mfa_pending"))

    def test_a_new_sign_in_replaces_a_pending_session(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        r = c.post("/api/auth/login", json={"email": alice.email, "password": PASSWORD})
        self.assertTrue(r.json()["mfa_required"])
        self.assertEqual(len(self.sessions_of(alice)), 1)

    def test_accounts_without_two_step_sign_in_are_unchanged(self):
        bob = helpers.make_user(password=PASSWORD)
        c, r = self.login(bob)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("mfa_required", r.json())
        self.assertEqual(c.get("/api/auth/me").status_code, 200)

    def test_a_wrong_password_still_gets_no_pending_session(self):
        alice, secret = self.with_mfa()
        c, r = self.login(alice, password="wrong password here")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.sessions_of(alice), [])


class MfaRecoveryCodeAtSignInTest(AccountTestCase):
    def codes_of(self, account: helpers.Account, c) -> list[str]:
        return c.get("/api/auth/recovery-codes").json()["codes"]

    def test_a_recovery_code_works_instead_of_the_app_and_only_once(self):
        alice, secret = self.with_mfa()
        c = self.mfa_client(alice, secret)
        codes = self.codes_of(alice, c)
        self.assertEqual(len(codes), 8)
        pending, _ = self.login(alice)
        r = self.second_step(pending, recovery_code=codes[0].lower().replace("-", " "))
        self.assertEqual((r.status_code, r.json()["recovery_codes_left"]), (200, 7))
        self.assertEqual(pending.get("/api/auth/me").status_code, 200)
        again, _ = self.login(alice)
        r = self.second_step(again, recovery_code=codes[0])
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_code", "recovery_code"))
        r = self.second_step(again, recovery_code=codes[1])
        self.assertEqual((r.status_code, r.json()["recovery_codes_left"]), (200, 6))
        self.assertEqual(c.get("/api/auth/security").json()["recovery_codes_left"], 6)

    def test_recovery_codes_and_authenticator_codes_are_not_mixed_up(self):
        alice, secret = self.with_mfa()
        codes = self.codes_of(alice, self.mfa_client(alice, secret))
        pending, _ = self.login(alice)
        self.assertEqual(self.second_step(pending, code=codes[0]).status_code, 422)
        self.assertEqual(self.second_step(pending, recovery_code=self.code(secret)).status_code, 422)


# ---------------------------------------------------------------------------
# Turning two-step sign-in off
# ---------------------------------------------------------------------------

class MfaDisableTest(AccountTestCase):
    def test_the_secret_does_not_linger_in_the_log(self):
        alice, secret = self.with_mfa()
        db = SessionLocal()
        try:
            blob = bytes(db.get(User, alice.id).totp_secret_enc)
        finally:
            db.close()
        self.assertEqual(self.mfa_client(alice, secret).post(
            "/api/auth/mfa/disable", json={"password": PASSWORD, "code": self.code(secret)}).status_code, 200)
        self.assertEqual(on_disk(blob), [])

    def test_it_takes_the_password_and_a_code(self):
        alice, secret = self.with_mfa()
        c = self.mfa_client(alice, secret)
        r = c.post("/api/auth/mfa/disable", json={"password": "wrong password!", "code": self.code(secret)})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_password", "password"))
        r = c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "code": self.code(secret, 5)})
        self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))
        r = c.post("/api/auth/mfa/disable", json={"password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (422, "invalid"))
        self.assertTrue(self.row(alice)["mfa"])
        r = c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "code": self.code(secret)})
        self.assertEqual((r.status_code, r.json()["mfa_enabled"]), (200, False))
        row = self.row(alice)
        self.assertEqual((row["mfa"], row["secret"], row["pending"], row["last_step"]), (False, None, None, None))
        _, r = self.login(alice)  # straight in
        self.assertNotIn("mfa_required", r.json())
        self.assertEqual(c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "code": "123456"}).json()["code"],
                         "mfa_off")

    def test_a_recovery_code_will_do_and_is_used_up(self):
        alice, secret = self.with_mfa()
        c = self.mfa_client(alice, secret)
        codes = c.get("/api/auth/recovery-codes").json()["codes"]
        r = c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "recovery_code": codes[3]})
        self.assertEqual((r.status_code, r.json()["recovery_codes_left"]), (200, 7))

    def test_a_replayed_code_does_not_turn_it_off(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        code = self.code(secret)
        self.assertEqual(self.second_step(c, code=code).status_code, 200)
        r = c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "code": code})
        self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))
        self.assertTrue(self.row(alice)["mfa"])


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------

class RecoveryCodesTest(AccountTestCase):
    def test_every_code_resets_the_password_once_and_keeps_the_data(self):
        alice = helpers.make_user(password=PASSWORD)
        self.write_pin(alice, "Alice's home MARKER")
        codes = self.signed_in(alice).get("/api/auth/recovery-codes").json()["codes"]
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
        code = self.signed_in(alice).get("/api/auth/recovery-codes").json()["codes"][0]
        r = self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": code,
                                                            "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200)
        db = SessionLocal()
        try:
            user = users.find_by_email(db, alice.email)
            self.assertEqual(users.unlock_with_password(db, user, NEW_PASSWORD), helpers.key_of(alice.email)[1])
            self.assertIsNone(users.unlock_with_password(db, user, PASSWORD))
        finally:
            db.close()

    def test_other_sessions_end_and_the_person_is_signed_in(self):
        alice = helpers.make_user(password=PASSWORD)
        old = self.signed_in(alice)
        code = old.get("/api/auth/recovery-codes").json()["codes"][0]
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
        good = self.signed_in(alice).get("/api/auth/recovery-codes").json()["codes"][0]
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
        good = self.signed_in(alice).get("/api/auth/recovery-codes").json()["codes"][0]
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
            ratelimit.reset_all()
            c.post("/api/auth/recover", json={"email": alice.email, "recovery_code": "AAAA-BBBB-CCCC-DDDD",
                                              "new_password": NEW_PASSWORD})
        ratelimit.reset_all()
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

    def test_with_two_step_sign_in_a_second_proof_is_needed(self):
        alice, secret = self.with_mfa()
        self.write_pin(alice, "MFA pin MARKER")
        codes = self.mfa_client(alice, secret).get("/api/auth/recovery-codes").json()["codes"]
        body = {"email": alice.email, "recovery_code": codes[0], "new_password": NEW_PASSWORD}
        c = self.anonymous()
        r = c.post("/api/auth/recover", json=body)
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "second_factor_required", "code"))
        r = self.anonymous().post("/api/auth/recover", json={**body, "code": self.code(secret, 6)})
        self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))
        r = self.anonymous().post("/api/auth/recover", json={**body, "second_recovery_code": codes[0]})  # the same one
        self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))
        self.assertEqual(self.login(alice)[1].status_code, 200)  # nothing was used up or changed
        r = self.anonymous().post("/api/auth/recover", json={**body, "code": self.code(secret)})
        self.assertEqual((r.status_code, r.json()["recovery_codes_left"]), (200, 7))
        c = self.anonymous()
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        _, again = self.login(alice, NEW_PASSWORD)
        self.assertTrue(again.json()["mfa_required"])  # two-step sign-in is still on

    def test_two_recovery_codes_do_it_without_the_app_and_use_up_both(self):
        alice, secret = self.with_mfa()
        self.write_pin(alice, "Second code MARKER")
        codes = self.mfa_client(alice, secret).get("/api/auth/recovery-codes").json()["codes"]
        r = self.anonymous().post("/api/auth/recover", json={
            "email": alice.email, "recovery_code": codes[0], "second_recovery_code": codes[1],
            "new_password": NEW_PASSWORD})
        self.assertEqual((r.status_code, r.json()["recovery_codes_left"]), (200, 6))
        c, login = self.login(alice, NEW_PASSWORD)
        self.assertTrue(login.json()["mfa_required"])
        self.assertEqual(self.second_step(c, recovery_code=codes[2]).status_code, 200)
        self.assertEqual(self.pin_labels(c), ["Second code MARKER"])

    def test_a_replayed_authenticator_code_does_not_help_a_reset(self):
        alice, secret = self.with_mfa()
        codes = self.mfa_client(alice, secret).get("/api/auth/recovery-codes").json()["codes"]
        c, _ = self.login(alice)
        code = self.code(secret)
        self.assertEqual(self.second_step(c, code=code).status_code, 200)
        r = self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": codes[0],
                                                            "new_password": NEW_PASSWORD, "code": code})
        self.assertEqual(r.status_code, 422)

    def test_a_disabled_account_cannot_reset(self):
        alice = helpers.make_user(password=PASSWORD)
        code = self.signed_in(alice).get("/api/auth/recovery-codes").json()["codes"][0]
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
        old = c.get("/api/auth/recovery-codes").json()["codes"]
        c.post("/api/auth/recovery-codes/ack")
        self.assertEqual(c.get("/api/auth/recovery-codes").status_code, 404)
        self.assertFalse(c.get("/api/auth/security").json()["recovery_codes_pending"])
        r = c.post("/api/auth/recovery-codes")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "recovery_codes_left": 8,
                                                           "recovery_codes_pending": True}))
        self.assertTrue(c.get("/api/auth/me").json()["recovery_codes_pending"])
        new = c.get("/api/auth/recovery-codes").json()["codes"]
        self.assertEqual(len(new), 8)
        self.assertFalse(set(old) & set(new))
        for code in old[:2]:  # the old ones are dead
            r = self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": code,
                                                                "new_password": NEW_PASSWORD})
            self.assertEqual(r.status_code, 401)
        self.assertEqual(c.post("/api/auth/recovery-codes/ack").status_code, 200)
        self.assertEqual(c.get("/api/auth/recovery-codes").status_code, 404)  # shown once
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
        code = c.get("/api/auth/recovery-codes").json()["codes"][0]
        self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": code,
                                                         "new_password": NEW_PASSWORD})
        c = helpers.client(alice.email, NEW_PASSWORD)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        self.assertEqual(c.get("/api/auth/security").json()["recovery_codes_left"], 7)
        c.post("/api/auth/recovery-codes")
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
            user = users.find_by_email(db, alice.email)
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
        self.assertEqual(c.post("/api/auth/mfa/setup").status_code, 403)
        c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        self.assertEqual(c.post("/api/auth/mfa/setup").status_code, 200)

    def test_two_step_sign_in_stays_on(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        self.assertEqual(self.second_step(c, code=self.code(secret)).status_code, 200)
        r = c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.row(alice)["mfa"])


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

    def test_a_session_waiting_for_its_code_ends_too(self):
        alice, secret = self.with_mfa()
        c = self.mfa_client(alice, secret)
        waiting, _ = self.login(alice)
        self.assertEqual(sorted(s.state for s in self.sessions_of(alice)), ["active", "pending_mfa"])
        c.post("/api/auth/sessions/end-others")
        self.assertEqual([s.state for s in self.sessions_of(alice)], ["active"])
        self.assertEqual(self.second_step(waiting, code=self.code(secret)).json()["code"], "mfa_expired")


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
ACCOUNT_TABLES = ("sessions", "recovery_codes", "auth_tokens", "admin_key_grants", "memberships",
                  "membership_grants", "email_prefs", "user_keys", "commute_cache")


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
                    thread = ChatThread(workspace_id=ws, job_ids_json=f"[{first.id}]", title=f"{tag} chat")
                    db.add(thread)
                    db.flush()
                    db.add(ChatMessage(thread_id=thread.id, role="user", content=f"{tag} question"))
                db.add(CommuteCache(key=f"{tag}-{account.id}", owner_id=account.id, result_json="{}"))
                now = utcnow()
                db.add_all([  # (the account is also the other end of these, so they are only emptied, not removed)
                    AuthToken(kind="reset", token_hash=os.urandom(32), user_id=account.id, expires_at=now + timedelta(days=1)),
                    AuthToken(kind="invite", token_hash=os.urandom(32), created_by=account.id,
                              expires_at=now + timedelta(days=1)),
                    MembershipGrant(user_id=account.id, kind="manual", starts_at=now, ends_at=now + timedelta(days=3),
                                    created_by=account.id),
                    AdminAudit(admin_id=account.id, action=f"{tag}_by"), AdminAudit(target_user_id=account.id, action=f"{tag}_on"),
                ])
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
                column = "owner_id" if table == "commute_cache" else "user_id"
                out[table] = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :u"), {"u": user_id}).scalar()
            for name, where in (("usage_of_user", "SELECT COUNT(*) FROM llm_usage WHERE user_id = :u"),
                                ("audit_entries_naming_user", "SELECT COUNT(*) FROM admin_audit WHERE admin_id = :u "
                                                              "OR target_user_id = :u"),
                                ("tokens_made_by_user", "SELECT COUNT(*) FROM auth_tokens WHERE created_by = :u"),
                                ("grants_made_by_user", "SELECT COUNT(*) FROM admin_key_grants WHERE granted_by = :u"),
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
        empty = {"admin_key_grants", "grants_made_by_user"}  # (only admins have these)
        self.assertTrue(all(v >= 1 for k, v in before_bob.items() if k not in empty), before_bob)
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

    def test_the_last_admin_cannot_delete_their_own_account(self):
        admin = self.signed_in(helpers.Account(helpers.admin_id(), helpers.ADMIN_EMAIL, helpers.ADMIN_PASSWORD, 0))
        r = admin.post("/api/account/delete", json={"password": helpers.ADMIN_PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "admin_cannot_delete"))
        # The only admin has nobody to ask, so they are told what to do instead.
        self.assertIn("only admin", r.json()["detail"])
        self.assertEqual(admin.post("/api/account/delete", json={"password": "wrong"}).status_code, 409)  # no oracle
        self.assertEqual(admin.get("/api/auth/me").status_code, 200)
        db = SessionLocal()
        try:
            with self.assertRaises(users.AccountProtected):
                users.delete_account(db, helpers.admin_id())
            self.assertIsNotNone(db.get(User, helpers.admin_id()))
        finally:
            db.close()

    def make_admin(self, tag: str = "") -> helpers.Account:
        """A new admin, promoted by the first (so they hold the admin details key), demoted again when the test ends."""
        account = helpers.make_user(password=PASSWORD, name=f"Second Admin {tag}")
        with helpers.client() as first:
            self.assertEqual(first.post(f"/api/admin/users/{account.id}/role", json={"role": "admin"}).status_code, 200)
        self.addCleanup(self.demote_quietly, account.id)
        return account

    def demote_quietly(self, user_id: int) -> None:
        with helpers.client() as first:
            first.post(f"/api/admin/users/{user_id}/role", json={"role": "user"})

    def apk_version(self) -> int:
        db = SessionLocal()
        try:
            return admin_key.current_public(db)[1]
        finally:
            db.close()

    def test_an_admin_deletes_their_account_while_another_admin_remains_and_that_admin_reads_everything(self):
        bob = helpers.make_user(password=PASSWORD, name="Bob Readable")
        second = self.make_admin("deleter")
        mine = self.populate(second, "admintag")
        version = self.apk_version()
        c = self.signed_in(second)
        r = c.post("/api/account/delete", json={"password": PASSWORD})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}), r.text)
        self.assertEqual({k: v for k, v in self.counts(mine["ws"], second.id).items() if v}, {})
        self.assertFalse(any((UPLOAD_DIR / f).exists() for f in mine["files"]))
        self.assertEqual(self.apk_version(), version + 1)  # the key they held was replaced
        db = SessionLocal()
        try:
            self.assertEqual(sorted(g.apk_version for g in db.query(AdminKeyGrant)), [version + 1])  # the first admin's
            self.assertEqual(db.query(AdminKeyGrant).one().user_id, helpers.admin_id())
        finally:
            db.close()
        with helpers.client() as first:  # still reads every detail, old and new
            listed = {u["id"]: u for u in first.get("/api/admin/users").json()["users"]}
            self.assertNotIn(second.id, listed)
            self.assertEqual((listed[bob.id]["email"], listed[bob.id]["name"]), (bob.email, "Bob Readable"))
            self.assertEqual(listed[helpers.admin_id()]["email"], helpers.ADMIN_EMAIL)
            entry = next(e for e in first.get("/api/admin/audit").json()["entries"] if e["action"] == "user_deleted"
                         and e["detail"] and e["detail"].get("apk_version") == version + 1)
            self.assertEqual((entry["admin"], entry["detail"]["role"]), (None, "admin"))
            self.assertNotIn(second.email, json.dumps(entry))
        self.assertEqual(self.login(second)[1].status_code, 401)

    def test_an_admin_who_cannot_open_the_admin_key_is_told_to_ask_another_admin(self):
        first = helpers.Account(helpers.admin_id(), helpers.ADMIN_EMAIL, helpers.ADMIN_PASSWORD, 0)
        keyless = helpers.make_user(password=PASSWORD, role="admin")  # an admin nobody has shared the key with
        self.addCleanup(self.demote_quietly, keyless.id)
        c = self.signed_in(keyless)
        r = c.post("/api/account/delete", json={"password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "admin_key_missing"), r.text)
        self.assertIn("Ask another admin", r.json()["detail"])
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        with helpers.client(first.email) as admin_client:  # the admin panel deletes them with its own key
            self.assertEqual(admin_client.delete(f"/api/admin/users/{keyless.id}").status_code, 200)

    # -- one routine behind both endpoints -----------------------------------------------------------------------

    def delete_through(self, how: str, victim: helpers.Account):
        """Delete ``victim`` as they would themselves (``self``) or as the admin panel does (``admin``)."""
        if how == "self":
            return self.signed_in(victim).post("/api/account/delete", json={"password": PASSWORD})
        with helpers.client() as admin:
            return admin.delete(f"/api/admin/users/{victim.id}")

    def test_both_endpoints_remove_the_same_rows_and_files_and_leave_other_people_alone(self):
        carol = helpers.make_user(password=PASSWORD)
        theirs = self.populate(carol, "caroltag")
        before_carol = self.counts(theirs["ws"], carol.id)
        removed = {}
        for how in ("self", "admin"):
            bob = helpers.make_user(password=PASSWORD)
            mine = self.populate(bob, f"{how}tag")
            self.signed_in(bob)  # a session to remove
            before = self.counts(mine["ws"], bob.id)
            self.assertTrue(all(v >= 1 for k, v in before.items() if k not in ("admin_key_grants", "grants_made_by_user")),
                            before)
            self.assertTrue(all((UPLOAD_DIR / f).is_file() for f in mine["files"]))
            r = self.delete_through(how, bob)
            self.assertEqual(r.status_code, 200, r.text)
            removed[how] = self.counts(mine["ws"], bob.id)
            self.assertEqual({k: v for k, v in removed[how].items() if v}, {}, how)
            self.assertFalse(any((UPLOAD_DIR / f).exists() for f in mine["files"]), how)
            self.assertEqual(self.counts(theirs["ws"], carol.id), before_carol, how)
            self.assertTrue(all((UPLOAD_DIR / f).is_file() for f in theirs["files"]), how)
        self.assertEqual(removed["self"], removed["admin"])
        self.assertEqual(self.login(carol)[1].status_code, 200)

    def test_a_failure_midway_leaves_the_account_whole_whichever_endpoint_was_used(self):
        for how in ("self", "admin"):
            bob = helpers.make_user(password=PASSWORD)
            mine = self.populate(bob, f"{how}fail")
            before = self.counts(mine["ws"], bob.id)  # (sessions come and go with the clients: left out below)
            with mock.patch.object(migrate_encryption, "owner_of", side_effect=RuntimeError("boom")):  # (after many deletes)
                with self.assertRaises(RuntimeError):
                    self.delete_through(how, bob)
            self.assertEqual(self.counts(mine["ws"], bob.id) | {"sessions": 0}, before | {"sessions": 0}, how)
            self.assertTrue(all((UPLOAD_DIR / f).is_file() for f in mine["files"]), how)
            self.assertEqual(self.delete_through(how, bob).status_code, 200, how)  # the lock was let go: it works now
            self.assertEqual(self.counts(mine["ws"], bob.id)["users"], 0)

    def test_a_failure_while_replacing_the_key_leaves_the_admin_account_whole(self):
        second = self.make_admin("fails")
        mine = self.populate(second, "keyfail")
        before, version = self.counts(mine["ws"], second.id), self.apk_version()
        c = self.signed_in(second)
        with mock.patch.object(admin_key, "new_key", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                c.post("/api/account/delete", json={"password": PASSWORD})
        self.assertEqual(self.counts(mine["ws"], second.id) | {"sessions": 0}, before | {"sessions": 0})
        self.assertEqual(self.apk_version(), version)
        self.assertTrue(all((UPLOAD_DIR / f).is_file() for f in mine["files"]))
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        self.assertEqual(c.post("/api/account/delete", json={"password": PASSWORD}).status_code, 200)

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
        mine = self.populate(bob, "admin-path")
        db = SessionLocal()
        try:
            self.assertFalse(crypto.has_scope())
            result = users.delete_account(db, bob.id)  # as the admin panel will: no key scope of the user's
        finally:
            db.close()
        self.assertEqual(result, {"workspaces": 2, "files": 2, "apk_rotated": False})
        self.assertEqual({k: v for k, v in self.counts(mine["ws"], bob.id).items() if v}, {})

    def test_deleting_the_account_ends_every_session_and_pending_sign_in(self):
        bob, secret = self.with_mfa()
        c, _ = self.login(bob)
        self.assertEqual(self.second_step(c, code=self.code(secret)).status_code, 200)
        other, _ = self.login(bob)  # waiting for a code
        self.assertEqual(len(self.sessions_of(bob)), 2)
        self.assertEqual(c.post("/api/account/delete", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(self.sessions_of(bob), [])
        self.assertEqual(other.get("/api/auth/state").json().get("mfa_pending"), None)


# ---------------------------------------------------------------------------
# What the logs and answers must not hold
# ---------------------------------------------------------------------------

class NoSecretsInLogsTest(AccountTestCase):
    def test_nothing_secret_reaches_the_log(self):
        alice = helpers.make_user(password=PASSWORD, name="Alice Loggable")
        with self.assertLogs(level="DEBUG") as logged:
            c = self.signed_in(alice)
            setup = c.post("/api/auth/mfa/setup").json()
            secret = setup["secret"]
            c.post("/api/auth/mfa/enable", json={"code": self.code(secret)})
            codes = c.get("/api/auth/recovery-codes").json()["codes"]
            self.clock.advance()
            pending, _ = self.login(alice)
            self.second_step(pending, code="000000")
            self.second_step(pending, code=self.code(secret))
            pending.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
            self.anonymous().post("/api/auth/recover", json={"email": alice.email, "recovery_code": codes[0],
                                                             "new_password": "another good password",
                                                             "code": self.code(secret, 1)})
            pending.post("/api/account/delete", json={"password": NEW_PASSWORD})
        text_out = "\n".join(logged.output)
        for secret_text in (secret, setup["secret_groups"], alice.email, "Alice Loggable", PASSWORD, NEW_PASSWORD,
                            "another good password", codes[0], codes[1], codes[0].replace("-", "")):
            self.assertNotIn(secret_text, text_out)


# ---------------------------------------------------------------------------
# Review fixes: what a stolen session (or a stolen session plus the password) can and can't do
# ---------------------------------------------------------------------------

class StolenSessionTest(AccountTestCase):
    def test_unsaved_recovery_codes_are_only_shown_after_a_recent_password(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        self.make_stale(c)
        # The codes can reset the password, so a session that hasn't entered it lately can't read them.
        r = c.get("/api/auth/recovery-codes")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        self.assertNotIn("codes", r.json())
        self.assertEqual(c.post("/api/auth/reauth", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(len(c.get("/api/auth/recovery-codes").json()["codes"]), 8)
        # Right after signing in they are shown at once (the sign-up and sign-in sessions start out recent).
        fresh, _ = self.login(alice)
        self.assertEqual(fresh.get("/api/auth/recovery-codes").status_code, 200)

    def test_the_password_alone_cannot_make_codes_to_switch_two_step_off(self):
        alice, secret = self.with_mfa()
        c = self.mfa_client(alice, secret)
        before = c.get("/api/auth/recovery-codes").json()["codes"]
        self.make_stale(c)
        self.assertEqual(c.post("/api/auth/reauth", json={"password": PASSWORD}).status_code, 200)
        r = c.post("/api/auth/recovery-codes")
        self.assertEqual((r.status_code, r.json()["code"]), (422, "invalid"))
        r = c.post("/api/auth/recovery-codes", json={"code": self.code(secret, 5)})
        self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))
        r = c.post("/api/auth/recovery-codes", json={"recovery_code": "AAAA-BBBB-CCCC-DDDD"})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_code", "recovery_code"))
        self.assertEqual(c.get("/api/auth/recovery-codes").json()["codes"], before)  # nothing was replaced
        self.assertTrue(self.row(alice)["mfa"])
        r = c.post("/api/auth/recovery-codes", json={"code": self.code(secret)})
        self.assertEqual((r.status_code, r.json()["recovery_codes_left"]), (200, 8))
        self.assertEqual(self.row(alice)["failed"], 0)  # a right code clears the wrong tries
        new = c.get("/api/auth/recovery-codes").json()["codes"]
        self.assertFalse(set(before) & set(new))

    def test_a_recovery_code_will_do_for_new_codes_too_and_wrong_ones_add_up_to_a_wait(self):
        alice, secret = self.with_mfa()
        c = self.mfa_client(alice, secret)
        old = c.get("/api/auth/recovery-codes").json()["codes"]
        r = c.post("/api/auth/recovery-codes", json={"recovery_code": old[0]})
        self.assertEqual(r.status_code, 200, r.text)
        new = c.get("/api/auth/recovery-codes").json()["codes"]
        self.assertFalse(set(old) & set(new))
        for n in range(1, 6):
            r = c.post("/api/auth/recovery-codes", json={"code": self.code(secret, 5 + n)})
            self.assertEqual(r.status_code, 422)
            self.assertEqual(self.row(alice)["failed"], n)
        r = c.post("/api/auth/recovery-codes", json={"code": self.code(secret)})  # even a right one waits now
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        self.assertIn("Retry-After", r.headers)
        self.assertIn("codes", r.json()["detail"])
        self.assertEqual(c.get("/api/auth/recovery-codes").json()["codes"], new)

    def test_without_two_step_new_codes_need_no_code(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        self.assertEqual(c.post("/api/auth/recovery-codes").status_code, 200)
        self.assertEqual(c.post("/api/auth/recovery-codes", json={}).status_code, 200)

    def test_wrong_codes_when_turning_two_step_off_add_up_to_the_wait(self):
        alice, secret = self.with_mfa()
        c = self.mfa_client(alice, secret)
        for n in range(1, 6):
            r = c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "code": self.code(secret, 4 + n)})
            self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))
            self.assertEqual(self.row(alice)["failed"], n, "the right password must not clear the count")
        self.assertIsNotNone(self.row(alice)["locked"])
        r = c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "code": self.code(secret)})
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        self.assertIn("codes", r.json()["detail"])
        self.assertTrue(self.row(alice)["mfa"])

    def test_a_right_code_clears_the_count_when_turning_two_step_off(self):
        alice, secret = self.with_mfa()
        c = self.mfa_client(alice, secret)
        for n in range(3):
            c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "code": self.code(secret, 5 + n)})
        self.assertEqual(self.row(alice)["failed"], 3)
        r = c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "code": self.code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        row = self.row(alice)
        self.assertEqual((row["failed"], row["locked"], row["mfa"]), (0, None, False))

    def test_a_wrong_password_still_counts_and_a_right_one_still_clears_elsewhere(self):
        alice = helpers.make_user(password=PASSWORD)
        c = self.signed_in(alice)
        c.post("/api/auth/reauth", json={"password": "wrong wrong wrong"})
        self.assertEqual(self.row(alice)["failed"], 1)
        self.assertEqual(c.post("/api/auth/reauth", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(self.row(alice)["failed"], 0)

    def test_the_wait_at_the_code_step_talks_about_codes_not_passwords(self):
        alice, secret = self.with_mfa()
        c, _ = self.login(alice)
        db = SessionLocal()
        try:
            row = db.get(User, alice.id)
            row.failed_logins, row.locked_until = 5, utcnow() + timedelta(minutes=5)
            db.commit()
        finally:
            db.close()
        r = self.second_step(c, code=self.code(secret))
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        self.assertIn("wrong codes", r.json()["detail"])
        self.assertNotIn("passwords", r.json()["detail"])
        # At the password step it is still about passwords.
        r = self.login(alice)[1]
        self.assertEqual(r.status_code, 429)
        self.assertIn("wrong passwords", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
