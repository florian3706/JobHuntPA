"""Email templates as plain functions. Design: docs/design/accounts.md, section 9.

Each returns ``(subject, text, html, extra_headers)`` ready for ``mailer.send(to, subject, text, html, extra_headers)``.
Nothing here reads settings except the default base URL, and nothing logs.

Accessibility: ``<html lang>``, a real ``<title>``, layout tables with ``role="presentation"``, h1/h2 headings, no
images, links whose text names their target, and colours above 4.5:1 (checked in tests). The page reads fine with
CSS stripped, and the text part carries the same content.

Link targets. A token that gives access (a reset or an email check) sits in the address's fragment, after "#": a
browser never sends the fragment to a server or in a Referer header, so it can't end up in an access log. The page reads
it with ``location.hash`` and posts it. An invitation links to the sign-up page itself (it reads
``?invite=``):
    {base}/reset-password.html#token=...   {base}/verify-email.html#token=...   {base}/signup.html?invite=...
    {base}/job.html?id=ID[&ws=WORKSPACE]   {base}/unsubscribe?token=...
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any, Iterable, Optional
from urllib.parse import quote, urlsplit

from backend import mailer
from backend.employment import normalise_employment_type
from backend.keys import ServerKeys

Template = tuple[str, str, str, dict]

# Colours (all on white or on CARD), contrast ratios checked in tests/test_email.py.
TEXT, MUTED, LINK, CARD, BORDER = "#12161d", "#4a5260", "#3a2be0", "#f4f5f8", "#c9ced8"
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

EMPLOYMENT_LABELS = {
    "full_time": "Full-time", "part_time": "Part-time", "contract": "Contract",
    "casual": "Casual", "temporary": "Temporary", "internship": "Internship",
}
WORK_MODE_LABELS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "Onsite", "on_site": "Onsite",
                    "on-site": "Onsite", "office": "Onsite"}


class NoBaseUrl(ValueError):
    """PUBLIC_BASE_URL isn't set (or isn't valid), so links can't be built."""


def _base(base_url: Optional[str]) -> str:
    base = (base_url or mailer.public_base_url() or "").rstrip("/")
    if not base:
        raise NoBaseUrl("PUBLIC_BASE_URL is not set")
    return base


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


# -- layout ------------------------------------------------------------------------------------------------------

def _page(title: str, preheader: str, body: str, footer: str = "") -> str:
    """The shared layout: one 600 px column in a presentation table; ``body`` and ``footer`` are HTML."""
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{_e(title)}</title>\n</head>\n'
        f'<body style="margin:0;padding:0;background-color:#ffffff;color:{TEXT};font-family:{FONT};'
        'font-size:16px;line-height:1.5;">\n'
        f'<div style="display:none;max-height:0;overflow:hidden;">{_e(preheader)}</div>\n'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background-color:#ffffff;"><tr><td align="center" style="padding:24px 16px;">\n'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:600px;"><tr><td style="font-size:16px;line-height:1.5;">\n'
        f'{body}\n'
        f'<div style="margin-top:24px;padding-top:16px;border-top:1px solid {BORDER};font-size:14px;'
        f'line-height:1.5;color:{MUTED};">{footer or "Sent by JobHuntPA."}</div>\n'
        '</td></tr></table>\n</td></tr></table>\n</body>\n</html>\n'
    )


def _h1(text: str) -> str:
    return f'<h1 style="margin:0 0 16px 0;font-size:24px;line-height:1.3;color:{TEXT};">{_e(text)}</h1>'


def _p(text: str) -> str:
    return f'<p style="margin:0 0 16px 0;">{_e(text)}</p>'


def _link(url: str, text: str) -> str:
    return f'<a href="{_e(url)}" style="color:{LINK};text-decoration:underline;">{_e(text)}</a>'


def _action(url: str, text: str) -> str:
    """The main link of a message, with the full address below it for clients that don't show links well."""
    return (f'<p style="margin:0 0 16px 0;font-size:18px;"><strong>{_link(url, text)}</strong></p>'
            f'<p style="margin:0 0 16px 0;font-size:14px;color:{MUTED};">If the link does not open, copy this address '
            f'into your browser: <span style="word-break:break-all;">{_e(url)}</span></p>')


def _when(expires: Any) -> str:
    if isinstance(expires, datetime):
        return expires.strftime("%d %B %Y, %H:%M UTC") if expires.tzinfo else expires.strftime("%d %B %Y, %H:%M")
    return str(expires)


def _simple(title: str, intro: str, link: Optional[tuple[str, str]], after: Iterable[str]) -> tuple[str, str]:
    """(text, html) for a message with a heading, an intro, an optional action link and closing paragraphs."""
    after = list(after)
    text = [title, "", intro]
    body = [_h1(title), _p(intro)]
    if link:
        text += ["", f"{link[1]}: {link[0]}"]
        body.append(_action(link[0], link[1]))
    for para in after:
        text += ["", para]
        body.append(_p(para))
    return "\n".join(text) + "\n", _page(title, intro, "\n".join(body))


