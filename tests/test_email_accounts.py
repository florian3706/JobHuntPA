"""Email in the account flows (accounts design §1.6, §6.1, §6.9, §7, §9; Phase H): address verification, a change of
address, security notices, invitations and the admin's reset button end to end with SMTP captured, and the periodic
clean-up of sessions and links.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)
from tests.mail_helpers import BASE, MailTestCase, html_of, text_of, token_in  # noqa: E402

from sqlalchemy import text  # noqa: E402

from backend import account_email, auth, email_templates, mail_queue, mailer, schedule, tokens, users  # noqa: E402
from backend.db import AuthSession, AuthToken, SessionLocal, User, engine, utcnow  # noqa: E402

PASSWORD = "user-password-123"


def rows(fn):
    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


def token_rows(user_id: int, kind: str) -> list[AuthToken]:
    return rows(lambda s: s.query(AuthToken).filter(AuthToken.user_id == user_id, AuthToken.kind == kind)
                .order_by(AuthToken.id).all())


class AccountMailCase(MailTestCase):
    def setUp(self):
        super().setUp()
        self.admin_id = helpers.admin_id()

    def signup_mode(self, mode: str) -> None:
        with helpers.client() as c:
            self.assertEqual(c.put("/api/admin/signup", json={"mode": mode}).status_code, 200)

    def open_signup(self) -> None:
        self.signup_mode("open")
        self.addCleanup(self.signup_mode, "invite")

    def new_email(self, prefix="newbie") -> str:
        return f"{prefix}-{os.urandom(4).hex()}@example.invalid"

    def sign_up(self, email=None, **fields):
        """Signs a new person up through the open form; (client, response, email)."""
        email = email or self.new_email()
        c = self.anonymous()
        r = c.post("/api/auth/signup", json={"email": email, "password": PASSWORD, **fields})
        if r.status_code == 201:
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
        return c, r, email

    def verified(self, user_id: int) -> bool:
        return bool(rows(lambda s: s.get(User, user_id)).email_verified)

    def user_id_of(self, email: str) -> int:
        return rows(lambda s: users.find_by_email(s, email)).id

    def confirm(self, token: str, anon=None):
        return (anon or self.anonymous()).post("/api/auth/verify-email", json={"token": token})

    def make_stale(self, user_id: int) -> None:
        with engine.begin() as conn:
            conn.execute(text("UPDATE sessions SET reauth_at = :t WHERE user_id = :u"),
                         {"t": utcnow() - timedelta(minutes=30), "u": user_id})


# ---------------------------------------------------------------------------
# Checking the address
# ---------------------------------------------------------------------------

class VerifyTest(AccountMailCase):
    def test_signing_up_sends_a_verification_link_that_works_once(self):
        self.open_signup()
        c, r, email = self.sign_up()
        self.assertEqual(r.status_code, 201, r.text)
        msg = self.last_to(email)
        self.assertEqual(msg["Subject"], "Confirm your email address for JobHuntPA")
        token = token_in(msg, "verify-email.html")  # the same link in both parts, in the fragment
        for part in (text_of(msg), html_of(msg)):
            self.assertIn(f"{BASE}/verify-email.html#token={token}", part)
            self.assertNotIn("?token=", part)
        uid = self.user_id_of(email)
        self.assertFalse(self.verified(uid))
        # no session needed: the link may be opened in another browser
        r = self.confirm(token)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "email_verified": True, "changed": False}))
        self.assertTrue(self.verified(uid))
        r = self.confirm(token)
        self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        status = c.get("/api/auth/email").json()
        self.assertEqual((status["verified"], status["email"], status["enabled"], status["pending_change"]),
                         (True, email, True, False))

    def test_opening_the_link_only_says_what_it_is_for_and_confirms_nothing(self):
        """The page asks first. A mail scanner that runs scripts, or one stray click, must not put an account on an
        address: only the POST that the "Confirm" button sends does that."""
        self.open_signup()
        _c, _r, email = self.sign_up()
        token = token_in(self.last_to(email), "verify-email.html")
        uid = self.user_id_of(email)
        for _ in range(3):  # (and it doesn't use the link up)
            r = self.anonymous().post("/api/auth/verify-email/info", json={"token": token})
            self.assertEqual((r.status_code, r.json()), (200, {"kind": "verify", "email": email}))
        self.assertFalse(self.verified(uid))
        self.assertEqual(self.confirm(token).status_code, 200)
        self.assertTrue(self.verified(uid))
        r = self.anonymous().post("/api/auth/verify-email/info", json={"token": token})
        self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        for bad in ("nonsense", "", "x" * 600):
            self.assertEqual(self.anonymous().post("/api/auth/verify-email/info", json={"token": bad}).status_code, 410)

    def test_a_change_of_address_names_the_address_and_waits_for_the_button(self):
        """Signed-in A asks to move to victim@...: the victim's mailbox gets the link. Opening it (even by a scanner)
        changes nothing; the page shows the address and a button."""
        mallory = helpers.make_user(password=PASSWORD)
        c = self.signed_in(mallory)
        victim = self.new_email("victim")
        self.assertEqual(c.post("/api/auth/email/change", json={"new_email": victim, "password": PASSWORD}).status_code, 200)
        token = token_in(self.last_to(victim), "verify-email.html")
        r = self.anonymous().post("/api/auth/verify-email/info", json={"token": token})
        self.assertEqual((r.status_code, r.json()), (200, {"kind": "change", "email": victim}))
        self.assertEqual(bytes(rows(lambda s: s.get(User, mallory.id)).email_hash),
                         users.server_keys().email_hash(mallory.email))  # nothing moved
        self.assertIsNone(rows(lambda s: users.find_by_email(s, victim)))

    def test_the_page_does_not_post_on_load(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "frontend" / "verify-email.js").read_text(encoding="utf-8")
        load = js[js.index("async function initVerify"):]
        self.assertIn("'/api/auth/verify-email/info'", load)
        self.assertNotIn("'/api/auth/verify-email'", load)  # that call is in confirmAddress, on the button
        self.assertIn("addEventListener('click', confirmAddress)", js)

    def test_the_token_is_random_hashed_and_good_for_24_hours(self):
        self.open_signup()
        _c, _r, email = self.sign_up()
        token = token_in(self.last_to(email), "verify-email.html")
        (row,) = token_rows(self.user_id_of(email), "verify_email")
        self.assertEqual(bytes(row.token_hash), tokens.hash_token(token))
        self.assertNotIn(token.encode(), bytes(row.token_hash) + bytes(row.payload_enc))
        self.assertNotIn(email.encode(), bytes(row.payload_enc))
        self.assertLess(abs(timedelta(hours=24) - (row.expires_at - row.created_at)), timedelta(seconds=30))
        self.assertEqual(len(token), 43)

    def test_an_expired_or_unknown_link_is_the_same_410(self):
        self.open_signup()
        _c, _r, email = self.sign_up()
        token = token_in(self.last_to(email), "verify-email.html")
        with engine.begin() as conn:
            conn.execute(text("UPDATE auth_tokens SET expires_at = :t WHERE token_hash = :h"),
                         {"t": utcnow() - timedelta(seconds=1), "h": tokens.hash_token(token)})
        answers = {(r.status_code, r.json()["code"], r.json()["detail"])
                   for r in (self.confirm(token), self.confirm("nonsense"), self.confirm(""), self.confirm("x" * 600))}
        self.assertEqual(len(answers), 1)
        self.assertEqual(next(iter(answers))[:2], (410, "link_invalid"))
        self.assertFalse(self.verified(self.user_id_of(email)))

    def test_a_link_of_another_kind_does_not_verify(self):
        self.open_signup()
        _c, _r, email = self.sign_up()
        uid = self.user_id_of(email)
        db = SessionLocal()
        try:
            token, _ = tokens.create(db, "reset", ttl=timedelta(hours=1), user_id=uid, payload={"email": email})
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.confirm(token).status_code, 410)

    def test_without_email_nothing_is_sent_and_nothing_stored(self):
        with mock.patch.dict(os.environ, {"SMTP_HOST": "", "PUBLIC_BASE_URL": ""}):
            self.open_signup()
            _c, r, email = self.sign_up()
            self.assertEqual(r.status_code, 201)
        self.assertEqual(self.flush(), [])
        self.assertEqual(token_rows(self.user_id_of(email), "verify_email"), [])

    def test_signing_up_does_not_wait_for_the_mail_server(self):
        self.open_signup()
        gate = threading.Event()
        real = mailer.send
        with mock.patch.object(mailer, "send", lambda *a, **k: (gate.wait(10), real(*a, **k))[1]):
            _c, r, email = self.sign_up()
            self.assertEqual(r.status_code, 201)
            self.assertEqual(self.sent, [])
            gate.set()
            self.assertEqual(len(self.to(email)), 1)

    def test_a_broken_mail_server_never_breaks_signing_up(self):
        self.open_signup()
        with mock.patch.object(mailer, "send", side_effect=RuntimeError("boom")):
            _c, r, _email = self.sign_up()
            self.assertEqual(r.status_code, 201)
            self.flush()

    def test_an_invitation_to_the_same_address_counts_as_verified(self):
        email = self.new_email("invited")
        with helpers.client() as admin:
            link = admin.post("/api/admin/invites", json={"email": email}).json()
        self.assertTrue(link["emailed"])
        self.assertEqual(len(self.to(email)), 1)  # the invitation
        invite = link["link"].split("invite=")[1]
        _c, r, _ = self.sign_up(email=email.upper(), invite=invite)
        self.assertEqual(r.status_code, 201, r.text)
        uid = self.user_id_of(email)
        self.assertTrue(self.verified(uid))
        self.assertEqual(len(self.to(email)), 1)  # no verification email on top
        self.assertEqual(token_rows(uid, "verify_email"), [])

    def test_an_invitation_used_with_another_address_is_checked_by_email(self):
        invited, other = self.new_email("invited"), self.new_email("other")
        with helpers.client() as admin:
            invite = admin.post("/api/admin/invites", json={"email": invited}).json()["link"].split("invite=")[1]
        _c, r, _ = self.sign_up(email=other, invite=invite)
        self.assertEqual(r.status_code, 201)
        self.assertFalse(self.verified(self.user_id_of(other)))
        self.assertEqual(len(self.to(other)), 1)
        self.assertIn("Confirm your email address", self.last_to(other)["Subject"])

    def test_send_it_again_replaces_the_old_link_and_is_limited(self):
        self.open_signup()
        c, _r, email = self.sign_up()
        first = token_in(self.last_to(email), "verify-email.html")
        r = c.post("/api/auth/verify-email/resend")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "sent": True, "already_verified": False}))
        second = token_in(self.last_to(email), "verify-email.html")
        self.assertNotEqual(first, second)
        self.assertEqual(self.confirm(first).status_code, 410)  # only the newest link works
        for _ in range(2):
            self.assertEqual(c.post("/api/auth/verify-email/resend").status_code, 200)
        r = c.post("/api/auth/verify-email/resend")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "rate_limited"))
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(len(self.to(email)), 4)  # the sign-up one and three resends
        self.assertEqual(self.confirm(token_in(self.last_to(email), "verify-email.html")).status_code, 200)
        r = c.post("/api/auth/verify-email/resend")
        self.assertEqual(r.json(), {"ok": True, "sent": False, "already_verified": True})

    def test_a_full_mail_queue_keeps_the_link_the_person_already_has(self):
        self.open_signup()
        c, _r, email = self.sign_up()
        first = token_in(self.last_to(email), "verify-email.html")
        with mock.patch.object(mail_queue, "MAX_WAITING", 0):
            r = c.post("/api/auth/verify-email/resend")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "sent": False, "already_verified": False}))
        self.assertEqual(len(token_rows(self.user_id_of(email), "verify_email")), 1)  # nothing replaced
        self.assertEqual(self.confirm(first).status_code, 200)

    def test_a_full_mail_queue_at_sign_up_makes_no_link(self):
        self.open_signup()
        with mock.patch.object(mail_queue, "MAX_WAITING", 0):
            _c, r, email = self.sign_up()
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(token_rows(self.user_id_of(email), "verify_email"), [])

    def test_a_notice_still_goes_when_ordinary_mail_has_filled_its_places_and_a_full_queue_is_logged(self):
        bob = helpers.make_user()
        with mock.patch.object(mail_queue, "MAX_WAITING", 1), mock.patch.object(mail_queue, "NOTICE_RESERVE", 1):
            self.assertIsNone(mail_queue.send("x@example.invalid", ("S", "t", "<p>h</p>", {})))  # ordinary: no room
            self.assertTrue(account_email.notify("password_changed", bob.email, bob.id))
            self.flush()
        self.assertEqual(len(self.to(bob.email)), 1)
        with mock.patch.object(mail_queue, "MAX_WAITING", 0), self.assertLogs("backend.account_email", "WARNING") as logs:
            self.assertFalse(account_email.notify("password_changed", bob.email, bob.id))
        self.assertIn("queue is full", logs.output[0])
        self.assertNotIn(bob.email, logs.output[0])

    def test_resend_needs_a_session_and_email(self):
        self.assertEqual(self.anonymous().post("/api/auth/verify-email/resend").status_code, 401)
        self.assertEqual(self.anonymous().get("/api/auth/email").status_code, 401)
        bob = helpers.make_user()
        c = self.signed_in(bob)
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": ""}):
            r = c.post("/api/auth/verify-email/resend")
            self.assertEqual((r.status_code, r.json()["code"]), (409, "email_not_set_up"))
            self.assertEqual(c.get("/api/auth/email").json()["missing"], ["public_url"])
        self.assertEqual(self.flush(), [])

    def test_confirming_takes_json_from_this_site_only(self):
        c = self.anonymous()
        self.assertEqual(c.post("/api/auth/verify-email", data={"token": "x"}).status_code, 415)
        self.assertEqual(c.post("/api/auth/verify-email", json={"token": "x"},
                                headers={"Origin": "https://evil.example"}).status_code, 403)


# ---------------------------------------------------------------------------
# Changing the address
# ---------------------------------------------------------------------------

class ChangeEmailTest(AccountMailCase):
    def person(self, name="Pat Example"):
        acct = helpers.make_user(password=PASSWORD, name=name)
        return acct, self.signed_in(acct)

    def start(self, c, new: str):
        return c.post("/api/auth/email/change", json={"new_email": new, "password": PASSWORD})

    def test_the_link_goes_to_the_new_address_and_nothing_changes_until_it_is_opened(self):
        bob, c = self.person()
        new = self.new_email("moved")
        r = self.start(c, new)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "sent": True}))
        msg = self.last_to(new)
        token = token_in(msg, "verify-email.html")
        started = self.to(bob.email)  # the old address hears of it now, and it is only a notice: no link
        self.assertEqual([m["Subject"] for m in started],
                         ["A change of email address was started on your JobHuntPA account"])
        for part in (text_of(started[0]), html_of(started[0])):
            self.assertNotIn("verify-email.html", part)
            self.assertNotIn(new, part)
        before = rows(lambda s: s.get(User, bob.id))
        self.assertEqual(bytes(before.email_hash), users.server_keys().email_hash(bob.email))
        self.assertEqual(self.login(bob.email)[1].status_code, 200)  # the old address still signs in
        self.assertEqual(c.get("/api/auth/email").json()["pending_change"], True)
        self.assertEqual(c.get("/api/auth/me").json()["user"]["email"], bob.email)
        self.assertEqual(token_rows(bob.id, "email_change")[0].used_at, None)
        self.assertTrue(token)

    def login(self, email, password=PASSWORD):
        helpers.ratelimit.reset_all()
        c = self.anonymous()
        return c, c.post("/api/auth/login", json={"email": email, "password": password})

    def test_confirming_updates_the_hash_and_both_copies_of_the_details_and_warns_the_old_address(self):
        bob, c = self.person()
        new = self.new_email("moved")
        self.start(c, new)
        token = token_in(self.last_to(new), "verify-email.html")
        other = self.anonymous()  # another browser: no session
        r = self.confirm(token, other)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "email_verified": True, "changed": True}))
        # signing in: the new address works, the old one doesn't
        c2, r = self.login(new)
        self.assertEqual(r.status_code, 200, r.text)
        c2.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertEqual(self.login(bob.email)[1].status_code, 401)
        # pii_self opens under the user's own key and has the new address and the same name
        me = c2.get("/api/auth/me").json()["user"]
        self.assertEqual((me["email"], me["name"]), (new, "Pat Example"))
        # pii_admin, sealed to the admin key, has them too, at the current version
        with helpers.client() as admin:
            row = next(u for u in admin.get("/api/admin/users").json()["users"] if u["id"] == bob.id)
        self.assertEqual((row["email"], row["name"], row["details_available"], row["email_verified"]),
                         (new, "Pat Example", True, True))
        # a notice went to the old address, and says nothing about either address
        notice = [m for m in self.to(bob.email) if "email address on" in m["Subject"]]
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0]["Subject"], "The email address on your JobHuntPA account was changed")
        for part in (text_of(notice[0]), html_of(notice[0])):
            self.assertIn("changed", part)
            self.assertNotIn(new, part)
            self.assertNotIn(bob.email, part)
        # the link is used up, and a reset link asked for the old address finds nobody
        self.assertEqual(self.confirm(token).status_code, 410)
        self.assertEqual(self.anonymous().post("/api/auth/forgot", json={"email": bob.email}).status_code, 202)
        self.assertEqual([m for m in self.to(bob.email) if "Reset" in m["Subject"]], [])

    def test_the_password_in_the_request_is_needed_not_just_a_recent_sign_in(self):
        """A cookie taken in the 15 minutes after sign-in is a recent sign-in. It must not be able to move the account
        to another mailbox, where "forgot password" would hand it over."""
        bob, c = self.person()
        new = self.new_email("thief")
        self.assertTrue(c.get("/api/auth/security").json()["recent_auth"])
        for body in ({"new_email": new}, {"new_email": new, "password": ""}):
            r = c.post("/api/auth/email/change", json=body)
            self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "invalid", "password"))
        r = c.post("/api/auth/email/change", json={"new_email": new, "password": "not the password"})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "wrong_password", "password"))
        self.assertEqual(self.to(new), [])
        self.assertEqual(token_rows(bob.id, "email_change"), [])
        self.assertEqual(c.get("/api/auth/email").json()["pending_change"], False)
        self.assertEqual(self.start(c, new).status_code, 200)

    def test_wrong_passwords_here_count_like_any_other_and_five_make_this_browser_wait(self):
        bob, c = self.person()
        for _ in range(5):
            self.assertEqual(c.post("/api/auth/email/change", json={"new_email": self.new_email(),
                                                                    "password": "wrong wrong"}).status_code, 422)
        self.assertEqual(helpers.wrong_tries(bob.id), 5)
        r = c.post("/api/auth/email/change", json={"new_email": self.new_email(), "password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))

    def test_with_two_step_sign_in_a_code_is_needed_too(self):
        from backend import totp
        bob, c = self.person()
        secret = c.post("/api/auth/mfa/setup").json()["secret"]
        step = int(time.time() // totp.STEP_SECONDS)
        self.assertEqual(c.post("/api/auth/mfa/enable", json={"code": totp.code_at(secret, step)}).status_code, 200)
        new = self.new_email("moved")
        r = c.post("/api/auth/email/change", json={"new_email": new, "password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "invalid", "code"))
        r = c.post("/api/auth/email/change", json={"new_email": new, "password": PASSWORD, "code": "000000"})
        self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))
        self.assertEqual(self.to(new), [])
        self.assertTrue(c.get("/api/auth/email").json()["mfa_enabled"])
        with mock.patch("backend.totp.now", lambda: (step + 1) * totp.STEP_SECONDS + 15.0):
            r = c.post("/api/auth/email/change", json={"new_email": new, "password": PASSWORD,
                                                       "code": totp.code_at(secret, step + 1)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.to(new)), 1)

    def test_the_message_to_the_new_address_says_what_it_is_for(self):
        bob, c = self.person()
        new = self.new_email("moved")
        self.start(c, new)
        msg = self.last_to(new)
        self.assertEqual(msg["Subject"], "Confirm your email address for JobHuntPA")
        for part in (text_of(msg), html_of(msg)):
            self.assertIn("asked to use this email address", part)
            self.assertIn("Nothing changes unless you open the link and confirm", part)
            self.assertNotIn(bob.email, part)

    def test_it_needs_recent_sign_in(self):
        bob, c = self.person()
        self.make_stale(bob.id)
        r = self.start(c, self.new_email())
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        self.assertEqual(self.flush(), [])
        self.assertEqual(c.post("/api/auth/reauth", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(self.start(c, self.new_email()).status_code, 200)

    def test_it_says_so_while_email_is_off_and_for_a_bad_or_taken_address(self):
        bob, c = self.person()
        carol = helpers.make_user()
        r = self.start(c, carol.email)
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (409, "email_taken", "new_email"))
        r = self.start(c, bob.email.upper())
        self.assertEqual((r.status_code, r.json()["code"]), (422, "same_address"))
        for bad in ("", "nope", "a@b"):
            r = self.start(c, bad)
            self.assertEqual((r.status_code, r.json()["field"]), (422, "new_email"))
        self.assertEqual(self.flush(), [])
        with mock.patch.dict(os.environ, {"SMTP_HOST": ""}):
            r = self.start(c, self.new_email())
            self.assertEqual((r.status_code, r.json()["code"]), (409, "email_not_set_up"))

    def test_the_link_is_single_use_and_expires(self):
        bob, c = self.person()
        new = self.new_email("moved")
        self.start(c, new)
        token = token_in(self.last_to(new), "verify-email.html")
        with engine.begin() as conn:
            conn.execute(text("UPDATE auth_tokens SET expires_at = :t WHERE token_hash = :h"),
                         {"t": utcnow() - timedelta(seconds=1), "h": tokens.hash_token(token)})
        self.assertEqual(self.confirm(token).status_code, 410)
        self.assertEqual(self.login(bob.email)[1].status_code, 200)
        self.start(c, new)
        again = token_in(self.last_to(new), "verify-email.html")
        self.assertEqual(self.confirm(again).status_code, 200)
        self.assertEqual(self.confirm(again).status_code, 410)

    def test_a_newer_request_replaces_an_older_one(self):
        bob, c = self.person()
        first_address, second_address = self.new_email("first"), self.new_email("second")
        self.start(c, first_address)
        first = token_in(self.last_to(first_address), "verify-email.html")
        self.start(c, second_address)
        second = token_in(self.last_to(second_address), "verify-email.html")
        self.assertEqual(self.confirm(first).status_code, 410)
        self.assertEqual(self.confirm(second).status_code, 200)
        self.assertEqual(self.login(second_address)[1].status_code, 200)

    def test_someone_else_taking_the_address_in_between_is_a_409_and_the_link_stays_good(self):
        bob, c = self.person()
        new = self.new_email("contested")
        self.start(c, new)
        token = token_in(self.last_to(new), "verify-email.html")
        taker = helpers.make_user(email=new)
        r = self.confirm(token)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "email_taken"))
        self.assertEqual(self.login(bob.email)[1].status_code, 200)
        self.assertEqual(token_rows(bob.id, "email_change")[0].used_at, None)  # given back
        self.assertTrue(taker.id)

    def test_a_link_for_an_old_address_cannot_verify_a_new_one(self):
        self.open_signup()
        c, _r, email = self.sign_up()
        old_link = token_in(self.last_to(email), "verify-email.html")
        new = self.new_email("moved")
        self.start(c, new)
        self.assertEqual(self.confirm(token_in(self.last_to(new), "verify-email.html")).status_code, 200)
        self.assertEqual(self.confirm(old_link).status_code, 410)  # it was for the address the account had

    def test_a_wipe_in_between_makes_the_link_useless_and_the_details_stay_readable(self):
        bob, c = self.person()
        new = self.new_email("moved")
        self.start(c, new)
        token = token_in(self.last_to(new), "verify-email.html")
        self.anonymous().post("/api/auth/forgot", json={"email": bob.email})
        reset = token_in(self.last_to(bob.email), "reset-password.html")
        r = self.anonymous().post("/api/auth/reset", json={"token": reset, "new_password": "a brand new password",
                                                           "accept_wipe": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.confirm(token).status_code, 410)  # the blob was sealed under the old data key
        c2, r = self.login(bob.email, "a brand new password")
        c2.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertEqual(c2.get("/api/auth/me").json()["user"]["email"], bob.email)

    def test_a_request_whose_key_was_replaced_before_it_finished_starts_no_change(self):
        bob, c = self.person()
        with engine.begin() as conn:  # (a wipe reset committed after this session loaded: the row has a new key id)
            conn.execute(text("UPDATE user_keys SET dek_kid = :k WHERE user_id = :u"), {"k": b"\x00\x01\x02\x03", "u": bob.id})
        r = self.start(c, self.new_email("moved"))
        self.assertEqual((r.status_code, r.json()["code"]), (401, "signed_out"))
        self.assertEqual(token_rows(bob.id, "email_change"), [])

    def stripe(self, key="sk_test_" + "a1B2c3D4" * 4):
        """Billing on with a mocked Stripe whose search finds customer cus_1 (the link opens without the person's key)."""
        stripe = mock.MagicMock()
        stripe.v1.customers.search.return_value = {"data": [{"id": "cus_1"}]}
        env = mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": key})
        env.start()
        patcher = mock.patch("backend.billing._client", return_value=stripe)
        patcher.start()
        self.addCleanup(env.stop)
        self.addCleanup(patcher.stop)
        return stripe

    def change(self, c, new):
        self.start(c, new)
        return self.confirm(token_in(self.last_to(new), "verify-email.html"))

    def test_the_stripe_customer_gets_the_new_address(self):
        bob, c = self.person()
        stripe = self.stripe()
        new = self.new_email("moved")
        self.assertEqual(self.change(c, new).status_code, 200)
        stripe.v1.customers.update.assert_called_once_with("cus_1", params={"email": new})

    def test_a_stripe_that_fails_or_a_person_without_one_never_stops_the_change(self):
        bob, c = self.person()
        stripe = self.stripe()
        stripe.v1.customers.update.side_effect = RuntimeError("down")
        self.assertEqual(self.change(c, self.new_email("moved")).status_code, 200)
        stripe.v1.customers.search.return_value = {"data": []}  # no customer: nothing to tell
        stripe.v1.subscriptions.search.return_value = {"data": []}
        stripe.v1.customers.update.reset_mock()
        self.assertEqual(self.change(c, self.new_email("again")).status_code, 200)
        stripe.v1.customers.update.assert_not_called()

    def test_without_billing_stripe_is_never_called(self):
        bob, c = self.person()
        stripe = self.stripe(key="")
        self.assertEqual(self.change(c, self.new_email("moved")).status_code, 200)
        self.assertEqual(stripe.mock_calls, [])

    def test_the_request_limit(self):
        bob, c = self.person()
        codes = [self.start(c, self.new_email()).status_code for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])

    def test_no_address_or_token_in_any_log_line(self):
        bob, c = self.person()
        new = self.new_email("moved")
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("test").info("marker")
            self.start(c, new)
            token = token_in(self.last_to(new), "verify-email.html")
            self.assertEqual(self.confirm(token).status_code, 200)
            self.assertEqual(self.confirm(token).status_code, 410)
            self.flush()
        everything = "\n".join(logs.output)
        for secret in (new, new.split("@")[0], bob.email, bob.email.split("@")[0], token):
            self.assertNotIn(secret, everything)
        self.assertIn("Email sent", everything)

    def test_the_message_for_a_change_has_both_parts(self):
        bob, c = self.person()
        new = self.new_email("moved")
        self.start(c, new)
        msg = self.last_to(new)
        self.assertEqual(msg.get_body(preferencelist=("plain",)).get_content_type(), "text/plain")
        self.assertEqual(msg.get_body(preferencelist=("html",)).get_content_type(), "text/html")
        token_in(msg, "verify-email.html")


