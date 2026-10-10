"""Ghost-job signs (backend/ghost.py, docs/design/ghost-jobs.md): each signal on planted postings with its edge
cases, the levels, the scorer's ghost_signs (shared for a public ad, private for an uploaded one), the job JSON, the
digest line and the export section, and that no statement filters, sorts or groups on a sealed column. The network
and the LLM are mocked throughout.

Run:  python3 -m unittest tests.test_ghost -v
"""
from __future__ import annotations

import copy
import io
import itertools
import json
import os
import re
import unittest
import zipfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest import mock

from tests import helpers  # noqa: E402  (signed-in clients; sets the test environment)

from sqlalchemy import event  # noqa: E402

from backend import crypto, digest, email_templates, ghost, pool, scorer  # noqa: E402
from backend.adapters.base import Adapter, Posting as Scraped  # noqa: E402
from backend.db import CompanyProfile, FitResult, Job, Posting, SessionLocal, engine, init_db  # noqa: E402
from backend.scraping.text import description_hash  # noqa: E402

init_db()

NOW = datetime(2026, 10, 8, 3, 0)
AD = ("About the role. You will run the delivery program for our enterprise customers, coordinate engineering and "
      "product teams in Sydney, and report to the executive team. We offer hybrid working with 3 days in the office. "
      "You bring ten years of delivery experience, strong stakeholder skills and a track record of shipping. ")
LLM_ENV = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://api.openai.com/v1", "LLM_MODEL": "m"}
_names = itertools.count(1)


def setUpModule():
    helpers.enter_admin_scope()  # these tests write rows directly and call background code as the admin


def tearDownModule():
    helpers.exit_admin_scope()


def days_ago(n: float, now: datetime = NOW) -> datetime:
    return now - timedelta(days=n)


def codes(signals) -> list[str]:
    return [s["code"] for s in signals]


def by_code(signals, code: str) -> dict:
    return next(s for s in signals if s["code"] == code)


def unique(prefix: str) -> str:
    return f"{prefix}{next(_names)}x{os.getpid()}"


def row(pid=1, key="acme|delivery manager", dhash="", where="Sydney NSW", first=60, since=None, last=50, closed=False,
        length=500, source="seek", came_back="gone", now=NOW) -> ghost.Row:
    return ghost.Row(pid, key, dhash, frozenset(ghost.duplicates.place_words(where)), days_ago(first, now),
                     days_ago(first if since is None else since, now), days_ago(last, now), closed, length, source, came_back)


# ---------------------------------------------------------------------------
# Wording
# ---------------------------------------------------------------------------

class WordingTest(unittest.TestCase):
    def sign(self, title="Delivery Manager", text=AD):
        return next((s for s in ghost.text_signals(title, text) if s["code"] == "evergreen_wording"), None)

    def test_strong_phrases_in_the_title_or_the_start_of_the_ad_are_enough_alone(self):
        for phrase in ("Talent Community", "talent network", "talent bench", "general application",
                       "general applications", "evergreen position", "not a current vacancy", "no current vacancies"):
            with self.subTest(phrase=phrase):
                in_title = self.sign(title=f"Project Manager - {phrase}")
                in_start = self.sign(text=f"We are building a {phrase} for the next year. " + AD)
                self.assertEqual((in_title["weight"], in_start["weight"]), (5, 5), phrase)
                self.assertEqual(ghost.level_of([in_title]), "likely")
        found = self.sign(text="Join our Talent-Community today. " + AD)
        self.assertEqual(found["text"], 'Mentions "talent community"')  # normalised: case, hyphen
        self.assertIn("future roles", found["detail"])

    def test_ambiguous_phrases_are_strong_only_in_the_title_or_beside_a_call_to_apply(self):
        for phrase in ("talent pool", "TALENT PIPELINE", "ongoing recruitment", "pipeline of candidates",
                       "pipeline of talented people"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.sign(title=f"Project Manager - {phrase}")["weight"], 5)
                self.assertEqual(self.sign(text=f"Join our {phrase} today. " + AD)["weight"], 5)
                self.assertEqual(self.sign(text=f"Register now for our {phrase}. " + AD)["weight"], 5)
                plain = self.sign(text=f"We are building a {phrase} for the next year. " + AD)
                self.assertEqual(plain["weight"], 3, phrase)  # only "possible": nothing says it is for applying
                self.assertEqual(ghost.level_of([plain]), "possible")
        # the call has to be close
        far = "Please apply. " + "We build things and ship them to customers every day. " * 3 + "We keep a talent pool. " + AD
        self.assertEqual(self.sign(text=far)["weight"], 3)
        # down a long ad it is a footer, with or without the call
        self.assertEqual(self.sign(text=(AD * 12) + "Join our talent pool.")["weight"], 1)
        found = self.sign(text="Join our talent pool, we are not hiring for a specific role. " + AD)
        self.assertEqual((found["weight"], ghost.level_of([found])), (5, "likely"))

    def test_recruiter_hr_and_sales_ads_name_these_phrases_as_their_duties(self):
        duties = {
            "Talent Acquisition Partner": "Build a pipeline of candidates for our engineering teams and manage the talent pool. ",
            "Senior Recruiter": "You will maintain our talent pipeline and screen applicants. ",
            "Sourcing Specialist": "Grow a pipeline of professionals. ",
            "HR Business Partner": "Run ongoing recruitment with our hiring managers. ",
            "Human Resources Advisor": "Keep the talent pool warm. ",
            "Account Executive, Sales": "Build a pipeline of people who buy from us. ",
            "Business Development Manager": "Build a pipeline of candidates for partnerships. ",
        }
        for title, text in duties.items():
            with self.subTest(title=title):
                signals = ghost.text_signals(title, text + AD)
                self.assertNotIn("evergreen_wording", codes(signals))
                self.assertIsNone(ghost.level_of(signals))
        # ... but the ad of any other role that says so is still read
        self.assertEqual(self.sign(title="Project Manager", text="Join our talent pipeline. " + AD)["weight"], 5)

    def test_normal_wording_for_pooled_roles_is_at_most_possible(self):
        teacher = self.sign(title="Casual Teacher", text="We run ongoing recruitment for casual teachers in the region. " + AD)
        self.assertEqual((teacher["weight"], ghost.level_of([teacher])), (3, "possible"))
        sales = ghost.text_signals("Account Executive", "You will build a pipeline of people to sell to. " + AD)
        self.assertEqual(ghost.level_of(sales), "possible")
        nurse = self.sign(title="Registered Nurse", text="Ongoing recruitment for our wards. " + AD)
        self.assertNotEqual(ghost.level_of([nurse]), "likely")

    def test_moderate_phrases_alone_are_not_even_possible(self):
        title = self.sign(title="Register your interest - Graduate Program")
        head = self.sign(text="Register your interest in our graduate program. " + AD)
        self.assertEqual((title["weight"], head["weight"]), (2, 2))
        self.assertEqual([ghost.level_of([title]), ghost.level_of([head])], [None, None])
        self.assertEqual(ghost.level_of([head, ghost.signal("many_similar", 1, "x")]), "possible")  # with a second sign
        for phrase in ("an expression of interest", "future opportunities that may arise", "should any arise",
                       "we are always hiring", "ongoing hiring", "registrations of interest", "keep your CV on file",
                       "future vacancies which may become available"):
            self.assertEqual(self.sign(text=f"{phrase} for good people. " + AD)["weight"], 2, phrase)

    def test_an_expression_of_interest_for_an_acting_or_closing_role_is_not_a_sign(self):
        self.assertIsNone(self.sign(title="Expression of Interest - Senior Project Officer (higher duties)"))
        self.assertIsNone(self.sign(title="Expression of Interest - Acting Director"))
        self.assertIsNone(self.sign(title="Expressions of Interest - Project Officer",
                                    text="A 12 month secondment opportunity. " + AD))
        self.assertIsNone(self.sign(title="Expression of Interest - Project Officer", text="Closing date: 30 October. " + AD))
        self.assertEqual(self.sign(title="Expression of Interest - Project Officer")["weight"], 2)  # otherwise a weak sign
        # ... and it doesn't hide another phrase
        self.assertEqual(self.sign(title="Expression of Interest - Acting Director",
                                   text="Join our talent community. " + AD)["text"], 'Mentions "talent community"')

    def test_a_mention_far_down_a_long_ad_is_a_footer_and_counts_little(self):
        body = (AD * 12)  # well over the short-ad limit
        self.assertGreater(len(body), ghost.SHORT_AD_CHARS + ghost.HEAD_CHARS)
        for phrase in ("Not the right role? Join our talent community.", "We will keep your details on file for future opportunities."):
            found = self.sign(text=body + phrase)
            self.assertEqual(found["weight"], 1, phrase)
            self.assertIsNone(ghost.level_of([found]))
        self.assertEqual(self.sign(text="Join our talent community. " + body)["weight"], 5)  # the same phrase up front
        self.assertEqual(self.sign(text="Talent community " + AD)["weight"], 5)  # a short ad: anywhere counts
        self.assertEqual(self.sign(text=AD + "Join our talent community.")["weight"], 5)

    def test_phrases_real_ads_use_are_left_alone(self):
        for text in ("This is an ongoing role with a permanent contract.", "Applications are reviewed on a rolling basis.",
                     "We hire talented people who love shipping.", "A swimming pool on site and a great talent for cooking.",
                     "Help build a pipeline of work for the team.", "Ongoing opportunity in the public service.",
                     "A general application development background.x", "Experience with data pipelines.",
                     "Future growth is strong.", "talentpool of one", "We anticipate the future needs of our users.",
                     "You can progress to future roles in leadership.", "We are always looking for ways to improve.",
                     "Always looking after our customers."):
            with self.subTest(text=text):
                self.assertIsNone(self.sign(text=text + " " + AD))

    def test_word_boundaries_and_spacing(self):
        self.assertIsNotNone(self.sign(text="our   TALENT\n pool is open. " + AD))
        self.assertIsNone(self.sign(text="the talent pools of oceans " + AD))  # (the plural is not the phrase)
        self.assertIsNone(self.sign(text="mytalent pool " + AD))

    def test_the_strongest_hit_is_the_one_reported(self):
        found = self.sign(title="Expression of interest", text="Join our talent pool. " + AD)
        self.assertEqual((found["weight"], found["text"]), (5, 'Mentions "talent pool"'))

    def test_recruiter_wording_counts_only_for_a_recruiter(self):
        hit = [s for s in ghost.text_signals("Analyst", "On behalf of our client, a leading bank. " + AD) if s["code"] == "recruiter_no_employer"]
        self.assertEqual(len(hit), 1)
        self.assertEqual((hit[0]["weight"], hit[0]["when"]), (1, "recruiter"))
        self.assertEqual(hit[0]["text"], "Recruiter ad: the employer may not be named")  # (it may name the employer)
        for text in ("A confidential client in finance.", "my client is hiring", "A client of ours in tech", "Confidential company, Sydney"):
            self.assertTrue(any(s["code"] == "recruiter_no_employer" for s in ghost.text_signals("Analyst", text + " " + AD)), text)
        for text in ("We serve clients across Australia.", "Keep client data confidential."):
            self.assertFalse(any(s["code"] == "recruiter_no_employer" for s in ghost.text_signals("Analyst", text)), text)


