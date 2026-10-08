"""Email settings for the admin panel's Server section (accounts design §9). Admins only, for reading as well as writing.

    GET  /api/settings/email         {enabled, missing, text, host, port, security, user, from_address, public_base_url,
                                      password: {set}, security_modes, default_ports}  (never the password)
    PUT  /api/settings/email         (recent sign-in, when it changes anything: 403 ``reauth_required``)
                                     {host?, port?, security?, user?, password?, from_address?, public_base_url?}:
                                     an omitted (or null) field is unchanged, "" clears it; 422 {detail, code, field}
                                     for a value that isn't allowed, and nothing is saved then. Answers like GET.
    POST /api/settings/email/test    sends a test email to the signed-in admin's own address (from their own details):
                                     202 {ok, status: "sending"}; 409 ``email_not_set_up`` / ``no_address``
    GET  /api/settings/email/test    {status: "none" | "sending" | "sent" | "failed", error?}: how the last test went
                                     (``error`` is the exception's class name, e.g. "SMTPAuthenticationError")

Email is on only while SMTP and the public address are both set (``enabled``); until then every email feature says
so. Values are validated by ``app_settings.ENV_RULES`` (no line breaks or quotes: a value can't start a new line
or a second setting, nor a header) and saved to .env like the other server settings, and take effect at once.

**The SMTP password.** The form removes every space and tab (Gmail shows its app passwords in groups of four, and they
work without the gaps), then saves the result as ``SMTP_PASSWORD_B64`` (base64url), which holds any printable ASCII
character, quotes and "#" included, and clears ``SMTP_PASSWORD``. A hand-written ``SMTP_PASSWORD`` keeps working until
the form saves a new one. The password is never sent back, only whether one is set. Changing the mail server needs the
password typed again, so a saved password can't be redirected to another server.

**From.** The form takes a plain address; the display name is fixed as "JobHuntPA".

**Encryption.** ``security: none`` (plain text) is only for a mail server on this computer (localhost or 127.x.x.x).
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend import account_email, admin_key, app_settings, email_templates, mail_queue, mailer, ratelimit
from backend.db import get_db
from backend.sessions import AuthProblem, AuthUser, current_user, require_admin, require_recent_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/email", tags=["settings"], dependencies=[Depends(require_admin)])

DEFAULT_PORTS = {"starttls": "587", "ssl": "465", "none": "25"}
FROM_NAME = "JobHuntPA"
_LOCAL_HOST = re.compile(r"localhost|127(?:\.[0-9]{1,3}){3}")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _saved_password() -> bool:
    return bool(_env("SMTP_PASSWORD") or _env("SMTP_PASSWORD_B64"))


def state() -> dict:
    sender = mailer.split_from(_env("SMTP_FROM"))
    off = mail_queue.missing()
    if not off:
        text = "Email is on."
    elif off == ["smtp"]:
        text = "Email is off: set the mail server and the From address."
    elif off == ["public_url"]:
        text = "Email is off: set the public address, which the links in emails point to."
    else:
        text = "Email is off: set the mail server, the From address and the public address."
    return {
        "enabled": not off, "missing": off, "text": text,
        "host": _env("SMTP_HOST"), "port": _env("SMTP_PORT"), "security": _env("SMTP_SECURITY").lower() or mailer.DEFAULT_SECURITY,
        "user": _env("SMTP_USER"), "from_address": sender[1] if sender else _env("SMTP_FROM"),
        "public_base_url": _env("PUBLIC_BASE_URL"), "password": {"set": _saved_password()},
        "security_modes": list(mailer.SECURITY_MODES), "default_ports": DEFAULT_PORTS,
    }


@router.get("")
def get_email_settings():
    return state()


class EmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown field is a 422, not silently dropped
    host: Optional[str] = None
    port: Optional[str] = None
    security: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    from_address: Optional[str] = None
    public_base_url: Optional[str] = None


def _s(value: str) -> str:
    """Spaces and tabs around a pasted value go; a line break doesn't (it is refused by the rules below)."""
    return value.strip(" \t")


def _bad(field: str, detail: str) -> AuthProblem:
    return AuthProblem(422, "invalid", detail, field=field)


def _checked(field: str, value: str, rule, detail: str) -> str:
    if value and not rule(value):  # no echo of the value in the message
        raise _bad(field, detail)
    return value


def _is_local(host: str) -> bool:
    return _LOCAL_HOST.fullmatch(host.lower()) is not None