# ---------------------------------------------------------------------------
# Security notices
# ---------------------------------------------------------------------------

class AddressShapeTest(AccountMailCase):
    """An address is one mailbox: nothing that a mail header or the SMTP envelope would read as a list."""

    LISTS = ["ceo,attacker@evil.example", "a@evil.example,b@evil.example", "a;b@evil.example", "Bob <b@evil.example>",
             "<b@evil.example>", '"b"@evil.example', "a(comment)@evil.example", "a@evil.example>", "a b@evil.example",
             "a@evil,example.org", "a\\b@evil.example", "a:b@evil.example", "a[1]@evil.example"]

    def test_the_checker_refuses_anything_but_one_plain_address(self):
        for value in self.LISTS:
            with self.subTest(value=value), self.assertRaises(users.AccountError):
                users.clean_email(value)
        for value in ("a@example.com", "first.last+tag@sub.example.co.uk", "o'brien@example.org", "x_y%z@example.io"):
            self.assertEqual(users.clean_email(value), value)

    def test_the_mailer_refuses_a_second_recipient_whatever_the_checker_says(self):
        sender = ("JobHuntPA", "jobs@example.org")
        for value in ("ceo,attacker@evil.example", "a@evil.example,b@evil.example", "a@evil.example, b@evil.example",
                      "Bob <b@evil.example>", "<b@evil.example>", "a@evil.example>", " a@example.com", "a@example.com "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mailer.build_message(value, "s", "t", "<p>h</p>", sender=sender)
            self.assertEqual(mailer.send(value, "s", "t", "<p>h</p>").error, "invalid message")
        self.assertEqual(mailer.build_message("a@example.com", "s", "t", "<p>h</p>", sender=sender)["To"], "a@example.com")

    def test_sign_up_and_changing_the_address_refuse_a_list(self):
        self.open_signup()
        _c, r, _email = self.sign_up(email="ceo,attacker@evil.example")
        self.assertEqual((r.status_code, r.json()["field"]), (422, "email"))
        bob = helpers.make_user(password=PASSWORD)
        r = self.signed_in(bob).post("/api/auth/email/change", json={"new_email": "ceo,me@evil.example"})
        self.assertEqual((r.status_code, r.json()["field"]), (422, "new_email"))
        self.assertEqual(self.flush(), [])


class NoticeTest(AccountMailCase):
    def parts_ok(self, msg, needle):
        for part in (text_of(msg), html_of(msg)):
            self.assertIn(needle, part)
        self.assertEqual(msg["Auto-Submitted"], "auto-generated")

    def test_changing_the_password_in_settings_sends_a_notice_to_the_users_own_address(self):
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        r = c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "another good password"})
        self.assertEqual(r.status_code, 200, r.text)
        msg = self.last_to(bob.email)
        self.assertEqual(msg["Subject"], "Your JobHuntPA password was changed")
        self.parts_ok(msg, "password")
        self.assertNotIn(bob.email, text_of(msg) + html_of(msg))
        self.assertEqual(len(self.flush()), 1)  # nobody else got anything

    def test_a_wrong_current_password_sends_nothing(self):
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        r = c.post("/api/auth/password", json={"current_password": "wrong wrong wrong", "new_password": "another good password"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.flush(), [])

    def test_a_reset_with_a_recovery_code_sends_a_notice(self):
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        code = c.get("/api/auth/recovery-codes").json()["codes"][0]
        r = self.anonymous().post("/api/auth/recover", json={"email": bob.email, "recovery_code": code,
                                                              "new_password": "brand new password"})
        self.assertEqual(r.status_code, 200, r.text)
        self.parts_ok(self.last_to(bob.email), "password")

    def test_an_admin_turning_off_two_step_sign_in_notifies_the_person_at_the_admin_copy_of_their_address(self):
        bob = helpers.make_user(password=PASSWORD)
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET mfa_enabled = 1 WHERE id = :i"), {"i": bob.id})
        with helpers.client() as admin:
            r = admin.post(f"/api/admin/users/{bob.id}/mfa/disable")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["notice_sent"])
        msg = self.last_to(bob.email)
        self.assertEqual(msg["Subject"], "Two-step sign-in was turned off on your JobHuntPA account")
        self.parts_ok(msg, "administrator")

    def test_no_notice_while_email_is_off_or_when_the_address_cannot_be_read(self):
        bob = helpers.make_user(password=PASSWORD)
        with mock.patch.dict(os.environ, {"SMTP_HOST": ""}), helpers.client() as admin:
            r = admin.post(f"/api/admin/users/{bob.id}/mfa/disable")
            self.assertEqual((r.status_code, r.json()["notice_sent"]), (200, False))
        stale = helpers.make_user(password=PASSWORD)
        with engine.begin() as conn:  # sealed to an older admin key: the admin can't read the address
            conn.execute(text("UPDATE users SET pii_admin_version = 0 WHERE id = :i"), {"i": stale.id})
        with helpers.client() as admin:
            r = admin.post(f"/api/admin/users/{stale.id}/mfa/disable")
        self.assertEqual((r.status_code, r.json()["notice_sent"]), (200, False))
        self.assertEqual(self.flush(), [])

    def test_a_notice_that_fails_never_undoes_the_action(self):
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        with mock.patch.object(email_templates, "security_notice", side_effect=RuntimeError("boom")):
            r = c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "another good password"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.login(bob.email, "another good password").status_code, 200)

    def login(self, email, password):
        helpers.ratelimit.reset_all()
        return self.anonymous().post("/api/auth/login", json={"email": email, "password": password})

    def test_every_event_has_a_subject_and_both_parts(self):
        for event in email_templates.SECURITY_EVENTS:
            subject, body, html, headers = email_templates.security_notice(event, base_url=BASE)
            self.assertTrue(subject and body and html.startswith("<!DOCTYPE html>"))
            self.assertIn(subject, html)  # the heading
            self.assertEqual(headers["Auto-Submitted"], "auto-generated")