# -- account messages --------------------------------------------------------------------------------------------

def password_reset(token: str, expires: Any, *, base_url: Optional[str] = None) -> Template:
    """A password reset: a link carrying ``token`` and the time (or text, e.g. "1 hour") it stops working."""
    url = f"{_base(base_url)}/reset-password.html#token={quote(token, safe='')}"
    subject = "Reset your JobHuntPA password"
    text, html = _simple(
        "Reset your password",
        "Someone asked to reset the password for your JobHuntPA account. If that was you, use the link below.",
        (url, "Choose a new password"),
        [f"The link works once and expires {_when(expires)}.",
         "If you did not ask for this, ignore this email. Your password stays the same."])
    return subject, text, html, {"Auto-Submitted": "auto-generated"}


def invite(token: str, *, inviter: Optional[str] = None, expires: Any = None, base_url: Optional[str] = None) -> Template:
    """An invitation to create an account; ``inviter`` is a display name (optional)."""
    url = f"{_base(base_url)}/signup.html?invite={quote(token, safe='')}"
    who = f"{inviter} has invited you" if inviter else "You have been invited"
    after = [f"The invitation expires {_when(expires)}."] if expires else []
    after.append("If you were not expecting this, you can ignore this email.")
    text, html = _simple(
        "You are invited to JobHuntPA",
        f"{who} to JobHuntPA, an assistant that finds, scores and tracks job postings for you.",
        (url, "Accept the invitation and create your account"), after)
    return "You are invited to JobHuntPA", text, html, {"Auto-Submitted": "auto-generated"}


def verify_email(token: str, *, expires: Any = None, change: bool = False, base_url: Optional[str] = None) -> Template:
    """Confirm an email address (new account, or with ``change`` a change of address: someone signed in to an account
    asked to use this address for it). The link opens a page that asks before it confirms anything."""
    url = f"{_base(base_url)}/verify-email.html#token={quote(token, safe='')}"
    after = [f"The link expires {_when(expires)}."] if expires else []
    if change:
        intro = ("Someone signed in to a JobHuntPA account asked to use this email address for it, instead of the "
                 "address it has now. If that was you, open the link and confirm.")
        after.append("If you did not ask for this, ignore this email. Nothing changes unless you open the link and "
                     "confirm.")
    else:
        intro = "Confirm this address so JobHuntPA can send you password resets and your job digest."
        after.append("If you did not create or change a JobHuntPA account with this address, ignore this email.")
    text, html = _simple("Confirm your email address", intro, (url, "Confirm my email address"), after)
    return "Confirm your email address for JobHuntPA", text, html, {"Auto-Submitted": "auto-generated"}


SECURITY_EVENTS = {
    "password_changed": ("Your JobHuntPA password was changed",
                         "The password for your JobHuntPA account was just changed.",
                         "If you made this change, nothing more is needed."),
    "mfa_disabled_by_admin": ("Two-step sign-in was turned off on your JobHuntPA account",
                              "An administrator turned off two-step sign-in (MFA) for your JobHuntPA account.",
                              "You can turn it on again in your account settings."),
    "recovery_codes_replaced": ("New recovery codes were made for your JobHuntPA account",
                               "New recovery codes were just made for your JobHuntPA account. The old ones no "
                               "longer work.",
                               "If you made them, nothing more is needed."),
    "email_change_started": ("A change of email address was started on your JobHuntPA account",
                             "Someone signed in to your JobHuntPA account asked to change its email address to "
                             "another one. Nothing has changed yet: it changes only when the link sent to the new "
                             "address is opened.",
                             "If you asked for this, nothing more is needed."),
    "mail_route_changed": ("The mail server or public address of your JobHuntPA was changed",
                          "An admin changed where JobHuntPA sends its email: the mail server or the public address "
                          "the links in emails point to. This notice went through the old server. For the next 24 "
                          "hours no admin can turn off anyone's two-step sign-in or send them a reset link.",
                          "If you or another admin made this change, nothing more is needed."),
    "email_changed": ("The email address on your JobHuntPA account was changed",
                      "The email address of your JobHuntPA account was just changed to a different one. This notice "
                      "goes to the old address.",
                      "If you made this change, nothing more is needed."),
}


