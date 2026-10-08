"""Memberships and Stripe, wired in (accounts design §8, Phase I): the gate at the endpoints, free searches and their
refunds, scheduled searches, checkout / verify / portal / webhook / sync with StripeClient mocked, the admin side
(Stripe settings, grants that move a subscriber's payment, exempting someone who pays), and account deletion
cancelling the subscription. Nothing here touches the network.

Run:  python3 -m unittest tests.test_membership -v
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest import mock

import stripe

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import access, billing, crypto, membership, ratelimit, schedule, tasks  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import (  # noqa: E402
    Membership, MembershipGrant, SearchRun, SessionLocal, User, UserKeys, utcnow,
)
from backend.sessions import require_admin  # noqa: E402

KEY = "sk_test_" + "a1B2c3D4" * 4
LIVE = "sk_live_" + "a1B2c3D4" * 4
WHSEC = "whsec_" + "x9Y8z7W6" * 4
NOW = datetime.now(timezone.utc)
TS = int(NOW.timestamp())
DAY = 86400
FRIDAY_1020 = datetime(2026, 10, 2, 0, 20, tzinfo=timezone.utc)  # Fri 10:20 Sydney: the 10:00 slot is due
SECRETS = ("cus_123", "sub_123", "pm_1", "4242", KEY, WHSEC, "cs_test_abc123")


def tearDownModule():
    helpers.remove_scratch_folders()


def stripe_sub(ref, status="active", **kw):
    """A Stripe subscription as the recent API returns it (period dates on the item)."""
    ts = int(time.time())  # now, not when the module was imported
    base = {
        "id": "sub_123", "status": status, "customer": "cus_123", "created": ts - 30 * DAY,
        "cancel_at_period_end": False, "metadata": {"jh_ref": ref},
        "items": {"data": [{"current_period_start": ts - DAY, "current_period_end": ts + 29 * DAY,
                            "price": {"id": "price_m", "lookup_key": "jobhunt_monthly", "unit_amount": 900,
                                      "currency": "aud", "recurring": {"interval": "month"}}}]},
        "default_payment_method": {"id": "pm_1", "card": {"brand": "visa", "last4": "4242"}},
    }
    base.update(kw)
    return base


def price(lookup_key, amount, interval):
    return {"id": "price_" + interval, "lookup_key": lookup_key, "unit_amount": amount, "currency": "aud",
            "recurring": {"interval": interval}}


def signed(payload: bytes, secret: str = WHSEC, when=None) -> str:
    t = int(when or time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def event(kind, obj, event_id="evt_1"):
    return json.dumps({"id": event_id, "object": "event", "type": kind, "data": {"object": obj}}).encode()


class Base(unittest.TestCase):
    """Billing on (a test key, two prices), Stripe's client, the search and the scorer mocked."""

    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        membership.LIMITER.reset()
        membership._tried.clear()
        billing.clear_price_cache()
        self.stripe = mock.MagicMock()
        self.stripe.v1.prices.list.return_value = {"data": [price("jobhunt_monthly", 900, "month"),
                                                            price("jobhunt_yearly", 9000, "year")]}
        env = mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": KEY, "STRIPE_PRICE_LOOKUP_KEY": "",
                                           "STRIPE_WEBHOOK_SECRET": "", "PUBLIC_BASE_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        for target, kwargs in (("backend.billing._client", {"return_value": self.stripe}),
                               ("backend.pipeline.run_search", {"return_value": {"added": 0}}),
                               ("backend.duplicates.scan_workspace", {"return_value": {}}),
                               ("backend.scorer.config_problem", {"return_value": None})):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.scored = mock.patch("backend.app._score", return_value={"scored": 0})
        self.score = self.scored.start()
        self.addCleanup(self.scored.stop)
        self.addCleanup(billing.clear_price_cache)

    # -- accounts ---------------------------------------------------------------------------------------------
    def user(self, **fields):
        """An ordinary account with these membership fields, and a signed-in client."""
        acct = helpers.make_user()
        if fields:
            self.set_membership(acct.id, **fields)
        c = helpers.client(acct.email)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return acct, c

    def set_membership(self, user_id, **fields):
        if fields.get("stripe_status"):
            fields.setdefault("stripe_mode", "test")  # (setUp runs on a test key)
        db = SessionLocal()
        try:
            db.query(Membership).filter(Membership.user_id == user_id).update(fields)
            db.commit()
        finally:
            db.close()

    def member(self, user_id) -> Membership:
        db = SessionLocal()
        try:
            row = db.get(Membership, user_id)
            db.expunge(row)
            return row
        finally:
            db.close()

    def grant(self, user_id, start=None, end=None, revoked=False):  # naive UTC times; by default around now
        db = SessionLocal()
        try:
            now = utcnow()
            db.add(MembershipGrant(user_id=user_id, kind="free_period", starts_at=start or now - timedelta(hours=1),
                                   ends_at=end or now + timedelta(days=1), revoked_at=now if revoked else None))
            db.commit()
        finally:
            db.close()

    def subscribe(self, user_id, status="active", period_end=None, grace=None):
        self.set_membership(user_id, stripe_status=status, plan="jobhunt_monthly",
                            current_period_end=period_end or utcnow() + timedelta(days=20), grace_until=grace)

    def run_and_wait(self, c, headers=None):
        r = c.post("/api/search/run", headers=headers or {})
        self.assertEqual(r.status_code, 200, r.text)
        run = r.json()
        for _ in range(400):
            got = c.get(f"/api/runs/{run['id']}", headers=headers or {}).json()
            if got["state"] not in ("queued", "running"):
                return run, got
            time.sleep(0.05)
        self.fail("the run didn't finish")

    def billing_off(self):
        patcher = mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        billing.clear_price_cache()

    def sealed_ids(self, acct):
        """The billing_self of an account, opened under its own key."""
        with helpers.as_account(acct):
            return json.loads(crypto.unseal(self.member(acct.id).billing_self,
                                            f"memberships.billing_self|{acct.id}"))


# The endpoints that start a paid feature, and the feature each is gated by (accounts design §8).
GATED = {
    ("POST", "/api/search/run"): access.SEARCH,
    ("POST", "/api/score/run"): access.RESCORE,
    ("POST", "/api/score/{job_id}"): access.RESCORE,
    ("POST", "/api/companies/research"): access.RESEARCH,
    ("POST", "/api/documents/{doc_id}/concise"): access.CONCISE_OR_SUMMARY,
    ("POST", "/api/documents/summary"): access.CONCISE_OR_SUMMARY,
    ("POST", "/api/documents/suggest-titles"): access.CONCISE_OR_SUMMARY,
    ("POST", "/api/chats"): access.CHAT,
    ("POST", "/api/chats/{chat_id}/messages"): access.CHAT,
    ("POST", "/api/jobs/{job_id}/cover-letter"): access.COVER_LETTER,
}
REQUESTS = {
    ("POST", "/api/search/run"): ("/api/search/run", None),
    ("POST", "/api/score/run"): ("/api/score/run", {"mode": "pending"}),
    ("POST", "/api/score/{job_id}"): ("/api/score/999999", None),
    ("POST", "/api/companies/research"): ("/api/companies/research", {}),
    ("POST", "/api/documents/{doc_id}/concise"): ("/api/documents/999999/concise", None),
    ("POST", "/api/documents/summary"): ("/api/documents/summary", None),
    ("POST", "/api/documents/suggest-titles"): ("/api/documents/suggest-titles", None),
    ("POST", "/api/chats"): ("/api/chats", {"job_ids": [999999], "message": "hello"}),
    ("POST", "/api/chats/{chat_id}/messages"): ("/api/chats/999999/messages", {"message": "hello"}),
    ("POST", "/api/jobs/{job_id}/cover-letter"): ("/api/jobs/999999/cover-letter", {}),
}


def api_routes():
    """Every endpoint of the app (FastAPI keeps included routers as lazy objects: open them)."""
    found = []

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes)

    walk(app.routes)
    return found


class EveryGatedEndpoint(unittest.TestCase):
    def routes(self):
        return api_routes()

    def test_the_gated_routes_are_exactly_the_documented_set(self):
        found = {}
        for route in self.routes():
            for dep in route.dependant.dependencies:
                feature = getattr(dep.call, "gated_feature", None)
                if feature:
                    for method in route.methods:
                        found[(method, route.path)] = feature
        self.assertEqual(found, GATED)
        self.assertEqual(set(REQUESTS), set(GATED))

    def test_no_endpoint_that_calls_the_llm_or_starts_a_run_is_left_ungated(self):
        """Every route whose code reaches the model or the run queue is gated, or admin-only (an admin needs no
        membership). New ones must be added to GATED, or this fails."""
        import inspect

        markers = ("call_model(", "tasks.start(", "start_search_run(", "_rewrite(", "score_one(", "web_search(",
                   "llm_providers.chat", "_answer(", "_llm_config(", "test_connection(", "detect_levels(")
        missing = []
        for route in self.routes():
            if not route.path.startswith("/api/"):
                continue
            calls = [d.call for d in route.dependant.dependencies] + [d.dependency for d in route.dependencies]
            if any(getattr(c, "gated_feature", None) for c in calls) or require_admin in calls:
                continue
            if any(m in inspect.getsource(route.endpoint) for m in markers):
                missing.append((sorted(route.methods), route.path))
        self.assertEqual(missing, [])

    def test_unknown_features_are_refused(self):
        with self.assertRaises(ValueError):
            membership.gate("teleport")