# ---------------------------------------------------------------------------
# Invitations and the admin's reset button
# ---------------------------------------------------------------------------

class InviteMailTest(AccountMailCase):
    def invite(self, **body):
        with helpers.client() as admin:
            return admin.post("/api/admin/invites", json=body)

    def test_an_invitation_with_an_address_is_emailed_and_signs_up(self):
        email = self.new_email("friend")
        r = self.invite(email=email)
        self.assertEqual(r.status_code, 201, r.text)
        link = r.json()
        self.assertEqual((link["emailed"], link["email_status"]), (True, "queued"))
        msg = self.last_to(email)
        token = token_in(msg, "signup.html")  # links to the sign-up page itself
        self.assertEqual(f"{BASE}/signup.html?invite={token}", f"{BASE}/signup.html?invite=" + link["link"].split("invite=")[1])
        for part in (text_of(msg), html_of(msg)):
            self.assertIn("invited", part)
            self.assertNotIn("accept-invite", part)
        # the person opens the link on this server: the page's state call accepts it, then they sign up
        c = self.anonymous()
        state = c.get(f"/api/auth/state?invite={token}").json()["signup"]
        self.assertEqual((state["allowed"], state["invite"]), (True, {"valid": True, "email": email}))
        r = c.post("/api/auth/signup", json={"email": email, "password": PASSWORD, "invite": token})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(self.verified(self.user_id_of(email)))
        self.assertEqual(c.get("/api/auth/state?invite=" + token).json()["signup"]["invite"]["valid"], False)

    def test_only_an_invitation_that_was_emailed_to_the_address_makes_it_verified(self):
        """The admin copies a link and sends it some other way (or mistypes the address, or email is down): the mailbox
        has proved nothing, so the new account is sent the usual verification link."""
        cases = {
            "copied": dict(send=False),                                # the admin sends the link herself
            "queue full": dict(),                                      # the mail queue takes nothing
            "mail server refuses": dict(),                             # SMTP fails
        }
        for name, extra in cases.items():
            email = self.new_email("friend")
            if name == "queue full":
                with mock.patch.object(mail_queue, "submit", return_value=None):
                    link = self.invite(email=email, **extra).json()
                self.assertEqual(link["email_status"], "busy")
            elif name == "mail server refuses":
                with mock.patch.object(mailer, "send", return_value=mailer.SendResult("failed", "refused")):
                    link = self.invite(email=email, **extra).json()
                    self.flush()
                self.assertEqual(link["email_status"], "queued")  # (it was queued; the server then said no)
            else:
                link = self.invite(email=email, **extra).json()
                self.assertEqual(link["email_status"], "off")
            _c, r, _ = self.sign_up(email=email, invite=link["link"].split("invite=")[1])
            self.assertEqual(r.status_code, 201, (name, r.text))
            uid = self.user_id_of(email)
            self.assertFalse(self.verified(uid), name)
            self.assertEqual(len(token_rows(uid, "verify_email")), 1, name)
            self.assertIn("Confirm your email address", self.last_to(email)["Subject"], name)

    def test_the_flag_is_set_by_the_mail_job_and_only_on_the_invitation_that_was_sent(self):
        email = self.new_email("friend")
        link = self.invite(email=email).json()
        self.flush()
        token = link["link"].split("invite=")[1]
        row = rows(lambda s: s.query(AuthToken).filter(AuthToken.id == link["id"]).one())
        self.assertEqual(tokens.open_payload(token, "invite", row.payload_enc), {"email": email, "emailed": True})
        other = self.invite(email=self.new_email("other"), send=False).json()
        row = rows(lambda s: s.query(AuthToken).filter(AuthToken.id == other["id"]).one())
        self.assertEqual(tokens.open_payload(other["link"].split("invite=")[1], "invite", row.payload_enc),
                         {"email": other["email"], "emailed": False})

    def test_an_invitation_made_before_the_flag_existed_is_treated_as_not_emailed(self):
        email = self.new_email("old")
        link = self.invite(email=email).json()
        self.flush()
        token = link["link"].split("invite=")[1]
        with engine.begin() as conn:  # the payload as the older version sealed it
            conn.execute(text("UPDATE auth_tokens SET payload_enc = :p WHERE id = :i"),
                         {"p": tokens.seal_payload(token, "invite", {"email": email}), "i": link["id"]})
        _c, r, _ = self.sign_up(email=email, invite=token)
        self.assertEqual(r.status_code, 201)
        self.assertFalse(self.verified(self.user_id_of(email)))

    def test_the_inviters_name_is_in_the_message_but_never_their_address(self):
        boss = helpers.make_user(password=PASSWORD, name="Dana Boss")
        with helpers.client() as first:
            first.post(f"/api/admin/users/{boss.id}/role", json={"role": "admin"})
        self.addCleanup(self.demote, boss.id)
        email = self.new_email("friend")
        with helpers.client(boss.email, PASSWORD) as admin:
            self.assertEqual(admin.post("/api/admin/invites", json={"email": email}).status_code, 201)
        msg = self.last_to(email)
        for part in (text_of(msg), html_of(msg)):
            self.assertIn("Dana Boss", part)
            self.assertNotIn(boss.email, part)

    def demote(self, user_id):
        with helpers.client() as first:
            first.post(f"/api/admin/users/{user_id}/role", json={"role": "user"})

    def test_the_copy_the_link_fallback_always_works(self):
        with mock.patch.dict(os.environ, {"SMTP_HOST": ""}):
            r = self.invite(email=self.new_email())
            self.assertEqual((r.status_code, r.json()["emailed"], r.json()["email_status"]), (201, False, "not_set_up"))
            self.assertIn("/signup.html?invite=", r.json()["link"])
            r = self.invite(email=self.new_email(), send=True)
            self.assertEqual((r.status_code, r.json()["code"]), (409, "email_not_set_up"))
        r = self.invite()
        self.assertEqual((r.status_code, r.json()["emailed"], r.json()["email_status"]), (201, False, "no_address"))
        r = self.invite(send=True)
        self.assertEqual((r.status_code, r.json()["field"]), (422, "email"))
        r = self.invite(email=self.new_email(), send=False)
        self.assertEqual((r.json()["emailed"], r.json()["email_status"]), (False, "off"))
        self.assertEqual(self.flush(), [])

    def test_a_refused_invitation_sends_nothing(self):
        r = self.invite(email="not an address")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.flush(), [])

    def test_the_address_is_in_no_log_and_the_audit_entry_has_no_message(self):
        email = self.new_email("friend")
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("test").info("marker")
            self.assertEqual(self.invite(email=email).status_code, 201)
            self.flush()
        self.assertNotIn(email, "\n".join(logs.output))
        self.assertNotIn(email.split("@")[0], "\n".join(logs.output))
        self.assertIn("Email sent", "\n".join(logs.output))