class LevelTest(unittest.TestCase):
    def level(self, *weights):
        return ghost.level_of([{"code": "x", "weight": w} for w in weights])

    def test_levels(self):
        self.assertIsNone(self.level())
        self.assertIsNone(self.level(1))
        self.assertIsNone(self.level(1, 1))
        self.assertEqual(self.level(1, 1, 1), "possible")
        self.assertEqual(self.level(3), "possible")
        self.assertEqual(self.level(5), "likely")  # one strong sign
        self.assertEqual(self.level(1, 5), "likely")
        self.assertEqual(self.level(3, 3), "likely")  # two moderate ones
        self.assertEqual(self.level(4, 2), "likely")

    def test_a_few_weak_signs_never_make_likely(self):
        weak = [{"code": c, "weight": 1} for c in ("long_open", "reposted", "date_refreshed", "many_similar", "recruiter_no_employer")]
        self.assertEqual(ghost.level_of(weak), "possible")  # even all of them together
        self.assertEqual(ghost.level_of(weak + [{"code": "company_news_cuts", "weight": 2}]), "possible")  # 7, but only one moderate sign

    def percent(self, *weights):
        return ghost.percent_of([{"code": "x", "weight": w} for w in weights])

    def test_the_percentage_grows_with_the_signs(self):
        self.assertEqual(self.percent(), 5)  # no signs: low, never 0 (the app can't know)
        self.assertEqual([self.percent(1), self.percent(1, 1), self.percent(2)], [15, 25, 25])
        self.assertEqual([self.percent(3), self.percent(3, 1), self.percent(3, 2)], [35, 40, 45])  # possible
        self.assertEqual([self.percent(5), self.percent(3, 3), self.percent(5, 3), self.percent(5, 5)], [70, 75, 85, 95])  # likely
        self.assertEqual(self.percent(5, 5, 5, 3, 2, 1), 95)  # never 100
        self.assertEqual(self.percent(*[1] * 12), 60)  # weak signs alone stay in the "possible" band

    def test_the_percentage_never_disagrees_with_the_level(self):
        """Every combination of up to five signs: an unflagged job is under 30%, a possible one 35 to 60, a likely
        one 70 or more, so the card's number, the filter, the digest and the export tell one story."""
        bands = {None: (5, 25), "possible": (35, 60), "likely": (70, 95)}
        for n in range(6):
            for weights in itertools.combinations_with_replacement((1, 2, 3, 5), n):
                low, high = bands[self.level(*weights)]
                self.assertTrue(low <= self.percent(*weights) <= high, weights)
                self.assertEqual(self.percent(*weights) % 5, 0, weights)  # (the colour steps are tenths, rounded)

    def test_strength_in_words(self):
        self.assertEqual([ghost.strength_of(w) for w in (1, 2, 3, 5, 6)], ["weak", "moderate", "moderate", "strong", "strong"])

    def test_the_strongest_sign_comes_first(self):
        found = ghost.ordered([ghost.signal("many_similar", 1, "a"), ghost.signal("long_open", 3, "b"),
                               ghost.signal("evergreen_wording", 5, "c"), ghost.signal("reposted", 1, "d")])
        self.assertEqual(codes(found), ["evergreen_wording", "long_open", "reposted", "many_similar"])


class AdSignsTest(unittest.TestCase):
    def test_the_llms_phrases_are_cleaned(self):
        self.assertEqual(ghost.clean_phrases(["  No specific team. ", "no specific team", "x" * 300, 5, None, "", "a", "b", "c"]),
                         ["No specific team", "x" * 100, "a", "b"])
        for bad in (None, "text", {"a": 1}, 3, [None]):
            self.assertEqual(ghost.clean_phrases(bad), [])

    def test_phrases_are_the_ads_own_words_and_never_an_accusation(self):
        ad = "We are building a talent pool for future roles. Send your resume and we may contact you."
        self.assertEqual(ghost.clean_phrases(["Building a TALENT POOL for future roles", "we may contact you"], ad),
                         ["Building a TALENT POOL for future roles", "we may contact you"])
        # text from the candidate's documents, or the model's own opinion, is not the ad's
        self.assertEqual(ghost.clean_phrases(["Jane Doe, Senior Engineer at Initech since 2019", "no specific team"], ad), [])
        self.assertEqual(ghost.clean_phrases(["talent pool run by Initech payroll"], ad), [])  # (under 80% of the words)
        self.assertEqual(ghost.clean_phrases(["is", "!!"], ad), [])  # (nothing to compare)
        # accusing words are dropped, in the ad's words or not
        for phrase in ("this is a scam", "Fake company", "possible fraud: talent pool", "a ghost job"):
            self.assertEqual(ghost.clean_phrases([phrase, "talent pool"], phrase + " talent pool"), ["talent pool"], phrase)
            self.assertEqual(ghost.clean_phrases([phrase]), [])  # (also without an ad to check against: stored ones)

    def test_signal_and_weight(self):
        self.assertIsNone(ghost.ad_signs_signal(None))
        self.assertIsNone(ghost.ad_signs_signal("[]"))
        self.assertIsNone(ghost.ad_signs_signal("not json"))
        one = ghost.ad_signs_signal('["no specific team or start date"]')
        two = ghost.ad_signs_signal('["no specific team", "collects resumes"]')
        self.assertEqual((one["weight"], two["weight"]), (2, 3))
        self.assertEqual(one["text"], "AI read of the ad: no specific team or start date")
        self.assertIn("can be wrong", one["detail"])
        self.assertIsNone(ghost.level_of([one]))  # one phrase is not even "possible" alone
        self.assertEqual(ghost.level_of([two]), "possible")  # an LLM's word alone never makes "likely"
        self.assertEqual(ghost.level_of([ghost.ad_signs_signal('["a", "b", "c", "d"]')]), "possible")
        self.assertEqual(ghost.level_of([one, ghost.signal("many_similar", 1, "x")]), "possible")
        self.assertIsNone(ghost.ad_signs_signal('["a scam", "fake ad"]'))  # stored before the filter existed


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

class TimeSignalsTest(unittest.TestCase):
    def signals(self, first=10, posted=None, since=None, last=0, closed=False, source=None):
        return ghost.time_signals(days_ago(first), days_ago(first if since is None else since), days_ago(last), closed,
                                  posted, NOW, source)

    def test_long_open_grows_with_age(self):
        self.assertEqual(self.signals(first=44), [])
        for days, weight in ((45, 1), (59, 1), (60, 3), (89, 3), (90, 5), (200, 5)):
            (found,) = self.signals(first=days)
            self.assertEqual((found["code"], found["weight"], found["text"]), ("long_open", weight, f"Open for {days} days"))
        self.assertEqual(ghost.level_of(self.signals(first=90)), "likely")
        self.assertEqual(ghost.level_of(self.signals(first=60)), "possible")
        self.assertIsNone(ghost.level_of(self.signals(first=50)))

    def test_only_while_it_is_still_listed(self):
        self.assertEqual(self.signals(first=100, closed=True), [])
        self.assertEqual(self.signals(first=100, last=15), [])  # not shown by a listing for over two weeks
        self.assertEqual(len(self.signals(first=100, last=13)), 1)

    def test_an_earlier_posted_date_moves_the_start_back(self):
        (found,) = self.signals(first=10, posted=days_ago(70).isoformat())
        self.assertEqual(found["text"], "Open for 70 days")
        self.assertEqual(self.signals(first=10, posted="3 days ago"), [])  # free text isn't a date
        self.assertEqual(self.signals(first=10, posted="2026-10-30"), [])  # nor is one in the future

    def test_a_renewed_seek_ad_is_not_called_long_open_for_its_renewal(self):
        # Seen 40 days ago, posted date moved to 2 days ago: renewed once (SEEK ads run 30 days). Only the weak
        # "renewed" sign, which never flags alone.
        found = self.signals(first=40, posted=days_ago(2).isoformat())
        self.assertEqual(codes(found), ["date_refreshed"])
        self.assertEqual(found[0]["text"], "Posted date renewed (first listed 29 August)")
        self.assertIsNone(ghost.level_of(found))
        # Still not enough at 50 days (long_open 45+ is weak), but at 60 days it is "possible", with the renewal in the reasons.
        self.assertIsNone(ghost.level_of(self.signals(first=50, posted=days_ago(2).isoformat())))
        sixty = self.signals(first=60, posted=days_ago(2).isoformat())
        self.assertEqual(sorted(codes(sixty)), ["date_refreshed", "long_open"])
        self.assertEqual(ghost.level_of(sixty), "possible")
        # A posted date only a little after first sight is just the ad's own date.
        self.assertEqual(self.signals(first=30, posted=days_ago(15).isoformat()), [])

    def test_a_closed_and_reopened_ad_starts_again(self):
        # First seen 100 days ago, closed, listed again 5 days ago. Its old posted date doesn't make it long open.
        found = self.signals(first=100, since=5, posted=days_ago(100).isoformat())
        self.assertEqual(found, [])
        (found,) = self.signals(first=100, since=50)
        self.assertEqual(found["text"], "Open for 50 days")
        self.assertTrue(ghost.reopened(days_ago(100), days_ago(5)))
        self.assertFalse(ghost.reopened(days_ago(100), days_ago(99.5)))

    def test_the_seek_sentence_is_only_for_seek(self):
        (seek,) = self.signals(first=100, source="seek")
        (other,) = self.signals(first=100, source="apsjobs:all")
        (none,) = self.signals(first=100)
        self.assertIn("SEEK ads run 30 days", seek["detail"])
        self.assertNotIn("SEEK", other["detail"])
        self.assertNotIn("SEEK", none["detail"])

    def test_an_edit_date_is_not_a_posted_date(self):
        # Greenhouse and Atlassian store the ad's last-edit date: edited 3 weeks after the pool first saw it is not "renewed".
        edited = days_ago(2).isoformat()
        for source in ("greenhouse:acme", "greenhouse", "atlassian"):
            self.assertEqual(self.signals(first=40, posted=edited, source=source), [], source)
        self.assertEqual(codes(self.signals(first=40, posted=edited, source="ashby:acme")), ["date_refreshed"])  # a posting date
        self.assertEqual(codes(self.signals(first=40, posted=edited, source="seek")), ["date_refreshed"])
        # nor does it move the start back
        (found,) = self.signals(first=50, posted=days_ago(120).isoformat(), source="greenhouse:acme")
        self.assertEqual(found["text"], "Open for 50 days")
        (found,) = self.signals(first=50, posted=days_ago(120).isoformat(), source="lever:acme")
        self.assertEqual(found["text"], "Open for 120 days")

    def test_an_ambiguous_day_month_date_does_not_make_an_ad_look_months_old(self):
        # "10/01/2026" on a US page is 1 October: read day-first it would be "Open for 271 days".
        self.assertEqual(self.signals(first=7, posted="10/01/2026"), [])
        (found,) = self.signals(first=7, posted="13/01/2026")  # unambiguous: the 13th
        self.assertEqual(found["text"], "Open for 268 days")
        (found,) = self.signals(first=7, posted="01/13/2026")
        self.assertEqual(found["text"], "Open for 268 days")

    def test_posted_dates(self):
        for raw, expect in (("2026-08-01", datetime(2026, 8, 1)), ("2026-08-01T10:00:00Z", datetime(2026, 8, 1, 10)),
                            ("2026-08-01T10:00:00+10:00", datetime(2026, 8, 1, 0)), ("12 June 2026", datetime(2026, 6, 12)),
                            ("3 Oct 2026", datetime(2026, 10, 3)), ("October 3, 2026", datetime(2026, 10, 3)),
                            ("13/10/2026", datetime(2026, 10, 13)), ("10/13/2026", datetime(2026, 10, 13)),
                            ("03/03/2026", datetime(2026, 3, 3)), ("03/10/2026", None), ("31/02/2026", None),  # (ambiguous: unread)
                            ("Wed, 02 Oct 2026 13:00:00 GMT", datetime(2026, 10, 2, 13)),
                            ("Wed, 02 Oct 2026 13:00:00 +1000", datetime(2026, 10, 2, 3)),
                            ("Posted 3 days ago", None), ("Posted 30+ Days Ago", None), ("", None), (None, None),
                            ("1970-01-01", None)):
            self.assertEqual(ghost.parse_posted(raw), expect, raw)


