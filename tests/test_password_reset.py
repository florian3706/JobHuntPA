"""Forgot password by email and the reset its link leads to (accounts design §6.5, §13 item 1, §16; Phase H): the
same answer for every address, single-use and expiring links, two-step sign-in during a reset, data kept with
background access and deleted without it (a sealed value read afterwards), and the admin key a wiped admin loses.
SMTP is captured, never used.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)
from tests.mail_helpers import BASE, MailTestCase, html_of, text_of, token_in  # noqa: E402

from sqlalchemy import text  # noqa: E402

from starlette.responses import Response  # noqa: E402

from backend import crypto, docs, lockout, mail_queue, mailer, tokens, totp, users  # noqa: E402
from backend.db import (  # noqa: E402
    AdminKeyGrant,
    AuthSession,
    AuthToken,
    ChatMessage,
    ChatThread,
    Document,
    FitResult,
    Job,
    Membership,
    RecoveryCode,
    SearchRun,
    SessionLocal,
    User,
    UserKeys,
    Workspace,
    engine,
    utcnow,
)

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new password"
SAME_BODY = {"ok": True, "message": "If an account has that address, we've sent a link to it. The link works once and "
                                    "expires in 1 hour. Check your spam folder, and wait a few minutes before asking "
                                    "again."}


def rows(fn):
    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


class ResetCase(MailTestCase):
    def setUp(self):
        super().setUp()
        self.clock = [(int(time.time()) // totp.STEP_SECONDS) * totp.STEP_SECONDS + 15.0]
        patch = mock.patch("backend.totp.now", lambda: self.clock[0])
        patch.start()
        self.addCleanup(patch.stop)

    def step(self) -> int:
        return int(self.clock[0] // totp.STEP_SECONDS)

    def advance(self, steps: int = 1) -> None:
        self.clock[0] += steps * totp.STEP_SECONDS

    # -- accounts ---------------------------------------------------------------------------------------------------

    def user(self, *, background: bool = False, mfa: bool = False, pin: str = "Home marker"):
        """An account with a sealed pin in its workspace; ``background`` turns background access on, ``mfa`` two-step
        sign-in on (returns the secret too)."""
        acct = helpers.make_user(password=PASSWORD)
        c = self.signed_in(acct)
        self.assertEqual(c.post("/api/pins", json={"label": pin, "kind": "home", "lat": -33.8, "lng": 151.2,
                                                   "radius_km": 10}).status_code, 201)
        if background:
            self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)
        secret = None
        if mfa:
            secret = c.post("/api/auth/mfa/setup").json()["secret"]
            self.assertEqual(c.post("/api/auth/mfa/enable", json={"code": totp.code_at(secret, self.step())}).status_code,
                             200)
            self.advance(2)
        c.post("/api/auth/logout")
        return (acct, secret) if mfa else acct

    def ask(self, address: str, anon=None):
        return (anon or self.anonymous()).post("/api/auth/forgot", json={"email": address})

    def link(self, acct) -> str:
        """Asks for a reset link for ``acct`` and returns its token."""
        self.assertEqual(self.ask(acct.email).status_code, 202)
        return token_in(self.last_to(acct.email), "reset-password.html")

    def reset(self, token: str, anon=None, **fields):
        body = {"token": token, "new_password": NEW_PASSWORD, **fields}
        return (anon or self.anonymous()).post("/api/auth/reset", json=body)

    def login(self, acct, password=NEW_PASSWORD):
        c = self.anonymous()
        helpers.ratelimit.reset_all()
        r = c.post("/api/auth/login", json={"email": acct.email, "password": password})
        return c, r

    def pin_labels(self, c) -> list[str]:
        return [p["label"] for p in c.get("/api/pins").json()]


# ---------------------------------------------------------------------------
# Asking for the link
# ---------------------------------------------------------------------------

class ForgotTest(ResetCase):
    def test_known_and_unknown_addresses_get_the_same_202(self):
        bob = self.user()
        known = self.ask(bob.email)
        unknown = self.ask("nobody-here@example.invalid")
        self.assertEqual((known.status_code, unknown.status_code), (202, 202))
        self.assertEqual(known.json(), SAME_BODY)
        self.assertEqual(unknown.json(), SAME_BODY)
        self.assertEqual(known.headers.get("content-type"), unknown.headers.get("content-type"))
        self.assertEqual(len(self.to(bob.email)), 1)
        self.assertEqual(len(self.flush()), 1)  # nothing went to the address that has no account
        self.assertEqual(rows(lambda s: s.query(AuthToken).filter(AuthToken.kind == "reset",
                                                                  AuthToken.user_id.is_(None)).count()), 0)  # none for it

    def test_the_message_has_both_parts_and_a_fragment_link(self):
        bob = self.user()
        self.ask(bob.email)
        msg = self.last_to(bob.email)
        self.assertEqual(msg["Subject"], "Reset your JobHuntPA password")
        token = token_in(msg, "reset-password.html")  # the same token in the text and in the HTML
        for part in (text_of(msg), html_of(msg)):
            self.assertIn(f"{BASE}/reset-password.html#token={token}", part)
            self.assertNotIn("?token=", part)  # never in the query: it would reach server logs and Referer headers
        self.assertEqual(msg.get_body(preferencelist=("plain",)).get_content_type(), "text/plain")
        self.assertEqual(msg.get_body(preferencelist=("html",)).get_content_type(), "text/html")

    def test_typing_the_address_another_way_still_finds_the_account(self):
        bob = self.user()
        self.assertEqual(self.ask("  " + bob.email.upper() + " ").status_code, 202)
        # the link goes to the address in one spelling (the one the account is found by), not as it was typed
        self.assertEqual(self.to(bob.email.upper().strip()), [])
        msg = self.last_to(bob.email.lower())
        row = rows(lambda s: s.query(AuthToken).filter(AuthToken.kind == "reset", AuthToken.user_id == bob.id).one())
        token = token_in(msg, "reset-password.html")
        self.assertEqual(tokens.open_payload(token, "reset", row.payload_enc), {"email": bob.email.lower()})

    def test_the_token_is_stored_as_a_hash_with_a_one_hour_life_and_a_sealed_address(self):
        bob = self.user()
        token = self.link(bob)
        row = rows(lambda s: s.query(AuthToken).filter(AuthToken.kind == "reset", AuthToken.user_id == bob.id).one())
        self.assertEqual(bytes(row.token_hash), tokens.hash_token(token))
        self.assertNotIn(token.encode(), bytes(row.token_hash) + bytes(row.payload_enc))
        self.assertNotIn(bob.email.encode(), bytes(row.payload_enc))  # the address is sealed under a key made from the token
        self.assertLess(abs(timedelta(hours=1) - (row.expires_at - row.created_at)), timedelta(seconds=30))
        self.assertEqual(len(token), 43 + 22)  # 32 random bytes, then the server key's signature of them
        self.assertEqual(tokens.open_payload(token, "reset", row.payload_enc), {"email": bob.email})

    def test_a_new_link_replaces_the_old_one(self):
        bob = self.user()
        first = self.link(bob)
        second = self.link(bob)
        self.assertNotEqual(first, second)
        self.assertEqual(self.anonymous().post("/api/auth/reset/info", json={"token": first}).status_code, 410)
        self.assertEqual(self.anonymous().post("/api/auth/reset/info", json={"token": second}).status_code, 200)

    def test_a_badly_typed_address_is_a_plain_422(self):
        for value in ("", "not an address", "a@b", "x" * 400 + "@example.com"):
            r = self.ask(value)
            self.assertEqual((r.status_code, r.json().get("field")), (422, "email"), value)
        self.assertEqual(self.flush(), [])

    def test_limits_per_address_answer_the_same_for_known_and_unknown(self):
        bob = self.user()
        ghost = "ghost-account@example.invalid"
        for _ in range(5):
            helpers.ratelimit.AUTH.reset()  # (the per-client limit is not what is under test)
            self.assertEqual(self.ask(bob.email).status_code, 202)
            self.assertEqual(self.ask(ghost).status_code, 202)
        self.assertEqual(len(self.to(bob.email)), 3)  # 3 an hour; the others were quietly dropped
        self.assertEqual(self.to(ghost), [])
        helpers.ratelimit.AUTH.reset()
        self.assertEqual(self.ask(bob.email).json(), SAME_BODY)

    def test_a_turned_off_account_is_told_nothing_and_sent_nothing(self):
        bob = self.user()
        with helpers.client() as admin:
            self.assertEqual(admin.post(f"/api/admin/users/{bob.id}/disable").status_code, 200)
        try:
            r = self.ask(bob.email)
            self.assertEqual((r.status_code, r.json()), (202, SAME_BODY))
            self.assertEqual(self.flush(), [])
        finally:
            with helpers.client() as admin:
                admin.post(f"/api/admin/users/{bob.id}/enable")

    def test_the_request_never_waits_for_the_mail_server(self):
        bob = self.user()
        gate, started = threading.Event(), threading.Event()
        real = mailer.send

        def slow(*args, **kwargs):
            started.set()
            gate.wait(10)
            return real(*args, **kwargs)

        with mock.patch.object(mailer, "send", slow):
            began = time.monotonic()
            r = self.ask(bob.email)
            took = time.monotonic() - began
            self.assertEqual(r.status_code, 202)
            self.assertLess(took, 5)
            self.assertTrue(started.wait(10))
            self.assertEqual(self.sent, [])  # still stuck in the mail server's hands
            gate.set()
            self.assertEqual(len(self.to(bob.email)), 1)

    def test_only_json_from_this_site(self):
        bob = self.user()
        c = self.anonymous()
        self.assertEqual(c.post("/api/auth/forgot", data={"email": bob.email}).status_code, 415)
        self.assertEqual(c.post("/api/auth/forgot", json={"email": bob.email},
                                headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.flush(), [])

    def test_the_sign_in_limit_per_client_applies(self):
        bob = self.user()
        c = self.anonymous()
        helpers.ratelimit.reset_all()
        codes = [c.post("/api/auth/forgot", json={"email": bob.email}).status_code for _ in range(12)]
        self.assertEqual(codes[:10], [202] * 10)
        self.assertEqual(codes[10:], [429, 429])

    def test_an_extra_field_is_refused(self):
        self.assertEqual(self.anonymous().post("/api/auth/forgot", json={"email": "a@example.com", "x": 1}).status_code,
                         422)


class ForgotLimitsTest(ResetCase):
    """Anonymous requests share limits with nobody who is trying to help, and can't leave the owner without a link."""

    def burst(self, address: str, n: int = 8) -> None:
        for _ in range(n):
            helpers.ratelimit.AUTH.reset()  # (the per-client limit is not what is under test)
            self.assertEqual(self.ask(address).status_code, 202)

    def test_anonymous_requests_cannot_use_up_the_admins_reset_button(self):
        bob = self.user()
        self.burst(bob.email)
        self.assertEqual(len(self.to(bob.email)), 3)  # (3 an hour; the rest were dropped, still with a 202)
        with helpers.client() as admin:
            r = admin.post(f"/api/admin/users/{bob.id}/reset-email")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(len(self.to(bob.email)), 4)
            # the button has its own limit, per person: 3 an hour
            codes = [admin.post(f"/api/admin/users/{bob.id}/reset-email").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_the_admins_button_does_not_use_up_what_the_person_may_ask_for_themselves(self):
        bob = self.user()
        with helpers.client() as admin:
            for _ in range(3):
                self.assertEqual(admin.post(f"/api/admin/users/{bob.id}/reset-email").status_code, 200)
        self.assertEqual(self.ask(bob.email).status_code, 202)
        self.assertEqual(len(self.to(bob.email)), 4)

    def test_an_owner_with_no_usable_link_can_always_get_one(self):
        """Six requests used up the day's allowance; the last link expires an hour later. The owner's own request,
        then, must still go out, or whoever keeps the limits used up denies them reset by email for a day."""
        bob = self.user()
        self.burst(bob.email)
        self.assertEqual(len(self.to(bob.email)), 3)
        helpers.ratelimit.AUTH.reset()
        self.assertEqual(self.ask(bob.email).status_code, 202)  # a link is still live: held back
        self.assertEqual(len(self.to(bob.email)), 3)
        with engine.begin() as conn:  # an hour passes: every link has expired
            conn.execute(text("UPDATE auth_tokens SET expires_at = :t WHERE user_id = :i AND kind = 'reset'"),
                         {"t": utcnow() - timedelta(minutes=1), "i": bob.id})
        helpers.ratelimit.AUTH.reset()
        self.assertEqual(self.ask(bob.email).status_code, 202)
        self.assertEqual(len(self.to(bob.email)), 4)
        token = token_in(self.last_to(bob.email), "reset-password.html")
        self.assertEqual(self.anonymous().post("/api/auth/reset/info", json={"token": token}).status_code, 200)
        # ... but only one such extra email while that link is live
        helpers.ratelimit.AUTH.reset()
        self.ask(bob.email)
        self.assertEqual(len(self.to(bob.email)), 4)

    def test_a_burst_with_no_usable_link_sends_only_one_extra_email(self):
        """The exception is counted when the request comes in, not when the mail thread makes the token: requests that
        arrive together (or while the thread is busy) can't each send one."""
        bob = self.user()
        server = users.server_keys()
        for _ in range(3):  # the limits are used up, and Bob has no link at all
            helpers.ratelimit.FORGOT_ADDRESS.hit(server.email_hash(bob.email))
            helpers.ratelimit.RESET_ACCOUNT.hit(bob.id)
        jobs = []
        lock = threading.Lock()

        def hold(fn, *args, **kwargs):  # (the mail thread is busy: nothing runs yet)
            with lock:
                jobs.append(fn.__name__)

        statuses = []

        def ask(c):
            statuses.append(c.post("/api/auth/forgot", json={"email": bob.email}).status_code)

        clients = [self.anonymous(client=(f"203.0.113.{n}", 4000)) for n in range(1, 13)]
        helpers.ratelimit.AUTH.reset()
        with mock.patch.object(mail_queue, "submit", hold), \
                mock.patch.object(helpers.ratelimit, "AUTH", helpers.ratelimit.Limiter([(1000, 60)])):
            threads = [threading.Thread(target=ask, args=(c,)) for c in clients]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertEqual(statuses, [202] * 12)
        self.assertEqual(jobs.count("_forgot_job"), 1)
        self.assertEqual(jobs.count("decoy"), 11)

    def test_held_back_requests_are_logged_by_account_id_only(self):
        bob = self.user()
        self.burst(bob.email, 3)
        with self.assertLogs("backend.password_reset", "INFO") as logs:
            helpers.ratelimit.AUTH.reset()
            self.ask(bob.email)
        text_ = "\n".join(logs.output)
        self.assertIn(f"user {bob.id}", text_)
        self.assertNotIn(bob.email, text_)


class ForgotTimingTest(ResetCase):
    """The mail thread is shared: a real account puts a send on it, so an address with no account must put a pause as
    long as a send, or the delay of the next email shows which addresses have accounts."""

    def test_every_answer_queues_one_job_and_an_address_with_no_account_queues_a_decoy(self):
        bob = self.user()
        jobs = []
        real = mail_queue.submit

        def spy(fn, *args, **kwargs):
            jobs.append(fn.__name__)
            return real(fn, *args, **kwargs)

        with mock.patch.object(mail_queue, "submit", spy):
            self.ask(bob.email)
            self.ask("nobody-here@example.invalid")
            for _ in range(3):  # the 2nd and 3rd go out; the 4th is held back by the limit: a decoy, like an unknown one
                helpers.ratelimit.AUTH.reset()
                self.ask(bob.email)
        self.assertEqual(jobs, ["_forgot_job", "decoy", "_forgot_job", "_forgot_job", "decoy"])

    def test_the_decoy_lasts_as_long_as_a_typical_send(self):
        with mail_queue._lock:
            mail_queue._send_times.clear()
        self.assertEqual(mail_queue.typical_send_seconds(), mail_queue.DEFAULT_SEND_S)
        with mail_queue._lock:
            mail_queue._send_times.extend([0.2, 0.4, 0.3])
        self.addCleanup(lambda: mail_queue._send_times.clear())
        self.assertAlmostEqual(mail_queue.typical_send_seconds(), 0.3)
        with mock.patch.object(mail_queue.time, "sleep") as sleep:
            mail_queue.decoy()
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.3)
        with mail_queue._lock:
            mail_queue._send_times.append(500.0)
        self.assertLessEqual(mail_queue.typical_send_seconds(), mail_queue.MAX_DECOY_S)

    def test_real_sends_are_timed(self):
        bob = self.user()
        with mail_queue._lock:
            mail_queue._send_times.clear()
        self.assertEqual(self.ask(bob.email).status_code, 202)
        self.flush()
        with mail_queue._lock:
            self.assertEqual(len(mail_queue._send_times), 1)


