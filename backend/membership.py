"""Memberships: the gate, free searches and the billing endpoints (accounts design §8, Phase I).

``backend/access.py`` decides (pure functions); ``backend/billing.py`` talks to Stripe (and nothing else does). This
module is what joins them to the database and the API:

**The gate.** ``gate(feature)`` is a FastAPI dependency for every endpoint that starts a paid feature; a refusal is
402 ``{"code": "membership_required", "detail": ..., "reason": ...}`` (``reason`` is access.py's reason code:
``ai_members_only`` for the AI features, ``free_searches_used`` for a search, ``membership_required`` for a scheduled
search). Only members use the AI features; a trial account gets its free searches (each still scores the jobs it
finds, and up to ``access.FREE_SEARCH_EXTRA_SCORES`` other jobs waiting for a first score, such as imported pages;
all inside the run) and nothing else. ``load_state`` builds the ``AccessState`` from the account's membership row, its unrevoked
grants, its role and the admin's free-search allowance (``signup.free_searches``); with billing off (no Stripe key,
or no price in Stripe, ``billing.enabled()``) every account counts as a member, which keeps local installs as they
were. When the gate sees a subscription whose period has passed it asks Stripe once (at most hourly per person).

**Free searches.** ``charge_search`` runs inside ``tasks.start``'s transaction: it uses one free search and marks the
run (``search_runs.free_search``) in the same commit as the run's row. ``refund_runs`` gives it back when the run ends
failed or is interrupted (``tasks``); it is idempotent, because it only refunds a run whose flag it clears.
Members, admins, exempt accounts and anyone in a free period are never charged. The run scores its own jobs
without asking the gate (see ``app.start_search_run``: the jobs it found and the newest waiting ones, never a merged
duplicate or another workspace's job), so a free search is worth having even though rescoring needs a membership.

**Endpoints** (all need a signed-in user; none is gated: billing must work when you have no access):

    GET  /api/billing/status          {billing: {enabled, test_mode, prices[]}, me: {...}} for the billing page
    POST /api/billing/checkout        {plan} -> {url}: send the browser there
    POST /api/billing/verify          {session_id} -> status: on return from Checkout
    POST /api/billing/portal          -> {url}: Stripe's customer portal (change card, cancel)
    POST /api/billing/webhook         Stripe only: 404 until STRIPE_WEBHOOK_SECRET is set; signed, no session, no CSRF

**What is stored where.** The gate's fields are plaintext columns of ``memberships`` (``stripe_status``, ``plan``,
``current_period_end``, ``grace_until``, ``cancel_at_period_end``). Stripe ids are never plaintext: ``billing_self``
(sealed under the person's own key: customer and subscription ids, so they can open the portal) and
``billing_admin`` (sealed to the admin details key: those ids, card brand and last 4, price and currency). A
card number never reaches this app. Without the person's key (the webhook, the 6-hourly sync) only the plaintext
fields and ``billing_admin`` are written, and ``billing_self`` keeps what it had.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend import access, admin_key, billing, crypto, mailer, signup, users
from backend.db import (
    Membership,
    MembershipGrant,
    SearchRun,
    SessionLocal,
    User,
    Workspace,
    get_db,
)
from backend.ratelimit import Limiter, retry_after, wait_text
from backend.sessions import AuthProblem, AuthUser, current_user, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing"])

SYNC_EVERY = timedelta(hours=1)        # on-demand syncs per person
RECHECK_EVERY = timedelta(minutes=5)   # ... for a subscription stored under another kind of Stripe key (see _foreign)
MAX_WEBHOOK_BYTES = 512 * 1024         # Stripe's events are a few KB
LIVE_STATUSES = ("active", "trialing", "past_due")
# Checkout, verify and portal calls cost Stripe API calls: per person and kind, 30 an hour is plenty.
LIMITER = Limiter([(30, 3600)])
_tried: dict[int, datetime] = {}       # person -> when an on-demand sync last went to Stripe (success or not)
_tried_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(when: Optional[datetime]) -> Optional[datetime]:
    """An aware time as naive UTC (how the database keeps them)."""
    if when is None:
        return None
    return when.astimezone(timezone.utc).replace(tzinfo=None) if when.tzinfo else when


def _iso(when: Optional[datetime]) -> Optional[str]:
    return when.replace(microsecond=0).isoformat() + "Z" if when else None


def membership_row(db: Session, user_id: int) -> Membership:
    """The person's membership row, made (with its random ``stripe_ref``) when an older account has none. Flushed,
    not committed."""
    row = db.get(Membership, user_id)
    if row is None:
        try:
            with db.begin_nested():
                row = Membership(user_id=user_id, stripe_ref=secrets.token_urlsafe(16))
                db.add(row)
        except IntegrityError:  # another request made it a moment ago
            row = db.get(Membership, user_id)
    return row


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def _foreign(m: Optional[Membership]) -> bool:
    """True when the stored subscription came from another kind of Stripe key than the one in use: a test-mode
    payment (Stripe's test card) kept after the operator switched to a live key. It must not count: nothing would
    ever ask Stripe about it before its period ran out. A row stored before ``stripe_mode`` existed counts too, until
    one sync with the current key confirms it."""
    mode = billing.status()["mode"]
    return bool(m and m.stripe_status is not None and mode is not None and m.stripe_mode != mode)


def load_state(db: Session, user_id: int, role: str) -> access.AccessState:
    """The ``AccessState`` of one account. An admin needs no membership, so Stripe isn't asked for them. A
    subscription stored under another kind of Stripe key (``_foreign``) is left out."""
    m = db.get(Membership, user_id)
    paying = m if m and not _foreign(m) else None  # (the free-search count and the exemption stay either way)
    grants = tuple((g.starts_at, g.ends_at) for g in db.query(MembershipGrant).filter(
        MembershipGrant.user_id == user_id, MembershipGrant.revoked_at.is_(None)))
    return access.AccessState(
        role=role, exempt=bool(m and m.exempt), grants=grants,
        free_searches_used=m.free_searches_used if m else 0, free_searches_limit=signup.free_searches(db),
        stripe_status=paying.stripe_status if paying else None,
        current_period_end=paying.current_period_end if paying else None,
        grace_until=paying.grace_until if paying else None,
        billing_enabled=True if role == "admin" else billing.enabled())


def decide(db: Session, user_id: int, role: str, feature: str, now: Optional[datetime] = None) -> access.Decision:
    """access.decide for this account, after asking Stripe once (at most hourly) when the subscription looks stale."""
    now = now or _now()
    decision = access.decide(feature, load_state(db, user_id, role), now)
    if (decision.needs_sync or _foreign(db.get(Membership, user_id))) and sync_user(db, user_id, now):
        decision = access.decide(feature, load_state(db, user_id, role), now)
    return decision


class MembershipRefused(AuthProblem):
    """402: this needs a membership. The page (frontend/common.js) shows ``detail`` with a link to the billing page."""

    def __init__(self, decision: access.Decision):
        super().__init__(402, "membership_required", decision.message)
        self.reason = decision.reason_code

    def response(self) -> JSONResponse:
        return JSONResponse({"detail": self.detail, "code": self.code, "reason": self.reason},
                            status_code=self.status)


def gate(feature: str):
    """A dependency that refuses (402) unless the signed-in account may use ``feature`` (access.FEATURES)."""
    if feature not in access.FEATURES:
        raise ValueError(f"unknown feature: {feature}")

    def dependency(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)) -> access.Decision:
        decision = decide(db, user.id, user.role, feature)
        if not decision.allowed:
            raise MembershipRefused(decision)
        return decision

    dependency.gated_feature = feature  # (tests list the gated routes by it)
    return dependency


def scheduled_block(db: Session, owner_id: int, now: Optional[datetime] = None) -> Optional[str]:
    """Why a scheduled search of this workspace owner can't run for lack of a membership (the scheduler records it
    as the slot's skip reason), or None. Needs no key."""
    user = db.get(User, owner_id)
    if user is None:
        return None
    decision = access.decide(access.SCHEDULED_SEARCH, load_state(db, owner_id, user.role), now or _now())
    return None if decision.allowed else "membership_required"


# ---------------------------------------------------------------------------
# Free searches
# ---------------------------------------------------------------------------

def charge_search(db: Session, run: SearchRun) -> None:
    """Called by ``tasks.start`` for a search run just added to ``db`` (before its commit). Uses one free search of
    the workspace's owner when they have no membership and marks the run, so both land in one commit. Raises
    ``access.MembershipRequired`` (nothing is queued) when none is left, even when two runs race for the last one:
    the update only counts while the person is under their limit."""
    owner = db.query(Workspace.owner_id).filter(Workspace.id == run.workspace_id).scalar()
    user = db.get(User, owner) if owner is not None else None
    if user is None:
        return
    now = _now()
    state = load_state(db, user.id, user.role)
    _, charged = access.consume_free_search(state, now)  # MembershipRequired: nothing left, no membership
    if not charged:
        return
    membership_row(db, user.id)
    taken = db.execute(update(Membership).where(
        Membership.user_id == user.id, Membership.free_searches_used < state.free_searches_limit)
        .values(free_searches_used=Membership.free_searches_used + 1)).rowcount
    if taken != 1:  # another run took the last one a moment ago
        access.consume_free_search(load_state(db, user.id, user.role), now)
        raise access.MembershipRequired("You have used your free searches.")
    run.free_search = True


def refund_runs(db: Session, run_ids: list[int]) -> int:
    """Give back the free search of each of these runs that used one (they failed or were interrupted). In ``db``'s
    transaction (the caller commits). Each run is refunded once: its flag is cleared first. Returns how many."""
    given = 0
    for run_id in run_ids:
        if db.execute(update(SearchRun).where(SearchRun.id == run_id, SearchRun.free_search.is_(True))
                      .values(free_search=False)).rowcount != 1:
            continue
        owner = select(Workspace.owner_id).join(SearchRun, SearchRun.workspace_id == Workspace.id) \
            .where(SearchRun.id == run_id).scalar_subquery()
        used = Membership.free_searches_used
        db.execute(update(Membership).where(Membership.user_id == owner)
                   .values(free_searches_used=case((used > 0, used - 1), else_=0)))
        given += 1
    return given


# ---------------------------------------------------------------------------
# Stripe's answers, stored
# ---------------------------------------------------------------------------

def _self_label(user_id: int) -> str:
    return f"memberships.billing_self|{user_id}"


def stored_ids(m: Membership, apk: Optional[admin_key.Apk] = None) -> dict:
    """``{customer, subscription}`` from ``billing_self`` (when this request is the person's own) or from
    ``billing_admin`` (when ``apk``, an admin's opened key, is given); ``{}`` when neither opens. They can be out of
    date (a new subscription after a cancelled one): the customer is what's relied on."""
    if m.billing_self is not None:
        try:
            data = json.loads(crypto.unseal(m.billing_self, _self_label(m.user_id)))
            if isinstance(data, dict):
                return data
        except (crypto.CryptoError, ValueError):  # not this person's request (no key, or another person's)
            pass
    data = admin_key.open_json(apk, m.billing_admin, admin_key.billing_label(m.user_id))
    return data if isinstance(data, dict) else {}


def apply_update(db: Session, m: Membership, upd: billing.MembershipUpdate, now: Optional[datetime] = None) -> None:
    """Store a Stripe subscription's state on the membership row (in ``db``'s transaction; the caller commits). The
    same input always gives the same row, so replays and re-syncs are harmless."""
    m.stripe_status = upd.stripe_status or None
    m.plan = upd.plan
    m.current_period_end = _naive(upd.current_period_end)
    m.cancel_at_period_end = upd.cancel_at_period_end
    m.grace_until = _naive(upd.grace_until)
    m.stripe_synced_at = _naive(now or _now())
    m.stripe_mode = billing.status()["mode"]
    ids = {"customer": upd.customer_id, "subscription": upd.subscription_id}
    public = admin_key.current_public(db)
    if public is not None:  # no admin has to be signed in: sealed to the admin key's public half
        m.billing_admin = admin_key.seal_json(
            public[0], {**ids, "card_brand": upd.card_brand, "card_last4": upd.card_last4, "amount": upd.amount,
                        "currency": upd.currency, "interval": upd.interval}, admin_key.billing_label(m.user_id))
    try:
        scope = crypto.current()
    except crypto.Locked:
        return  # a sync with nobody's key: billing_self keeps what it had
    if scope.user_id == m.user_id:
        m.billing_self = crypto.seal(json.dumps(ids).encode("utf-8"), _self_label(m.user_id))


def sync_user(db: Session, user_id: int, now: Optional[datetime] = None, *, force: bool = False) -> bool:
    """Ask Stripe about this person's subscription and store the answer. At most once an hour unless ``force``.
    True when the row was updated (or dropped, see ``_forget_stripe``); Stripe's failures are logged (class only)
    and leave the row as it was."""
    now = now or _now()
    m = db.get(Membership, user_id)
    if m is None or not billing.status()["configured"]:
        return False
    foreign = _foreign(m)
    every = RECHECK_EVERY if foreign else SYNC_EVERY  # (a doubtful subscription is looked at sooner)
    with _tried_lock:
        last = _tried.get(user_id)
        if not force and last is not None and now - last < every:
            return False
        synced = m.stripe_synced_at.replace(tzinfo=timezone.utc) if m.stripe_synced_at else None
        if not force and not foreign and synced is not None and now - synced < every:  # (it was synced under the other key)
            return False
        _tried[user_id] = now
    gone = False  # Stripe has nothing under the stored customer (another account or mode, or deleted)
    try:
        customer = stored_ids(m).get("customer")
        found = None
        if customer:
            try:
                found = billing.sync_subscription(customer, now)
                gone = found is None
            except billing.NotFound:
                gone = True
        if found is None and (gone or not customer):
            found = billing.sync_by_ref(m.stripe_ref, now)
    except billing.BillingError:
        return False
    if found is None:
        if gone or foreign:
            _forget_stripe(m)  # what it says can't be true any more: it would keep access going and block checkout
        m.stripe_synced_at = _naive(now)
    else:
        if found.jh_ref and not hmac.compare_digest(found.jh_ref.encode(), m.stripe_ref.encode()):
            return False  # not this person's subscription
        apply_update(db, m, found, now)
    db.commit()
    return found is not None or gone


def _forget_stripe(m: Membership) -> None:
    """Drop everything stored from a Stripe account that no longer knows the person (a test key swapped for a live
    one): the subscription's state and the sealed ids. Free searches, grants and the exemption stay."""
    m.stripe_status = m.plan = m.current_period_end = m.grace_until = None
    m.cancel_at_period_end = False
    m.stripe_mode = None
    m.billing_self = m.billing_admin = None


def sync_tick(now: Optional[datetime] = None) -> int:
    """The 6-hourly safety net behind the webhook (called from backend/schedule.py): every subscription in the Stripe
    account, matched to its member by ``jh_ref``. Returns how many members were updated; 0 with no Stripe key."""
    if not billing.status()["configured"]:
        return 0
    db = SessionLocal()

    def apply(upd: billing.MembershipUpdate) -> None:
        m = db.query(Membership).filter(Membership.stripe_ref == upd.jh_ref).first() if upd.jh_ref else None
        if m is None:
            return
        try:
            apply_update(db, m, upd, now)
            db.commit()
        except BaseException:
            db.rollback()
            raise

    try:
        return billing.sync_tick(apply, now)
    except billing.BillingError:
        return 0  # logged by billing (class and code only)
    finally:
        db.close()


def handle_event(event: Any) -> str:
    """One verified Stripe event: fetch the subscription it is about and store its current state. ``ignored`` (not an
    event we use), ``unmatched`` (a subscription that isn't a member's) or ``applied``. BillingError when Stripe
    can't be asked: the route answers 503 and Stripe sends the event again."""
    target = billing.event_subscription(event)
    if target is None:
        return "ignored"
    sub_id, hint = target
    upd = billing.fetch_subscription(sub_id)
    ref = upd.jh_ref or hint
    db = SessionLocal()
    try:
        m = db.query(Membership).filter(Membership.stripe_ref == ref).first() if ref else None
        if m is None:
            log.info("Stripe webhook: a subscription that isn't any member's")
            return "unmatched"
        if upd.stripe_status in billing.DEAD_STATUSES and upd.customer_id:
            # A late event for an ended subscription must not undo the one that replaced it.
            best = billing.sync_subscription(upd.customer_id)
            if best is not None and (not best.jh_ref or hmac.compare_digest(best.jh_ref.encode(), m.stripe_ref.encode())):
                upd = best
        apply_update(db, m, upd)
        db.commit()
        return "applied"
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Admin and account-deletion helpers (the Stripe side of what admin.py and users.py do)
# ---------------------------------------------------------------------------

def billing_summary(m: Optional[Membership], apk: Optional[admin_key.Apk]) -> Optional[dict]:
    """What the admin's list shows about how someone pays (``billing_admin``, opened with the admin key): card brand
    and last 4, plan, price. Never a card number (there isn't one)."""
    if m is None or apk is None or m.billing_admin is None:
        return None
    data = admin_key.open_json(apk, m.billing_admin, admin_key.billing_label(m.user_id))
    if not isinstance(data, dict):
        return None
    return {"card_brand": data.get("card_brand"), "card_last4": data.get("card_last4"), "plan": m.plan,
            "amount": data.get("amount"), "currency": data.get("currency"), "interval": data.get("interval")}


def cancel_at_period_end_for(m: Membership, apk: Optional[admin_key.Apk]) -> int:
    """End the person's subscription when the period they paid for is over (an admin exempted them). Returns how
    many subscriptions were set to end; BillingError when Stripe refuses. The caller commits ``m``."""
    ids = stored_ids(m, apk)
    count = _stripe_for(ids, m, lambda customer, ref: billing.cancel_subscriptions(customer, ref, at_period_end=True))
    if count:
        m.cancel_at_period_end = True
    return count


def _stripe_for(ids: dict, m: Membership, fn) -> Any:
    """``fn(customer_id, ref)`` for the stored customer; for the member's reference when that customer is gone."""
    customer = ids.get("customer")
    if customer:
        try:
            return fn(customer, None)
        except billing.BillingError:
            pass
    return fn(None, m.stripe_ref)


def extend_for_grant(db: Session, user_id: int, starts_at: datetime, ends_at: datetime,
                     apk: Optional[admin_key.Apk], now: Optional[datetime] = None) -> dict:
    """A free period for someone who pays: move their next payment to its end (``trial_end``, no proration), so they
    aren't charged while it runs. ``{"extended": bool, "trial_end": iso|None, "problem": text|None}``; never raises:
    the grant stands either way, and the admin is told when Stripe couldn't be changed. Commits."""
    now = now or _now()
    out: dict = {"extended": False, "trial_end": None, "problem": None}
    m = db.get(Membership, user_id)
    if m is None or m.stripe_status not in LIVE_STATUSES or not billing.status()["configured"]:
        return out
    period_end = m.current_period_end.replace(tzinfo=timezone.utc) if m.current_period_end else None
    if period_end is not None and starts_at.replace(tzinfo=timezone.utc) > period_end:
        return out  # the free period starts after their next payment: nothing to move yet
    trial_end = billing.trial_end_for_grant(period_end, ends_at.replace(tzinfo=timezone.utc), now)
    if trial_end is None:
        return out
    ids = stored_ids(m, apk)
    try:
        sub_id = _stripe_for(ids, m, lambda customer, ref: billing.live_subscription_id(customer, ref))
        if sub_id is None:
            out["problem"] = "They have no subscription in Stripe."
            return out
        billing.extend_trial(sub_id, trial_end)
        out.update(extended=True, trial_end=_iso(_naive(datetime.fromtimestamp(trial_end, tz=timezone.utc))))
        apply_update(db, m, billing.fetch_subscription(sub_id), now)  # the new period end, as Stripe now has it
        db.commit()
    except billing.BillingError as exc:
        db.rollback()
        out["problem"] = str(exc) if not out["extended"] else None
    return out


def shrink_for_grant(db: Session, user_id: int, old_end: datetime, apk: Optional[admin_key.Apk],
                     now: Optional[datetime] = None) -> dict:
    """The other way round: a free period that was ended or made shorter (it used to end at ``old_end``) must not
    leave the Stripe trial ``extend_for_grant`` set. The trial is moved back to the end of the latest free period
    that remains, or ended now (the member is billed now) when none does. Only a trial that ends no later than
    ``old_end`` is touched (it was set for this free period, not by Stripe's own price). Same answer as
    ``extend_for_grant`` (``extended`` says Stripe was changed); never raises; commits."""
    now = now or _now()
    out: dict = {"extended": False, "trial_end": None, "problem": None}
    m = db.get(Membership, user_id)
    if m is None or m.stripe_status not in LIVE_STATUSES or not billing.status()["configured"]:
        return out
    try:
        sub_id = _stripe_for(stored_ids(m, apk), m, lambda customer, ref: billing.live_subscription_id(customer, ref))
        if sub_id is None:
            return out
        current = billing.fetch_subscription(sub_id)
        trial_end = current.trial_end
        if current.stripe_status != "trialing" or trial_end is None or trial_end <= now \
                or trial_end > old_end.replace(tzinfo=timezone.utc) + timedelta(seconds=1):
            return out
        remaining = [g.ends_at for g in db.query(MembershipGrant).filter(
            MembershipGrant.user_id == user_id, MembershipGrant.revoked_at.is_(None),
            MembershipGrant.ends_at > _naive(now), MembershipGrant.starts_at <= _naive(trial_end))]
        if remaining:
            wanted = billing.trial_end_for_grant(None, max(remaining).replace(tzinfo=timezone.utc), now,
                                                 trial_end=trial_end)
            if wanted is None or wanted >= int(trial_end.timestamp()):
                return out  # a remaining free period covers the trial
            billing.extend_trial(sub_id, wanted)
            out["trial_end"] = _iso(_naive(datetime.fromtimestamp(wanted, tz=timezone.utc)))
        else:
            billing.end_trial(sub_id)
        out["extended"] = True
        apply_update(db, m, billing.fetch_subscription(sub_id), now)
        db.commit()
    except billing.BillingError as exc:
        db.rollback()
        out["problem"] = str(exc) if not out["extended"] else None
    return out


def cancel_for_deletion(db: Session, user_id: int, actor_id: Optional[int] = None) -> str:
    """Cancel the person's Stripe subscription now, before their account is deleted (design §6.8 step 2). Best effort:
    ``none`` (no Stripe, or nothing to cancel), ``cancelled`` or ``failed`` (logged without any id or address; the
    account is deleted all the same, and the audit entry says to cancel it in Stripe). The customer is found through
    whichever of their own key (a self-delete), the deleting admin's key, or the member's reference works."""
    if not billing.status()["configured"]:
        return "none"
    m = db.get(Membership, user_id)
    if m is None:
        return "none"
    apk = None
    if actor_id is not None:
        try:
            apk = admin_key.open_apk(db, actor_id)
        except admin_key.ApkUnavailable:
            pass
    try:
        count = _stripe_for(stored_ids(m, apk), m, lambda customer, ref: billing.cancel_subscriptions(customer, ref))
    except billing.BillingError:
        log.warning("Account deletion: a Stripe subscription couldn't be cancelled; cancel it in the Stripe dashboard")
        return "failed"
    if count:
        log.info("Account deletion: %d Stripe subscription(s) cancelled", count)
    return "cancelled" if count else "none"


def update_customer_email(db: Session, user_id: int, email: str) -> None:
    """After an address change: tell Stripe the new address, so its receipts and payment notices stop going to the
    old one. Never raises: the change stands either way (the person can still edit it in the portal). The customer
    is the stored one when this request can open it; the link is usually opened without the person's key, so
    otherwise they are found by the member's reference. Nothing is logged here but billing's own class-only line."""
    if not billing.status()["configured"]:
        return
    m = db.get(Membership, user_id)
    if m is None:
        return
    try:
        customer = stored_ids(m).get("customer") or billing.find_customer_id(m.stripe_ref)
        if customer:
            billing.set_customer_email(customer, email)
    except billing.BillingError:
        pass  # logged by billing


# ---------------------------------------------------------------------------
# The person's state, for the billing page
# ---------------------------------------------------------------------------

def person_state(db: Session, user_id: int, role: str, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    state = load_state(db, user_id, role)
    m = db.get(Membership, user_id)
    if _foreign(m):
        m = None  # (a test-mode payment under a live key shows as no subscription; the sync on opening the page sorts it out)
    reason = access.member_reason(state, now)
    searching = access.decide(access.SEARCH, state, now)
    grants_end = [g[1] for g in state.grants if access._utc(g[0]) <= access._utc(now) < access._utc(g[1])]
    return {
        "member": reason is not None,
        "reason": reason,                      # admin, exempt, billing_off, grant, subscribed, grace, or null
        "free_searches_left": access.free_searches_left(state),
        "free_searches_limit": state.free_searches_limit,
        "free_search_extra_scores": access.FREE_SEARCH_EXTRA_SCORES,  # waiting jobs a free search scores besides its own
        "can_search": searching.allowed,
        "message": None if searching.allowed else searching.message,
        "plan": m.plan if m else None,
        "stripe_status": m.stripe_status if m else None,
        "current_period_end": _iso(m.current_period_end) if m else None,
        "cancel_at_period_end": bool(m.cancel_at_period_end) if m else False,
        "grace_until": _iso(m.grace_until) if m and m.stripe_status == "past_due" else None,
        "free_period_until": _iso(_naive(max(access._utc(e) for e in grants_end))) if grants_end else None,
        "can_manage": bool(m and (m.stripe_status is not None or m.billing_self is not None)),
        "can_subscribe": billing.enabled() and not (m and m.stripe_status in LIVE_STATUSES),
    }


def prices_info() -> list[dict]:
    """The plans on offer as the page shows them: ``{plan, interval, amount, currency}``; ``amount`` is in cents."""
    try:
        return [{"plan": p["lookup_key"], "interval": p["interval"], "amount": p["amount"], "currency": p["currency"]}
                for p in billing.list_prices()]
    except billing.BillingError:
        return []


def _stripe_problem(exc: billing.BillingError) -> AuthProblem:
    if isinstance(exc, billing.NoPrice):
        return AuthProblem(409, "no_price", str(exc))
    if isinstance(exc, billing.NoSession):
        return AuthProblem(404, "no_session", str(exc))
    return AuthProblem(502, "stripe_failed", str(exc))


def _need_billing() -> None:
    if not billing.enabled():
        raise AuthProblem(409, "billing_off", "Memberships aren't set up on this JobHuntPA.")


def _throttle(user_id: int, kind: str) -> None:
    wait = LIMITER.hit((user_id, kind))
    if wait is not None:
        raise AuthProblem(429, "rate_limited", f"Too many tries. Try again in {wait_text(wait)}.",
                          headers={"Retry-After": retry_after(wait)})


def base_url(request: Request) -> str:
    return mailer.public_base_url() or str(request.base_url).rstrip("/")


def _status(db: Session, user: AuthUser) -> dict:
    return {"billing": {"enabled": billing.enabled(), "test_mode": billing.status()["test_mode"],
                        "prices": prices_info()},
            "me": person_state(db, user.id, user.role)}


@router.get("/status")
def get_status(user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """What the billing page shows: whether payments are on, test mode, the prices, and this person's standing
    (member and why, free searches left, plan, period end, whether it ends at the period's end). Opening it also
    refreshes a person who has a subscription from Stripe (at most hourly)."""
    m = db.get(Membership, user.id)
    if m is not None and (m.stripe_status is not None or m.billing_self is not None):
        sync_user(db, user.id)
    return _status(db, user)


class CheckoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: str = Field(max_length=200)


@router.post("/checkout")
def checkout(body: CheckoutIn, request: Request, user: AuthUser = Depends(current_user),
             db: Session = Depends(get_db)):
    """Start Stripe Checkout for ``plan`` (a lookup key from ``status.billing.prices``): ``{url}``, to which the page
    sends the browser. The session carries the person's opaque reference, and comes back to billing.html."""
    _need_billing()
    _throttle(user.id, "checkout")
    if body.plan not in billing.plan_keys():
        raise AuthProblem(422, "invalid", "Choose one of the plans on the page.", field="plan")
    m = membership_row(db, user.id)
    if m.stripe_status in LIVE_STATUSES:
        confirmed = sync_user(db, user.id, force=True)  # someone who cancelled in Stripe isn't held to a stale "active"
        if m.stripe_status in LIVE_STATUSES and (
                confirmed or access.subscription_reason(load_state(db, user.id, user.role), _now()) is not None):
            # (a status Stripe couldn't confirm, whose paid period is long over, doesn't lock the person out)
            if m.stripe_status == "past_due":
                raise AuthProblem(409, "already_subscribed", "Your last payment didn't go through. Use Manage to "
                                                             "update your payment method.")
            raise AuthProblem(409, "already_subscribed", "You already have a membership. Use Manage to change it.")
    customer = stored_ids(m).get("customer")
    email = None if customer else users.details(db, user.id).get("email") or None
    base = base_url(request)
    try:
        session = billing.create_checkout_session(
            m.stripe_ref, email, f"{base}/billing.html?session_id={{CHECKOUT_SESSION_ID}}",
            f"{base}/billing.html?cancelled=1", customer_id=customer, plan=body.plan)
    except billing.BillingError as exc:
        raise _stripe_problem(exc) from None
    db.commit()
    if not isinstance(session.get("url"), str):
        raise AuthProblem(502, "stripe_failed", "Stripe didn't give a checkout address. Try again in a moment.")
    return {"url": session["url"]}


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(max_length=300)


@router.post("/verify")
def verify(body: VerifyIn, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """On return from Checkout: fetch the session, check it is this person's (its ``client_reference_id`` must be
    their reference: 403 ``session_mismatch`` for anyone else's), then store the subscription's state, their
    ``billing_self`` and the admins' ``billing_admin``, and mark the Stripe customer with the reference. Answers like
    ``/status``. Safe to repeat."""
    _need_billing()
    _throttle(user.id, "verify")
    if not billing.SESSION_RE.fullmatch(body.session_id):
        raise AuthProblem(422, "invalid", "That isn't a checkout session.", field="session_id")
    m = membership_row(db, user.id)
    try:
        result = billing.checkout_result(billing.fetch_checkout_session(body.session_id))
    except billing.BillingError as exc:
        raise _stripe_problem(exc) from None
    if result.ref is None or not hmac.compare_digest(result.ref.encode(), m.stripe_ref.encode()):
        log.warning("user %s: a checkout session that isn't theirs was refused", user.id)
        raise AuthProblem(403, "session_mismatch", "That checkout belongs to someone else, so it was not applied.")
    if not result.complete or result.subscription is None:
        raise AuthProblem(409, "not_complete", "The payment hasn't gone through yet. Check again in a moment, or "
                                               "try again from the billing page.")
    upd = billing.apply_subscription(result.subscription)
    if upd.jh_ref is not None and not hmac.compare_digest(upd.jh_ref.encode(), m.stripe_ref.encode()):
        log.warning("user %s: a checkout whose subscription isn't theirs was refused", user.id)
        raise AuthProblem(403, "session_mismatch", "That checkout belongs to someone else, so it was not applied.")
    apply_update(db, m, upd)
    db.commit()
    if upd.customer_id:
        try:
            billing.set_customer_ref(upd.customer_id, m.stripe_ref)
        except billing.BillingError:
            pass  # only a convenience for finding them again; logged by billing
    return _status(db, user)


@router.post("/portal")
def portal(request: Request, user: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Stripe's customer portal (change card, cancel): ``{url}``. The customer is the one in ``billing_self``, or,
    after a reset wiped it, the one found again by the person's reference. Needs the password entered in the last 15
    minutes: the portal shows the billing name, address and card, and cancels or changes the plan, so a stolen
    session cookie must not open it."""
    _need_billing()
    require_recent_auth(user)
    _throttle(user.id, "portal")
    m = membership_row(db, user.id)
    customer = stored_ids(m).get("customer")
    try:
        if not customer:
            customer = billing.find_customer_id(m.stripe_ref)
            if customer:
                m.billing_self = crypto.seal(json.dumps({"customer": customer}).encode("utf-8"), _self_label(user.id))
                db.commit()
        if not customer:
            raise AuthProblem(409, "no_customer", "You don't have a subscription to manage yet.")
        url = billing.create_portal_session(customer, f"{base_url(request)}/billing.html")
    except billing.BillingError as exc:
        raise _stripe_problem(exc) from None
    if not isinstance(url, str):
        raise AuthProblem(502, "stripe_failed", "Stripe didn't give a portal address. Try again in a moment.")
    return {"url": url}


async def _body(request: Request, limit: int) -> Optional[bytes]:
    """The request body, or None once it is bigger than ``limit`` (nothing more is read)."""
    size = request.headers.get("content-length")
    if size is not None and size.isdigit() and int(size) > limit:
        return None
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhook")
async def webhook(request: Request):
    """Stripe's events. Answers 404 until ``STRIPE_WEBHOOK_SECRET`` is set (as if there were no such route). The
    signature is checked first (400 when it fails), with no session and no CSRF token (backend/sessions.py leaves
    this path out of both: Stripe's signature is the proof). Each event says which subscription changed; its
    current state is fetched and stored, so a repeated or late event can't leave a stale state behind. Answers 200
    for events that aren't ours, 503 when Stripe can't be asked (Stripe then sends it again)."""
    if not billing.status()["webhook_enabled"]:
        raise HTTPException(status_code=404, detail="Not Found")
    payload = await _body(request, MAX_WEBHOOK_BYTES)
    if payload is None:
        return JSONResponse({"detail": "That's too big.", "code": "too_big"}, status_code=413)
    try:
        event = billing.verify_webhook(payload, request.headers.get("stripe-signature"))
    except billing.WebhookError:
        return JSONResponse({"detail": "The signature isn't valid.", "code": "bad_signature"}, status_code=400)
    if event is None:
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        outcome = await run_in_threadpool(handle_event, event)
    except billing.BillingError:
        return JSONResponse({"detail": "Stripe couldn't be reached. Try again.", "code": "stripe_failed"},
                            status_code=503)
    return {"received": True, "outcome": outcome}
