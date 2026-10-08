"""The admin's email settings, the test email and the mail thread (accounts design §9, §16; Phase H). Header-injection
attempts, the SMTP password (spaces, quotes, base64 in .env), what an ordinary user may not touch, and that sending
happens on one background thread. SMTP is captured, never used.
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import unittest
from datetime import timedelta
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)
from tests.mail_helpers import MailTestCase, html_of, text_of  # noqa: E402

from sqlalchemy import text  # noqa: E402

from backend import config, email_settings, mail_queue, mail_route, mailer  # noqa: E402
from backend.db import SessionLocal, engine, utcnow  # noqa: E402

GOOD = {"host": "smtp.example.org", "port": "465", "security": "ssl", "user": "me@example.org",
        "password": "hunter2-hunter2", "from_address": "jobs@example.org", "public_base_url": "https://jobs.example.org"}
VARS = ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USER", "SMTP_PASSWORD", "SMTP_PASSWORD_B64", "SMTP_FROM",
        "PUBLIC_BASE_URL")


class SettingsCase(MailTestCase):
    """Starts from no email settings, with the .env file put back at the end."""

    email_on = False

    def setUp(self):
        super().setUp()
        path = config.ENV_PATH
        before = path.read_text(encoding="utf-8") if path.exists() else None

        def restore():
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(before, encoding="utf-8")

        self.addCleanup(restore)
        self.clear_hold()
        self.addCleanup(self.clear_hold)
        self.admin = self.signed_in()
        email_settings._tests.clear()

    @staticmethod
    def clear_hold():
        """The hold after a change of route (mail_route) is a row in the shared test database: no test leaves one."""
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM app_settings WHERE key = 'mail_route_changed_at'"))

    def put(self, client=None, **fields):
        return (client or self.admin).put("/api/settings/email", json=fields)

    def env_file(self) -> str:
        return config.ENV_PATH.read_text(encoding="utf-8") if config.ENV_PATH.exists() else ""

    def snapshot(self) -> tuple:
        return self.env_file(), tuple(os.environ.get(v) for v in VARS)


class AccessTest(SettingsCase):
    def test_only_admins_can_read_or_change_them(self):
        bob = helpers.make_user()
        c = self.signed_in(bob)
        for call in (lambda: c.get("/api/settings/email"), lambda: c.put("/api/settings/email", json=GOOD),
                     lambda: c.post("/api/settings/email/test"), lambda: c.get("/api/settings/email/test")):
            self.assertEqual(call().status_code, 403)
        anon = self.anonymous()
        for call in (lambda: anon.get("/api/settings/email"), lambda: anon.put("/api/settings/email", json=GOOD),
                     lambda: anon.post("/api/settings/email/test")):
            self.assertEqual(call().status_code, 401)
        self.assertEqual(self.env_file(), "")

    def test_a_change_needs_the_csrf_token(self):
        c = helpers.client()
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        c.headers.pop("X-CSRF-Token")
        self.assertEqual(c.put("/api/settings/email", json=GOOD).status_code, 403)


class SaveTest(SettingsCase):
    def test_it_starts_off_and_says_what_is_missing(self):
        r = self.admin.get("/api/settings/email").json()
        self.assertFalse(r["enabled"])
        self.assertEqual(r["missing"], ["smtp", "public_url"])
        self.assertIn("Email is off", r["text"])
        self.assertEqual(r["password"], {"set": False})
        self.assertEqual(r["default_ports"], {"starttls": "587", "ssl": "465", "none": "25"})
        self.assertEqual(r["security_modes"], ["starttls", "ssl", "none"])

    def test_an_unset_encryption_shows_the_mode_that_is_used(self):
        r = self.admin.get("/api/settings/email").json()
        self.assertEqual(r["security"], "starttls")
        self.assertIn(r["security"], r["security_modes"])
        self.assertEqual(r["default_ports"][r["security"]], "587")
        self.assertEqual(self.put(security="ssl").json()["security"], "ssl")

    def test_saving_everything_turns_email_on_at_once_and_writes_the_env_file(self):
        r = self.put(**GOOD)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["enabled"])
        self.assertEqual((body["host"], body["port"], body["security"], body["user"], body["from_address"],
                          body["public_base_url"]), (GOOD["host"], "465", "ssl", GOOD["user"], "jobs@example.org",
                                                     "https://jobs.example.org"))
        self.assertEqual(body["password"], {"set": True})
        self.assertNotIn(GOOD["password"], r.text)
        cfg = mailer.get_config()
        self.assertEqual((cfg.host, cfg.port, cfg.security, cfg.user, cfg.password, cfg.from_name, cfg.from_address),
                         ("smtp.example.org", 465, "ssl", "me@example.org", GOOD["password"], "JobHuntPA",
                          "jobs@example.org"))
        self.assertEqual(mailer.public_base_url(), "https://jobs.example.org")
        text = self.env_file()
        self.assertIn("SMTP_HOST=smtp.example.org", text)
        self.assertIn("SMTP_FROM=JobHuntPA <jobs@example.org>", text)
        self.assertNotIn(GOOD["password"], text)  # only its base64 is on disk

    def test_a_partial_save_leaves_the_rest_and_empty_clears(self):
        self.put(**GOOD)
        r = self.put(port="587", security="starttls").json()
        self.assertEqual((r["host"], r["port"], r["security"], r["password"]["set"]), (GOOD["host"], "587", "starttls", True))
        self.assertEqual(self.put().json()["host"], GOOD["host"])  # nothing sent: nothing changed
        self.assertEqual(self.admin.put("/api/settings/email", json={"host": None, "port": None}).json()["port"], "587")
        r = self.put(public_base_url="").json()
        self.assertEqual((r["enabled"], r["missing"], r["public_base_url"]), (False, ["public_url"], ""))
        r = self.put(host="", from_address="", password="", user="").json()
        self.assertEqual((r["missing"], r["password"], r["user"], r["from_address"]), (["smtp", "public_url"], {"set": False}, "", ""))
        self.assertIsNone(mailer.get_config())

    def test_an_unknown_field_is_refused(self):
        self.assertEqual(self.put(host="smtp.example.org", sneaky="x").status_code, 422)
        self.assertEqual(self.put(smtp_password_b64="eA").status_code, 422)  # the env names can't be written directly
        self.assertEqual(self.env_file(), "")

    def test_the_public_address_may_end_with_a_slash(self):
        self.assertEqual(self.put(public_base_url="https://jobs.example.org/").json()["public_base_url"],
                         "https://jobs.example.org")

    def test_every_change_is_audited_by_name_and_never_by_value(self):
        self.put(**GOOD)
        entries = self.admin.get("/api/admin/audit?limit=5").json()["entries"]
        entry = next(e for e in entries if e["action"] == "server_settings_changed")
        self.assertEqual(set(entry["detail"]["fields"]), {"email_host", "email_port", "email_security", "email_user",
                                                          "email_password", "email_from", "public_url"})
        self.assertNotIn("example.org", str(entry["detail"]))
        self.assertNotIn(GOOD["password"], str(entry["detail"]))


class InjectionTest(SettingsCase):
    """Nothing with a line break (or anything else that could start a new .env line, a header or a second address)
    gets in, whichever field it comes through, and a refused save changes nothing at all."""

    BAD = {
        "host": ["smtp.example.org\nSMTP_FROM=x", "smtp.example.org\r\n", "smtp example.org", "-bad.example.org",
                 "smtp.example.org:25", "a..b", "x" * 300, "http://smtp.example.org", "smtp.example.org#"],
        "port": ["587\n", "0", "65536", "-1", "abc", "5 87", "99999999"],
        "security": ["tls", "plain", "starttls\nssl", "ssl\r\nBcc: evil@example.com"],
        "user": ["me@example.org\nSMTP_HOST=evil.example", "DOMAIN\\user", "me\r\nBcc: evil@example.com", "a b", "x" * 300, "me'",
                 'me"', "me#x"],
        "from_address": ["jobs@example.org\r\nBcc: evil@example.com", "jobs@example.org\nSubject: x",
                         "Evil <jobs@example.org>", "a@example.org, b@example.org", "jobs@example", "no address",
                         "<jobs@example.org>", "jobs@example.org>", "jo bs@example.org", "x" * 300 + "@example.org"],
        "public_base_url": ["https://jobs.example.org\nSMTP_HOST=evil", "https://jobs.example.org?x=1",
                            "https://jobs.example.org#top", "https://user:pw@jobs.example.org", "ftp://jobs.example.org",
                            "javascript:alert(1)", "jobs.example.org", "https://", "http://jobs.example.org",
                            "https://jobs.example.org/a b", "https://jobs.example.org\r\n"],
    }

    def test_every_bad_value_is_a_422_that_names_its_field_and_changes_nothing(self):
        self.put(host="smtp.example.org", from_address="jobs@example.org")  # something already saved
        before = self.snapshot()
        for field, values in self.BAD.items():
            for value in values:
                if field == "public_base_url" and value == "http://jobs.example.org":
                    continue  # (http is allowed for localhost only: checked next)
                r = self.put(**{field: value})
                self.assertEqual((r.status_code, r.json().get("field"), r.json().get("code")), (422, field, "invalid"),
                                 (field, value))
                if len(value.strip()) > 16:
                    self.assertNotIn(value.strip(), r.json()["detail"])  # a value is never echoed back
                self.assertEqual(self.snapshot(), before, (field, value))
        self.assertEqual(self.put(public_base_url="http://jobs.example.org").status_code, 422)
        self.assertEqual(self.put(public_base_url="http://localhost:8040").status_code, 200)

    def test_a_save_needs_a_recent_sign_in(self):
        self.put(host="smtp.example.org", from_address="jobs@example.org")  # (recent: the admin just signed in)
        before = self.snapshot()
        with engine.begin() as conn:
            conn.execute(text("UPDATE sessions SET reauth_at = :t"), {"t": utcnow() - timedelta(minutes=30)})
        for fields in ({"public_base_url": "https://attacker.example"}, {"host": "evil.example.com", "password": "mine-mine"},
                       {"password": ""}):
            r = self.put(**fields)
            self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"), fields)
            self.assertEqual(self.snapshot(), before, fields)
        self.assertEqual(self.admin.get("/api/settings/email").status_code, 200)  # reading needs no password
        self.assertEqual(self.put().status_code, 200)  # nothing to change: nothing to confirm
        self.assertEqual(self.admin.post("/api/auth/reauth", json={"password": helpers.ADMIN_PASSWORD}).status_code, 200)
        self.assertEqual(self.put(public_base_url="https://jobs.example.org").status_code, 200)

    def test_one_bad_field_stops_the_whole_save(self):
        before = self.snapshot()
        r = self.put(host="smtp.example.org", port="99999", from_address="jobs@example.org")
        self.assertEqual((r.status_code, r.json()["field"]), (422, "port"))
        self.assertEqual(self.snapshot(), before)

    def test_a_password_can_not_become_a_second_env_line(self):
        r = self.put(password="abc\nSMTP_HOST=evil.example")  # whitespace is removed, then it is just a password
        self.assertEqual(r.status_code, 200)
        text = self.env_file()
        self.assertNotIn("evil.example", text.replace("SMTP_PASSWORD_B64=", ""))
        self.assertEqual(mailer.get_config(), None)  # (no host)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("SMTP_HOST")]), 0)
        self.assertEqual(os.environ.get("SMTP_HOST") or "", "")
        self.assertEqual(mailer.decode_password(os.environ["SMTP_PASSWORD_B64"]), "abcSMTP_HOST=evil.example")

    def test_the_mailer_refuses_a_line_break_in_any_header_whatever_the_settings_say(self):
        with self.assertRaises(ValueError):
            mailer.build_message("a@example.org\r\nBcc: evil@example.com", "s", "t", "<p>h</p>",
                                 sender=("JobHuntPA", "jobs@example.org"))
        with self.assertRaises(ValueError):
            mailer.build_message("a@example.org", "s\nBcc: evil@example.com", "t", "<p>h</p>",
                                 sender=("JobHuntPA", "jobs@example.org"))

    def test_plain_text_mail_is_only_for_a_server_on_this_computer(self):
        r = self.put(host="smtp.example.org", security="none")
        self.assertEqual((r.status_code, r.json()["field"]), (422, "security"))
        self.assertEqual(self.put(host="localhost", security="none", port="25").status_code, 200)
        self.assertEqual(self.put(host="127.0.0.1").status_code, 200)
        r = self.put(host="mail.example.org")  # leaving localhost while the connection is still unencrypted
        self.assertEqual((r.status_code, r.json()["field"]), (422, "security"))
        self.assertEqual(self.put(host="mail.example.org", security="starttls").status_code, 200)

    def test_the_mail_server_cannot_be_changed_to_collect_the_saved_password(self):
        self.put(**GOOD)
        r = self.put(host="evil.example.com")
        self.assertEqual((r.status_code, r.json()["field"]), (422, "password"))
        self.assertEqual(os.environ["SMTP_HOST"], GOOD["host"])
        self.assertEqual(self.put(host="smtp.example.net", password="another-password").status_code, 200)
        self.assertEqual(self.put(host="SMTP.EXAMPLE.NET").status_code, 200)  # the same server: no new password needed
        self.assertEqual(self.put(host="").status_code, 200)  # switching mail off isn't a redirect


class ValidatorTest(unittest.TestCase):
    """The rules the settings form and .env share (backend.app_settings.ENV_RULES use these)."""

    def test_plain_http_is_for_this_computer_only(self):
        for good in ("https://jobs.example.org", "https://192.168.1.5:8040", "http://localhost:8040",
                     "http://127.0.0.1:8040", "http://127.1.2.3", "http://[::1]:8040", "http://LOCALHOST"):
            self.assertTrue(mailer.valid_public_base_url(good), good)
        for bad in ("http://192.168.1.5", "http://jobs.example.org", "http://10.0.0.1:8040", "http://0.0.0.0",
                    "http://localhost.example.org", "http://127.0.0.1.example.org"):
            self.assertFalse(mailer.valid_public_base_url(bad), bad)
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "http://192.168.1.5:8040"}):  # a hand-edited .env
            self.assertIsNone(mailer.public_base_url())

    def test_a_user_name_has_no_backslash(self):
        for bad in ("a\\b", "DOMAIN\\user", "\\"):
            self.assertFalse(mailer.valid_user(bad), bad)
        for good in ("me@example.org", "mailer", "a.b_c%d+e-f"):
            self.assertTrue(mailer.valid_user(good), good)


class PasswordTest(SettingsCase):
    def test_spaces_gmail_shows_are_removed(self):
        self.put(host="smtp.gmail.com", from_address="me@gmail.com", password="abcd efgh ijkl mnop")
        self.assertEqual(os.environ["SMTP_HOST"], "smtp.gmail.com")
        self.assertEqual(mailer.get_config().password, "abcdefghijklmnop")
        self.put(password=" abcd\tefgh  ijkl mnop ") if False else None

    def test_any_printable_ascii_survives_the_env_file(self):
        for password in ("p\"a'ss#w=rd!", "$ecret`with\\slashes{}", "a" * 256):
            self.put(host="smtp.example.org", from_address="jobs@example.org", password=password)
            self.assertEqual(mailer.get_config().password, password)
            # what a restart reads from the file is the same password
            seen = {}
            for line in self.env_file().splitlines():
                key, _, val = line.partition("=")
                seen[key.strip()] = val.strip().strip("'\"")
            self.assertEqual(mailer.decode_password(seen["SMTP_PASSWORD_B64"]), password)
            self.assertEqual(seen.get("SMTP_PASSWORD", ""), "")

    def test_what_smtplib_could_not_send_is_refused(self):
        for password in ("pässword", "密码密码密码", "x" * 257, "  "):
            r = self.put(password=password)
            self.assertEqual((r.status_code, r.json()["field"]), (422, "password"), password)
        self.assertEqual(self.env_file(), "")

    def test_a_hand_written_password_keeps_working_until_one_is_saved(self):
        with mock.patch.dict(os.environ, {"SMTP_HOST": "smtp.example.org", "SMTP_FROM": "jobs@example.org",
                                          "SMTP_PASSWORD": "old-hand-written", "SMTP_PASSWORD_B64": ""}):
            self.assertEqual(mailer.get_config().password, "old-hand-written")
            self.assertEqual(self.admin.get("/api/settings/email").json()["password"], {"set": True})
            self.put(password="new-from-the-form")
            self.assertEqual(mailer.get_config().password, "new-from-the-form")
            self.assertEqual(os.environ.get("SMTP_PASSWORD", ""), "")
            self.put(password="")
            self.assertEqual(mailer.get_config().password, "")
            self.assertEqual(self.admin.get("/api/settings/email").json()["password"], {"set": False})

    def test_the_b64_value_wins_and_a_bad_one_is_ignored(self):
        env = {"SMTP_HOST": "smtp.example.org", "SMTP_FROM": "jobs@example.org", "SMTP_PASSWORD": "plain-one"}
        encoded = base64.urlsafe_b64encode(b"b64 one".replace(b" ", b"-")).rstrip(b"=").decode()
        with mock.patch.dict(os.environ, {**env, "SMTP_PASSWORD_B64": encoded}):
            self.assertEqual(mailer.get_config().password, "b64-one")
        for bad in ("!!!", "====", "w5zDvA", "A" * 400):  # not base64url, or not printable ASCII once decoded
            with mock.patch.dict(os.environ, {**env, "SMTP_PASSWORD_B64": bad}):
                self.assertEqual(mailer.get_config().password, "plain-one", bad)
        self.assertFalse(mailer.valid_password_b64("not base64!"))
        self.assertTrue(mailer.valid_password_b64(mailer.encode_password("ok-password")))

    def test_the_password_is_never_in_a_response_a_repr_or_a_log(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("test").info("marker")
            r = self.put(**GOOD)
            g = self.admin.get("/api/settings/email")
            self.assertNotIn(GOOD["password"], r.text + g.text)
            self.assertNotIn(GOOD["password"], repr(mailer.get_config()))
            self.assertNotIn(base64.urlsafe_b64encode(GOOD["password"].encode()).decode().rstrip("="),
                             r.text + g.text)
        self.assertNotIn(GOOD["password"], "\n".join(logs.output))


class TestEmailTest(SettingsCase):
    def configured(self):
        self.put(**{**GOOD, "host": "smtp.example.org"})

    def test_it_goes_to_the_admins_own_address_in_both_parts(self):
        self.configured()
        r = self.admin.post("/api/settings/email/test")
        self.assertEqual((r.status_code, r.json()), (202, {"ok": True, "status": "sending"}))
        msg = self.last_to(helpers.ADMIN_EMAIL)
        self.assertEqual(msg["Subject"], "JobHuntPA test email")
        self.assertEqual(msg["From"], "JobHuntPA <jobs@example.org>")
        for part in (text_of(msg), html_of(msg)):
            self.assertIn("test message", part)
        self.assertNotIn(helpers.ADMIN_EMAIL, text_of(msg) + html_of(msg))
        self.assertEqual(self.admin.get("/api/settings/email/test").json(), {"status": "sent"})

    def test_a_failure_says_what_went_wrong_without_an_address(self):
        self.configured()
        failed = mailer.SendResult("failed", "SMTPAuthenticationError")
        with mock.patch.object(mailer, "send", return_value=failed):
            self.assertEqual(self.admin.post("/api/settings/email/test").status_code, 202)
            self.assertTrue(mail_queue.drain(10))
        self.assertEqual(self.admin.get("/api/settings/email/test").json(),
                         {"status": "failed", "error": "SMTPAuthenticationError"})

    def test_the_request_does_not_wait_for_the_mail_server(self):
        self.configured()
        gate = threading.Event()
        real = mailer.send
        with mock.patch.object(mailer, "send", lambda *a, **k: (gate.wait(10), real(*a, **k))[1]):
            self.assertEqual(self.admin.post("/api/settings/email/test").status_code, 202)
            self.assertEqual(self.admin.get("/api/settings/email/test").json(), {"status": "sending"})
            gate.set()
            self.assertTrue(mail_queue.drain(10))
        self.assertEqual(self.admin.get("/api/settings/email/test").json(), {"status": "sent"})

    def test_it_says_so_while_email_is_off(self):
        self.assertEqual(self.admin.get("/api/settings/email/test").json(), {"status": "none"})
        r = self.admin.post("/api/settings/email/test")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "email_not_set_up"))
        self.assertIn("Email is off", r.json()["detail"])
        self.assertEqual(self.flush(), [])

    def test_it_is_limited(self):
        self.configured()
        codes = [self.admin.post("/api/settings/email/test").status_code for _ in range(6)]
        self.assertEqual(codes, [202] * 5 + [429])

    def test_each_admin_sees_only_their_own_result(self):
        self.configured()
        self.admin.post("/api/settings/email/test")
        self.assertTrue(mail_queue.drain(10))
        other = helpers.make_user()
        with helpers.client() as first:
            first.post(f"/api/admin/users/{other.id}/role", json={"role": "admin"})
        self.addCleanup(self.demote, other.id)
        c = self.signed_in(other)
        self.assertEqual(c.get("/api/settings/email/test").json(), {"status": "none"})

    def demote(self, user_id):
        with helpers.client() as first:
            first.post(f"/api/admin/users/{user_id}/role", json={"role": "user"})


class MailThreadTest(MailTestCase):
    def test_everything_runs_one_at_a_time_on_one_thread_in_order(self):
        seen, gate = [], threading.Event()

        def job(i):
            if i == 0:
                gate.wait(5)
            seen.append((i, threading.current_thread().name))

        futures = [mail_queue.submit(job, i) for i in range(5)]
        self.assertTrue(all(futures))
        self.assertEqual(seen, [])  # the first is waiting: nothing else started
        gate.set()
        self.assertTrue(mail_queue.drain(10))
        self.assertEqual([i for i, _ in seen], [0, 1, 2, 3, 4])
        self.assertEqual(len({name for _, name in seen}), 1)
        self.assertTrue(seen[0][1].startswith("jobhunt-mail"))
        self.assertNotEqual(seen[0][1], threading.current_thread().name)

    def test_a_job_that_fails_is_logged_by_class_and_the_next_one_still_runs(self):
        def boom():
            raise RuntimeError("secret@example.org was in this message")

        with self.assertLogs("backend.mail_queue", level="WARNING") as logs:
            mail_queue.submit(boom)
            done = mail_queue.submit(lambda: "next")
            self.assertEqual(done.result(10), "next")
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("secret@example.org", "\n".join(logs.output))

    def test_too_much_waiting_is_dropped_not_piled_up(self):
        gate = threading.Event()
        with mock.patch.object(mail_queue, "MAX_WAITING", 3), mock.patch.object(mail_queue, "NOTICE_RESERVE", 0):
            futures = [mail_queue.submit(gate.wait, 5) for _ in range(5)]
            self.assertEqual([f is not None for f in futures], [True, True, True, False, False])
            gate.set()
            self.assertTrue(mail_queue.drain(10))

    def test_the_last_places_are_kept_for_security_notices(self):
        gate = threading.Event()
        with mock.patch.object(mail_queue, "MAX_WAITING", 5), mock.patch.object(mail_queue, "NOTICE_RESERVE", 2):
            ordinary = [mail_queue.submit(gate.wait, 5) is not None for _ in range(5)]
            self.assertEqual(ordinary, [True, True, True, False, False])
            self.assertFalse(mail_queue.has_room())
            self.assertTrue(mail_queue.has_room(priority=True))
            notices = [mail_queue.submit(gate.wait, 5, priority=True) is not None for _ in range(3)]
            self.assertEqual(notices, [True, True, False])
            self.assertFalse(mail_queue.has_room(priority=True))
            gate.set()
            self.assertTrue(mail_queue.drain(10))

    def test_send_queues_and_returns_at_once(self):
        gate = threading.Event()
        real = mailer.send
        with mock.patch.object(mailer, "send", lambda *a, **k: (gate.wait(10), real(*a, **k))[1]):
            future = mail_queue.send("someone@example.org", ("Subject", "text", "<p>html</p>", {}))
            self.assertIsNotNone(future)
            self.assertFalse(future.done())
            gate.set()
            self.assertTrue(future.result(10).ok)
        self.assertEqual(len(self.sent), 1)

    def test_nothing_is_queued_while_email_is_off(self):
        with mock.patch.dict(os.environ, {"SMTP_HOST": "", "PUBLIC_BASE_URL": ""}):
            self.assertFalse(mail_queue.enabled())
            self.assertIsNone(mail_queue.send("someone@example.org", ("S", "t", "<p>h</p>", {})))
            self.assertEqual(mail_queue.missing(), ["smtp", "public_url"])
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": ""}):
            self.assertEqual(mail_queue.missing(), ["public_url"])
        self.assertEqual(mail_queue.missing(), [])
        self.assertEqual(self.flush(), [])

    def test_no_address_subject_or_body_in_the_log(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("test").info("marker")
            f = mail_queue.send("private-person@example.org", ("Very private subject", "Very private body",
                                                              "<p>Very private body</p>", {}), user_id=7)
            f.result(10)
        everything = "\n".join(logs.output)
        for secret in ("private-person", "example.org", "Very private"):
            self.assertNotIn(secret, everything)
        self.assertIn("Email sent for user 7", everything)

    def test_a_real_send_failure_is_reported_by_class_only(self):
        import smtplib

        # (inside capture the outbox swallows the message: leave it for this call, so the mailer really tries)
        mailer_box = mailer._outbox
        mailer._outbox = None
        try:
            with mock.patch.object(smtplib, "SMTP", side_effect=smtplib.SMTPConnectError(421, "no")), \
                    self.assertLogs("backend.mailer", level="INFO") as logs:
                result = mailer.send("someone@example.org", "S", "t", "<p>h</p>")
        finally:
            mailer._outbox = mailer_box
        self.assertEqual((result.status, result.error), ("failed", "SMTPConnectError"))
        self.assertNotIn("someone@example.org", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()


class RouteChangeTest(SettingsCase):
    """A new mail server or public address decides where every reset link goes (accounts design §2.5)."""

    def setUp(self):
        super().setUp()
        # (no second admin account: one would stay in the shared test database and end "the last admin" for other tests)
        self.other = helpers.make_user()
        self.others = [self.other.id]
        patcher = mock.patch("backend.admin._active_admins", side_effect=lambda db, besides=None: list(self.others))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bob = helpers.make_user()
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET mfa_enabled = 1 WHERE id = :i"), {"i": self.bob.id})

    def hold(self):
        db = SessionLocal()
        try:
            return mail_route.hold_until(db)
        finally:
            db.close()

    def admin_actions(self):
        return (self.admin.post(f"/api/admin/users/{self.bob.id}/mfa/disable"),
                self.admin.post(f"/api/admin/users/{self.bob.id}/reset-email"))

    def test_setting_mail_up_for_the_first_time_changes_nothing_else(self):
        self.assertEqual(self.put(**GOOD).status_code, 200)
        self.assertIsNone(self.hold())
        self.assertEqual([m["To"] for m in self.sent], [])
        for r in self.admin_actions():
            self.assertNotEqual(r.status_code, 409, r.text)

    def test_a_new_mail_server_tells_the_other_admins_through_the_old_one_and_holds_the_risky_actions(self):
        self.put(**GOOD)
        old = mailer.get_config()
        self.assertIsNotNone(old)
        with mock.patch.object(mail_queue, "send", wraps=mail_queue.send) as send:
            r = self.put(host="relay.evil.example", password="their-own-password")
            self.assertEqual(r.status_code, 200, r.text)
        mail_queue.drain()
        notices = [c for c in send.call_args_list if c.args[1][0].startswith("The mail server")]
        self.assertEqual(len(notices), 1, "one other admin")
        self.assertEqual(notices[0].args[0], self.other.email)
        self.assertEqual(notices[0].kwargs["config"].host, "smtp.example.org", "through the old server")
        self.assertTrue(notices[0].kwargs["priority"])
        self.assertIn(self.other.email, [m["To"] for m in self.sent])
        self.assertNotIn(self.admin_email(), [m["To"] for m in self.sent], "the admin who made it knows")
        self.assertEqual(mailer.get_config().host, "relay.evil.example")
        self.assertIsNotNone(self.hold())
        before = len(self.sent)
        for r in self.admin_actions():
            self.assertEqual((r.status_code, r.json()["code"]), (409, "mail_changed_recently"), r.text)
            self.assertIn("recovery code", r.json()["detail"])
        mail_queue.drain()
        self.assertEqual(len(self.sent), before, "nothing was sent to the person")
        with engine.begin() as conn:
            self.assertTrue(conn.execute(text("SELECT mfa_enabled FROM users WHERE id = :i"), {"i": self.bob.id}).scalar())

    def admin_email(self):
        return helpers.ADMIN_EMAIL

    def test_the_public_address_counts_too(self):
        self.put(**GOOD)
        self.assertEqual(self.put(public_base_url="https://phish.example").status_code, 200)
        self.assertIsNotNone(self.hold())
        self.assertEqual(self.admin_actions()[1].json()["code"], "mail_changed_recently")

    def test_saving_the_same_route_again_or_other_fields_starts_no_hold(self):
        self.put(**GOOD)
        for fields in (dict(GOOD), {"port": "465"}, {"host": "SMTP.example.org"}, {"user": "someone@example.org"},
                       {"from_address": "other@example.org"}, {"password": "another-password-1"}):
            self.assertEqual(self.put(**fields).status_code, 200, fields)
            self.assertIsNone(self.hold(), fields)
        self.assertEqual([m["To"] for m in self.sent], [])

    def test_the_hold_ends_after_24_hours_and_other_people_are_never_held(self):
        self.put(**GOOD)
        self.put(host="relay.example.net", password="new-relay-password")
        with engine.begin() as conn:
            conn.execute(text("UPDATE app_settings SET value_json = :v WHERE key = 'mail_route_changed_at'"),
                         {"v": f'"{(utcnow() - timedelta(hours=23)).isoformat()}"'})
        self.assertEqual(self.admin_actions()[0].status_code, 409)
        with engine.begin() as conn:
            conn.execute(text("UPDATE app_settings SET value_json = :v WHERE key = 'mail_route_changed_at'"),
                         {"v": f'"{(utcnow() - timedelta(hours=25)).isoformat()}"'})
        self.assertEqual([r.status_code for r in self.admin_actions()], [200, 200])

    def test_a_lone_admin_does_not_block_the_change(self):
        self.put(**GOOD)
        self.others = []  # no other admin who is turned on
        self.assertEqual(self.put(host="relay.example.net", password="new-relay-password").status_code, 200)
        self.assertIsNotNone(self.hold(), "the hold doesn't depend on anyone being told")
        mail_queue.drain()
        self.assertEqual([m["To"] for m in self.sent], [])
