"""What memberships, email and digests left behind in the API: nothing. The routes that a membership used to gate (Run search, scoring, research, chat, cover letters, title ideas, shorter
versions and summaries of documents) answer every signed-in account the same way now, whatever Stripe settings an
old .env still holds (docs/design/open-source.md §1.3). Who may call them: anyone signed in, in their own workspace;
signed out, they answer 401. The network and the AI are never reached: the cases end at the first check of the
route itself (a missing job, nothing to work on) or queue a run that has nothing to do.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402

from backend.app import app  # noqa: E402

# What switched the gate on before (a Stripe key and price), and an AI provider so the AI routes get past their own
# "set up the AI first" check.
OLD_ENV = {"STRIPE_SECRET_KEY": "sk_test_" + "a1B2c3D4" * 4, "STRIPE_PRICE_LOOKUP_KEY": "jobhunt_monthly",
           "LLM_BASE_URL": "https://api.openai.com/v1", "LLM_MODEL": "m", "LLM_API_KEY": "sk-not-used-here"}
MISSING = 987654321

# (method, path, body, the answer the route itself gives an account with nothing to work on)
ROUTES = [
    ("post", "/api/search/run", None, 200),
    ("post", "/api/score/run", {"mode": "pending"}, 200),
    ("post", f"/api/score/{MISSING}", None, 404),
    ("post", "/api/companies/research", {"mode": "missing"}, 200),
    ("post", "/api/chats", {"job_ids": [MISSING], "message": "Does this fit?"}, 404),
    ("post", f"/api/chats/{MISSING}/messages", {"message": "And then?"}, 404),
    ("post", f"/api/jobs/{MISSING}/cover-letter", {}, 404),
    ("post", "/api/documents/suggest-titles", None, 400),
    ("post", f"/api/documents/{MISSING}/concise", None, 404),
    ("post", "/api/documents/summary", None, 400),
]


class FormerlyGatedRoutesTest(unittest.TestCase):
    def setUp(self):
        helpers.started()
        env = mock.patch.dict(os.environ, OLD_ENV)
        env.start()
        self.addCleanup(env.stop)

    def wait_for_runs(self, c) -> None:
        for _ in range(400):
            run = c.get("/api/runs/latest").json()
            if not run or run["state"] not in ("queued", "running"):
                return
            time.sleep(0.05)
        self.fail("a run didn't finish")

    def test_an_ordinary_new_account_gets_the_routes_own_answer(self):
        """A new account had no membership and, with payments on, would have been refused (402) or limited to a few
        free searches. Now each route answers it as it answers anyone."""
        bob = helpers.make_user()
        with helpers.client(bob.email) as c:
            for method, path, body, expected in ROUTES:
                with self.subTest(path=path):
                    r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
                    self.assertNotEqual(r.status_code, 402, r.text)
                    self.assertNotIn("membership", r.text.lower())
                    self.assertEqual(r.status_code, expected, f"{path}: {r.text}")
                    self.wait_for_runs(c)
            for _ in range(5):  # as many searches as anyone wants: no free searches to run out of
                self.assertEqual(c.post("/api/search/run").status_code, 200)
                self.wait_for_runs(c)

    def test_signed_out_they_answer_401(self):
        anonymous = TestClient(app)
        with anonymous:
            for method, path, body, _ in ROUTES:
                r = getattr(anonymous, method)(path, **({"json": body} if body is not None else {}))
                self.assertEqual(r.status_code, 401, path)

    def test_no_route_depends_on_a_membership_check(self):
        def names(dependant):
            for dep in dependant.dependencies:
                yield getattr(dep.call, "__qualname__", "") + " " + getattr(dep.call, "__module__", "")
                yield from names(dep)
        for route in app.routes:
            dependant = getattr(route, "dependant", None)
            if dependant is not None:
                for name in names(dependant):
                    self.assertNotIn("membership", name, route.path)

    def test_the_billing_routes_are_gone(self):
        with helpers.client() as c:
            for path in ("/api/billing/status", "/api/admin/stripe"):
                self.assertEqual(c.get(path).status_code, 404, path)
            for path in ("/api/billing/checkout", "/api/billing/verify", "/api/billing/portal", "/api/billing/webhook",
                         f"/api/admin/users/{helpers.admin_id()}/grants"):
                self.assertIn(c.post(path, json={}).status_code, (404, 405), path)  # (no route: the page files answer)
            self.assertIn(c.put(f"/api/admin/users/{helpers.admin_id()}/membership", json={}).status_code, (404, 405))



class EmailAndDigestRoutesGoneTest(unittest.TestCase):
    """No mail is ever sent: the routes of email, password reset by email, address checks and job digests are gone,
    and the ones that were open without a session (forgot, reset, opening a link) are not open any more."""

    OPEN_BEFORE = ("/api/auth/forgot", "/api/auth/reset/info", "/api/auth/reset", "/api/auth/verify-email/info",
                   "/api/auth/verify-email")
    SIGNED_IN_BEFORE = ("/api/auth/verify-email/resend", "/api/auth/email/change", "/api/settings/email/test")

    def test_signed_out_nothing_answers_but_the_sign_in_check(self):
        with TestClient(app) as anonymous:
            for path in self.OPEN_BEFORE + self.SIGNED_IN_BEFORE:
                self.assertEqual(anonymous.post(path, json={}).status_code, 401, path)
            self.assertIn(anonymous.get("/unsubscribe?token=x").status_code, (404, 405))
            self.assertIn(anonymous.post("/unsubscribe?token=x", json={}).status_code, (404, 405))

    def test_signed_in_they_are_not_found(self):
        with helpers.client() as c:
            for path in self.OPEN_BEFORE + self.SIGNED_IN_BEFORE:
                self.assertIn(c.post(path, json={}).status_code, (404, 405), path)
            for path in ("/api/auth/email", "/api/account/digest", "/api/settings/email"):
                self.assertEqual(c.get(path).status_code, 404, path)
            self.assertIn(c.post(f"/api/admin/users/{helpers.admin_id()}/reset-email").status_code, (404, 405))

    def test_an_invitation_is_a_link_to_copy_and_never_emailed(self):
        with helpers.client() as c:
            r = c.post("/api/admin/invites", json={"email": "someone@example.invalid", "send": True})
            self.assertEqual(r.status_code, 422, "there is nothing to send it with")
            r = c.post("/api/admin/invites", json={"email": "someone@example.invalid"})
            self.assertEqual(r.status_code, 201, r.text)
            self.assertIn("/signup.html?invite=", r.json()["link"])
            self.assertFalse({"emailed", "email_status"} & set(r.json()))
        with helpers.client(helpers.make_user().email) as b:  # admins only
            self.assertEqual(b.post("/api/admin/invites", json={}).status_code, 403)

if __name__ == "__main__":
    unittest.main()
