"""Tests for the email layer: settings validation, mailer (mocked SMTP), templates and the digest. No network.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from email import message_from_bytes, policy
from html.parser import HTMLParser
from unittest import mock
from urllib.parse import parse_qs, urlsplit

_TMP = tempfile.mkdtemp()
os.environ.setdefault("JOBHUNT_DB_PATH", os.path.join(_TMP, "test.db"))  # never the real database
os.environ.setdefault("JOBHUNT_SCHEDULER", "off")
os.environ.setdefault("JOBHUNT_KEY_FILE", os.path.join(_TMP, "default-server-keys.json"))

from backend import app_settings, email_templates as et, mailer  # noqa: E402
from backend.keys import ServerKeys  # noqa: E402

BASE = "https://jobs.example.com"
SMTP_ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "mailer", "SMTP_PASSWORD": "s3cret!pw",
            "SMTP_FROM": "JobHuntPA <jobs@example.com>", "SMTP_SECURITY": "starttls", "PUBLIC_BASE_URL": BASE}
SMTP_VARS = tuple(SMTP_ENV)

JOB = {
    "id": 42, "workspace_id": 3, "title": "Senior Project Manager", "company": "Acme Pty Ltd",
    "salary_min": 150000, "salary_max": 170000, "office_text": "Sydney CBD", "office_days": 3,
    "work_mode": "hybrid", "employment_type": "full_time", "fit_score": 87.4,
    "fit_summary": "Strong delivery background; light on cloud.", "gap_count": 2,
    "url": "https://www.seek.com.au/job/12345",
}


def keys() -> ServerKeys:
    return ServerKeys.create(os.path.join(tempfile.mkdtemp(dir=_TMP), "server-keys.json"))


def clean_env():
    return mock.patch.dict(os.environ, {k: "" for k in SMTP_VARS})


def configured_env():
    return mock.patch.dict(os.environ, SMTP_ENV)


def luminance(hex_colour: str) -> float:
    rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links, self.texts, self._href, self.tags = [], [], None, []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "a":
            self._href = dict(attrs).get("href")
            self.links.append([self._href, ""])

    def handle_endtag(self, tag):
        if tag == "a":
            self._href = None

    def handle_data(self, data):
        self.texts.append(data)
        if self._href is not None:
            self.links[-1][1] += data


class ValidationTests(unittest.TestCase):
    def ok(self, name, value):
        self.assertTrue(app_settings.ENV_RULES[name](value), (name, value))

    def bad(self, name, value):
        self.assertFalse(app_settings.ENV_RULES[name](value), (name, value))

    def test_all_settings_are_allow_listed(self):
        for name in SMTP_VARS:
            self.assertIn(name, app_settings.ENV_RULES)

    def test_good_values(self):
        self.ok("SMTP_HOST", "smtp.example.com")
        self.ok("SMTP_PORT", "465")
        self.ok("SMTP_USER", "me@example.com")
        self.ok("SMTP_PASSWORD", "p@ss#w0rd!{}")
        self.ok("SMTP_FROM", "jobs@example.com")
        self.ok("SMTP_FROM", "JobHuntPA <jobs@example.com>")
        self.ok("SMTP_SECURITY", "ssl")
        self.ok("PUBLIC_BASE_URL", "https://jobs.example.com")
        self.ok("PUBLIC_BASE_URL", "http://localhost:8040")
        self.ok("PUBLIC_BASE_URL", "https://host.tail1234.ts.net/app")

    def test_line_breaks_are_refused_everywhere(self):
        good = {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "25", "SMTP_USER": "me", "SMTP_PASSWORD": "pw1234",
                "SMTP_FROM": "jobs@example.com", "SMTP_SECURITY": "none", "PUBLIC_BASE_URL": "https://x.example.com"}
        for name, value in good.items():
            for bad in (value + "\n", value + "\r\nBcc: evil@example.com", "\n" + value, value + "\r", value + "\x00"):
                self.bad(name, bad)
                with self.assertRaises(ValueError):
                    app_settings.update_env_file(os.path.join(_TMP, "nope.env"), {name: bad})

    def test_bad_values(self):
        for v in ("", "bad host", "-x.com", "a..b", "smtp.example.com:25", "x" * 300):
            self.bad("SMTP_HOST", v)
        for v in ("0", "65536", "-1", "25.5", "abc", "99999999", " 25"):
            self.bad("SMTP_PORT", v)
        for v in ("has space", "a'b", 'a"b', "pass word", "", "x" * 300, "pa\tss"):
            self.bad("SMTP_PASSWORD", v)
        for v in ("not an address", "a@b", "<jobs@example.com>", "x <jobs@example.com> y", "a@example.com, b@example.com",
                  "Evil <a@example.com>, b@example.com", 'Na"me <a@example.com>', "a@exa mple.com"):
            self.bad("SMTP_FROM", v)
        for v in ("TLS", "starttls ", "plain", ""):
            self.bad("SMTP_SECURITY", v)
        for v in ("ftp://x.example.com", "https://x.example.com?a=1", "https://x.example.com#top",
                  "https://user:pw@x.example.com", "x.example.com", "https://x.example.com/", "https://",
                  "https://x.example.com:99999", "javascript:alert(1)", "https://x.example.com/a b"):
            self.bad("PUBLIC_BASE_URL", v)

    def test_nothing_is_written_for_a_refused_value(self):
        path = os.path.join(_TMP, "write.env")
        with open(path, "w") as f:
            f.write("A=1\n")
        with self.assertRaises(ValueError):
            app_settings.update_env_file(path, {"SMTP_PORT": "70000", "SMTP_HOST": "smtp.example.com"})
        self.assertEqual(open(path).read(), "A=1\n")
        app_settings.update_env_file(path, {"SMTP_PORT": "465", "SMTP_FROM": "JobHuntPA <jobs@example.com>"})
        self.assertIn("SMTP_FROM=JobHuntPA <jobs@example.com>", open(path).read())

    def test_env_example_lists_every_setting(self):
        text = open(os.path.join(os.path.dirname(__file__), "..", ".env.example"), encoding="utf-8").read()
        for name in SMTP_VARS + ("SMTP_PASSWORD_B64",):
            self.assertRegex(text, rf"(?m)^# {name}=")

    def test_config(self):
        with clean_env():
            self.assertIsNone(mailer.get_config())
            self.assertIsNone(mailer.public_base_url())
        with configured_env():
            cfg = mailer.get_config()
            self.assertEqual((cfg.host, cfg.port, cfg.security, cfg.from_address), ("smtp.example.com", 587, "starttls", "jobs@example.com"))
            self.assertNotIn("s3cret", repr(cfg))
            self.assertEqual(mailer.public_base_url(), BASE)


class MailerTests(unittest.TestCase):
    def send(self, security="starttls", port="587", **kw):
        env = dict(SMTP_ENV, SMTP_SECURITY=security, SMTP_PORT=port, **kw)
        with mock.patch.dict(os.environ, env), \
                mock.patch("backend.mailer.smtplib.SMTP") as smtp, mock.patch("backend.mailer.smtplib.SMTP_SSL") as smtp_ssl:
            result = mailer.send("you@example.org", "Hello", "plain body", "<p>html body</p>", {"X-Test": "1"})
        return result, smtp, smtp_ssl

    def test_not_configured(self):
        with clean_env(), self.assertLogs("backend.mailer", "INFO") as logs, \
                mock.patch("backend.mailer.smtplib.SMTP") as smtp:
            result = mailer.send("you@example.org", "Subject", "Body text", "<p>x</p>")
        self.assertEqual(result.status, "not_configured")
        self.assertFalse(result.ok)
        self.assertFalse(result.configured)
        smtp.assert_not_called()
        self.assertEqual(len(logs.output), 1)

    def test_starttls(self):
        result, smtp, smtp_ssl = self.send("starttls", "587")
        self.assertTrue(result.ok)
        smtp_ssl.assert_not_called()
        self.assertEqual(smtp.call_args.args[:2], ("smtp.example.com", 587))
        server = smtp.return_value.__enter__.return_value
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("mailer", "s3cret!pw")
        msg = server.send_message.call_args.args[0]
        self.assertEqual(msg["To"], "you@example.org")
        self.assertEqual(msg["From"], "JobHuntPA <jobs@example.com>")
        self.assertEqual(msg["X-Test"], "1")

    def test_ssl(self):
        result, smtp, smtp_ssl = self.send("ssl", "465")
        self.assertTrue(result.ok)
        smtp.assert_not_called()
        self.assertEqual(smtp_ssl.call_args.args[:2], ("smtp.example.com", 465))
        server = smtp_ssl.return_value.__enter__.return_value
        server.starttls.assert_not_called()
        server.login.assert_called_once()
        server.send_message.assert_called_once()

    def test_none_without_login(self):
        result, smtp, _ = self.send("none", "25", SMTP_USER="", SMTP_PASSWORD="")
        self.assertTrue(result.ok)
        server = smtp.return_value.__enter__.return_value
        server.starttls.assert_not_called()
        server.login.assert_not_called()
        server.send_message.assert_called_once()

    def test_failure_is_a_result_not_an_exception(self):
        with configured_env(), mock.patch("backend.mailer.smtplib.SMTP", side_effect=ConnectionRefusedError("boom to you@example.org")):
            result = mailer.send("you@example.org", "S", "t", "<p>t</p>")
        self.assertEqual((result.status, result.error), ("failed", "ConnectionRefusedError"))

    def test_header_injection_is_refused(self):
        with mailer.capture() as box:
            self.assertEqual(mailer.send("a@example.org\r\nBcc: b@example.org", "S", "t", "h").status, "failed")
            self.assertEqual(mailer.send("a@example.org", "S\nBcc: x@example.org", "t", "h").status, "failed")
            self.assertEqual(mailer.send("a@example.org", "S", "t", "h", {"X": "a\nb"}).status, "failed")
        self.assertEqual(box, [])

    def test_capture_outbox_and_message_shape(self):
        with clean_env(), mailer.capture() as box:
            result = mailer.send("you@example.org", "Hi", "plain", "<p>html</p>")
        self.assertTrue(result.ok)
        msg = box[0]
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        self.assertEqual([p.get_content_type() for p in msg.iter_parts()], ["text/plain", "text/html"])
        reparsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
        self.assertIn("plain", reparsed.get_body(("plain",)).get_content())

    def test_nothing_sensitive_is_logged(self):
        secret_to, secret_body = "private.person@example.org", "TOPSECRETBODY"
        with configured_env(), self.assertLogs("backend.mailer", "DEBUG") as logs:
            with mock.patch("backend.mailer.smtplib.SMTP"):
                mailer.send(secret_to, "TOPSECRETSUBJECT", secret_body, "<p>" + secret_body + "</p>")
            with mock.patch("backend.mailer.smtplib.SMTP", side_effect=OSError(f"cannot reach {secret_to} {secret_body}")):
                mailer.send(secret_to, "TOPSECRETSUBJECT", secret_body, "<p>x</p>")
            mailer.send("bad\naddress@example.org", "S", "t", "h")
        joined = "\n".join(logs.output)
        for needle in (secret_to, "private.person", secret_body, "TOPSECRETSUBJECT", "s3cret!pw", "bad", "example.org"):
            self.assertNotIn(needle, joined)


class AccountTemplateTests(unittest.TestCase):
    def check_parts(self, tpl, *needles):
        subject, text, html, headers = tpl
        self.assertTrue(subject and "\n" not in subject)
        self.assertIsInstance(headers, dict)
        for n in needles:
            self.assertIn(n, text)
            self.assertIn(n, html)
        return subject, text, html, headers

    def check_accessible(self, html):
        p = Links(html)
        self.assertRegex(html, r'<html lang="en">')
        self.assertRegex(html, r"<title>[^<]{8,}</title>")
        self.assertEqual(html.count("<table"), html.count('<table role="presentation"'))
        self.assertIn("<h1", html)
        self.assertNotIn("<img", html)
        for href, text in p.links:
            self.assertGreaterEqual(len(text.split()), 2, f"link text {text!r} is not meaningful out of context")
            self.assertNotRegex(text.lower(), r"^(click here|here|link|more)$")
            self.assertRegex(href, r"^https?://")
        return p

    def test_password_reset(self):
        subject, text, html, h = self.check_parts(
            et.password_reset("abc.123.xyz", "in 1 hour", base_url=BASE), "in 1 hour", f"{BASE}/reset-password.html#token=abc.123.xyz")
        self.check_accessible(html)
        self.assertIn("Reset", subject)

    def test_token_is_url_encoded_and_html_escaped(self):
        _, text, html, _ = et.password_reset('a&b"<c>', "soon", base_url=BASE)
        self.assertIn("#token=a%26b%22%3Cc%3E", text)
        self.assertNotIn("<c>", html)

    def test_invite_and_verify(self):
        _, _, html, _ = self.check_parts(et.invite("tok_1", inviter="Ana <b>", base_url=BASE), f"{BASE}/signup.html?invite=tok_1")
        self.assertNotIn("<b>", html)
        self.check_accessible(html)
        _, _, html, _ = self.check_parts(et.verify_email("tok_2", expires="tomorrow", base_url=BASE), f"{BASE}/verify-email.html#token=tok_2", "tomorrow")
        self.check_accessible(html)

    def test_security_notices(self):
        for event, needle in (("password_changed", "password"), ("mfa_disabled_by_admin", "administrator"),
                              ("email_changed", "email address")):
            _, text, html, _ = self.check_parts(et.security_notice(event, base_url=BASE), needle)
            self.check_accessible(html)
        with self.assertRaises(ValueError):
            et.security_notice("nonsense")

    def test_tokens_that_give_access_are_in_the_fragment_never_the_query(self):
        for tpl in (et.password_reset("tok-1", "soon", base_url=BASE), et.verify_email("tok-2", base_url=BASE)):
            for part in (tpl[1], tpl[2]):
                self.assertNotIn("?token=", part)
                self.assertRegex(part, r"\.html#token=tok-[12]")
        text = et.invite("tok-3", base_url=BASE)[1]  # (the sign-up page reads ?invite=: Phase F built it that way)
        self.assertIn(f"{BASE}/signup.html?invite=tok-3", text)

    def test_the_smtp_test_message(self):
        subject, text, html, headers = et.smtp_test_message(base_url=BASE)
        self.assertEqual(subject, "JobHuntPA test email")
        self.assertIn("test message", text)
        self.assertIn("test message", html)
        self.check_accessible(html)

    def test_no_base_url(self):
        with clean_env(), self.assertRaises(et.NoBaseUrl):
            et.password_reset("t", "soon")

    def test_contrast(self):
        for fg in (et.TEXT, et.MUTED, et.LINK):
            self.assertGreaterEqual(contrast(fg, "#ffffff"), 4.5, fg)
            self.assertGreaterEqual(contrast(fg, et.CARD), 4.5, fg)

    def test_every_colour_used_is_one_we_checked(self):
        _, _, html, _ = et.job_digest([JOB], keys=keys(), unsubscribe_id="u1", base_url=BASE)
        used = set(re.findall(r"(?<![&\w])#[0-9a-fA-F]{6}\b", html))
        self.assertLessEqual({c.lower() for c in used}, {et.TEXT, et.MUTED, et.LINK, et.CARD, et.BORDER, "#ffffff"})


class DigestTests(unittest.TestCase):
    def digest(self, jobs=None, **kw):
        k = kw.pop("keys", None) or keys()
        kw.setdefault("unsubscribe_id", "user_abc-123")
        kw.setdefault("base_url", BASE)
        return k, et.job_digest([JOB] if jobs is None else jobs, keys=k, **kw)

    def test_every_card_field_in_both_parts(self):
        _, (subject, text, html, _) = self.digest(every_days=7)
        self.assertIn("1 new job", subject)
        for needle in ("Senior Project Manager", "Acme Pty Ltd", "$150k–$170k", "Sydney CBD", "3 days in the office",
                       "Hybrid", "Full-time", "87", "Strong delivery background; light on cloud.", "2 gaps",
                       f"{BASE}/job.html?id=42&ws=3", "https://www.seek.com.au/job/12345"):
            self.assertIn(needle, text, needle)
            self.assertIn(needle.replace("&", "&amp;"), html, needle)
        self.assertIn("every 7 days", text)
        self.assertIn("every 7 days", html)

    def test_card_is_a_heading_with_clear_links(self):
        _, (_, _, html, _) = self.digest()
        p = Links(html)
        self.assertIn("h2", [t for t, _ in p.tags])
        self.assertEqual("".join(p.texts).count("Senior Project Manager"), 3)  # h2 and both link texts
        texts = [t for _, t in p.links]
        self.assertTrue(any(t.startswith("Open ") and "Senior Project Manager" in t for t in texts))
        self.assertTrue(any("original posting" in t for t in texts))
        self.assertEqual(html.count("<table"), html.count('<table role="presentation"'))
        self.assertIn('<html lang="en">', html)

    def test_only_the_summary_blurb_not_requirement_detail(self):
        job = dict(JOB, requirements=[{"text": "SECRETREQ"}], evidence="SECRETEVIDENCE", matched=["x"], gaps=["g1", "g2", "g3"])
        del job["gap_count"]
        _, (_, text, html, _) = self.digest([job])
        self.assertNotIn("SECRETREQ", text + html)
        self.assertNotIn("SECRETEVIDENCE", text + html)
        self.assertIn("3 gaps", text)

    def test_missing_fields_are_skipped(self):
        _, (_, text, html, _) = self.digest([{"id": 7, "title": "Bare Role"}])
        self.assertIn("Bare Role", text)
        self.assertIn(f"{BASE}/job.html?id=7", text)
        for absent in ("Company", "Salary", "Location", "Work type", "Employment", "Fit score", "Gaps", "Original posting", "None", "null"):
            self.assertNotIn(absent, text + html, absent)
        _, (_, text, html, _) = self.digest([{}])
        self.assertIn("Untitled job", text)
        self.assertNotIn("/job.html", text + html)

    def test_field_formats(self):
        card = lambda **kw: et.digest_card(dict(kw), BASE)  # noqa: E731
        self.assertEqual(card(salary_min=95000, salary_max=95000)["salary"], "$95k")
        self.assertEqual(card(salary_min=120000)["salary"], "From $120k")
        self.assertEqual(card(salary_max=130500)["salary"], "Up to $130.5k")
        self.assertEqual(card(salary_text="$600 per day")["salary"], "$600 per day")
        self.assertEqual(card(location_text="Melbourne")["location"], "Melbourne")
        self.assertEqual(card(office_text="HQ", location_text="Melbourne")["location"], "HQ")
        self.assertEqual(card(office_days=1)["office_days"], "1 day in the office")
        self.assertIsNone(card(office_days=0)["office_days"])
        self.assertEqual(card(fit_score=250)["score"], 100)
        self.assertEqual(card(fit_score=0)["score"], 0)
        self.assertEqual(card(gap_count=1)["gaps"], "1 gap")
        self.assertEqual(card(gap_count=0)["gaps"], "0 gaps")
        self.assertEqual({card(work_mode=m)["work_mode"] for m in ("remote", "hybrid", "onsite")}, {"Remote", "Hybrid", "Onsite"})
        self.assertIsNone(card(work_mode="unknown")["work_mode"])
        for raw, label in (("full_time", "Full-time"), ("Part time", "Part-time"), ("CONTRACTOR", "Contract"),
                           ("casual", "Casual"), ("temporary", "Temporary"), ("internship", "Internship")):
            self.assertEqual(card(employment_type=raw)["employment"], label)
        self.assertIsNone(card(employment_type="gibberish")["employment"])
        self.assertIsNone(card(url="javascript:alert(1)")["original_url"])

    def test_html_in_job_data_is_escaped(self):
        _, (_, text, html, _) = self.digest([dict(JOB, title="<script>x</script>", company='A & "B"', fit_summary="<b>hi</b>")])
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>hi", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("A &amp; &quot;B&quot;", html)

    def test_more_count_and_plural(self):
        _, (subject, text, _, _) = self.digest([JOB, dict(JOB, id=43)], more_count=23)
        self.assertIn("25 new jobs", subject)
        self.assertIn("And 23 more in JobHuntPA", text)

    def test_unsubscribe_link_and_token_verify(self):
        k, (_, text, html, headers) = self.digest(unsubscribe_id="user_abc-123")
        url = re.search(r"Unsubscribe from this digest: (\S+)", text).group(1)
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", f"{BASE}/unsubscribe")
        token = parse_qs(parts.query)["token"][0]
        self.assertEqual(k.verify("unsub", token), "user_abc-123")
        self.assertIsNone(k.verify("reset", token))
        self.assertIsNone(k.verify("unsub", token[:-2] + "xx"))
        self.assertIsNone(keys().verify("unsub", token))  # another install's keys
        self.assertIn(url, html)
        link_texts = [t for h, t in Links(html).links if h == url]
        self.assertEqual(link_texts, ["Unsubscribe from this digest"])
        # still valid after the signing key rotates (old emails keep working)
        k.rotate_signing()
        self.assertEqual(k.verify("unsub", token), "user_abc-123")

    def test_rfc8058_headers(self):
        _, (_, text, _, headers) = self.digest()
        url = re.search(r"Unsubscribe from this digest: (\S+)", text).group(1)
        self.assertEqual(headers["List-Unsubscribe"], f"<{url}>")
        self.assertTrue(url.startswith("https://"))
        self.assertEqual(headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        with mailer.capture() as box:
            mailer.send("you@example.org", "S", text, "<p>x</p>", headers)
        self.assertEqual(box[0]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertEqual(box[0]["List-Unsubscribe"], f"<{url}>")

    def test_footer_states_frequency_and_how_to_change(self):
        _, (_, text, html, _) = self.digest(every_days=1)
        for part in (text, html):
            self.assertIn("every day", part)
            self.assertIn("email settings", part)

    def test_accessibility_of_whole_digest(self):
        _, (subject, _, html, _) = self.digest([JOB, dict(JOB, id=2, title="Second Role")], name="Flo")
        self.assertIn(f"<title>{subject}</title>", html)
        self.assertIn("<h1", html)
        self.assertEqual(html.count("<h2"), 2)
        self.assertNotIn("<img", html)
        for href, text in Links(html).links:
            self.assertGreaterEqual(len(text.split()), 3, text)

    def test_digest_end_to_end_through_the_mailer(self):
        _, (subject, text, html, headers) = self.digest()
        with mailer.capture() as box:
            self.assertTrue(mailer.send("you@example.org", subject, text, html, headers).ok)
        parts = list(box[0].iter_parts())
        self.assertIn("Senior Project Manager", parts[0].get_content())
        self.assertIn("<h2", parts[1].get_content())


if __name__ == "__main__":
    unittest.main()