class GateAtTheEndpoints(Base):
    """The truth table (backend/access.py) at the endpoints: 402 membership_required, or any other answer."""

    def answers(self, c):
        out = {}
        for key, (path, body) in REQUESTS.items():
            r = c.post(path, json=body) if body is not None else c.post(path)
            out[key] = r
        return out

    def assertAllowed(self, c, msg=""):
        for key, r in self.answers(c).items():
            self.assertNotEqual(r.status_code, 402, f"{key} {msg} {r.text}")

    def assertRefused(self, c, msg="", search_left=False):
        """Every gated endpoint says 402: the AI features always with ai_members_only, a search with
        free_searches_used (or, with ``search_left``, a search is let through: the account has free searches)."""
        for key, r in self.answers(c).items():
            if search_left and GATED[key] == access.SEARCH:
                self.assertNotEqual(r.status_code, 402, f"{key} {msg} {r.text}")
                continue
            self.assertEqual(r.status_code, 402, f"{key} {msg} {r.text}")
            body = r.json()
            self.assertEqual(body["code"], "membership_required")
            self.assertTrue(body["detail"])
            ai = GATED[key] != access.SEARCH
            self.assertEqual(body["reason"], "ai_members_only" if ai else "free_searches_used", f"{key} {msg}")
            self.assertEqual("AI features are for members" in body["detail"], ai, f"{key} {msg} {body['detail']}")

    def test_a_trial_account_may_search_but_every_ai_feature_is_refused(self):
        acct, c = self.user()
        self.assertRefused(c, "(trial)", search_left=True)
        self.assertEqual(self.member(acct.id).free_searches_used, 1, "only the search used a free search")

    def test_the_ai_refusal_is_the_same_whatever_is_left_of_the_free_searches(self):
        for used in (0, 2, 3):
            acct, c = self.user(free_searches_used=used)
            for key, r in self.answers(c).items():
                if GATED[key] != access.SEARCH:
                    self.assertEqual((r.status_code, r.json()["reason"]), (402, "ai_members_only"), (used, key))

    def test_a_failed_ai_request_uses_no_free_search_and_starts_nothing(self):
        acct, c = self.user()
        for key, (path, body) in REQUESTS.items():
            if GATED[key] != access.SEARCH:
                c.post(path, json=body) if body is not None else c.post(path)
        self.assertEqual(self.member(acct.id).free_searches_used, 0)
        self.assertEqual(c.get("/api/runs/latest").json(), None)
        self.score.assert_not_called()

    def test_when_the_free_searches_are_used_every_paid_feature_is_refused_but_viewing_stays(self):
        acct, c = self.user(free_searches_used=3)
        self.assertRefused(c)
        for path in ("/api/jobs", "/api/documents", "/api/profile", "/api/pins", "/api/schedule", "/api/runs/latest",
                     "/api/billing/status", "/api/account/prefs", "/api/chats", "/api/companies"):
            self.assertNotEqual(c.get(path).status_code, 402, path)
        self.assertEqual(c.patch("/api/jobs/status", json={"ids": [], "status": "applied"}).status_code, 200)

    def test_an_admin_needs_no_membership(self):
        with helpers.client() as admin:
            self.set_membership(helpers.admin_id(), free_searches_used=50)
            self.addCleanup(self.set_membership, helpers.admin_id(), free_searches_used=0)
            self.assertAllowed(admin, "(admin)")

    def test_exempt_accounts_are_members(self):
        acct, c = self.user(free_searches_used=3, exempt=True)
        self.assertAllowed(c, "(exempt)")

    def test_an_active_free_period_makes_a_member_and_an_ended_one_does_not(self):
        acct, c = self.user(free_searches_used=3)
        self.grant(acct.id, revoked=True)
        self.grant(acct.id, start=utcnow() - timedelta(days=3), end=utcnow() - timedelta(days=1))
        self.grant(acct.id, start=utcnow() + timedelta(days=1), end=utcnow() + timedelta(days=2))
        self.assertRefused(c)
        self.grant(acct.id)
        self.assertAllowed(c, "(grant)")

    def test_subscription_statuses(self):
        acct, c = self.user(free_searches_used=3)
        cases = [("active", None, True), ("trialing", None, True), ("past_due", utcnow() + timedelta(days=2), True),
                 ("past_due", utcnow() - timedelta(minutes=1), False), ("past_due", None, False),
                 ("canceled", None, False), ("unpaid", None, False), ("incomplete", None, False),
                 ("paused", None, False)]
        for status, grace, ok in cases:
            self.subscribe(acct.id, status, grace=grace)
            (self.assertAllowed if ok else self.assertRefused)(c, f"({status}, grace {grace})")

    def test_billing_off_lets_everyone_through(self):
        acct, c = self.user(free_searches_used=3)
        self.assertRefused(c)
        self.billing_off()
        self.assertAllowed(c, "(billing off)")

    def test_no_price_in_stripe_means_billing_off_too(self):
        acct, c = self.user(free_searches_used=3)
        self.stripe.v1.prices.list.return_value = {"data": []}
        billing.clear_price_cache()
        self.assertAllowed(c, "(no price)")

    def test_stripe_being_down_keeps_the_gate_closed(self):
        acct, c = self.user(free_searches_used=3)
        self.stripe.v1.prices.list.side_effect = RuntimeError("network down")
        billing.clear_price_cache()
        self.assertRefused(c)

    def test_a_stale_subscription_is_checked_with_stripe_at_most_once_an_hour(self):
        acct, c = self.user(free_searches_used=3)
        self.subscribe(acct.id, "active", period_end=utcnow() - timedelta(hours=2))  # within the 3-day tolerance
        self.stripe.v1.subscriptions.search.return_value = {"data": [
            stripe_sub(self.member(acct.id).stripe_ref, "canceled")]}
        self.assertEqual(c.post("/api/documents/summary").status_code, 402)  # Stripe says it ended
        self.assertEqual(self.member(acct.id).stripe_status, "canceled")
        self.subscribe(acct.id, "active", period_end=utcnow() - timedelta(hours=2))
        self.assertNotEqual(c.post("/api/documents/summary").status_code, 402)  # no second ask within the hour
        self.assertEqual(self.stripe.v1.subscriptions.search.call_count, 1)

    def test_a_stale_subscription_that_stripe_renewed_stays_a_member(self):
        acct, c = self.user(free_searches_used=3)
        ref = self.member(acct.id).stripe_ref
        self.subscribe(acct.id, "active", period_end=utcnow() - timedelta(hours=2))
        self.stripe.v1.subscriptions.search.return_value = {"data": [stripe_sub(ref)]}
        self.assertNotEqual(c.post("/api/documents/summary").status_code, 402)
        m = self.member(acct.id)
        self.assertGreater(m.current_period_end, utcnow() + timedelta(days=20))


