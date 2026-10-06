"""Stripe memberships (accounts design, section 8), through the official ``stripe`` SDK.

The server is not reachable by Stripe yet, so status is kept current by checking the Checkout session when the
person returns (``fetch_checkout_session``) and by polling (``sync_subscription``). ``verify_webhook`` is off
until ``STRIPE_WEBHOOK_SECRET`` is set.

Settings (.env, admin only, see backend/app_settings.py): STRIPE_SECRET_KEY (``sk_test_`` means test mode),
STRIPE_PRICE_LOOKUP_KEY (default ``jobhunt_monthly``), STRIPE_WEBHOOK_SECRET.

Card numbers never reach this app. Only Stripe ids, plan, status, period end and (optionally) card brand and
last 4 are read. Nothing here logs an email, a card detail or a Stripe object: failures log the exception
class and Stripe's error code only.

Free periods for someone who already subscribes become a Stripe ``trial_end`` extension: call
``trial_end_for_grant`` with their period end and the grant's end; when it returns a timestamp, pass it to
``extend_trial`` (``subscriptions.update(trial_end=..., proration_behavior="none")``), which moves their next
payment to the end of the free period. Without a subscription, a grant alone is enough (access.py).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import stripe

from backend.access import GRACE

log = logging.getLogger(__name__)

DEFAULT_LOOKUP_KEY = "jobhunt_monthly"
PRICE_CACHE_SECONDS = 600
MAX_TRIAL_DAYS = 700  # Stripe refuses a trial_end more than two years ahead
_TIMEOUT = 20

_price_cache: dict = {}
_price_lock = threading.Lock()


class BillingError(Exception):
    """A Stripe call failed. The message is safe to show and log (no Stripe object or personal data)."""


class WebhookError(BillingError):
    """The webhook's signature or payload is invalid."""


@dataclass(frozen=True)
class MembershipUpdate:
    """Our fields for one Stripe subscription, ready to store on the membership row."""
    stripe_status: str                          # Stripe's own word: active, trialing, past_due, canceled, ...
    plan: Optional[str]                         # the price's lookup key, e.g. jobhunt_monthly
    current_period_end: Optional[datetime]      # aware UTC
    cancel_at_period_end: bool
    grace_until: Optional[datetime]             # only while past_due: 3 days after the failed renewal
    customer_id: Optional[str]
    subscription_id: Optional[str]
    jh_ref: Optional[str]                       # memberships.stripe_ref, from the subscription's metadata
    card_brand: Optional[str] = None
    card_last4: Optional[str] = None
    trial_end: Optional[datetime] = None


# ---- configuration --------------------------------------------------------------------------------------------

def _secret_key() -> str:
    return (os.getenv("STRIPE_SECRET_KEY") or "").strip()


