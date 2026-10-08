"""Stripe memberships (accounts design, section 8), through the official ``stripe`` SDK.

Stripe tells us through the webhook (``verify_webhook``; the route is in membership.py and answers only once
``STRIPE_WEBHOOK_SECRET`` is set). Without it, status is refreshed when the person returns from Checkout
(``fetch_checkout_session``), when the membership page is opened and by the 6-hourly sync (``sync_subscription``).

Settings (.env, admin only, see backend/app_settings.py): STRIPE_SECRET_KEY (``sk_test_`` means test mode),
STRIPE_PRICE_LOOKUP_KEY (default ``jobhunt_monthly``), STRIPE_WEBHOOK_SECRET.

Card numbers never reach this app. Only Stripe ids, plan, status, period end and (optionally) card brand and
last 4 are read. Nothing here logs an email, a card detail or a Stripe object: failures log the exception
class and Stripe's error code only.

Free periods for someone who already subscribes become a Stripe ``trial_end`` extension: call
``trial_end_for_grant`` with their period end and the grant's end; when it returns a timestamp, pass it to
``extend_trial`` (``subscriptions.update(trial_end=..., proration_behavior="none")``), which moves their next
payment to the end of the free period. ``end_trial`` (``trial_end="now"``) undoes it when the free period is ended
early, and ``extend_trial`` with an earlier timestamp when it is shortened. Without a subscription, a grant
alone is enough (access.py).

This module is the only place that talks to Stripe. What the app does with the answers (the membership rows, the
gate, the endpoints) is backend/membership.py. Plans: the price with the lookup key ``STRIPE_PRICE_LOOKUP_KEY``
(default ``jobhunt_monthly``) and, when it exists, the one with ``jobhunt_yearly``; a plan is named by its lookup
key. ``enabled()`` is the gate's "is billing on": a key is set and at least one plan's price exists.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

import stripe

from backend.access import GRACE

log = logging.getLogger(__name__)

DEFAULT_LOOKUP_KEY = "jobhunt_monthly"
YEARLY_LOOKUP_KEY = "jobhunt_yearly"
PRICE_CACHE_SECONDS = 600
UNSURE_SECONDS = 60   # how long enabled() remembers "Stripe didn't answer", so a slow Stripe isn't asked per request
ID_RE = re.compile(r"[A-Za-z0-9_]{3,255}")             # a Stripe object id (cus_..., sub_...)
REF_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")             # a membership's stripe_ref (secrets.token_urlsafe)
SESSION_RE = re.compile(r"cs_[A-Za-z0-9_]{6,250}")      # a Checkout session id
MAX_TRIAL_DAYS = 700  # Stripe refuses a trial_end more than two years ahead
_TIMEOUT = 20

_price_cache: dict = {}
_enabled_cache: dict = {}
_price_lock = threading.Lock()


class BillingError(Exception):
    """A Stripe call failed. The message is safe to show and log (no Stripe object or personal data)."""


class NoPrice(BillingError):
    """Stripe answered, and no active price carries the lookup key."""


class NotFound(BillingError):
    """Stripe answered, and has no such customer (a key of another Stripe account or mode, or a deleted customer)."""


class NoSession(BillingError):
    """Stripe answered, and has no such Checkout session (a typo, or one of another Stripe account or mode)."""


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
    amount: Optional[int] = None                # the price, in the currency's smallest unit (cents)
    currency: Optional[str] = None
    interval: Optional[str] = None              # month, year


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

def _cache_id(key: str, plan: str) -> tuple:
    """Cache entries belong to one Stripe account (a hash of its secret key) and one lookup key, so switching from a
    test key to a live one never reuses a price id of the other."""
    return hashlib.sha256(key.encode()).hexdigest()[:16], plan


def plan_keys() -> list[str]:
    """The lookup keys of the plans on offer: the configured one first, then the yearly one."""
    return list(dict.fromkeys([lookup_key(), YEARLY_LOOKUP_KEY]))


def _price(item: Any, key: str) -> dict:
    return {"id": _g(item, "id"), "lookup_key": key, "interval": _g(_g(item, "recurring"), "interval"),
            "amount": _g(item, "unit_amount"), "currency": _g(item, "currency")}


def list_prices(refresh: bool = False) -> list[dict]:
    """The plans whose price exists in Stripe, in ``plan_keys`` order: ``{id, lookup_key, interval, amount,
    currency}`` each (``amount`` in cents). Cached for 10 minutes; one call. BillingError when Stripe can't be asked
    (no key, no connection); an empty list when it answered and has none."""
    secret, keys = _secret_key(), plan_keys()
    if not secret:
        raise BillingError("Billing isn't set up: there is no Stripe key.")
    with _price_lock:
        hits = [_price_cache.get(_cache_id(secret, k)) for k in keys]
        if not refresh and all(h and time.monotonic() - h[0] < PRICE_CACHE_SECONDS for h in hits):
            return [h[1] for h in hits if h[1]]
    try:
        page = _client().v1.prices.list(params={"lookup_keys": keys, "active": True, "limit": 10})
    except BillingError:
        raise
    except Exception as exc:  # stripe.StripeError, network errors
        raise _fail("look up the prices", exc) from None
    found: dict = {}
    for item in _g(page, "data", []) or []:
        key = _g(item, "lookup_key")
        if key in keys and key not in found:
            found[key] = _price(item, key)
    now = time.monotonic()
    with _price_lock:
        for k in keys:
            _price_cache[_cache_id(secret, k)] = (now, found.get(k))
    return [found[k] for k in keys if k in found]


def find_price(refresh: bool = False, plan: Optional[str] = None) -> dict:
    """The active price carrying the lookup key (``plan``, or the configured one): ``{id, lookup_key, ...}``. Cached
    for 10 minutes. NoPrice when Stripe has none."""
    key = plan or lookup_key()
    cache_id = _cache_id(_secret_key(), key)
    with _price_lock:
        hit = _price_cache.get(cache_id)
        if hit and hit[1] and not refresh and time.monotonic() - hit[0] < PRICE_CACHE_SECONDS:
            return hit[1]
    try:
        page = _client().v1.prices.list(params={"lookup_keys": [key], "active": True, "limit": 1})
    except BillingError:
        raise
    except Exception as exc:  # stripe.StripeError, network errors
        raise _fail("look up the price", exc) from None
    data = _g(page, "data", [])
    if not data:
        raise NoPrice("The membership price isn't set up in Stripe yet (no active price has the lookup key "
                      f"{key}).")
    price = _price(data[0], key)
    with _price_lock:
        _price_cache[cache_id] = (time.monotonic(), price)
    return price


def clear_price_cache() -> None:
    with _price_lock:
        _price_cache.clear()
        _enabled_cache.clear()


def enabled() -> bool:
    """Whether memberships are on: a key is set and Stripe has at least one plan's price. This is the gate's "billing
    off" test (no key or no prices: everyone has access, which keeps local installs as they were). It fails closed:
    when Stripe can't be asked, the last answer stands, and with none, billing counts as on (a key was set, so
    nobody gets the paid features for free because of a network error). Cached like the prices."""
    secret = _secret_key()
    if not secret:
        return False
    cache_id = _cache_id(secret, ",".join(plan_keys()))
    with _price_lock:
        hit = _enabled_cache.get(cache_id)
        if hit and time.monotonic() < hit[0]:
            return hit[1]
    try:
        value, ttl = bool(list_prices()), PRICE_CACHE_SECONDS
    except BillingError:
        value, ttl = (hit[1] if hit else True), UNSURE_SECONDS
    with _price_lock:
        _enabled_cache[cache_id] = (time.monotonic() + ttl, value)
    return value


# ---- Stripe calls ---------------------------------------------------------------------------------------------

def create_checkout_session(customer_ref: str, email: Optional[str], success_url: str, cancel_url: str,
                            customer_id: Optional[str] = None, plan: Optional[str] = None) -> dict:
    """A subscription Checkout session for the price found by lookup key. ``customer_ref`` is the membership's
    opaque ``stripe_ref``; it is sent as client_reference_id and as metadata.jh_ref (on the session and the
    subscription), which is how a subscription is matched to a member. Pass ``customer_id`` when the customer
    is known, otherwise ``email`` pre-fills Checkout. ``success_url`` should contain ``{CHECKOUT_SESSION_ID}``.
    ``plan`` is a lookup key from ``plan_keys`` (the configured one when left out). Returns {"id", "url"}."""
    price = find_price(plan=plan)
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
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "code", None) == "resource_missing":
            raise NoSession("That checkout wasn't found.") from None
        raise _fail("check the checkout", exc) from None
    except Exception as exc:
        raise _fail("check the checkout", exc) from None


@dataclass(frozen=True, repr=False)
class CheckoutResult:
    ref: Optional[str]            # client_reference_id: whose checkout this was
    subscription: Any             # the expanded subscription, or None while there isn't one
    complete: bool                # the person finished paying (or starting the trial)

    def __repr__(self) -> str:
        return "<billing.CheckoutResult>"


def checkout_result(session: Any) -> CheckoutResult:
    """What a retrieved Checkout session says (see ``fetch_checkout_session``)."""
    sub = _g(session, "subscription")
    ref = _g(session, "client_reference_id")
    return CheckoutResult(ref if isinstance(ref, str) else None, None if isinstance(sub, str) else sub,
                          _g(session, "status") == "complete")


_STATUS_RANK = {"active": 0, "trialing": 0, "past_due": 1, "unpaid": 2, "paused": 3, "incomplete": 4,
                "canceled": 5, "incomplete_expired": 6}
_DEAD = ("canceled", "incomplete_expired")
DEAD_STATUSES = _DEAD
_EXPAND_SUBS = ["data.default_payment_method"]


def _best(subs: list) -> Optional[Any]:
    """The subscription that counts: a live one before a past-due one before a dead one, newer before older."""
    if not subs:
        return None
    return min(subs, key=lambda s: (_STATUS_RANK.get(_g(s, "status", ""), 9), -int(_g(s, "created", 0) or 0)))


def _check_id(value: Any) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise BillingError("That isn't a Stripe id.")
    return value


def _subscriptions(customer_id: Optional[str] = None, ref: Optional[str] = None) -> list:
    """A customer's subscriptions (every status), or, when the customer isn't known, those carrying the member's
    ``metadata.jh_ref`` (Stripe's search, which can trail a minute behind)."""
    try:
        if customer_id:
            page = _client().v1.subscriptions.list(params={
                "customer": _check_id(customer_id), "status": "all", "limit": 20, "expand": _EXPAND_SUBS})
        elif isinstance(ref, str) and REF_RE.fullmatch(ref):
            page = _client().v1.subscriptions.search(params={
                "query": f"metadata['jh_ref']:'{ref}'", "limit": 20, "expand": _EXPAND_SUBS})
        else:
            raise BillingError("There is no customer or reference to look up.")
    except BillingError:
        raise
    except stripe.InvalidRequestError as exc:
        if customer_id and getattr(exc, "code", None) == "resource_missing":
            raise NotFound("Stripe has no such customer.") from None
        raise _fail("refresh the subscription", exc) from None
    except Exception as exc:
        raise _fail("refresh the subscription", exc) from None
    return list(_g(page, "data", []))


def sync_subscription(customer_id: str, now: Optional[datetime] = None) -> Optional[MembershipUpdate]:
    """Polling path: the customer's best subscription mapped to our fields, or None when they have none."""
    best = _best(_subscriptions(customer_id=customer_id))
    return apply_subscription(best, now) if best is not None else None


def sync_by_ref(ref: str, now: Optional[datetime] = None) -> Optional[MembershipUpdate]:
    """The same, for a member whose customer id is unknown (their ``billing_self`` was lost in a reset): found again
    by the ``jh_ref`` carried on the subscription."""
    best = _best(_subscriptions(ref=ref))
    return apply_subscription(best, now) if best is not None else None


def fetch_subscription(subscription_id: str, now: Optional[datetime] = None) -> MembershipUpdate:
    """One subscription, mapped (the webhook's way: the event only says which one changed)."""
    try:
        sub = _client().v1.subscriptions.retrieve(
            _check_id(subscription_id), params={"expand": ["default_payment_method"]})
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("fetch the subscription", exc) from None
    return apply_subscription(sub, now)


def find_customer_id(ref: str) -> Optional[str]:
    """The customer who carries the member's ``jh_ref`` (on the customer, set when they returned from Checkout, or
    on their subscription), or None."""
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        return None
    try:
        page = _client().v1.customers.search(params={"query": f"metadata['jh_ref']:'{ref}'", "limit": 1})
        found = _g(page, "data", [])
        if found:
            return _g(found[0], "id")
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("find the customer", exc) from None
    best = _best(_subscriptions(ref=ref))
    customer = _g(best, "customer") if best is not None else None
    return customer if isinstance(customer, str) else _g(customer, "id")


def set_customer_ref(customer_id: str, ref: str) -> None:
    """Mark the customer with the member's ``jh_ref``, so they can be found again."""
    try:
        _client().v1.customers.update(_check_id(customer_id), params={"metadata": {"jh_ref": ref}})
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("mark the customer", exc) from None


def set_customer_email(customer_id: str, email: str) -> None:
    """The address Stripe sends receipts and payment notices to (a customer made by Checkout got the address the
    person had then)."""
    try:
        _client().v1.customers.update(_check_id(customer_id), params={"email": email})
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("update the customer's address", exc) from None


def live_subscription_id(customer_id: Optional[str] = None, ref: Optional[str] = None) -> Optional[str]:
    """The id of the member's subscription that counts (see ``_best``), or None when it has ended or there is none."""
    best = _best([s for s in _subscriptions(customer_id=customer_id, ref=ref) if _g(s, "status", "") not in _DEAD])
    return _g(best, "id") if best is not None else None


def cancel_subscriptions(customer_id: Optional[str] = None, ref: Optional[str] = None, *,
                         at_period_end: bool = False) -> int:
    """End the member's subscriptions: now, or (``at_period_end``) when the period they paid for is over. The
    customer's, or the ones carrying ``ref`` when the customer isn't known. Returns how many were ended."""
    live = [s for s in _subscriptions(customer_id=customer_id, ref=ref) if _g(s, "status", "") not in _DEAD]
    for sub in live:
        try:
            if at_period_end:
                _client().v1.subscriptions.update(_check_id(_g(sub, "id")), params={"cancel_at_period_end": True})
            else:
                _client().v1.subscriptions.cancel(_check_id(_g(sub, "id")))
        except BillingError:
            raise
        except Exception as exc:
            raise _fail("cancel the subscription", exc) from None
    return len(live)


def sync_tick(apply: Callable[[MembershipUpdate], Any], now: Optional[datetime] = None) -> int:
    """Every subscription in the Stripe account, newest status per member (``jh_ref``), handed to ``apply``. The
    scheduler's 6-hourly safety net behind the webhook. Returns how many members were handled. A subscription
    without a ``jh_ref`` isn't ours and is skipped; one member's failure doesn't stop the others."""
    try:
        page = _client().v1.subscriptions.list(params={"status": "all", "limit": 100, "expand": _EXPAND_SUBS})
        best: dict[str, Any] = {}
        for sub in page.auto_paging_iter():
            ref = _g(_g(sub, "metadata"), "jh_ref")
            if isinstance(ref, str) and ref:
                best[ref] = _best([best[ref], sub]) if ref in best else sub
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("refresh the subscriptions", exc) from None
    done = 0
    for sub in best.values():
        try:
            apply(apply_subscription(sub, now))
            done += 1
        except Exception as exc:  # noqa: BLE001  (the message may hold a Stripe id)
            log.warning("Stripe sync: one member's update failed (%s)", type(exc).__name__)
    return done


def extend_trial(subscription_id: str, trial_end: int) -> None:
    """Push the next payment back to ``trial_end`` (a timestamp from ``trial_end_for_grant``), with no proration."""
    try:
        _client().v1.subscriptions.update(
            subscription_id, params={"trial_end": int(trial_end), "proration_behavior": "none"})
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("extend the free period", exc) from None


def end_trial(subscription_id: str) -> None:
    """Undo a free period that ended early: end the trial now, so the member is billed now (no proration)."""
    try:
        _client().v1.subscriptions.update(
            subscription_id, params={"trial_end": "now", "proration_behavior": "none"})
    except BillingError:
        raise
    except Exception as exc:
        raise _fail("end the free period", exc) from None


def trial_end_for_grant(current_period_end: Optional[datetime], grant_end: datetime,
                        now: Optional[datetime] = None, *, trial_end: Optional[datetime] = None) -> Optional[int]:
    """The ``trial_end`` timestamp that makes a subscriber's free period count, or None when nothing needs
    doing (the grant ends before their next payment, or is already over). Capped at what Stripe accepts. When the
    subscription is already in a trial (``trial_end``, from Stripe), the timestamp can also be an earlier one (a
    free period that was shortened); None when it is the trial's end already."""
    now = now or datetime.now(timezone.utc)
    now_ts, end_ts = _ts(now), _ts(grant_end)
    if end_ts <= now_ts:
        return None
    if trial_end is not None:
        wanted = min(end_ts, now_ts + MAX_TRIAL_DAYS * 86400)
        return None if wanted == _ts(trial_end) else wanted
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
        amount=_g(price, "unit_amount"),
        currency=_g(price, "currency"),
        interval=_g(_g(price, "recurring"), "interval"),
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


# Events that say a member's subscription may have changed. Each is answered by fetching the subscription (so the
# event's own copy, which can be out of date or out of order, is never trusted for the state).
HANDLED_EVENTS = frozenset({"checkout.session.completed", "customer.subscription.created",
                            "customer.subscription.updated", "customer.subscription.deleted", "invoice.paid",
                            "invoice.payment_failed"})


def event_subscription(event: Any) -> Optional[tuple[str, Optional[str]]]:
    """``(subscription id, member reference)`` an event is about, or None for an event that isn't ours to handle.
    The reference is only a hint (Checkout's ``client_reference_id``), for a subscription that carries none."""
    kind = _g(event, "type")
    if kind not in HANDLED_EVENTS:
        return None
    obj = _g(_g(event, "data"), "object")
    if kind.startswith("customer.subscription."):
        sub = _g(obj, "id")
    elif kind == "checkout.session.completed":
        sub = _g(obj, "subscription")
    else:  # invoices: the subscription moved under parent.subscription_details in newer API versions
        sub = _g(obj, "subscription") or _g(_g(_g(obj, "parent"), "subscription_details"), "subscription")
    if isinstance(sub, dict) or hasattr(sub, "get"):
        sub = _g(sub, "id")
    if not isinstance(sub, str) or not ID_RE.fullmatch(sub):
        return None
    hint = _g(obj, "client_reference_id") if kind == "checkout.session.completed" else None
    return sub, hint if isinstance(hint, str) else None