class FreeSearches(Base):
    def test_a_manual_search_uses_one_and_marks_the_run(self):
        acct, c = self.user()
        run, done = self.run_and_wait(c)
        self.assertTrue(run["free_search"])
        self.assertEqual(done["state"], "done")
        self.assertEqual(self.member(acct.id).free_searches_used, 1)

    def test_a_search_that_is_already_running_isnt_charged_twice(self):
        acct, c = self.user()
        import threading
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch("backend.pipeline.run_search", side_effect=lambda ws, progress: gate.wait(10) or {}):
            first = c.post("/api/search/run").json()
            again = c.post("/api/search/run").json()
            self.assertTrue(again.get("already_running"))
            self.assertEqual(self.member(acct.id).free_searches_used, 1)
            gate.set()
            for _ in range(200):
                if c.get(f"/api/runs/{first['id']}").json()["state"] == "done":
                    break
                time.sleep(0.05)

    def test_a_failed_run_gives_it_back(self):
        acct, c = self.user()
        with mock.patch("backend.pipeline.run_search", side_effect=RuntimeError("boom")):
            run, done = self.run_and_wait(c)
        self.assertEqual(done["state"], "failed")
        self.assertEqual(self.member(acct.id).free_searches_used, 0)
        self.assertFalse(c.get(f"/api/runs/{run['id']}").json()["free_search"])

    def test_a_run_that_fails_after_the_search_stage_keeps_the_free_search_used(self):
        """An answer built to break the scoring must not turn every search into a free one: the search had run (and
        fetched the sources) by then."""
        for where, patch in (("scoring", mock.patch("backend.app._score", side_effect=RuntimeError("bad answer"))),
                             ("duplicates", mock.patch("backend.duplicates.scan_workspace", side_effect=RuntimeError("x")))):
            acct, c = self.user()
            with patch:
                run, done = self.run_and_wait(c)
            self.assertEqual(done["state"], "failed", where)
            self.assertEqual(self.member(acct.id).free_searches_used, 1, where)
            self.assertFalse(done["free_search"], "the mark is cleared so nothing can refund it later")
            tasks.mark_interrupted()
            self.assertEqual(self.member(acct.id).free_searches_used, 1, where)

    def test_the_free_search_is_marked_while_the_search_runs_and_kept_once_it_is_over(self):
        acct, c = self.user()
        seen = {}

        def search_then_stop(ws, progress):
            seen["during"] = c.get("/api/runs/latest").json()["free_search"]
            return {"job_ids": []}
        with mock.patch("backend.pipeline.run_search", side_effect=search_then_stop):
            self.run_and_wait(c)
        self.assertTrue(seen["during"], "marked while the search runs")
        self.assertEqual(self.member(acct.id).free_searches_used, 1)

    def test_a_failure_is_refunded_once_only(self):
        acct, c = self.user(free_searches_used=2)
        run, _ = self.run_and_wait(c)  # uses the 3rd
        self.assertEqual(self.member(acct.id).free_searches_used, 3)
        db = SessionLocal()
        try:
            db.query(SearchRun).filter(SearchRun.id == run["id"]).update({SearchRun.free_search: True})
            self.assertEqual(membership.refund_runs(db, [run["id"], run["id"]]), 1)
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.member(acct.id).free_searches_used, 2)

    def test_an_interrupted_run_gives_it_back(self):
        acct, c = self.user(free_searches_used=2)
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                db.add(SearchRun(workspace_id=acct.workspace_id, kind="search", state="running", progress_json="{}",
                                 free_search=True))
                db.add(SearchRun(workspace_id=acct.workspace_id, kind="search", state="running", progress_json="{}"))
                db.commit()
            finally:
                db.close()
        tasks.mark_interrupted()
        self.assertEqual(self.member(acct.id).free_searches_used, 1)  # one refund, for the run that used one
        tasks.mark_interrupted()
        self.assertEqual(self.member(acct.id).free_searches_used, 1)

    def runs(self, ws_id, kind=None):
        db = SessionLocal()
        try:
            q = db.query(SearchRun.kind, SearchRun.free_search).filter(SearchRun.workspace_id == ws_id)
            return [(k, bool(f)) for k, f in (q.filter(SearchRun.kind == kind) if kind else q)]
        finally:
            db.close()

    def wait_for_runs(self, c, run_id):
        for _ in range(400):
            if c.get(f"/api/runs/{run_id}").json()["state"] not in ("queued", "running"):
                return
            time.sleep(0.05)
        self.fail("the run didn't finish")

    def test_members_are_never_charged(self):
        acct, c = self.user(exempt=True)
        run, _ = self.run_and_wait(c)
        self.assertFalse(run["free_search"])
        self.assertEqual(self.member(acct.id).free_searches_used, 0)
        acct, c = self.user()
        self.subscribe(acct.id)
        self.run_and_wait(c)
        self.assertEqual(self.member(acct.id).free_searches_used, 0)
        with helpers.client() as admin:
            before = self.member(helpers.admin_id()).free_searches_used
            self.run_and_wait(admin)
            self.assertEqual(self.member(helpers.admin_id()).free_searches_used, before)

    def test_nothing_is_used_during_a_free_period(self):
        acct, c = self.user()
        self.grant(acct.id)
        run, _ = self.run_and_wait(c)
        self.assertFalse(run["free_search"])
        self.assertEqual(self.member(acct.id).free_searches_used, 0)

    def test_nothing_is_used_while_billing_is_off(self):
        acct, c = self.user()
        self.billing_off()
        self.run_and_wait(c)
        self.assertEqual(self.member(acct.id).free_searches_used, 0)

    def test_the_run_that_uses_the_last_free_search_still_scores_its_jobs(self):
        acct, c = self.user(free_searches_used=2)
        run, done = self.run_and_wait(c)
        self.assertEqual(done["state"], "done")
        self.score.assert_called_once()
        self.assertEqual(self.member(acct.id).free_searches_used, 3)
        again = c.post("/api/search/run")
        self.assertEqual((again.status_code, again.json()["code"]), (402, "membership_required"))
        self.assertEqual(self.member(acct.id).free_searches_used, 3)

    def test_a_free_search_scores_only_the_jobs_it_found_but_a_members_search_scores_every_pending_one(self):
        acct, c = self.user()
        with mock.patch("backend.pipeline.run_search", return_value={"job_ids": [7, 8]}):
            self.run_and_wait(c)
        self.assertEqual(self.score.call_args.kwargs["only"], {7, 8})
        member, mc = self.user(exempt=True)
        with mock.patch("backend.pipeline.run_search", return_value={"job_ids": [7, 8]}):
            self.run_and_wait(mc)
        self.assertIsNone(self.score.call_args.kwargs["only"])

    def test_select_jobs_can_be_limited_to_some_ids(self):
        from backend.scorer import select_jobs
        with mock.patch("backend.pool.job_states", return_value=[(1, False, None, False, "full"),
                                                                 (2, False, None, False, "full")]), \
                mock.patch("backend.filters.softened_job_ids", return_value=set()):
            db = mock.MagicMock()
            db.query.return_value.join.return_value.filter.return_value = []
            self.assertEqual(select_jobs(db, "pending", "h", 1), [1, 2])
            self.assertEqual(select_jobs(db, "pending", "h", 1, only={2}), [2])

    def test_a_free_search_run_scores_through_the_real_scoring_code_with_no_membership(self):
        """The run's own scoring is not behind the gate: a trial account's run reaches the model for its jobs
        (the model call is the only thing replaced), while rescoring by hand is refused."""
        self.scored.stop()  # the real backend.app._score
        self.addCleanup(self.scored.start)
        acct, c = self.user()
        with mock.patch("backend.scorer.select_jobs", return_value=[4242]), \
                mock.patch("backend.scorer._duplicate_groups", return_value={4242: []}), \
                mock.patch("backend.scorer.score_one", return_value={"score": 77}) as score_one:
            run, done = self.run_and_wait(c)
            self.assertEqual(done["state"], "done", done)
            score_one.assert_called_once()
            self.assertEqual(score_one.call_args.args[0], 4242)
            self.assertEqual(done["summary"]["scoring"]["scored"], 1)
            self.assertFalse(done["summary"]["scoring"].get("aborted"))
            self.assertEqual(c.post("/api/score/run", json={"mode": "pending"}).status_code, 402)
            self.assertEqual(c.post("/api/score/4242").status_code, 402)
            score_one.assert_called_once()  # the refused requests scored nothing

    def test_the_wizards_title_step_works_for_a_trial_account_without_the_ai(self):
        """The sign-up wizard only saves typed titles (PUT /api/profile): a trial account can finish it. Title
        suggestions are refused with the members-only reason, and the wizard never calls them."""
        acct, c = self.user()
        r = c.post("/api/documents/suggest-titles")
        self.assertEqual((r.status_code, r.json()["reason"]), (402, "ai_members_only"))
        profile = c.get("/api/profile")
        self.assertEqual(profile.status_code, 200)
        saved = c.put("/api/profile", json={**profile.json(), "titles": ["Product Manager", "Business Analyst"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(c.get("/api/profile").json()["titles"], ["Product Manager", "Business Analyst"])
        wizard = (Path(__file__).resolve().parent.parent / "frontend" / "welcome.js").read_text(encoding="utf-8")
        for path in ("suggest-titles", "/api/score", "/api/chats", "cover-letter", "companies/research", "/concise",
                     "documents/summary"):
            self.assertNotIn(path, wizard)

    def test_two_runs_cannot_both_take_the_last_free_search(self):
        """The gate said yes to both; the guarded update lets only one through."""
        acct, c = self.user(free_searches_used=2)
        stale = access.AccessState(free_searches_used=0)
        self.set_membership(acct.id, free_searches_used=3)  # the other run took it
        db = SessionLocal()
        try:
            run = SearchRun(workspace_id=acct.workspace_id, kind="search")
            with mock.patch.object(membership, "load_state", return_value=stale), \
                    self.assertRaises(access.MembershipRequired):
                membership.charge_search(db, run)
        finally:
            db.close()
        self.assertEqual(self.member(acct.id).free_searches_used, 3)

    def test_the_run_and_the_charge_commit_together(self):
        acct, c = self.user()

        def charge_then_fail(db, run):
            membership.charge_search(db, run)
            raise RuntimeError("the queue broke")

        with helpers.as_account(acct), self.assertRaises(RuntimeError):
            tasks.start("search", lambda progress: {}, acct.workspace_id, charge=charge_then_fail)
        self.assertEqual(self.member(acct.id).free_searches_used, 0)
        db = SessionLocal()
        try:
            self.assertEqual(db.query(SearchRun.id).filter(SearchRun.workspace_id == acct.workspace_id).count(), 0)
        finally:
            db.close()

    def test_scoring_and_the_rest_dont_use_free_searches(self):
        acct, c = self.user()
        self.subscribe(acct.id)  # the AI features are for members
        for url, body in (("/api/score/run", {"mode": "pending"}), ("/api/companies/research", {})):
            r = c.post(url, json=body)
            self.assertIn(r.status_code, (200, 202), f"{url}: {r.text}")
            self.wait_for_runs(c, r.json()["id"])
        self.assertEqual(self.member(acct.id).free_searches_used, 0)
        for kind in ("score", "research"):
            runs = self.runs(acct.workspace_id, kind)
            self.assertEqual(runs, [(kind, False)])

    def test_a_scheduled_run_of_a_member_is_never_charged(self):
        from backend.app import start_search_run
        acct, c = self.user()
        self.subscribe(acct.id)
        with helpers.as_account(acct):
            run = start_search_run(acct.workspace_id, scheduled=True)
        self.wait_for_runs(c, run["id"])
        self.assertEqual((run["scheduled"], run["free_search"]), (True, False))
        self.assertEqual(self.runs(acct.workspace_id), [("search", False)])
        self.assertEqual(self.member(acct.id).free_searches_used, 0)

    def test_a_run_started_without_a_charge_leaves_the_flag_unset(self):
        acct, c = self.user()
        with helpers.as_account(acct):
            run = tasks.start("search", lambda progress: {}, acct.workspace_id)
        self.wait_for_runs(c, run["id"])
        self.assertFalse(run["free_search"])
        self.assertEqual(self.runs(acct.workspace_id), [("search", False)])
        self.assertEqual(self.member(acct.id).free_searches_used, 0)


class ScheduledSearches(Base):
    def new_user(self, background=True, **fields):
        acct, c = self.user(**fields)
        profile = c.get("/api/profile").json()
        c.put("/api/profile", json={**profile, "titles": ["Product Manager"], "seek_enabled": True})
        if background:
            self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)
        return acct, c

    def tick(self, ws_id, started, now=FRIDAY_1020):
        def start(ws):
            started.append((ws, crypto.current().user_id))
            return {"id": 7000 + len(started)}

        real = schedule.nothing_to_search
        with mock.patch.object(schedule, "nothing_to_search",
                               side_effect=lambda d, w: real(d, w) if w == ws_id else True), \
                mock.patch.object(schedule.tasks, "active_run", return_value=None):
            return schedule.tick(start, now)

    def test_without_a_membership_the_slot_is_passed_over_with_a_reason_the_user_sees(self):
        acct, c = self.new_user(free_searches_used=0)  # free searches are for Run search only
        started = []
        self.assertIsNone(self.tick(acct.workspace_id, started))
        self.assertEqual(started, [])
        info = c.get("/api/schedule").json()
        self.assertEqual(info["skipped"]["reason"], "membership_required")
        self.assertEqual(info["skipped"]["text"], "Scheduled search skipped: scheduled searches need a membership")
        self.assertTrue(info["membership_required"])
        self.set_membership(acct.id, exempt=True)
        self.assertFalse(c.get("/api/schedule").json()["membership_required"])

    SLOT = FRIDAY_1020.replace(tzinfo=None)  # the tick's clock: free periods are judged at this time

    def test_a_member_with_background_access_runs(self):
        acct, c = self.new_user()
        self.grant(acct.id, self.SLOT - timedelta(hours=1), self.SLOT + timedelta(days=1))
        started = []
        self.assertEqual(self.tick(acct.workspace_id, started)["id"], 7001)
        self.assertEqual(started, [(acct.workspace_id, acct.id)])

    def test_a_subscriber_and_an_exempt_account_run_too(self):
        for fields in ({"exempt": True}, {"stripe_status": "active", "current_period_end": utcnow() + timedelta(days=5)}):
            acct, c = self.new_user(**fields)
            started = []
            self.assertIsNotNone(self.tick(acct.workspace_id, started), fields)

    def test_a_member_without_background_access_is_told_to_turn_it_on(self):
        acct, c = self.new_user(background=False, exempt=True)
        started = []
        self.assertIsNone(self.tick(acct.workspace_id, started))
        self.assertEqual(c.get("/api/schedule").json()["skipped"]["reason"], "background_off")

    def test_a_past_due_account_past_its_grace_is_passed_over(self):
        acct, c = self.new_user(stripe_status="past_due", grace_until=self.SLOT - timedelta(hours=1),
                                current_period_end=self.SLOT + timedelta(days=20))
        self.assertIsNone(self.tick(acct.workspace_id, []))
        self.assertEqual(c.get("/api/schedule").json()["skipped"]["reason"], "membership_required")

    def test_with_billing_off_everyone_runs(self):
        self.billing_off()
        acct, c = self.new_user()
        self.assertIsNotNone(self.tick(acct.workspace_id, []))

    def test_the_skip_goes_away_when_a_later_slot_runs(self):
        acct, c = self.new_user()
        self.assertIsNone(self.tick(acct.workspace_id, []))
        self.assertIsNotNone(c.get("/api/schedule").json()["skipped"])
        self.grant(acct.id, self.SLOT, self.SLOT + timedelta(days=1))
        self.assertIsNotNone(self.tick(acct.workspace_id, [], FRIDAY_1020 + timedelta(hours=2)))
        self.assertIsNone(c.get("/api/schedule").json()["skipped"])


class AccountsWithoutAMembershipRow(Base):
    """Accounts from before memberships have no row: they count as new, and get one when it's needed."""

    def drop_row(self, user_id):
        db = SessionLocal()
        try:
            db.query(Membership).filter(Membership.user_id == user_id).delete()
            db.commit()
        finally:
            db.close()

    def test_the_gate_treats_them_as_new_and_a_search_makes_the_row(self):
        acct, c = self.user()
        self.drop_row(acct.id)
        self.assertIsNone(c.get("/api/billing/status").json()["me"]["stripe_status"])
        self.assertEqual(c.post("/api/documents/summary").json()["reason"], "ai_members_only")  # a trial account
        run, _ = self.run_and_wait(c)
        m = self.member(acct.id)
        self.assertEqual(m.free_searches_used, 1)
        self.assertTrue(m.stripe_ref)

    def test_checkout_makes_the_row_with_a_random_reference(self):
        acct, c = self.user()
        self.drop_row(acct.id)
        self.stripe.v1.checkout.sessions.create.return_value = {"id": "cs_1", "url": "https://checkout.stripe.test/x"}
        self.assertEqual(c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"}).status_code, 200)
        ref = self.member(acct.id).stripe_ref
        self.assertGreaterEqual(len(ref), 20)
        self.assertEqual(self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"]["client_reference_id"], ref)


class Prices(Base):
    def test_the_configured_lookup_key_comes_first_and_the_yearly_one_is_optional(self):
        self.assertEqual(billing.plan_keys(), ["jobhunt_monthly", "jobhunt_yearly"])
        with mock.patch.dict(os.environ, {"STRIPE_PRICE_LOOKUP_KEY": "my_price"}):
            self.assertEqual(billing.plan_keys(), ["my_price", "jobhunt_yearly"])
        with mock.patch.dict(os.environ, {"STRIPE_PRICE_LOOKUP_KEY": "jobhunt_yearly"}):
            self.assertEqual(billing.plan_keys(), ["jobhunt_yearly"])
        self.stripe.v1.prices.list.return_value = {"data": [price("jobhunt_yearly", 9000, "year")]}
        self.assertEqual([p["lookup_key"] for p in billing.list_prices()], ["jobhunt_yearly"])
        self.assertTrue(billing.enabled())  # one plan is enough

    def test_prices_are_asked_for_once_and_cached(self):
        billing.list_prices()
        billing.list_prices()
        billing.enabled()
        self.assertEqual(self.stripe.v1.prices.list.call_count, 1)

    def test_a_cache_never_crosses_from_a_test_key_to_a_live_one(self):
        billing.list_prices()
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": LIVE}):
            billing.list_prices()
        self.assertEqual(self.stripe.v1.prices.list.call_count, 2)

    def test_no_key_means_off_without_asking_stripe(self):
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""}):
            self.assertFalse(billing.enabled())
            with self.assertRaises(billing.BillingError):
                billing.list_prices()
        self.stripe.v1.prices.list.assert_not_called()

    def test_an_unreachable_stripe_is_remembered_for_a_minute_not_asked_per_request(self):
        self.stripe.v1.prices.list.side_effect = RuntimeError("down")
        self.assertTrue(billing.enabled())
        self.assertTrue(billing.enabled())
        self.assertEqual(self.stripe.v1.prices.list.call_count, 1)

    def test_a_known_no_price_survives_a_later_outage(self):
        self.stripe.v1.prices.list.return_value = {"data": []}
        self.assertFalse(billing.enabled())
        with mock.patch.object(billing.time, "monotonic", return_value=time.monotonic() + billing.PRICE_CACHE_SECONDS + 1):
            self.stripe.v1.prices.list.side_effect = RuntimeError("down")
            self.assertFalse(billing.enabled())  # the last answer stands


class Checkout(Base):
    def test_checkout_creates_a_session_for_this_person(self):
        acct, c = self.user()
        self.stripe.v1.checkout.sessions.create.return_value = {"id": "cs_test_abc123", "url": "https://checkout.stripe.test/x"}
        r = c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"})
        self.assertEqual((r.status_code, r.json()), (200, {"url": "https://checkout.stripe.test/x"}))
        params = self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"]
        ref = self.member(acct.id).stripe_ref
        self.assertEqual((params["client_reference_id"], params["metadata"], params["subscription_data"]),
                         (ref, {"jh_ref": ref}, {"metadata": {"jh_ref": ref}}))
        self.assertEqual(params["line_items"], [{"price": "price_month", "quantity": 1}])
        self.assertEqual(params["customer_email"], acct.email)
        self.assertNotIn("customer", params)
        self.assertTrue(params["success_url"].endswith("/billing.html?session_id={CHECKOUT_SESSION_ID}"))
        self.assertTrue(params["cancel_url"].endswith("/billing.html?cancelled=1"))

    def test_the_urls_use_the_public_address(self):
        acct, c = self.user()
        self.stripe.v1.checkout.sessions.create.return_value = {"id": "cs_1", "url": "https://checkout.stripe.test/x"}
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://jobs.example.com"}):
            self.assertEqual(c.post("/api/billing/checkout", json={"plan": "jobhunt_yearly"}).status_code, 200)
        params = self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"]
        self.assertEqual(params["success_url"], "https://jobs.example.com/billing.html?session_id={CHECKOUT_SESSION_ID}")
        self.assertEqual(params["cancel_url"], "https://jobs.example.com/billing.html?cancelled=1")
        self.assertEqual(params["line_items"][0]["price"], "price_year")

    def test_a_known_customer_is_reused_instead_of_the_email(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.checkout.sessions.retrieve.return_value = {
            "id": "cs_test_abc123", "client_reference_id": ref, "status": "complete",
            "subscription": stripe_sub(ref, "canceled")}
        self.assertEqual(c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"}).status_code, 200)
        self.stripe.v1.checkout.sessions.create.return_value = {"id": "cs_2", "url": "https://checkout.stripe.test/y"}
        self.assertEqual(c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"}).status_code, 200)
        params = self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"]
        self.assertEqual(params["customer"], "cus_123")
        self.assertNotIn("customer_email", params)

    def test_refusals(self):
        acct, c = self.user()
        self.assertEqual(c.post("/api/billing/checkout", json={"plan": "nope"}).status_code, 422)
        self.assertEqual(c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly", "x": 1}).status_code, 422)
        self.assertEqual(c.post("/api/billing/checkout", json={}).status_code, 422)
        self.stripe.v1.checkout.sessions.create.return_value = {"id": "cs_1", "url": "u://x"}
        self.subscribe(acct.id)
        self.stripe.v1.subscriptions.search.return_value = {"data": [stripe_sub(self.member(acct.id).stripe_ref)]}
        r = c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "already_subscribed"))
        self.billing_off()
        r = c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "billing_off"))

    def test_stripe_errors_answer_502_with_a_safe_message(self):
        acct, c = self.user()
        self.stripe.v1.checkout.sessions.create.side_effect = RuntimeError(f"bad {acct.email} cus_123 {KEY}")
        r = c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"})
        self.assertEqual((r.status_code, r.json()["code"]), (502, "stripe_failed"))
        for secret in (acct.email, "cus_123", KEY):
            self.assertNotIn(secret, r.text)

    def test_it_needs_a_signed_in_user_and_the_csrf_token(self):
        anon = TestClient(app)
        self.assertEqual(anon.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"}).status_code, 401)
        acct, c = self.user()
        r = c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"}, headers={"X-CSRF-Token": "wrong"})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "csrf"))

    def test_the_calls_are_rate_limited(self):
        acct, c = self.user()
        self.stripe.v1.checkout.sessions.create.return_value = {"id": "cs_1", "url": "https://checkout.stripe.test/x"}
        codes = [c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"}).status_code for _ in range(31)]
        self.assertEqual(codes[:30], [200] * 30)
        self.assertEqual(codes[30], 429)


def missing(what="customer"):
    return stripe.InvalidRequestError(f"No such {what}: 'cus_123'", what, code="resource_missing")


def person_member(db, user_id) -> bool:
    return membership.person_state(db, user_id, "user")["member"]


class StaleStripeState(Base):
    """What a membership row holds from a Stripe account or mode the app no longer talks to (a test key swapped
    for a live one), and a subscription that has ended while its row still says so."""

    def paying(self, **fields):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.checkout.sessions.retrieve.return_value = {
            "id": "cs_test_abc123", "client_reference_id": ref, "status": "complete", "subscription": stripe_sub(ref)}
        self.assertEqual(c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"}).status_code, 200)
        if fields:
            self.set_membership(acct.id, **fields)
        membership._tried.clear()
        return acct, c, ref

    def sync(self, acct):
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                return membership.sync_user(db, acct.id, force=True)
            finally:
                db.close()

    def old_period(self, **extra):
        return {"current_period_end": utcnow() - timedelta(days=10), **extra}

    def test_a_customer_the_stripe_account_doesnt_know_is_dropped_and_checkout_works(self):
        acct, c, ref = self.paying(**self.old_period(free_searches_used=1))
        self.stripe.v1.subscriptions.list.side_effect = missing()
        self.stripe.v1.subscriptions.search.return_value = {"data": []}
        self.stripe.v1.checkout.sessions.create.return_value = {"id": "cs_2", "url": "https://checkout.stripe.test/y"}
        r = c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"})
        self.assertEqual((r.status_code, r.json()), (200, {"url": "https://checkout.stripe.test/y"}))
        m = self.member(acct.id)
        self.assertEqual((m.stripe_status, m.plan, m.current_period_end, m.grace_until, m.billing_self, m.billing_admin),
                         (None, None, None, None, None, None))
        self.assertEqual(m.free_searches_used, 1)  # what isn't Stripe's stays
        self.assertNotIn("customer", self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"])

    def test_the_gate_stops_trusting_it_the_same_way(self):
        acct, c, ref = self.paying(**self.old_period(free_searches_used=3, current_period_end=utcnow() - timedelta(days=1),
                                                 stripe_synced_at=utcnow() - timedelta(hours=2)))
        self.stripe.v1.subscriptions.list.side_effect = missing()
        self.stripe.v1.subscriptions.search.return_value = {"data": []}
        r = c.post("/api/score/run", json={"mode": "pending"})
        self.assertEqual((r.status_code, r.json()["code"]), (402, "membership_required"))
        self.assertIsNone(self.member(acct.id).stripe_status)

    def test_a_test_mode_subscription_gives_no_access_under_a_live_key(self):
        """The yearly plan paid with Stripe's test card, then the operator saves a live key: the row says active for a
        year, and nothing would ask Stripe before then."""
        acct, c, ref = self.paying(current_period_end=utcnow() + timedelta(days=360), stripe_synced_at=utcnow())
        self.assertEqual(self.member(acct.id).stripe_mode, "test")
        self.assertEqual(c.post("/api/score/run", json={"mode": "pending"}).status_code, 200)  # (a member under the test key)
        self.stripe.v1.subscriptions.list.side_effect = missing()
        self.stripe.v1.subscriptions.search.return_value = {"data": []}
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": LIVE}):
            billing.clear_price_cache()
            r = c.post("/api/score/run", json={"mode": "pending"})
            self.assertEqual((r.status_code, r.json()["code"]), (402, "membership_required"))
            self.assertIsNone(self.member(acct.id).stripe_status, "Stripe doesn't know the customer: forgotten")
            self.assertIsNone(self.member(acct.id).stripe_mode)
            me = c.get("/api/billing/status").json()["me"]
            self.assertEqual((me["member"], me["stripe_status"], me["plan"]), (False, None, None))

    def test_a_test_mode_row_is_left_out_even_before_stripe_has_been_asked(self):
        acct, c, ref = self.paying(current_period_end=utcnow() + timedelta(days=360), stripe_synced_at=utcnow())
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": LIVE}):
            with SessionLocal() as db:
                state = membership.load_state(db, acct.id, "user")
                self.assertIsNone(state.stripe_status)
                self.assertIsNotNone(membership.scheduled_block(db, acct.id), "the scheduler can't run it either")
                self.assertFalse(person_member(db, acct.id))

    def test_a_subscription_the_live_account_does_have_is_kept_and_marked_live(self):
        acct, c, ref = self.paying()
        self.set_membership(acct.id, stripe_mode=None)  # a row from before the mode was stored
        membership._tried.clear()
        self.stripe.v1.subscriptions.list.return_value = {"data": [stripe_sub(ref)]}
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": LIVE}):
            r = c.post("/api/score/run", json={"mode": "pending"})
            self.assertEqual(r.status_code, 200, r.text)
            m = self.member(acct.id)
            self.assertEqual((m.stripe_status, m.stripe_mode), ("active", "live"))

    def test_a_customer_with_no_subscriptions_at_all_is_dropped_too(self):
        acct, c, ref = self.paying()
        self.stripe.v1.subscriptions.list.return_value = {"data": []}
        self.stripe.v1.subscriptions.search.return_value = {"data": []}
        self.assertTrue(self.sync(acct))  # the row changed
        self.assertIsNone(self.member(acct.id).stripe_status)

    def test_a_failed_check_keeps_what_was_stored(self):
        acct, c, ref = self.paying()
        self.stripe.v1.subscriptions.list.side_effect = RuntimeError("Stripe is down")
        self.assertFalse(self.sync(acct))
        self.assertEqual(self.member(acct.id).stripe_status, "active")

    def test_a_status_stripe_cant_confirm_doesnt_lock_the_person_out_once_its_period_is_over(self):
        acct, c, ref = self.paying(**self.old_period(current_period_end=utcnow() - timedelta(days=10)))
        self.stripe.v1.subscriptions.list.side_effect = RuntimeError("Stripe is down")
        self.stripe.v1.checkout.sessions.create.return_value = {"id": "cs_2", "url": "https://checkout.stripe.test/y"}
        self.assertEqual(c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"}).status_code, 200)

    def test_a_status_stripe_cant_confirm_still_refuses_while_the_period_runs(self):
        acct, c, ref = self.paying()
        self.stripe.v1.subscriptions.list.side_effect = RuntimeError("Stripe is down")
        r = c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "already_subscribed"))

    def test_past_due_beyond_the_grace_points_at_the_card_not_at_the_plans(self):
        past = utcnow() - timedelta(days=5)
        acct, c, ref = self.paying(stripe_status="past_due", grace_until=utcnow() - timedelta(days=2),
                                   current_period_end=past, stripe_synced_at=utcnow())
        me = c.get("/api/billing/status").json()["me"]
        self.assertEqual((me["member"], me["stripe_status"], me["can_subscribe"], me["can_manage"]),
                         (False, "past_due", False, True))
        late = stripe_sub(ref, "past_due")
        late["items"]["data"][0]["current_period_start"] = int(past.replace(tzinfo=timezone.utc).timestamp())
        self.stripe.v1.subscriptions.list.return_value = {"data": [late]}
        r = c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "already_subscribed"))
        self.assertIn("payment method", r.json()["detail"])
        self.stripe.v1.checkout.sessions.create.assert_not_called()


class Verify(Base):
    def session(self, ref, **kw):
        return {"id": "cs_test_abc123", "client_reference_id": ref, "status": "complete", "mode": "subscription",
                "subscription": stripe_sub(ref), **kw}

    def test_a_good_return_makes_a_member_and_stores_the_details_sealed(self):
        acct, c = self.user(free_searches_used=3)
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.checkout.sessions.retrieve.return_value = self.session(ref)
        r = c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
        self.assertEqual(r.status_code, 200, r.text)
        me = r.json()["me"]
        self.assertEqual((me["member"], me["reason"], me["plan"], me["stripe_status"], me["can_manage"]),
                         (True, "subscribed", "jobhunt_monthly", "active", True))
        m = self.member(acct.id)
        self.assertEqual((m.stripe_status, m.plan, m.cancel_at_period_end), ("active", "jobhunt_monthly", False))
        self.assertAlmostEqual(m.current_period_end.timestamp(), (utcnow() + timedelta(days=29)).timestamp(), delta=60)
        self.assertIsNotNone(m.stripe_synced_at)
        self.assertEqual(c.post("/api/documents/summary").status_code, 400)  # allowed now: nothing ticked to summarise
        # the customer is marked with the reference, so it can be found again
        self.stripe.v1.customers.update.assert_called_once_with("cus_123", params={"metadata": {"jh_ref": ref}})
        args, kwargs = self.stripe.v1.checkout.sessions.retrieve.call_args
        self.assertEqual(args, ("cs_test_abc123",))
        self.assertIn("subscription.default_payment_method", kwargs["params"]["expand"])

    def test_billing_self_and_billing_admin_are_sealed(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.checkout.sessions.retrieve.return_value = self.session(ref)
        c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
        m = self.member(acct.id)
        for blob in (m.billing_self, m.billing_admin):
            self.assertIsNotNone(blob)
            for needle in (b"cus_123", b"sub_123", b"4242", b"visa", b"aud", b"900"):
                self.assertNotIn(needle, bytes(blob))
        self.assertEqual(self.sealed_ids(acct), {"customer": "cus_123", "subscription": "sub_123"})
        # the other person's key doesn't open it, and the admin key opens the other one
        other = helpers.make_user()
        with helpers.as_account(other), self.assertRaises(crypto.CryptoError):
            crypto.unseal(m.billing_self, f"memberships.billing_self|{acct.id}")
        with helpers.client() as admin:
            row = next(u for u in admin.get("/api/admin/users").json()["users"] if u["id"] == acct.id)
        self.assertEqual(row["membership"]["billing"], {"card_brand": "visa", "card_last4": "4242",
                                                        "plan": "jobhunt_monthly", "amount": 900, "currency": "aud",
                                                        "interval": "month"})
        self.assertNotIn("cus_123", json.dumps(row))
        self.assertNotIn("sub_123", json.dumps(row))

    def test_someone_elses_session_is_refused_and_changes_nothing(self):
        alice, ca = self.user()
        bob, cb = self.user()
        self.stripe.v1.checkout.sessions.retrieve.return_value = self.session(self.member(bob.id).stripe_ref)
        r = ca.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "session_mismatch"))
        self.assertIsNone(self.member(alice.id).stripe_status)
        self.assertIsNone(self.member(alice.id).billing_self)
        self.assertIsNone(self.member(bob.id).stripe_status)
        self.stripe.v1.customers.update.assert_not_called()

    def test_a_session_with_no_reference_or_a_subscription_of_another_is_refused(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        other = self.session(ref)
        other["client_reference_id"] = None
        self.stripe.v1.checkout.sessions.retrieve.return_value = other
        self.assertEqual(c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"}).status_code, 403)
        crossed = self.session(ref)
        crossed["subscription"] = stripe_sub("someone-elses-reference")
        self.stripe.v1.checkout.sessions.retrieve.return_value = crossed
        self.assertEqual(c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"}).status_code, 403)
        self.assertIsNone(self.member(acct.id).stripe_status)

    def test_an_unfinished_checkout_isnt_applied(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.checkout.sessions.retrieve.return_value = self.session(ref, status="open", subscription=None)
        r = c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "not_complete"))
        self.assertIsNone(self.member(acct.id).stripe_status)

    def test_bad_session_ids_never_reach_stripe(self):
        acct, c = self.user()
        for bad in ("", "x", "cs_", "cs_test/../../customers", "cs_test_abc 123", "sub_123", "cs_" + "a" * 300):
            self.assertEqual(c.post("/api/billing/verify", json={"session_id": bad}).status_code, 422, bad)
        self.stripe.v1.checkout.sessions.retrieve.assert_not_called()

    def test_repeating_it_changes_nothing(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.checkout.sessions.retrieve.return_value = self.session(ref)
        first = c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"}).json()
        again = c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"}).json()
        self.assertEqual(first["me"], again["me"])

    def test_the_status_page_says_where_things_stand(self):
        acct, c = self.user()
        s = c.get("/api/billing/status").json()
        self.assertEqual(s["billing"]["enabled"], True)
        self.assertEqual(s["billing"]["test_mode"], True)
        self.assertEqual(s["billing"]["prices"], [
            {"plan": "jobhunt_monthly", "interval": "month", "amount": 900, "currency": "aud"},
            {"plan": "jobhunt_yearly", "interval": "year", "amount": 9000, "currency": "aud"}])
        me = s["me"]
        self.assertEqual((me["member"], me["reason"], me["free_searches_left"], me["free_searches_limit"],
                          me["can_search"], me["can_subscribe"], me["can_manage"]),
                         (False, None, 3, 3, True, True, False))
        self.set_membership(acct.id, free_searches_used=3)
        me = c.get("/api/billing/status").json()["me"]
        self.assertEqual((me["free_searches_left"], me["can_search"]), (0, False))
        self.assertIn("free searches", me["message"])
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": LIVE}):
            billing.clear_price_cache()
            self.assertFalse(c.get("/api/billing/status").json()["billing"]["test_mode"])

    def test_the_status_of_a_subscriber_and_of_someone_in_a_free_period(self):
        acct, c = self.user()
        self.subscribe(acct.id, "active")
        self.set_membership(acct.id, cancel_at_period_end=True)
        self.stripe.v1.subscriptions.search.return_value = {"data": []}
        me = c.get("/api/billing/status").json()["me"]
        self.assertEqual((me["member"], me["reason"], me["cancel_at_period_end"], me["can_subscribe"]),
                         (True, "subscribed", True, False))
        self.assertTrue(me["current_period_end"].endswith("Z"))
        other, c2 = self.user()
        self.grant(other.id)
        me = c2.get("/api/billing/status").json()["me"]
        self.assertEqual((me["member"], me["reason"]), (True, "grant"))
        self.assertTrue(me["free_period_until"].endswith("Z"))

    def test_billing_off_status(self):
        acct, c = self.user()
        self.billing_off()
        s = c.get("/api/billing/status").json()
        self.assertEqual((s["billing"]["enabled"], s["billing"]["prices"], s["me"]["member"], s["me"]["reason"]),
                         (False, [], True, "billing_off"))


class UnknownCheckout(Base):
    def test_a_checkout_stripe_doesnt_know_is_a_404_not_an_outage(self):
        acct, c = self.user()
        self.stripe.v1.checkout.sessions.retrieve.side_effect = missing("checkout.session")
        r = c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
        self.assertEqual((r.status_code, r.json()["code"]), (404, "no_session"))
        self.assertNotIn("cs_test_abc123", r.text)

    def test_any_other_stripe_failure_is_still_a_502(self):
        acct, c = self.user()
        for exc in (stripe.InvalidRequestError("bad", "x", code="parameter_invalid_empty"), RuntimeError("down")):
            self.stripe.v1.checkout.sessions.retrieve.side_effect = exc
            r = c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
            self.assertEqual((r.status_code, r.json()["code"]), (502, "stripe_failed"))


class Portal(Base):
    def test_the_portal_opens_for_the_stored_customer(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.checkout.sessions.retrieve.return_value = {
            "id": "cs_test_abc123", "client_reference_id": ref, "status": "complete", "subscription": stripe_sub(ref)}
        c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
        self.stripe.v1.billing_portal.sessions.create.return_value = {"url": "https://portal.stripe.test/p"}
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://jobs.example.com"}):
            r = c.post("/api/billing/portal")
        self.assertEqual((r.status_code, r.json()), (200, {"url": "https://portal.stripe.test/p"}))
        self.assertEqual(self.stripe.v1.billing_portal.sessions.create.call_args.kwargs["params"],
                         {"customer": "cus_123", "return_url": "https://jobs.example.com/billing.html"})
        self.stripe.v1.customers.search.assert_not_called()

    def test_a_customer_lost_in_a_wipe_is_found_again_by_the_reference(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.customers.search.return_value = {"data": [{"id": "cus_456"}]}
        self.stripe.v1.billing_portal.sessions.create.return_value = {"url": "https://portal.stripe.test/p"}
        r = c.post("/api/billing/portal")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.stripe.v1.customers.search.call_args.kwargs["params"]["query"],
                         f"metadata['jh_ref']:'{ref}'")
        self.assertEqual(self.stripe.v1.billing_portal.sessions.create.call_args.kwargs["params"]["customer"],
                         "cus_456")
        self.assertEqual(self.sealed_ids(acct), {"customer": "cus_456"})  # remembered, sealed

    def test_the_portal_needs_the_password_entered_recently(self):
        """A stolen session cookie must not open the portal: it shows the billing address and card and can cancel."""
        from backend.db import AuthSession
        acct, c = self.user()
        self.stripe.v1.customers.search.return_value = {"data": [{"id": "cus_456"}]}
        self.stripe.v1.billing_portal.sessions.create.return_value = {"url": "https://portal.stripe.test/p"}
        db = SessionLocal()
        try:
            db.query(AuthSession).filter(AuthSession.user_id == acct.id).update(
                {AuthSession.reauth_at: utcnow() - timedelta(minutes=20)})
            db.commit()
        finally:
            db.close()
        r = c.post("/api/billing/portal")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
        self.stripe.v1.billing_portal.sessions.create.assert_not_called()
        self.stripe.v1.customers.search.assert_not_called()
        self.assertEqual(c.post("/api/auth/reauth", json={"password": helpers.USER_PASSWORD}).status_code, 200)
        self.assertEqual(c.post("/api/billing/portal").status_code, 200)

    def test_no_customer_at_all(self):
        acct, c = self.user()
        self.stripe.v1.customers.search.return_value = {"data": []}
        self.stripe.v1.subscriptions.search.return_value = {"data": []}
        r = c.post("/api/billing/portal")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "no_customer"))

    def test_stripe_failing_is_a_502(self):
        acct, c = self.user()
        self.stripe.v1.customers.search.side_effect = RuntimeError("down")
        self.assertEqual(c.post("/api/billing/portal").status_code, 502)


class Webhook(Base):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": WHSEC})
        env.start()
        self.addCleanup(env.stop)
        self.anon = TestClient(app)  # Stripe has no session and sends no CSRF token

    def post(self, payload, sig=None, **kw):
        return self.anon.post("/api/billing/webhook", content=payload,
                              headers={"Stripe-Signature": sig if sig is not None else signed(payload),
                                       "Content-Type": "application/json"}, **kw)

    def test_the_route_is_a_404_until_a_secret_is_set(self):
        payload = event("invoice.paid", {"subscription": "sub_123"})
        with mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": ""}):
            r = self.post(payload)
            self.assertEqual((r.status_code, r.json()), (404, {"detail": "Not Found"}))
            r = self.post(payload, sig="garbage")
            self.assertEqual(r.status_code, 404)
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    def test_a_bad_or_missing_signature_is_refused_and_applies_nothing(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(ref)
        payload = event("customer.subscription.updated", stripe_sub(ref))
        for sig in (signed(payload, "whsec_wrongwrongwrongwrong"), "garbage", "", signed(payload, when=time.time() - 3600)):
            r = self.post(payload, sig=sig)
            self.assertEqual((r.status_code, r.json()["code"]), (400, "bad_signature"), sig)
        r = self.anon.post("/api/billing/webhook", content=payload)  # no header
        self.assertEqual(r.status_code, 400)
        r = self.post(payload + b" ", sig=signed(payload))  # tampered
        self.assertEqual(r.status_code, 400)
        self.stripe.v1.subscriptions.retrieve.assert_not_called()
        self.assertIsNone(self.member(acct.id).stripe_status)

    def test_a_late_event_for_an_ended_subscription_doesnt_undo_the_new_one(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        old = stripe_sub(ref, "canceled", id="sub_old", created=int(time.time()) - 90 * DAY)
        new = stripe_sub(ref, "active", id="sub_new")
        self.set_membership(acct.id, stripe_status="active", plan="jobhunt_monthly",
                            current_period_end=utcnow() + timedelta(days=29))
        self.stripe.v1.subscriptions.retrieve.return_value = old
        self.stripe.v1.subscriptions.list.return_value = {"data": [old, new]}
        r = self.post(event("customer.subscription.deleted", old))
        self.assertEqual((r.status_code, r.json()["outcome"]), (200, "applied"))
        self.assertEqual(self.member(acct.id).stripe_status, "active")
        self.assertEqual(self.stripe.v1.subscriptions.list.call_args.kwargs["params"]["customer"], "cus_123")

    def test_an_ended_subscription_is_applied_when_it_is_the_only_one(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        old = stripe_sub(ref, "canceled")
        self.stripe.v1.subscriptions.retrieve.return_value = old
        self.stripe.v1.subscriptions.list.return_value = {"data": [old]}
        self.post(event("customer.subscription.deleted", old))
        self.assertEqual(self.member(acct.id).stripe_status, "canceled")

    def test_a_live_event_costs_no_extra_call(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(ref)
        self.post(event("customer.subscription.updated", stripe_sub(ref)))
        self.stripe.v1.subscriptions.list.assert_not_called()

    def test_a_good_event_is_applied_by_fetching_the_subscription(self):
        acct, c = self.user(free_searches_used=3)
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(ref)
        # the event's own copy says canceled; the fetched state is what counts
        payload = event("customer.subscription.updated", stripe_sub(ref, "canceled"))
        r = self.post(payload)
        self.assertEqual((r.status_code, r.json()), (200, {"received": True, "outcome": "applied"}))
        self.assertEqual(self.stripe.v1.subscriptions.retrieve.call_args.args, ("sub_123",))
        m = self.member(acct.id)
        self.assertEqual((m.stripe_status, m.plan), ("active", "jobhunt_monthly"))
        self.assertNotIn(b"cus_123", bytes(m.billing_admin))
        self.assertNotEqual(c.post("/api/documents/summary").status_code, 402)

    def test_a_replay_is_idempotent(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(ref, "past_due")
        payload = event("invoice.payment_failed", {"subscription": "sub_123"})
        self.assertEqual(self.post(payload).status_code, 200)
        first = self.member(acct.id)
        self.assertEqual(self.post(payload).status_code, 200)
        second = self.member(acct.id)
        self.assertEqual((first.stripe_status, first.grace_until, first.current_period_end),
                         (second.stripe_status, second.grace_until, second.current_period_end))
        self.assertEqual(second.stripe_status, "past_due")
        self.assertIsNotNone(second.grace_until)

    def test_every_event_type_in_the_design_is_handled(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(ref)
        objects = {
            "checkout.session.completed": {"subscription": "sub_123", "client_reference_id": ref},
            "customer.subscription.created": {"id": "sub_123"},
            "customer.subscription.updated": {"id": "sub_123"},
            "customer.subscription.deleted": {"id": "sub_123"},
            "invoice.paid": {"subscription": "sub_123"},
            "invoice.payment_failed": {"parent": {"subscription_details": {"subscription": "sub_123"}}},
        }
        for kind, obj in objects.items():
            self.set_membership(acct.id, stripe_status=None)
            r = self.post(event(kind, obj))
            self.assertEqual((r.status_code, r.json()["outcome"]), (200, "applied"), kind)
            self.assertEqual(self.member(acct.id).stripe_status, "active", kind)

    def test_other_events_and_other_peoples_subscriptions_are_acknowledged_and_ignored(self):
        acct, c = self.user()
        r = self.post(event("customer.created", {"id": "cus_9"}))
        self.assertEqual((r.status_code, r.json()["outcome"]), (200, "ignored"))
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub("nobody-has-this-reference")
        r = self.post(event("customer.subscription.updated", {"id": "sub_999"}))
        self.assertEqual((r.status_code, r.json()["outcome"]), (200, "unmatched"))
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(None)
        r = self.post(event("customer.subscription.updated", {"id": "sub_999"}))
        self.assertEqual(r.json()["outcome"], "unmatched")
        self.assertIsNone(self.member(acct.id).stripe_status)

    def test_a_subscription_for_one_member_is_never_applied_to_another(self):
        alice, ca = self.user()
        bob, cb = self.user()
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(self.member(bob.id).stripe_ref)
        # a checkout event naming alice, for a subscription that carries bob's reference: bob's reference wins
        payload = event("checkout.session.completed", {"subscription": "sub_123",
                                                       "client_reference_id": self.member(alice.id).stripe_ref})
        self.assertEqual(self.post(payload).status_code, 200)
        self.assertIsNone(self.member(alice.id).stripe_status)
        self.assertEqual(self.member(bob.id).stripe_status, "active")

    def test_stripe_down_means_503_so_stripe_retries(self):
        self.stripe.v1.subscriptions.retrieve.side_effect = RuntimeError("down")
        r = self.post(event("invoice.paid", {"subscription": "sub_123"}))
        self.assertEqual((r.status_code, r.json()["code"]), (503, "stripe_failed"))

    def test_a_huge_body_is_refused_unread(self):
        r = self.post(b"x" * (membership.MAX_WEBHOOK_BYTES + 1), sig="t=1,v1=00")
        self.assertEqual(r.status_code, 413)

    def test_it_needs_no_session_and_no_csrf_token_and_runs_with_nobodys_key(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        before = self.member(acct.id).billing_self
        seen = []
        real = membership.apply_update

        def spy(db, m, upd, now=None):
            seen.append(crypto.has_scope())
            return real(db, m, upd, now)

        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(ref)
        with mock.patch.object(membership, "apply_update", spy):
            # even a request that carries a valid session cookie is treated as Stripe's: no key scope, no CSRF
            r = c.post("/api/billing/webhook", content=event("invoice.paid", {"subscription": "sub_123"}),
                       headers={"Stripe-Signature": signed(event("invoice.paid", {"subscription": "sub_123"})),
                                "X-CSRF-Token": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, [False])
        self.assertEqual(self.member(acct.id).billing_self, before)  # sealed under their key: untouched here

    def test_a_cross_site_browser_post_is_still_refused(self):
        payload = event("invoice.paid", {"subscription": "sub_123"})
        r = self.anon.post("/api/billing/webhook", content=payload,
                           headers={"Stripe-Signature": signed(payload), "Origin": "https://evil.example"})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "bad_origin"))


class Sync(Base):
    class Page:
        def __init__(self, subs):
            self.subs = subs

        def auto_paging_iter(self):
            return iter(self.subs)

    def test_the_six_hourly_sync_updates_every_member_by_reference(self):
        a, _ = self.user()
        b, _ = self.user()
        ref_a, ref_b = self.member(a.id).stripe_ref, self.member(b.id).stripe_ref
        self.stripe.v1.subscriptions.list.return_value = self.Page([
            stripe_sub(ref_a, "active", id="sub_a"),
            stripe_sub(ref_b, "canceled", id="sub_b_old", created=TS - 90 * DAY),
            stripe_sub(ref_b, "past_due", id="sub_b"),
            stripe_sub(None, "active", id="sub_stranger"),
            stripe_sub("not-a-member", "active", id="sub_x"),
        ])
        self.assertEqual(membership.sync_tick(), 3)
        self.assertEqual(self.stripe.v1.subscriptions.list.call_args.kwargs["params"]["status"], "all")
        self.assertEqual(self.member(a.id).stripe_status, "active")
        self.assertEqual(self.member(b.id).stripe_status, "past_due")  # the live one beats the canceled one
        self.assertIsNotNone(self.member(b.id).grace_until)
        self.assertIsNotNone(self.member(a.id).billing_admin)

    def test_one_failing_member_does_not_stop_the_others(self):
        a, _ = self.user()
        ref = self.member(a.id).stripe_ref
        self.stripe.v1.subscriptions.list.return_value = self.Page([stripe_sub("x" * 12), stripe_sub(ref)])
        real = membership.apply_update
        calls = []

        def flaky(db, m, upd, now=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("one bad row")
            return real(db, m, upd, now)

        # the first subscription isn't a member's, so apply_update is only reached for the second: make it flaky too
        with mock.patch.object(membership, "apply_update", flaky):
            membership.sync_tick()
        self.assertEqual(len(calls), 1)

    def test_nothing_happens_without_a_key(self):
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""}):
            self.assertEqual(membership.sync_tick(), 0)
        self.stripe.v1.subscriptions.list.assert_not_called()

    def test_stripe_failing_is_survived(self):
        self.stripe.v1.subscriptions.list.side_effect = RuntimeError("down")
        self.assertEqual(membership.sync_tick(), 0)

    def test_the_scheduler_runs_the_sync_every_six_hours(self):
        self.stripe.v1.subscriptions.list.return_value = self.Page([])
        schedule._billing_synced[0] = None
        t0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        self.addCleanup(schedule._billing_synced.__setitem__, 0, None)
        self.assertEqual(schedule.billing_sync_tick(t0), 0)                                  # at start-up
        self.assertIsNone(schedule.billing_sync_tick(t0 + timedelta(hours=5, minutes=59)))   # not due
        self.assertEqual(schedule.billing_sync_tick(t0 + timedelta(hours=6)), 0)             # due
        self.assertEqual(self.stripe.v1.subscriptions.list.call_count, 2)

    def test_the_scheduler_thread_is_off_with_the_scheduler(self):
        with mock.patch.dict(os.environ, {"JOBHUNT_SCHEDULER": "off"}), \
                mock.patch.object(schedule.threading, "Thread") as thread:
            schedule.start_scheduler(lambda ws: {})
        thread.assert_not_called()


class AdminSide(Base):
    def setUp(self):
        super().setUp()
        self.admin = helpers.client()
        self.admin.__enter__()
        self.addCleanup(self.admin.__exit__, None, None, None)

    def subscriber(self):
        """An account that pays (verified through the real endpoint, so every sealed value is as in production)."""
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.checkout.sessions.retrieve.return_value = {
            "id": "cs_test_abc123", "client_reference_id": ref, "status": "complete", "subscription": stripe_sub(ref)}
        self.assertEqual(c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"}).status_code, 200)
        self.stripe.reset_mock()
        self.stripe.v1.prices.list.return_value = {"data": [price("jobhunt_monthly", 900, "month")]}
        return acct, c, ref

    # -- Stripe settings --------------------------------------------------------------------------------------
    def test_the_settings_say_whether_things_are_set_never_what(self):
        r = self.admin.get("/api/admin/stripe")
        self.assertEqual(r.status_code, 200)
        s = r.json()
        self.assertEqual((s["configured"], s["mode"], s["test_mode"], s["enabled"]), (True, "test", True, True))
        self.assertEqual(s["secret_key"], {"set": True, "mode": "test"})
        self.assertEqual(s["webhook"]["enabled"], False)
        self.assertTrue(s["webhook"]["url"].endswith("/api/billing/webhook"))
        self.assertEqual([(p["plan"], p["found"], p["amount"]) for p in s["prices"]],
                         [("jobhunt_monthly", True, 900), ("jobhunt_yearly", True, 9000)])
        self.assertEqual(s["setup"], [])
        for secret in (KEY, WHSEC, KEY[-6:]):
            self.assertNotIn(secret, r.text)
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": LIVE, "STRIPE_WEBHOOK_SECRET": WHSEC}):
            billing.clear_price_cache()
            r = self.admin.get("/api/admin/stripe")
            self.assertEqual((r.json()["mode"], r.json()["test_mode"], r.json()["webhook"]["enabled"]),
                             ("live", False, True))
            for secret in (LIVE, WHSEC):
                self.assertNotIn(secret, r.text)

    def test_with_no_key_or_no_price_it_shows_setup_instructions(self):
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""}):
            s = self.admin.get("/api/admin/stripe").json()
        self.assertEqual((s["configured"], s["mode"], s["enabled"], s["prices"]), (False, None, False, []))
        self.assertTrue(any("jobhunt_monthly" in step for step in s["setup"]))
        self.stripe.v1.prices.list.return_value = {"data": []}
        s = self.admin.get("/api/admin/stripe?refresh=true").json()
        self.assertEqual((s["configured"], s["enabled"]), (True, False))
        self.assertIn("jobhunt_monthly", s["problem"])
        self.assertEqual([p["found"] for p in s["prices"]], [False, False])
        self.assertTrue(s["setup"])

    def test_stripe_being_unreachable_is_said_without_details(self):
        self.stripe.v1.prices.list.side_effect = RuntimeError(f"no route to {KEY}")
        s = self.admin.get("/api/admin/stripe?refresh=true").json()
        self.assertIn("Stripe couldn't", s["problem"])
        self.assertNotIn(KEY, json.dumps(s))

    def test_saving_settings_writes_through_app_settings_and_needs_a_recent_password(self):
        saved = {}
        with mock.patch("backend.app_settings._save", side_effect=lambda u: saved.update(u)):
            r = self.admin.put("/api/admin/stripe", json={"secret_key": LIVE, "price_lookup_key": "my_price"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(saved, {"STRIPE_SECRET_KEY": LIVE, "STRIPE_PRICE_LOOKUP_KEY": "my_price"})
            self.assertNotIn(LIVE, r.text)
            entry = self.admin.get("/api/admin/audit?limit=1").json()["entries"][0]
            self.assertEqual(entry["action"], "server_settings_changed")
            self.assertEqual(entry["detail"]["fields"], ["stripe_price_lookup_key", "stripe_secret_key"])
            self.assertNotIn(LIVE, json.dumps(entry))
            db = SessionLocal()
            try:
                from backend.db import AuthSession
                db.query(AuthSession).filter(AuthSession.user_id == helpers.admin_id()).update(
                    {AuthSession.reauth_at: utcnow() - timedelta(minutes=20)})
                db.commit()
            finally:
                db.close()
            saved.clear()
            r = self.admin.put("/api/admin/stripe", json={"secret_key": KEY})
            self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"))
            self.assertEqual(saved, {})

    def test_bad_values_are_refused_without_echo(self):
        with mock.patch("backend.app_settings._save") as save:
            for body in ({"secret_key": "pk_test_" + "a" * 30}, {"secret_key": KEY + "\nX=1"},
                         {"webhook_secret": "nope"}, {"price_lookup_key": "has space"}, {}):
                r = self.admin.put("/api/admin/stripe", json=body)
                self.assertEqual(r.status_code, 422, body)
                for value in body.values():
                    self.assertNotIn(value, r.text)
            self.assertEqual(self.admin.put("/api/admin/stripe", json={"unknown": "x"}).status_code, 422)
            save.assert_not_called()

    def test_saving_clears_the_price_cache_and_clearing_a_key_turns_billing_off(self):
        acct, c = self.user(free_searches_used=3)
        self.assertEqual(c.post("/api/documents/summary").status_code, 402)
        with mock.patch.dict(os.environ, {}):
            r = self.admin.put("/api/admin/stripe", json={"secret_key": ""})  # (written to the test .env)
            self.assertEqual(r.status_code, 200)
            self.assertNotEqual(c.post("/api/documents/summary").status_code, 402)

    def test_ordinary_users_cannot_see_or_change_the_settings(self):
        acct, c = self.user()
        self.assertEqual(c.get("/api/admin/stripe").status_code, 403)
        self.assertEqual(c.put("/api/admin/stripe", json={"secret_key": KEY}).status_code, 403)

    # -- grants and subscribers ---------------------------------------------------------------------------------
    def grant(self, acct, **body):
        return self.admin.post(f"/api/admin/users/{acct.id}/grants", json={"kind": "free_period", **body})

    def test_membership_editing_feeds_the_gate(self):
        acct, c = self.user(free_searches_used=3)
        self.assertEqual(c.post("/api/documents/summary").status_code, 402)
        self.assertEqual(self.admin.put(f"/api/admin/users/{acct.id}/membership", json={"exempt": True}).status_code, 200)
        self.assertNotEqual(c.post("/api/documents/summary").status_code, 402)
        self.admin.put(f"/api/admin/users/{acct.id}/membership", json={"exempt": False})
        self.assertEqual(c.post("/api/documents/summary").status_code, 402)
        r = self.grant(acct, days=3)
        self.assertEqual(r.status_code, 201)
        self.assertNotEqual(c.post("/api/documents/summary").status_code, 402)
        self.admin.delete(f"/api/admin/users/{acct.id}/grants/{r.json()['grant']['id']}")
        self.assertEqual(c.post("/api/documents/summary").status_code, 402)
        self.admin.put(f"/api/admin/users/{acct.id}/membership", json={"free_searches_used": 0})
        self.assertEqual(c.post("/api/documents/summary").status_code, 402)  # free searches don't open the AI
        self.assertNotEqual(c.post("/api/search/run").status_code, 402)

    def test_a_free_period_for_someone_who_pays_moves_their_next_payment(self):
        acct, c, ref = self.subscriber()
        self.stripe.v1.subscriptions.list.return_value = {"data": [stripe_sub(ref)]}
        end = utcnow() + timedelta(days=60)
        later = stripe_sub(ref, "trialing")
        later["items"]["data"][0]["current_period_end"] = int((end).replace(tzinfo=timezone.utc).timestamp())
        self.stripe.v1.subscriptions.retrieve.return_value = later
        r = self.grant(acct, ends_at=end.isoformat())
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(r.json()["stripe"]["extended"])
        self.assertIsNone(r.json()["stripe"]["problem"])
        args, kwargs = self.stripe.v1.subscriptions.update.call_args
        self.assertEqual(args, ("sub_123",))
        self.assertEqual(kwargs["params"]["proration_behavior"], "none")
        self.assertAlmostEqual(kwargs["params"]["trial_end"], int(end.replace(tzinfo=timezone.utc).timestamp()), delta=2)
        m = self.member(acct.id)
        self.assertEqual(m.stripe_status, "trialing")  # the row follows what Stripe now says
        self.assertAlmostEqual(m.current_period_end.timestamp(), end.timestamp(), delta=5)

    def test_a_short_free_period_for_a_subscriber_moves_nothing(self):
        acct, c, ref = self.subscriber()
        r = self.grant(acct, days=3)  # their period runs 29 more days
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["stripe"], {"extended": False, "trial_end": None, "problem": None})
        self.stripe.v1.subscriptions.update.assert_not_called()

    def test_changing_a_grant_extends_too(self):
        acct, c, ref = self.subscriber()
        self.stripe.v1.subscriptions.list.return_value = {"data": [stripe_sub(ref)]}
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(ref, "trialing")
        gid = self.grant(acct, days=3).json()["grant"]["id"]
        self.stripe.v1.subscriptions.update.assert_not_called()
        r = self.admin.patch(f"/api/admin/users/{acct.id}/grants/{gid}", json={"days": 90})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["stripe"]["extended"])
        self.stripe.v1.subscriptions.update.assert_called_once()

    # -- ending or shortening a free period undoes the trial it made ---------------------------------------------
    def trial_sub(self, ref, end, status="trialing"):
        """The subscription as Stripe has it while a free period holds the next payment back until ``end``."""
        ts = int(end.replace(tzinfo=timezone.utc).timestamp())
        sub = stripe_sub(ref, status, trial_end=ts)
        sub["items"]["data"][0]["current_period_end"] = ts
        return sub

    def free_for(self, acct, ref, days):
        """A free period of ``days`` days for a subscriber, as the admin adds it. Returns its id and end."""
        end = utcnow() + timedelta(days=days)
        self.stripe.v1.subscriptions.list.return_value = {"data": [stripe_sub(ref)]}
        self.stripe.v1.subscriptions.retrieve.return_value = self.trial_sub(ref, end)
        r = self.grant(acct, ends_at=end.isoformat())
        self.assertTrue(r.json()["stripe"]["extended"], r.text)
        self.stripe.v1.subscriptions.update.reset_mock()
        return r.json()["grant"]["id"], end

    def test_ending_a_free_period_bills_the_subscriber_again_now(self):
        acct, c, ref = self.subscriber()
        gid, end = self.free_for(acct, ref, 365)
        self.assertEqual(self.member(acct.id).stripe_status, "trialing")
        self.stripe.v1.subscriptions.list.return_value = {"data": [self.trial_sub(ref, end)]}
        self.stripe.v1.subscriptions.retrieve.side_effect = [self.trial_sub(ref, end), stripe_sub(ref)]
        r = self.admin.delete(f"/api/admin/users/{acct.id}/grants/{gid}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["stripe"], {"extended": True, "trial_end": None, "problem": None})
        self.stripe.v1.subscriptions.update.assert_called_once_with(
            "sub_123", params={"trial_end": "now", "proration_behavior": "none"})
        self.assertEqual(self.member(acct.id).stripe_status, "active")  # as Stripe now says
        self.assertLess(self.member(acct.id).current_period_end, utcnow() + timedelta(days=60))

    def test_ending_one_free_period_keeps_the_trial_for_the_one_that_remains(self):
        acct, c, ref = self.subscriber()
        gid, end = self.free_for(acct, ref, 120)
        later = utcnow() + timedelta(days=30)
        self.grant(acct, ends_at=later.isoformat())  # a second, shorter one: nothing to move yet
        self.stripe.v1.subscriptions.update.reset_mock()
        self.stripe.v1.subscriptions.list.return_value = {"data": [self.trial_sub(ref, end)]}
        self.stripe.v1.subscriptions.retrieve.side_effect = [self.trial_sub(ref, end), self.trial_sub(ref, later)]
        r = self.admin.delete(f"/api/admin/users/{acct.id}/grants/{gid}")
        self.assertTrue(r.json()["stripe"]["extended"], r.text)
        kwargs = self.stripe.v1.subscriptions.update.call_args.kwargs
        self.assertAlmostEqual(kwargs["params"]["trial_end"], int(later.replace(tzinfo=timezone.utc).timestamp()), delta=2)
        self.assertEqual(kwargs["params"]["proration_behavior"], "none")
        self.assertEqual(self.stripe.v1.subscriptions.update.call_count, 1)

    def test_shortening_a_free_period_moves_the_trial_back(self):
        acct, c, ref = self.subscriber()
        gid, end = self.free_for(acct, ref, 365)
        self.stripe.v1.subscriptions.list.return_value = {"data": [self.trial_sub(ref, end)]}
        shorter = utcnow() + timedelta(days=45)
        self.stripe.v1.subscriptions.retrieve.side_effect = [self.trial_sub(ref, end), self.trial_sub(ref, shorter)]
        r = self.admin.patch(f"/api/admin/users/{acct.id}/grants/{gid}", json={"ends_at": shorter.isoformat()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["stripe"]["extended"])
        kwargs = self.stripe.v1.subscriptions.update.call_args.kwargs
        self.assertAlmostEqual(kwargs["params"]["trial_end"], int(shorter.replace(tzinfo=timezone.utc).timestamp()), delta=2)
        self.assertAlmostEqual(self.member(acct.id).current_period_end.timestamp(), shorter.timestamp(), delta=5)

    def test_a_trial_that_stripe_itself_set_is_left_alone(self):
        acct, c, ref = self.subscriber()
        gid = self.grant(acct, days=3).json()["grant"]["id"]  # moves nothing: their period runs 29 more days
        own = self.trial_sub(ref, utcnow() + timedelta(days=100))
        self.stripe.v1.subscriptions.list.return_value = {"data": [own]}
        self.stripe.v1.subscriptions.retrieve.return_value = own
        r = self.admin.delete(f"/api/admin/users/{acct.id}/grants/{gid}")
        self.assertEqual(r.json()["stripe"], {"extended": False, "trial_end": None, "problem": None})
        self.stripe.v1.subscriptions.update.assert_not_called()

    def test_ending_a_free_period_says_when_stripe_could_not_be_changed(self):
        acct, c, ref = self.subscriber()
        gid, end = self.free_for(acct, ref, 200)
        self.stripe.v1.subscriptions.list.return_value = {"data": [self.trial_sub(ref, end)]}
        self.stripe.v1.subscriptions.retrieve.return_value = self.trial_sub(ref, end)
        self.stripe.v1.subscriptions.update.side_effect = RuntimeError(f"nope {acct.email} cus_123")
        r = self.admin.delete(f"/api/admin/users/{acct.id}/grants/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["stripe"]["extended"])
        self.assertIn("Stripe couldn't", r.json()["stripe"]["problem"])
        for secret in (acct.email, "cus_123"):
            self.assertNotIn(secret, r.text)
        self.assertEqual(self.member(acct.id).stripe_status, "trialing")  # unchanged: the admin was told

    def test_ending_a_free_period_of_someone_who_doesnt_pay_never_reaches_stripe(self):
        acct, c = self.user()
        gid = self.grant(acct, days=5).json()["grant"]["id"]
        r = self.admin.delete(f"/api/admin/users/{acct.id}/grants/{gid}")
        self.assertEqual(r.json()["stripe"], {"extended": False, "trial_end": None, "problem": None})
        self.stripe.v1.subscriptions.list.assert_not_called()
        self.stripe.v1.subscriptions.update.assert_not_called()

    def test_people_without_a_subscription_never_reach_stripe(self):
        acct, c = self.user()
        r = self.grant(acct, days=400)
        self.assertEqual((r.status_code, r.json()["stripe"]["extended"]), (201, False))
        self.stripe.v1.subscriptions.update.assert_not_called()
        self.stripe.v1.subscriptions.list.assert_not_called()

    def test_a_stripe_failure_keeps_the_grant_and_says_so(self):
        acct, c, ref = self.subscriber()
        self.stripe.v1.subscriptions.list.return_value = {"data": [stripe_sub(ref)]}
        self.stripe.v1.subscriptions.update.side_effect = RuntimeError(f"nope {acct.email} cus_123")
        r = self.grant(acct, days=100)
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.json()["stripe"]["extended"])
        self.assertIn("Stripe couldn't", r.json()["stripe"]["problem"])
        for secret in (acct.email, "cus_123"):
            self.assertNotIn(secret, r.text)
        db = SessionLocal()
        try:
            self.assertEqual(db.query(MembershipGrant).filter(MembershipGrant.user_id == acct.id).count(), 1)
        finally:
            db.close()

    # -- exempting someone who pays -----------------------------------------------------------------------------
    def test_exempting_a_subscriber_does_not_touch_their_subscription_by_itself(self):
        acct, c, ref = self.subscriber()
        r = self.admin.put(f"/api/admin/users/{acct.id}/membership", json={"exempt": True})
        self.assertEqual((r.status_code, r.json()["subscription_ending"]), (200, False))
        self.stripe.v1.subscriptions.update.assert_not_called()
        self.stripe.v1.subscriptions.cancel.assert_not_called()
        self.assertFalse(self.member(acct.id).cancel_at_period_end)

    def test_the_admin_can_choose_to_end_it_at_the_period_end(self):
        acct, c, ref = self.subscriber()
        self.stripe.v1.subscriptions.list.return_value = {"data": [stripe_sub(ref)]}
        r = self.admin.put(f"/api/admin/users/{acct.id}/membership",
                           json={"exempt": True, "cancel_subscription_at_period_end": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["subscription_ending"])
        self.stripe.v1.subscriptions.update.assert_called_once_with("sub_123", params={"cancel_at_period_end": True})
        self.stripe.v1.subscriptions.cancel.assert_not_called()  # at the period end, not now
        self.assertTrue(self.member(acct.id).cancel_at_period_end)
        self.assertTrue(self.member(acct.id).exempt)
        self.assertTrue(r.json()["user"]["membership"]["cancel_at_period_end"])

    def test_ending_it_needs_the_exemption_and_a_working_stripe(self):
        acct, c, ref = self.subscriber()
        r = self.admin.put(f"/api/admin/users/{acct.id}/membership", json={"cancel_subscription_at_period_end": True})
        self.assertEqual(r.status_code, 422)
        self.stripe.v1.subscriptions.list.side_effect = RuntimeError("down")
        self.stripe.v1.subscriptions.search.side_effect = RuntimeError("down")
        r = self.admin.put(f"/api/admin/users/{acct.id}/membership",
                           json={"exempt": True, "cancel_subscription_at_period_end": True})
        self.assertEqual((r.status_code, r.json()["code"]), (502, "stripe_failed"))
        self.assertFalse(self.member(acct.id).exempt)  # nothing changed

    def test_turning_off_a_subscriber_says_they_are_still_billed_and_changes_nothing_at_stripe(self):
        acct, c, ref = self.subscriber()
        r = self.admin.post(f"/api/admin/users/{acct.id}/disable")
        self.assertEqual((r.status_code, r.json()["subscription"], r.json()["user"]["status"]),
                         (200, "still_billing", "disabled"))
        self.stripe.v1.subscriptions.update.assert_not_called()
        self.assertFalse(self.member(acct.id).cancel_at_period_end)
        plain = helpers.make_user()
        self.assertEqual(self.admin.post(f"/api/admin/users/{plain.id}/disable").json()["subscription"], "none")

    def test_turning_off_a_subscriber_can_end_the_subscription_at_the_period_end(self):
        acct, c, ref = self.subscriber()
        self.stripe.v1.subscriptions.list.return_value = {"data": [stripe_sub(ref)]}
        r = self.admin.post(f"/api/admin/users/{acct.id}/disable", json={"cancel_subscription_at_period_end": True})
        self.assertEqual((r.status_code, r.json()["subscription"]), (200, "cancelled"))
        self.stripe.v1.subscriptions.update.assert_called_once_with("sub_123", params={"cancel_at_period_end": True})
        self.assertTrue(self.member(acct.id).cancel_at_period_end)
        again = self.admin.post(f"/api/admin/users/{acct.id}/disable")  # (already ending: nothing to warn about)
        self.assertEqual(again.json()["subscription"], "cancelled")

    def test_a_stripe_failure_still_turns_the_account_off_and_says_so(self):
        acct, c, ref = self.subscriber()
        self.stripe.v1.subscriptions.list.side_effect = RuntimeError("down")
        self.stripe.v1.subscriptions.search.side_effect = RuntimeError("down")
        r = self.admin.post(f"/api/admin/users/{acct.id}/disable", json={"cancel_subscription_at_period_end": True})
        self.assertEqual((r.status_code, r.json()["subscription"], r.json()["user"]["status"]),
                         (200, "failed", "disabled"))
        self.assertFalse(self.member(acct.id).cancel_at_period_end)

    def test_a_refused_turn_off_never_reaches_stripe(self):
        self.stripe.v1.subscriptions.update.reset_mock()
        r = self.admin.post(f"/api/admin/users/{helpers.admin_id()}/disable", json={"cancel_subscription_at_period_end": True})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "self"))
        self.stripe.v1.subscriptions.update.assert_not_called()

    def test_the_users_list_shows_how_each_person_pays(self):
        acct, c, ref = self.subscriber()
        users = self.admin.get("/api/admin/users").json()["users"]
        mine = next(u for u in users if u["id"] == acct.id)["membership"]
        self.assertEqual((mine["billing"]["card_brand"], mine["billing"]["card_last4"], mine["billing"]["plan"]),
                         ("visa", "4242", "jobhunt_monthly"))
        plain = next(u for u in users if u["id"] == helpers.admin_id())["membership"]
        self.assertIsNone(plain["billing"])


class DeletingAnAccount(Base):
    def subscriber(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        self.stripe.v1.checkout.sessions.retrieve.return_value = {
            "id": "cs_test_abc123", "client_reference_id": ref, "status": "complete", "subscription": stripe_sub(ref)}
        c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
        self.stripe.v1.subscriptions.list.return_value = {"data": [stripe_sub(ref)]}
        return acct, c, ref

    def delete(self, c, acct):
        return c.post("/api/account/delete", json={"password": helpers.USER_PASSWORD})

    def exists(self, user_id):
        db = SessionLocal()
        try:
            return db.get(User, user_id) is not None
        finally:
            db.close()

    def test_the_subscription_is_cancelled_first(self):
        acct, c, ref = self.subscriber()
        order = []
        self.stripe.v1.subscriptions.cancel.side_effect = lambda *a, **k: order.append(("cancel", self.exists(acct.id)))
        r = self.delete(c, acct)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "subscription": "cancelled"}))
        self.stripe.v1.subscriptions.cancel.assert_called_once_with("sub_123")
        self.assertEqual(order, [("cancel", True)])  # the account still existed when Stripe was told
        self.assertFalse(self.exists(acct.id))
        self.assertEqual(self.stripe.v1.subscriptions.list.call_args.kwargs["params"]["customer"], "cus_123")

    def test_the_admin_deleting_someone_cancels_it_too(self):
        acct, c, ref = self.subscriber()
        with helpers.client() as admin:
            r = admin.delete(f"/api/admin/users/{acct.id}")
        self.assertEqual((r.status_code, r.json()["subscription"]), (200, "cancelled"))
        self.stripe.v1.subscriptions.cancel.assert_called_once_with("sub_123")
        self.assertFalse(self.exists(acct.id))

    def test_a_lost_customer_is_found_by_the_reference(self):
        acct, c, ref = self.subscriber()
        self.set_membership(acct.id, billing_self=None, billing_admin=None)
        self.stripe.v1.subscriptions.search.return_value = {"data": [stripe_sub(ref)]}
        r = self.delete(c, acct)
        self.assertEqual(r.json()["subscription"], "cancelled")
        self.assertEqual(self.stripe.v1.subscriptions.search.call_args.kwargs["params"]["query"],
                         f"metadata['jh_ref']:'{ref}'")
        self.stripe.v1.subscriptions.cancel.assert_called_once()

    def test_a_stripe_failure_never_keeps_the_account_and_is_logged_without_ids(self):
        acct, c, ref = self.subscriber()
        self.stripe.v1.subscriptions.list.side_effect = RuntimeError(f"No such customer cus_123 for {acct.email}")
        self.stripe.v1.subscriptions.search.side_effect = RuntimeError("search is down too")
        with self.assertLogs(level=logging.INFO) as logs:
            r = self.delete(c, acct)
        self.assertEqual((r.status_code, r.json()["subscription"]), (200, "failed"))
        self.assertFalse(self.exists(acct.id))
        text = "\n".join(logs.output)
        self.assertIn("couldn't be cancelled", text)
        for secret in (acct.email, "cus_123", "sub_123", KEY, ref):
            self.assertNotIn(secret, text)
        with helpers.client() as admin:  # the audit log tells the admins to cancel it in Stripe
            entry = next(e for e in admin.get("/api/admin/audit").json()["entries"] if e["action"] == "user_deleted")
        self.assertEqual(entry["detail"]["subscription"], "failed")

    def test_a_refused_deletion_cancels_nothing(self):
        acct, c, ref = self.subscriber()
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                db.add(SearchRun(workspace_id=acct.workspace_id, kind="search", state="running", progress_json="{}"))
                db.commit()
            finally:
                db.close()
        r = self.delete(c, acct)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "run_active"))
        self.stripe.v1.subscriptions.cancel.assert_not_called()
        self.assertTrue(self.exists(acct.id))
        tasks.mark_interrupted()

    def test_a_deletion_refused_for_the_admin_key_cancels_nothing(self):
        acct, c, ref = self.subscriber()
        def role(value):
            db = SessionLocal()
            try:
                db.query(User).filter(User.id == acct.id).update({"role": value})
                db.commit()
            finally:
                db.close()

        role("admin")  # an admin with no key of their own
        self.addCleanup(role, "user")  # (the other tests count the admins)
        r = self.delete(c, acct)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "admin_key_missing"), r.text)
        self.stripe.v1.subscriptions.cancel.assert_not_called()
        self.assertTrue(self.exists(acct.id))

    def test_a_run_queued_after_the_first_check_cancels_nothing(self):
        acct, c, ref = self.subscriber()
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                db.add(SearchRun(workspace_id=acct.workspace_id, kind="search", state="queued", progress_json="{}"))
                db.commit()
            finally:
                db.close()
        with mock.patch("backend.users._check_deletable"):  # the run arrived after it, before the lock
            r = self.delete(c, acct)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "run_active"))
        self.stripe.v1.subscriptions.cancel.assert_not_called()
        self.assertTrue(self.exists(acct.id))
        tasks.mark_interrupted()

    def test_without_stripe_or_a_subscription_there_is_nothing_to_cancel(self):
        acct, c = self.user()
        self.stripe.v1.customers.search.return_value = {"data": []}
        self.stripe.v1.subscriptions.search.return_value = {"data": []}
        r = self.delete(c, acct)
        self.assertEqual(r.json(), {"ok": True})  # Stripe wasn't involved: nothing to say
        other, c2 = self.user()
        self.billing_off()
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""}):
            r = self.delete(c2, other)
        self.assertEqual(r.json(), {"ok": True})


