"""The admin panel's backend (accounts design §1.4, §7, Phase F): stats, the list of people with their sealed
details, roles and the admin key (grant, rotation, re-grant, the command that makes a new key), sign-up modes and
invitations, the audit log, the server key's rotation, plaintext clean-up and the AI error messages ordinary users
see.

All of it runs on the shared test database (the admin from tests/__init__.py plus users each test makes). A test
that makes someone an admin or changes the sign-up mode puts it back, so other modules see what they expect.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend import admin, admin_key, crypto, docs, key_rotation, keys, llm_providers, migrate_encryption, ratelimit  # noqa: E402
from backend import tokens, totp, users  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import (  # noqa: E402
    DB_PATH,
    UPLOAD_DIR,
    AdminAudit,
    AdminKeyGrant,
    AuthSession,
    AuthToken,
    Document,
    Job,
    LlmUsage,
    Membership,
    MembershipGrant,
    SearchRun,
    SessionLocal,
    User,
    UserKeys,
    Workspace,
    engine,
    utcnow,
)

PASSWORD = "user-password-123"


def db_rows(fn):
    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


def apk_version() -> int:
    return db_rows(lambda s: admin_key.current_public(s)[1])


class AdminTestCase(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)
        self.admin_id = helpers.admin_id()

    def signed_in(self, account=None, **kwargs) -> helpers.SignedInClient:
        c = helpers.client(account.email) if account else helpers.client(**kwargs)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def anonymous(self) -> TestClient:
        c = TestClient(app)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def make_admin(self, **kwargs) -> helpers.Account:
        """A new admin (promoted by the first admin, who must hold the key), demoted again when the test ends."""
        account = helpers.make_user(**kwargs)
        r = self.signed_in().post(f"/api/admin/users/{account.id}/role", json={"role": "admin"})
        self.assertEqual(r.status_code, 200, r.text)
        self.addCleanup(self.demote_quietly, account.id)
        return account

    def demote_quietly(self, user_id: int) -> None:
        c = helpers.client()
        with c:
            c.post(f"/api/admin/users/{user_id}/role", json={"role": "user"})

    def user_row(self, client, user_id: int) -> dict:
        r = client.get("/api/admin/users")
        self.assertEqual(r.status_code, 200, r.text)
        return next(u for u in r.json()["users"] if u["id"] == user_id)

    def make_stale(self, reauth_minutes_ago: int = 30) -> None:
        """Every session of the admin entered the password a while ago."""
        with engine.begin() as conn:
            conn.execute(text("UPDATE sessions SET reauth_at = :t WHERE user_id = :u"),
                         {"t": utcnow() - timedelta(minutes=reauth_minutes_ago), "u": self.admin_id})


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class StatsTest(AdminTestCase):
    def test_people_figures_count_users_by_activity_and_age(self):
        c = self.signed_in()
        before = c.get("/api/admin/stats").json()["users"]
        today = date.today()
        a, b, d = helpers.make_user(), helpers.make_user(), helpers.make_user()
        with engine.begin() as conn:
            for acct, active, created in ((a, today, 0), (b, today - timedelta(days=8), 45), (d, today - timedelta(days=40), 10)):
                conn.execute(text("UPDATE users SET last_active_on = :d, created_at = :c WHERE id = :i"),
                             {"d": active.isoformat(), "c": utcnow() - timedelta(days=created), "i": acct.id})
        after = c.get("/api/admin/stats").json()["users"]
        self.assertEqual({k: after[k] - before[k] for k in after},
                         {"total": 3, "admins": 0, "active_7d": 1, "active_30d": 2, "new_30d": 2})

    def test_llm_usage_by_day_model_purpose_and_user(self):
        c = self.signed_in()
        bob, carol = helpers.make_user(), helpers.make_user()
        model = f"stats-model-{os.getpid()}"
        today, yesterday = utcnow(), utcnow() - timedelta(days=1)
        rows = [  # (when, user_id, workspace_id, purpose, input, output, cached, cache_write)
            (today, bob.id, None, "scoring", 1000, 100, 600, 50),
            (today, bob.id, None, "scoring", 500, 50, 0, None),
            (yesterday, carol.id, None, "chat", 200, 20, None, None),
            (yesterday, None, bob.workspace_id, "chat", 300, 30, 100, None),   # an older row: the workspace's owner
            (today - timedelta(days=60), bob.id, None, "scoring", 9999, 999, 999, 99),  # outside the window
        ]
        db = SessionLocal()
        ids = []
        for when, uid, ws, purpose, i, o, ca, w in rows:
            row = LlmUsage(created_at=when, user_id=uid, workspace_id=ws, purpose=purpose, provider="openai",
                           model=model, input_tokens=i, output_tokens=o, cached_tokens=ca, cache_write_tokens=w)
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
        db.close()
        self.addCleanup(lambda: db_rows(lambda s: (s.query(LlmUsage).filter(LlmUsage.id.in_(ids)).delete(
            synchronize_session=False), s.commit())))
        out = c.get("/api/admin/stats?days=30").json()["llm"]
        mine = [r for r in out["by_day"] if r["model"] == model]
        by = {(r["day"], r["purpose"]): r for r in mine}
        self.assertEqual(len(mine), 2)  # (day, purpose) groups: the old row is outside the window
        scoring = by[(today.date().isoformat(), "scoring")]
        self.assertEqual((scoring["requests"], scoring["input"], scoring["output"], scoring["cached"],
                          scoring["cache_write"]), (2, 1500, 150, 600, 50))
        self.assertEqual(scoring["cache_hit_rate"], 0.4)
        chat = by[(yesterday.date().isoformat(), "chat")]
        self.assertEqual((chat["requests"], chat["input"], chat["cached"]), (2, 500, 100))
        self.assertEqual(chat["cache_hit_rate"], 0.2)
        users_of = {(r["user"]["id"], r["model"]): r for r in out["by_user"] if r["model"] == model}
        self.assertEqual(users_of[(bob.id, model)]["requests"], 3)  # two own rows and the owner-fallback one
        self.assertEqual(users_of[(bob.id, model)]["input"], 1800)
        self.assertEqual(users_of[(bob.id, model)]["cached"], 700)
        self.assertEqual(users_of[(carol.id, model)]["input"], 200)
        self.assertEqual(users_of[(carol.id, model)]["cache_hit_rate"], 0.0)
        # Everything in the window sums into the totals; the old row appears only with a longer window.
        self.assertGreaterEqual(out["totals"]["input"], 2000)
        wide = c.get("/api/admin/stats?days=90").json()["llm"]["by_day"]
        self.assertEqual(sum(r["input"] for r in wide if r["model"] == model), 2000 + 9999)
        self.assertEqual(c.get("/api/admin/stats?days=0").status_code, 422)

    def test_usage_is_recorded_for_the_requests_user_else_the_workspaces_owner(self):
        bob = helpers.make_user()
        model = f"recorded-{os.getpid()}"
        cfg = {"base_url": "https://llm.example.com/v1", "model": model, "purpose": "chat"}
        with helpers.as_account(bob):
            llm_providers.record_usage({**cfg, "workspace_id": None}, {"input_tokens": 5})
        llm_providers.record_usage({**cfg, "workspace_id": bob.workspace_id}, {"input_tokens": 6})   # no key scope
        llm_providers.record_usage({**cfg, "workspace_id": None}, {"input_tokens": 7})
        got = db_rows(lambda s: sorted((r.input_tokens, r.user_id) for r in s.query(LlmUsage).filter(
            LlmUsage.model == model)))
        self.assertEqual(got, [(5, bob.id), (6, bob.id), (7, None)])
        db_rows(lambda s: (s.query(LlmUsage).filter(LlmUsage.model == model).delete(), s.commit()))


# ---------------------------------------------------------------------------
# The list of people
# ---------------------------------------------------------------------------

class UsersTest(AdminTestCase):
    def test_list_has_details_roles_and_summaries(self):
        c = self.signed_in()
        bob = helpers.make_user(name="Bob Example")
        background = users.create_user(f"bg-{os.getpid()}@example.invalid", PASSWORD, name="Back Ground",
                                       background=True)
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET mfa_enabled = 1 WHERE id = :i"), {"i": bob.id})
            conn.execute(text("UPDATE memberships SET exempt = 1, free_searches_used = 2, stripe_status = 'active', "
                              "plan = 'monthly' WHERE user_id = :i"), {"i": bob.id})
        out = c.get("/api/admin/users").json()
        self.assertTrue(out["admin_key_available"])
        row = next(u for u in out["users"] if u["id"] == bob.id)
        self.assertEqual((row["email"], row["name"], row["role"], row["status"]), (bob.email, "Bob Example", "user", "active"))
        self.assertTrue(row["details_available"])
        self.assertIsNone(row["details_note"])
        self.assertEqual((row["mfa"], row["background"], row["workspaces"]), (True, False, 1))
        self.assertEqual(row["membership"]["exempt"], True)
        self.assertEqual(row["membership"]["free_searches_used"], 2)
        self.assertEqual((row["membership"]["stripe_status"], row["membership"]["plan"]), ("active", "monthly"))
        self.assertEqual(row["membership"]["active_grants"], 0)
        self.assertTrue(row["created_at"].endswith("Z"))
        self.assertNotIn("admin_key_shared", row)
        self.assertTrue(next(u for u in out["users"] if u["id"] == background.user_id)["background"])
        first = next(u for u in out["users"] if u["id"] == self.admin_id)
        self.assertEqual((first["role"], first["admin_key_shared"], first["email"]), ("admin", True, helpers.ADMIN_EMAIL))

    def test_stale_details_say_so_until_the_person_signs_in(self):
        c = self.signed_in()
        bob = helpers.make_user(name="Stale Bob")
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET pii_admin_version = 0 WHERE id = :i"), {"i": bob.id})
        row = self.user_row(c, bob.id)
        self.assertEqual((row["email"], row["name"], row["details_available"]), (None, None, False))
        self.assertEqual(row["details_note"], "Details unavailable until this person signs in")
        self.signed_in(bob)  # signing in re-seals them from the person's own copy
        row = self.user_row(c, bob.id)
        self.assertEqual((row["email"], row["name"]), (bob.email, "Stale Bob"))
        self.assertEqual(db_rows(lambda s: s.get(User, bob.id).pii_admin_version), apk_version())

    def test_unknown_person_is_404(self):
        c = self.signed_in()
        for method, path, body in (("post", "/api/admin/users/999999/disable", None),
                                   ("put", "/api/admin/users/999999/membership", {"exempt": True}),
                                   ("post", "/api/admin/users/999999/mfa/disable", None)):
            r = getattr(c, method)(path, **({"json": body} if body else {}))
            self.assertEqual((r.status_code, r.json()["code"]), (404, "not_found"), path)


# ---------------------------------------------------------------------------
# Nothing from users' own data
# ---------------------------------------------------------------------------

class NoContentTest(AdminTestCase):
    MARKERS = ("QZXJOBTITLE", "QZXDOCTEXT", "QZXWORKSPACE", "QZXPROFILE", "QZXCOMPANY", "QZXDESCRIPTION")

    def test_no_admin_response_carries_a_users_own_content(self):
        bob = helpers.make_user(name="Plain Bob")
        b = self.signed_in(bob)
        self.assertEqual(b.patch(f"/api/workspaces/{bob.workspace_id}", json={"name": "QZXWORKSPACE search"}).status_code, 200)
        self.assertEqual(b.put("/api/profile", json={"titles": ["QZXPROFILE engineer"]}).status_code, 200)
        with helpers.as_account(bob):
            db = SessionLocal()
            db.add(Job(workspace_id=bob.workspace_id, url="https://bob.example/qzx", origin="private",
                       title="QZXJOBTITLE", company="QZXCOMPANY", description="QZXDESCRIPTION", status="applied"))
            db.add(Document(workspace_id=bob.workspace_id, filename="cv QZXDOCTEXT.txt", text="QZXDOCTEXT body",
                            filetype=".txt", kind="resume"))
            db.commit()
            db.close()
        c = self.signed_in()
        calls = [("get", "/api/admin/stats", None), ("get", "/api/admin/users", None), ("get", "/api/admin/signup", None),
                 ("get", "/api/admin/invites", None), ("get", "/api/admin/audit", None),
                 ("get", "/api/admin/plaintext-audit", None),
                 ("put", f"/api/admin/users/{bob.id}/membership", {"exempt": True}),
                 ("post", f"/api/admin/users/{bob.id}/grants", {"kind": "free_period", "days": 3, "note": "thanks"}),
                 ("post", f"/api/admin/users/{bob.id}/disable", None), ("post", f"/api/admin/users/{bob.id}/enable", None),
                 ("post", f"/api/admin/users/{bob.id}/mfa/disable", None),
                 ("post", f"/api/admin/users/{bob.id}/reset-email", None),
                 ("post", f"/api/admin/users/{bob.id}/regrant", None),
                 ("post", f"/api/admin/users/{bob.id}/role", {"role": "user"}),
                 ("post", "/api/admin/invites", {"email": "someone@example.invalid"}), ("get", "/api/admin/audit", None)]
        seen = []
        for method, path, body in calls:
            r = getattr(c, method)(path, **({"json": body} if body else {}))
            seen.append(r.text)
        joined = "\n".join(seen)
        for marker in self.MARKERS:
            self.assertNotIn(marker, joined)
        # ... and they really are in Bob's data (the test would pass vacuously otherwise).
        b = self.signed_in(bob)  # (the calls above turned the account off and on, which signs it out)
        self.assertIn("QZXJOBTITLE", b.get("/api/jobs").text)
        self.assertEqual(b.get("/api/workspaces").json()[0]["name"], "QZXWORKSPACE search")
        # Admin routes don't take a workspace: the header is ignored, not honoured.
        r = c.get("/api/admin/users", headers={"X-Workspace": str(bob.workspace_id)})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("QZXWORKSPACE", r.text)

    def test_admin_key_scope_cannot_open_anothers_data(self):
        bob = helpers.make_user()
        with helpers.as_account(bob):
            db = SessionLocal()
            db.add(Document(workspace_id=bob.workspace_id, filename="x", text="QZXDOCTEXT", filetype=".txt"))
            db.commit()
            db.close()
        with helpers.as_admin():
            db = SessionLocal()
            with self.assertRaises(crypto.KeyMismatch):
                db.query(Document).filter(Document.workspace_id == bob.workspace_id).first().text
            db.close()


# ---------------------------------------------------------------------------
# Roles and the admin key
# ---------------------------------------------------------------------------

class RoleTest(AdminTestCase):
    def read_as(self, account) -> dict:
        """Every person's {email} as this admin reads them."""
        c = helpers.client(account.email)
        with c:
            out = c.get("/api/admin/users").json()
            self.assertTrue(out["admin_key_available"])
            return {u["id"]: u["email"] for u in out["users"]}

    def test_promote_then_demote_rotates_the_key_and_remaining_admins_still_read_everything(self):
        c = self.signed_in()
        bob = helpers.make_user(name="Bob")
        first_entry = db_rows(lambda s: s.query(AdminAudit.id).order_by(AdminAudit.id.desc()).first()[0])
        carol = self.make_admin(name="Carol")             # promoted: a grant of the current key
        version = apk_version()
        self.assertEqual(db_rows(lambda s: s.get(AdminKeyGrant, carol.id).apk_version), version)
        self.assertEqual(self.read_as(carol)[bob.id], bob.email)   # Carol reads with her own grant
        # Something sealed to the key in each place: a note, an audit detail, billing details.
        self.assertEqual(c.post(f"/api/admin/users/{bob.id}/grants", json={"kind": "manual", "days": 5,
                                                                          "note": "before rotation"}).status_code, 201)
        with helpers.as_admin():
            apk_public = admin_key.current_public(SessionLocal())[0]
        db = SessionLocal()
        membership = db.get(Membership, bob.id)
        membership.billing_admin = admin_key.seal_json(apk_public, {"last4": "4242"}, admin_key.billing_label(bob.id))
        db.commit()
        db.close()
        old_public = apk_public
        r = c.post(f"/api/admin/users/{carol.id}/role", json={"role": "user"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["apk_rotated"])
        self.assertEqual(apk_version(), version + 1)
        self.assertNotEqual(db_rows(lambda s: admin_key.current_public(s)[0]), old_public)
        # Carol lost the grant (and the panel); the first admin reads everything with a new grant.
        self.assertIsNone(db_rows(lambda s: s.get(AdminKeyGrant, carol.id)))
        self.assertEqual(db_rows(lambda s: s.get(AdminKeyGrant, self.admin_id).apk_version), version + 1)
        with helpers.client(carol.email) as carol_client:
            self.assertEqual(carol_client.get("/api/admin/users").status_code, 403)
        row = self.user_row(c, bob.id)
        self.assertEqual((row["email"], row["name"]), (bob.email, "Bob"))
        self.assertEqual(row["membership"]["grants"][0]["note"], "before rotation")
        self.assertEqual(db_rows(lambda s: s.get(User, bob.id).pii_admin_version), version + 1)

        # (the new private key is reachable only through an admin's own grant: do what a request does)
        with helpers.as_admin():
            s = SessionLocal()
            apk = admin_key.open_apk(s, self.admin_id)
            self.assertEqual(admin_key.open_json(apk, s.get(Membership, bob.id).billing_admin,
                                                 admin_key.billing_label(bob.id)), {"last4": "4242"})
            s.close()
        mine = [e for e in c.get("/api/admin/audit").json()["entries"] if e["id"] > first_entry]
        self.assertIn("role_changed", [e["action"] for e in mine])  # entries made before the rotation still open
        self.assertTrue(all(e["detail"] is not None for e in mine))
        self.assertGreaterEqual(len(mine), 3)

    def test_a_failed_demotion_changes_nothing(self):
        """The new key, the re-sealed values, the grants and the role change are one transaction."""
        c = self.signed_in()
        bob = helpers.make_user(name="Bob")
        carol = self.make_admin(name="Carol")
        version = apk_version()
        public = db_rows(lambda s: admin_key.current_public(s)[0])
        before = db_rows(lambda s: {g.user_id: bytes(g.sealed_priv) for g in s.query(AdminKeyGrant)})
        pii = db_rows(lambda s: bytes(s.get(User, bob.id).pii_admin))
        with mock.patch.object(admin_key, "audit", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            c.post(f"/api/admin/users/{carol.id}/role", json={"role": "user"})
        self.assertEqual(db_rows(lambda s: s.get(User, carol.id).role), "admin")
        self.assertEqual((apk_version(), db_rows(lambda s: admin_key.current_public(s)[0])), (version, public))
        self.assertEqual(db_rows(lambda s: {g.user_id: bytes(g.sealed_priv) for g in s.query(AdminKeyGrant)}), before)
        self.assertEqual(db_rows(lambda s: bytes(s.get(User, bob.id).pii_admin)), pii)
        self.assertEqual(self.read_as(carol)[bob.id], bob.email)

    def test_two_remaining_admins_both_read_after_a_demotion(self):
        bob = helpers.make_user(name="Bob")
        carol, dave = self.make_admin(name="Carol"), self.make_admin(name="Dave")
        self.assertEqual(self.signed_in().post(f"/api/admin/users/{dave.id}/role", json={"role": "user"}).status_code, 200)
        for account in (carol, helpers.Account(self.admin_id, helpers.ADMIN_EMAIL, helpers.ADMIN_PASSWORD, 0)):
            self.assertEqual(self.read_as(account)[bob.id], bob.email, account.email)
        with helpers.client(dave.email) as d:
            self.assertEqual(d.get("/api/admin/users").status_code, 403)

    def test_the_last_admin_cannot_be_demoted_or_turned_off(self):
        c = self.signed_in()
        r = c.post(f"/api/admin/users/{self.admin_id}/role", json={"role": "user"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "last_admin"))
        r = c.post(f"/api/admin/users/{self.admin_id}/disable")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "self"))
        carol = self.make_admin()
        # A second admin who is turned off doesn't count as one that could take over.
        self.assertEqual(c.post(f"/api/admin/users/{carol.id}/disable").status_code, 200)
        r = c.post(f"/api/admin/users/{self.admin_id}/role", json={"role": "user"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "last_admin"))
        self.assertEqual(c.post(f"/api/admin/users/{carol.id}/enable").status_code, 200)
        c2 = helpers.client(carol.email)
        with c2:  # Carol can demote the first admin (herself remaining), but nobody can turn off the last one
            r = c2.post(f"/api/admin/users/{self.admin_id}/disable")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(c2.post(f"/api/admin/users/{self.admin_id}/enable").status_code, 200)

    def test_changing_a_role_needs_a_recent_password(self):
        c = self.signed_in()
        bob = helpers.make_user()
        self.make_stale()
        r = c.post(f"/api/admin/users/{bob.id}/role", json={"role": "admin"})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        self.assertEqual(db_rows(lambda s: s.get(User, bob.id).role), "user")
        for path in (f"/api/admin/users/{bob.id}/regrant", f"/api/admin/users/{bob.id}/mfa/disable",
                     "/api/admin/keys/rotate", "/api/admin/plaintext-files/delete"):
            r = c.post(path)
            self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"), path)

    def test_regrant_is_for_admins_only(self):
        bob = helpers.make_user()
        r = self.signed_in().post(f"/api/admin/users/{bob.id}/regrant")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "not_admin"))

    def test_regrant_gives_a_new_keypair_admin_access_again(self):
        c = self.signed_in()
        carol = self.make_admin()
        uid, dek = helpers.key_of(carol.email)
        priv, pub = crypto.new_keypair()
        db = SessionLocal()
        row = db.get(UserKeys, carol.id)
        with crypto.as_user(uid, dek):  # what a locked reset does: a new key pair under the (new) data key
            row.x25519_priv_enc = crypto.seal(priv, f"user_keys.x25519_priv|{carol.id}")
        row.x25519_pub = pub
        db.commit()
        db.close()
        with helpers.client(carol.email) as cc:
            self.assertFalse(cc.get("/api/admin/users").json()["admin_key_available"])
            self.assertEqual(c.post(f"/api/admin/users/{carol.id}/regrant").status_code, 200)
            self.assertTrue(cc.get("/api/admin/users").json()["admin_key_available"])

    def test_an_admin_without_the_key_cannot_promote_or_demote(self):
        carol = self.make_admin()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM admin_key_grants WHERE user_id = :i"), {"i": carol.id})
        bob = helpers.make_user()
        with helpers.client(carol.email) as cc:
            r = cc.post(f"/api/admin/users/{bob.id}/role", json={"role": "admin"})
            self.assertEqual((r.status_code, r.json()["code"]), (409, "admin_key_missing"))
            r = cc.get("/api/admin/users")
            self.assertFalse(r.json()["admin_key_available"])
            self.assertTrue(all(u["email"] is None for u in r.json()["users"]))  # no details without the key