class ForgotWithoutEmailTest(ResetCase):
    email_on = False

    def test_it_says_email_is_not_set_up(self):
        r = self.ask("someone@example.invalid")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "email_not_set_up"))
        self.assertIn("recovery code", r.json()["detail"])
        self.assertTrue(r.json()["detail"].startswith("This JobHuntPA can't send reset links"))
        self.assertFalse(self.anonymous().get("/api/auth/state").json()["email_enabled"])

    def test_the_state_says_when_email_is_on(self):
        with mock.patch.dict("os.environ", {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "jobs@example.com",
                                            "PUBLIC_BASE_URL": BASE}):
            self.assertTrue(self.anonymous().get("/api/auth/state").json()["email_enabled"])
        with mock.patch.dict("os.environ", {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "jobs@example.com"}):
            self.assertFalse(self.anonymous().get("/api/auth/state").json()["email_enabled"])  # no public address


# ---------------------------------------------------------------------------
# Opening the link
# ---------------------------------------------------------------------------

class LinkTest(ResetCase):
    def info(self, token):
        return self.anonymous().post("/api/auth/reset/info", json={"token": token})

    def test_info_says_whether_the_data_is_kept_and_a_code_is_needed(self):
        locked, kept = self.user(), self.user(background=True)
        (mfa, _secret) = self.user(mfa=True)
        self.assertEqual(self.info(self.link(locked)).json(), {"keeps_data": False, "mfa_required": False})
        self.assertEqual(self.info(self.link(kept)).json(), {"keeps_data": True, "mfa_required": False})
        self.assertEqual(self.info(self.link(mfa)).json(), {"keeps_data": False, "mfa_required": True})

    def test_info_does_not_use_the_link_up(self):
        bob = self.user()
        token = self.link(bob)
        for _ in range(3):
            self.assertEqual(self.info(token).status_code, 200)
        self.assertEqual(self.reset(token, accept_wipe=True).status_code, 200)

    def test_unknown_used_and_expired_links_all_look_the_same(self):
        bob = self.user()
        used = self.link(bob)
        self.assertEqual(self.reset(used, accept_wipe=True).status_code, 200)
        expired = self.link(bob)
        with engine.begin() as conn:
            conn.execute(text("UPDATE auth_tokens SET expires_at = :t WHERE token_hash = :h"),
                         {"t": utcnow() - timedelta(minutes=1), "h": tokens.hash_token(expired)})
        answers = set()
        for token in ("nonsense", "x" * 500, "", used, expired):
            helpers.ratelimit.reset_all()
            for r in (self.info(token), self.reset(token, accept_wipe=True, new_password=PASSWORD)):
                answers.add((r.status_code, r.json()["code"], r.json()["detail"]))
        self.assertEqual({a[:2] for a in answers}, {(410, "link_invalid")})
        self.assertEqual(len({a[2] for a in answers}), 1)  # one message for all of them

    def test_a_link_works_once(self):
        bob = self.user(background=True)
        token = self.link(bob)
        first = self.reset(token)
        self.assertEqual(first.status_code, 200, first.text)
        second = self.reset(token, new_password="another password again")
        self.assertEqual((second.status_code, second.json()["code"]), (410, "link_invalid"))
        self.assertEqual(self.login(bob)[1].status_code, 200)  # the first password stands
        self.assertEqual(self.login(bob, "another password again")[1].status_code, 401)

    def test_two_requests_at_the_same_moment_only_one_wins(self):
        bob = self.user(background=True)
        token = self.link(bob)
        results = []

        def go(password):
            results.append(self.reset(token, new_password=password).status_code)

        threads = [threading.Thread(target=go, args=(f"password number {i} here",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(results), [200, 410])

    def test_an_expired_link_is_refused_and_nothing_changes(self):
        bob = self.user(background=True)
        token = self.link(bob)
        with engine.begin() as conn:
            conn.execute(text("UPDATE auth_tokens SET expires_at = :t WHERE token_hash = :h"),
                         {"t": utcnow() - timedelta(seconds=1), "h": tokens.hash_token(token)})
        r = self.reset(token)
        self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        self.assertEqual(self.login(bob, PASSWORD)[1].status_code, 200)

    def test_a_link_of_another_kind_is_not_a_reset_link(self):
        bob = self.user()
        db = SessionLocal()
        try:
            token, _row = tokens.create(db, "verify_email", ttl=timedelta(hours=1), user_id=bob.id,
                                        payload={"email": bob.email})
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.info(token).status_code, 410)
        self.assertEqual(self.reset(token, accept_wipe=True).status_code, 410)

    def test_a_weak_password_is_refused_and_keeps_the_link(self):
        bob = self.user(background=True)
        token = self.link(bob)
        r = self.reset(token, new_password="short")
        self.assertEqual((r.status_code, r.json()["field"]), (422, "new_password"))
        self.assertEqual(self.reset(token).status_code, 200)

    def test_a_turned_off_account_cannot_reset(self):
        bob = self.user(background=True)
        token = self.link(bob)
        with helpers.client() as admin:
            admin.post(f"/api/admin/users/{bob.id}/disable")
        try:
            self.assertEqual(self.info(token).status_code, 403)
            r = self.reset(token)
            self.assertEqual((r.status_code, r.json()["code"]), (403, "disabled"))
        finally:
            with helpers.client() as admin:
                admin.post(f"/api/admin/users/{bob.id}/enable")
        self.assertEqual(self.login(bob, PASSWORD)[1].status_code, 200)  # still the old password

    def test_only_json_from_this_site(self):
        bob = self.user()
        token = self.link(bob)
        c = self.anonymous()
        self.assertEqual(c.post("/api/auth/reset", data={"token": token, "new_password": NEW_PASSWORD}).status_code, 415)
        self.assertEqual(c.post("/api/auth/reset", json={"token": token, "new_password": NEW_PASSWORD},
                                headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(c.post("/api/auth/reset/info", json={"token": token},
                                headers={"Origin": "https://evil.example"}).status_code, 403)


# ---------------------------------------------------------------------------
# Data kept (background access)
# ---------------------------------------------------------------------------

class KeepDataTest(ResetCase):
    def test_background_access_keeps_the_data_and_signs_the_person_in(self):
        bob = self.user(background=True, pin="Kept marker")
        old = self.signed_in(bob)  # another browser, signed in with the old password
        token = self.link(bob)
        c = self.anonymous()
        r = self.reset(token, anon=c)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["kept_data"], body["wiped"], body["admin_key_lost"]), (True, False, False))
        self.assertEqual(len(body["recovery_codes"]), 8)  # new ones: the old ones were replaced (below)
        self.assertEqual(body["user"], {"id": bob.id, "role": "user"})
        c.headers["X-CSRF-Token"] = body["csrf"]
        self.assertEqual(c.get("/api/auth/me").status_code, 200)  # signed in
        self.assertEqual(self.pin_labels(c), ["Kept marker"])  # the sealed value opens with the same data key
        self.assertEqual(old.get("/api/auth/me").status_code, 401)  # every other session ended
        self.assertEqual(self.login(bob, PASSWORD)[1].status_code, 401)
        fresh, r = self.login(bob)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pin_labels(fresh), ["Kept marker"])

    def test_accept_wipe_is_not_needed_and_changes_nothing_when_the_data_is_kept(self):
        bob = self.user(background=True, pin="Kept marker")
        r = self.reset(self.link(bob), accept_wipe=True)
        self.assertEqual((r.status_code, r.json()["kept_data"]), (200, True))
        self.assertEqual(self.pin_labels(self.login(bob)[0]), ["Kept marker"])

    def test_the_old_recovery_codes_stop_working_and_the_new_ones_are_shown(self):
        """Each recovery code opens the data key. Whoever held the account before (someone who signed up with this
        address first, turned background access on and kept the codes) must not be able to use one afterwards."""
        bob = self.user(background=True, pin="Kept marker")
        old = helpers.show_codes(self.signed_in(bob)).json()["codes"]
        self.assertEqual(len(old), 8)
        r = self.reset(self.link(bob))
        self.assertEqual(r.status_code, 200, r.text)
        new = r.json()["recovery_codes"]
        self.assertEqual(len(new), 8)
        self.assertFalse(set(old) & set(new))
        for code in old[:3]:
            helpers.ratelimit.reset_all()
            gone = self.anonymous().post("/api/auth/recover", json={"email": bob.email, "recovery_code": code,
                                                                    "new_password": "attacker's new password"})
            self.assertEqual((gone.status_code, gone.json()["code"]), (401, "wrong_recovery"))
        self.assertEqual(self.login(bob)[1].status_code, 200)  # the password the owner chose still stands
        # Nothing in the database wraps the key for an old code, and the new ones are kept sealed until saved.
        rows_now = rows(lambda s: s.query(RecoveryCode).filter(RecoveryCode.user_id == bob.id).all())
        self.assertEqual(len(rows_now), 8)
        self.assertTrue(all(rc.used_at is None and rc.wrapped_dek is not None for rc in rows_now))
        c = self.anonymous()
        c.post("/api/auth/login", json={"email": bob.email, "password": NEW_PASSWORD})
        self.assertEqual(c.get("/api/auth/me").json()["recovery_codes_pending"], True)
        helpers.ratelimit.reset_all()
        mine = self.anonymous().post("/api/auth/recover", json={"email": bob.email, "recovery_code": new[0],
                                                               "new_password": "yet another password"})
        self.assertEqual(mine.status_code, 200, mine.text)

    def test_a_pending_authenticator_setup_of_the_previous_holder_is_dropped(self):
        bob = self.user(background=True)
        c = self.signed_in(bob)
        self.assertEqual(c.post("/api/auth/mfa/setup").status_code, 200)  # started, never finished
        self.assertIsNotNone(rows(lambda s: s.get(User, bob.id)).totp_pending_enc)
        self.assertEqual(self.reset(self.link(bob)).status_code, 200)
        row = rows(lambda s: s.get(User, bob.id))
        self.assertIsNone(row.totp_pending_enc)
        self.assertFalse(row.mfa_enabled)

    def test_a_notice_goes_to_the_address_and_names_no_address(self):
        bob = self.user(background=True)
        self.reset(self.link(bob))
        notice = [m for m in self.to(bob.email) if "password was changed" in m["Subject"]]
        self.assertEqual(len(notice), 1)
        for part in (text_of(notice[0]), html_of(notice[0])):
            self.assertIn("password", part)
            self.assertNotIn(bob.email, part)

    def test_the_old_password_copy_of_the_key_is_gone_and_the_lockout_cleared(self):
        bob = self.user(background=True)
        helpers.lock_out(bob.id)
        self.assertGreater(helpers.ratelimit.guesses.waiting(bob.id), 0)
        self.assertEqual(self.reset(self.link(bob)).status_code, 200)  # no lockout check without two-step sign-in
        self.assertEqual((helpers.ratelimit.guesses.waiting(bob.id), helpers.wrong_tries(bob.id)), (0, 0))
        self.assertTrue(rows(lambda s: s.get(User, bob.id)).email_verified)  # the mailbox answered

    def test_it_works_for_someone_else_signed_in_in_the_same_browser(self):
        bob, carol = self.user(background=True), self.user()
        c = self.signed_in(carol)
        r = self.reset(self.link(bob), anon=c)
        self.assertEqual(r.status_code, 200)
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertEqual(c.get("/api/auth/me").json()["user"]["id"], bob.id)
        # the browser's old session is replaced, as at any new sign-in
        self.assertEqual(rows(lambda s: s.query(AuthSession).filter(AuthSession.user_id == carol.id).count()), 0)