class NothingSensitiveIsLogged(Base):
    def test_a_full_billing_round_leaves_no_ids_addresses_or_keys_in_the_logs(self):
        acct, c = self.user()
        ref = self.member(acct.id).stripe_ref
        env = mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": WHSEC})
        env.start()
        self.addCleanup(env.stop)
        anon = TestClient(app)
        self.stripe.v1.checkout.sessions.create.return_value = {"id": "cs_test_abc123", "url": "https://checkout.stripe.test/x"}
        self.stripe.v1.checkout.sessions.retrieve.return_value = {
            "id": "cs_test_abc123", "client_reference_id": ref, "status": "complete", "subscription": stripe_sub(ref)}
        self.stripe.v1.subscriptions.retrieve.return_value = stripe_sub(ref)
        self.stripe.v1.billing_portal.sessions.create.return_value = {"url": "https://portal.stripe.test/p"}
        self.stripe.v1.subscriptions.list.return_value = {"data": [stripe_sub(ref)]}
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("probe").debug("probe")
            c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"})
            c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
            c.get("/api/billing/status")
            c.post("/api/billing/portal")
            payload = event("customer.subscription.updated", stripe_sub(ref))
            anon.post("/api/billing/webhook", content=payload, headers={"Stripe-Signature": signed(payload)})
            anon.post("/api/billing/webhook", content=payload, headers={"Stripe-Signature": "t=1,v1=" + "0" * 64})
            self.stripe.v1.subscriptions.retrieve.side_effect = RuntimeError(f"{acct.email} cus_123 {KEY}")
            anon.post("/api/billing/webhook", content=payload, headers={"Stripe-Signature": signed(payload)})
            self.stripe.v1.checkout.sessions.retrieve.side_effect = RuntimeError(f"{acct.email} cus_123 {KEY}")
            c.post("/api/billing/verify", json={"session_id": "cs_test_abc123"})
            membership.sync_tick()
            c.post("/api/account/delete", json={"password": helpers.USER_PASSWORD})
        text = "\n".join(logs.output)
        for secret in (*SECRETS, acct.email, ref, "visa"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