def security_notice(event: str, *, when: Any = None, base_url: Optional[str] = None) -> Template:
    """A security notice. ``event`` is one of ``SECURITY_EVENTS``."""
    if event not in SECURITY_EVENTS:
        raise ValueError(f"Unknown security event: {event}")
    subject, intro, note = SECURITY_EVENTS[event]
    after = [f"When: {_when(when)}."] if when else []
    after += [note, "If this was not you, reset your password straight away and contact the person who runs your "
                    "JobHuntPA."]
    link = None
    base = base_url or mailer.public_base_url()
    if base:
        link = (f"{base.rstrip('/')}/", "Open JobHuntPA")
    text, html = _simple(subject, intro, link, after)
    return subject, text, html, {"Auto-Submitted": "auto-generated"}


def smtp_test_message(*, base_url: Optional[str] = None) -> Template:
    """What an admin gets from "Send a test email": proof that mail leaves this server."""
    base = base_url or mailer.public_base_url()
    link = (f"{base.rstrip('/')}/", "Open JobHuntPA") if base else None
    text, html = _simple(
        "Email is working",
        "This is a test message from your JobHuntPA. If you can read it, JobHuntPA can send password resets, "
        "invitations and security notices.",
        link, ["You asked for this test in the admin panel. Nothing else is needed."])
    return "JobHuntPA test email", text, html, {"Auto-Submitted": "auto-generated"}


# -- the job digest ----------------------------------------------------------------------------------------------

def _money(n: Any) -> Optional[str]:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n >= 1000:
        k = n / 1000
        return f"${k:.0f}k" if abs(k - round(k)) < 0.05 else f"${k:.1f}k"
    return f"${n:.0f}"


def _salary(job: dict) -> Optional[str]:
    lo, hi = _money(job.get("salary_min")), _money(job.get("salary_max"))
    if lo and hi:
        return lo if lo == hi else f"{lo}–{hi}"
    if lo or hi:
        return (f"From {lo}" if lo else f"Up to {hi}")
    return _clean(job.get("salary_text"))


def _clean(value: Any, limit: int = 600) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _http_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    return value.strip() if parts.scheme in ("http", "https") and parts.netloc else None


def _score(value: Any) -> Optional[int]:
    try:
        n = round(float(value))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, n))