class NewAdminKeyTest(AdminTestCase):
    def test_new_admin_key_marks_details_stale_and_they_heal_at_sign_in(self):
        c = self.signed_in()
        bob = helpers.make_user(name="Bob Heal")
        carol = helpers.make_user(name="Carol Heal")
        bg = users.create_user(f"heal-bg-{os.getpid()}@example.invalid", PASSWORD, name="Back Ground", background=True)
        before = apk_version()
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(keys.main(["new-admin-key", "--email", f"  {carol.email.upper()}  "]), 0)
        self.addCleanup(self.demote_quietly, carol.id)
        self.assertEqual(apk_version(), before + 1)
        self.assertEqual(db_rows(lambda s: s.get(User, carol.id).role), "admin")
        # Everyone is stale, except those the server key could re-seal at once (background access).
        self.assertEqual(db_rows(lambda s: s.get(User, bob.id).pii_admin_version), before)
        self.assertEqual(db_rows(lambda s: s.get(User, bg.user_id).pii_admin_version), before + 1)
        # The old admin was given the new key too; reading shows the stale rows as unavailable.
        row = self.user_row(c, bob.id)
        self.assertEqual((row["details_available"], row["details_note"]), (False, "Details unavailable until this person signs in"))
        self.assertTrue(self.user_row(c, bg.user_id)["details_available"])
        # Signing in re-seals Bob's details from his own copy.
        self.signed_in(bob)
        row = self.user_row(c, bob.id)
        self.assertEqual((row["email"], row["name"]), (bob.email, "Bob Heal"))
        # Carol (not signed in since) heals at her next sign-in as well, and can read everything.
        cc = self.signed_in(carol)
        self.assertEqual(self.user_row(cc, carol.id)["email"], carol.email)
        entry = c.get("/api/admin/audit").json()["entries"]
        self.assertIn("admin_key_replaced", [e["action"] for e in entry])

    def test_unknown_address_changes_nothing(self):
        version = apk_version()
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(keys.main(["new-admin-key", "--email", "nobody@example.invalid"]), 2)
        self.assertIn("No account", err.getvalue())
        self.assertEqual(apk_version(), version)