# ---------------------------------------------------------------------------
# Data deleted (no background access)
# ---------------------------------------------------------------------------

class WipeTest(ResetCase):
    def populate(self, acct) -> dict:
        """Rows of every kind in two workspaces of ``acct`` and a sealed upload each; the other person's pin stays."""
        db = SessionLocal()
        files = []
        try:
            with helpers.as_account(acct):
                second = Workspace(name="Second", owner_id=acct.id)
                db.add(second)
                db.flush()
                for ws in (acct.workspace_id, second.id):
                    job = Job(workspace_id=ws, url=f"https://wipe.example/{ws}", title="Wipe marker job")
                    db.add(job)
                    db.flush()
                    db.add(FitResult(job_id=job.id, status="ok", score=80, evidence_json="{}"))
                    db.add(SearchRun(workspace_id=ws, kind="search", state="done", progress_json="{}"))
                    stored = docs.save_upload(f"wipe marker bytes {ws}".encode())
                    files.append(stored)
                    db.add(Document(workspace_id=ws, filename="r.pdf", filetype=".pdf", stored_name=stored,
                                    text="wipe marker text", kind="resume"))
                    thread = ChatThread(workspace_id=ws, job_ids_json=f"[{job.id}]", title="wipe chat")
                    db.add(thread)
                    db.flush()
                    db.add(ChatMessage(thread_id=thread.id, role="user", content="wipe marker"))
                db.query(Membership).filter(Membership.user_id == acct.id).update(
                    {"billing_self": b"sealed-under-the-old-key"})
                db.commit()
                return {"ws": [acct.workspace_id, second.id], "files": files}
        finally:
            db.close()

    def count(self, table: str, where: str, **params) -> int:
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), params).scalar()

    def test_without_background_access_the_wipe_must_be_accepted(self):
        bob = self.user(pin="Lost marker")
        made = self.populate(bob)
        token = self.link(bob)
        r = self.reset(token)
        self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (409, "wipe_not_accepted", "accept_wipe"))
        self.assertIn("deletes them", r.json()["detail"])
        r = self.reset(token, accept_wipe=False)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.login(bob, PASSWORD)[1].status_code, 200)  # nothing changed
        self.assertEqual(self.count("jobs", "workspace_id = :w", w=made["ws"][0]), 1)
        self.assertEqual(self.anonymous().post("/api/auth/reset/info", json={"token": token}).status_code, 200)  # link kept

    def test_accepting_deletes_the_saved_data_and_starts_again(self):
        bob, carol = self.user(pin="Lost marker"), self.user(pin="Carol marker")
        made = self.populate(bob)
        other_files = self.populate(carol)["files"]
        before = rows(lambda s: s.get(User, bob.id))
        keys_before = rows(lambda s: s.get(UserKeys, bob.id))
        old_admin_copy = bytes(before.pii_admin)
        token = self.link(bob)
        c = self.anonymous()
        r = self.reset(token, anon=c, accept_wipe=True)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["kept_data"], body["wiped"], body["admin_key_lost"]), (False, True, False))
        codes = body["recovery_codes"]
        self.assertEqual(len(codes), 8)
        c.headers["X-CSRF-Token"] = body["csrf"]
        # the saved data is gone, with its files, and the person has a fresh workspace
        marks = ", ".join(str(i) for i in made["ws"])
        for table in ("jobs", "documents", "search_runs", "chat_threads"):
            self.assertEqual(self.count(table, f"workspace_id IN ({marks})"), 0, table)
        self.assertEqual(self.count("workspaces", f"id IN ({marks})"), 0)
        self.assertEqual(self.pin_labels(c), [])
        me = c.get("/api/auth/me").json()
        self.assertEqual(len(me["workspaces"]), 1)
        self.assertNotIn(c.get("/api/workspaces").json()[0]["id"], made["ws"])  # an id is never given out twice
        self.assertEqual([w["name"] for w in c.get("/api/workspaces").json()], ["My job search"])
        for name in made["files"]:
            self.assertFalse((docs.UPLOAD_DIR / name).exists(), "an upload of the wiped account is still on disk")
        for name in other_files:
            self.assertTrue((docs.UPLOAD_DIR / name).exists())
        # another person's data is untouched
        self.assertEqual(self.pin_labels(self.login(carol, PASSWORD)[0]), ["Carol marker"])
        # the account row, membership and the admin copy of the details stay; the keys and the personal copy are new
        after = rows(lambda s: s.get(User, bob.id))
        keys_after = rows(lambda s: s.get(UserKeys, bob.id))
        self.assertEqual(bytes(after.email_hash), bytes(before.email_hash))
        self.assertEqual(bytes(after.pii_admin), old_admin_copy)
        self.assertNotEqual(bytes(keys_after.dek_kid), bytes(keys_before.dek_kid))
        self.assertNotEqual(bytes(keys_after.x25519_pub), bytes(keys_before.x25519_pub))
        self.assertEqual(c.get("/api/auth/me").json()["user"]["email"], bob.email)  # pii_self, from the link's address
        self.assertEqual(self.count("memberships", "user_id = :u AND billing_self IS NULL", u=bob.id), 1)
        self.assertEqual((keys_after.server_wrapped_dek, after.onboarding_step), (None, 1))
        # new recovery codes, shown on the next page; the old ones are gone
        self.assertEqual(helpers.show_codes(c, NEW_PASSWORD).json()["codes"], codes)
        self.assertEqual(self.count("recovery_codes", "user_id = :u AND wrapped_dek IS NOT NULL", u=bob.id), 8)
        # the password is the new one, and a recovery code of the new set resets it again with the data kept
        self.assertEqual(self.login(bob, PASSWORD)[1].status_code, 401)
        r = self.anonymous().post("/api/auth/recover", json={"email": bob.email, "recovery_code": codes[0],
                                                          "new_password": "and one more password"})
        self.assertEqual(r.status_code, 200, r.text)
        notice = [m for m in self.to(bob.email) if "password was changed" in m["Subject"]]
        self.assertEqual(len(notice), 2)  # the reset, then the recovery

    def test_every_session_ends_and_the_person_is_signed_in(self):
        bob = self.user()
        old = self.signed_in(bob)
        c = self.anonymous()
        self.assertEqual(self.reset(self.link(bob), anon=c, accept_wipe=True).status_code, 200)
        self.assertEqual(old.get("/api/auth/me").status_code, 401)
        self.assertEqual(rows(lambda s: s.query(AuthSession).filter(AuthSession.user_id == bob.id).count()), 1)

    def test_a_running_search_stops_the_wipe_and_gives_the_link_back(self):
        bob = self.user()
        token = self.link(bob)
        c = self.anonymous()  # (starting a client marks runs left running as interrupted: add the run after it)
        db = SessionLocal()
        try:
            with helpers.as_account(bob):
                db.add(SearchRun(workspace_id=bob.workspace_id, kind="search", state="running", progress_json="{}"))
                db.commit()
        finally:
            db.close()
        r = self.reset(token, anon=c, accept_wipe=True)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "run_active"))
        with engine.begin() as conn:
            conn.execute(text("UPDATE search_runs SET state = 'done' WHERE workspace_id = :w"), {"w": bob.workspace_id})
        self.assertEqual(self.reset(token, accept_wipe=True).status_code, 200)

    def test_an_admin_who_is_wiped_loses_the_admin_key_and_another_admin_can_share_it_again(self):
        helpers.started()
        boss = helpers.make_user(password=PASSWORD)
        with helpers.client() as first:
            self.assertEqual(first.post(f"/api/admin/users/{boss.id}/role", json={"role": "admin"}).status_code, 200)
        self.addCleanup(self.demote, boss.id)
        with helpers.client(boss.email, PASSWORD) as c:
            self.assertTrue(c.get("/api/admin/users").json()["admin_key_available"])
        r = self.reset(self.link(boss), accept_wipe=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["admin_key_lost"])  # a warning for the page to show
        self.assertEqual(rows(lambda s: s.get(AdminKeyGrant, boss.id)), None)
        self.assertIn("admin_key_lost", [e["action"] for e in self._audit()])
        with helpers.client() as first:
            row = next(u for u in first.get("/api/admin/users").json()["users"] if u["id"] == boss.id)
            self.assertFalse(row["admin_key_shared"])  # "Share the admin key again"
            self.assertEqual(row["role"], "admin")
        c = self.anonymous()
        login = c.post("/api/auth/login", json={"email": boss.email, "password": NEW_PASSWORD})
        c.headers["X-CSRF-Token"] = login.json()["csrf"]
        self.assertFalse(c.get("/api/admin/users").json()["admin_key_available"])  # they can't read details until then
        with helpers.client() as first:
            self.assertEqual(first.post(f"/api/admin/users/{boss.id}/regrant").status_code, 200)
            self.assertTrue(next(u for u in first.get("/api/admin/users").json()["users"]
                                 if u["id"] == boss.id)["admin_key_shared"])
        c.post("/api/auth/reauth", json={"password": NEW_PASSWORD})
        self.assertTrue(c.get("/api/admin/users").json()["admin_key_available"])

    def test_an_admin_with_background_access_keeps_the_key(self):
        boss = helpers.make_user(password=PASSWORD)
        with helpers.client() as first:
            first.post(f"/api/admin/users/{boss.id}/role", json={"role": "admin"})
        self.addCleanup(self.demote, boss.id)
        with helpers.client(boss.email, PASSWORD) as c:
            c.post("/api/account/background", json={"enabled": True})
        r = self.reset(self.link(boss))
        self.assertEqual((r.status_code, r.json()["admin_key_lost"]), (200, False))
        self.assertIsNotNone(rows(lambda s: s.get(AdminKeyGrant, boss.id)))

    def demote(self, user_id: int) -> None:
        with helpers.client() as first:
            first.post(f"/api/admin/users/{user_id}/role", json={"role": "user"})

    def test_a_request_holding_the_old_key_cannot_wrap_it_under_new_codes_after_the_wipe(self):
        bob = self.user()
        _, old_dek = helpers.unlock(bob.email, PASSWORD)
        db = SessionLocal()
        try:
            wiped = users.reset_password_wiping_data(db, bob.id, NEW_PASSWORD, bob.email)
            db.expunge_all()  # (a handler's session may have read the row before the wipe)
            with self.assertRaises(crypto.KeyMismatch):
                users.replace_recovery_codes(db, bob.id, old_dek)
            db.rollback()
            # the codes the reset page showed still open the new key, and nothing wraps the old one
            opened = users.open_recovery_code(db, bob.id, wiped.recovery_codes[0])
            self.assertEqual(opened[1], wiped.dek)
            self.assertEqual(db.query(RecoveryCode).filter(RecoveryCode.user_id == bob.id,
                                                                 RecoveryCode.wrapped_dek.isnot(None)).count(), 8)
            row = db.get(UserKeys, bob.id)
            with crypto.as_user(bob.id, wiped.dek):
                self.assertEqual(len(json.loads(crypto.unseal(bytes(row.pending_codes_enc), users._codes_label(bob.id)))), 8)
        finally:
            db.close()

    def test_new_codes_for_a_signed_in_person_whose_key_was_replaced_answer_signed_out(self):
        bob = self.user()
        c = self.signed_in(bob)
        with mock.patch("backend.users.replace_recovery_codes", side_effect=crypto.KeyMismatch("x")):
            r = c.post("/api/auth/recovery-codes", json={"password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (401, "signed_out"))

    def _audit(self) -> list[dict]:
        with helpers.client() as first:
            return first.get("/api/admin/audit?limit=50").json()["entries"]


# ---------------------------------------------------------------------------
# Two-step sign-in during a reset
# ---------------------------------------------------------------------------

class MfaResetTest(ResetCase):
    def test_a_code_is_required_with_or_without_background_access(self):
        for background in (False, True):
            bob, secret = self.user(background=background, mfa=True, pin="Mfa marker")
            token = self.link(bob)
            self.assertEqual(self.anonymous().post("/api/auth/reset/info", json={"token": token}).json()["mfa_required"],
                             True)
            r = self.reset(token, accept_wipe=True)
            self.assertEqual((r.status_code, r.json()["code"], r.json()["field"]), (422, "second_factor_required", "code"))
            self.assertEqual(self.login(bob, PASSWORD)[1].status_code, 200)  # nothing changed ... (a pending session)
            r = self.reset(token, accept_wipe=True, code=totp.code_at(secret, self.step()), recovery_code="x")
            self.assertEqual(r.status_code, 422)  # one or the other, not both
            r = self.reset(token, accept_wipe=True, code="000000")
            self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))
            self.assertEqual(helpers.wrong_tries(bob.id), 1)
            self.assertEqual(self.anonymous().post("/api/auth/reset/info", json={"token": token}).status_code, 200)

    def test_the_right_code_resets_and_cannot_be_replayed(self):
        bob, secret = self.user(background=True, mfa=True, pin="Mfa marker")
        token = self.link(bob)
        code = totp.code_at(secret, self.step())
        c = self.anonymous()
        r = self.reset(token, anon=c, code=code)
        self.assertEqual(r.status_code, 200, r.text)
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertEqual(self.pin_labels(c), ["Mfa marker"])
        self.assertEqual(helpers.wrong_tries(bob.id), 0)
        again = self.link(bob)
        r = self.reset(again, code=code, new_password="a different new password")  # the same step: replay
        self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))
        self.advance()
        self.assertEqual(self.reset(again, code=totp.code_at(secret, self.step()),
                                    new_password="a different new password").status_code, 200)

    def test_a_recovery_code_stands_in_for_the_app_and_is_used_up(self):
        bob, _secret = self.user(background=True, mfa=True)
        # the codes shown at sign-up are still waiting to be confirmed: read them with a session
        db = SessionLocal()
        try:
            with helpers.as_account(bob):
                codes = users.pending_recovery_codes(db, bob.id)
        finally:
            db.close()
        token = self.link(bob)
        r = self.reset(token, recovery_code=codes[0])
        self.assertEqual(r.status_code, 200, r.text)
        # (all eight are replaced by the reset, the one used up with the rest)
        self.assertEqual(rows(lambda s: users.recovery_codes_left(s, bob.id)), 8)
        again = self.link(bob)
        r = self.reset(again, recovery_code=codes[0], new_password="a different new password")
        self.assertEqual((r.status_code, r.json()["code"]), (422, "wrong_code"))  # gone

    def test_five_wrong_codes_from_one_place_make_that_place_wait(self):
        bob, secret = self.user(background=True, mfa=True)
        token = self.link(bob)
        with mock.patch.object(helpers.ratelimit, "CODE_TRIES", 50):  # (the link's own limit is tested below)
            statuses = [self.reset(token, code="000000").status_code for _ in range(6)]
            self.assertEqual(statuses[:5], [422] * 5)
            r = self.reset(token, code=totp.code_at(secret, self.step()))
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        self.assertIn("Retry-After", r.headers)

    def test_wrong_codes_from_an_outsider_do_not_keep_the_owner_out(self):
        """The owner has the link (the mailbox) and the app; a stranger who guesses codes against the account waits
        alone, from their own address."""
        bob, secret = self.user(background=True, mfa=True)
        token = self.link(bob)
        stranger = self.anonymous(client=("203.0.113.9", 4000))
        with mock.patch.object(helpers.ratelimit, "CODE_TRIES", 50):
            statuses = [self.reset(token, anon=stranger, code="000000").status_code for _ in range(6)]
        self.assertEqual(statuses, [422] * 5 + [429])
        owner = self.anonymous(client=("198.51.100.7", 5000))
        r = self.reset(token, anon=owner, code=totp.code_at(secret, self.step()))
        self.assertEqual(r.status_code, 200, r.text)

    def test_five_wrong_codes_on_one_link_use_it_up_whoever_sends_them(self):
        bob, secret = self.user(background=True, mfa=True)
        token = self.link(bob)
        statuses = [self.reset(token, anon=self.anonymous(client=(f"2001:db8::{n:x}", 4000)),
                               code="000000").status_code for n in range(1, 8)]
        self.assertEqual(statuses, [422] * 4 + [410] * 3)
        r = self.reset(token, code=totp.code_at(secret, self.step()))  # not even the right one now
        self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        self.assertEqual(self.login(bob, PASSWORD)[1].status_code, 200)  # nothing changed
        fresh = self.link(bob)  # a new link is a new set of 5
        self.advance()
        self.assertEqual(self.reset(fresh, code=totp.code_at(secret, self.step())).status_code, 200)

    def test_codes_guessed_from_many_addresses_across_links_run_out(self):
        """The reset link isn't used up by a wrong code, so a guesser with many addresses (an IPv6 /64) and a mailbox
        gets 5 tries per link; the codes' own ceiling over all addresses stops them after about 10 an hour."""
        bob, secret = self.user(background=True, mfa=True)
        sent = []
        for n in range(1, 41):
            if n % 4 == 1:  # a new link every 4 tries
                helpers.ratelimit.FORGOT_ADDRESS.reset()
                helpers.ratelimit.RESET_ACCOUNT.reset()
                token = self.link(bob)
            anon = self.anonymous(client=(f"2001:db8::{n:x}", 4000))
            helpers.ratelimit.AUTH.reset()
            sent.append(self.reset(token, anon=anon, code="000000").status_code)
        self.assertEqual(sent.count(422), 10)
        self.assertEqual(set(sent[10:]), {429})
        helpers.ratelimit.AUTH.reset()
        r = self.reset(token, anon=self.anonymous(client=("2001:db8::ffff", 4000)),
                       code=totp.code_at(secret, self.step()))
        self.assertEqual(r.status_code, 429)

    def test_a_browser_the_account_signed_in_from_is_not_held_up_by_other_places_wrong_codes(self):
        bob, secret = self.user(background=True, mfa=True)
        for n in range(1, 11):  # ten wrong codes from other places: the ceiling is reached
            helpers.ratelimit.code_guesses.begin(bob.id, f"2001:db8::{n:x}")
        token = self.link(bob)
        r = self.reset(token, anon=self.anonymous(client=("2001:db8::ffff", 4000)), code=totp.code_at(secret, self.step()))
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        self.assertIn("several places", r.json()["detail"])
        owner = self.anonymous(client=("2001:db8::fffe", 4000))
        response = Response()
        lockout.remember_device(response, {"type": "http", "scheme": "http", "headers": [], "server": ("testserver", 80)},
                                bob.id)
        owner.cookies.set("jh_device", response.headers["set-cookie"].split("jh_device=")[1].split(";")[0])
        r = self.reset(token, anon=owner, code=totp.code_at(secret, self.step()))
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_wiping_reset_also_needs_the_code_before_anything_is_deleted(self):
        bob, secret = self.user(mfa=True, pin="Mfa marker")
        token = self.link(bob)
        self.assertEqual(self.reset(token, accept_wipe=True).status_code, 422)
        self.assertEqual(self.reset(token, accept_wipe=True, code="123456").status_code, 422)
        self.assertEqual(rows(lambda s: s.query(Workspace).filter(Workspace.id == bob.workspace_id).count()), 1)
        r = self.reset(token, accept_wipe=True, code=totp.code_at(secret, self.step()))
        self.assertEqual((r.status_code, r.json()["wiped"]), (200, True))
        self.assertEqual(self.login(bob)[1].status_code, 200)
        self.assertTrue(rows(lambda s: s.get(User, bob.id)).mfa_enabled)  # the secret is under the server key: kept


