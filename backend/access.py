"""Who may do what: the membership gate (accounts design, section 8). Pure functions over plain data.

Nothing here touches the database, the network or the clock (``now`` is always passed in), so the caller loads an
``AccessState`` from the account's membership row, grants and subscription, asks ``decide``, and answers 402 with
``Decision.message`` when it isn't allowed.

Rules (first match wins):
  1. billing off, an admin or an exempt account: always allowed
  2. an active free period (grant): ``starts_at <= now < ends_at``
  3. a subscription that is active or trialing, or past_due and now < ``grace_until`` (3 days after the failed
     payment, set by ``billing.apply_subscription``)
  4. otherwise: view_jobs always; search while free searches remain; the LLM features while free searches remain
     (the run that uses the last free search still finishes its own scoring: the caller scores inside the run
     without asking the gate); scheduled_search never (it needs a membership).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

FREE_SEARCHES = 3
GRACE = timedelta(days=3)

VIEW_JOBS = "view_jobs"
SEARCH = "search"
SCHEDULED_SEARCH = "scheduled_search"
RESCORE = "rescore"
CHAT = "chat"
COVER_LETTER = "cover_letter"
RESEARCH = "research"
CONCISE_OR_SUMMARY = "concise_or_summary"
FEATURES = (VIEW_JOBS, SEARCH, SCHEDULED_SEARCH, RESCORE, CHAT, COVER_LETTER, RESEARCH, CONCISE_OR_SUMMARY)

_PAID_STATUSES = ("active", "trialing")


def _utc(when: Optional[datetime]) -> Optional[datetime]:
    """A naive datetime is read as UTC (that's how the database stores them)."""
    if when is None:
        return None
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


@dataclass(frozen=True)
class AccessState:
    role: str = "user"                       # 'user' | 'admin'
    exempt: bool = False                     # memberships.exempt
    grants: tuple = ()                       # unrevoked free periods: ((starts_at, ends_at), ...)
    free_searches_used: int = 0              # memberships.free_searches_used
    free_searches_limit: int = FREE_SEARCHES  # app_settings 'free_searches'
    stripe_status: Optional[str] = None      # memberships.stripe_status (Stripe's own status words)
    current_period_end: Optional[datetime] = None
    grace_until: Optional[datetime] = None   # set while past_due
    billing_enabled: bool = True             # False when no Stripe key or prices: everyone has access


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason_code: str   # why: admin, exempt, billing_off, grant, subscribed, grace, free_search, view_always,
    #                    membership_required, free_searches_used
    message: str       # plain words for the user (the body of a 402)
    needs_sync: bool = False  # the subscription looks stale (period end passed): the caller should sync with Stripe


class MembershipRequired(Exception):
    """Raised by ``consume_free_search`` when there is no free search left and no membership."""


_MSG_FREE_USED = ("You have used your free searches. Your jobs stay visible; become a member to run new "
                  "searches, rescore, chat, write cover letters and research companies.")
_MSG_SCHEDULED = "Scheduled searches need a membership. Become a member to turn them on."


def has_active_grant(state: AccessState, now: datetime) -> bool:
    now = _utc(now)
    return any(_utc(start) <= now < _utc(end) for start, end in state.grants)


def subscription_reason(state: AccessState, now: datetime) -> Optional[str]:
    """'subscribed', 'grace' or None. An active subscription whose period has ended is trusted for a grace
    period, so a late sync never locks out someone who is paying."""
    now = _utc(now)
    status = (state.stripe_status or "").lower()
    if status in _PAID_STATUSES:
        end = _utc(state.current_period_end)
        if end is None or now < end:
            return "subscribed"
        return "grace" if now < end + GRACE else None
    if status == "past_due":
        until = _utc(state.grace_until)
        return "grace" if until is not None and now < until else None
    return None


def needs_sync(state: AccessState, now: datetime) -> bool:
    """True when an active, trialing or past-due subscription's period has passed: ask Stripe (at most hourly)."""
    end = _utc(state.current_period_end)
    return (state.stripe_status or "").lower() in ("active", "trialing", "past_due") and end is not None and _utc(now) >= end


def member_reason(state: AccessState, now: datetime) -> Optional[str]:
    """Why this account counts as a member, or None."""
    if not state.billing_enabled:
        return "billing_off"
    if state.role == "admin":
        return "admin"
    if state.exempt:
        return "exempt"
    if has_active_grant(state, now):
        return "grant"
    return subscription_reason(state, now)


def free_searches_left(state: AccessState) -> int:
    return max(0, state.free_searches_limit - state.free_searches_used)


def decide(feature: str, state: AccessState, now: datetime) -> Decision:
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature}")
    if feature == VIEW_JOBS:
        return Decision(True, member_reason(state, now) or "view_always", "")
    reason = member_reason(state, now)
    if reason:
        return Decision(True, reason, "", needs_sync=needs_sync(state, now))
    stale = needs_sync(state, now)
    if feature == SCHEDULED_SEARCH:
        return Decision(False, "membership_required", _MSG_SCHEDULED, stale)
    if free_searches_left(state) > 0:
        return Decision(True, "free_search", "", stale)
    return Decision(False, "free_searches_used", _MSG_FREE_USED, stale)


def consume_free_search(state: AccessState, now: datetime) -> tuple:
    """Count one search run. Returns ``(new_state, charged)``: ``charged`` is True when a free search was used
    (store it as ``search_runs.free_search`` so a failed run can be refunded). Members, and anyone in a free
    period, aren't charged. Raises MembershipRequired when nothing is left."""
    if member_reason(state, now):
        return state, False
    if free_searches_left(state) <= 0:
        raise MembershipRequired(_MSG_FREE_USED)
    return dataclasses.replace(state, free_searches_used=state.free_searches_used + 1), True


def refund_free_search(state: AccessState) -> AccessState:
    """Give one back (the run failed or was interrupted). Call it only for runs recorded as free_search."""
    return dataclasses.replace(state, free_searches_used=max(0, state.free_searches_used - 1))