class NewsTest(unittest.TestCase):
    def profile(self, items, status="done", researched=NOW - timedelta(days=20), note="", name="Newsco"):
        return CompanyProfile(key=name.lower(), name=name, status=status, controversies_json=json.dumps(items),
                              controversy_note=note, news_researched_at=researched)

    def test_cuts_in_the_last_twelve_months(self):
        for title in ("Newsco announces 400 layoffs", "Redundancies hit Newsco staff", "Newsco job cuts continue",
                      "Hiring freeze at Newsco", "Newsco cutting 200 jobs", "Newsco to axe 300 roles", "Newsco retrenchments"):
            found = ghost.news_signal(self.profile([{"title": title, "year": "2026", "summary": ""}]), NOW)
            self.assertEqual((found["code"], found["weight"]), ("company_news_cuts", 2), title)
        found = ghost.news_signal(self.profile([{"title": "Fine", "year": "2026", "summary": "after the layoffs in July"}]), NOW)
        self.assertIsNotNone(found)
        self.assertIn("From the company research", ghost.news_signal(self.profile([{"title": "Newsco layoffs", "year": "2026"}]), NOW)["detail"])
        self.assertIsNone(ghost.level_of([found]))  # context, not a flag by itself

    def test_what_does_not_count(self):
        old = self.profile([{"title": "Newsco layoffs", "year": "2024", "summary": ""}])
        self.assertIsNone(ghost.news_signal(old, NOW))
        self.assertIsNone(ghost.news_signal(self.profile([{"title": "Data breach", "year": "2026", "summary": "fined"}]), NOW))
        self.assertIsNone(ghost.news_signal(self.profile([{"title": "layoffs", "year": "2026"}], status="error"), NOW))
        self.assertIsNone(ghost.news_signal(None, NOW))
        # The one-line note is not read ("no layoffs found" must not flag).
        self.assertIsNone(ghost.news_signal(self.profile([], note="No layoffs or redundancies found."), NOW))
        self.assertIsNone(ghost.news_signal(self.profile("not a list"), NOW))
        # An undated item counts only while the research itself is recent.
        undated = [{"title": "Newsco layoffs", "year": ""}]
        self.assertIsNotNone(ghost.news_signal(self.profile(undated), NOW))
        self.assertIsNone(ghost.news_signal(self.profile(undated, researched=NOW - timedelta(days=400)), NOW))
        self.assertIsNone(ghost.news_signal(self.profile(undated, researched=None), NOW))


# ---------------------------------------------------------------------------
# The pool around a posting
# ---------------------------------------------------------------------------

class PoolSignalsTest(unittest.TestCase):
    def test_posted_three_times(self):
        me = row(1, first=10, last=0)
        earlier = [row(2, first=100, last=80), row(3, first=60, last=40)]
        (found,) = ghost.pool_signals(me, earlier + [me], NOW)
        self.assertEqual((found["code"], found["weight"]), ("reposted", 5))
        self.assertEqual(found["text"], "Posted 3 times since 30 June")
        self.assertEqual(ghost.level_of([found]), "likely")
        (twice,) = ghost.pool_signals(me, earlier[1:], NOW)
        self.assertEqual((twice["weight"], twice["text"]), (1, "Posted 2 times since 9 August"))
        self.assertIsNone(ghost.level_of([twice]))

    def test_the_same_ad_on_two_boards_at_once_is_one_ad(self):
        me = row(1, first=10, last=0)
        elsewhere = row(2, first=12, last=0)  # still listed: at the same time
        also = row(3, first=10, last=1)
        self.assertEqual(ghost.pool_signals(me, [elsewhere, also], NOW), [])
        # ... also when the other copy was dropped from its listing the day this one appeared (not a day apart)
        self.assertEqual(ghost.pool_signals(me, [row(4, first=15, last=10)], NOW), [])
        # Two boards that each had it before count as one earlier appearance, not two.
        (found,) = ghost.pool_signals(me, [row(5, first=80, last=60), row(6, first=78, last=61)], NOW)
        self.assertEqual(found["text"], "Posted 2 times since 20 July")

    def test_what_is_not_the_same_role(self):
        me = row(1, first=10, last=0)
        self.assertEqual(ghost.pool_signals(me, [row(2, key="acme|analyst", first=100, last=80)], NOW), [])
        self.assertEqual(ghost.pool_signals(me, [row(3, where="Melbourne VIC", first=100, last=80)], NOW), [])  # another place
        self.assertEqual(len(ghost.pool_signals(me, [row(4, where="", first=100, last=80)], NOW)), 1)  # no place: could be here
        self.assertEqual(ghost.pool_signals(me, [row(5, first=400, last=300)], NOW), [])  # over the window
        single = row(6, key="acme|nurse", first=10, last=0)
        self.assertEqual(ghost.pool_signals(single, [row(7, key="acme|nurse", first=100, last=80)], NOW), [])  # one-word title

    def test_the_same_description_is_the_same_role_whatever_the_title(self):
        me = row(1, key="acme|delivery manager", dhash="h1", first=10, last=0)
        other = row(2, key="beta|program lead", dhash="h1", first=100, last=80)
        (found,) = ghost.pool_signals(me, [other], NOW)
        self.assertEqual(found["code"], "reposted")
        short = row(3, key="beta|program lead", dhash="h1", first=100, last=80, length=50)
        self.assertEqual(ghost.pool_signals(me, [short], NOW), [])  # a teaser that short proves nothing
        self.assertEqual(ghost.pool_signals(row(4, dhash=""), [row(5, key="x|y z", dhash="", first=100, last=80)], NOW), [])

    def test_a_closed_and_reopened_ad_counts_its_earlier_run(self):
        for why in ("gone", "unlisted"):
            me = row(1, first=100, since=5, last=0, came_back=why)
            (found,) = ghost.pool_signals(me, [], NOW)
            self.assertEqual((found["text"], found["weight"]), ("Posted 2 times since 30 June", 1))
        self.assertEqual(ghost.pool_signals(row(2, first=100, since=100, last=0), [], NOW), [])

    def test_an_ad_that_was_only_not_listed_for_a_while_did_not_come_back(self):
        # Closed as "stale" (no search listed it for 45 days, for example because the criteria changed), then listed again.
        self.assertEqual(ghost.pool_signals(row(1, first=100, since=5, last=0, came_back="stale"), [], NOW), [])
        self.assertEqual(ghost.pool_signals(row(1, first=100, since=5, last=0, came_back=""), [], NOW), [])

    def test_a_copy_found_through_another_source_is_not_an_earlier_posting(self):
        # SEEK's copy was last listed 30 days ago; the company's own site was crawled 28 days ago (it was added later).
        seek = row(2, first=60, last=30, source="seek")
        site = row(1, first=28, last=0, source="site:acme.com")
        self.assertEqual(ghost.pool_signals(site, [seek], NOW), [])
        # ... also by the same description, and a third board adds nothing
        third = row(3, first=5, last=0, source="greenhouse:acme")
        self.assertEqual(ghost.pool_signals(third, [seek, site], NOW), [])
        self.assertEqual(ghost.pool_signals(row(4, dhash="h", first=28, last=0, source="site:acme.com"),
                                            [row(5, key="other|role two", dhash="h", first=60, last=30)], NOW), [])
        # the same source posting it again is a repost, and a source that isn't known counts as before
        self.assertEqual(len(ghost.pool_signals(row(6, first=28, last=0), [row(7, first=60, last=30)], NOW)), 1)
        self.assertEqual(len(ghost.pool_signals(row(8, first=28, last=0, source=""), [seek], NOW)), 1)

    def test_many_similar(self):
        me = row(1, first=10, last=0)
        four = [row(i, first=5, last=0) for i in range(2, 5)]  # 3 others + me = 4
        self.assertEqual(ghost.pool_signals(me, four, NOW), [])
        five = four + [row(9, first=5, last=0)]
        (found,) = ghost.pool_signals(me, five, NOW)
        self.assertEqual((found["code"], found["weight"], found["text"]), ("many_similar", 1, "5 open ads with this title at this company"))
        self.assertIsNone(ghost.level_of([found]))
        closed = [row(i, first=5, last=0, closed=True) for i in range(10, 20)]
        self.assertEqual(ghost.pool_signals(me, four + closed, NOW), [])  # closed ones aren't open
        old = [row(i, first=90, last=40) for i in range(20, 30)]
        self.assertNotIn("many_similar", codes(ghost.pool_signals(me, four + old, NOW)))  # not listed for weeks

    def test_a_big_employer_hiring_for_many_different_roles_is_not_a_mass_posting(self):
        me = row(1, key="bigco|delivery manager", first=10, last=0)
        others = [row(100 + i, key=f"bigco|role {chr(97 + i % 26)}{i} lead", first=10, last=0) for i in range(60)]
        self.assertEqual(ghost.pool_signals(me, others, NOW), [])


