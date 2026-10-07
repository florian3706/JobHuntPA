"""Tests for Stripe memberships (backend/billing.py) and the Stripe settings in backend/app_settings.py.
The stripe SDK client is mocked: nothing here touches the network.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("JOBHUNT_ENV_PATH", os.path.join(_TMP, "test.env"))  # never read or write the real .env

from backend import access, app_settings, billing  # noqa: E402
from backend.billing import BillingError, WebhookError  # noqa: E402

KEY = "sk_test_" + "a1B2c3D4" * 4
LIVE = "sk_live_" + "a1B2c3D4" * 4
WHSEC = "whsec_" + "x9Y8z7W6" * 4
EMAIL = "someone.private@example.com"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())
DAY = 86400


def sub(status="active", **kw):
    """A Stripe subscription as the recent API returns it (period dates on the item)."""
    base = {
        "id": "sub_123", "status": status, "customer": "cus_123", "created": TS - 30 * DAY,
        "cancel_at_period_end": False, "metadata": {"jh_ref": "ref_abc"},
        "items": {"data": [{"current_period_start": TS - DAY, "current_period_end": TS + 29 * DAY,
                            "price": {"id": "price_1", "lookup_key": "jobhunt_monthly"}}]},
        "default_payment_method": {"id": "pm_1", "card": {"brand": "visa", "last4": "4242"}},
    }
    base.update(kw)
    return base


def env(**values):
    return mock.patch.dict(os.environ, values, clear=False)


class Configured(unittest.TestCase):
    def setUp(self):
        billing.clear_price_cache()
        patcher = env(STRIPE_SECRET_KEY=KEY, STRIPE_PRICE_LOOKUP_KEY="", STRIPE_WEBHOOK_SECRET="")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = mock.MagicMock()
        self.client.v1.prices.list.return_value = {"data": [{"id": "price_1"}]}
        p = mock.patch.object(billing, "_client", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)


class Status(unittest.TestCase):
    def test_not_configured(self):
        with env(STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="", STRIPE_PRICE_LOOKUP_KEY=""):
            st = billing.status()
        self.assertEqual((st["configured"], st["mode"], st["test_mode"], st["webhook_enabled"]),
                         (False, None, False, False))
        self.assertEqual(st["price_lookup_key"], "jobhunt_monthly")

    def test_test_and_live(self):
        with env(STRIPE_SECRET_KEY=KEY, STRIPE_WEBHOOK_SECRET=WHSEC, STRIPE_PRICE_LOOKUP_KEY="my_price"):
            st = billing.status()
        self.assertEqual((st["configured"], st["mode"], st["test_mode"], st["webhook_enabled"], st["price_lookup_key"]),
                         (True, "test", True, True, "my_price"))
        with env(STRIPE_SECRET_KEY=LIVE):
            self.assertEqual(billing.status()["mode"], "live")

    def test_status_never_contains_the_secrets(self):
        with env(STRIPE_SECRET_KEY=KEY, STRIPE_WEBHOOK_SECRET=WHSEC):
            text = json.dumps(billing.status())
        self.assertNotIn(KEY, text)
        self.assertNotIn(WHSEC, text)

    def test_no_key_means_a_clear_error(self):
        with env(STRIPE_SECRET_KEY=""):
            with self.assertRaises(BillingError):
                billing._client()


class Calls(Configured):
    def test_checkout_session_parameters(self):
        self.client.v1.checkout.sessions.create.return_value = {"id": "cs_1", "url": "https://checkout.stripe.test/x"}
        out = billing.create_checkout_session("ref_abc", EMAIL, "https://app/billing.html?session_id={CHECKOUT_SESSION_ID}",
                                              "https://app/billing.html?cancelled=1")
        self.assertEqual(out, {"id": "cs_1", "url": "https://checkout.stripe.test/x"})
        self.client.v1.prices.list.assert_called_once()
        self.assertEqual(self.client.v1.prices.list.call_args.kwargs["params"]["lookup_keys"], ["jobhunt_monthly"])
        params = self.client.v1.checkout.sessions.create.call_args.kwargs["params"]
        self.assertEqual(params["mode"], "subscription")
        self.assertEqual(params["line_items"], [{"price": "price_1", "quantity": 1}])
        self.assertEqual(params["client_reference_id"], "ref_abc")
        self.assertEqual(params["metadata"], {"jh_ref": "ref_abc"})
        self.assertEqual(params["subscription_data"], {"metadata": {"jh_ref": "ref_abc"}})
        self.assertEqual(params["customer_email"], EMAIL)
        self.assertNotIn("customer", params)
        self.assertIn("{CHECKOUT_SESSION_ID}", params["success_url"])

    def test_checkout_with_a_known_customer_sends_no_email(self):
        self.client.v1.checkout.sessions.create.return_value = {"id": "cs_1", "url": "u"}
        billing.create_checkout_session("ref_abc", EMAIL, "s", "c", customer_id="cus_9")
        params = self.client.v1.checkout.sessions.create.call_args.kwargs["params"]
        self.assertEqual(params["customer"], "cus_9")
        self.assertNotIn("customer_email", params)

    def test_price_found_by_configured_lookup_key_and_cached(self):
        with env(STRIPE_PRICE_LOOKUP_KEY="jobhunt_yearly"):
            self.assertEqual(billing.find_price()["id"], "price_1")
            billing.find_price()
        self.assertEqual(self.client.v1.prices.list.call_count, 1)
        self.assertEqual(self.client.v1.prices.list.call_args.kwargs["params"]["lookup_keys"], ["jobhunt_yearly"])
        billing.find_price(refresh=True)
        self.assertEqual(self.client.v1.prices.list.call_count, 2)

    def test_missing_price_explains_itself(self):
        self.client.v1.prices.list.return_value = {"data": []}
        with self.assertRaises(BillingError) as ctx:
            billing.find_price()
        self.assertIn("jobhunt_monthly", str(ctx.exception))

    def test_portal_session(self):
        self.client.v1.billing_portal.sessions.create.return_value = {"url": "https://portal.test/p"}
        self.assertEqual(billing.create_portal_session("cus_1", "https://app/billing.html"), "https://portal.test/p")
        self.assertEqual(self.client.v1.billing_portal.sessions.create.call_args.kwargs["params"],
                         {"customer": "cus_1", "return_url": "https://app/billing.html"})

    def test_fetch_checkout_session_expands_the_subscription(self):
        self.client.v1.checkout.sessions.retrieve.return_value = {"id": "cs_1", "client_reference_id": "ref_abc"}
        got = billing.fetch_checkout_session("cs_1")
        self.assertEqual(got["client_reference_id"], "ref_abc")
        args, kwargs = self.client.v1.checkout.sessions.retrieve.call_args
        self.assertEqual(args, ("cs_1",))
        self.assertIn("subscription", kwargs["params"]["expand"])

    def test_sync_picks_the_live_subscription_over_a_newer_canceled_one(self):
        self.client.v1.subscriptions.list.return_value = {"data": [
            sub("canceled", id="sub_new", created=TS), sub("active", id="sub_old")]}
        update = billing.sync_subscription("cus_123")
        self.assertEqual((update.subscription_id, update.stripe_status), ("sub_old", "active"))
        self.assertEqual(self.client.v1.subscriptions.list.call_args.kwargs["params"]["customer"], "cus_123")

    def test_sync_with_no_subscription(self):
        self.client.v1.subscriptions.list.return_value = {"data": []}
        self.assertIsNone(billing.sync_subscription("cus_123"))

    def test_stripe_errors_become_safe_billing_errors(self):
        self.client.v1.subscriptions.list.side_effect = RuntimeError(f"No such customer for {EMAIL}")
        with self.assertRaises(BillingError) as ctx:
            billing.sync_subscription("cus_123")
        self.assertNotIn(EMAIL, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)


class ApplySubscription(unittest.TestCase):
    def test_every_stripe_status_is_carried_through(self):
        for status in ("active", "trialing", "past_due", "unpaid", "canceled", "incomplete", "incomplete_expired",
                       "paused"):
            update = billing.apply_subscription(sub(status), NOW)
            self.assertEqual(update.stripe_status, status)
            self.assertEqual(update.grace_until is not None, status == "past_due", status)

    def test_what_the_gate_makes_of_each_status(self):
        expected = {"active": True, "trialing": True, "past_due": True, "unpaid": False, "canceled": False,
                    "incomplete": False, "incomplete_expired": False, "paused": False}
        for status, ok in expected.items():
            u = billing.apply_subscription(sub(status), NOW)
            state = access.AccessState(free_searches_used=3, stripe_status=u.stripe_status,
                                       current_period_end=u.current_period_end, grace_until=u.grace_until)
            self.assertEqual(access.decide("search", state, NOW).allowed, ok, status)

    def test_fields(self):
        u = billing.apply_subscription(sub("active", cancel_at_period_end=True, trial_end=TS + 5 * DAY), NOW)
        self.assertEqual(u.plan, "jobhunt_monthly")
        self.assertEqual(u.current_period_end, NOW + timedelta(days=29))
        self.assertTrue(u.cancel_at_period_end)
        self.assertEqual((u.customer_id, u.subscription_id, u.jh_ref), ("cus_123", "sub_123", "ref_abc"))
        self.assertEqual((u.card_brand, u.card_last4), ("visa", "4242"))
        self.assertEqual(u.trial_end, NOW + timedelta(days=5))
        self.assertIsNone(u.grace_until)
        self.assertEqual(u.current_period_end.tzinfo, timezone.utc)

    def test_past_due_grace_is_three_days_from_the_failed_period_start_and_idempotent(self):
        a = billing.apply_subscription(sub("past_due"), NOW)
        b = billing.apply_subscription(sub("past_due"), NOW + timedelta(days=2))
        self.assertEqual(a.grace_until, NOW - timedelta(days=1) + timedelta(days=3))
        self.assertEqual(a.grace_until, b.grace_until)  # polling doesn't extend it

    def test_old_style_period_on_the_subscription(self):
        old = sub("active", current_period_end=TS + 7 * DAY, current_period_start=TS - DAY, items={"data": [
            {"price": {"id": "p", "recurring": {"interval": "year"}}}]})
        u = billing.apply_subscription(old, NOW)
        self.assertEqual(u.current_period_end, NOW + timedelta(days=7))
        self.assertEqual(u.plan, "year")

    def test_latest_item_period_end_wins(self):
        two = sub("active", items={"data": [
            {"current_period_end": TS + DAY, "price": {"lookup_key": "a"}},
            {"current_period_end": TS + 9 * DAY, "price": {"lookup_key": "b"}}]})
        self.assertEqual(billing.apply_subscription(two, NOW).current_period_end, NOW + timedelta(days=9))

    def test_unexpanded_payment_method_and_customer_object(self):
        u = billing.apply_subscription(sub("active", default_payment_method="pm_1", customer={"id": "cus_obj"}), NOW)
        self.assertEqual((u.card_brand, u.card_last4, u.customer_id), (None, None, "cus_obj"))

    def test_sparse_subscription_does_not_crash(self):
        u = billing.apply_subscription({"id": "sub_x", "status": "active"}, NOW)
        self.assertEqual((u.plan, u.current_period_end, u.jh_ref, u.card_last4), (None, None, None, None))
        u = billing.apply_subscription({"status": "past_due"}, NOW)
        self.assertEqual(u.grace_until, NOW + timedelta(days=3))

    def test_works_on_real_sdk_objects(self):
        import stripe
        obj = stripe.Subscription.construct_from(sub("active"), "sk_test_x")
        u = billing.apply_subscription(obj, NOW)
        self.assertEqual((u.plan, u.card_last4, u.jh_ref), ("jobhunt_monthly", "4242", "ref_abc"))


class TrialExtension(Configured):
    def test_extension_needed_only_when_the_grant_outlasts_the_next_payment(self):
        end = NOW + timedelta(days=10)
        self.assertEqual(billing.trial_end_for_grant(end, NOW + timedelta(days=40), NOW), TS + 40 * DAY)
        self.assertIsNone(billing.trial_end_for_grant(end, end, NOW))                       # boundary: equal
        self.assertEqual(billing.trial_end_for_grant(end, end + timedelta(seconds=1), NOW), int(end.timestamp()) + 1)
        self.assertIsNone(billing.trial_end_for_grant(end, NOW + timedelta(days=3), NOW))
        self.assertIsNone(billing.trial_end_for_grant(end, NOW, NOW))                       # already over
        self.assertEqual(billing.trial_end_for_grant(None, NOW + timedelta(days=5), NOW), TS + 5 * DAY)

    def test_capped_at_what_stripe_accepts(self):
        far = NOW + timedelta(days=2000)
        self.assertEqual(billing.trial_end_for_grant(None, far, NOW), TS + billing.MAX_TRIAL_DAYS * DAY)

    def test_naive_datetimes_are_read_as_utc(self):
        self.assertEqual(billing.trial_end_for_grant(None, (NOW + timedelta(days=5)).replace(tzinfo=None), NOW),
                         TS + 5 * DAY)

    def test_a_trial_that_is_already_set_can_be_lowered(self):
        trial = NOW + timedelta(days=300)
        end = NOW + timedelta(days=40)
        self.assertEqual(billing.trial_end_for_grant(None, end, NOW, trial_end=trial), TS + 40 * DAY)
        self.assertIsNone(billing.trial_end_for_grant(None, trial, NOW, trial_end=trial))   # already what it should be
        self.assertIsNone(billing.trial_end_for_grant(None, NOW, NOW, trial_end=trial))     # over: end_trial's job

    def test_end_trial_bills_now_without_proration(self):
        billing.end_trial("sub_123")
        self.client.v1.subscriptions.update.assert_called_once_with(
            "sub_123", params={"trial_end": "now", "proration_behavior": "none"})

    def test_extend_trial_updates_without_proration(self):
        billing.extend_trial("sub_123", TS + 40 * DAY)
        self.client.v1.subscriptions.update.assert_called_once_with(
            "sub_123", params={"trial_end": TS + 40 * DAY, "proration_behavior": "none"})


class Webhook(unittest.TestCase):
    PAYLOAD = json.dumps({"id": "evt_1", "object": "event", "type": "invoice.paid",
                          "data": {"object": {"id": "in_1"}}}).encode()

    @staticmethod
    def signed(payload, secret, when=None):
        t = int(when or time.time())
        mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        return f"t={t},v1={mac}"

    def test_off_without_a_secret(self):
        with env(STRIPE_WEBHOOK_SECRET=""):
            self.assertIsNone(billing.verify_webhook(self.PAYLOAD, self.signed(self.PAYLOAD, WHSEC)))
            self.assertIsNone(billing.verify_webhook(self.PAYLOAD, None))

    def test_on_with_a_good_signature(self):
        with env(STRIPE_WEBHOOK_SECRET=WHSEC):
            event = billing.verify_webhook(self.PAYLOAD, self.signed(self.PAYLOAD, WHSEC))
        self.assertEqual(event["type"], "invoice.paid")

    def test_on_with_a_bad_signature_missing_header_or_stale_timestamp(self):
        with env(STRIPE_WEBHOOK_SECRET=WHSEC):
            for header in (self.signed(self.PAYLOAD, "whsec_wrongwrongwrongwrong"), None, "garbage",
                           self.signed(self.PAYLOAD, WHSEC, when=time.time() - 3600)):
                with self.assertRaises(WebhookError, msg=header):
                    billing.verify_webhook(self.PAYLOAD, header)

    def test_tampered_payload_is_refused(self):
        with env(STRIPE_WEBHOOK_SECRET=WHSEC):
            header = self.signed(self.PAYLOAD, WHSEC)
            with self.assertRaises(WebhookError):
                billing.verify_webhook(self.PAYLOAD + b" ", header)

    def test_webhook_error_is_a_billing_error(self):
        self.assertTrue(issubclass(WebhookError, BillingError))


class SettingsValidation(unittest.TestCase):
    def setUp(self):
        self.env = Path(_TMP) / f"settings-{id(self)}.env"

    def put(self, **values):
        app_settings.update_env_file(self.env, values)

    def test_names_are_allow_listed(self):
        for name in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_LOOKUP_KEY", "STRIPE_WEBHOOK_SECRET"):
            self.assertIn(name, app_settings.ENV_RULES)

    def test_good_values_are_written(self):
        self.put(STRIPE_SECRET_KEY=KEY, STRIPE_PRICE_LOOKUP_KEY="jobhunt_monthly", STRIPE_WEBHOOK_SECRET=WHSEC)
        text = self.env.read_text()
        for line in (f"STRIPE_SECRET_KEY={KEY}", "STRIPE_PRICE_LOOKUP_KEY=jobhunt_monthly",
                     f"STRIPE_WEBHOOK_SECRET={WHSEC}"):
            self.assertIn(line, text)
        self.put(STRIPE_SECRET_KEY=LIVE)
        self.put(STRIPE_SECRET_KEY="")  # clearing is allowed

    def test_bad_values_are_refused_and_nothing_is_written(self):
        bad = {
            "STRIPE_SECRET_KEY": [
                "pk_test_" + "a" * 32, "rk_test_" + "a" * 32, "sk_prod_" + "a" * 32, "sk_test_short",
                KEY + "\n", KEY + "\nOTHER=1", KEY + "\r", " " + KEY, KEY + " x", "sk_test_" + "a" * 16 + "#x",
                'sk_test_"' + "a" * 20, "sk_test_" + "é" * 20, WHSEC,
            ],
            "STRIPE_WEBHOOK_SECRET": [
                "whsec", "whsec_short", WHSEC + "\n", WHSEC + "\nX=1", "xhsec_" + "a" * 32, KEY, WHSEC + " ",
                "whsec_" + "a" * 20 + '"',
            ],
            "STRIPE_PRICE_LOOKUP_KEY": [
                "has space", "line\nbreak", "jobhunt_monthly\n", "a\rb", "-leading", "x" * 201, "quote'd",
                "semi;colon", "ünï", "a=b", "a#b",
            ],
        }
        for name, values in bad.items():
            for value in values:
                with self.assertRaises(ValueError, msg=f"{name}={value!r}"):
                    self.put(**{name: value})
        self.assertFalse(self.env.exists())

    def test_lookup_key_formats(self):
        for good in ("jobhunt_monthly", "jobhunt-yearly", "Plan.2026", "a", "x" * 200):
            self.put(STRIPE_PRICE_LOOKUP_KEY=good)

    def test_env_example_documents_the_settings(self):
        text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
        for name in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_LOOKUP_KEY", "STRIPE_WEBHOOK_SECRET"):
            self.assertIn(f"# {name}=", text)

    def test_requirements_pin_stripe_with_bounds(self):
        text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
        self.assertRegex(text, r"(?m)^stripe>=\d+,<\d+\s*$")


class NothingSensitiveIsLogged(Configured):
    SECRETS = (EMAIL, "4242", "visa", KEY, WHSEC, "cus_123", "pm_1")

    def captured(self, fn):
        with self.assertLogs(level=logging.DEBUG) as cm:
            logging.getLogger("backend.billing").debug("probe")  # assertLogs needs at least one record
            try:
                fn()
            except BillingError:
                pass
        return "\n".join(cm.output)

    def test_failures_log_only_the_exception_class(self):
        class FakeStripeError(Exception):
            code = "resource_missing"

        self.client.v1.checkout.sessions.create.side_effect = FakeStripeError(f"Bad {EMAIL} card 4242 {KEY}")
        self.client.v1.subscriptions.list.side_effect = FakeStripeError(f"{EMAIL} cus_123")
        self.client.v1.billing_portal.sessions.create.side_effect = FakeStripeError("pm_1 visa")
        self.client.v1.checkout.sessions.retrieve.side_effect = FakeStripeError(EMAIL)
        self.client.v1.subscriptions.update.side_effect = FakeStripeError(EMAIL)
        out = self.captured(lambda: billing.create_checkout_session("ref", EMAIL, "s", "c"))
        out += self.captured(lambda: billing.sync_subscription("cus_123"))
        out += self.captured(lambda: billing.create_portal_session("cus_123", "r"))
        out += self.captured(lambda: billing.fetch_checkout_session("cs_1"))
        out += self.captured(lambda: billing.extend_trial("sub_123", TS + DAY))
        self.assertIn("FakeStripeError", out)
        self.assertIn("resource_missing", out)
        for secret in self.SECRETS:
            self.assertNotIn(secret, out)

    def test_successful_calls_and_mapping_log_nothing_sensitive(self):
        self.client.v1.checkout.sessions.create.return_value = {"id": "cs_1", "url": "u"}
        self.client.v1.subscriptions.list.return_value = {"data": [sub("past_due")]}
        out = self.captured(lambda: billing.create_checkout_session("ref", EMAIL, "s", "c"))
        out += self.captured(lambda: billing.sync_subscription("cus_123"))
        for secret in self.SECRETS:
            self.assertNotIn(secret, out)

    def test_rejected_webhook_logs_no_payload_or_secret(self):
        with env(STRIPE_WEBHOOK_SECRET=WHSEC):
            out = self.captured(lambda: billing.verify_webhook(Webhook.PAYLOAD + b"x", "t=1,v1=" + "0" * 64))
        self.assertIn("rejected", out)
        self.assertNotIn(WHSEC, out)
        self.assertNotIn("evt_1", out)

    def test_membership_update_repr_has_no_secrets(self):
        text = repr(billing.apply_subscription(sub("active"), NOW))
        self.assertNotIn(KEY, text)
        self.assertNotIn(EMAIL, text)


if __name__ == "__main__":
    unittest.main()
