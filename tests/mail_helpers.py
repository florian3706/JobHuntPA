"""What the email tests share (accounts design §9, Phase H): SMTP and the public address set to throwaway values, every
message captured instead of sent (``mailer.capture``), a way to wait for the mail thread, and readers for what a
message says and links to. Nothing here talks to a network."""
from __future__ import annotations

import os
import re
import unittest
from email.message import EmailMessage
from typing import Optional
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402

from backend import mail_queue, mailer, ratelimit  # noqa: E402
from backend.app import app  # noqa: E402

BASE = "https://jobs.example.com"
SMTP_ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "mailer",
            "SMTP_PASSWORD_B64": "", "SMTP_PASSWORD": "s3cret!pw", "SMTP_FROM": "JobHuntPA <jobs@example.com>",
            "SMTP_SECURITY": "starttls", "PUBLIC_BASE_URL": BASE}


def text_of(msg: EmailMessage) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


def html_of(msg: EmailMessage) -> str:
    return msg.get_body(preferencelist=("html",)).get_content()


def token_in(msg: EmailMessage, page: str) -> str:
    """The token in the link to ``page`` ("reset-password.html" or "verify-email.html", in the fragment, or
    "signup.html" with ?invite=). Both parts of the message must carry the same link."""
    found = []
    for body in (text_of(msg), html_of(msg)):
        m = re.search(re.escape(f"{BASE}/{page}") + r"[#?](?:token|invite)=([A-Za-z0-9_-]+)", body)
        assert m, f"no link to {page} in a part of the message"
        found.append(m.group(1))
    assert found[0] == found[1], "the two parts link to different tokens"
    return found[0]


class MailTestCase(unittest.TestCase):
    """Email on (SMTP and the public address set), messages captured. ``self.sent`` is the list of messages."""

    email_on = True

    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)
        env = mock.patch.dict(os.environ, SMTP_ENV if self.email_on else {k: "" for k in SMTP_ENV})
        env.start()
        self.addCleanup(env.stop)
        capture = mailer.capture()
        self.sent: list[EmailMessage] = capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.addCleanup(mail_queue.drain)

    # -- the mail thread ------------------------------------------------------------------------------------------

    def flush(self) -> list[EmailMessage]:
        """Waits for the mail thread, then returns every message captured so far."""
        self.assertTrue(mail_queue.drain(20), "the mail thread didn't finish")
        return self.sent

    def to(self, address: str) -> list[EmailMessage]:
        return [m for m in self.flush() if m["To"] == address]

    def last_to(self, address: str) -> EmailMessage:
        got = self.to(address)
        self.assertTrue(got, "no message was sent to that address")
        return got[-1]

    # -- clients --------------------------------------------------------------------------------------------------

    def anonymous(self) -> TestClient:
        c = TestClient(app)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def signed_in(self, account: Optional[helpers.Account] = None, **kwargs) -> helpers.SignedInClient:
        c = helpers.client(account.email, **kwargs) if account else helpers.client(**kwargs)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c