# ---------------------------------------------------------------------------
# Planted postings and the whole path
# ---------------------------------------------------------------------------

class Plant:
    """Postings and jobs written straight into the test database (relative to NOW)."""

    ws = None

    @classmethod
    def workspace(cls):
        return helpers.new_workspace(unique("ghost-ws"))

    @staticmethod
    def posting(db, company, title, *, first=0, last=0, since=None, closed=None, text=None, where="Sydney NSW", posted=None,
                source="seek", hash_of=None, url=None) -> Posting:
        text = text if text is not None else AD + f" Reference {unique('ref')}."  # (an ad of its own: equal text is the same ad)
        p = Posting(url=url or f"https://ads.example/{unique('p')}", source=source, company=company, title=title,
                    location_text=where, description=text, detail_status="full", rev=1, posted_at=posted,
                    first_seen=days_ago(first), listed_since=days_ago(first if since is None else since),
                    last_listed_at=days_ago(last), closed_at=None if closed is None else days_ago(closed))
        pool.derive(p)
        if hash_of is not None:
            p.description_hash = hash_of
        ghost.stamp(p)
        db.add(p)
        db.flush()
        return p

    @staticmethod
    def job(db, ws, posting=None, **kw) -> Job:
        job = Job(workspace_id=ws, origin="pool" if posting is not None else "private", status="to_review",
                  posting=posting, **kw)
        if posting is None:
            job.url = f"https://private.example/{unique('j')}"
        db.add(job)
        db.flush()
        return job


class StoredSignsTest(unittest.TestCase, Plant):
    def setUp(self):
        self.ws, self.other_ws = self.workspace(), self.workspace()  # (made first: nothing is waiting to be written)
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        self.company = unique("Ghostco")
        self.title = "Delivery Program Manager"
        self.ad = AD + f" Reference {self.company}."

    def refresh(self, now=NOW):
        ghost.refresh(self.db, now)
        self.db.commit()
        self.db.expire_all()

    def assess(self, posting, ws=None):
        job = self.job(self.db, ws or self.ws, posting)
        self.db.commit()
        return ghost.assess_jobs(self.db, [job], {}, NOW)[job.id]

    def test_posted_three_times_over_months(self):
        self.posting(self.db, self.company, self.title, first=100, last=85, closed=40)
        self.posting(self.db, self.company, self.title, first=60, last=45, closed=10)
        now_ad = self.posting(self.db, self.company, self.title, first=5, last=0)
        self.refresh()
        out = self.assess(now_ad)
        self.assertEqual(out["level"], "likely")
        self.assertEqual(out["signals"][0], {"code": "reposted", "text": "Posted 3 times since 30 June",
                                              "detail": out["signals"][0]["detail"], "strength": "strong"})

    def test_the_closed_ones_count_and_so_do_other_urls_and_external_ids(self):
        self.posting(self.db, self.company.upper(), self.title.lower() + " (6 month contract)", first=90, last=70, closed=20,
                     url="https://other.example/a")
        mine = self.posting(self.db, self.company + " Pty Ltd", self.title, first=3, last=0)
        self.refresh()
        out = self.assess(mine)
        self.assertEqual(codes(out["signals"]), ["reposted"])  # (company suffix, case and a contract term don't matter)
        self.assertIsNone(out["level"])  # twice is weak

    def test_the_same_ad_on_two_boards_at_once_is_not_a_repost(self):
        a = self.posting(self.db, self.company, self.title, first=20, last=0, source="seek", text=self.ad)
        b = self.posting(self.db, self.company, self.title, first=19, last=0, source="indeed", text=a.description)  # same text, too
        self.refresh()
        self.assertEqual(self.assess(a)["signals"], [])
        self.assertEqual(self.assess(b)["signals"], [])

    def test_the_same_text_under_another_title_is_a_repost(self):
        self.posting(self.db, self.company, "Program Lead", first=90, last=60, closed=50, text=self.ad + "unique to this role " * 10)
        mine = self.posting(self.db, self.company, "Delivery Manager", first=10, last=0, text=self.ad + "unique to this role " * 10)
        self.refresh()
        self.assertEqual(codes(self.assess(mine)["signals"]), ["reposted"])

    def test_a_closed_and_reopened_posting_is_one_ad_that_came_back(self):
        # Through the real upsert path: listed 100 days ago, closed as stale, listed again 5 days ago.
        url = f"https://ads.example/{unique('reopen')}"
        scraped = Scraped(url=url, title=self.title, company=self.company, location_text="Sydney NSW",
                          description=self.ad, detail_status="full")
        posting, _ = pool.upsert_scraped(self.db, scraped, "seek", None, days_ago(100))
        self.db.flush()
        pool.upsert_scraped(self.db, scraped, "seek", None, days_ago(80))
        pool.close(posting, "gone", days_ago(30))
        posting, new = pool.upsert_scraped(self.db, scraped, "seek", None, days_ago(5))
        pool.upsert_scraped(self.db, scraped, "seek", None, days_ago(0))
        self.db.commit()
        self.assertFalse(new)
        self.assertEqual(posting.listed_since, days_ago(5))
        self.assertEqual(posting.reopened_from, "gone")
        self.refresh()
        out = self.assess(posting)
        self.assertEqual(codes(out["signals"]), ["reposted"])  # not long_open: it wasn't open for those 100 days
        self.assertEqual(out["signals"][0]["text"], "Posted 2 times since 30 June")

    def test_a_posting_dropped_for_not_being_listed_and_listed_again_is_not_a_repost(self):
        # Closed as "stale" (the search changed and nothing listed it for 45 days), then a search found it again.
        url = f"https://ads.example/{unique('stale')}"
        scraped = Scraped(url=url, title=self.title, company=self.company, location_text="Sydney NSW",
                          description=self.ad, detail_status="full")
        posting, _ = pool.upsert_scraped(self.db, scraped, "seek", None, days_ago(100))
        self.db.flush()
        pool.close(posting, "stale", days_ago(30))
        posting, _ = pool.upsert_scraped(self.db, scraped, "seek", None, days_ago(5))
        self.db.commit()
        self.assertEqual((posting.reopened_from, posting.listed_since), ("stale", days_ago(5)))
        self.refresh()
        self.assertEqual(self.assess(posting)["signals"], [])  # (and not "open for 100 days" either)

    def test_one_ad_found_on_boards_at_different_times_is_not_a_repost(self):
        # SEEK ran the ad for 30 days; the company's own site was added as a source 32 days after the pool first saw it,
        # and a third board two months after that. The employer posted it once.
        seek = self.posting(self.db, self.company, self.title, first=100, last=70, closed=65, source="seek", text=self.ad)
        site = self.posting(self.db, self.company, self.title, first=68, last=40, closed=35, source="site:acme.example", text=self.ad)
        third = self.posting(self.db, self.company, self.title, first=5, last=0, source="greenhouse:acme", text=self.ad)
        self.refresh()
        for posting in (site, third):
            self.assertEqual(self.assess(posting)["signals"], [], posting.source)
        # the same source listing it again after it ended is
        again = self.posting(self.db, self.company, self.title, first=3, last=0, source="seek", text=self.ad + " Again.")
        self.refresh()
        self.assertEqual(codes(self.assess(again)["signals"]), ["reposted"])
        self.assertEqual(seek.source, "seek")

    def test_a_big_employer_with_many_different_roles(self):
        roles = ["Data Engineer", "Payroll Officer", "Network Architect", "Legal Counsel", "Brand Manager", "Safety Advisor",
                 "Fleet Coordinator", "Tax Accountant", "Product Designer", "Site Supervisor", "Trainer Assessor", "Buyer"]
        postings = [self.posting(self.db, self.company, t, first=20 + i, last=0, text=self.ad + t * 40) for i, t in enumerate(roles)]
        self.refresh()
        for p in postings:
            self.assertEqual(self.assess(p)["signals"], [], p.title)

    def test_mass_posting_is_a_weak_sign(self):
        postings = [self.posting(self.db, self.company, self.title, first=3, last=0, where=place, text=self.ad + place)
                    for place in ("Sydney NSW", "Sydney NSW Parramatta", "Sydney CBD", "North Sydney", "Sydney Olympic Park")]
        self.refresh()
        out = self.assess(postings[0])
        self.assertEqual(codes(out["signals"]), ["many_similar"])
        self.assertIsNone(out["level"])
        self.assertEqual(out["signals"][0]["text"], "5 open ads with this title at this company")

    def test_refresh_is_incremental_and_cheap(self):
        for i in range(3):
            self.posting(self.db, self.company, self.title, first=40 + i, last=0, where=f"Town{i}", text=AD + str(i))
        done = ghost.refresh(self.db, NOW)
        self.assertGreaterEqual(done, 3)
        self.db.commit()
        self.assertEqual(ghost.refresh(self.db, NOW + timedelta(hours=2)), 0)  # nothing is due again so soon
        self.assertGreaterEqual(ghost.refresh(self.db, NOW + timedelta(days=2)), 3)
        # a limit caps one run, oldest first
        self.assertEqual(ghost.refresh(self.db, NOW + timedelta(days=5), limit=2), 2)

    def test_closed_postings_are_history_not_worked_out(self):
        gone = self.posting(self.db, self.company, self.title, first=90, last=60, closed=50)
        self.assertIsNotNone(gone.repost_key)  # (kept as history for later ads)
        self.assertIsNone(gone.ghost_checked_at)
        self.refresh()
        self.db.refresh(gone)
        self.assertIsNone(gone.ghost_checked_at)

    def test_postings_from_before_this_feature_are_filled_in(self):
        p = Posting(url=f"https://ads.example/{unique('old')}", company=self.company, title=self.title, location_text="Sydney NSW",
                    description="Join our talent pool. " + AD, detail_status="full", first_seen=days_ago(30), last_listed_at=days_ago(0))
        self.db.add(p)
        self.db.commit()
        self.assertIsNone(p.ghost_json)
        self.refresh()
        self.db.refresh(p)
        self.assertEqual(p.repost_key, ghost.pool_key(self.company, self.title, "Sydney NSW"))
        self.assertEqual(codes(json.loads(p.ghost_json)["text"]), ["evergreen_wording"])
        self.assertIsNotNone(p.ghost_checked_at)

    def test_a_changed_ad_clears_the_ai_reading_of_the_old_one(self):
        url = f"https://ads.example/{unique('chg')}"
        old = Scraped(url=url, title=self.title, company=self.company, location_text="Sydney NSW", description=SAYS_AD,
                      detail_status="full")
        posting, _ = pool.upsert_scraped(self.db, old, "seek", None, days_ago(10))
        posting.ad_ghost_json = json.dumps([SAYS])
        self.db.commit()
        # a different title (or source) alone keeps it: the ad's wording is the same
        pool.upsert_scraped(self.db, Scraped(url=url, title=self.title + " II", company=self.company, location_text="Sydney NSW",
                                             description=SAYS_AD, detail_status="full"), "indeed", None, days_ago(9))
        self.db.commit()
        self.assertEqual(json.loads(posting.ad_ghost_json), [SAYS])
        # the employer rewrites the ad
        pool.upsert_scraped(self.db, Scraped(url=url, title=self.title + " II", company=self.company, location_text="Sydney NSW",
                                             description=AD + "A new team of five joins in March.", detail_status="full"),
                            "indeed", None, days_ago(8))
        self.db.commit()
        self.assertIsNone(posting.ad_ghost_json)
        # a summary replaced by the full ad is a change of the ad, too
        summary = Scraped(url=url + "-2", title=self.title, company=self.company, location_text="Sydney NSW",
                          description="Short teaser.", detail_status="summary")
        other, _ = pool.upsert_scraped(self.db, summary, "seek", None, days_ago(7))
        other.ad_ghost_json = "[]"
        pool.apply_detail(other, Scraped(url=url + "-2", title=self.title, company=self.company, location_text="Sydney NSW",
                                         description=SAYS_AD, detail_status="full"), days_ago(6))
        self.db.commit()
        self.assertIsNone(other.ad_ghost_json)

    def test_a_changed_ad_is_stamped_again_by_the_pool(self):
        scraped = Scraped(url=f"https://ads.example/{unique('chg')}", title=self.title, company=self.company,
                          location_text="Sydney NSW", description="Short teaser.", detail_status="summary")
        posting, _ = pool.upsert_scraped(self.db, scraped, "seek", None, NOW)
        self.assertEqual(json.loads(posting.ghost_json)["text"], [])
        pool.apply_detail(posting, Scraped(url=scraped.url, title=self.title, company=self.company,
                                           description="Join our talent pool. " + AD, detail_status="full"), NOW)
        self.assertEqual(codes(json.loads(posting.ghost_json)["text"]), ["evergreen_wording"])
        self.assertEqual(posting.repost_key, ghost.pool_key(self.company, self.title, "Sydney NSW"))

    def test_another_workspaces_private_jobs_are_never_counted(self):
        mine = self.posting(self.db, self.company, self.title, first=3, last=0)
        other = self.other_ws
        for i in range(8):  # eight private jobs for the same role in someone else's workspace
            self.job(self.db, other, None, company=self.company, title=self.title, description=AD + str(i), detail_status="full")
        self.refresh()
        self.assertEqual(self.assess(mine)["signals"], [])
        # ... and a private job gets no pool signs at all, whatever the pool holds about the same role.
        self.posting(self.db, self.company, self.title, first=90, last=60, closed=50)
        self.refresh()
        private = self.job(self.db, self.ws, None, company=self.company, title=self.title, description=self.ad, detail_status="full")
        self.db.commit()
        self.assertEqual(ghost.assess_jobs(self.db, [private], {}, NOW)[private.id], ghost.NONE)

    def test_a_merged_copy_counts_only_as_a_different_posting_over_time(self):
        ws = self.ws
        main = self.posting(self.db, self.company, self.title, first=10, last=0, source="seek", text=self.ad)
        board_two = self.posting(self.db, self.company, self.title, first=10, last=0, source="seek", text=self.ad)  # at once
        self.refresh()
        main_job = self.job(self.db, ws, main)
        copy_job = self.job(self.db, ws, board_two, duplicate_of=main_job.id)
        self.db.commit()
        self.assertEqual(ghost.assess_jobs(self.db, [main_job], {}, NOW)[main_job.id]["signals"], [])
        # The same copy ended long before the main ad appeared: a different posting over time.
        self.db.query(Posting).filter(Posting.id == board_two.id).update(
            {Posting.first_seen: days_ago(90), Posting.last_listed_at: days_ago(60), Posting.closed_at: days_ago(30)})
        self.db.query(Posting).filter(Posting.id == main.id).update({Posting.ghost_checked_at: None})
        self.db.commit()
        self.refresh()
        self.db.expire_all()
        self.assertEqual(codes(ghost.assess_jobs(self.db, [self.db.get(Job, main_job.id)], {}, NOW)[main_job.id]["signals"]), ["reposted"])
        self.assertIsNotNone(copy_job.duplicate_of)