# ---------------------------------------------------------------------------
# Links that must stop working
# ---------------------------------------------------------------------------

class StaleLinkTest(ResetCase):
    def info(self, token):
        return self.anonymous().post("/api/auth/reset/info", json={"token": token})

    def test_a_link_to_an_old_address_stops_working_when_the_address_changes(self):
        bob = self.user(background=True)
        token = self.link(bob)  # sent to the old address
        c = self.signed_in(bob)
        new = f"moved-{bob.id}@example.invalid"
        self.assertEqual(c.post("/api/auth/email/change", json={"new_email": new, "password": PASSWORD}).status_code, 200)
        confirm = token_in(self.last_to(new), "verify-email.html")
        self.assertEqual(self.anonymous().post("/api/auth/verify-email", json={"token": confirm}).status_code, 200)
        for r in (self.info(token), self.reset(token)):
            self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        helpers.ratelimit.reset_all()
        r = self.anonymous().post("/api/auth/login", json={"email": new, "password": PASSWORD})
        self.assertEqual(r.status_code, 200)  # the password is the one it was
        self.assertEqual(rows(lambda s: s.query(AuthToken).filter(AuthToken.kind == "reset", AuthToken.user_id == bob.id,
                                                                  AuthToken.used_at.is_(None)).count()), 0)

    def test_a_link_whose_address_is_no_longer_the_accounts_is_refused_even_if_it_was_not_deleted(self):
        bob = self.user(background=True)
        token = self.link(bob)
        with engine.begin() as conn:  # (the address changed some way that left the link behind)
            conn.execute(text("UPDATE users SET email_hash = :h WHERE id = :i"),
                         {"h": users.server_keys().email_hash("someone-else@example.invalid"), "i": bob.id})
        for r in (self.info(token), self.reset(token)):
            self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        self.assertEqual(self.flush()[-1]["To"], bob.email)  # (and no notice went anywhere: nothing was reset)
        self.assertEqual(len(self.flush()), 1)

    def test_changing_the_password_ends_unused_reset_links(self):
        bob = self.user(background=True)
        token = self.link(bob)
        c = self.signed_in(bob)
        r = c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "another good password"})
        self.assertEqual(r.status_code, 200, r.text)
        for r in (self.info(token), self.reset(token, accept_wipe=True)):
            self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        self.assertEqual(self.login(bob, "another good password")[1].status_code, 200)

    def test_resetting_with_a_recovery_code_ends_unused_reset_links(self):
        bob = self.user()
        token = self.link(bob)
        c = self.signed_in(bob)
        code = helpers.show_codes(c).json()["codes"][0]
        r = self.anonymous().post("/api/auth/recover", json={"email": bob.email, "recovery_code": code,
                                                              "new_password": "recovered password"})
        self.assertEqual(r.status_code, 200, r.text)
        for r in (self.info(token), self.reset(token, accept_wipe=True)):
            self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        self.assertEqual(self.login(bob, "recovered password")[1].status_code, 200)

    def other_links(self, acct) -> None:
        """An address-change link and a second reset link for ``acct`` that a reset must not leave usable."""
        c = self.signed_in(acct)
        self.assertEqual(c.post("/api/auth/email/change", json={"new_email": f"later-{acct.id}@example.invalid",
                                                                        "password": PASSWORD}).status_code, 200)
        db = SessionLocal()
        try:
            tokens.create(db, "reset", ttl=timedelta(hours=1), user_id=acct.id, payload={"email": acct.email})
            db.commit()
        finally:
            db.close()

    def unused(self, acct) -> int:
        return rows(lambda s: s.query(AuthToken).filter(AuthToken.user_id == acct.id,
                                                        AuthToken.kind.in_(["reset", "email_change"]),
                                                        AuthToken.used_at.is_(None)).count())

    def test_a_reset_that_keeps_the_data_ends_the_other_reset_and_address_links(self):
        bob = self.user(background=True)
        token = self.link(bob)
        self.other_links(bob)
        self.assertEqual(self.unused(bob), 3)
        self.assertEqual(self.reset(token).status_code, 200)
        self.assertEqual(self.unused(bob), 0)

    def test_a_reset_that_deletes_the_data_ends_the_other_reset_and_address_links(self):
        bob = self.user()
        token = self.link(bob)
        self.other_links(bob)
        self.assertEqual(self.reset(token, accept_wipe=True).status_code, 200)
        self.assertEqual(self.unused(bob), 0)