def _webhook_secret() -> str:
    return (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()


def lookup_key() -> str:
    return (os.getenv("STRIPE_PRICE_LOOKUP_KEY") or "").strip() or DEFAULT_LOOKUP_KEY


def status() -> dict:
    """What the admin page and the gate need: whether billing is configured, and in which mode."""
    key = _secret_key()
    return {
        "configured": bool(key),
        "mode": "test" if key.startswith("sk_test_") else "live" if key else None,
        "test_mode": key.startswith("sk_test_"),
        "webhook_enabled": bool(_webhook_secret()),
        "price_lookup_key": lookup_key(),
    }


def _client() -> "stripe.StripeClient":
    key = _secret_key()
    if not key:
        raise BillingError("Billing isn't set up: there is no Stripe key.")
    return stripe.StripeClient(
        key, max_network_retries=2, http_client=stripe.RequestsClient(timeout=_TIMEOUT))


def _fail(action: str, exc: Exception) -> BillingError:
    """Log the class and Stripe's error code only (messages can quote emails or ids), return a safe error."""
    code = getattr(exc, "code", None)
    log.warning("Stripe %s failed: %s%s", action, type(exc).__name__, f" ({code})" if isinstance(code, str) else "")
    return BillingError(f"Stripe couldn't {action}. Try again in a moment.")


def _g(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a Stripe object or a plain dict (None-safe)."""
    if obj is None:
        return default
    try:
        value = obj[name]
    except (KeyError, TypeError, AttributeError, IndexError):
        value = getattr(obj, name, default)
    return default if value is None else value


def _when(ts: Any) -> Optional[datetime]:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None


def _ts(when: datetime) -> int:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp())


# ---- prices ---------------------------------------------------------------------------------------------------

def find_price(refresh: bool = False) -> dict:
    """The active price carrying the lookup key: {id, lookup_key}. Cached for 10 minutes."""
    key = lookup_key()
    with _price_lock:
        hit = _price_cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < PRICE_CACHE_SECONDS:
            return hit[1]
    try:
        page = _client().v1.prices.list(params={"lookup_keys": [key], "active": True, "limit": 1})
    except BillingError:
        raise
    except Exception as exc:  # stripe.StripeError, network errors
        raise _fail("look up the price", exc) from None
    data = _g(page, "data", [])
    if not data:
        raise BillingError("The membership price isn't set up in Stripe yet (no active price has the lookup key "
                           f"{key}).")
    price = {"id": _g(data[0], "id"), "lookup_key": key}
    with _price_lock:
        _price_cache[key] = (time.monotonic(), price)
    return price


def clear_price_cache() -> None:
    with _price_lock:
        _price_cache.clear()


# ---- Stripe calls ---------------------------------------------------------------------------------------------

def create_checkout_session(customer_ref: str, email: Optional[str], success_url: str, cancel_url: str,
                            customer_id: Optional[str] = None) -> dict:
    """A subscription Checkout session for the price found by lookup key. ``customer_ref`` is the membership's
    opaque ``stripe_ref``; it is sent as client_reference_id and as metadata.jh_ref (on the session and the
    subscription), which is how a subscription is matched to a member. Pass ``customer_id`` when the customer
    is known, otherwise ``email`` pre-fills Checkout. ``success_url`` should contain ``{CHECKOUT_SESSION_ID}``.
    Returns {"id", "url"}."""
    price = find_price()
    params: dict = {
        "mode": "subscription",
        "line_items": [{"price": price["id"], "quantity": 1}],
        "client_reference_id": customer_ref,
        "metadata": {"jh_ref": customer_ref},
        "subscription_data": {"metadata": {"jh_ref": customer_ref}},
        "success_url": success_url,
        "cancel_url": cancel_url,
        "allow_promotion_codes": True,
    }
    if customer_id:
        params["customer"] = customer_id
    elif email:
        params["customer_email"] = email
    try:
        session = _client().v1.checkout.sessions.create(params=params)
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("start checkout", exc) from None
    return {"id": _g(session, "id"), "url": _g(session, "url")}


def create_portal_session(customer_id: str, return_url: str) -> str:
    """The Stripe billing portal URL (change plan, update card, cancel)."""
    try:
        session = _client().v1.billing_portal.sessions.create(
            params={"customer": customer_id, "return_url": return_url})
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("open the billing portal", exc) from None
    return _g(session, "url")


def fetch_checkout_session(session_id: str) -> Any:
    """The Checkout session with its subscription and the subscription's payment method expanded, for the
    return redirect. The caller checks ``client_reference_id`` equals the member's stripe_ref, then passes
    ``session["subscription"]`` to ``apply_subscription``."""
    try:
        return _client().v1.checkout.sessions.retrieve(
            session_id, params={"expand": ["subscription", "subscription.default_payment_method"]})
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("check the checkout", exc) from None


_STATUS_RANK = {"active": 0, "trialing": 0, "past_due": 1, "unpaid": 2, "paused": 3, "incomplete": 4,
                "canceled": 5, "incomplete_expired": 6}


def sync_subscription(customer_id: str, now: Optional[datetime] = None) -> Optional[MembershipUpdate]:
    """Polling path: the customer's best subscription mapped to our fields, or None when they have none."""
    try:
        page = _client().v1.subscriptions.list(params={
            "customer": customer_id, "status": "all", "limit": 10,
            "expand": ["data.default_payment_method"]})
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("refresh the subscription", exc) from None
    subs = list(_g(page, "data", []))
    if not subs:
        return None
    best = min(subs, key=lambda s: (_STATUS_RANK.get(_g(s, "status", ""), 9), -int(_g(s, "created", 0) or 0)))
    return apply_subscription(best, now)


def extend_trial(subscription_id: str, trial_end: int) -> None:
    """Push the next payment back to ``trial_end`` (a timestamp from ``trial_end_for_grant``), with no proration."""
    try:
        _client().v1.subscriptions.update(
            subscription_id, params={"trial_end": int(trial_end), "proration_behavior": "none"})
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("extend the free period", exc) from None


def trial_end_for_grant(current_period_end: Optional[datetime], grant_end: datetime,
                        now: Optional[datetime] = None) -> Optional[int]:
    """The ``trial_end`` timestamp that makes a subscriber's free period count, or None when nothing needs
    doing (the grant ends before their next payment, or is already over). Capped at what Stripe accepts."""
    now = now or datetime.now(timezone.utc)
    now_ts, end_ts = _ts(now), _ts(grant_end)
    if end_ts <= now_ts:
        return None
    if current_period_end is not None and end_ts <= _ts(current_period_end):
        return None
    return min(end_ts, now_ts + MAX_TRIAL_DAYS * 86400)


# ---- mapping --------------------------------------------------------------------------------------------------

def apply_subscription(sub: Any, now: Optional[datetime] = None) -> MembershipUpdate:
    """Pure mapping of a Stripe subscription (expanded default_payment_method is optional) to our fields.

    The period end comes from the subscription items (recent API versions keep it there; the latest of them),
    else from the subscription itself. A past_due subscription gets ``grace_until`` = the start of the failed
    period + 3 days: the same input always gives the same answer, so repeated polling never extends it."""
    items = _g(_g(sub, "items"), "data", []) or []
    ends = [e for e in (_g(i, "current_period_end") for i in items) if e] or [_g(sub, "current_period_end")]
    starts = [s for s in (_g(i, "current_period_start") for i in items) if s] or [_g(sub, "current_period_start")]
    period_end = _when(max((e for e in ends if e), default=None))
    period_start = _when(max((s for s in starts if s), default=None))

    status_ = str(_g(sub, "status", "") or "")
    grace_until = None
    if status_ == "past_due":
        anchor = period_start or period_end or now or datetime.now(timezone.utc)
        grace_until = anchor + GRACE

    price = _g(items[0], "price") if items else None
    plan = _g(price, "lookup_key") or _g(_g(price, "recurring"), "interval")

    method = _g(sub, "default_payment_method")
    card = _g(method, "card") if not isinstance(method, str) else None
    customer = _g(sub, "customer")
    return MembershipUpdate(
        stripe_status=status_,
        plan=plan,
        current_period_end=period_end,
        cancel_at_period_end=bool(_g(sub, "cancel_at_period_end", False)),
        grace_until=grace_until,
        customer_id=customer if isinstance(customer, str) else _g(customer, "id"),
        subscription_id=_g(sub, "id"),
        jh_ref=_g(_g(sub, "metadata"), "jh_ref"),
        card_brand=_g(card, "brand"),
        card_last4=_g(card, "last4"),
        trial_end=_when(_g(sub, "trial_end")),
    )


# ---- webhooks (off until a secret is set) ---------------------------------------------------------------------

def verify_webhook(payload: bytes, sig_header: Optional[str]) -> Optional[Any]:
    """The verified Stripe event, or None when webhooks are off (no STRIPE_WEBHOOK_SECRET: the route answers
    404). Raises WebhookError for a bad signature or payload (the route answers 400)."""
    secret = _webhook_secret()
    if not secret:
        return None
    try:
        return stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as exc:  # SignatureVerificationError, ValueError
        log.warning("Stripe webhook rejected: %s", type(exc).__name__)
        raise WebhookError("The webhook signature or payload isn't valid.") from None