class JobReadTest(unittest.TestCase, Plant):
    """assess_jobs on planted jobs: text, research, the ad's own text, the LLM's reading, recruiters."""

    def setUp(self):
        self.ws, self.ws2 = self.workspace(), self.workspace()
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        self.company = unique("Readco")

    def read(self, job, profiles=None):
        self.db.commit()
        return ghost.assess_jobs(self.db, [job], profiles or {}, NOW)[job.id]

    def test_evergreen_wording_makes_likely(self):
        p = self.posting(self.db, self.company, "Project Manager", first=2, last=0, text="Join our talent pool of project managers. " + AD)
        out = self.read(self.job(self.db, self.ws, p))
        self.assertEqual((out["level"], codes(out["signals"])), ("likely", ["evergreen_wording"]))

    def test_a_recruiters_own_talent_pool_duty_is_not_a_flag(self):
        p = self.posting(self.db, self.company, "Talent Acquisition Partner", first=2, last=0,
                         text="Build a pipeline of candidates and manage the talent pool. " + AD)
        out = self.read(self.job(self.db, self.ws, p))
        self.assertEqual(out, {"level": None, "percent": 5, "signals": []})

    def test_long_open_and_news_together(self):
        p = self.posting(self.db, self.company, "Project Manager", first=65, last=0)
        profile = CompanyProfile(key=self.company.lower(), name=self.company, status="done", news_researched_at=days_ago(5),
                                 controversies_json=json.dumps([{"title": f"{self.company} layoffs", "year": "2026", "summary": ""}]))
        out = self.read(self.job(self.db, self.ws, p, company=None), {self.company.lower(): profile})
        self.assertEqual(codes(out["signals"]), ["long_open", "company_news_cuts"])
        self.assertEqual(out["level"], "possible")  # 3 + 2 = 5: under the 6 that two moderate signs need

    def test_a_job_with_no_posting_gets_text_and_research_only(self):
        job = self.job(self.db, self.ws, None, company=self.company, title="Analyst", detail_status="full",
                       description="We keep an evergreen position open. " + AD, first_seen=days_ago(300))
        out = self.read(job)
        self.assertEqual(codes(out["signals"]), ["evergreen_wording"])  # no long_open: no posting to know how long it was listed

    def test_a_job_reading_its_own_ad_is_judged_on_that_ad(self):
        # The pool's ad says "talent pool"; this workspace uploaded the full ad, which doesn't.
        p = self.posting(self.db, self.company, "Project Manager", first=2, last=0, text="Join our talent pool. " + AD)
        job = self.job(self.db, self.ws, p, description=AD + "Real specifics.", detail_status="full")
        self.assertEqual(self.read(job)["signals"], [])
        # ... and the other way round.
        p2 = self.posting(self.db, self.company, "Project Manager 2", first=2, last=0)
        job2 = self.job(self.db, self.ws, p2, description="Our talent community welcomes you. " + AD, detail_status="full")
        self.assertEqual(codes(self.read(job2)["signals"]), ["evergreen_wording"])

    def test_the_recruiter_sign_needs_a_recruiter(self):  # (its label: "the employer may not be named")
        text = "On behalf of our client we are hiring. " + AD
        p = self.posting(self.db, "Peak Talent Partners", "Analyst", first=2, last=0, text=text)
        out = self.read(self.job(self.db, self.ws, p))  # the name looks like a recruiter's
        self.assertEqual(codes(out["signals"]), ["recruiter_no_employer"])
        self.assertIsNone(out["level"])
        direct = self.posting(self.db, "Peak Widgets", "Analyst", first=2, last=0, text=text)
        self.assertEqual(self.read(self.job(self.db, self.ws, direct))["signals"], [])
        # research can say a name that looks like a recruiter's isn't one
        not_recruiter = CompanyProfile(key="peak talent partners", name="Peak Talent Partners", status="done", is_recruiter=False)
        again = self.posting(self.db, "Peak Talent Partners", "Analyst 2", first=2, last=0, text=text)
        self.assertEqual(self.read(self.job(self.db, self.ws, again), {"peak talent partners": not_recruiter})["signals"], [])

    def test_the_llms_reading_of_a_public_ad_is_on_the_posting(self):
        p = self.posting(self.db, self.company, "Project Manager", first=2, last=0)
        p.ad_ghost_json = json.dumps(["no specific team", "collects resumes for future roles"])
        out = self.read(self.job(self.db, self.ws, p))
        self.assertEqual(codes(out["signals"]), ["ad_signs"])
        self.assertEqual(out["level"], "possible")  # (two phrases; one is not enough alone)
        # a workspace reading its own ad ignores the posting's reading and uses its own
        own = self.job(self.db, self.ws2, p, description=AD, detail_status="full")
        self.assertEqual(self.read(own)["signals"], [])
        own.ad_ghost_json = json.dumps(["no start date"])
        read = self.read(own)
        self.assertEqual((codes(read["signals"]), read["level"]), (["ad_signs"], None))

    def test_signals_never_carry_weights_and_are_strongest_first(self):
        p = self.posting(self.db, self.company, "Project Manager", first=100, last=0, text="Join our talent pool. " + AD)
        out = self.read(self.job(self.db, self.ws, p))
        self.assertEqual(codes(out["signals"]), ["evergreen_wording", "long_open"])
        self.assertTrue(all(set(s) <= {"code", "text", "detail", "strength"} for s in out["signals"]))  # words, no weight
        self.assertEqual([s["strength"] for s in out["signals"]], ["strong", "strong"])
        self.assertEqual(set(out), {"level", "percent", "signals"})
        self.assertEqual((out["level"], out["percent"]), ("likely", 95))

    def explain(self, job, profiles=None):
        self.db.commit()
        out = ghost.assess_jobs(self.db, [job], profiles or {}, NOW, explain=True)[job.id]
        return out, {c["code"]: c for c in out["checks"]}

    def test_the_job_page_gets_every_check_and_what_it_found(self):
        p = self.posting(self.db, self.company, "Project Manager", first=100, last=0, text="Join our talent pool. " + AD)
        p.ghost_checked_at = NOW
        out, checks = self.explain(self.job(self.db, self.ws, p))
        self.assertEqual([c["code"] for c in out["checks"]], [code for code, _ in ghost.CHECKS])  # all eight, in a fixed order
        self.assertEqual({code for code, c in checks.items() if c["state"] == "found"}, set(codes(out["signals"])))
        self.assertTrue(all(set(c) <= {"code", "label", "state", "note"} and c["label"] for c in out["checks"]))
        self.assertNotIn("note", checks["long_open"])  # a found sign has its own text and detail
        self.assertEqual([checks[c]["state"] for c in ("reposted", "many_similar", "recruiter_no_employer")], ["clear"] * 3)
        # What could not be checked says why: the percentage rests on less.
        self.assertEqual(checks["ad_signs"]["state"], "unknown")
        self.assertIn("when the job is scored", checks["ad_signs"]["note"])
        self.assertEqual(checks["company_news_cuts"]["state"], "unknown")
        self.assertIn("not been researched", checks["company_news_cuts"]["note"])

    def test_a_clean_job_says_what_was_looked_at(self):
        p = self.posting(self.db, self.company, "Delivery Lead", first=12, last=0, posted=days_ago(12).date().isoformat())
        p.ghost_checked_at = NOW
        p.ad_ghost_json = "[]"  # the AI read the ad and found nothing
        profile = CompanyProfile(key=self.company.lower(), name=self.company, status="done", news_researched_at=days_ago(5),
                                 controversies_json="[]")
        out, checks = self.explain(self.job(self.db, self.ws, p), {self.company.lower(): profile})
        self.assertEqual((out["level"], out["percent"], out["signals"]), (None, 5, []))
        self.assertEqual({c["state"] for c in out["checks"]}, {"clear"})
        self.assertEqual(checks["long_open"]["note"], "Listed for 12 days.")
        # A closed ad isn't "open for" anything, and one a search hasn't finished with has no history yet.
        closed = self.posting(self.db, self.company, "Delivery Lead Two", first=80, last=20, closed=20)
        _, checks = self.explain(self.job(self.db, self.ws, closed))
        self.assertEqual(checks["long_open"], {"code": "long_open", "label": checks["long_open"]["label"], "state": "clear",
                                               "note": "The ad is closed."})
        self.assertEqual((checks["reposted"]["state"], checks["many_similar"]["state"]), ("unknown", "unknown"))
        self.assertIn("next search", checks["reposted"]["note"])

    def test_a_job_that_did_not_come_from_a_search_has_no_history_to_check(self):
        job = self.job(self.db, self.ws, None, company=None, title="Analyst", detail_status="summary", description="A teaser.")
        out, checks = self.explain(job)
        self.assertEqual((out["level"], out["percent"]), (None, 5))
        for code in ("long_open", "reposted", "date_refreshed", "many_similar"):
            self.assertEqual(checks[code]["state"], "unknown", code)
            self.assertIn("did not come from a search", checks[code]["note"])
        self.assertEqual(checks["company_news_cuts"]["note"], "No employer is named.")
        self.assertEqual(checks["evergreen_wording"]["state"], "clear")
        self.assertIn("listing summary", checks["evergreen_wording"]["note"])  # read, but on little
        # An edit date is not a posted date: that check can't be made for such a source.
        p = self.posting(self.db, self.company, "Engineer", first=5, last=0, source="greenhouse:acme", posted=days_ago(1).isoformat())
        _, checks = self.explain(self.job(self.db, self.ws, p))
        self.assertEqual(checks["date_refreshed"]["state"], "unknown")
        self.assertIn("last edit", checks["date_refreshed"]["note"])

    def test_the_list_costs_no_query_per_job(self):
        postings = [self.posting(self.db, unique("Bulkco"), "Project Manager", first=2, last=0) for _ in range(40)]
        for p in postings:
            self.job(self.db, self.ws, p)
        for _ in range(10):
            self.job(self.db, self.ws, None, company=unique("Bulkco"), title="Analyst", description=AD, detail_status="full")
        self.db.commit()
        self.db.expire_all()
        jobs = pool.jobs_query(self.db, card=True).filter(Job.workspace_id == self.ws).all()
        self.assertEqual(len(jobs), 50)
        statements = []

        def count(_conn, _cursor, statement, *_a):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", count)
        try:
            ghost.assess_jobs(self.db, jobs, {}, NOW)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        self.assertEqual(len(statements), 1, statements)  # one batch for all fifty: no query per job