# ---------------------------------------------------------------------------
# Disable, MFA, reset
# ---------------------------------------------------------------------------

class AccountActionsTest(AdminTestCase):
    def test_disable_signs_them_out_and_enable_lets_them_back(self):
        c = self.signed_in()
        bob = helpers.make_user()
        b = self.signed_in(bob)
        self.assertEqual(b.get("/api/auth/me").status_code, 200)
        r = c.post(f"/api/admin/users/{bob.id}/disable")
        self.assertEqual((r.status_code, r.json()["user"]["status"]), (200, "disabled"))
        self.assertEqual(b.get("/api/auth/me").status_code, 401)
        self.assertEqual(db_rows(lambda s: s.query(AuthSession).filter(AuthSession.user_id == bob.id).count()), 0)
        login = self.anonymous().post("/api/auth/login", json={"email": bob.email, "password": bob.password})
        self.assertEqual((login.status_code, login.json()["code"]), (403, "disabled"))
        self.assertEqual(c.post(f"/api/admin/users/{bob.id}/enable").json()["user"]["status"], "active")
        self.assertEqual(self.anonymous().post("/api/auth/login", json={"email": bob.email, "password": bob.password}).status_code, 200)

    def test_mfa_off_clears_the_secrets_and_signs_them_out(self):
        c = self.signed_in()
        bob = helpers.make_user()
        b = self.signed_in(bob)  # (signing in with two-step on arrives with Phase E)
        server = users.server_keys()
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET mfa_enabled = 1, totp_secret_enc = :s, totp_pending_enc = :p, "
                              "totp_last_step = 77 WHERE id = :i"),
                         {"s": server.kek_wrap(b"s" * 20, f"totp|{bob.id}")[1], "p": server.kek_wrap(b"p" * 20, f"totp|{bob.id}")[1],
                          "i": bob.id})
        with mock.patch.object(admin, "notify_mfa_disabled", return_value=False) as notice:
            r = c.post(f"/api/admin/users/{bob.id}/mfa/disable")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["user"]["mfa"], r.json()["notice_sent"]), (False, False))
        notice.assert_called_once()  # the call point Phase H fills in
        row = db_rows(lambda s: s.get(User, bob.id))
        self.assertEqual((row.mfa_enabled, row.totp_secret_enc, row.totp_pending_enc, row.totp_last_step),
                         (False, None, None, None))
        self.assertEqual(b.get("/api/auth/me").status_code, 401)
        self.assertEqual(c.post(f"/api/admin/users/{self.admin_id}/mfa/disable").json()["code"], "self")

    def test_reset_email_waits_for_email(self):
        c = self.signed_in()
        bob = helpers.make_user()
        r = c.post(f"/api/admin/users/{bob.id}/reset-email")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "email_not_set_up"))
        self.assertTrue(r.json()["detail"].startswith("Email isn't set up yet"))


