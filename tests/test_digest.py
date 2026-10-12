"""Job digest emails (accounts design §9, §13 item 2, §16; Phase J): what is new, when a digest is due, what it holds,
the unsubscribe link (GET changes nothing, one-click POST needs no session), the settings API, and ``first_run_id``.
SMTP is captured (``mailer.capture``); nothing touches a network.

Run:  python3 -m unittest tests.test_digest -v
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from tests import helpers  # noqa: E402  (sets the test environment first)
from tests.mail_helpers import BASE, MailTestCase, html_of, text_of  # noqa: E402

from backend import crypto, digest, mail_queue, mailer, membership, pool, schedule, tasks, users  # noqa: E402
from backend.adapters.base import Posting as Scraped  # noqa: E402
from backend.db import EmailPrefs, FitResult, Job, Posting, SearchRun, SearchSchedule, SessionLocal, User  # noqa: E402
from backend.filters import Criteria  # noqa: E402
from backend.keys import ServerKeys  # noqa: E402

UTC = timezone.utc
# Wednesday 8 October 2026, 11:30 in Sydney (daylight saving began on the 4th): a digest is due at once.
NOW = datetime(2026, 10, 8, 0, 30, tzinfo=UTC)
OPTED_IN = datetime(2026, 10, 5, 2, 0)           # naive UTC, like the database
RUN_AT = datetime(2026, 10, 6, 3, 0)             # a scheduled run, after opting in
TZ = {"NSW": ZoneInfo("Australia/Sydney"), "WA": ZoneInfo("Australia/Perth"), "NT": ZoneInfo("Australia/Darwin"),
      "QLD": ZoneInfo("Australia/Brisbane")}


def naive(aware: datetime) -> datetime:
    return aware.astimezone(UTC).replace(tzinfo=None)


class Links(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.links: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href", ""))


class Base(MailTestCase):
    def setUp(self):
        super().setUp()
        db = SessionLocal()
        try:  # other tests' accounts must not get a digest in this test's tick
            db.query(EmailPrefs).update({EmailPrefs.digest_enabled: False})
            db.commit()
        finally:
            db.close()

    # -- accounts and rows ----------------------------------------------------------------------------------------

    def user(self, *, digest: bool = True, every: int = 1, verified: bool = True, background: bool = True,
             name: str = "", state: str | None = None, opted_in: datetime | None = OPTED_IN) -> helpers.Account:
        acct = helpers.make_user(name=name)
        db = SessionLocal()
        try:
            db.get(User, acct.id).email_verified = verified
            prefs = db.get(EmailPrefs, acct.id)
            prefs.digest_enabled, prefs.digest_every_days = digest, every
            prefs.opted_in_at = opted_in if digest else None
            if state:
                db.add(SearchSchedule(id=acct.workspace_id, state=state))
            db.commit()
            if background:
                users.enable_background(db, acct.id, helpers.key_of(acct.email)[1])
        finally:
            db.close()
        return acct

    def prefs(self, acct) -> EmailPrefs:
        db = SessionLocal()
        try:
            row = db.get(EmailPrefs, acct.id)
            db.expunge(row)
            return row
        finally:
            db.close()

    def set_prefs(self, acct, **fields) -> None:
        db = SessionLocal()
        try:
            for k, v in fields.items():
                setattr(db.get(EmailPrefs, acct.id), k, v)
            db.commit()
        finally:
            db.close()

    def make_run(self, acct, ws: int | None = None, *, scheduled: bool = True, started: datetime = RUN_AT,
            state: str = "done") -> int:
        """A search run of ``ws`` (the account's own workspace by default), sealed under the account's key."""
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                run = SearchRun(workspace_id=ws or acct.workspace_id, kind="search", scheduled=scheduled, state=state,
                                started_at=started)
                db.add(run)
                db.commit()
                return run.id
            finally:
                db.close()

    _n = iter(range(1000, 10 ** 6))

    def job(self, acct, run_id: int | None, *, ws: int | None = None, score: int | None = 80, status: str = "ok",
            summary: str = "A strong match.", gaps: tuple = ("no cloud",), hidden: bool = False,
            excluded: str | None = None, closed: bool = False, duplicate_of: int | None = None, **fields) -> int:
        n = next(self._n)
        content = dict(title=f"Role {n}", company=f"Company {n}", location_text="Sydney NSW", work_mode="hybrid",
                       employment_type="full_time", office_days=3, salary_min=150000, salary_max=170000, source="seek")
        content.update(fields)
        now = datetime(2026, 10, 6, 1, 0)
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                posting = Posting(url=f"https://www.seek.com.au/job/{n}", detail_status="none", rev=0, first_seen=now,
                                  last_listed_at=now, closed_at=now if closed else None, **content)
                db.add(posting)
                db.flush()
                job = Job(workspace_id=ws or acct.workspace_id, origin="pool", status="to_review", posting=posting,
                          first_run_id=run_id, hidden=hidden, excluded_reason=excluded, duplicate_of=duplicate_of)
                db.add(job)
                db.flush()
                if score is not None or status != "ok":
                    db.add(FitResult(job_id=job.id, status=status, score=score if status == "ok" else None,
                                     evidence_json=json.dumps({"summary": summary, "gaps": list(gaps)})))
                db.commit()
                return job.id
            finally:
                db.close()

    def titles(self, msg) -> list[str]:
        return re.findall(r"^(Role \d+)$", text_of(msg), re.M)

    def digests_to(self, acct) -> list:
        return self.to(acct.email)

    def tick(self, now: datetime = NOW) -> int:
        return digest.tick(now)

    def token_of(self, msg) -> str:
        url = msg["List-Unsubscribe"].strip("<>")
        return parse_qs(urlsplit(url).query)["token"][0]


# ---------------------------------------------------------------------------
# What is new
# ---------------------------------------------------------------------------

class SelectionTests(Base):
    def new(self, acct, since: datetime = OPTED_IN) -> list[int]:
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                return sorted(j.id for j in digest.new_jobs(db, acct.id, since))
            finally:
                db.close()

    def test_only_jobs_from_scheduled_runs_after_the_cutoff_count(self):
        acct = self.user()
        fresh = self.job(acct, self.make_run(acct))
        self.job(acct, self.make_run(acct, scheduled=False))                              # started by hand
        self.job(acct, self.make_run(acct, started=OPTED_IN - timedelta(seconds=1)))      # before opting in
        self.job(acct, None)                                                       # made outside any run
        on_the_dot = self.job(acct, self.make_run(acct, started=OPTED_IN))
        self.assertEqual(self.new(acct), sorted([fresh, on_the_dot]))

    def test_merged_copies_are_left_out(self):
        acct = self.user()
        run = self.make_run(acct)
        kept = self.job(acct, run)
        self.job(acct, run, duplicate_of=kept)
        self.assertEqual(self.new(acct), [kept])

    def test_excluded_hidden_and_closed_jobs_are_decided_in_python(self):
        acct = self.user()
        run = self.make_run(acct)
        kept = self.job(acct, run)
        self.job(acct, run, excluded="Too far from home")
        self.job(acct, run, hidden=True)
        self.job(acct, run, closed=True)
        self.assertEqual(self.new(acct), [kept])
        # The sealed columns can't even be asked about in SQL (backend/crypto.py refuses): that is why.
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                with self.assertRaises(Exception):
                    db.query(Job.id).filter(Job.hidden.is_(False)).all()
                with self.assertRaises(Exception):
                    db.query(Job.id).filter(Job.excluded_reason.is_(None)).all()
            finally:
                db.close()

    def test_another_users_jobs_never_come_along(self):
        alice, bob = self.user(), self.user()
        mine = self.job(alice, self.make_run(alice))
        theirs = self.job(bob, self.make_run(bob))
        self.assertEqual(self.new(alice), [mine])
        self.assertEqual(self.new(bob), [theirs])
        # a run id that belongs to someone else's workspace is no run of this one
        stolen = self.job(alice, self.make_run(alice, bob.workspace_id))
        self.assertNotIn(stolen, self.new(alice))
        self.assertNotIn(stolen, self.new(bob))

    def test_best_fit_first_unscored_last_and_capped_at_60(self):
        acct = self.user()
        run = self.make_run(acct)
        ids = {s: self.job(acct, run, score=s) for s in (40, 95, 70)}
        unscored = self.job(acct, run, score=None)
        failed = self.job(acct, run, score=None, status="error")
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                found, more = digest.cards(db, digest.new_jobs(db, acct.id, OPTED_IN))
            finally:
                db.close()
        self.assertEqual([c["id"] for c in found[:3]], [ids[95], ids[70], ids[40]])
        self.assertEqual({c["id"] for c in found[3:]}, {unscored, failed})
        self.assertEqual(more, 0)
        for i in range(60):
            self.job(acct, run, score=i % 100)
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                found, more = digest.cards(db, digest.new_jobs(db, acct.id, OPTED_IN))
            finally:
                db.close()
        self.assertEqual((len(found), more), (60, 5))
        scores = [c["fit_score"] for c in found]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_jobs_of_several_workspaces_are_grouped_and_named(self):
        acct = self.user()
        second = helpers.new_workspace("Side search", acct.id)
        a, b = self.make_run(acct), self.make_run(acct, second)
        j1, j2 = self.job(acct, a, score=50), self.job(acct, b, score=90, ws=second)
        j3 = self.job(acct, a, score=70)
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                found, _ = digest.cards(db, digest.new_jobs(db, acct.id, OPTED_IN))
            finally:
                db.close()
        self.assertEqual([c["id"] for c in found], [j3, j1, j2])
        self.assertEqual({c["workspace_name"] for c in found}, {"My job search", "Side search"})

    def test_gaps_are_counted_the_way_the_frontend_counts_them(self):
        self.assertEqual(digest.gap_count({"gaps": ["a", "b", "c"]}), 3)
        reqs = [{"point": "x", "matched": False}, {"point": "y", "matched": True}, {"point": "z"}]
        self.assertEqual(digest.gap_count({"gaps": [], "requirements": reqs}), 2)
        self.assertEqual(digest.gap_count({"requirements": reqs}), 2)
        self.assertEqual(digest.gap_count({"gaps": ["a"], "requirements": reqs}), 1)
        self.assertEqual(digest.gap_count({}), 0)
        self.assertEqual(digest.gap_count({"gaps": "oops", "requirements": "oops"}), 0)


# ---------------------------------------------------------------------------
# When it is due
# ---------------------------------------------------------------------------

def prefs_row(every: int, *, sent: datetime | None = None, opted: datetime | None = None,
              checked: datetime | None = None) -> EmailPrefs:
    return EmailPrefs(user_id=0, digest_enabled=True, digest_every_days=every, last_sent_at=sent, opted_in_at=opted,
                      last_checked_at=checked, unsubscribe_id="x" * 22)


def local(day: int, hour: int, minute: int, zone: str = "WA") -> datetime:
    """Local time in October 2026 as an aware datetime."""
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ[zone])


class DueTests(unittest.TestCase):
    def test_every_frequency_is_due_at_seven_on_the_nth_local_day(self):
        sent = naive(local(5, 7, 5))  # sent on Monday the 5th in Perth
        for n in range(1, 8):
            row = prefs_row(n, sent=sent)
            due_day = 5 + n
            with self.subTest(every=n):
                self.assertFalse(digest.is_due(row, TZ["WA"], local(due_day - 1, 23, 59)), "the day before")
                self.assertFalse(digest.is_due(row, TZ["WA"], local(due_day, 6, 59)), "before 07:00")
                self.assertTrue(digest.is_due(row, TZ["WA"], local(due_day, 7, 0)), "at 07:00")
                self.assertTrue(digest.is_due(row, TZ["WA"], local(due_day, 23, 59)), "later that day")
                self.assertTrue(digest.is_due(row, TZ["WA"], local(due_day + 1, 7, 0)), "a missed day is caught up")

    def test_the_first_digest_counts_from_opting_in(self):
        row = prefs_row(2, opted=naive(local(5, 21, 0)))  # opted in at 21:00, so the 5th counts as day 0
        self.assertFalse(digest.is_due(row, TZ["WA"], local(6, 8, 0)))
        self.assertFalse(digest.is_due(row, TZ["WA"], local(6, 23, 0)))
        self.assertTrue(digest.is_due(row, TZ["WA"], local(7, 7, 0)))

    def test_opting_in_again_after_a_pause_counts_from_the_later_time(self):
        row = prefs_row(1, sent=naive(local(1, 7, 5)), opted=naive(local(9, 12, 0)))
        self.assertFalse(digest.is_due(row, TZ["WA"], local(9, 20, 0)))
        self.assertTrue(digest.is_due(row, TZ["WA"], local(10, 7, 0)))
        self.assertEqual(digest._since(row), naive(local(9, 12, 0)))

    def test_nothing_is_due_without_a_start_date(self):
        self.assertFalse(digest.is_due(prefs_row(1), TZ["WA"], local(20, 12, 0)))

    def test_once_a_day_checked_and_checked_again_the_next_day(self):
        row = prefs_row(1, sent=naive(local(5, 7, 5)), checked=naive(local(6, 7, 10)))
        self.assertFalse(digest.is_due(row, TZ["WA"], local(6, 7, 20)), "already checked today")
        self.assertFalse(digest.is_due(row, TZ["WA"], local(6, 23, 0)))
        self.assertTrue(digest.is_due(row, TZ["WA"], local(7, 7, 0)), "a new day")

    def test_the_users_own_time_zone_decides(self):
        # The same instant: Tuesday 6 October, 20:45 UTC.
        instant = datetime(2026, 10, 5, 20, 45, tzinfo=UTC)
        sent = datetime(2026, 10, 4, 22, 0)  # 5 October, in every Australian zone
        row = prefs_row(1, sent=sent)
        self.assertEqual({z: digest.is_due(row, TZ[z], instant) for z in TZ},
                         {"NSW": True,    # 07:45 on the 6th (daylight saving: UTC+11)
                          "QLD": False,   # 06:45
                          "NT": False,    # 06:15
                          "WA": False})   # 04:45
        later = instant + timedelta(hours=3)  # 23:45 UTC: it is past 07:00 on the 6th in all four zones
        self.assertEqual({z: digest.is_due(row, TZ[z], later) for z in TZ}, {z: True for z in TZ})

    def test_a_day_boundary_that_differs_by_zone(self):
        sent = datetime(2026, 10, 5, 14, 30)  # 6 October 01:30 in Sydney, 5 October 22:30 in Perth
        row = prefs_row(2, sent=sent)
        now = datetime(2026, 10, 6, 23, 30, tzinfo=UTC)  # 7 October: 10:30 in Sydney, 07:30 in Perth
        self.assertFalse(digest.is_due(row, TZ["NSW"], now), "one day after the 6th")
        self.assertTrue(digest.is_due(row, TZ["WA"], now), "two days after the 5th")

    def test_daylight_saving_does_not_shift_the_hour(self):
        sydney = TZ["NSW"]
        row = prefs_row(1, sent=naive(datetime(2026, 10, 3, 7, 5, tzinfo=sydney)))  # the day before it starts
        self.assertFalse(digest.is_due(row, sydney, datetime(2026, 10, 4, 6, 59, tzinfo=sydney)))
        self.assertTrue(digest.is_due(row, sydney, datetime(2026, 10, 4, 7, 0, tzinfo=sydney)))

    def test_the_zone_comes_from_the_first_workspaces_schedule(self):
        helpers.started()
        acct = helpers.make_user()
        db = SessionLocal()
        try:
            self.assertEqual(str(digest._zone(db, acct.id)), "Australia/Sydney")  # no schedule row: NSW
            db.add(SearchSchedule(id=acct.workspace_id, state="WA"))
            db.commit()
            self.assertEqual(str(digest._zone(db, acct.id)), "Australia/Perth")
            helpers.new_workspace("Second", acct.id)  # a later workspace doesn't count
            self.assertEqual(str(digest._zone(db, acct.id)), "Australia/Perth")
        finally:
            db.close()


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

class TickTests(Base):
    def test_a_digest_is_sent_with_the_new_jobs_best_first(self):
        acct = self.user(name="Flo")
        run = self.make_run(acct)
        low, high = self.job(acct, run, score=55), self.job(acct, run, score=91)
        self.assertEqual(self.tick(), 1)
        (msg,) = self.digests_to(acct)
        self.assertEqual(msg["To"], acct.email)
        self.assertIn("2 new jobs", msg["Subject"])
        self.assertIn("Hi Flo", text_of(msg))
        self.assertLess(text_of(msg).index(f"id={high}"), text_of(msg).index(f"id={low}"))
        prefs = self.prefs(acct)
        self.assertEqual(prefs.last_sent_at, naive(NOW))
        self.assertEqual(prefs.last_checked_at, naive(NOW))

    def test_no_new_jobs_sends_nothing_and_only_marks_the_check(self):
        acct = self.user()
        self.job(acct, self.make_run(acct, started=OPTED_IN - timedelta(days=1)))  # before the cutoff
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.digests_to(acct), [])
        prefs = self.prefs(acct)
        self.assertIsNone(prefs.last_sent_at)
        self.assertEqual(prefs.last_checked_at, naive(NOW))
        # not asked again the same day: a job that turns up later waits for tomorrow's check
        self.job(acct, self.make_run(acct))
        self.assertEqual(self.tick(NOW + timedelta(hours=1)), 0)
        self.assertEqual(self.tick(NOW + timedelta(days=1)), 1)

    def test_a_second_tick_the_same_day_does_not_repeat_it(self):
        acct = self.user()
        self.job(acct, self.make_run(acct))
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.tick(NOW + timedelta(minutes=10)), 0)
        self.assertEqual(len(self.digests_to(acct)), 1)

    def test_the_next_digest_holds_only_what_came_after_the_last(self):
        acct = self.user(every=2)
        first = self.job(acct, self.make_run(acct))
        self.assertEqual(self.tick(), 1)
        self.job(acct, self.make_run(acct, started=naive(NOW) + timedelta(hours=6)))
        self.assertEqual(self.tick(NOW + timedelta(days=1)), 0)   # every 2 days: not yet
        self.assertEqual(self.tick(NOW + timedelta(days=2)), 1)
        second = self.digests_to(acct)[1]
        self.assertIn("1 new job", second["Subject"])
        self.assertNotIn(f"id={first}&", text_of(second))

    def test_it_waits_before_seven_local_time(self):
        acct = self.user()
        self.job(acct, self.make_run(acct))
        self.assertEqual(self.tick(datetime(2026, 10, 7, 19, 59, tzinfo=UTC)), 0)  # 06:59 Sydney
        self.assertIsNone(self.prefs(acct).last_checked_at)
        self.assertEqual(self.tick(datetime(2026, 10, 7, 20, 0, tzinfo=UTC)), 1)    # 07:00 Sydney

    def test_a_failed_send_records_nothing_and_the_next_tick_tries_again(self):
        acct = self.user()
        self.job(acct, self.make_run(acct))
        with mock.patch.object(mailer, "send", return_value=mailer.SendResult("failed", "SMTPException")):
            self.assertEqual(self.tick(), 0)
        prefs = self.prefs(acct)
        self.assertIsNone(prefs.last_sent_at)
        self.assertIsNone(prefs.last_checked_at)
        self.assertEqual(self.digests_to(acct), [])
        self.assertEqual(self.tick(NOW + timedelta(minutes=10)), 1)
        self.assertEqual(self.prefs(acct).last_sent_at, naive(NOW + timedelta(minutes=10)))

    def three_bad_then_a_good_one(self):
        bad = [self.user() for _ in range(3)]
        good = self.user()
        for a in (*bad, good):
            self.job(a, self.make_run(a))
        return bad, good

    def assert_bad_ones_are_checked_and_the_good_one_gets_its_digest(self, bad, good, tried):
        self.assertEqual(self.tick(), 1, "three accounts' own trouble does not end the tick")
        self.flush()
        self.assertEqual(len(self.digests_to(good)), 1)
        for a in bad:
            self.assertEqual(self.prefs(a).last_checked_at, naive(NOW), "not tried again until their next day")
            self.assertIsNone(self.prefs(a).last_sent_at)
        before = tried()
        self.assertEqual(self.tick(NOW + timedelta(minutes=10)), 0)
        self.assertEqual(tried(), before, "and not tried again 10 minutes later")

    def test_three_refused_addresses_do_not_use_up_the_failure_budget(self):
        bad, good = self.three_bad_then_a_good_one()
        refused = {a.email for a in bad}
        real = mailer.send
        calls = []

        def send(to, *args, **kwargs):
            calls.append(to)
            if to in refused:
                return mailer.SendResult("failed", "SMTPRecipientsRefused")
            return real(to, *args, **kwargs)

        with mock.patch.object(mailer, "send", side_effect=send):
            self.assert_bad_ones_are_checked_and_the_good_one_gets_its_digest(bad, good, lambda: len(calls))

    def test_three_keys_the_server_cannot_open_do_not_use_up_the_failure_budget(self):
        bad, good = self.three_bad_then_a_good_one()
        broken = {a.id for a in bad}
        real = users.background_key
        calls = []

        def key(db, uid):
            calls.append(uid)
            return None if uid in broken else real(db, uid)

        with mock.patch.object(users, "background_key", side_effect=key):
            self.assert_bad_ones_are_checked_and_the_good_one_gets_its_digest(bad, good, lambda: len(calls))

    def test_three_accounts_with_no_address_do_not_use_up_the_failure_budget(self):
        bad, good = self.three_bad_then_a_good_one()
        nameless = {a.id for a in bad}
        real = users.details
        calls = []

        def details(db, uid):
            calls.append(uid)
            return {**real(db, uid), "email": ""} if uid in nameless else real(db, uid)

        with mock.patch.object(users, "details", side_effect=details):
            self.assert_bad_ones_are_checked_and_the_good_one_gets_its_digest(bad, good, lambda: len(calls))

    def test_an_exception_for_one_account_never_counts_against_the_others(self):
        bad, good = self.three_bad_then_a_good_one()
        broken = {a.id for a in bad}
        real = digest.cards

        def cards(db, found):
            if crypto.current().user_id in broken:
                raise ValueError("secret")
            return real(db, found)

        with mock.patch.object(digest, "cards", side_effect=cards):
            self.assertEqual(self.tick(), 1)

    def test_a_digest_that_went_is_never_sent_again_when_saving_it_fails(self):
        acct = self.user()
        self.job(acct, self.make_run(acct))
        real = digest._record
        fail = {"left": digest.RECORD_TRIES}

        def record(db, row, stamp, *, sent):
            if sent and fail["left"] > 0:
                fail["left"] -= 1
                raise RuntimeError("database is locked")
            return real(db, row, stamp, sent=sent)

        with mock.patch.object(digest, "sleep"), mock.patch.object(digest, "_record", side_effect=record):
            self.assertEqual(self.tick(), 1, "it went, so it counts")
        self.flush()
        self.assertEqual(len(self.digests_to(acct)), 1)
        self.assertIsNone(self.prefs(acct).last_sent_at, "the save failed")
        self.assertEqual(self.tick(NOW + timedelta(minutes=10)), 0)
        self.flush()
        self.assertEqual(len(self.digests_to(acct)), 1, "not sent a second time")
        self.assertEqual(self.prefs(acct).last_sent_at, naive(NOW), "saved on the next check")
        self.assertEqual(self.tick(NOW + timedelta(minutes=20)), 0)
        self.assertEqual(len(self.digests_to(acct)), 1)

    def test_saving_a_sent_digest_is_tried_again_before_it_gives_up(self):
        acct = self.user()
        self.job(acct, self.make_run(acct))
        from sqlalchemy.orm import Session
        real = Session.commit
        fails = {"left": 2}

        def commit(session):
            if fails["left"] and any(isinstance(o, EmailPrefs) and o.last_sent_at for o in session.dirty):
                fails["left"] -= 1
                raise RuntimeError("database is locked")
            return real(session)

        with mock.patch.object(digest, "sleep") as nap, mock.patch.object(Session, "commit", commit):
            self.assertEqual(self.tick(), 1)
        self.assertEqual(nap.call_count, 2)
        self.assertEqual(self.prefs(acct).last_sent_at, naive(NOW))
        self.flush()
        self.assertEqual(len(self.digests_to(acct)), 1)

    def test_the_tick_stops_after_three_failures_and_one_users_trouble_does_not_stop_the_rest(self):
        accts = [self.user() for _ in range(5)]
        for a in accts:
            self.job(a, self.make_run(a))
        with mock.patch.object(mailer, "send", return_value=mailer.SendResult("failed", "OSError")) as send:
            self.assertEqual(self.tick(), 0)
        self.assertEqual(send.call_count, digest.MAX_FAILURES, "the mail server is down: it counts")
        self.assertEqual(self.tick(NOW + timedelta(minutes=10)), 5)

    def test_a_run_still_going_holds_the_check_back(self):
        acct = self.user()
        self.job(acct, self.make_run(acct))
        busy = self.make_run(acct, state="running", started=naive(NOW) - timedelta(minutes=5))
        self.assertEqual(self.tick(), 0)
        self.assertIsNone(self.prefs(acct).last_checked_at)
        db = SessionLocal()
        try:
            db.query(SearchRun).filter(SearchRun.id == busy).update({SearchRun.state: "done"})
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.tick(NOW + timedelta(minutes=10)), 1)

    def test_who_gets_one(self):
        wanted = self.user()
        off = self.user(digest=False)
        unconfirmed = self.user(verified=False)
        no_key = self.user(background=False)
        disabled = self.user()
        db = SessionLocal()
        try:
            db.get(User, disabled.id).status = "disabled"
            db.commit()
        finally:
            db.close()
        for a in (wanted, off, unconfirmed, no_key, disabled):
            self.job(a, self.make_run(a))
        self.assertEqual(self.tick(), 1)
        self.flush()
        self.assertEqual([m["To"] for m in self.sent], [wanted.email])
        for a in (off, unconfirmed, no_key, disabled):
            self.assertIsNone(self.prefs(a).last_checked_at, "not even looked at")

    def test_nothing_happens_while_email_is_off(self):
        acct = self.user()
        self.job(acct, self.make_run(acct))
        with mock.patch.dict("os.environ", {"SMTP_HOST": "", "PUBLIC_BASE_URL": ""}):
            self.assertFalse(mail_queue.enabled())
            self.assertEqual(self.tick(), 0)
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.prefs(acct).last_checked_at)

    def test_opting_out_stops_the_emails(self):
        acct = self.user()
        self.job(acct, self.make_run(acct))
        with self.signed_in(acct) as c:
            self.assertFalse(c.put("/api/account/digest", json={"enabled": False}).json()["enabled"])
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.sent, [])

    def test_a_workspace_with_no_scheduled_searches_simply_has_no_new_jobs(self):
        acct = self.user()
        self.job(acct, self.make_run(acct, scheduled=False))  # no membership, no schedule: only manual runs
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.sent, [])

    def test_the_scheduler_loop_runs_it_every_ten_minutes(self):
        calls = []
        with mock.patch.object(digest, "tick", side_effect=lambda now: calls.append(now) or 0):
            with mock.patch.object(schedule, "_last_digest", None):
                schedule.digest_tick(NOW)
                schedule.digest_tick(NOW + timedelta(minutes=9))
                schedule.digest_tick(NOW + timedelta(minutes=10))
        self.assertEqual(calls, [NOW, NOW + timedelta(minutes=10)])
        with mock.patch.object(digest, "tick", side_effect=RuntimeError("secret job title")), \
                mock.patch.object(schedule, "_last_digest", None):
            self.assertIsNone(schedule.digest_tick(NOW))  # never raises

    def test_the_loop_itself_calls_it(self):
        with mock.patch.object(schedule.threading, "Thread") as thread, \
                mock.patch.object(schedule._stop, "wait", side_effect=[False, True]), \
                mock.patch.object(schedule, "tick"), mock.patch.object(schedule, "housekeeping"), \
                mock.patch.object(schedule, "billing_sync_tick"), \
                mock.patch.object(schedule, "digest_tick") as digest_tick, \
                mock.patch.dict("os.environ", {"JOBHUNT_SCHEDULER": "on"}):
            schedule.start_scheduler(lambda ws: {})
            loop = thread.call_args.kwargs["target"]
            loop()
        digest_tick.assert_called_once_with()

    def test_the_scheduler_does_not_start_the_loop_when_it_is_off(self):
        with mock.patch.dict("os.environ", {"JOBHUNT_SCHEDULER": "off"}), \
                mock.patch.object(schedule.threading, "Thread") as thread:
            schedule.start_scheduler(lambda ws: {})
        thread.assert_not_called()


# ---------------------------------------------------------------------------
# What the email holds
# ---------------------------------------------------------------------------

class EmailTests(Base):
    def test_every_card_field_is_in_both_parts_and_both_headers_are_there(self):
        acct = self.user()
        jid = self.job(acct, self.make_run(acct), score=87, summary="Strong delivery; light on cloud.",
                       gaps=("cloud", "go"), title="Senior Project Manager", company="Acme Pty Ltd",
                       location_text="Sydney NSW", salary_min=150000, salary_max=170000, office_days=3,
                       work_mode="hybrid", employment_type="full_time", source="seek")
        self.assertEqual(self.tick(), 1)
        (msg,) = self.digests_to(acct)
        url = f"https://www.seek.com.au/job/{int(re.search(r'job/(\d+)', text_of(msg)).group(1))}"
        needles = ["Senior Project Manager", "Acme Pty Ltd", "$150k–$170k", "Sydney NSW", "3 days in the office",
                   "Hybrid", "Full-time", "87 out of 100", "Strong delivery; light on cloud.", "2 gaps",
                   f"{BASE}/job.html?id={jid}&ws={acct.workspace_id}", url, "SEEK"]
        for part, body in (("text", text_of(msg)), ("html", html_of(msg))):
            for needle in needles:
                self.assertIn(needle.replace("&", "&amp;") if part == "html" and "&" in needle else needle, body,
                              f"{part}: {needle}")
        # the heading is the job title, and the cards have the open link and the original ad's link
        self.assertRegex(html_of(msg), r"<h2[^>]*>Senior Project Manager</h2>")
        links = Links(html_of(msg)).links
        self.assertIn(f"{BASE}/job.html?id={jid}&ws={acct.workspace_id}", links)
        self.assertIn(url, links)
        # no requirement detail, no evidence, no status
        self.assertNotIn("to_review", text_of(msg) + html_of(msg))
        # RFC 8058 headers, and the footer link is the same one
        unsub = msg["List-Unsubscribe"]
        self.assertEqual(msg["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertEqual(msg["Auto-Submitted"], "auto-generated")
        self.assertRegex(unsub, rf"^<{re.escape(BASE)}/unsubscribe\?token=[A-Za-z0-9_.-]+>$")
        self.assertIn(unsub.strip("<>"), text_of(msg))
        self.assertIn(unsub.strip("<>"), Links(html_of(msg)).links)

    def test_an_unscored_job_shows_without_a_score_and_a_missing_field_is_left_out(self):
        acct = self.user()
        self.job(acct, self.make_run(acct), score=None, salary_min=None, salary_max=None, office_days=None,
                 employment_type=None, work_mode="unknown")
        self.assertEqual(self.tick(), 1)
        body = text_of(self.digests_to(acct)[0])
        for absent in ("Fit score", "Gaps", "Salary", "Office days", "Employment", "Work type", "None", "null"):
            self.assertNotIn(absent, body)

    def test_zero_gaps_on_a_scored_job_is_stated(self):
        acct = self.user()
        self.job(acct, self.make_run(acct), gaps=())
        self.tick()
        self.assertIn("0 gaps", text_of(self.digests_to(acct)[0]))

    def test_more_than_sixty_says_how_many_more(self):
        acct = self.user()
        run = self.make_run(acct)
        for i in range(64):
            self.job(acct, run, score=i)
        self.tick()
        (msg,) = self.digests_to(acct)
        self.assertIn("64 new jobs", msg["Subject"])
        self.assertIn("And 4 more in JobHuntPA", text_of(msg))
        self.assertEqual(len(re.findall(r"^Open in JobHuntPA:", text_of(msg), re.M)), 60)

    def test_several_workspaces_name_the_workspace_on_each_card(self):
        acct = self.user()
        second = helpers.new_workspace("Side search", acct.id)
        self.job(acct, self.make_run(acct))
        self.job(acct, self.make_run(acct, second), ws=second)
        self.tick()
        body = text_of(self.digests_to(acct)[0])
        self.assertIn("Workspace: My job search", body)
        self.assertIn("Workspace: Side search", body)

    def test_a_job_with_html_in_its_text_is_escaped(self):
        acct = self.user()
        self.job(acct, self.make_run(acct), title="<script>alert(1)</script>", summary="<b>bold</b>")
        self.tick()
        html = html_of(self.digests_to(acct)[0])
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>bold", html)

    def test_the_source_label_follows_the_frontends_rule(self):
        from backend.email_templates import source_label

        for raw, label in (("seek", "SEEK"), ("import", "imported page"), ("browser", "browser extension"),
                           ("browser:bulk:seek.com.au", "SEEK - browser extension (bulk)"),
                           ("browser:single:seek.com.au", "SEEK - browser extension (single)"),
                           ("browser:single:au.linkedin.com", "LinkedIn - browser extension (single)"),
                           ("browser:single:careers.acme.example", "careers.acme.example - browser extension (single)"),
                           ("browser:bulk:", "browser extension (bulk)"),
                           ("browser:other:seek.com.au", "SEEK - browser extension"),
                           ("apsjobs:q=x", "APSJobs"),
                           ("site:acme.com", "acme.com"), ("generic:jobs.example.org", "jobs.example.org"),
                           ("workable:compass-education", "compass-education (Workable)"),
                           ("greenhouse:acme/jobs", "acme (Greenhouse)"), ("plain", "plain"), ("", None),
                           (None, None)):
            self.assertEqual(source_label(raw), label, raw)

    def test_no_address_or_job_text_is_logged(self):
        acct = self.user(name="Flo Unique")
        secret = "Zyxwv Confidential Title"
        self.job(acct, self.make_run(acct), title=secret, summary="Zyxwv private summary",
                 company="Zyxwv Corp")
        records: list[str] = []

        class Catch(logging.Handler):
            def emit(self, record):
                if not record.name.startswith(("httpx", "httpcore")):  # the test client's own request line
                    records.append(record.getMessage())

        handler, root = Catch(level=logging.DEBUG), logging.getLogger()
        old = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            self.tick()
            with mock.patch.object(mailer, "send", return_value=mailer.SendResult("failed", "OSError")):
                self.job(acct, self.make_run(acct, started=naive(NOW) + timedelta(hours=1)), title=secret)
                self.tick(NOW + timedelta(days=1))
            with mock.patch.object(digest, "cards", side_effect=ValueError(f"{secret} {acct.email}")):
                self.tick(NOW + timedelta(days=2))
            msg = self.digests_to(acct)[0]
            c = self.anonymous()
            c.post(f"/unsubscribe?token={self.token_of(msg)}", data={"List-Unsubscribe": "One-Click"})
            c.post(f"/unsubscribe/resubscribe?token={self.token_of(msg)}")
            c.post("/unsubscribe?token=garbage")
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        self.assertTrue(records, "something should have been logged")
        everything = "\n".join(records)
        for private in (acct.email, "Flo Unique", "Zyxwv", self.token_of(msg)):
            self.assertNotIn(private, everything)


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------

class UnsubscribeTests(Base):
    def setUp(self):
        super().setUp()
        self.acct = self.user(every=3)
        self.job(self.acct, self.make_run(self.acct))
        self.tick()
        self.token = self.token_of(self.digests_to(self.acct)[0])
        self.anon = self.anonymous()

    def enabled(self) -> bool:
        return bool(self.prefs(self.acct).digest_enabled)

    def test_get_shows_a_confirmation_page_and_changes_nothing(self):
        before = self.prefs(self.acct)
        r = self.anon.get(f"/unsubscribe?token={self.token}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.enabled())
        after = self.prefs(self.acct)
        self.assertEqual((after.digest_enabled, after.opted_in_at, after.last_sent_at),
                         (before.digest_enabled, before.opted_in_at, before.last_sent_at))
        self.assertEqual(self.anon.head(f"/unsubscribe?token={self.token}").status_code, 200)
        self.assertTrue(self.enabled())
        page = r.text
        self.assertRegex(page, r'<form method="post" action="/unsubscribe\?token=[^"]+">')
        self.assertIn("every 3 days", page)
        self.assertIn("<h1>Unsubscribe from job digests?</h1>", page)
        self.assertIn('<html lang="en">', page)
        self.assertIn("<title>Unsubscribe from job digests? - JobHuntPA</title>", page)
        self.assertNotIn("<script", page)

    def test_the_page_is_safe_to_serve(self):
        r = self.anon.get(f"/unsubscribe?token={self.token}")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(r.headers["referrer-policy"], "no-referrer")
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])
        self.assertNotIn("script-src", r.headers["content-security-policy"].replace("default-src 'none'", ""))
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")

    def test_a_one_click_post_works_with_no_session_and_no_origin(self):
        self.assertNotIn("origin", self.anon.headers)
        self.assertEqual(len(self.anon.cookies), 0)
        r = self.anon.post(f"/unsubscribe?token={self.token}", data={"List-Unsubscribe": "One-Click"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.enabled())
        self.assertIn("You are unsubscribed", r.text)
        # idempotent, and a body-less POST (some senders) works too
        self.assertEqual(self.anon.post(f"/unsubscribe?token={self.token}").status_code, 200)
        self.assertFalse(self.enabled())

    def test_the_pages_own_form_post_works_without_a_session(self):
        r = self.anon.post(f"/unsubscribe?token={self.token}", headers={"Origin": BASE})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.enabled())

    def test_a_signed_in_browser_posting_another_accounts_token_changes_only_that_account(self):
        other = self.user()
        with self.signed_in(other) as c:
            self.assertEqual(c.put("/api/account/digest", json={"enabled": True}).status_code, 200)
            r = c.post(f"/unsubscribe?token={self.token}", headers={"Origin": BASE})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.enabled(), "the token decides, not the cookie")
        self.assertTrue(self.prefs(other).digest_enabled, "the signed-in account is untouched")

    def resub_token(self) -> str:
        return users.server_keys().sign("resub", self.prefs(self.acct).unsubscribe_id, ttl=digest.RESUBSCRIBE_TTL)

    def resubscribe_form(self, html: str) -> str:
        return re.search(r'action="(/unsubscribe/resubscribe\?token=[^"]+)"', html).group(1).replace("&amp;", "&")

    def test_the_emails_token_cannot_turn_digests_back_on_only_the_pages_short_lived_one_can(self):
        page = self.anon.post(f"/unsubscribe?token={self.token}").text
        form = self.resubscribe_form(page)
        self.assertNotIn(self.token, form, "the page's button carries its own token")
        # the email's token: no button, and the resubscribe route refuses it
        for r in (self.anon.get(f"/unsubscribe?token={self.token}"), self.anon.post(f"/unsubscribe?token={self.token}")):
            self.assertNotIn("<form", r.text)
            self.assertIn("Settings &gt; Job digest emails", r.text)
        self.assertEqual(self.anon.post(f"/unsubscribe/resubscribe?token={self.token}").status_code, 410)
        self.assertFalse(self.enabled())
        # the page's token works, once it has not run out
        resub = form.split("token=")[1]
        stale = users.server_keys().sign("resub", self.prefs(self.acct).unsubscribe_id, ttl=digest.RESUBSCRIBE_TTL,
                                         now=time.time() - digest.RESUBSCRIBE_TTL - 5)
        self.assertEqual(self.anon.post(f"/unsubscribe/resubscribe?token={stale}").status_code, 410)
        self.assertFalse(self.enabled())
        # and it cannot unsubscribe
        self.assertEqual(self.anon.post(form).status_code, 200)
        self.assertTrue(self.enabled())
        self.assertEqual(self.anon.post(f"/unsubscribe?token={resub}").status_code, 410)

    def test_the_result_pages_lead_back_to_the_app(self):
        off = self.anon.post(f"/unsubscribe?token={self.token}")
        self.assertIn('<a href="/">Open JobHuntPA</a>', off.text)
        self.assertIn('<a href="/">Open JobHuntPA</a>', self.anon.get(f"/unsubscribe?token={self.token}").text)
        on = self.anon.post(self.resubscribe_form(off.text))
        self.assertIn('<a href="/">Open JobHuntPA</a>', on.text)
        self.assertIn("Settings &gt; Job digest emails", on.text)

    def test_it_stops_the_emails_and_resubscribing_starts_them_again(self):
        off = self.anon.post(f"/unsubscribe?token={self.token}", data={"List-Unsubscribe": "One-Click"})
        self.job(self.acct, self.make_run(self.acct, started=datetime.utcnow() - timedelta(hours=3)))
        self.assertEqual(self.tick(NOW + timedelta(days=3)), 0)
        self.assertEqual(len(self.digests_to(self.acct)), 1)
        before = datetime.utcnow()
        r = self.anon.post(self.resubscribe_form(off.text))  # the button of the page that unsubscribed
        self.assertEqual(r.status_code, 200)
        self.assertIn("subscribed again", r.text)
        prefs = self.prefs(self.acct)
        self.assertTrue(prefs.digest_enabled)
        self.assertGreaterEqual(prefs.opted_in_at, before - timedelta(seconds=1))
        self.assertIsNone(prefs.last_checked_at)
        # the cutoff is now (by the real clock): what turned up during the pause is not sent, what comes after is
        later = datetime.now(UTC).astimezone(TZ["NSW"]).replace(hour=12, minute=0) + timedelta(days=3)
        self.job(self.acct, self.make_run(self.acct, started=datetime.utcnow() - timedelta(hours=1)))
        self.assertEqual(self.tick(later), 0)
        self.set_prefs(self.acct, last_checked_at=None)
        fresh = self.job(self.acct, self.make_run(self.acct, started=naive(later) - timedelta(hours=3)))
        self.assertEqual(self.tick(later + timedelta(days=1)), 1)
        self.assertEqual(len(re.findall(r"^Open in JobHuntPA:", text_of(self.digests_to(self.acct)[1]), re.M)), 1)
        self.assertIn(f"id={fresh}&", text_of(self.digests_to(self.acct)[1]))

    def test_resubscribing_while_subscribed_changes_nothing(self):
        before = self.prefs(self.acct)
        self.assertEqual(self.anon.post(f"/unsubscribe/resubscribe?token={self.resub_token()}").status_code, 200)
        after = self.prefs(self.acct)
        self.assertEqual((after.opted_in_at, after.last_checked_at), (before.opted_in_at, before.last_checked_at))

    def test_a_bad_token_changes_nothing_and_every_failure_looks_alike(self):
        good = self.token
        payload, mac = good.split(".")
        other = ServerKeys.create(tempfile.mkdtemp() + "/keys.json")
        retired = ServerKeys.create(tempfile.mkdtemp() + "/keys.json")
        old = retired.sign("unsub", payload)
        retired.rotate_signing(now=time.time() - 40 * 86400)  # its key was retired 40 days ago: gone
        bad = {"missing": "", "garbage": "garbage", "no mac": payload, "mac cut": f"{payload}.{mac[:-1]}",
               "mac changed": f"{payload}.{'A' if mac[0] != 'A' else 'B'}{mac[1:]}",
               "other id": f"{'A' * 22}.{mac}", "another install": other.sign("unsub", payload),
               "another purpose": users.server_keys().sign("reset", payload), "huge": "a" * 5000,
               "signed for no account": users.server_keys().sign("unsub", "A" * 22), "dots": "...", "sql": "x' OR '1'='1"}
        answers = set()
        for name, token in bad.items():
            with self.subTest(name):
                for method, path in (("post", "/unsubscribe"), ("post", "/unsubscribe/resubscribe"), ("get", "/unsubscribe")):
                    r = getattr(self.anon, method)(path, params={"token": token})
                    self.assertEqual(r.status_code, 410)
                    answers.add(r.text)
                self.assertTrue(self.enabled())
        with mock.patch.object(users, "server_keys", return_value=retired):
            r = self.anon.post("/unsubscribe", params={"token": old})
            self.assertEqual(r.status_code, 410, "a token whose signing key is long gone")
            answers.add(r.text)
        self.assertTrue(self.enabled())
        # one answer whatever was wrong with the token (and for GET as for POST)
        self.assertEqual(len(answers), 1)
        self.assertEqual(self.anon.post("/unsubscribe").status_code, 410)  # no token at all

    def test_the_previous_signing_key_still_works_after_a_rotation(self):
        users.server_keys().rotate_signing()
        self.assertEqual(self.anon.post(f"/unsubscribe?token={self.token}").status_code, 200)
        self.assertFalse(self.enabled())

    def test_a_deleted_account_gets_the_same_answer_as_a_bad_token(self):
        db = SessionLocal()
        try:
            db.query(EmailPrefs).filter(EmailPrefs.user_id == self.acct.id).delete()
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.anon.post(f"/unsubscribe?token={self.token}").status_code, 410)

    def test_a_body_that_is_too_large_is_refused_and_changes_nothing(self):
        r = self.anon.post(f"/unsubscribe?token={self.token}", content=b"x" * 5000)
        self.assertEqual(r.status_code, 413)
        self.assertTrue(self.enabled())

    def test_rate_limit_is_30_a_minute_per_address(self):
        statuses = [self.anon.post("/unsubscribe", params={"token": "bad"}).status_code for _ in range(31)]
        self.assertEqual(statuses[:30], [410] * 30)
        self.assertEqual(statuses[30], 429)
        r = self.anon.post(f"/unsubscribe?token={self.token}")
        self.assertEqual(r.status_code, 429)
        self.assertIn("retry-after", r.headers)
        self.assertTrue(self.enabled(), "a refused request changes nothing")
        # a GET is only a page: it isn't counted
        self.assertEqual(self.anon.get(f"/unsubscribe?token={self.token}").status_code, 200)

    def test_only_these_paths_and_methods_skip_the_session(self):
        """The routes sit outside /api, which the session middleware guards; everything under /api still needs one."""
        self.assertEqual(self.anon.get("/api/account/digest").status_code, 401)
        self.assertEqual(self.anon.put("/api/account/digest", json={"enabled": True}).status_code, 401)
        self.assertEqual(self.anon.post("/api/unsubscribe", params={"token": self.token}).status_code, 401)
        self.assertNotEqual(self.anon.put("/unsubscribe", params={"token": self.token}).status_code, 200)
        self.assertNotEqual(self.anon.delete("/unsubscribe", params={"token": self.token}).status_code, 200)
        self.assertNotEqual(self.anon.get("/unsubscribe/resubscribe", params={"token": self.token}).status_code, 200)
        self.assertTrue(self.enabled())

    def test_the_server_page_is_accessible_markup(self):
        for r in (self.anon.get(f"/unsubscribe?token={self.token}"),
                  self.anon.post(f"/unsubscribe?token={self.token}"),
                  self.anon.post(f"/unsubscribe/resubscribe?token={self.resub_token()}"),
                  self.anon.get(f"/unsubscribe?token={self.token}"), self.anon.get("/unsubscribe?token=bad")):
            page = r.text
            self.assertTrue(page.startswith("<!DOCTYPE html>"))
            self.assertEqual(page.count("<h1>"), 1)
            self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', page)
            self.assertEqual(page.count("<main>"), 1)
            self.assertIn("min-height:44px", page)  # a touch target of at least 44 px
            self.assertIn(":focus-visible", page)
            self.assertIn("prefers-color-scheme:dark", page)
            self.assertEqual(page.count("<form"), page.count("<button"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsTests(Base):
    def test_a_new_account_has_it_off_and_is_told_why_it_cant_send(self):
        acct = helpers.make_user()
        with self.signed_in(acct) as c:
            body = c.get("/api/account/digest").json()
        self.assertEqual((body["enabled"], body["every_days"], body["can_send"]), (False, 1, False))
        self.assertEqual({p["code"] for p in body["problems"]}, {"email_unconfirmed", "background_off"})
        self.assertTrue(all(p["text"] for p in body["problems"]))
        self.assertEqual((body["send_time"], body["time_zone"], body["last_sent_at"]), ("07:00", "Australia/Sydney", None))

    def test_can_send_when_everything_is_in_place(self):
        acct = self.user(digest=False)
        with self.signed_in(acct) as c:
            self.assertEqual(c.get("/api/account/digest").json()["problems"], [])
            self.assertTrue(c.get("/api/account/digest").json()["can_send"])

    def test_each_reason_is_named(self):
        acct = self.user(digest=False)
        with self.signed_in(acct) as c:
            with mock.patch.dict("os.environ", {"SMTP_HOST": ""}):
                self.assertEqual([p["code"] for p in c.get("/api/account/digest").json()["problems"]], ["email_off"])
            with mock.patch.object(membership, "scheduled_block", return_value="membership_required"):
                body = c.get("/api/account/digest").json()
            self.assertEqual([p["code"] for p in body["problems"]], ["membership_required"])
            self.assertFalse(body["can_send"])

    def test_turning_it_on_sets_the_start_and_the_unsubscribe_id(self):
        acct = self.user(digest=False)
        with self.signed_in(acct) as c:
            before = datetime.utcnow()
            body = c.put("/api/account/digest", json={"enabled": True, "every_days": 4}).json()
            self.assertEqual((body["enabled"], body["every_days"]), (True, 4))
            prefs = self.prefs(acct)
            self.assertTrue(prefs.digest_enabled)
            self.assertEqual(prefs.digest_every_days, 4)
            self.assertGreaterEqual(prefs.opted_in_at, before - timedelta(seconds=1))
            self.assertRegex(prefs.unsubscribe_id, r"^[A-Za-z0-9_-]{22}$")
            # saving again, or changing only the frequency, keeps the start
            c.put("/api/account/digest", json={"enabled": True})
            c.put("/api/account/digest", json={"every_days": 2})
            again = self.prefs(acct)
            self.assertEqual((again.opted_in_at, again.digest_every_days), (prefs.opted_in_at, 2))
            # off keeps the frequency
            self.assertFalse(c.put("/api/account/digest", json={"enabled": False}).json()["enabled"])
            self.assertEqual(self.prefs(acct).digest_every_days, 2)

    def test_turning_it_on_again_starts_afresh(self):
        acct = self.user(digest=False)
        self.set_prefs(acct, last_checked_at=OPTED_IN)
        with self.signed_in(acct) as c:
            c.put("/api/account/digest", json={"enabled": True})
        prefs = self.prefs(acct)
        self.assertIsNone(prefs.last_checked_at)
        self.assertGreater(prefs.opted_in_at, OPTED_IN)

    def test_a_missing_row_is_made_with_a_good_id(self):
        acct = self.user(digest=False)
        db = SessionLocal()
        try:
            db.query(EmailPrefs).filter(EmailPrefs.user_id == acct.id).delete()
            db.commit()
        finally:
            db.close()
        with self.signed_in(acct) as c:
            self.assertFalse(c.get("/api/account/digest").json()["enabled"])
            self.assertTrue(c.put("/api/account/digest", json={"enabled": True}).json()["enabled"])
        self.assertRegex(self.prefs(acct).unsubscribe_id, r"^[A-Za-z0-9_-]{22}$")

    def test_validation(self):
        acct = self.user(digest=False)
        with self.signed_in(acct) as c:
            for bad in ({}, {"every_days": 0}, {"every_days": 8}, {"every_days": "x"}, {"enabled": "maybe"},
                        {"enabled": True, "extra": 1}):
                self.assertEqual(c.put("/api/account/digest", json=bad).status_code, 422, bad)
            for good in (1, 7):
                self.assertEqual(c.put("/api/account/digest", json={"every_days": good}).json()["every_days"], good)
            self.assertFalse(self.prefs(acct).digest_enabled)

    def test_it_needs_a_session_and_the_csrf_token_and_only_changes_ones_own(self):
        a, b = self.user(digest=False), self.user(digest=False)
        anon = self.anonymous()
        self.assertEqual(anon.get("/api/account/digest").status_code, 401)
        with self.signed_in(a) as c:
            token = c.headers.pop("X-CSRF-Token")
            r = c.put("/api/account/digest", json={"enabled": True})
            self.assertEqual((r.status_code, r.json()["code"]), (403, "csrf"))
            c.headers["X-CSRF-Token"] = token
            c.put("/api/account/digest", json={"enabled": True})
        self.assertTrue(self.prefs(a).digest_enabled)
        self.assertFalse(self.prefs(b).digest_enabled)

    def test_the_wizard_offers_digests(self):
        from backend import account

        self.assertTrue(account.features()["digests"])

    def test_the_wizard_does_not_offer_digests_when_email_is_not_set_up(self):
        from backend import account

        with mock.patch.dict("os.environ", {"SMTP_HOST": "", "PUBLIC_BASE_URL": ""}):
            self.assertFalse(mail_queue.enabled())
            self.assertFalse(account.features()["digests"])

    def test_the_export_prints_the_setting(self):
        from backend import export

        for every, words in ((1, "on, every day"), (5, "on, every 5 days")):
            acct = self.user(every=every)
            self.set_prefs(acct, last_sent_at=datetime(2026, 10, 7, 21, 0))
            db = SessionLocal()
            try:
                with helpers.as_account(acct):
                    md = export._account_md(db, db.get(User, acct.id), [])
            finally:
                db.close()
            self.assertIn(words, md)
            self.assertIn("Last digest sent", md)
            self.assertNotIn(self.prefs(acct).unsubscribe_id, md)
        off = self.user(digest=False)
        db = SessionLocal()
        try:
            with helpers.as_account(off):
                self.assertIn("Job digest emails: off", export._account_md(db, db.get(User, off.id), []))
        finally:
            db.close()


# ---------------------------------------------------------------------------
# jobs.first_run_id
# ---------------------------------------------------------------------------

def scraped(n: int) -> Scraped:
    return Scraped(url=f"https://jobs.digest.example/{n}", title="Program Manager", company="Digestco",
                   location_text="Sydney NSW", country="AU", external_id=str(n))


class FirstRunTests(Base):
    def link(self, acct, n: int) -> tuple[int, int | None, bool]:
        """Link a posting for the account's workspace now, as the code in a run would: (job id, first_run_id, new)."""
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                posting, _ = pool.upsert_scraped(db, scraped(n), "seek", None, datetime.utcnow())
                job, is_new = pool.link(db, acct.workspace_id, posting, datetime.utcnow())
                db.commit()
                return job.id, job.first_run_id, is_new
            finally:
                db.close()

    def in_run(self, acct, work, *, scheduled: bool = True) -> tuple[int, object]:
        """Runs ``work()`` as a background run of the account's workspace and waits for it: (run id, its result)."""
        out = []
        with helpers.as_account(acct):
            run = tasks.start("search", lambda progress: out.append(work()) or {}, acct.workspace_id,
                              scheduled=scheduled)
        deadline = time.time() + 20
        while time.time() < deadline:
            db = SessionLocal()
            try:
                state = db.query(SearchRun.state).filter(SearchRun.id == run["id"]).scalar()
            finally:
                db.close()
            if state in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(state, "done")
        return run["id"], out[0]

    def test_a_link_made_in_a_run_records_the_run(self):
        acct = self.user()
        run_id, (job_id, first_run, new) = self.in_run(acct, lambda: self.link(acct, 1))
        self.assertTrue(new)
        self.assertEqual(first_run, run_id)
        db = SessionLocal()
        try:
            self.assertEqual(db.query(Job.first_run_id).filter(Job.id == job_id).scalar(), run_id)
        finally:
            db.close()
        self.assertIsNone(tasks.current_run_id.get(), "the run's context doesn't leak out")

    def test_a_link_made_outside_a_run_stays_null(self):
        acct = self.user()
        job_id, first_run, new = self.link(acct, 2)
        self.assertTrue(new)
        self.assertIsNone(first_run)

    def test_a_later_run_does_not_change_it(self):
        acct = self.user()
        first, (job_id, _, _) = self.in_run(acct, lambda: self.link(acct, 3))
        second, (same_id, run_seen, new) = self.in_run(acct, lambda: self.link(acct, 3))
        self.assertEqual((same_id, new, run_seen), (job_id, False, first))
        self.assertNotEqual(first, second)

    def test_the_run_reaches_threads_the_run_starts(self):
        from concurrent.futures import ThreadPoolExecutor

        from backend.threads import submit

        acct = self.user()

        def work():
            with ThreadPoolExecutor(max_workers=1) as ex:
                return submit(ex, tasks.current_run_id.get).result()

        run_id, seen = self.in_run(acct, work)
        self.assertEqual(seen, run_id)

    def test_pool_matches_are_stamped_too(self):
        acct = self.user()
        other = self.user()
        with helpers.as_account(other):  # someone else's search puts the ad in the pool
            db = SessionLocal()
            try:
                pool.upsert_scraped(db, scraped(9), "seek", None, datetime.utcnow())
                db.commit()
            finally:
                db.close()

        def match():
            db = SessionLocal()
            try:
                with mock.patch("backend.pipeline.enrich"):
                    n = pool.match_pool(db, acct.workspace_id, Criteria(titles=["Program Manager"]), datetime.utcnow())
                db.commit()
                return n
            finally:
                db.close()

        run_id, matched = self.in_run(acct, match)
        self.assertGreaterEqual(matched, 1)  # (the pool holds other tests' ads too)
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                self.assertEqual([j.first_run_id for j in db.query(Job).filter(Job.workspace_id == acct.workspace_id)],
                                 [run_id] * matched)
            finally:
                db.close()

    def test_the_column_is_added_to_an_older_database(self):
        from sqlalchemy import inspect

        from backend.db import engine

        self.assertIn("first_run_id", {c["name"] for c in inspect(engine).get_columns("jobs")})


if __name__ == "__main__":
    unittest.main()
