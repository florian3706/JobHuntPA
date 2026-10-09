"""The identity key (IK): the admin details key is separate from background access (accounts design §1.2, §1.4, §2.5).

The IK protects only the X25519 private key, which opens an admin's grant of the admin details key (APK). It is
wrapped under the password, each recovery code and each session's cookie, never under the server KEK. So someone with
the database, the key file and the server KEK (which opens a background-access account's data key) still can't open
the APK; an admin who is signed in can.

Everything here runs through the app's own endpoints on the shared test database, or in a throwaway process
(``helpers.scenario``) for the command-line tool.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402

from backend import admin_key, crypto, ratelimit, tasks, totp, users  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import (  # noqa: E402
    DB_PATH,
    AdminAudit,
    AdminKeyGrant,
    AuthSession,
    RecoveryCode,
    SessionLocal,
    User,
    UserKeys,
)

PASSWORD = "user-password-123"
NEW_PASSWORD = "a brand new password 77"


def tearDownModule():
    helpers.remove_scratch_folders()


def rows(fn):
    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


def raw_database_bytes() -> bytes:
    """The database file and its log, as someone with a copy of data/ would read them."""
    out = b""
    for suffix in ("", "-wal"):
        path = str(DB_PATH) + suffix
        if os.path.exists(path):
            with open(path, "rb") as fh:
                out += fh.read()
    return out


class Case(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)

    # -- clients ---------------------------------------------------------------------------------------------------

    def signed_in(self, account=None, password=None) -> helpers.SignedInClient:
        c = helpers.client(account.email, password) if account else helpers.client()
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def anonymous(self) -> TestClient:
        c = TestClient(app)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def login(self, email: str, password: str) -> TestClient:
        ratelimit.AUTH.reset()
        c = self.anonymous()
        r = c.post("/api/auth/login", json={"email": email, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        if "csrf" in r.json():
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
        return c

    # -- accounts --------------------------------------------------------------------------------------------------

    def make_admin(self, password: str = PASSWORD) -> helpers.Account:
        """A new admin (promoted by the first admin, who holds the key), demoted again when the test ends."""
        account = helpers.make_user(password=password)
        self.assertEqual(self.signed_in().post(f"/api/admin/users/{account.id}/role", json={"role": "admin"})
                         .status_code, 200)
        self.addCleanup(self.demote, account.id)
        return account

    def demote(self, user_id: int) -> None:
        with helpers.client() as first:
            first.post(f"/api/admin/users/{user_id}/role", json={"role": "user"})

    def sees_details(self, client) -> bool:
        r = client.get("/api/admin/users")
        self.assertEqual(r.status_code, 200, r.text)
        return bool(r.json()["admin_key_available"])

    def assert_identity(self, email: str, password: str, codes=()) -> bytes:
        """The password opens the IK, which opens the account's private key (the one its public key belongs to), and
        each of ``codes`` opens the same IK. Returns the IK."""
        db = SessionLocal()
        try:
            user = users.find_by_email(db, email)
            unlocked = users.unlock(db, user, password)
            self.assertIsNotNone(unlocked)
            self.assertIsNotNone(unlocked.ik, "the account has an identity key")
            row = db.get(UserKeys, user.id)
            priv = users.open_private_key(row, user.id, unlocked.ik)
            self.assertEqual(crypto.public_key_of(priv), bytes(row.x25519_pub))
            for code in codes:
                opened = users.open_recovery_code(db, user.id, code)
                self.assertIsNotNone(opened, "the code is good")
                self.assertEqual(opened[1], unlocked.dek)
                self.assertEqual(opened[2], unlocked.ik)
            return unlocked.ik
        finally:
            db.close()

    def assert_session_has_ik(self, client) -> None:
        sid = crypto.session_id(crypto.b64d(client.cookies.get("jh_session")))
        self.assertIsNotNone(rows(lambda s: s.get(AuthSession, sid).wrapped_ik))

    def make_legacy(self, account: helpers.Account, *, keep_sessions: bool = False) -> None:
        """Puts an account back the way it was before the identity key: the private key and the unconfirmed codes sealed
        under the data key, no IK anywhere (not under the password, the codes or a session). Its sessions end, or with
        ``keep_sessions`` stay, as sessions made before the IK."""
        uid, dek = helpers.key_of(account.email)
        ik = helpers.ik_of(account.email)
        db = SessionLocal()
        try:
            row = db.get(UserKeys, uid)
            priv = users.open_private_key(row, uid, ik)
            pending = users.pending_recovery_codes(db, uid, ik)
            with crypto.as_user(uid, dek):
                row.x25519_priv_enc = crypto.seal(priv, f"user_keys.x25519_priv|{uid}")
                row.pending_codes_enc = (crypto.seal(json.dumps(pending).encode("ascii"), users._codes_label(uid))
                                         if pending else None)
            row.pw_wrapped_ik = None
            db.query(RecoveryCode).filter(RecoveryCode.user_id == uid).update({RecoveryCode.wrapped_ik: None})
            if keep_sessions:
                db.query(AuthSession).filter(AuthSession.user_id == uid).update({AuthSession.wrapped_ik: None})
            else:
                db.query(AuthSession).filter(AuthSession.user_id == uid).delete()
            db.commit()
        finally:
            db.close()
        helpers._iks.pop(account.email, None)  # (it is made again at the next sign-in)


# ---------------------------------------------------------------------------
# The attack is gone
# ---------------------------------------------------------------------------

class ServerKeyTest(Case):
    def test_the_server_key_and_the_database_do_not_open_the_admin_key(self):
        """A copy of data/ (the database and server-keys.json) is enough to open a background-access account's data key.
        It must not be enough to open the admin details key."""
        uid, dek = helpers.key_of()
        ik = helpers.ik_of()
        server = users.server_keys()
        db = SessionLocal()
        try:
            row = db.get(UserKeys, uid)
            self.assertIsNotNone(row.server_wrapped_dek, "the first admin has background access")
            stolen = server.kek_unwrap(bytes(row.server_wrapped_dek), f"srv|{uid}")  # all the operator needs
            self.assertEqual(stolen, dek)
            # The old route: the private key sealed under the data key.
            with crypto.as_user(uid, stolen):
                with self.assertRaises(crypto.CryptoError):
                    crypto.unseal(row.x25519_priv_enc, f"user_keys.x25519_priv|{uid}")
            for key in (stolen, os.urandom(32)):  # the data key as a wrapping key, and a guess
                with self.assertRaises(crypto.CryptoError):
                    users.open_private_key(row, uid, key)
            self.assertEqual(users.open_private_key(row, uid, ik) is not None, True)  # (the IK does)
            grant = db.get(AdminKeyGrant, uid)
            self.assertIsNotNone(grant)
            # What a background run can do: act with the data key. The chain to the admin key needs the IK.
            with crypto.as_user(uid, stolen):
                with self.assertRaises(crypto.NoIdentityKey):
                    crypto.open_ik()
                with self.assertRaises(admin_key.ApkUnavailable) as caught:
                    admin_key.open_apk(db, uid)
                self.assertTrue(caught.exception.sign_in_again)
        finally:
            db.close()

    def test_unconfirmed_recovery_codes_are_not_under_the_data_key(self):
        """A code opens the IK, so while a background-access account has codes it hasn't confirmed saving, the data key
        (which the server key opens) must not hold them."""
        bob = helpers.make_user(password=PASSWORD)
        with helpers.client(bob.email) as c:
            self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)
        db = SessionLocal()
        try:
            row = db.get(UserKeys, bob.id)
            self.assertIsNotNone(row.pending_codes_enc)
            stolen = users.server_keys().kek_unwrap(bytes(row.server_wrapped_dek), f"srv|{bob.id}")
            with crypto.as_user(bob.id, stolen):
                with self.assertRaises(crypto.CryptoError):
                    crypto.unseal(row.pending_codes_enc, users._codes_label(bob.id))
                with self.assertRaises(crypto.CryptoError):  # and the helper doesn't fall back to a guess
                    users.pending_recovery_codes(db, bob.id)
            self.assertEqual(users.pending_recovery_codes(db, bob.id, helpers.ik_of(bob.email)),
                             helpers.pending_codes(bob))
        finally:
            db.close()

    def test_nothing_in_the_database_wraps_an_identity_key_under_the_server_key(self):
        """Every blob in the database that the server KEK opens is the data key of a background-access account or a
        two-step secret. None of them is, or holds, an identity key."""
        admin = helpers.ADMIN_EMAIL
        uid = helpers.key_of(admin)[0]
        iks = {helpers.ik_of(admin)}
        bob = helpers.make_user(password=PASSWORD)
        iks.add(helpers.ik_of(bob.email))
        with helpers.client(bob.email) as c:  # a background-access account and a signed-in session
            self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)
        server = users.server_keys()
        found: dict[tuple[str, str], list[bytes]] = {}
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                for col in [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]:
                    for (value,) in con.execute(f'SELECT "{col}" FROM "{table}" WHERE typeof("{col}") = \'blob\''):
                        if server.kek_id_of(bytes(value)) is not None:
                            found.setdefault((table, col), []).append(bytes(value))
        finally:
            con.close()
        self.assertIn(("user_keys", "server_wrapped_dek"), found)
        allowed = {("user_keys", "server_wrapped_dek"), ("users", "totp_secret_enc"), ("users", "totp_pending_enc")}
        self.assertLessEqual(set(found), allowed, "only these columns are ever wrapped under the server key")
        opened = {}
        for owner, blob in rows(lambda s: [(i, bytes(b)) for i, b in s.query(UserKeys.user_id, UserKeys.server_wrapped_dek)
                                           .filter(UserKeys.server_wrapped_dek.isnot(None))]):
            try:
                opened[owner] = server.kek_unwrap(blob, f"srv|{owner}")  # each is the data key of its account
            except crypto.CryptoError:
                continue  # (a blob other tests planted or moved between rows)
        self.assertIn(uid, opened)
        self.assertIn(bob.id, opened)
        for plain in opened.values():
            self.assertEqual(len(plain), crypto.KEY_LEN)
            self.assertNotIn(plain, iks)
        # The identity keys themselves are in no file, in the clear.
        raw = raw_database_bytes()
        for key in iks:
            self.assertNotIn(key, raw)
        priv = rows(lambda s: users.open_private_key(s.get(UserKeys, uid), uid, helpers.ik_of(admin)))
        self.assertNotIn(priv, raw)
        # ... and no column of the admin's rows or sessions opens as an identity key under the server key.
        row = rows(lambda s: s.get(UserKeys, uid))
        blobs = [row.pw_wrapped_ik, row.x25519_priv_enc, row.pending_codes_enc]
        blobs += [r.wrapped_ik for r in rows(lambda s: s.query(RecoveryCode).filter(RecoveryCode.user_id == uid).all())]
        blobs += [r.wrapped_ik for r in rows(lambda s: s.query(AuthSession).filter(AuthSession.user_id == uid).all())]
        for blob in filter(None, blobs):
            self.assertIsNone(server.kek_id_of(bytes(blob)), "not wrapped under any server key")

    def test_an_admin_who_is_signed_in_still_reads_every_detail(self):
        bob = helpers.make_user(password=PASSWORD, name="Bob Reader")
        c = self.signed_in()
        r = c.get("/api/admin/users")
        self.assertTrue(r.json()["admin_key_available"])
        row = next(u for u in r.json()["users"] if u["id"] == bob.id)
        self.assertEqual((row["email"], row["name"]), (bob.email, "Bob Reader"))
        self.assert_session_has_ik(c)

    def test_a_background_run_cannot_open_the_admin_key_but_the_request_can(self):
        uid, dek = helpers.key_of()
        db = SessionLocal()
        try:
            with crypto.as_user(uid, dek):  # what scheduled work runs with
                with self.assertRaises(admin_key.ApkUnavailable):
                    admin_key.open_apk(db, uid)
            with crypto.as_user(uid, dek, lambda: helpers.ik_of()):  # a signed-in request
                self.assertEqual(admin_key.open_apk(db, uid).version, admin_key.current_public(db)[1])
            other = helpers.make_user(password=PASSWORD)
            with crypto.as_user(other.id, helpers.key_of(other.email)[1], lambda: helpers.ik_of(other.email)):
                with self.assertRaises(admin_key.ApkUnavailable):  # someone else's request can't use the admin's grant
                    admin_key.open_apk(db, uid)
        finally:
            db.close()

    def test_a_queued_run_does_not_inherit_the_requests_identity_key_opener(self):
        uid, dek = helpers.key_of()
        ws = helpers.new_workspace("IK queued run", owner=uid)
        seen = {}

        def job(progress):
            seen.update(scope=crypto.has_scope(), ik=crypto.has_ik(), user=crypto.current().user_id)
            return {}

        with crypto.as_user(uid, dek, lambda: b"x" * 32):  # a signed-in request
            tasks.start("search", job, ws)
            self.assertTrue(crypto.has_ik(), "the request keeps its own opener")
        for _ in range(400):  # (the single worker may be finishing another test's run first)
            if seen:
                break
            time.sleep(0.05)
        self.assertEqual(seen, {"scope": True, "ik": False, "user": uid})

    def test_the_ik_is_never_kept_between_calls(self):
        calls = []

        def opener():
            calls.append(1)
            return helpers.ik_of()

        uid, dek = helpers.key_of()
        db = SessionLocal()
        try:
            with crypto.as_user(uid, dek, opener):
                admin_key.open_apk(db, uid)
                admin_key.open_apk(db, uid)
        finally:
            db.close()
        self.assertEqual(len(calls), 2, "opened afresh each time")


# ---------------------------------------------------------------------------
# Every flow that makes or re-wraps the DEK does the same for the IK
# ---------------------------------------------------------------------------

class FlowsTest(Case):
    def test_account_creation_wraps_the_ik_under_the_password_the_codes_and_the_session_only(self):
        email = f"flows-new-{os.urandom(4).hex()}@example.invalid"
        account = users.create_user(email, PASSWORD, background=True)
        row = rows(lambda s: s.get(UserKeys, account.user_id))
        self.assertIsNotNone(row.server_wrapped_dek)  # background access on ...
        self.assertIsNotNone(row.pw_wrapped_ik)       # ... and the IK is under the password
        server = users.server_keys()
        for blob in (row.pw_wrapped_ik, row.x25519_priv_enc, row.pending_codes_enc):
            self.assertIsNone(server.kek_id_of(bytes(blob)))
        self.assertEqual(rows(lambda s: s.query(RecoveryCode).filter(RecoveryCode.user_id == account.user_id,
                                                                     RecoveryCode.wrapped_ik.isnot(None)).count()), 8)
        self.assertEqual(self.assert_identity(email, PASSWORD, account.recovery_codes), account.ik)

    def test_sign_up_by_invitation_leaves_a_working_ik(self):
        first = self.signed_in()
        self.addCleanup(self.set_mode, "invite")
        token = first.post("/api/admin/invites", json={}).json()["link"].split("invite=")[1]
        email = f"flows-invite-{os.urandom(4).hex()}@example.invalid"
        anon = self.anonymous()
        r = anon.post("/api/auth/signup", json={"email": email, "password": PASSWORD, "invite": token})
        self.assertEqual(r.status_code, 201, r.text)
        self.assert_session_has_ik(anon)
        self.assert_identity(email, PASSWORD, r.json()["recovery_codes"])

    def test_open_sign_up_leaves_a_working_ik(self):
        self.signed_in().put("/api/admin/signup", json={"mode": "open"})
        self.addCleanup(self.set_mode, "invite")
        email = f"flows-open-{os.urandom(4).hex()}@example.invalid"
        anon = self.anonymous()
        r = anon.post("/api/auth/signup", json={"email": email, "password": PASSWORD})
        self.assertEqual(r.status_code, 201, r.text)
        self.assert_session_has_ik(anon)
        self.assert_identity(email, PASSWORD, r.json()["recovery_codes"])

    def set_mode(self, mode: str) -> None:
        with helpers.client() as c:
            c.put("/api/admin/signup", json={"mode": mode})

    def test_the_bootstrap_admin_has_a_working_ik_and_a_session_that_opens_the_admin_key(self):
        self.assert_identity(helpers.ADMIN_EMAIL, helpers.ADMIN_PASSWORD)
        c = self.signed_in()
        self.assertTrue(self.sees_details(c))
        self.assert_session_has_ik(c)

    def test_sign_in_with_two_step_carries_the_ik_through_the_code_step(self):
        admin = self.make_admin()
        t = [(int(time.time()) // totp.STEP_SECONDS) * totp.STEP_SECONDS + 15.0]
        with mock.patch("backend.totp.now", lambda: t[0]):
            c = self.signed_in(admin)
            secret = c.post("/api/auth/mfa/setup").json()["secret"]
            step = int(t[0] // totp.STEP_SECONDS)
            self.assertEqual(c.post("/api/auth/mfa/enable", json={"code": totp.code_at(secret, step)}).status_code, 200)
            c.post("/api/auth/logout")
            t[0] += 2 * totp.STEP_SECONDS
            ratelimit.AUTH.reset()
            d = self.anonymous()
            r = d.post("/api/auth/login", json={"email": admin.email, "password": PASSWORD})
            self.assertEqual((r.status_code, r.json().get("mfa_required")), (200, True))
            pending = rows(lambda s: s.query(AuthSession).filter(AuthSession.user_id == admin.id,
                                                                 AuthSession.state == "pending_mfa").one())
            self.assertIsNotNone(pending.wrapped_ik)
            self.assertEqual(d.get("/api/admin/users").status_code, 401)  # nothing opens before the code
            ratelimit.AUTH.reset()
            r = d.post("/api/auth/login/mfa", json={"code": totp.code_at(secret, int(t[0] // totp.STEP_SECONDS))})
            self.assertEqual(r.status_code, 200, r.text)
            d.headers["X-CSRF-Token"] = r.json()["csrf"]
            self.assertTrue(self.sees_details(d))
            self.assert_session_has_ik(d)
            self.assertEqual(rows(lambda s: s.query(AuthSession).filter(AuthSession.user_id == admin.id,
                                                                        AuthSession.state == "pending_mfa").count()), 0)

    def test_a_password_change_wraps_the_ik_under_the_new_password(self):
        admin = self.make_admin()
        c = self.signed_in(admin)
        before = self.assert_identity(admin.email, PASSWORD)
        r = c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        helpers._passwords[admin.email] = NEW_PASSWORD
        self.assertEqual(self.assert_identity(admin.email, NEW_PASSWORD), before)  # the same IK, nothing re-made
        db = SessionLocal()
        try:
            self.assertIsNone(users.unlock(db, users.find_by_email(db, admin.email), PASSWORD))
        finally:
            db.close()
        self.assertTrue(self.sees_details(c))  # the signed-in session carries on
        self.assertTrue(self.sees_details(self.login(admin.email, NEW_PASSWORD)))

    def test_new_recovery_codes_carry_the_ik_and_the_old_ones_go(self):
        admin = self.make_admin()
        old = helpers.pending_codes(admin)
        c = self.signed_in(admin)
        r = c.post("/api/auth/recovery-codes", json={"password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        codes = r.json()["recovery_codes"]
        ik = self.assert_identity(admin.email, PASSWORD, codes)
        db = SessionLocal()
        try:
            for code in old:
                self.assertIsNone(users.open_recovery_code(db, admin.id, code))
            row = db.get(UserKeys, admin.id)
            # the unconfirmed codes are under the IK, not the data key
            self.assertEqual(users.pending_recovery_codes(db, admin.id, ik), codes)
            with crypto.as_user(admin.id, helpers.key_of(admin.email)[1]):
                with self.assertRaises(crypto.CryptoError):
                    crypto.unseal(row.pending_codes_enc, users._codes_label(admin.id))
        finally:
            db.close()
        shown = c.post("/api/auth/recovery-codes/show", json={"password": PASSWORD})
        self.assertEqual(shown.json()["codes"], codes)

    def test_a_reset_with_a_recovery_code_keeps_the_ik_and_the_admin_key(self):
        admin = self.make_admin()
        codes = helpers.pending_codes(admin)
        before = self.assert_identity(admin.email, PASSWORD, codes)
        anon = self.anonymous()
        ratelimit.AUTH.reset()
        r = anon.post("/api/auth/recover", json={"email": admin.email, "recovery_code": codes[0],
                                                 "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["admin_key_lost"])
        helpers._passwords[admin.email] = NEW_PASSWORD
        anon.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertTrue(self.sees_details(anon))  # the new session holds the IK the code opened
        self.assert_session_has_ik(anon)
        self.assertEqual(self.assert_identity(admin.email, NEW_PASSWORD, codes[1:]), before)
        self.assertIsNotNone(rows(lambda s: s.get(AdminKeyGrant, admin.id)))

    def test_a_reset_by_email_that_keeps_the_data_makes_a_new_key_pair_and_ik(self):
        bob = helpers.make_user(password=PASSWORD)
        with helpers.client(bob.email) as c:
            self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)
        old_ik = helpers.ik_of(bob.email)
        old_pub = rows(lambda s: bytes(s.get(UserKeys, bob.id).x25519_pub))
        db = SessionLocal()
        try:
            kept = users.reset_password_keeping_data(db, bob.id, NEW_PASSWORD)
        finally:
            db.close()
        helpers._passwords[bob.email] = NEW_PASSWORD
        helpers._iks.pop(bob.email, None)
        self.assertNotEqual(kept.ik, old_ik)
        self.assertFalse(kept.admin_key_lost)  # (an ordinary user had no share)
        self.assertEqual(self.assert_identity(bob.email, NEW_PASSWORD, kept.recovery_codes), kept.ik)
        self.assertNotEqual(rows(lambda s: bytes(s.get(UserKeys, bob.id).x25519_pub)), old_pub)
        db = SessionLocal()
        try:
            with self.assertRaises(crypto.CryptoError):  # the old IK opens nothing now
                users.open_private_key(db.get(UserKeys, bob.id), bob.id, old_ik)
        finally:
            db.close()

    def test_a_reset_by_email_that_deletes_the_data_makes_a_new_key_pair_and_ik(self):
        bob = helpers.make_user(password=PASSWORD)
        old_ik = helpers.ik_of(bob.email)
        db = SessionLocal()
        try:
            wiped = users.reset_password_wiping_data(db, bob.id, NEW_PASSWORD, bob.email)
        finally:
            db.close()
        helpers._passwords[bob.email] = NEW_PASSWORD
        helpers._keys.pop(bob.email, None)
        helpers._iks.pop(bob.email, None)
        self.assertNotEqual(wiped.ik, old_ik)
        self.assertEqual(self.assert_identity(bob.email, NEW_PASSWORD, wiped.recovery_codes), wiped.ik)

    def test_an_admin_who_resets_by_email_with_the_data_kept_needs_the_key_shared_again(self):
        admin = self.make_admin()
        with helpers.client(admin.email) as c:
            self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)
        db = SessionLocal()
        try:
            kept = users.reset_password_keeping_data(db, admin.id, NEW_PASSWORD)
        finally:
            db.close()
        helpers._passwords[admin.email] = NEW_PASSWORD
        helpers._iks.pop(admin.email, None)
        self.assertTrue(kept.admin_key_lost)
        self.assertIsNone(rows(lambda s: s.get(AdminKeyGrant, admin.id)))
        self.assertIn("admin_key_lost", [a for (a,) in rows(lambda s: s.query(AdminAudit.action).all())])
        c = self.login(admin.email, NEW_PASSWORD)
        self.assertFalse(self.sees_details(c))
        self.assertFalse(c.get("/api/admin/users").json()["admin_key_needs_sign_in"])  # (it is the grant, not the session)
        with helpers.client() as first:
            self.assertEqual(first.post(f"/api/admin/users/{admin.id}/regrant").status_code, 200)
        self.assertTrue(self.sees_details(c))

    def test_the_wrong_ik_cannot_be_used_to_change_a_password_or_make_codes(self):
        bob = helpers.make_user(password=PASSWORD)
        uid, dek = helpers.key_of(bob.email)
        db = SessionLocal()
        try:
            user = db.get(User, uid)
            with self.assertRaises(crypto.KeyMismatch):
                users.apply_new_password(db, user, dek, None, users.new_password_keys(NEW_PASSWORD))
            with self.assertRaises(crypto.CryptoError):
                users.apply_new_password(db, user, dek, os.urandom(32), users.new_password_keys(NEW_PASSWORD))
            db.rollback()
            with self.assertRaises(crypto.KeyMismatch):
                users.replace_recovery_codes(db, uid, dek, None)
            db.rollback()
            with self.assertRaises(crypto.CryptoError):
                users.replace_recovery_codes(db, uid, dek, os.urandom(32))
            db.rollback()
        finally:
            db.close()
        self.assert_identity(bob.email, PASSWORD)  # nothing changed

    def test_deleting_an_account_removes_every_wrap_of_its_ik(self):
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        before = rows(lambda s: (bytes(s.get(UserKeys, bob.id).pw_wrapped_ik), bytes(s.get(UserKeys, bob.id).x25519_priv_enc),
                                 [bytes(r.wrapped_ik) for r in s.query(RecoveryCode).filter(RecoveryCode.user_id == bob.id)],
                                 [bytes(r.wrapped_ik) for r in s.query(AuthSession).filter(AuthSession.user_id == bob.id)]))
        self.assertTrue(before[0] and before[1] and len(before[2]) == 8 and len(before[3]) == 1)
        r = c.post("/api/account/delete", json={"password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(rows(lambda s: (s.query(UserKeys).filter(UserKeys.user_id == bob.id).count(),
                                         s.query(RecoveryCode).filter(RecoveryCode.user_id == bob.id).count(),
                                         s.query(AuthSession).filter(AuthSession.user_id == bob.id).count())), (0, 0, 0))
        raw = raw_database_bytes()
        for blob in [before[0], before[1], *before[2], *before[3]]:
            self.assertNotIn(blob, raw, "no wrap of the IK is left in the database file or its log")


# ---------------------------------------------------------------------------
# Accounts from before the identity key
# ---------------------------------------------------------------------------

class MigrationTest(Case):
    def test_the_next_password_sign_in_moves_the_private_key_to_a_new_ik_and_removes_the_old_copy(self):
        admin = self.make_admin()
        codes = helpers.pending_codes(admin)
        self.make_legacy(admin)
        uid, dek = helpers.key_of(admin.email)
        old = rows(lambda s: bytes(s.get(UserKeys, uid).x25519_priv_enc))
        old_pending = rows(lambda s: bytes(s.get(UserKeys, uid).pending_codes_enc))
        with crypto.as_user(uid, dek):  # the DEK-sealed form, as the old version wrote it
            crypto.unseal(old, f"user_keys.x25519_priv|{uid}")
        self.assertIn(old, raw_database_bytes())
        c = self.login(admin.email, PASSWORD)  # no start-up step: the password does it
        row = rows(lambda s: s.get(UserKeys, uid))
        self.assertIsNotNone(row.pw_wrapped_ik)
        self.assertNotEqual(bytes(row.x25519_priv_enc), old)
        with crypto.as_user(uid, dek):
            with self.assertRaises(crypto.CryptoError):  # not under the data key any more
                crypto.unseal(row.x25519_priv_enc, f"user_keys.x25519_priv|{uid}")
        ik = self.assert_identity(admin.email, PASSWORD)
        self.assertEqual(rows(lambda s: users.pending_recovery_codes(s, uid, ik)), codes)  # moved under the IK too
        raw = raw_database_bytes()
        self.assertNotIn(old, raw, "the DEK-sealed copy is gone from the database file and its log")
        self.assertNotIn(old_pending, raw)
        # the new session opens the admin key, and so does every later one; the grant was kept
        self.assertTrue(self.sees_details(c))
        self.assert_session_has_ik(c)
        self.assertIsNotNone(rows(lambda s: s.get(AdminKeyGrant, uid)))
        self.assertTrue(self.sees_details(self.login(admin.email, PASSWORD)))
        # signing in again doesn't make another IK
        self.assertEqual(self.assert_identity(admin.email, PASSWORD), ik)

    def test_a_password_check_in_settings_migrates_too(self):
        """Changing the password from a session made before the IK: the password opens the data key, so the account gets
        its IK there and then, wrapped under the new password. (That session stays without one.)"""
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        self.make_legacy(bob, keep_sessions=True)
        r = c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(rows(lambda s: s.get(UserKeys, bob.id).pw_wrapped_ik))
        self.assert_identity(bob.email, NEW_PASSWORD)
        db = SessionLocal()
        try:
            self.assertIsNone(users.unlock(db, users.find_by_email(db, bob.email), PASSWORD))
        finally:
            db.close()

    def test_a_session_from_before_the_change_asks_to_sign_in_again(self):
        admin = self.make_admin()
        c = self.signed_in(admin)
        self.assertTrue(self.sees_details(c))
        sid = crypto.session_id(crypto.b64d(c.cookies.get("jh_session")))
        db = SessionLocal()
        try:
            db.query(AuthSession).filter(AuthSession.id == sid).update({AuthSession.wrapped_ik: None})
            db.commit()
        finally:
            db.close()
        r = c.get("/api/admin/users")  # the panel still answers, saying what to do
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["admin_key_available"])
        self.assertTrue(r.json()["admin_key_needs_sign_in"])
        self.assertTrue(all(u["email"] is None for u in r.json()["users"]))
        other = helpers.make_user(password=PASSWORD)
        r = c.post(f"/api/admin/users/{other.id}/role", json={"role": "admin"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "admin_key_missing"))
        self.assertEqual(r.json()["detail"], "Sign in again to open people's details.")
        self.assertEqual(c.get("/api/auth/me").status_code, 200)  # everything else works
        self.assertTrue(self.sees_details(self.login(admin.email, PASSWORD)))  # signing in again fixes it

    def test_a_reset_with_a_code_from_before_the_change_keeps_the_data_and_asks_for_the_key_again(self):
        admin = self.make_admin()
        c = self.signed_in(admin)
        profile = c.get("/api/profile").json()
        self.assertEqual(c.put("/api/profile", json={**profile, "titles": ["Kept Through Old Codes"]}).status_code, 200)
        codes = helpers.pending_codes(admin)
        self.assertEqual(c.post("/api/auth/recovery-codes/ack").status_code, 200)  # saved: their text is gone
        self.make_legacy(admin)
        self.login(admin.email, PASSWORD)  # migrates the account; the codes were made before and stay without an IK
        helpers._iks.pop(admin.email, None)
        sec = self.signed_in(admin).get("/api/auth/security").json()
        self.assertTrue(sec["recovery_codes_old"])  # Settings suggests making new codes
        self.assertEqual(rows(lambda s: users.recovery_codes_without_ik(s, admin.id)), 8)
        old_pub = rows(lambda s: bytes(s.get(UserKeys, admin.id).x25519_pub))
        anon = self.anonymous()
        ratelimit.AUTH.reset()
        r = anon.post("/api/auth/recover", json={"email": admin.email, "recovery_code": codes[0],
                                                 "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["admin_key_lost"])
        helpers._passwords[admin.email] = NEW_PASSWORD
        anon.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertEqual(anon.get("/api/profile").json()["titles"], ["Kept Through Old Codes"])  # the data is kept
        self.assertNotEqual(rows(lambda s: bytes(s.get(UserKeys, admin.id).x25519_pub)), old_pub)
        self.assertIsNone(rows(lambda s: s.get(AdminKeyGrant, admin.id)))
        self.assertFalse(self.sees_details(anon))
        self.assertIn("admin_key_lost", [a for (a,) in rows(lambda s: s.query(AdminAudit.action).all())])
        # the other old codes can't be taken for the new IK; they still reset (the same way)
        ik = self.assert_identity(admin.email, NEW_PASSWORD)
        self.assertEqual(rows(lambda s: s.query(RecoveryCode).filter(RecoveryCode.user_id == admin.id,
                                                                     RecoveryCode.wrapped_ik.isnot(None)).count()), 0)
        self.assertIsNotNone(ik)
        with helpers.client() as first:  # another admin shares the key again
            self.assertEqual(first.post(f"/api/admin/users/{admin.id}/regrant").status_code, 200)
        self.assertTrue(self.sees_details(anon))
        # new codes carry the IK, and the suggestion goes away
        d = self.signed_in(admin, NEW_PASSWORD)
        self.assertEqual(d.post("/api/auth/recovery-codes", json={"password": NEW_PASSWORD}).status_code, 200)
        self.assertFalse(d.get("/api/auth/security").json()["recovery_codes_old"])

    def test_the_migration_makes_a_new_key_pair_and_replaces_the_admin_key(self):
        """A copy of the database from before the migration (or the DEK-sealed private key in a backup) must not open
        the grant the admin has now, nor anything sealed to the admin key from then on."""
        admin = self.make_admin()
        self.make_legacy(admin)
        uid, dek = helpers.key_of(admin.email)
        before = rows(lambda s: (bytes(s.get(UserKeys, uid).x25519_priv_enc), bytes(s.get(UserKeys, uid).x25519_pub),
                                 bytes(s.get(AdminKeyGrant, uid).sealed_priv), s.get(AdminKeyGrant, uid).apk_version))
        old_blob, old_pub, old_grant, old_version = before
        self.login(admin.email, PASSWORD)  # migrates (and replaces the admin key)
        helpers._iks.pop(admin.email, None)
        row = rows(lambda s: s.get(UserKeys, uid))
        self.assertNotEqual(bytes(row.x25519_pub), old_pub, "a new key pair, not the old private key re-wrapped")
        ik = self.assert_identity(admin.email, PASSWORD)
        self.assertEqual(crypto.public_key_of(users.open_private_key(row, uid, ik)), bytes(row.x25519_pub))
        current = rows(lambda s: admin_key.current_public(s))
        self.assertGreater(current[1], old_version)
        grant = rows(lambda s: s.get(AdminKeyGrant, uid))
        self.assertEqual(grant.apk_version, current[1])
        self.assertIn("admin_key_replaced", [a for (a,) in rows(lambda s: s.query(AdminAudit.action).all())])
        # everyone who held the key still does, and the migrated admin reads details through the new grant
        self.assertTrue(self.sees_details(self.login(admin.email, PASSWORD)))
        self.assertTrue(self.sees_details(self.signed_in()))
        # the attack: the DEK (from the server key) opens the old blob, which gives the old private key ...
        with crypto.as_user(uid, dek):
            old_priv = crypto.unseal(old_blob, f"user_keys.x25519_priv|{uid}")
        with self.assertRaises(crypto.CryptoError):  # ... and that opens nothing of today
            crypto.open_sealed(old_priv, grant.sealed_priv, admin_key.grant_label(uid, grant.apk_version))
        # the old grant (in the backup) still opens the old key, which opens nothing written to the current one
        old_apk = admin_key.Apk(crypto.open_sealed(old_priv, old_grant, admin_key.grant_label(uid, old_version)),
                                b"", old_version)
        newcomer = helpers.make_user(password=PASSWORD)
        pii = rows(lambda s: s.get(User, newcomer.id).pii_admin)
        self.assertIsNone(admin_key.open_json(old_apk, pii, admin_key.pii_label(newcomer.id)))
        self.assertIsNotNone(admin_key.open_json(self.current_apk(), pii, admin_key.pii_label(newcomer.id)))

    def current_apk(self) -> "admin_key.Apk":
        uid, dek = helpers.key_of()
        db = SessionLocal()
        try:
            with crypto.as_user(uid, dek, lambda: helpers.ik_of()):
                return admin_key.open_apk(db, uid)
        finally:
            db.close()

    def test_a_grant_that_does_not_open_is_deleted_by_the_migration(self):
        admin = self.make_admin()
        self.make_legacy(admin)
        db = SessionLocal()
        try:
            db.get(AdminKeyGrant, admin.id).sealed_priv = os.urandom(120)
            db.commit()
        finally:
            db.close()
        self.login(admin.email, PASSWORD)
        self.assertIsNone(rows(lambda s: s.get(AdminKeyGrant, admin.id)))
        self.assertIn("admin_key_lost", [a for (a,) in rows(lambda s: s.query(AdminAudit.action).all())])
        self.assert_identity(admin.email, PASSWORD)

    def test_unconfirmed_codes_get_the_ik_in_the_migration_and_keep_the_admin_key_in_a_reset(self):
        admin = self.make_admin()
        codes = helpers.pending_codes(admin)
        self.make_legacy(admin)
        self.login(admin.email, PASSWORD)  # migrates: the codes' text is in hand, so they carry the IK too
        helpers._iks.pop(admin.email, None)
        self.assertEqual(rows(lambda s: users.recovery_codes_without_ik(s, admin.id)), 0)
        self.assert_identity(admin.email, PASSWORD, codes)
        self.assertFalse(self.signed_in(admin).get("/api/auth/security").json()["recovery_codes_old"])
        anon = self.anonymous()
        ratelimit.AUTH.reset()
        r = anon.post("/api/auth/recover", json={"email": admin.email, "recovery_code": codes[0],
                                                 "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["admin_key_lost"])
        helpers._passwords[admin.email] = NEW_PASSWORD
        anon.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertTrue(self.sees_details(anon))

    def test_a_reset_with_a_code_without_an_ik_drops_the_unconfirmed_codes_it_cannot_show(self):
        admin = self.make_admin()
        codes = helpers.pending_codes(admin)
        c = self.signed_in(admin)
        self.assertTrue(c.get("/api/auth/security").json()["recovery_codes_pending"])
        db = SessionLocal()  # codes from before the IK that are still unconfirmed, on an account that has an IK
        try:
            db.query(RecoveryCode).filter(RecoveryCode.user_id == admin.id).update({RecoveryCode.wrapped_ik: None})
            db.commit()
        finally:
            db.close()
        anon = self.anonymous()
        ratelimit.AUTH.reset()
        r = anon.post("/api/auth/recover", json={"email": admin.email, "recovery_code": codes[0],
                                                 "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["admin_key_lost"])
        helpers._passwords[admin.email] = NEW_PASSWORD
        helpers._iks.pop(admin.email, None)
        anon.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertFalse(anon.get("/api/auth/security").json()["recovery_codes_pending"])
        self.assertIsNone(rows(lambda s: s.get(UserKeys, admin.id).pending_codes_enc))
        self.assert_identity(admin.email, NEW_PASSWORD)

    def test_a_reset_with_an_old_code_before_any_sign_in_keeps_the_key(self):
        """The account has no IK yet, so the code (which opens the data key, which opens the private key) can still give
        it one: nothing is lost."""
        admin = self.make_admin()
        codes = helpers.pending_codes(admin)
        self.make_legacy(admin)
        old_pub = rows(lambda s: bytes(s.get(UserKeys, admin.id).x25519_pub))
        anon = self.anonymous()
        ratelimit.AUTH.reset()
        r = anon.post("/api/auth/recover", json={"email": admin.email, "recovery_code": codes[0],
                                                 "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["admin_key_lost"])
        helpers._passwords[admin.email] = NEW_PASSWORD
        helpers._iks.pop(admin.email, None)
        anon.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertTrue(self.sees_details(anon))  # the grant was carried to the new key pair
        self.assertNotEqual(rows(lambda s: bytes(s.get(UserKeys, admin.id).x25519_pub)), old_pub)
        self.assert_identity(admin.email, NEW_PASSWORD)

    def test_a_reset_with_a_current_code_after_the_migration_keeps_the_key(self):
        admin = self.make_admin()
        self.make_legacy(admin)
        self.login(admin.email, PASSWORD)  # migrated
        helpers._iks.pop(admin.email, None)
        d = self.signed_in(admin)
        codes = d.post("/api/auth/recovery-codes", json={"password": PASSWORD}).json()["recovery_codes"]
        anon = self.anonymous()
        ratelimit.AUTH.reset()
        r = anon.post("/api/auth/recover", json={"email": admin.email, "recovery_code": codes[0],
                                                 "new_password": NEW_PASSWORD})
        self.assertEqual((r.status_code, r.json()["admin_key_lost"]), (200, False))
        helpers._passwords[admin.email] = NEW_PASSWORD
        anon.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertTrue(self.sees_details(anon))
        self.assertEqual(rows(lambda s: users.recovery_codes_without_ik(s, admin.id)), 0)


class LegacyGrantTest(Case):
    """An account with background access that hasn't signed in with its password since the update still has its private
    key under the data key, which the server key opens: a grant sealed to it would not be protected from the key file."""

    def legacy_user(self, *, background: bool = True) -> helpers.Account:
        user = helpers.make_user(password=PASSWORD)
        if background:
            self.assertEqual(self.signed_in(user).post("/api/account/background", json={"enabled": True}).status_code,
                             200)
            self.assertIsNotNone(rows(lambda s: s.get(UserKeys, user.id).server_wrapped_dek))
        self.make_legacy(user)
        return user

    def actions(self) -> list:
        return [a for (a,) in rows(lambda s: s.query(AdminAudit.action).all())]

    def test_promoting_such_an_account_asks_them_to_sign_in_first(self):
        user = self.legacy_user()
        with helpers.client() as first:
            r = first.post(f"/api/admin/users/{user.id}/role", json={"role": "admin"})
            self.assertEqual((r.status_code, r.json()["code"]), (409, "needs_sign_in"))
            self.assertEqual(r.json()["detail"], "Ask them to sign in once first, then share the key.")
            self.assertEqual(rows(lambda s: s.get(User, user.id).role), "user")
            self.assertIsNone(rows(lambda s: s.get(AdminKeyGrant, user.id)))
            self.login(user.email, PASSWORD)  # their next sign-in gives them an identity key
            self.addCleanup(self.demote, user.id)
            self.assertEqual(first.post(f"/api/admin/users/{user.id}/role", json={"role": "admin"}).status_code, 200)
        self.assertTrue(self.sees_details(self.login(user.email, PASSWORD)))

    def test_promoting_such_an_account_without_background_access_works(self):
        user = self.legacy_user(background=False)  # the server key doesn't open its data key
        self.addCleanup(self.demote, user.id)
        with helpers.client() as first:
            self.assertEqual(first.post(f"/api/admin/users/{user.id}/role", json={"role": "admin"}).status_code, 200)

    def test_sharing_the_key_again_with_such_an_admin_waits_for_their_sign_in(self):
        admin = self.make_admin()
        self.assertEqual(self.signed_in(admin).post("/api/account/background", json={"enabled": True}).status_code, 200)
        self.make_legacy(admin)
        grant = rows(lambda s: bytes(s.get(AdminKeyGrant, admin.id).sealed_priv))
        with helpers.client() as first:
            r = first.post(f"/api/admin/users/{admin.id}/regrant")
            self.assertEqual((r.status_code, r.json()["code"]), (409, "needs_sign_in"))
            self.assertEqual(rows(lambda s: bytes(s.get(AdminKeyGrant, admin.id).sealed_priv)), grant)  # untouched
            self.login(admin.email, PASSWORD)
            self.assertEqual(first.post(f"/api/admin/users/{admin.id}/regrant").status_code, 200)

    def test_a_new_admin_key_skips_such_an_admin_until_they_have_signed_in(self):
        admin = self.make_admin()
        self.assertEqual(self.signed_in(admin).post("/api/account/background", json={"enabled": True}).status_code, 200)
        self.make_legacy(admin)
        other = self.make_admin()
        with helpers.client() as first:  # demoting someone replaces the key
            version = rows(lambda s: admin_key.current_public(s)[1])
            self.assertEqual(first.post(f"/api/admin/users/{other.id}/role", json={"role": "user"}).status_code, 200)
            self.assertGreater(rows(lambda s: admin_key.current_public(s)[1]), version)
            self.assertIsNone(rows(lambda s: s.get(AdminKeyGrant, admin.id)), "not given the new key")
            self.assertIn("admin_key_not_shared", self.actions())
            self.assertTrue(self.sees_details(first))  # everyone else got it
            c = self.login(admin.email, PASSWORD)  # signing in gives them an identity key, but no grant
            self.assertFalse(self.sees_details(c))
            self.assertEqual(first.post(f"/api/admin/users/{admin.id}/regrant").status_code, 200)
        self.assertTrue(self.sees_details(self.login(admin.email, PASSWORD)))


LEGACY_RECOVER = '''
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from backend import crypto, keys, users
from backend.db import SessionLocal, User, UserKeys, init_db

ADMIN_PW = "root-password-123"
init_db()
users.startup()                                   # the admin, from the environment (it has an identity key)
db = SessionLocal()
admin = users.find_by_email(db, "root@example.invalid")
unlocked = users.unlock(db, admin, ADMIN_PW)
row = db.get(UserKeys, admin.id)
priv = users.open_private_key(row, admin.id, unlocked.ik)
with crypto.as_user(admin.id, unlocked.dek):      # ... put back the way an older version kept it
    row.x25519_priv_enc = crypto.seal(priv, f"user_keys.x25519_priv|{admin.id}")
row.pw_wrapped_ik = None
db.commit()
db.close()
key_path = Path(FOLDER) / "server-keys.json"
key_path.unlink()


def recover(email, password):
    out_, err_ = io.StringIO(), io.StringIO()
    with redirect_stdout(out_), redirect_stderr(err_):
        status = keys.main(["recover-lost", "--admin-email", email], ask_password=lambda prompt: password)
    return {"status": status, "out": out_.getvalue(), "err": err_.getvalue()}


out["legacy"] = recover("root@example.invalid", ADMIN_PW)
out["file"] = key_path.exists()
users.startup()                                   # a restart: the new file matches the database
db = SessionLocal()
out["found"] = users.find_by_email(db, "root@example.invalid") is not None
db.close()
'''


class RecoverLostTest(Case):
    def test_recover_lost_opens_the_admin_key_through_the_password_and_the_ik(self):
        """The ``recover-lost`` scenario in tests/test_background.py runs it on an account with an IK; this one on an
        admin who hasn't signed in since the IK was introduced (the private key is still under the data key)."""
        out = helpers.scenario(LEGACY_RECOVER, env={"JOBHUNT_ADMIN_EMAIL": "root@example.invalid",
                                                    "JOBHUNT_ADMIN_PASSWORD": "root-password-123"})
        self.assertEqual(out["legacy"]["status"], 0, out["legacy"])
        self.assertTrue(out["file"])
        self.assertTrue(out["found"])


if __name__ == "__main__":
    unittest.main()