class MembershipTest(AdminTestCase):
    def test_exempt_free_searches_and_grants_are_stored_with_sealed_notes(self):
        c = self.signed_in()
        bob = helpers.make_user()
        r = c.put(f"/api/admin/users/{bob.id}/membership", json={"exempt": True, "free_searches_used": 3})
        m = r.json()["user"]["membership"]
        self.assertEqual((m["exempt"], m["free_searches_used"]), (True, 3))
        self.assertEqual(c.put(f"/api/admin/users/{bob.id}/membership", json={}).status_code, 422)
        self.assertEqual(c.put(f"/api/admin/users/{bob.id}/membership", json={"free_searches_used": -1}).status_code, 422)
        r = c.post(f"/api/admin/users/{bob.id}/grants", json={"kind": "free_period", "days": 14, "note": "launch gift"})
        self.assertEqual(r.status_code, 201, r.text)
        grant = r.json()["grant"]
        self.assertEqual((grant["kind"], grant["note"], grant["active"]), ("free_period", "launch gift", True))
        raw = db_rows(lambda s: s.get(MembershipGrant, grant["id"]).note_admin)
        self.assertNotIn(b"launch gift", raw)  # sealed to the admin key
        start = datetime.fromisoformat(grant["starts_at"][:-1])
        end = datetime.fromisoformat(grant["ends_at"][:-1])
        self.assertEqual((end - start).days, 14)
        # Dates; a bad period; both end and days; neither.
        r = c.post(f"/api/admin/users/{bob.id}/grants", json={"kind": "manual", "starts_at": "2030-01-01",
                                                              "ends_at": "2030-02-01T00:00:00Z"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertFalse(r.json()["grant"]["active"])
        for body in ({"kind": "manual", "starts_at": "2030-02-01", "ends_at": "2030-01-01"},
                     {"kind": "manual", "days": 3, "ends_at": "2031-01-01"}, {"kind": "manual"},
                     {"kind": "manual", "days": 3, "ends_at": "garbage"}, {"kind": "other", "days": 3}):
            self.assertEqual(c.post(f"/api/admin/users/{bob.id}/grants", json=body).status_code, 422, body)
        # Change, end, and the history stays.
        gid = grant["id"]
        r = c.patch(f"/api/admin/users/{bob.id}/grants/{gid}", json={"days": 30, "note": "extended"})
        self.assertEqual((r.json()["grant"]["note"],), ("extended",))
        self.assertEqual((datetime.fromisoformat(r.json()["grant"]["ends_at"][:-1]) - start).days, 30)
        self.assertEqual(c.patch(f"/api/admin/users/{bob.id}/grants/{gid}", json={"note": ""}).json()["grant"]["note"], None)
        r = c.delete(f"/api/admin/users/{bob.id}/grants/{gid}")
        self.assertEqual((r.json()["grant"]["active"], r.json()["grant"]["revoked_at"] is not None), (False, True))
        self.assertEqual(c.patch(f"/api/admin/users/{bob.id}/grants/{gid}", json={"days": 3}).status_code, 409)
        other = helpers.make_user()  # a grant belongs to the person in the path
        self.assertEqual(c.delete(f"/api/admin/users/{other.id}/grants/{gid}").status_code, 404)
        shown = self.user_row(c, bob.id)["membership"]
        self.assertEqual(shown["active_grants"], 0)
        self.assertEqual(len(shown["grants"]), 2)
        self.assertEqual(db_rows(lambda s: s.query(MembershipGrant).filter(MembershipGrant.user_id == bob.id).count()), 2)


class UpgradeTest(unittest.TestCase):
    def test_an_older_database_gets_the_usage_user_column(self):
        out = helpers.scenario("""
            from sqlalchemy import text
            from backend.db import engine, init_db
            with engine.begin() as conn:  # llm_usage as the version before accounts made it
                conn.execute(text("CREATE TABLE llm_usage (id INTEGER PRIMARY KEY, created_at DATETIME, "
                                  "workspace_id INTEGER, purpose VARCHAR, provider VARCHAR, model VARCHAR, "
                                  "input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, "
                                  "cache_write_tokens INTEGER)"))
                conn.execute(text("INSERT INTO llm_usage (created_at, model) VALUES (CURRENT_TIMESTAMP, 'old')"))
            init_db()
            with engine.connect() as conn:
                out["columns"] = [r[1] for r in conn.execute(text("PRAGMA table_info(llm_usage)"))]
                out["rows"] = conn.execute(text("SELECT COUNT(*), COUNT(user_id) FROM llm_usage")).one()[:]
        """)
        self.assertIn("user_id", out["columns"])
        self.assertEqual(out["rows"], [1, 0])


# ---------------------------------------------------------------------------
# Every admin route refuses non-admins
# ---------------------------------------------------------------------------

class NonAdminTest(AdminTestCase):
    def test_every_admin_route_is_403_for_an_ordinary_user(self):
        bob = helpers.make_user()
        b = self.signed_in(bob)
        anonymous = self.anonymous()
        seen = 0
        for route in admin.router.routes:
            path = route.path.replace("{user_id}", str(self.admin_id)).replace("{grant_id}", "1") \
                .replace("{invite_id}", "1")
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                seen += 1
                r = b.request(method, path, json={})
                self.assertEqual(r.status_code, 403, f"{method} {path}: {r.text}")
                self.assertEqual(anonymous.request(method, path, json={}).status_code, 401, f"{method} {path}")
        self.assertGreaterEqual(seen, 20)
        # And the state didn't change: the first admin is still an admin, with their grant.
        self.assertEqual(db_rows(lambda s: s.get(User, self.admin_id).role), "admin")

    def test_features_say_admin_panel_for_admins_only(self):
        self.assertTrue(self.signed_in().get("/api/auth/me").json()["features"]["admin_panel"])
        b = self.signed_in(helpers.make_user())
        self.assertFalse(b.get("/api/auth/me").json()["features"]["admin_panel"])
        self.assertFalse(b.get("/api/account/onboarding").json()["features"]["admin_panel"])


# ---------------------------------------------------------------------------
# Sign-up
# ---------------------------------------------------------------------------

class SignupTest(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.set_mode, "invite")

    def set_mode(self, mode: str, free: int | None = None) -> None:
        c = helpers.client()
        with c:
            body = {"mode": mode} if free is None else {"mode": mode, "free_searches": free}
            self.assertEqual(c.put("/api/admin/signup", json=body).status_code, 200)

    def new_email(self) -> str:
        return f"signup-{os.urandom(4).hex()}@example.invalid"

    def sign_up(self, client, **fields):
        body = {"email": self.new_email(), "password": PASSWORD, **fields}
        return client.post("/api/auth/signup", json=body)

    def test_settings_default_to_invite_and_are_validated_and_audited(self):
        c = self.signed_in()
        self.assertEqual(c.get("/api/admin/signup").json(), {"mode": "invite", "free_searches": 3})
        self.assertEqual(c.put("/api/admin/signup", json={"mode": "open", "free_searches": 5}).json(),
                         {"mode": "open", "free_searches": 5})
        for body in ({}, {"mode": "wide open"}, {"free_searches": -1}, {"free_searches": 10_000}, {"mode": "open", "x": 1}):
            self.assertEqual(c.put("/api/admin/signup", json=body).status_code, 422, body)
        self.assertEqual(c.get("/api/admin/signup").json()["mode"], "open")
        self.assertIn("signup_changed", [e["action"] for e in c.get("/api/admin/audit").json()["entries"]])
        self.set_mode("invite", 3)

    def test_state_reports_the_mode_and_whether_an_invite_works(self):
        anon = self.anonymous()
        state = anon.get("/api/auth/state").json()
        self.assertEqual(state["signup"], {"mode": "invite", "allowed": False, "encrypting": False,
                                           "invite": {"valid": False, "email": None}})
        link = self.signed_in().post("/api/admin/invites", json={"email": "Pat@Example.invalid"}).json()
        token = link["link"].split("invite=")[1]
        got = anon.get(f"/api/auth/state?invite={token}").json()["signup"]
        self.assertEqual((got["allowed"], got["invite"]), (True, {"valid": True, "email": "Pat@Example.invalid"}))
        bad = anon.get("/api/auth/state?invite=nope").json()["signup"]
        self.assertEqual((bad["allowed"], bad["invite"]["valid"]), (False, False))
        self.set_mode("open")
        self.assertTrue(anon.get("/api/auth/state").json()["signup"]["allowed"])
        self.set_mode("closed")
        self.assertEqual(anon.get(f"/api/auth/state?invite={token}").json()["signup"]["allowed"], False)

    def test_invite_mode_needs_a_valid_single_use_invitation(self):
        admin_client = self.signed_in()
        anon = self.anonymous()
        r = self.sign_up(anon)
        self.assertEqual((r.status_code, r.json()["code"]), (403, "invite_needed"))
        r = self.sign_up(anon, invite="not-a-token")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "invite_invalid"))
        made = admin_client.post("/api/admin/invites", json={}).json()
        self.assertTrue(made["link"].startswith("http://testserver/signup.html?invite="))
        token = made["link"].split("invite=")[1]
        stored = db_rows(lambda s: s.query(AuthToken).filter(AuthToken.id == made["id"]).one())
        self.assertNotIn(token.encode(), bytes(stored.token_hash) + (stored.payload_enc or b""))  # only a hash is stored
        self.assertEqual(stored.token_hash, tokens.hash_token(token))
        self.assertLess(timedelta(days=7) - (stored.expires_at - stored.created_at), timedelta(minutes=1))
        email = self.new_email()
        r = self.sign_up(anon, email=email, invite=token, name="Newcomer", remember=True)
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(len(body["recovery_codes"]), 8)
        self.assertIn("jh_session", anon.cookies)
        me = anon.get("/api/auth/me").json()
        self.assertEqual((me["user"]["email"], me["user"]["name"], me["user"]["role"]), (email, "Newcomer", "user"))
        self.assertTrue(me["recovery_codes_pending"])
        self.assertEqual(me["onboarding_step"], 1)
        self.assertEqual(len(me["workspaces"]), 1)
        self.assertFalse(me["features"]["admin_panel"])
        created = db_rows(lambda s: s.get(User, me["user"]["id"]))
        self.assertEqual((created.created_via, created.email_verified), ("invite", False))
        self.assertEqual(db_rows(lambda s: s.get(User, me["user"]["id"]).pii_admin_version), apk_version())
        # Used up.
        again = self.sign_up(self.anonymous(), invite=token)
        self.assertEqual((again.status_code, again.json()["code"]), (403, "invite_invalid"))
        row = next(i for i in admin_client.get("/api/admin/invites").json()["invites"] if i["id"] == made["id"])
        self.assertEqual((row["status"], row["used_by"]), ("used", me["user"]["id"]))
        # The admin sees the new person with their details.
        self.assertEqual(self.user_row(admin_client, me["user"]["id"])["email"], email)

    def test_an_invitation_for_this_address_counts_as_verified(self):
        c = self.signed_in()
        email = self.new_email()
        token = c.post("/api/admin/invites", json={"email": email}).json()["link"].split("invite=")[1]
        r = self.sign_up(self.anonymous(), email=email.upper(), invite=token)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(db_rows(lambda s: users.find_by_email(s, email).email_verified))

    def test_expired_and_cancelled_invitations_are_refused_and_a_failed_signup_keeps_the_invite(self):
        c = self.signed_in()
        anon = self.anonymous()
        old = c.post("/api/admin/invites", json={}).json()
        with engine.begin() as conn:
            conn.execute(text("UPDATE auth_tokens SET expires_at = :t WHERE id = :i"),
                         {"t": utcnow() - timedelta(minutes=1), "i": old["id"]})
        r = self.sign_up(anon, invite=old["link"].split("invite=")[1])
        self.assertEqual((r.status_code, r.json()["code"]), (403, "invite_invalid"))
        self.assertEqual(next(i for i in c.get("/api/admin/invites").json()["invites"] if i["id"] == old["id"])["status"], "expired")
        cancelled = c.post("/api/admin/invites", json={}).json()
        self.assertEqual(c.delete(f"/api/admin/invites/{cancelled['id']}").status_code, 200)
        self.assertEqual(self.sign_up(anon, invite=cancelled["link"].split("invite=")[1]).status_code, 403)
        self.assertEqual(c.delete(f"/api/admin/invites/{cancelled['id']}").status_code, 404)
        # A bad password or a taken address doesn't use the invitation up.
        fresh = c.post("/api/admin/invites", json={}).json()["link"].split("invite=")[1]
        r = self.sign_up(anon, invite=fresh, password="short")
        self.assertEqual((r.status_code, r.json()["field"]), (422, "password"))
        taken = helpers.make_user()
        r = self.sign_up(anon, invite=fresh, email=taken.email)
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (409, "email_taken", "email"))
        self.assertEqual(r.json()["detail"], "An account with this email already exists. Sign in or reset your password.")
        self.assertEqual(self.sign_up(anon, invite=fresh).status_code, 201)

    def test_open_mode_needs_no_invitation_and_closed_mode_allows_nobody(self):
        anon = self.anonymous()
        self.set_mode("open")
        r = self.sign_up(anon)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(db_rows(lambda s: s.get(User, anon.get("/api/auth/me").json()["user"]["id"]).created_via), "open")
        # (a link that no longer works doesn't get in the way)
        self.assertEqual(self.sign_up(self.anonymous(), invite="stale-link").status_code, 201)
        self.set_mode("closed")
        link = self.signed_in().post("/api/admin/invites", json={}).json()["link"].split("invite=")[1]
        for invite in (None, link):
            r = self.sign_up(self.anonymous(), invite=invite)
            self.assertEqual((r.status_code, r.json()["code"]), (403, "signup_closed"))

    def test_signup_checks_what_login_checks(self):
        self.set_mode("open")
        anon = self.anonymous()
        r = anon.post("/api/auth/signup", data={"email": self.new_email(), "password": PASSWORD})  # a form post
        self.assertEqual(r.status_code, 415)
        r = anon.post("/api/auth/signup", json={"email": self.new_email(), "password": PASSWORD},
                      headers={"Origin": "https://evil.example"})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "bad_origin"))
        r = anon.post("/api/auth/signup", json={"email": "nope", "password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["field"]), (422, "email"))
        r = anon.post("/api/auth/signup", json={"email": self.new_email(), "password": PASSWORD, "bogus": 1})
        self.assertEqual(r.status_code, 422)
        # Per address: after ten tries within five minutes, 429.
        ratelimit.reset_all()
        codes = [anon.post("/api/auth/signup", json={"email": "nope", "password": PASSWORD}).status_code for _ in range(12)]
        self.assertEqual(codes[:10], [422] * 10)
        self.assertEqual(codes[10:], [429, 429])

    def test_signups_wait_while_the_encryption_runs(self):
        self.set_mode("open")
        anon = self.anonymous()
        with mock.patch.object(migrate_encryption, "is_done", return_value=False):
            r = self.sign_up(anon)
            self.assertEqual((r.status_code, r.json()["code"]), (423, "encrypting"))
            state = anon.get("/api/auth/state").json()["signup"]
            self.assertEqual((state["allowed"], state["encrypting"]), (False, True))
        self.assertEqual(self.sign_up(anon).status_code, 201)

    def test_audit_log_keeps_sealed_details_for_admins(self):
        c = self.signed_in()
        c.post("/api/admin/invites", json={"email": "audit-me@example.invalid"})
        raw = db_rows(lambda s: [bytes(r.detail_admin) for r in s.query(AdminAudit).filter(AdminAudit.action == "invite_created")])
        self.assertTrue(raw)
        self.assertTrue(all(b"audit-me" not in blob for blob in raw))
        out = c.get("/api/admin/audit?limit=5").json()
        first = out["entries"][0]
        self.assertEqual((first["action"], first["admin"]["id"], first["admin"]["email"]),
                         ("invite_created", self.admin_id, helpers.ADMIN_EMAIL))
        self.assertEqual(first["detail"]["email"], "audit-me@example.invalid")
        self.assertEqual(len(out["entries"]), 5)
        older = c.get(f"/api/admin/audit?limit=2&before_id={first['id']}").json()
        self.assertTrue(all(e["id"] < first["id"] for e in older["entries"]))
        listed = next(i for i in c.get("/api/admin/invites").json()["invites"] if i["status"] == "pending")
        self.assertEqual(listed["email"], "audit-me@example.invalid")


# ---------------------------------------------------------------------------
# Plaintext clean-up
# ---------------------------------------------------------------------------

class PlaintextTest(AdminTestCase):
    @staticmethod
    def delete_plain_rows():
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM documents WHERE filename = 'plain.txt'"))

    def test_audit_lists_leftovers_and_non_blob_values_and_delete_removes_files(self):
        c = self.signed_in()
        out = c.get("/api/admin/plaintext-audit").json()
        self.assertEqual(out["encryption"]["state"], "done")
        self.assertEqual((out["columns"], out["files"]), ({}, []))
        backup = DB_PATH.parent / f"{DB_PATH.name}.bak-before-test"
        backup.write_bytes(b"old plaintext")
        self.addCleanup(lambda: backup.unlink() if backup.exists() else None)
        bob = helpers.make_user()
        with engine.begin() as conn:  # a plaintext value in a sealed column (somebody wrote the database directly)
            conn.execute(text("INSERT INTO documents (workspace_id, filename, filetype, use_for_scoring, use_concise, "
                              "created_at) VALUES (:w, 'plain.txt', '.txt', 1, 0, CURRENT_TIMESTAMP)"),
                         {"w": bob.workspace_id})
        self.addCleanup(self.delete_plain_rows)
        out = c.get("/api/admin/plaintext-audit").json()
        self.assertEqual(out["columns"], {"documents.filename": 1})
        self.assertFalse(out["clean"])
        self.assertIn(backup.name, [i["path"] for i in out["leftovers"]])
        self.assertNotIn("_abs", json.dumps(out))
        self.delete_plain_rows()
        r = c.post("/api/admin/plaintext-files/delete")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(backup.name, r.json()["deleted"])
        self.assertFalse(backup.exists())
        self.assertEqual(r.json()["leftovers"], [])
        self.assertTrue(c.get("/api/admin/plaintext-audit").json()["clean"])
        with mock.patch.object(migrate_encryption, "is_done", return_value=False):
            self.assertEqual(c.post("/api/admin/plaintext-files/delete").json()["code"], "encrypting")


# ---------------------------------------------------------------------------
# Server key rotation
# ---------------------------------------------------------------------------

class KekRotationTest(AdminTestCase):
    def background_user(self):
        return users.create_user(f"rot-{os.urandom(4).hex()}@example.invalid", PASSWORD, background=True)

    def kek_ids(self, user_id: int):
        row = db_rows(lambda s: s.get(UserKeys, user_id))
        return row.server_kek_id, users.server_keys().kek_id_of(row.server_wrapped_dek)

    def test_rotation_rewraps_every_server_wrapped_value(self):
        c = self.signed_in()
        a, b = self.background_user(), self.background_user()
        server = users.server_keys()
        plain = helpers.make_user()
        secret, pending = b"S" * 20, b"P" * 20
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET totp_secret_enc = :s, totp_pending_enc = :p WHERE id = :i"),
                         {"s": server.kek_wrap(secret, f"totp|{plain.id}")[1], "p": server.kek_wrap(pending, f"totp|{plain.id}")[1],
                          "i": plain.id})
        old_id = server.current_kek_id
        r = c.post("/api/admin/keys/rotate")
        self.assertEqual(r.status_code, 200, r.text)
        new_id = r.json()["key"]
        self.assertNotEqual(new_id, old_id)
        self.assertGreaterEqual(r.json()["rewrapped"], 4)
        self.assertEqual(users.server_keys().kek_ids, [new_id])  # the old key is gone from the file
        self.assertEqual(keys.ServerKeys.load().kek_ids, [new_id])
        for acct in (a, b):
            self.assertEqual(self.kek_ids(acct.user_id), (new_id, new_id))
            self.assertEqual(db_rows(lambda s: users.background_key(s, acct.user_id)), acct.dek)
        row = db_rows(lambda s: s.get(User, plain.id))
        self.assertEqual(server.kek_unwrap(row.totp_secret_enc, f"totp|{plain.id}"), secret)
        self.assertEqual(server.kek_unwrap(row.totp_pending_enc, f"totp|{plain.id}"), pending)
        self.assertEqual(server.kek_id_of(row.totp_secret_enc), new_id)
        self.assertIn("server_key_rotated", [e["action"] for e in c.get("/api/admin/audit").json()["entries"]])

    def test_rows_still_under_the_old_key_after_a_crash_are_rewrapped_at_startup(self):
        a = self.background_user()
        server = users.server_keys()
        old_id = server.current_kek_id
        server.start_kek_rotation()  # ... and the process dies here: the file has both keys, the rows have the old one
        self.assertTrue(keys.ServerKeys.load().kek_rotation_pending)
        self.assertEqual(self.kek_ids(a.user_id)[1], old_id)
        with TestClient(app):  # a start-up: users.startup() finishes the rotation first
            pass
        new_id = users.server_keys().current_kek_id
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(self.kek_ids(a.user_id), (new_id, new_id))
        self.assertEqual(keys.ServerKeys.load().kek_ids, [new_id])
        self.assertEqual(db_rows(lambda s: users.background_key(s, a.user_id)), a.dek)

    def test_a_crash_after_the_rows_were_stored_only_drops_the_old_key(self):
        a = self.background_user()
        server = users.server_keys()
        server.start_kek_rotation()
        rows, seen = key_rotation._rows_factory(server)
        todo = []
        for row in rows():  # step 2 stored everything but the process died before the file was pruned
            secret = server.kek_unwrap(row.blob, row.label)
            todo.append((row.ref, *server.kek_wrap(secret, row.label)))
        key_rotation._store_factory(seen)(todo)
        self.assertTrue(server.kek_rotation_pending)
        self.assertEqual(key_rotation.finish_pending(server), 0)
        self.assertFalse(server.kek_rotation_pending)
        self.assertEqual(db_rows(lambda s: users.background_key(s, a.user_id)), a.dek)

    def test_a_wrap_not_yet_committed_holds_the_rotation_back(self):
        """A wrap made before a rotation but committed after it would sit under a dropped key for good."""
        plain = helpers.make_user()
        server = users.server_keys()
        secret = "JBSWY3DPEHPK3PXP"
        wrapped, release, result = threading.Event(), threading.Event(), {}

        def writer():  # what users.enable_totp does
            db = SessionLocal()
            try:
                with server.wrapping():
                    blob = users.seal_totp(plain.id, secret)
                    wrapped.set()
                    release.wait(10)
                    db.query(User).filter(User.id == plain.id).update({User.totp_secret_enc: blob})
                    db.commit()
            finally:
                db.close()

        def rotator():
            result["moved"] = key_rotation.rotate(server)

        w = threading.Thread(target=writer)
        w.start()
        self.assertTrue(wrapped.wait(10))
        r = threading.Thread(target=rotator)
        r.start()
        r.join(0.5)
        self.assertTrue(r.is_alive(), "the rotation waits for the wrap's commit")
        release.set()
        w.join(10)
        r.join(30)
        self.assertEqual(server.kek_ids, [server.current_kek_id])
        blob = db_rows(lambda s: bytes(s.get(User, plain.id).totp_secret_enc))
        self.assertEqual(users.open_totp(plain.id, blob), secret)
        self.assertEqual(server.kek_id_of(blob), server.current_kek_id)

    def test_the_users_wrap_paths_hold_the_lock_until_they_commit(self):
        a = users.create_user(f"hold-{os.urandom(4).hex()}@example.invalid", PASSWORD)
        seen = []
        real = keys.ServerKeys.wrapping

        def spy(self):
            seen.append(1)
            return real(self)

        with mock.patch.object(keys.ServerKeys, "wrapping", spy):
            db = SessionLocal()
            try:
                users.enable_background(db, a.user_id, a.dek)
                users.create_user(f"hold-{os.urandom(4).hex()}@example.invalid", PASSWORD, background=True)
                user = db.get(User, a.user_id)
                users.start_totp_enrolment(db, user)
                secret = users.open_totp(a.user_id, user.totp_pending_enc)
                self.assertTrue(users.enable_totp(db, user, totp.code_at(secret, totp.current_step())))
            finally:
                db.close()
        self.assertEqual(len(seen), 4)

    def test_rotation_leaves_no_old_copy_in_the_log(self):
        a = self.background_user()
        old = db_rows(lambda s: bytes(s.get(UserKeys, a.user_id).server_wrapped_dek))
        key_rotation.rotate(users.server_keys())
        self.assertNotEqual(db_rows(lambda s: bytes(s.get(UserKeys, a.user_id).server_wrapped_dek)), old)
        for suffix in ("", "-wal"):  # raw bytes, no checkpoint first
            path = DB_PATH.with_name(DB_PATH.name + suffix)
            if path.exists():
                self.assertNotIn(old, path.read_bytes(), f"the retired key's copy is in {path.name}")

    def test_the_command_line_uses_the_same_code(self):
        a = self.background_user()
        old_id = users.server_keys().current_kek_id
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(keys.main(["rotate-kek"]), 0)
        self.assertIn("Done", out.getvalue())
        fresh = keys.ServerKeys.load()
        self.assertNotEqual(fresh.current_kek_id, old_id)
        self.assertEqual(fresh.kek_ids, [fresh.current_kek_id])
        users._server_keys = fresh  # (the command ran in this process on the same file)
        self.assertEqual(db_rows(lambda s: users.background_key(s, a.user_id)), a.dek)


# ---------------------------------------------------------------------------
# AI error messages
# ---------------------------------------------------------------------------

class ErrorHygieneTest(AdminTestCase):
    LLM_ENV = {"LLM_API_KEY": "sk-secret-key-1234", "LLM_BASE_URL": "https://llm.example.com/v1",
               "LLM_MODEL": "model-name-xyz"}
    NONE_ENV = {"LLM_MODEL": "", "LLM_BASE_URL": "", "LLM_API_KEY": ""}

    def job_for(self, account) -> int:
        with helpers.as_account(account):
            db = SessionLocal()
            job = Job(workspace_id=account.workspace_id, url=f"https://hygiene.example/{os.urandom(3).hex()}",
                      origin="private", title="Analyst", company="Acme", description="Do things", status="to_review")
            db.add(job)
            db.commit()
            job_id = job.id
            db.close()
        return job_id

    def rejected(self, status=401):
        response = mock.Mock(status_code=status, text="invalid api key for https://llm.example.com/v1 sk-secret-key-1234",
                             headers={})
        return mock.patch.object(llm_providers.httpx, "post", return_value=response)

    def test_ordinary_users_get_a_plain_sentence_and_admins_keep_the_detail(self):
        bob = helpers.make_user()
        b = self.signed_in(bob)
        a = self.signed_in()
        job = self.job_for(bob)
        admin_job = self.job_for(helpers.Account(self.admin_id, helpers.ADMIN_EMAIL, "", self.admin_workspace()))
        with mock.patch.dict(os.environ, self.LLM_ENV), self.rejected(), mock.patch.object(llm_providers.time, "sleep"):
            r = b.post(f"/api/score/{job}", headers={"X-Workspace": str(bob.workspace_id)})
            self.assertEqual((r.status_code, r.json()["detail"]), (502, llm_providers.USER_PROBLEM))
            for hidden in ("llm.example.com", "sk-secret", "401", "rejected"):
                self.assertNotIn(hidden, r.text)
            stored = b.get(f"/api/jobs/{job}", headers={"X-Workspace": str(bob.workspace_id)}).text
            self.assertNotIn("llm.example.com", stored)
            self.assertIn(llm_providers.USER_PROBLEM, stored)  # what is kept on the job is the plain sentence too
            r = a.post(f"/api/score/{admin_job}", headers={"X-Workspace": str(self.admin_workspace())})
            self.assertEqual(r.status_code, 502)
            self.assertIn("llm.example.com", r.json()["detail"])  # the admin keeps the provider's address and answer

    def test_unset_up_reads_the_same_for_users_and_gives_admins_the_setting(self):
        bob = helpers.make_user()
        b, a = self.signed_in(bob), self.signed_in()
        job = self.job_for(bob)
        with mock.patch.dict(os.environ, self.NONE_ENV):
            r = b.post(f"/api/score/{job}", headers={"X-Workspace": str(bob.workspace_id)})
            self.assertEqual((r.status_code, r.json()["detail"]), (400, llm_providers.USER_NOT_SET_UP))
            from backend.scorer import config_problem
            with helpers.as_admin():
                self.assertIn("in the admin panel (Server > AI provider)", config_problem())
            with helpers.as_account(bob):
                self.assertEqual(config_problem(), llm_providers.USER_NOT_SET_UP)
            self.assertEqual(config_problem(), llm_providers.USER_NOT_SET_UP)  # no key scope: fail closed
        from backend import app as app_module
        self.assertEqual(app_module.NOT_SET_UP, llm_providers.USER_NOT_SET_UP)

    def test_web_search_problem_and_run_errors_hide_the_setup_from_users(self):
        bob = helpers.make_user()
        cfg = {"base_url": "https://api.deepseek.com/v1", "model": "m"}
        with helpers.as_account(bob):
            plain = llm_providers.web_search_problem(cfg, "Company research")
            self.assertNotIn("DeepSeek", plain)
            self.assertNotIn("Settings", plain)
            self.assertEqual(llm_providers.user_message(llm_providers.ScorerError("HTTP 401 at https://x.example")),
                             llm_providers.USER_PROBLEM)
        with helpers.as_admin():
            self.assertIn("admin panel (Server, AI provider)", llm_providers.web_search_problem(cfg, "Company research"))
            self.assertIn("https://x.example", llm_providers.user_message(llm_providers.ScorerError("at https://x.example")))
        from backend import tasks

        def failing(progress):
            raise llm_providers.ScorerError("HTTP 401: API key rejected by https://llm.example.com/v1", auth=True)

        with helpers.as_account(bob):
            run = tasks.start("search", failing, bob.workspace_id)
            import time
            for _ in range(100):
                latest = tasks._full(run["id"])
                if latest["state"] != "queued" and latest["state"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(latest["state"], "failed")
        self.assertNotIn("llm.example.com", json.dumps(latest))

    def admin_workspace(self) -> int:
        return users.workspace_ids(SessionLocal(), self.admin_id)[0]


# ---------------------------------------------------------------------------
# Deleting an account
# ---------------------------------------------------------------------------

class DeleteUserTest(AdminTestCase):
    def plant(self, account) -> str:
        """A document with an upload, a job and some usage for ``account``; the upload's stored name."""
        with helpers.as_account(account):
            stored = docs.save_upload(b"upload of " + account.email.encode())
            db = SessionLocal()
            db.add(Document(workspace_id=account.workspace_id, filename="cv.pdf", text="cv text", filetype=".pdf",
                            kind="resume", stored_name=stored))
            db.add(Job(workspace_id=account.workspace_id, url=f"https://x.example/{account.id}", origin="private",
                       title="A job", company="Co", description="d", status="applied"))
            db.add(LlmUsage(workspace_id=account.workspace_id, user_id=account.id, purpose="chat", model="m",
                            input_tokens=5, output_tokens=1))
            db.commit()
            db.close()
        return stored

    def counts(self, account) -> dict:
        def read(s):
            ws = account.workspace_id
            return {"workspaces": s.query(Workspace).filter(Workspace.owner_id == account.id).count(),
                    "documents": s.query(Document).filter(Document.workspace_id == ws).count(),
                    "jobs": s.query(Job).filter(Job.workspace_id == ws).count(),
                    "keys": s.query(UserKeys).filter(UserKeys.user_id == account.id).count(),
                    "sessions": s.query(AuthSession).filter(AuthSession.user_id == account.id).count(),
                    "membership": s.query(Membership).filter(Membership.user_id == account.id).count(),
                    "grants": s.query(MembershipGrant).filter(MembershipGrant.user_id == account.id).count(),
                    "usage": s.query(LlmUsage).filter(LlmUsage.user_id == account.id).count(),
                    "user": s.query(User).filter(User.id == account.id).count()}
        return db_rows(read)

    def test_the_delete_holds_the_start_lock_so_no_run_is_queued_meanwhile(self):
        """tasks.start takes this lock too: while the rows go, a run can't be queued for the workspace."""
        from backend import tasks
        c = self.signed_in()
        bob = helpers.make_user(name="Bob")
        finished = threading.Event()
        statuses = []

        def delete():
            statuses.append(c.delete(f"/api/admin/users/{bob.id}").status_code)
            finished.set()

        with tasks.start_lock():  # a start is in progress
            t = threading.Thread(target=delete)
            t.start()
            self.assertFalse(finished.wait(0.7), "the delete waits for the start lock")
            self.assertEqual(self.counts(bob)["user"], 1)
        self.assertTrue(finished.wait(20))
        t.join()
        self.assertEqual(statuses, [200])
        self.assertEqual(self.counts(bob)["user"], 0)

    def test_the_rows_and_files_go_and_another_account_is_untouched(self):
        c = self.signed_in()
        bob, dave = helpers.make_user(name="Bob"), helpers.make_user(name="Dave")
        bob_file, dave_file = self.plant(bob), self.plant(dave)
        for account in (bob, dave):
            self.assertEqual(c.post(f"/api/admin/users/{account.id}/grants", json={"kind": "manual", "days": 3}).status_code, 201)
        self.signed_in(bob)
        self.assertTrue((UPLOAD_DIR / bob_file).is_file())
        dave_before = self.counts(dave)
        self.assertEqual(dave_before["user"], 1)
        r = c.delete(f"/api/admin/users/{bob.id}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "apk_rotated": False})
        self.assertEqual(set(self.counts(bob).values()), {0})
        self.assertFalse((UPLOAD_DIR / bob_file).exists())
        self.assertEqual(self.counts(dave), dave_before)
        self.assertTrue((UPLOAD_DIR / dave_file).is_file())
        with helpers.as_account(dave):
            self.assertEqual(docs.read_upload(dave_file), b"upload of " + dave.email.encode())
        ids = [u["id"] for u in c.get("/api/admin/users").json()["users"]]
        self.assertNotIn(bob.id, ids)
        self.assertIn(dave.id, ids)
        login = self.anonymous().post("/api/auth/login", json={"email": bob.email, "password": bob.password})
        self.assertIn(login.status_code, (401, 403))
        self.assertEqual(c.delete(f"/api/admin/users/{bob.id}").status_code, 404)

    def test_the_audit_entry_holds_no_personal_details_and_usage_stays_without_a_person(self):
        c = self.signed_in()
        bob = helpers.make_user(name="Bobby Tables")
        self.plant(bob)
        self.assertEqual(c.delete(f"/api/admin/users/{bob.id}").status_code, 200)
        entry = next(e for e in c.get("/api/admin/audit").json()["entries"] if e["action"] == "user_deleted")
        self.assertEqual((entry["admin"]["id"], entry["target"]["id"] if entry["target"] else None), (self.admin_id, None))
        self.assertEqual(entry["detail"], {"user_id": bob.id, "role": "user", "workspaces": 1, "files": 1})
        self.assertNotIn(bob.email, json.dumps(entry))
        self.assertNotIn("Bobby", json.dumps(entry))
        raw = db_rows(lambda s: s.get(AdminAudit, entry["id"]).detail_admin)
        self.assertNotIn(b"user_id", raw)  # sealed
        usage = db_rows(lambda s: s.query(LlmUsage).filter(LlmUsage.model == "m", LlmUsage.input_tokens == 5)
                        .order_by(LlmUsage.id.desc()).first())
        self.assertIsNotNone(usage)
        self.assertEqual((usage.user_id, usage.workspace_id), (None, None))

    def test_not_yourself_not_the_last_admin_not_during_a_search_and_only_with_a_recent_password(self):
        c = self.signed_in()
        r = c.delete(f"/api/admin/users/{self.admin_id}")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "self"))
        carol = self.make_admin()
        with mock.patch.object(users, "is_last_admin", return_value=True):  # nobody else could take over
            r = c.delete(f"/api/admin/users/{carol.id}")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "last_admin"))
        self.assertEqual(db_rows(lambda s: s.get(User, carol.id).role), "admin")
        bob = helpers.make_user()
        with helpers.as_account(bob):
            db = SessionLocal()
            run = SearchRun(workspace_id=bob.workspace_id, state="running")
            db.add(run)
            db.commit()
            run_id = run.id
            db.close()
        r = c.delete(f"/api/admin/users/{bob.id}")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "busy"))
        self.assertEqual(self.counts(bob)["user"], 1)
        with engine.begin() as conn:
            conn.execute(text("UPDATE search_runs SET state = 'done' WHERE id = :i"), {"i": run_id})
        self.make_stale()
        r = c.delete(f"/api/admin/users/{bob.id}")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        self.assertEqual(self.counts(bob)["user"], 1)
        with helpers.client(bob.email) as b:
            self.assertEqual(b.delete(f"/api/admin/users/{bob.id}").status_code, 403)  # not an admin
        self.assertEqual(self.anonymous().delete(f"/api/admin/users/{bob.id}").status_code, 401)

    def test_deleting_an_admin_replaces_the_key_and_the_others_still_read_everything(self):
        c = self.signed_in()
        bob = helpers.make_user(name="Bob")
        carol = self.make_admin(name="Carol")
        self.plant(carol)
        self.assertEqual(c.post(f"/api/admin/users/{bob.id}/grants", json={"kind": "manual", "days": 5, "note": "kept note"}).status_code, 201)
        version = apk_version()
        r = c.delete(f"/api/admin/users/{carol.id}")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "apk_rotated": True}))
        self.assertEqual(apk_version(), version + 1)
        self.assertIsNone(db_rows(lambda s: s.get(AdminKeyGrant, carol.id)))
        self.assertEqual(db_rows(lambda s: s.get(AdminKeyGrant, self.admin_id).apk_version), version + 1)
        row = self.user_row(c, bob.id)
        self.assertEqual((row["email"], row["name"]), (bob.email, "Bob"))
        self.assertEqual(row["membership"]["grants"][0]["note"], "kept note")
        entries = c.get("/api/admin/audit").json()["entries"]
        deleted = next(e for e in entries if e["action"] == "user_deleted")
        self.assertEqual(deleted["detail"]["role"], "admin")
        self.assertEqual(deleted["detail"]["apk_version"], version + 1)
        self.assertTrue(all(e["detail"] is not None for e in entries if e["action"] in ("grant_added", "role_changed")))
        self.assertEqual(self.counts(carol)["user"], 0)

    def test_a_failed_deletion_changes_nothing(self):
        c = self.signed_in()
        bob = helpers.make_user()
        stored = self.plant(bob)
        with mock.patch.object(admin_key, "audit", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            c.delete(f"/api/admin/users/{bob.id}")
        self.assertEqual(self.counts(bob)["user"], 1)
        self.assertEqual(self.counts(bob)["documents"], 1)
        self.assertTrue((UPLOAD_DIR / stored).is_file())


# ---------------------------------------------------------------------------
# Notes sealed while a demotion replaces the key, and grant dates
# ---------------------------------------------------------------------------

class GrantNoteRaceTest(AdminTestCase):
    def demote_meanwhile(self, carol) -> threading.Thread:
        """Makes another admin's demotion of ``carol`` start right after a route has read the admin key's public half."""
        real = admin._public
        started = threading.Event()
        other = helpers.client()
        other.__enter__()
        self.addCleanup(other.__exit__, None, None, None)
        thread = threading.Thread(target=lambda: (started.set(), other.post(f"/api/admin/users/{carol.id}/role", json={"role": "user"})))

        def wrapped(db):
            value = real(db)  # the key as this request read it ...
            if not started.is_set():
                thread.start()  # ... and then the demotion replaces it
                started.wait(5)
                thread.join(1.5)  # (with the write lock already taken, the demotion has to wait for this request)
            return value

        patcher = mock.patch.object(admin, "_public", wrapped)
        patcher.start()
        self.addCleanup(patcher.stop)
        return thread

    def test_a_new_note_is_never_sealed_to_a_key_a_demotion_just_replaced(self):
        c = self.signed_in()
        bob = helpers.make_user()
        carol = self.make_admin()
        version = apk_version()
        thread = self.demote_meanwhile(carol)
        r = c.post(f"/api/admin/users/{bob.id}/grants", json={"kind": "free_period", "days": 3, "note": "racing note"})
        self.assertEqual(r.status_code, 201, r.text)
        thread.join(30)
        self.assertEqual(apk_version(), version + 1)
        self.assertEqual(self.user_row(c, bob.id)["membership"]["grants"][0]["note"], "racing note")

    def test_a_changed_note_is_never_sealed_to_a_key_a_demotion_just_replaced(self):
        c = self.signed_in()
        bob = helpers.make_user()
        carol = self.make_admin()
        gid = c.post(f"/api/admin/users/{bob.id}/grants", json={"kind": "manual", "days": 3}).json()["grant"]["id"]
        version = apk_version()
        thread = self.demote_meanwhile(carol)
        r = c.patch(f"/api/admin/users/{bob.id}/grants/{gid}", json={"note": "changed while racing"})
        self.assertEqual(r.status_code, 200, r.text)
        thread.join(30)
        self.assertEqual(apk_version(), version + 1)
        self.assertEqual(self.user_row(c, bob.id)["membership"]["grants"][0]["note"], "changed while racing")


class GrantDatesTest(AdminTestCase):
    def test_changing_only_the_note_leaves_the_dates_alone(self):
        c = self.signed_in()
        bob = helpers.make_user()
        grant = c.post(f"/api/admin/users/{bob.id}/grants", json={"kind": "free_period", "days": 30}).json()["grant"]
        r = c.patch(f"/api/admin/users/{bob.id}/grants/{grant['id']}", json={"note": "only the note"})
        self.assertEqual(r.status_code, 200, r.text)
        changed = r.json()["grant"]
        self.assertEqual((changed["starts_at"], changed["ends_at"], changed["note"]),
                         (grant["starts_at"], grant["ends_at"], "only the note"))
        # ... while sending the dates back as a date alone is what moves them to midnight.
        r = c.patch(f"/api/admin/users/{bob.id}/grants/{grant['id']}", json={"ends_at": grant["ends_at"][:10]})
        self.assertTrue(r.json()["grant"]["ends_at"].endswith("T00:00:00Z"))

    def test_the_admin_page_sends_only_what_was_edited_and_ends_a_period_at_the_end_of_its_day(self):
        src = open(os.path.join(os.path.dirname(helpers.__file__), "..", "frontend", "admin.js"), encoding="utf-8").read()
        self.assertIn("editingDates", src)
        self.assertIn("body.ends_at = addDay(end, 1)", src)
        self.assertNotIn("g.ends_at.slice(0, 10)", src)


# ---------------------------------------------------------------------------
# The audit log's details, unlocking, and settings changes
# ---------------------------------------------------------------------------

class AuditDetailTest(AdminTestCase):
    def test_an_action_with_nothing_to_say_has_an_empty_detail_and_a_sealed_one_that_wont_open_is_null(self):
        c = self.signed_in()
        bob = helpers.make_user()
        self.assertEqual(c.post(f"/api/admin/users/{bob.id}/disable").status_code, 200)
        entries = c.get("/api/admin/audit").json()["entries"]
        disabled = next(e for e in entries if e["action"] == "user_disabled" and e["target"]["id"] == bob.id)
        self.assertEqual(disabled["detail"], {})
        self.assertIsNone(db_rows(lambda s: s.get(AdminAudit, disabled["id"]).detail_admin))
        # a detail sealed to a key that is gone is the one that says "not readable"
        self.assertEqual(c.put(f"/api/admin/users/{bob.id}/membership", json={"exempt": True}).status_code, 200)
        entry = next(e for e in c.get("/api/admin/audit").json()["entries"] if e["action"] == "membership_updated")
        self.assertEqual(entry["detail"], {"exempt": True})
        with engine.begin() as conn:
            conn.execute(text("UPDATE admin_audit SET detail_admin = :b WHERE id = :i"), {"b": b"not a sealed value", "i": entry["id"]})
        entry = next(e for e in c.get("/api/admin/audit").json()["entries"] if e["id"] == entry["id"])
        self.assertIsNone(entry["detail"])


class UnlockTest(AdminTestCase):
    def test_a_locked_account_is_reported_and_enable_unlocks_it_with_an_audit_entry(self):
        c = self.signed_in()
        bob = helpers.make_user()
        self.assertFalse(self.user_row(c, bob.id)["locked"])
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET failed_logins = 7, locked_until = :t WHERE id = :i"),
                         {"t": utcnow() + timedelta(minutes=10), "i": bob.id})
        self.assertTrue(self.user_row(c, bob.id)["locked"])
        r = c.post(f"/api/admin/users/{bob.id}/enable")
        self.assertEqual((r.status_code, r.json()["changed"], r.json()["user"]["locked"]), (200, False, False))
        row = db_rows(lambda s: s.get(User, bob.id))
        self.assertEqual((row.failed_logins, row.locked_until, row.status), (0, None, "active"))
        self.assertIn("user_unlocked", [e["action"] for e in c.get("/api/admin/audit").json()["entries"]])
        # an expired wait is not "locked"
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET locked_until = :t WHERE id = :i"), {"t": utcnow() - timedelta(minutes=1), "i": bob.id})
        self.assertFalse(self.user_row(c, bob.id)["locked"])