def _count(value: Any) -> Optional[int]:
    if isinstance(value, (list, tuple)):
        return len(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def source_label(source: Any) -> Optional[str]:
    """Where an ad was listed, in words ("SEEK"): the same rule as ``sourceLabel`` in frontend/common.js."""
    s = str(source or "").strip()
    if not s:
        return None
    if s == "seek":
        return "SEEK"
    if s == "import":
        return "imported page"
    if s == "browser":
        return "browser extension"
    kind, sep, rest = s.partition(":")
    if not sep:
        return _clean(s, 80)
    if kind == "apsjobs":
        return "APSJobs"
    if kind in ("site", "generic"):
        return _clean(rest, 80)
    return _clean(f"{rest.split('/')[0]} ({kind[:1].upper()}{kind[1:]})", 80)


def digest_card(job: dict, base: str) -> dict:
    """The displayable facts of one job: a dict of ready text (a field that is missing is None), and the links."""
    mode = str(job.get("work_mode") or "").strip().lower()
    emp = normalise_employment_type(job.get("employment_type"))
    days = _count(job.get("office_days"))
    jid = job.get("id")
    link = None
    if jid is not None and str(jid).strip():
        link = f"{base}/job.html?id={quote(str(jid), safe='')}"
        if job.get("workspace_id") is not None:
            link += f"&ws={quote(str(job['workspace_id']), safe='')}"
    gaps = _count(job["gap_count"]) if "gap_count" in job else _count(job.get("gaps"))
    return {
        "title": _clean(job.get("title"), 200) or "Untitled job",
        "company": _clean(job.get("company"), 200),
        "salary": _salary(job),
        "location": _clean(job.get("office_text"), 200) or _clean(job.get("location_text"), 200),
        "office_days": _plural(days, "day in the office", "days in the office") if days else None,
        "work_mode": WORK_MODE_LABELS.get(mode),
        "employment": EMPLOYMENT_LABELS.get(emp) if emp else None,
        "score": _score(job.get("fit_score")),
        "summary": _clean(job.get("fit_summary")),
        "gaps": None if gaps is None else _plural(gaps, "gap", "gaps"),
        "open_url": link,
        "original_url": _http_url(job.get("url")),
        "source": source_label(job.get("source")),
        "workspace": _clean(job.get("workspace_name"), 100),
        "ghost": f"Possible ghost job: {reason}" if (reason := _clean(job.get("ghost_reason"), 160)) else None,
    }


def _frequency(every_days: int) -> str:
    return "every day" if every_days == 1 else f"every {every_days} days"


def job_digest(jobs: list[dict], *, keys: ServerKeys, unsubscribe_id: str, every_days: int = 1,
               more_count: int = 0, name: Optional[str] = None, base_url: Optional[str] = None) -> Template:
    """The job digest. ``jobs`` are plain dicts with the keys id, title, company, salary_min, salary_max,
    salary_text, office_text, location_text, office_days, work_mode, employment_type, fit_score, fit_summary,
    gap_count (or gaps: a list or a number), url, ghost_reason (the first sign that the ad may not be a current
    vacancy, backend/ghost.py; a line "Possible ghost job: ..." is added when set), and workspace_id (optional).
    Any may be missing.
    ``unsubscribe_id`` (letters, digits, - and _) is signed with ``keys``; ``more_count`` says how many more jobs
    were left out ("and 23 more in JobHuntPA")."""
    base = _base(base_url)
    every_days = max(1, int(every_days))
    token = keys.sign("unsub", unsubscribe_id)
    unsub = f"{base}/unsubscribe?token={quote(token, safe='')}"
    cards = [digest_card(j, base) for j in jobs if isinstance(j, dict)]
    n = len(cards) + max(0, int(more_count))
    heading = f"{_plural(n, 'new job', 'new jobs')} for you"
    subject = f"JobHuntPA: {heading}"
    intro = (f"Hi {name}, here " if name else "Here ") + (
        "is the job JobHuntPA found since your last digest." if n == 1
        else "are the jobs JobHuntPA found since your last digest, best fit first.")
    freq = (f"You get this digest {_frequency(every_days)}. Change how often, or turn it off, in JobHuntPA under "
            "Settings > Job digest emails.")

    text = [heading, "", intro]
    html = [_h1(heading), _p(intro)]
    for c in cards:
        t, h = _card_parts(c)
        text += ["", "-" * 40, *t]
        html.append(h)
    if more_count > 0:
        more = f"And {more_count} more in JobHuntPA."
        text += ["", more]
        html.append(_p(more))
    text += ["", "-" * 40, freq, f"Unsubscribe from this digest: {unsub}"]
    footer = (f'<p style="margin:0 0 8px 0;">{_e(freq)}</p>'
              f'<p style="margin:0;">{_link(unsub, "Unsubscribe from this digest")}</p>')
    headers = {
        "List-Unsubscribe": f"<{unsub}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "Auto-Submitted": "auto-generated",
    }
    return subject, "\n".join(text) + "\n", _page(subject, intro, "\n".join(html), footer), headers


def _card_parts(c: dict) -> tuple[list[str], str]:
    """(text lines, html) for one job card."""
    meta = [("Workspace", c["workspace"]), ("Company", c["company"]), ("Salary", c["salary"]), ("Location", c["location"]),
            ("Office days", c["office_days"]), ("Work type", c["work_mode"]), ("Employment", c["employment"]),
            ("Fit score", None if c["score"] is None else f"{c['score']} out of 100"), ("Gaps", c["gaps"])]
    meta = [(k, v) for k, v in meta if v]
    lines = [c["title"]] + [f"{k}: {v}" for k, v in meta]
    if c["summary"]:
        lines += ["", c["summary"]]
    if c["ghost"]:
        lines += ["", c["ghost"]]
    if c["open_url"]:
        lines += ["", f"Open in JobHuntPA: {c['open_url']}"]
    if c["original_url"]:
        where = f" on {c['source']}" if c["source"] else ""
        lines += [f"Original posting{where}: {c['original_url']}"]

    rows = "".join(
        f'<tr><th scope="row" align="left" style="padding:2px 12px 2px 0;font-weight:bold;color:{MUTED};'
        f'vertical-align:top;white-space:nowrap;">{_e(k)}</th>'
        f'<td style="padding:2px 0;color:{TEXT};">{_e(v)}</td></tr>' for k, v in meta)
    links = []
    if c["open_url"]:
        links.append(f'<strong>{_link(c["open_url"], "Open " + c["title"] + " in JobHuntPA")}</strong>')
    if c["original_url"]:
        where = f" on {c['source']}" if c["source"] else ""
        links.append(_link(c["original_url"], f"View the original posting for {c['title']}{where}"))
    html = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:0 0 16px 0;background-color:{CARD};border:1px solid {BORDER};">'
        '<tr><td style="padding:16px;">'
        f'<h2 style="margin:0 0 8px 0;font-size:18px;line-height:1.3;color:{TEXT};">{_e(c["title"])}</h2>'
        + (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 8px 0;">'
           f'{rows}</table>' if rows else '')
        + (f'<p style="margin:0 0 12px 0;">{_e(c["summary"])}</p>' if c["summary"] else '')
        + (f'<p style="margin:0 0 12px 0;">{_e(c["ghost"])}</p>' if c["ghost"] else '')
        + "".join(f'<p style="margin:0 0 4px 0;">{x}</p>' for x in links)
        + '</td></tr></table>')
    return lines, html