@router.put("")
def put_email_settings(body: EmailIn, admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Saves what is sent and leaves the rest. Everything is checked before anything is written."""
    updates: dict[str, str] = {}
    if body.host is not None:
        updates["SMTP_HOST"] = _checked("host", _s(body.host), mailer.valid_host,
                                        "The mail server must be a name like smtp.example.com, without http://, a "
                                        "port or spaces.")
    if body.port is not None:
        updates["SMTP_PORT"] = _checked("port", _s(body.port), mailer.valid_port,
                                        "The port must be a number from 1 to 65535, such as 587.")
    if body.security is not None:
        updates["SMTP_SECURITY"] = _checked("security", _s(body.security).lower(), mailer.valid_security,
                                            "Choose starttls, ssl or none.")
    if body.user is not None:
        updates["SMTP_USER"] = _checked("user", _s(body.user), mailer.valid_user,
                                        "The user name may use letters, digits and . _ % + @ - only (up to 254 "
                                        "characters).")
    if body.from_address is not None:
        address = _s(body.from_address)
        if address and not (mailer.ADDRESS_RE.fullmatch(address) and len(address) <= 254):
            raise _bad("from_address", "Enter one email address like jobs@example.com.")
        updates["SMTP_FROM"] = f"{FROM_NAME} <{address}>" if address else ""
    if body.public_base_url is not None:
        base = _s(body.public_base_url)
        base = base[:-1] if base.endswith("/") and len(base) > 1 else base
        help_ = ("The public address must look like https://jobs.example.com: https (http only for localhost), no "
                 "login details, query or #.")
        updates["PUBLIC_BASE_URL"] = _checked("public_base_url", base, mailer.valid_public_base_url, help_)
    if body.password is not None:
        if body.password:
            try:
                updates["SMTP_PASSWORD_B64"] = mailer.encode_password(mailer.clean_password(body.password))
            except ValueError as exc:
                raise _bad("password", str(exc)) from None
            updates["SMTP_PASSWORD"] = ""  # the new one replaces a hand-written one
        else:
            updates["SMTP_PASSWORD_B64"] = updates["SMTP_PASSWORD"] = ""
    host = (updates["SMTP_HOST"] if "SMTP_HOST" in updates else _env("SMTP_HOST")).lower()
    security = (updates["SMTP_SECURITY"] if "SMTP_SECURITY" in updates else _env("SMTP_SECURITY")).lower()
    if security == "none" and host and not _is_local(host):
        raise _bad("security", "Without encryption, mail can only go to a mail server on this computer. Choose "
                               "starttls or ssl.")
    if updates.get("SMTP_HOST") and host != _env("SMTP_HOST").lower() and _saved_password() and body.password is None:
        raise _bad("password", "This changes the mail server, so enter its password again in the same save. The "
                               "password saved for the old server is never sent anywhere else.")
    for name, value in updates.items():  # the same rules as .env (no line break, quote or "#" in any value)
        if value and not app_settings.ENV_RULES[name](value):
            raise _bad({"SMTP_FROM": "from_address", "SMTP_PASSWORD_B64": "password"}.get(name, name.lower()),
                       "That value isn't allowed.")
    if updates:
        require_recent_auth(admin)  # these settings decide where every reset link points and which server relays it
        app_settings._save(updates)
        names = {"SMTP_HOST": "email_host", "SMTP_PORT": "email_port", "SMTP_SECURITY": "email_security",
                 "SMTP_USER": "email_user", "SMTP_FROM": "email_from", "PUBLIC_BASE_URL": "public_url",
                 "SMTP_PASSWORD_B64": "email_password"}
        admin_key.audit_settings(db, admin.id, {names[var]: bool(value) for var, value in updates.items()
                                                if var in names})
    return state()


# ---------------------------------------------------------------------------
# "Send a test email"
# ---------------------------------------------------------------------------

_tests: dict[int, dict] = {}  # admin id -> {"status": ..., "error"?: class name}; kept in memory, never an address
_tests_lock = threading.Lock()


def _remember(admin_id: int, value: dict) -> None:
    with _tests_lock:
        _tests[admin_id] = value


def _run_test(admin_id: int, address: str) -> None:
    """On the mail thread: send, and note how it went."""
    result = mail_queue.deliver_now(address, email_templates.smtp_test_message(), admin_id)
    _remember(admin_id, {"status": "sent"} if result.ok else {"status": "failed", "error": result.error or "failed"})


@router.post("/test", status_code=202)
def send_test(admin: AuthUser = Depends(current_user), db: Session = Depends(get_db)):
    """Queues a test message to the admin's own address; ``GET`` says how it went."""
    if not mail_queue.enabled():
        raise AuthProblem(409, "email_not_set_up", state()["text"] + " Save those first, then try again.")
    wait = ratelimit.EMAIL_TEST.hit(admin.id)
    if wait is not None:
        raise AuthProblem(429, "rate_limited", f"Wait {ratelimit.wait_text(wait)} before sending another test.",
                          headers={"Retry-After": ratelimit.retry_after(wait)})
    address = account_email.own_address(db, admin.id)
    if not address:
        raise AuthProblem(409, "no_address", "Your account has no email address saved to send the test to.")
    _remember(admin.id, {"status": "sending"})
    if mail_queue.submit(_run_test, admin.id, address) is None:
        _remember(admin.id, {"status": "failed", "error": "busy"})
    return {"ok": True, "status": "sending"}


@router.get("/test")
def test_result(admin: AuthUser = Depends(current_user)):
    with _tests_lock:
        return dict(_tests.get(admin.id) or {"status": "none"})