class AdminResetButtonTest(AccountMailCase):
    def press(self, user_id: int):
        with helpers.client() as admin:
            return admin.post(f"/api/admin/users/{user_id}/reset-email")

    def test_it_emails_a_link_to_the_address_the_admin_can_read_and_the_link_resets_the_password(self):
        bob = helpers.make_user(password=PASSWORD)
        r = self.press(bob.id)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        msg = self.last_to(bob.email)
        self.assertEqual(msg["Subject"], "Reset your JobHuntPA password")
        token = token_in(msg, "reset-password.html")
        (row,) = token_rows(bob.id, "reset")
        self.assertEqual((row.created_by, bytes(row.token_hash)), (self.admin_id, tokens.hash_token(token)))
        self.assertLess(abs(timedelta(hours=1) - (row.expires_at - row.created_at)), timedelta(seconds=30))
        c = self.anonymous()
        self.assertEqual(c.post("/api/auth/reset/info", json={"token": token}).json(),
                         {"keeps_data": False, "mfa_required": False})
        r = c.post("/api/auth/reset", json={"token": token, "new_password": "chosen by the user", "accept_wipe": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.post("/api/auth/reset", json={"token": token, "new_password": "chosen by the user",
                                                         "accept_wipe": True}).status_code, 410)
        self.assertEqual(self.login(bob.email, "chosen by the user").status_code, 200)  # the admin never set it
        with helpers.client() as admin:
            self.assertIn("reset_email_sent", [e["action"] for e in admin.get("/api/admin/audit?limit=20").json()["entries"]])

    def login(self, email, password):
        helpers.ratelimit.reset_all()
        return self.anonymous().post("/api/auth/login", json={"email": email, "password": password})

    def test_it_says_when_email_is_off(self):
        bob = helpers.make_user()
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": ""}):
            r = self.press(bob.id)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "email_not_set_up"))
        self.assertTrue(r.json()["detail"].startswith("Email isn't set up yet"))
        self.assertEqual(token_rows(bob.id, "reset"), [])
        self.assertEqual(self.flush(), [])

    def test_details_an_admin_cannot_read_a_turned_off_account_and_an_unknown_one(self):
        stale = helpers.make_user()
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET pii_admin_version = 0 WHERE id = :i"), {"i": stale.id})
        r = self.press(stale.id)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "details_unavailable"))
        off = helpers.make_user()
        with helpers.client() as admin:
            admin.post(f"/api/admin/users/{off.id}/disable")
        r = self.press(off.id)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "disabled"))
        self.assertEqual(self.press(999999).status_code, 404)
        self.assertEqual(self.flush(), [])

    def test_it_is_limited_per_person_and_only_for_admins(self):
        bob = helpers.make_user()
        admin = self.signed_in()  # (signing in clears the limits, so one client makes all the presses)
        self.assertEqual([admin.post(f"/api/admin/users/{bob.id}/reset-email").status_code for _ in range(4)],
                         [200, 200, 200, 429])
        c = self.signed_in(bob)
        self.assertEqual(c.post(f"/api/admin/users/{bob.id}/reset-email").status_code, 403)
        self.assertEqual(len(self.to(bob.email)), 3)

    def test_the_press_does_not_wait_for_the_mail_server(self):
        bob = helpers.make_user()
        gate = threading.Event()
        real = mailer.send
        with mock.patch.object(mailer, "send", lambda *a, **k: (gate.wait(10), real(*a, **k))[1]):
            self.assertEqual(self.press(bob.id).status_code, 200)
            self.assertEqual(self.sent, [])
            gate.set()
            self.assertEqual(len(self.to(bob.email)), 1)