# ---------------------------------------------------------------------------
# What a reset does besides changing the password
# ---------------------------------------------------------------------------

class WriterTest(ResetCase):
    """Someone who can write the database (not the key file) can't point a reset link at themselves (Phase L review;
    accounts design §2.5)."""

    def info(self, token):
        return self.anonymous().post("/api/auth/reset/info", json={"token": token})

    def refused(self, token, bob):
        for r in (self.info(token), self.reset(token)):
            self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        self.assertEqual(self.login(bob, PASSWORD)[1].status_code, 200)  # the password is the one it was

    def test_a_row_made_without_the_key_file_is_no_link(self):
        bob = self.user(background=True)
        db = SessionLocal()
        try:  # a writer knows everything a link needs except the server key's signature
            token, _row = tokens.create(db, "reset", ttl=timedelta(hours=1), user_id=bob.id,
                                        payload={"email": bob.email})
            db.commit()
        finally:
            db.close()
        self.refused(token, bob)

    def test_a_link_of_another_account_moved_to_this_one_is_no_link(self):
        bob, mallory = self.user(background=True), self.user(background=True)
        token = self.link(mallory)  # mallory's own, real link
        with engine.begin() as conn:
            conn.execute(text("UPDATE auth_tokens SET user_id = :b WHERE token_hash = :h"),
                         {"b": bob.id, "h": tokens.hash_token(token)})
        self.refused(token, bob)

    def test_an_email_hash_rewritten_to_another_address_does_not_send_a_working_link_to_it(self):
        bob = self.user(background=True)
        attacker = f"attacker-{bob.id}@example.invalid"
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET email_hash = :h WHERE id = :i"),
                         {"h": users.server_keys().email_hash(attacker), "i": bob.id})
        self.assertEqual(self.ask(attacker).status_code, 202)
        token = token_in(self.last_to(attacker), "reset-password.html")  # a genuine, signed link, to the wrong person
        for r in (self.info(token), self.reset(token)):
            self.assertEqual((r.status_code, r.json()["code"]), (410, "link_invalid"))
        with engine.begin() as conn:  # put it back: the account is still bob's, with the password it had
            conn.execute(text("UPDATE users SET email_hash = :h WHERE id = :i"),
                         {"h": users.server_keys().email_hash(bob.email), "i": bob.id})
        self.assertEqual(self.login(bob, PASSWORD)[1].status_code, 200)
        self.assertEqual(self.flush()[-1]["To"], attacker)  # (no "password changed" notice: nothing was reset)
        self.assertEqual(len([m for m in self.flush() if "changed" in (m["Subject"] or "")]), 0)

    def test_a_link_to_the_accounts_real_address_in_another_spelling_still_works(self):
        bob = self.user(background=True)
        token = self.link(bob)
        r = self.reset(token)
        self.assertEqual((r.status_code, r.json()["kept_data"]), (200, True))
        notice = [m for m in self.flush() if m["To"] == bob.email and "changed" in (m["Subject"] or "").lower()]
        self.assertEqual(len(notice), 1)  # the notice goes to the address the account holds