class ServerSettingsAuditTest(AdminTestCase):
    SECRET_KEY = "sk-super-secret-key-12345"

    def audit_entries(self, c, action="server_settings_changed"):
        return [e for e in c.get("/api/admin/audit").json()["entries"] if e["action"] == action]

    def test_changing_the_ai_settings_and_commute_keys_is_logged_by_name_never_by_value(self):
        from backend import app_settings
        c = self.signed_in()
        env = {"LLM_API_KEY": "", "LLM_BASE_URL": "https://api.openai.com/v1", "LLM_MODEL": "old-model"}
        with mock.patch.dict(os.environ, env), mock.patch.object(app_settings, "_save"):
            before = len(self.audit_entries(c))
            r = c.put("/api/settings/llm", json={"model": "new-model", "api_key": self.SECRET_KEY})
            self.assertEqual(r.status_code, 200, r.text)
            r = c.put("/api/settings/keys", json={"tomtom": "tomtom-key-123456", "tfnsw": ""})
            self.assertEqual(r.status_code, 200, r.text)
            r = c.put("/api/llm/reasoning", json={"scoring": "low", "chat": ""})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(c.put("/api/settings/llm", json={}).status_code, 200)  # nothing changed: nothing logged
            self.assertEqual(c.put("/api/settings/keys", json={"tomtom": "bad key"}).status_code, 422)  # refused: nothing logged
        entries = self.audit_entries(c)[:3]
        self.assertEqual(len(self.audit_entries(c)) - before, 3)
        self.assertEqual([e["detail"] for e in reversed(entries)], [
            {"fields": ["ai_key", "ai_model"], "cleared": []},
            {"fields": ["tfnsw_key", "tomtom_key"], "cleared": ["tfnsw_key"]},
            {"fields": ["reasoning_chat", "reasoning_scoring"], "cleared": ["reasoning_chat"]}])
        everything = json.dumps(c.get("/api/admin/audit").json()) + repr(db_rows(lambda s: [bytes(a.detail_admin or b"") for a in s.query(AdminAudit)]))
        for value in (self.SECRET_KEY, "tomtom-key-123456", "new-model"):
            self.assertNotIn(value, everything)
        self.assertEqual(entries[0]["admin"]["id"], self.admin_id)

    def test_a_non_admin_can_change_nothing_and_leaves_no_entry(self):
        c = self.signed_in()
        before = len(self.audit_entries(c))
        with self.signed_in(helpers.make_user()) as b:
            self.assertEqual(b.put("/api/settings/llm", json={"model": "x"}).status_code, 403)
            self.assertEqual(b.put("/api/llm/reasoning", json={"scoring": "low"}).status_code, 403)
        self.assertEqual(len(self.audit_entries(c)), before)