# ---------------------------------------------------------------------------
# The periodic clean-up
# ---------------------------------------------------------------------------

class CleanupTest(AccountMailCase):
    def setUp(self):
        super().setUp()
        self.bob = helpers.make_user()
        self.now = utcnow()

    def session(self, name: str, *, idle: timedelta, absolute: timedelta) -> str:
        sid = f"cleanup-{name}-{os.urandom(3).hex()}"
        db = SessionLocal()
        try:
            db.add(AuthSession(id=sid, user_id=self.bob.id, wrapped_dek=b"x", created_at=self.now,
                               idle_expires_at=self.now + idle, absolute_expires_at=self.now + absolute))
            db.commit()
        finally:
            db.close()
        return sid

    def token(self, kind: str, *, expires: timedelta, used: timedelta | None = None) -> bytes:
        h = os.urandom(32)
        db = SessionLocal()
        try:
            db.add(AuthToken(kind=kind, token_hash=h, user_id=self.bob.id, expires_at=self.now + expires,
                             used_at=self.now + used if used is not None else None))
            db.commit()
        finally:
            db.close()
        return h

    def present(self, table: str, column: str, value) -> bool:
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :v"), {"v": value}).scalar() == 1

    def test_expired_sessions_and_dead_links_go_and_live_ones_stay(self):
        live = self.session("live", idle=timedelta(hours=1), absolute=timedelta(days=1))
        idle = self.session("idle", idle=-timedelta(minutes=1), absolute=timedelta(days=1))
        old = self.session("old", idle=timedelta(hours=1), absolute=-timedelta(minutes=1))
        keep = [self.token("reset", expires=timedelta(minutes=30)),                       # still usable
                self.token("reset", expires=-timedelta(hours=2)),                          # expired two hours ago: kept a day
                self.token("verify_email", expires=timedelta(hours=5), used=-timedelta(hours=3)),  # used this morning
                self.token("invite", expires=-timedelta(days=3)),                          # in the admin's list
                self.token("invite", expires=timedelta(days=3), used=-timedelta(days=2))]
        gone = [self.token("reset", expires=-timedelta(days=2)), self.token("email_change", expires=timedelta(hours=1),
                                                                            used=-timedelta(days=2)),
                self.token("invite", expires=-timedelta(days=40)),
                self.token("invite", expires=timedelta(days=3), used=-timedelta(days=35))]
        counts = auth.cleanup_tick(self.now)
        self.assertEqual(counts, {"sessions": 2, "tokens": 2, "invites": 2})
        self.assertTrue(self.present("sessions", "id", live))
        self.assertFalse(self.present("sessions", "id", idle) or self.present("sessions", "id", old))
        for h in keep:
            self.assertTrue(self.present("auth_tokens", "token_hash", h))
        for h in gone:
            self.assertFalse(self.present("auth_tokens", "token_hash", h))
        self.assertEqual(auth.cleanup_tick(self.now), {"sessions": 0, "tokens": 0, "invites": 0})

    def test_it_runs_every_ten_minutes_with_the_scheduler_loop(self):
        self.session("idle", idle=-timedelta(minutes=1), absolute=timedelta(days=1))
        schedule._last_cleanup = None
        from datetime import datetime, timezone
        t0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(auth, "cleanup_tick") as tick:
            self.assertTrue(schedule.housekeeping(t0))
            self.assertFalse(schedule.housekeeping(t0 + timedelta(minutes=9)))
            self.assertTrue(schedule.housekeeping(t0 + timedelta(minutes=10)))
            self.assertEqual(tick.call_count, 2)
        schedule._last_cleanup = None

    def test_a_failing_clean_up_never_stops_the_loop(self):
        schedule._last_cleanup = None
        with mock.patch.object(auth, "cleanup_tick", side_effect=RuntimeError("boom")):
            self.assertTrue(schedule.housekeeping())
        schedule._last_cleanup = None

    def test_it_is_off_with_the_scheduler(self):
        before = {t.name for t in threading.enumerate()}
        with mock.patch.dict(os.environ, {"JOBHUNT_SCHEDULER": "off"}):
            schedule.start_scheduler(lambda ws: {})
        self.assertEqual({t.name for t in threading.enumerate()} - before, set())  # no loop, so no clean-up either

    def test_the_loop_calls_it(self):
        ran = threading.Event()
        schedule._last_cleanup = None
        with mock.patch.dict(os.environ, {"JOBHUNT_SCHEDULER": "on"}), \
                mock.patch.object(schedule, "TICK_SECONDS", 0.05), \
                mock.patch.object(schedule, "tick"), \
                mock.patch.object(auth, "cleanup_tick", side_effect=lambda *a: ran.set()):
            schedule.start_scheduler(lambda ws: {})
            try:
                self.assertTrue(ran.wait(10))
            finally:
                schedule.stop_scheduler()
        schedule._last_cleanup = None


if __name__ == "__main__":
    unittest.main()
