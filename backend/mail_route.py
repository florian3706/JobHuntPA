"""Where JobHuntPA's email goes: the mail server and the public address (accounts design §2.5 and §9).

Whoever controls that route receives every password reset link. So a change to it is treated as owner-level:

* the other admins get a notice about it, sent through the **old** server before the change is saved
  (``notify_admins``), and
* for ``HOLD`` after the change no admin can turn off someone's two-step sign-in or send them a reset link
  (``refuse_during_hold``): both would otherwise let an admin who had just pointed the mail at their own server
  take over an account without its owner seeing a single email.

The time of the last change is an ``app_settings`` row (no key needed). The first set-up, when nothing was set
before, starts no hold.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional

from sqlalchemy.orm import Session

from backend import account_email, mailer, users
from backend.db import utcnow
from backend.sessions import AuthProblem, AuthUser

log = logging.getLogger(__name__)

HOLD = timedelta(hours=24)
SETTING = "mail_route_changed_at"
ROUTE_VARS = ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "PUBLIC_BASE_URL")
DEFAULT_PORTS = {"starttls": "587", "ssl": "465", "none": "25"}


def route(values: Mapping[str, str]) -> tuple:
    """The parts of the settings that decide where mail and its links go, in one comparable form."""
    def get(name: str) -> str:
        return (values.get(name) or "").strip()
    security = get("SMTP_SECURITY").lower() or mailer.DEFAULT_SECURITY
    return (get("SMTP_HOST").lower(), get("SMTP_PORT") or DEFAULT_PORTS.get(security, ""), security,
            get("PUBLIC_BASE_URL").rstrip("/").lower())


def is_change(old: Mapping[str, str], new: Mapping[str, str]) -> bool:
    """True when ``new`` sends mail or its links somewhere else than ``old`` did. Setting it up for the first time
    (nothing was set) is not a change."""
    if not any((old.get(name) or "").strip() for name in ("SMTP_HOST", "PUBLIC_BASE_URL")):
        return False
    return route(old) != route(new)


def mark_changed(db: Session, now: Optional[datetime] = None) -> None:
    users._put_setting(db, SETTING, (now or utcnow()).isoformat())
    db.commit()


def hold_until(db: Session, now: Optional[datetime] = None) -> Optional[datetime]:
    """When the hold after the last change ends (naive UTC), or None when none is running."""
    value = users._setting(db, SETTING)
    try:
        ends = datetime.fromisoformat(value) + HOLD
    except (TypeError, ValueError):
        return None
    if ends.tzinfo is not None:
        ends = ends.astimezone(timezone.utc).replace(tzinfo=None)
    return ends if ends > (now or utcnow()) else None


def refuse_during_hold(db: Session, what: str) -> None:
    """409 ``mail_changed_recently`` while the hold runs. ``what`` finishes "An admin can't ... yet"."""
    ends = hold_until(db)
    if ends is not None:
        raise AuthProblem(409, "mail_changed_recently",
                          f"The mail settings were changed in the last 24 hours, so an admin can't {what} yet. Try "
                          f"again after {ends.strftime('%Y-%m-%d %H:%M')} UTC. Until then the person can use a "
                          "recovery code on the sign-in page.")


def other_admin_addresses(db: Session, admin: AuthUser) -> list[str]:
    from backend import admin as admin_panel  # (the admin panel imports this module)
    return admin_panel.other_admin_addresses(db, admin)


def other_admins(db: Session, admin: AuthUser) -> int:
    """How many other active admins there are (the ones to tell)."""
    from backend import admin as admin_panel
    return len(admin_panel._active_admins(db, besides=admin.id))


def refuse_without_key(db: Session, admin: AuthUser) -> None:
    """409 ``admin_key_missing`` for an admin who can't open the admin details key while other admins are to be told:
    their addresses are sealed to that key, so the notice would silently go to nobody."""
    from backend import admin as admin_panel
    if other_admins(db, admin) and admin_panel._open_apk(db, admin) is None:
        raise admin_panel._key_missing()


def notify_admins(addresses: list[str], config: Optional[mailer.SmtpConfig]) -> int:
    """Queue the change notice to each address, through ``config`` (the settings from before the change). Returns how
    many were queued. Never raises."""
    return sum(account_email.notify("mail_route_changed", address, None, config=config) for address in addresses)