# ---------------------------------------------------------------------------
# A key file changed by another process
# ---------------------------------------------------------------------------

class StaleKeyFileTest(AdminTestCase):
    def test_a_running_app_with_an_old_copy_never_overwrites_a_newer_key_file(self):
        c = self.signed_in()
        a = users.create_user(f"stale-{os.urandom(4).hex()}@example.invalid", PASSWORD, background=True)
        app_copy = users.server_keys()
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(keys.main(["rotate-kek"]), 0)  # another process rotates; this one still has the old copy
        on_disk = keys.ServerKeys.load()
        self.assertNotEqual(on_disk.current_kek_id, app_copy.current_kek_id)
        before = on_disk.path.read_bytes()
        try:
            r = c.post("/api/admin/keys/rotate")
            self.assertEqual((r.status_code, r.json()["code"]), (409, "key_file_changed"))
            self.assertEqual(on_disk.path.read_bytes(), before)  # nothing was written
            for change in (app_copy.rotate_pepper, app_copy.drop_old_peppers, app_copy.rotate_signing):
                with self.assertRaises(keys.KeyFileError):
                    change()
                self.assertEqual(on_disk.path.read_bytes(), before)
        finally:
            users._server_keys = on_disk  # what a restart does
        self.assertEqual(db_rows(lambda s: users.background_key(s, a.user_id)), a.dek)

    def test_the_command_line_refuses_while_the_app_is_running(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        run_file = os.path.join(folder, "running.json")
        with mock.patch.object(key_rotation, "DATA_DIR", __import__("pathlib").Path(folder)):
            key_rotation.refuse_while_running()  # no file: fine
            open(run_file, "w").write(json.dumps({"port": 8000, "pid": os.getppid(), "launcher_pid": 1}))
            with self.assertRaises(keys.KeyFileError) as caught:
                key_rotation.server_keys_for_cli()
            self.assertIn("running", str(caught.exception))
            with mock.patch("sys.stderr", new=io.StringIO()) as err:
                self.assertEqual(key_rotation.rotate_kek_command(), 2)
            self.assertIn("running", err.getvalue())
            open(run_file, "w").write(json.dumps({"port": 8000, "pid": 2 ** 22 + 12345}))  # a process that is gone
            key_rotation.refuse_while_running()
            open(run_file, "w").write("not json")
            key_rotation.refuse_while_running()


if __name__ == "__main__":
    unittest.main()