class AfterResetTest(ResetCase):
    def test_signing_in_by_reset_starts_a_waiting_encryption_migration(self):
        for background in (True, False):
            bob = self.user(background=background)
            token = self.link(bob)
            with mock.patch("backend.password_reset._after_sign_in") as after:
                r = self.reset(token, accept_wipe=True)
            self.assertEqual(r.status_code, 200, r.text)
            after.assert_called_once()
            user_id, dek = after.call_args.args
            self.assertEqual((user_id, isinstance(dek, bytes) and len(dek)), (bob.id, 32))

    def test_a_running_search_refuses_a_wipe_before_a_code_is_spent(self):
        bob, secret = self.user(mfa=True)
        token = self.link(bob)
        c = self.anonymous()
        db = SessionLocal()
        try:
            with helpers.as_account(bob):
                db.add(SearchRun(workspace_id=bob.workspace_id, kind="search", state="running", progress_json="{}"))
                db.commit()
        finally:
            db.close()
        code = totp.code_at(secret, self.step())
        r = self.reset(token, anon=c, accept_wipe=True, code=code)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "run_active"))
        self.assertEqual(helpers.wrong_tries(bob.id), 0)
        with engine.begin() as conn:
            conn.execute(text("UPDATE search_runs SET state = 'done' WHERE workspace_id = :w"), {"w": bob.workspace_id})
        r = self.reset(token, accept_wipe=True, code=code)  # the same code: it was not used up
        self.assertEqual(r.status_code, 200, r.text)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

class LogTest(ResetCase):
    def test_no_address_and_no_token_in_any_log_line(self):
        bob = self.user(background=True)
        ghost = "ghost-in-the-logs@example.invalid"
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("test").info("(marker so assertLogs always has a line)")
            self.assertEqual(self.ask(bob.email).status_code, 202)
            self.assertEqual(self.ask(ghost).status_code, 202)
            token = token_in(self.last_to(bob.email), "reset-password.html")
            self.assertEqual(self.reset(token).status_code, 200)
            self.assertEqual(self.reset(token).status_code, 410)
            self.flush()
        everything = "\n".join(logs.output)
        for secret in (bob.email, bob.email.split("@")[0], ghost, "ghost-in-the-logs", token, PASSWORD, NEW_PASSWORD):
            self.assertNotIn(secret, everything)
        self.assertIn("Email sent", everything)  # but the log does say that mail went


if __name__ == "__main__":
    unittest.main()