# ---------------------------------------------------------------------------
# No sealed column in SQL
# ---------------------------------------------------------------------------

PLAIN_JOB_COLUMNS = {"id", "workspace_id", "posting_id", "duplicate_of", "url_index", "first_seen", "origin", "source_id",
                     "posting_rev", "first_run_id"}


def sealed_clauses(statements: list[str]) -> list[str]:
    """Every use of a jobs column in a WHERE, ORDER BY, GROUP BY, DISTINCT or HAVING clause that isn't a plain one."""
    bad = []
    for sql in statements:
        sql = " ".join(sql.split())
        if re.search(r"\bDISTINCT\b", sql, re.I) and re.search(r"\bjobs\b", sql):
            bad.append(sql)
        for clause in re.findall(r"\b(?:WHERE|ORDER BY|GROUP BY|HAVING)\b(.*?)(?=\b(?:ORDER BY|GROUP BY|HAVING|LIMIT)\b|$)", sql, re.I):
            for col in re.findall(r"\bjobs\.(\w+)", clause):
                if col not in PLAIN_JOB_COLUMNS:
                    bad.append(f"{col}: {sql}")
    return bad


class NoSealedSqlTest(unittest.TestCase, Plant):
    def test_a_search_a_list_and_a_detail_page_never_filter_or_sort_on_sealed_columns(self):
        ws = self.workspace()
        db = SessionLocal()
        company = unique("Sqlco")
        self.posting(db, company, "Delivery Program Manager", first=100, last=70, closed=60)
        mine = self.posting(db, company, "Delivery Program Manager", first=100, last=0, text="Join our talent pool. " + AD + company)
        job = self.job(db, ws, mine)
        self.job(db, ws, None, company=company, title="Delivery Program Manager", description=AD, detail_status="full")
        db.commit()
        job_id = job.id
        db.close()
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_a):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", record)
        try:
            db = SessionLocal()
            ghost.refresh(db, datetime.utcnow())
            db.commit()
            db.close()
            with helpers.client() as c:
                listed = c.get("/api/jobs", headers={"X-Workspace": str(ws)})
                one = c.get(f"/api/jobs/{job_id}", headers={"X-Workspace": str(ws)})
        finally:
            event.remove(engine, "before_cursor_execute", record)
        self.assertEqual((listed.status_code, one.status_code), (200, 200))
        self.assertTrue(any("postings" in s for s in statements))
        self.assertEqual(sealed_clauses(statements), [])
        self.assertEqual(sealed_clauses(["SELECT jobs.id FROM jobs WHERE jobs.hidden = 1"])[0][:6], "hidden")  # (the check itself works)

    def test_the_module_names_no_sealed_column_in_a_query(self):
        source = Path(ghost.__file__).read_text(encoding="utf-8")
        used = set(re.findall(r"\bJob\.(\w+)", source))
        self.assertLessEqual(used, {"id", "_description", "ad_ghost_json"})  # read in a column query, filtered by id only
        for call in re.findall(r"\.(?:order_by|group_by|distinct)\(([^)]*)\)", source):
            self.assertNotIn("Job.", call)


