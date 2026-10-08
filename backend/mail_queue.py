"""Sending email from one background thread (accounts design §9 and §16), so a request never waits on SMTP.

``send`` and ``submit`` put work on a single-thread executor and return at once. The work runs in order, one message
at a time, so a slow mail server delays later messages but never a request. Nothing here logs an address, a subject
or a body: the log has the account's id (when the caller gives one), whether the message went, and an exception's
class name.

Email is *on* only when SMTP and the public address are both set (``enabled``): every email feature checks it and
says so plainly when it is off (``OFF_TEXT``). The tests capture messages with ``mailer.capture()`` and wait for the
queue with ``drain``.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Callable, Optional

from backend import mailer

log = logging.getLogger(__name__)

MAX_WAITING = 200  # messages waiting for the mail server; more are dropped (a flood, or a server that is down)
NOTICE_RESERVE = 50  # the last of those places are for security notices (``priority``): links anyone can ask for stop first
OFF_TEXT = ("Email isn't set up on this JobHuntPA yet. An admin sets it up in the admin panel (Server): the mail "
            "server and the public address.")

DEFAULT_SEND_S = 0.3  # what a decoy job lasts until some real sends have been timed
MAX_DECOY_S = 10.0

_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None
_waiting: set[Future] = set()
_send_times: deque = deque(maxlen=20)  # how long the last real sends took (seconds)


def missing() -> list[str]:
    """What an admin still has to set for email to work: ``smtp`` and/or ``public_url`` (empty when it's on)."""
    out = []
    if mailer.get_config() is None:
        out.append("smtp")
    if mailer.public_base_url() is None:
        out.append("public_url")
    return out


def enabled() -> bool:
    """Whether email features are on: SMTP is set up and so is the public address that links point to."""
    return not missing()


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jobhunt-mail")
        return _executor


def _limit(priority: bool) -> int:
    return MAX_WAITING if priority else MAX_WAITING - NOTICE_RESERVE


def submit(fn: Callable[..., Any], *args: Any, priority: bool = False, **kwargs: Any) -> Optional[Future]:
    """Run ``fn`` on the mail thread, after the work already waiting. Returns its Future, or None when too much is
    waiting: ``priority`` jobs (security notices) may use the places the others can't, so someone who can fill the
    queue with reset and sign-up links still can't keep an owner's "password changed" notice out. ``fn``'s
    exceptions are logged (class name only) and never reach the caller."""
    with _lock:
        if len(_waiting) >= _limit(priority):
            log.warning("Email work dropped: %d jobs are waiting for the mail server", len(_waiting))
            return None

    def run() -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001  (nothing may escape: the thread must go on with the next job)
            log.warning("Email work failed: %s", exc.__class__.__name__)
            return None

    future = _pool().submit(run)
    with _lock:
        _waiting.add(future)
    future.add_done_callback(_forget)
    return future


def has_room(priority: bool = False) -> bool:
    """Whether ``submit`` would take another job now. Callers that change something before sending (a new link
    replaces the old one) look first, so a full queue doesn't cost the person the link they have."""
    with _lock:
        return len(_waiting) < _limit(priority)


def _forget(future: Future) -> None:
    with _lock:
        _waiting.discard(future)


def deliver_now(to: str, template: tuple, user_id: Optional[int] = None) -> mailer.SendResult:
    """Send now, on the calling thread: for work already running on the mail thread (``submit``). Request code uses
    ``send``."""
    subject, text, html, headers = template
    began = time.monotonic()
    result = mailer.send(to, subject, text, html, headers)
    with _lock:
        _send_times.append(time.monotonic() - began)
    log.info("Email %s%s", result.status, f" for user {user_id}" if user_id is not None else "")
    return result


def typical_send_seconds() -> float:
    """The average of the last 20 sends (``DEFAULT_SEND_S`` before there are any), at most ``MAX_DECOY_S``."""
    with _lock:
        times = list(_send_times)
    return min(MAX_DECOY_S, sum(times) / len(times)) if times else DEFAULT_SEND_S


def decoy() -> None:
    """A job that keeps the mail thread busy as long as a typical send. The forgot-password form queues one for an
    address that has no account (or that no link is sent to), because a real link takes the thread for a send: with
    nothing queued, the next email to anyone would come sooner, and timing it would show which addresses have an
    account (accounts design §2.5)."""
    time.sleep(typical_send_seconds())


def send(to: str, template: tuple, *, user_id: Optional[int] = None, priority: bool = False) -> Optional[Future]:
    """Queue one message (``template`` is what ``backend.email_templates`` returns). The Future's result is a
    ``mailer.SendResult``. None when email is off or too much is waiting: the caller decides whether that matters.
    ``priority`` is for security notices (see ``submit``)."""
    if not enabled():
        return None
    return submit(deliver_now, to, template, user_id, priority=priority)


def drain(timeout: float = 15.0) -> bool:
    """Wait until everything queued so far has run (tests, and shutting down). False if it took longer than
    ``timeout`` seconds."""
    with _lock:
        pending = list(_waiting)
    done, not_done = wait(pending, timeout=timeout)
    return not not_done
