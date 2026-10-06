"""Tests for the membership gate (backend/access.py). Pure data in, decisions out; no database, no network.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend import access
from backend.access import (
    AccessState, FEATURES, MembershipRequired, consume_free_search, decide, refund_free_search,
)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
S = timedelta(seconds=1)
DAY = timedelta(days=1)
LLM = ("rescore", "chat", "cover_letter", "research", "concise_or_summary")


def allowed(feature, **kw):
    return decide(feature, AccessState(**kw), NOW).allowed


class GateTruthTable(unittest.TestCase):
    def test_features_are_the_documented_set(self):
        self.assertEqual(set(FEATURES), {"view_jobs", "search", "scheduled_search", *LLM})

    def test_unknown_feature_raises(self):
        with self.assertRaises(ValueError):
            decide("teleport", AccessState(), NOW)

    def test_view_jobs_is_always_allowed(self):
        for kw in ({}, {"free_searches_used": 99}, {"stripe_status": "canceled"}, {"billing_enabled": False}):
            self.assertTrue(allowed("view_jobs", **kw), kw)

    def test_fresh_user_has_free_searches_for_everything_but_schedules(self):
        for f in ("search", *LLM):
            d = decide(f, AccessState(), NOW)
            self.assertTrue(d.allowed, f)
            self.assertEqual(d.reason_code, "free_search")
        d = decide("scheduled_search", AccessState(), NOW)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason_code, "membership_required")

    def test_after_the_free_searches_only_viewing_is_left(self):
        st = AccessState(free_searches_used=3)
        for f in ("search", "scheduled_search", *LLM):
            d = decide(f, st, NOW)
            self.assertFalse(d.allowed, f)
            self.assertTrue(d.message.endswith(".") and len(d.message) > 20)
        self.assertEqual(decide("search", st, NOW).reason_code, "free_searches_used")
        self.assertTrue(decide("view_jobs", st, NOW).allowed)

    def test_free_search_limit_boundary_and_custom_limit(self):
        self.assertTrue(allowed("search", free_searches_used=2))
        self.assertFalse(allowed("search", free_searches_used=3))
        self.assertTrue(allowed("search", free_searches_used=4, free_searches_limit=5))
        self.assertFalse(allowed("search", free_searches_used=1, free_searches_limit=0))

    def test_admin_and_exempt_always_allowed(self):
        for kw, code in (({"role": "admin"}, "admin"), ({"exempt": True}, "exempt")):
            for f in FEATURES:
                d = decide(f, AccessState(free_searches_used=99, **kw), NOW)
                self.assertTrue(d.allowed, (f, kw))
                self.assertEqual(d.reason_code, code)

    def test_billing_off_allows_everything(self):
        for f in FEATURES:
            d = decide(f, AccessState(billing_enabled=False, free_searches_used=99), NOW)
            self.assertTrue(d.allowed)
            self.assertEqual(d.reason_code, "billing_off")

    def test_grant_boundaries(self):
        grant = ((NOW - H, NOW + H),)
        for f in FEATURES:
            self.assertTrue(allowed(f, grants=grant, free_searches_used=3), f)
        # starts_at <= now < ends_at
        self.assertTrue(allowed("search", grants=((NOW, NOW + H),), free_searches_used=3))
        self.assertFalse(allowed("search", grants=((NOW - H, NOW),), free_searches_used=3))
        self.assertFalse(allowed("search", grants=((NOW + S, NOW + H),), free_searches_used=3))
        self.assertTrue(allowed("scheduled_search", grants=((NOW - H, NOW + S),)))
        self.assertFalse(allowed("scheduled_search", grants=((NOW - H, NOW - S),)))
        # any one of several
        self.assertTrue(allowed("chat", grants=((NOW - 3 * H, NOW - 2 * H), (NOW - H, NOW + H)), free_searches_used=3))

    def test_naive_datetimes_are_read_as_utc(self):
        naive = ((NOW - H).replace(tzinfo=None), (NOW + H).replace(tzinfo=None))
        self.assertTrue(allowed("chat", grants=(naive,), free_searches_used=3))

    def test_active_and_trialing_subscriptions_allow(self):
        for status in ("active", "trialing", "ACTIVE"):
            for f in FEATURES:
                d = decide(f, AccessState(stripe_status=status, current_period_end=NOW + DAY, free_searches_used=3), NOW)
                self.assertTrue(d.allowed, (status, f))
                self.assertEqual(d.reason_code, "subscribed")

    def test_active_subscription_without_a_period_end_is_trusted(self):
        self.assertTrue(allowed("scheduled_search", stripe_status="active", free_searches_used=3))

    def test_period_end_boundary_then_stale_grace(self):
        end = NOW
        st = AccessState(stripe_status="active", current_period_end=end, free_searches_used=3)
        # one second before the end: subscribed, nothing to sync
        d = decide("search", AccessState(stripe_status="active", current_period_end=end + S, free_searches_used=3), NOW)
        self.assertEqual((d.allowed, d.reason_code, d.needs_sync), (True, "subscribed", False))
        # at the end: a late sync is tolerated for 3 days, and the caller is told to sync
        d = decide("search", st, NOW)
        self.assertEqual((d.allowed, d.reason_code, d.needs_sync), (True, "grace", True))
        self.assertTrue(decide("search", st, end + timedelta(days=3) - S).allowed)
        d = decide("search", st, end + timedelta(days=3))
        self.assertFalse(d.allowed)
        self.assertTrue(d.needs_sync)

    def test_past_due_grace_boundary(self):
        st = AccessState(stripe_status="past_due", grace_until=NOW + S, free_searches_used=3)
        d = decide("search", st, NOW)
        self.assertEqual((d.allowed, d.reason_code), (True, "grace"))
        self.assertFalse(allowed("search", stripe_status="past_due", grace_until=NOW, free_searches_used=3))
        self.assertFalse(allowed("search", stripe_status="past_due", free_searches_used=3))  # no grace recorded

    def test_other_statuses_fall_back_to_free_searches(self):
        for status in ("canceled", "unpaid", "incomplete", "incomplete_expired", "paused", "", None):
            kw = {"stripe_status": status, "current_period_end": NOW + DAY, "grace_until": NOW + DAY}
            self.assertFalse(allowed("search", free_searches_used=3, **kw), status)
            self.assertFalse(allowed("scheduled_search", **kw), status)
            self.assertTrue(allowed("search", **kw), status)  # still has free searches
            self.assertTrue(allowed("view_jobs", free_searches_used=3, **kw))

    def test_member_with_exhausted_free_searches_can_schedule(self):
        self.assertTrue(allowed("scheduled_search", stripe_status="active", current_period_end=NOW + DAY,
                                free_searches_used=3))


class FreeSearchAccounting(unittest.TestCase):
    def test_consume_counts_up_to_the_limit_then_raises(self):
        st = AccessState()
        for i in range(1, 4):
            st, charged = consume_free_search(st, NOW)
            self.assertTrue(charged)
            self.assertEqual(st.free_searches_used, i)
        with self.assertRaises(MembershipRequired):
            consume_free_search(st, NOW)
        self.assertEqual(st.free_searches_used, 3)

    def test_returns_new_state_without_mutating(self):
        st = AccessState()
        new, _ = consume_free_search(st, NOW)
        self.assertEqual(st.free_searches_used, 0)
        self.assertEqual(new.free_searches_used, 1)
        with self.assertRaises(Exception):
            st.free_searches_used = 5  # frozen

    def test_members_are_not_charged(self):
        for st in (AccessState(role="admin"), AccessState(exempt=True),
                   AccessState(grants=((NOW - H, NOW + H),)),
                   AccessState(stripe_status="active", current_period_end=NOW + DAY),
                   AccessState(billing_enabled=False)):
            new, charged = consume_free_search(st, NOW)
            self.assertFalse(charged)
            self.assertEqual(new.free_searches_used, 0)

    def test_member_with_used_up_searches_is_not_blocked(self):
        st = AccessState(exempt=True, free_searches_used=3)
        self.assertEqual(consume_free_search(st, NOW), (st, False))

    def test_refund_gives_one_back_and_never_goes_negative(self):
        st = refund_free_search(AccessState(free_searches_used=3))
        self.assertEqual(st.free_searches_used, 2)
        self.assertTrue(allowed("search", free_searches_used=st.free_searches_used))
        self.assertEqual(refund_free_search(AccessState()).free_searches_used, 0)

    def test_failed_run_round_trip(self):
        st = AccessState(free_searches_used=2)
        st, charged = consume_free_search(st, NOW)
        self.assertTrue(charged)
        self.assertFalse(allowed("search", free_searches_used=st.free_searches_used))
        st = refund_free_search(st)
        self.assertTrue(allowed("search", free_searches_used=st.free_searches_used))

    def test_needs_sync_helper(self):
        self.assertTrue(access.needs_sync(AccessState(stripe_status="past_due", current_period_end=NOW - S), NOW))
        self.assertFalse(access.needs_sync(AccessState(stripe_status="canceled", current_period_end=NOW - S), NOW))
        self.assertFalse(access.needs_sync(AccessState(stripe_status="active", current_period_end=NOW + S), NOW))


if __name__ == "__main__":
    unittest.main()