class UpgradeTest(unittest.TestCase):
    def test_an_older_database_gets_the_columns_the_index_and_the_signs(self):
        out = helpers.scenario("""
            from datetime import datetime
            from sqlalchemy import text, inspect
            from backend import db as d, ghost
            d.init_db()
            old = ("listed_since", "repost_key", "ghost_json", "ghost_checked_at", "ad_ghost_json", "reopened_from")
            with d.engine.begin() as c:
                c.execute(text("INSERT INTO postings (url, company, title, location_text, description, detail_status, "
                               "first_seen, last_listed_at, rev, not_a_job) VALUES ('https://x.example/1', 'Oldco', "
                               "'Delivery Program Manager', 'Sydney NSW', 'Join our talent pool.', 'full', "
                               "'2026-09-01', '2026-10-01', 1, 0)"))
                c.execute(text("DROP INDEX ix_postings_repost_key"))
                for col in old:
                    c.execute(text("ALTER TABLE postings DROP COLUMN " + col))
                c.execute(text("ALTER TABLE jobs DROP COLUMN ad_ghost_json"))
            d.init_db()
            insp = inspect(d.engine)
            out["columns"] = sorted(set(old) & {c["name"] for c in insp.get_columns("postings")})
            out["job_column"] = "ad_ghost_json" in {c["name"] for c in insp.get_columns("jobs")}
            out["index"] = "ix_postings_repost_key" in {i["name"] for i in insp.get_indexes("postings")}
            db = d.SessionLocal()
            out["done"] = ghost.refresh(db, datetime(2026, 10, 8))
            db.commit()
            row = db.query(d.Posting).one()
            out["key"] = row.repost_key
            out["text"] = [s["code"] for s in __import__("json").loads(row.ghost_json)["text"]]
            out["checked"] = row.ghost_checked_at is not None
        """)
        self.assertEqual(out["columns"], sorted(["listed_since", "repost_key", "ghost_json", "ghost_checked_at", "ad_ghost_json", "reopened_from"]))
        self.assertTrue(out["job_column"])
        self.assertTrue(out["index"])
        self.assertEqual((out["done"], out["key"], out["text"], out["checked"]),
                         (1, "oldco|delivery manager program", ["evergreen_wording"], True))


# ---------------------------------------------------------------------------
# The scorer
# ---------------------------------------------------------------------------

SAYS = "We are building a talent pool for future roles"  # wording in the ads the scorer tests read
SAYS_AD = AD + SAYS + ". "


def llm_answer(**extra) -> str:
    return json.dumps({"score": 70, "requirements": [], "gaps": [], "summary": "ok", "office_location": "",
                       "employment_type": "", **extra})


class ScorerTest(unittest.TestCase, Plant):
    def setUp(self):
        self.ws, self.ws2 = self.workspace(), self.workspace()
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        self.company = unique("Scoreco")

    def score(self, job_id: int, answer: str):
        with mock.patch.object(scorer, "call_model", return_value=answer):
            return scorer.score_one(job_id, "My resume", "hash", {"model": "m"})

    def fresh(self, job_id: int) -> Job:
        self.db.expire_all()
        return self.db.get(Job, job_id)

    def test_the_prompt_asks_for_it_once_and_briefly(self):
        self.assertIn('"ghost_signs"', scorer.SYSTEM_PROMPT)
        self.assertEqual(scorer.SYSTEM_PROMPT.count("ghost_signs"), 2)  # the shape and one rule
        self.assertIn("Only when the full description is given", scorer.SYSTEM_PROMPT)
        rule = scorer.SYSTEM_PROMPT[scorer.SYSTEM_PROMPT.index('12. "ghost_signs"'):]
        self.assertIn("word for word", rule)  # quotes from the ad, not the model's opinion
        self.assertIn("It never changes the score", rule)
        self.assertIn("A missing team, start date or detail is not a sign", rule)
        self.assertLess(scorer.SYSTEM_PROMPT.index("10. The CANDIDATE"), scorer.SYSTEM_PROMPT.index('12. "ghost_signs"'))

    def test_parsing(self):
        self.assertEqual(scorer.parse_result(llm_answer(ghost_signs=[" No specific role ", "no specific role", 3]))["ghost_signs"],
                         ["No specific role"])
        self.assertEqual(scorer.parse_result(llm_answer(ghost_signs=[]))["ghost_signs"], [])
        self.assertEqual(scorer.parse_result(llm_answer(ghost_signs="nonsense"))["ghost_signs"], [])
        self.assertIsNone(scorer.parse_result(json.dumps({"score": 5}))["ghost_signs"])  # not answered: unknown

    def test_a_public_ad_shares_it_with_everyone(self):
        p = self.posting(self.db, self.company, "Project Manager", first=2, last=0, text=SAYS_AD)
        mine, theirs = self.job(self.db, self.ws, p), self.job(self.db, self.ws2, p)
        self.db.commit()
        result = self.score(mine.id, llm_answer(ghost_signs=[SAYS]))
        self.assertNotIn("ghost_signs", result)  # (like the other facts, it isn't part of the stored fit)
        self.assertNotIn("ghost_signs", self.db.query(FitResult).filter(FitResult.job_id == mine.id).one().evidence_json)
        self.db.expire_all()
        self.assertEqual(json.loads(self.db.get(Posting, p.id).ad_ghost_json), [SAYS])
        self.assertIsNone(self.fresh(mine.id).ad_ghost_json)
        for job in (mine, theirs):  # the other workspace reads it too
            out = ghost.assess_jobs(self.db, [self.fresh(job.id)], {}, NOW)[job.id]
            self.assertIn("ad_signs", codes(out["signals"]))

    def test_only_the_ads_own_words_are_shared(self):
        # The model saw the candidate's CV with the ad: a phrase from it, or its own accusation, must not reach the pool.
        p = self.posting(self.db, self.company, "Project Manager", first=2, last=0, text=SAYS_AD)
        mine, theirs = self.job(self.db, self.ws, p), self.job(self.db, self.ws2, p)
        self.db.commit()
        self.score(mine.id, llm_answer(ghost_signs=["Jane Doe, jane@example.com, Senior Engineer at Initech", "this ad is a scam",
                                                    "fake company", "ghost job"]))
        self.db.expire_all()
        stored = self.db.get(Posting, p.id).ad_ghost_json
        self.assertEqual(json.loads(stored), [])  # (read, nothing found)
        for text in ("Jane", "Initech", "scam", "fake", "ghost"):
            self.assertNotIn(text, stored)
        for job in (mine, theirs):
            self.assertNotIn("ad_signs", codes(ghost.assess_jobs(self.db, [self.fresh(job.id)], {}, NOW)[job.id]["signals"]))
        # a real quote next to a made-up one keeps only the quote
        self.score(mine.id, llm_answer(ghost_signs=["Initech payroll migration lead", "a talent pool for future roles"]))
        self.db.expire_all()
        self.assertEqual(json.loads(self.db.get(Posting, p.id).ad_ghost_json), ["a talent pool for future roles"])

    def test_the_ai_reading_does_not_move_the_fit_score(self):
        p = self.posting(self.db, self.company, "Project Manager", first=2, last=0, text=SAYS_AD)
        job = self.job(self.db, self.ws, p)
        self.db.commit()
        plain = self.score(job.id, llm_answer(ghost_signs=[]))["score"]
        flagged = self.score(job.id, llm_answer(ghost_signs=[SAYS]))["score"]
        self.assertEqual(plain, flagged)

    def test_a_privately_uploaded_ad_keeps_it_private(self):
        p = self.posting(self.db, self.company, "Project Manager", first=2, last=0)
        mine = self.job(self.db, self.ws, p, description=SAYS_AD + "private", detail_status="full")
        theirs = self.job(self.db, self.ws2, p)
        self.db.commit()
        self.score(mine.id, llm_answer(ghost_signs=["a talent pool for future roles"]))
        self.db.expire_all()
        self.assertIsNone(self.db.get(Posting, p.id).ad_ghost_json)  # the pool's is untouched
        self.assertEqual(json.loads(self.fresh(mine.id).ad_ghost_json), ["a talent pool for future roles"])
        self.assertNotIn("ad_signs", codes(ghost.assess_jobs(self.db, [self.fresh(theirs.id)], {}, NOW)[theirs.id]["signals"]))
        self.assertIn("ad_signs", codes(ghost.assess_jobs(self.db, [self.fresh(mine.id)], {}, NOW)[mine.id]["signals"]))
        raw = self.db.connection().exec_driver_sql("SELECT ad_ghost_json FROM jobs WHERE id = ?", (mine.id,)).scalar()
        self.assertIsInstance(raw, bytes)  # sealed in the database
        self.assertNotIn(b"talent pool", raw)

    def test_a_private_job_keeps_it_on_the_job(self):
        job = self.job(self.db, self.ws, None, company=self.company, title="Analyst", description=SAYS_AD, detail_status="full")
        self.db.commit()
        self.score(job.id, llm_answer(ghost_signs=["building a talent pool"]))
        self.assertEqual(json.loads(self.fresh(job.id).ad_ghost_json), ["building a talent pool"])

    def test_a_listing_summary_is_too_little_to_judge(self):
        p = self.posting(self.db, self.company, "Project Manager", first=2, last=0, text="Short teaser.")
        p.detail_status = "summary"
        job = self.job(self.db, self.ws, p)
        self.db.commit()
        self.score(job.id, llm_answer(ghost_signs=["no specific role"]))
        self.db.expire_all()
        self.assertIsNone(self.db.get(Posting, p.id).ad_ghost_json)

    def test_an_answer_without_it_changes_nothing_and_an_empty_one_clears_it(self):
        p = self.posting(self.db, self.company, "Project Manager", first=2, last=0, text=SAYS_AD)
        job = self.job(self.db, self.ws, p)
        self.db.commit()
        self.score(job.id, llm_answer(ghost_signs=["building a talent pool"]))
        self.score(job.id, json.dumps({"score": 60, "requirements": [], "gaps": [], "summary": "x"}))
        self.db.expire_all()
        self.assertEqual(json.loads(self.db.get(Posting, p.id).ad_ghost_json), ["building a talent pool"])
        self.score(job.id, llm_answer(ghost_signs=[]))
        self.db.expire_all()
        self.assertEqual(self.db.get(Posting, p.id).ad_ghost_json, "[]")
        self.assertNotIn("ad_signs", codes(ghost.assess_jobs(self.db, [self.fresh(job.id)], {}, NOW)[job.id]["signals"]))


# ---------------------------------------------------------------------------
# The job JSON
# ---------------------------------------------------------------------------

