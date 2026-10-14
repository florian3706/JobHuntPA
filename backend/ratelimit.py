"""Rate limits and sign-in lockout (accounts design, section 3.6). In memory: the app is a single uvicorn process.

* **Per client address** (``scope["client"]``, which uvicorn's proxy headers take from X-Forwarded-For when the
  request comes through a proxy on this computer): the sign-in endpoints allow 10 requests in 5 minutes and 60 in
  an hour (``AUTH``).
* **Wrong passwords and codes** (``GuessGuard``), counted per account **and where the try came from**: 5 wrong ones
  in a row from one network address (or one browser that has signed in before: see backend/lockout.py) make *that
  source* wait 30 seconds, doubling with each further failure up to 15 minutes (``lockout_seconds``). Someone who
  only knows an address can therefore lock out their own network address, never the person it belongs to. On top
  of that, an account-wide ceiling (``ACCOUNT_CEILING``: 100 wrong tries in an hour from anywhere) slows a guesser
  who spreads tries over many addresses; it applies to browsers that haven't signed in to the account before, and
  not to the owner's own. There is no permanent lockout, which would let anyone lock a person out for good.
  A try is counted *before* the slow password check and given back if it was right, so tries sent at the same moment
  can't all slip past the limit. An email address with no account is counted the same way in a second guard
  (``unknown_guesses``), so the answers can't tell the two apart.

Refusals are 429 with ``Retry-After`` and a plain message. There is no CAPTCHA (WCAG 3.3.8).
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
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


def _whole_seconds(seconds: float) -> int:
    """``seconds`` rounded up to whole seconds, at least 1. A wait is the difference of two clock readings, so a
    30-second one can come out as 30.000000000000057: rounded to the microsecond first, it stays 30."""
    return max(1, math.ceil(round(seconds, 6)))


def retry_after(seconds: float) -> str:
    """A Retry-After value: whole seconds, at least 1."""
    return str(_whole_seconds(seconds))


def wait_text(seconds: float) -> str:
    seconds = _whole_seconds(seconds)
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

    def clear(self, key: Hashable) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


ACCOUNT_CEILING = (100, 60 * 60)  # wrong tries from any source in an hour, before a browser new to the account waits
MAX_SOURCES = 200                 # sources remembered per account; the oldest are forgotten first
FORGET_AFTER_S = 24 * 60 * 60     # a source's failures in a row are forgotten after a day without a try


class Waiting(Exception):
    """The try is refused: ``wait`` seconds until it may be made. ``ceiling`` is True when the whole account is the
    reason (too many wrong tries from everywhere), False when this source has failed 5 times in a row."""

    def __init__(self, wait: float, ceiling: bool = False):
        super().__init__("waiting")
        self.wait, self.ceiling = wait, ceiling


@dataclass
class Ticket:
    """One counted try (``GuessGuard.begin``). Give it back with ``succeed`` when the password was right, or ``refund``
    when it was right but a code is still to come; do nothing when it was wrong (it stays counted)."""
    guard: "GuessGuard" = field(repr=False)
    account: Hashable = field(repr=False)
    source: str = field(repr=False)
    stamp: float
    before: tuple[int, float, float]
    after: tuple[int, float, float]

    @property
    def failures(self) -> int:
        """Wrong tries in a row from this source, this one included."""
        return self.after[0]

    def succeed(self) -> None:
        self.guard.succeed(self)

    def refund(self) -> None:
        self.guard.refund(self)


class _Account:
    def __init__(self):
        self.sources: "OrderedDict[str, list]" = OrderedDict()  # source -> [failures in a row, locked until, last try]
        self.tries: deque = deque()                              # when the tries still in the ceiling's window were made


class GuessGuard:
    """Wrong passwords and codes, per account and per source (a network address, or a browser that has signed in to
    the account before). A try is counted before the slow check (``begin``, one step under a lock), so tries sent at
    the same moment can't all get past the 5; a right one is given back (``succeed``, ``refund``). Keys are anything
    hashable: a user id, or the hash of an email address that has no account. ``ceiling`` is (how many, per seconds)
    wrong tries from any source before a source new to the account waits (``ACCOUNT_CEILING`` when not given)."""

    def __init__(self, ceiling: Optional[tuple[int, float]] = None):
        self._ceiling = ceiling
        self._accounts: "OrderedDict[Hashable, _Account]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def ceiling(self) -> tuple[int, float]:
        return self._ceiling or ACCOUNT_CEILING

    def begin(self, account: Hashable, source: str, *, known: bool = False, ceiling: bool = True,
              now: Optional[float] = None) -> Ticket:
        """Counts one try from ``source`` and returns its ticket, or raises ``Waiting`` (the try is then not counted).
        ``known``: the source is a browser that has signed in to the account before; it never meets the ceiling.
        ``ceiling`` False: the caller already holds a proof only the owner has (the password at the code step)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            acct = self._accounts.pop(account, None) or _Account()
            try:
                state = acct.sources.get(source)
                if state is not None and now - state[2] > FORGET_AFTER_S:
                    state = None
                if state is not None and state[1] > now:
                    raise Waiting(state[1] - now)
                limit, window = self.ceiling
                while acct.tries and acct.tries[0] <= now - window:
                    acct.tries.popleft()
                if ceiling and not known and len(acct.tries) >= limit:
                    raise Waiting(acct.tries[0] + window - now, ceiling=True)
                before = (state[0], state[1], state[2]) if state is not None else (0, 0.0, now)
                failures = before[0] + 1
                wait = lockout_seconds(failures)
                after = (failures, now + wait if wait else 0.0, now)
                acct.sources.pop(source, None)
                acct.sources[source] = list(after)
                while len(acct.sources) > MAX_SOURCES:
                    acct.sources.popitem(last=False)
                acct.tries.append(now)
                return Ticket(self, account, source, now, before, after)
            finally:
                if acct.sources or acct.tries:
                    self._accounts[account] = acct
                while len(self._accounts) > MAX_KEYS:
                    self._accounts.popitem(last=False)

    def succeed(self, ticket: Ticket) -> None:
        """The password was right: this source starts again from no wrong tries, and the try leaves the ceiling."""
        with self._lock:
            acct = self._accounts.get(ticket.account)
            if acct is None:
                return
            acct.sources.pop(ticket.source, None)
            self._drop_try(acct, ticket)

    def refund(self, ticket: Ticket) -> None:
        """The password was right but the code is still to come: the try is given back, nothing else forgiven."""
        with self._lock:
            acct = self._accounts.get(ticket.account)
            if acct is None:
                return
            state = acct.sources.get(ticket.source)
            if state is not None:
                if tuple(state) == ticket.after:
                    state[:] = list(ticket.before)
                else:  # other tries came in meanwhile: only this one is taken off
                    state[0] = max(0, state[0] - 1)
                    if not lockout_seconds(state[0]):
                        state[1] = 0.0
                if state[0] == 0:
                    acct.sources.pop(ticket.source, None)
            self._drop_try(acct, ticket)

    @staticmethod
    def _drop_try(acct: _Account, ticket: Ticket) -> None:
        try:
            acct.tries.remove(ticket.stamp)
        except ValueError:
            pass

    def waiting(self, account: Hashable, now: Optional[float] = None) -> float:
        """Seconds until some source of the account may try again, or the ceiling ends: 0 when nothing waits."""
        now = time.monotonic() if now is None else now
        with self._lock:
            acct = self._accounts.get(account)
            if acct is None:
                return 0.0
            wait = max([s[1] - now for s in acct.sources.values()] + [0.0])
            limit, window = self.ceiling
            recent = [t for t in acct.tries if t > now - window]
            if len(recent) >= limit:
                wait = max(wait, recent[0] + window - now)
            return wait

    def failures(self, account: Hashable) -> dict:
        """Wrong tries in a row per source (tests, and the log)."""
        with self._lock:
            acct = self._accounts.get(account)
            return {source: state[0] for source, state in acct.sources.items()} if acct else {}

    def clear(self, account: Hashable) -> None:
        """Forget every wrong try of the account (its password was changed or reset)."""
        with self._lock:
            self._accounts.pop(account, None)

    def reset(self) -> None:
        with self._lock:
            self._accounts.clear()


