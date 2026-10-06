"""Rate limits and sign-in lockout (accounts design, section 3.6). In memory: the app is a single uvicorn process.

* **Per client address** (``scope["client"]``, which uvicorn's proxy headers take from X-Forwarded-For when the
  request comes through a proxy on this computer): the sign-in endpoints allow 10 requests in 5 minutes and 60 in
  an hour (``AUTH``).
* **Per account:** after 5 wrong passwords in a row, the next try must wait 30 seconds, doubling with each further
  failure up to 15 minutes (``lockout_seconds``). There is no permanent lockout, which would let anyone lock a
  person out for good. Accounts are counted in ``users.failed_logins`` / ``users.locked_until``; an email address
  with no account is counted here, in memory (``unknown_emails``), so the answers can't tell the two apart.

Refusals are 429 with ``Retry-After`` and a plain message. There is no CAPTCHA (WCAG 3.3.8).
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from typing import Hashable, Optional

LOCK_AFTER = 5          # failures in a row before the first wait
FIRST_WAIT_S = 30
MAX_WAIT_S = 15 * 60
MAX_KEYS = 10_000       # remembered addresses or emails; the oldest are forgotten first


def lockout_seconds(failures: int) -> int:
    """How long to wait after ``failures`` wrong passwords in a row: 0 below 5, then 30 s, 60 s, 120 s ... 15 min."""
    if failures < LOCK_AFTER:
        return 0
    return min(MAX_WAIT_S, FIRST_WAIT_S * 2 ** min(failures - LOCK_AFTER, 16))


def retry_after(seconds: float) -> str:
    """A Retry-After value: whole seconds, at least 1."""
    return str(max(1, math.ceil(seconds)))


def wait_text(seconds: float) -> str:
    seconds = max(1, math.ceil(seconds))
    if seconds < 90:
        return f"{seconds} seconds"
    return f"{math.ceil(seconds / 60)} minutes"


class Limiter:
    """A sliding-window limit per key: ``rules`` is ``[(how many, per seconds), ...]``, all of which apply."""

    def __init__(self, rules: list[tuple[int, float]]):
        self.rules = rules
        self._longest = max(window for _, window in rules)
        self._hits: "OrderedDict[Hashable, deque]" = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: Hashable, now: Optional[float] = None) -> Optional[float]:
        """Count one request for ``key``. None when it is allowed, else how many seconds until it would be (and the
        refused request isn't counted)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            times = self._hits.pop(key, None) or deque()
            while times and times[0] <= now - self._longest:
                times.popleft()
            wait = 0.0
            for limit, window in self.rules:
                recent = [t for t in times if t > now - window]
                if len(recent) >= limit:
                    wait = max(wait, recent[-limit] + window - now)
            if wait <= 0:
                times.append(now)
            if times:
                self._hits[key] = times  # most recently used last
            while len(self._hits) > MAX_KEYS:
                self._hits.popitem(last=False)
            return wait if wait > 0 else None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class FailureCounter:
    """Failures in a row per key, with the lockout above: for email addresses that have no account."""

    def __init__(self):
        self._state: "OrderedDict[Hashable, tuple[int, float]]" = OrderedDict()  # key -> (failures, locked until)
        self._lock = threading.Lock()

    def locked_for(self, key: Hashable, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            _, until = self._state.get(key, (0, 0.0))
            return max(0.0, until - now)

    def fail(self, key: Hashable, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            failures, _ = self._state.pop(key, (0, 0.0))
            failures += 1
            self._state[key] = (failures, now + lockout_seconds(failures))
            while len(self._state) > MAX_KEYS:
                self._state.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


# The sign-in endpoints (login, first run; later sign-up, forgot, reset and recover), per client address.
AUTH = Limiter([(10, 5 * 60), (60, 60 * 60)])
# Wrong passwords for email addresses that have no account.
unknown_emails = FailureCounter()


def reset_all() -> None:
    """Forget every count (tests)."""
    AUTH.reset()
    unknown_emails.reset()