class JobJsonTest(unittest.TestCase, Plant):
    def setUp(self):
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        self.ws = self.workspace()
        self.h = {"X-Workspace": str(self.ws)}
        self.client = helpers.client()
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def listed(self) -> dict:
        r = self.client.get("/api/jobs", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return {j["id"]: j for j in r.json()}

    def test_every_job_has_a_ghost_object(self):
        now = datetime.utcnow()
        plain = self.posting(self.db, unique("Jsonco"), "Quiet Role Lead", first=1, last=0)
        plain.first_seen = plain.listed_since = plain.last_listed_at = now
        evergreen = self.posting(self.db, unique("Jsonco"), "Project Manager", text="Join our talent pool. " + AD)
        evergreen.first_seen = evergreen.listed_since = evergreen.last_listed_at = now
        stale = self.posting(self.db, unique("Jsonco"), "Finance Manager")
        stale.first_seen = stale.listed_since = now - timedelta(days=75)
        stale.last_listed_at = now
        ids = {n: self.job(self.db, self.ws, p).id for n, p in (("plain", plain), ("evergreen", evergreen), ("stale", stale))}
        private = self.job(self.db, self.ws, None, company=unique("Jsonco"), title="Analyst", description=AD, detail_status="full").id
        self.db.commit()
        jobs = self.listed()
        self.assertEqual(jobs[ids["plain"]]["ghost"], {"level": None, "percent": 5, "signals": []})
        self.assertEqual(jobs[private]["ghost"], {"level": None, "percent": 5, "signals": []})
        self.assertEqual(jobs[ids["evergreen"]]["ghost"],
                         {"level": "likely", "percent": 70, "signals": [{"code": "evergreen_wording", "text": 'Mentions "talent pool"'}]})
        self.assertEqual(jobs[ids["stale"]]["ghost"],
                         {"level": "possible", "percent": 35, "signals": [{"code": "long_open", "text": "Open for 75 days"}]})
        for job in jobs.values():
            self.assertEqual(set(job["ghost"]), {"level", "percent", "signals"})
            self.assertTrue(all(set(s) == {"code", "text"} for s in job["ghost"]["signals"]))  # the list has no detail
        # The job page adds a sentence of detail and the strength to each reason, and every check that was made.
        detail = self.client.get(f"/api/jobs/{ids['evergreen']}", headers=self.h).json()["ghost"]
        self.assertEqual((detail["level"], detail["percent"]), ("likely", 70))
        self.assertEqual(set(detail["signals"][0]), {"code", "text", "detail", "strength"})
        self.assertIn("future roles", detail["signals"][0]["detail"])
        self.assertEqual(detail["signals"][0]["strength"], "strong")
        self.assertEqual([c["code"] for c in detail["checks"]], [code for code, _ in ghost.CHECKS])
        self.assertEqual([c["code"] for c in detail["checks"] if c["state"] == "found"], ["evergreen_wording"])
        # ... also for a job with nothing found (the page says what was looked at), and one that is not from a search.
        for job_id in (ids["plain"], private):
            clean = self.client.get(f"/api/jobs/{job_id}", headers=self.h).json()["ghost"]
            self.assertEqual((clean["level"], clean["percent"], clean["signals"]), (None, 5, []))
            self.assertEqual(len(clean["checks"]), len(ghost.CHECKS))
            self.assertNotIn("found", {c["state"] for c in clean["checks"]})

    def test_nothing_is_hidden_or_moved_by_a_flag(self):
        p = self.posting(self.db, unique("Jsonco"), "Project Manager", first=1, text="Join our talent pool. " + AD)
        job = self.job(self.db, self.ws, p)
        self.db.commit()
        listed = self.listed()[job.id]
        self.assertEqual((listed["ghost"]["level"], listed["status"], listed["hidden"], listed["excluded_reason"], listed["fit"]),
                         ("likely", "to_review", False, None, None))

    def test_merged_copies_are_not_listed_and_follow_their_main_job(self):
        p = self.posting(self.db, unique("Jsonco"), "Project Manager", first=1, text="Join our talent pool. " + AD)
        main = self.job(self.db, self.ws, p)
        copy_posting = self.posting(self.db, unique("Jsonco"), "Project Manager", first=1, text="Join our talent pool. " + AD)
        copy = self.job(self.db, self.ws, copy_posting, duplicate_of=main.id)
        self.db.commit()
        jobs = self.listed()
        self.assertIn(main.id, jobs)
        self.assertNotIn(copy.id, jobs)
        self.assertEqual([a["id"] for a in jobs[main.id]["also_on"]], [copy.id])
        self.assertNotIn("ghost", jobs[main.id]["also_on"][0])

    def test_the_whole_search_path_stores_the_signs(self):
        company, title = unique("Pathco"), "Delivery Program Lead"
        db = self.db
        for first, last, closed in ((95, 80, 30), (50, 40, 10)):  # two earlier runs of the role, both over
            self.posting(db, company, title, first=first, last=last, closed=closed, source="greenhouse:pathco")
        db.commit()
        listing = [Scraped(url=f"https://ads.example/{unique('s')}", title=title, company=company, location_text="Sydney NSW",
                           description=AD, detail_status="full")]

        class Feed(Adapter):
            source, method, complete_listing, has_details = "greenhouse:pathco", "test", False, False

            def list_postings(self):
                return [copy.copy(p) for p in listing]

        from backend.pipeline import run_search
        ws = self.ws
        with mock.patch("backend.pipeline.geocode", return_value=None), mock.patch("backend.offices.geocode", return_value=None), \
                mock.patch("backend.pipeline.repair_geocodes"), \
                mock.patch("backend.pipeline.build_adapters", return_value=[(None, Feed())]):
            self.client.put("/api/profile", headers=self.h, json={"titles": [title], "seek_enabled": False})
            run_search(ws)
        (job,) = [j for j in self.listed().values() if j["company"] == company]
        self.assertEqual(job["ghost"]["level"], "likely")
        self.assertEqual([s["code"] for s in job["ghost"]["signals"]], ["reposted"])
        self.assertRegex(job["ghost"]["signals"][0]["text"], r"^Posted 3 times since \d+ \w+$")


# ---------------------------------------------------------------------------
# Digest and export
# ---------------------------------------------------------------------------

class DigestCardTest(unittest.TestCase):
    JOB = {"id": 9, "title": "Project Manager", "company": "Acme", "url": "https://ads.example/9"}

    def test_a_flagged_job_gets_one_line(self):
        card = email_templates.digest_card({**self.JOB, "ghost_reason": "Open for 74 days"}, "https://jobs.example.com")
        self.assertEqual(card["ghost"], "Possible ghost job: Open for 74 days")
        self.assertIsNone(email_templates.digest_card(self.JOB, "https://jobs.example.com")["ghost"])
        self.assertIsNone(email_templates.digest_card({**self.JOB, "ghost_reason": "  "}, "https://jobs.example.com")["ghost"])

    def test_the_line_is_in_both_parts_and_escaped(self):
        from backend.keys import ServerKeys
        import tempfile

        keys = ServerKeys.create(os.path.join(tempfile.mkdtemp(), "server-keys.json"))
        jobs = [{**self.JOB, "ghost_reason": 'Mentions "talent <b>pool</b>"'}, {**self.JOB, "id": 10, "title": "Other"}]
        _, text, html, _ = email_templates.job_digest(jobs, keys=keys, unsubscribe_id="u1", base_url="https://jobs.example.com")
        self.assertEqual(text.count("Possible ghost job:"), 1)
        self.assertEqual(html.count("Possible ghost job:"), 1)
        self.assertIn('Possible ghost job: Mentions "talent <b>pool</b>"', text)
        self.assertIn("Possible ghost job: Mentions &quot;talent &lt;b&gt;pool&lt;/b&gt;&quot;", html)
        self.assertNotIn("<b>pool", html)


class DigestFlowTest(unittest.TestCase, Plant):
    def test_cards_carry_the_first_reason_of_a_flagged_job_only(self):
        acct = helpers.make_user()
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                flagged = self.posting(db, unique("Cardco"), "Project Manager", first=0, text="Join our talent pool. " + AD)
                plain = self.posting(db, unique("Cardco"), "Quiet Analyst", first=0)
                jobs = [self.job(db, acct.workspace_id, p) for p in (flagged, plain)]
                db.commit()
                cards, more = digest.cards(db, jobs)
            finally:
                db.close()
        reasons = {c["id"]: c["ghost_reason"] for c in cards}
        self.assertEqual(reasons[jobs[0].id], 'Mentions "talent pool"')
        self.assertIsNone(reasons[jobs[1].id])
        self.assertEqual(more, 0)


class ExportTest(unittest.TestCase, Plant):
    def test_a_flagged_job_has_a_section_and_a_column(self):
        acct = helpers.make_user()
        with helpers.as_account(acct):
            db = SessionLocal()
            try:
                flagged = self.job(db, acct.workspace_id, self.posting(db, unique("Expco"), "Project Manager", first=100,
                                                                      text="Join our talent pool. " + AD))
                quiet = self.job(db, acct.workspace_id, self.posting(db, unique("Expco"), "Quiet Analyst", first=1))
                db.commit()
                flagged_id, quiet_id = flagged.id, quiet.id
            finally:
                db.close()
        with helpers.client(acct.email) as c:
            r = c.post("/api/account/export")
        self.assertEqual(r.status_code, 200)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        texts = {n: z.read(n).decode() for n in z.namelist() if n.endswith(".md")}
        files = {n: t for n, t in texts.items() if "/Jobs/To review/" in n}
        self.assertEqual(len(files), 2)
        (with_section,) = [t for t in files.values() if "Project Manager" in t.splitlines()[0]]
        (without,) = [t for t in files.values() if "Quiet Analyst" in t.splitlines()[0]]
        self.assertIn("## Is this a real vacancy?", with_section)
        self.assertIn("Several signs suggest this ad may not be a current vacancy:", with_section)
        self.assertIn('- Mentions "talent pool"', with_section)
        self.assertIn("Open for", with_section)
        self.assertIn("the job may well be real", with_section)
        self.assertLess(with_section.index("## Is this a real vacancy?"), with_section.index("## Description"))
        self.assertNotIn("real vacancy", without)
        (index,) = [t for n, t in texts.items() if n.endswith("/Jobs/Jobs.md")]
        self.assertIn("| Job | Company | Status | Fit | Closed | Ghost job? |", index)
        lines = [ln for ln in index.splitlines() if ln.startswith("| [")]
        self.assertEqual(sorted(ln.rstrip().split("|")[-2].strip() for ln in lines), ["", "Likely"])
        self.assertTrue(flagged_id and quiet_id)


if __name__ == "__main__":
    unittest.main()