# Wrong passwords and codes for accounts, and for email addresses that have no account (a separate guard, so
# someone typing made-up addresses can't push real accounts' counts out; behaviour is the same, so the answers
# can't tell the two apart).
guesses = GuessGuard()
unknown_guesses = GuessGuard()


def forget_account(account: Hashable) -> None:
    """The account starts again with no wrong passwords or codes (its password changed or was reset)."""
    guesses.clear(account)


def forget_user(user_id: int) -> None:
    """Everything counted under a user id, for an account that is deleted (SQLite may give the id to the next one)."""
    forget_account(user_id)


# The sign-in endpoints (login, first run and recover), per client address.
AUTH = Limiter([(10, 5 * 60), (60, 60 * 60)])

# The first run's step 1, where the data lives (backend/data_folder.py), per client address: a few folders may be tried.
FOLDER = Limiter([(30, 5 * 60), (120, 60 * 60)])

# Browser-extension keys that don't work (unknown, revoked, expired, malformed), per client address: past this a key
# that doesn't work answers 429 instead of 401; one that works still gets in (backend/sessions.py).
BAD_KEY = Limiter([(20, 5 * 60), (100, 60 * 60)])


def reset_all() -> None:
    """Forget every count (tests)."""
    AUTH.reset()
    FOLDER.reset()
    BAD_KEY.reset()
    guesses.reset()
    unknown_guesses.reset()

