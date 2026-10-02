"""Unit tests for parsing, filtering and scoring logic (no network).

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["JOBHUNT_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["JOBHUNT_SCHEDULER"] = "off"  # tests drive backend.schedule.tick() themselves

from backend.adapters.ats import detect_ats  # noqa: E402
from backend.adapters.base import Posting  # noqa: E402
from backend.adapters.generic import is_job_link, parse_detail, parse_listing  # noqa: E402
from backend.adapters.seek import build_search_url, parse_job_page, parse_search_page  # noqa: E402
from backend.db import init_db  # noqa: E402
from backend.filters import Criteria, exclusion_reason, parse_salary  # noqa: E402
from backend.geo import classify_work_mode, guess_country  # noqa: E402
from backend.scorer import ScorerError, parse_result  # noqa: E402
from backend.scraping.robots import RobotsRules  # noqa: E402

init_db()

SEEK_ROBOTS = """
User-agent: *
Disallow: */job/
Disallow: *?
Disallow: /graphql
Allow: *?advertiserid
Allow: *?keywords
User-agent: GPTBot
Disallow: /companies
"""


class RobotsTest(unittest.TestCase):
    def test_seek_rules(self):
        r = RobotsRules(SEEK_ROBOTS, "jobhuntpa")
        url = build_search_url("Product Manager", "All Sydney NSW")
        self.assertEqual(url, "https://au.seek.com/Product-Manager-jobs/in-All-Sydney-NSW")
        self.assertTrue(r.allowed(url))
        self.assertFalse(r.allowed(url + "?page=2"))
        self.assertFalse(r.allowed("https://www.seek.com.au/job/12345"))
        self.assertFalse(r.allowed("https://www.seek.com.au/jobs?where=Sydney&keywords=pm"))
        self.assertTrue(r.allowed("https://www.seek.com.au/Product-Manager-jobs"))

    def test_crawl_delay_and_specific_agent(self):
        r = RobotsRules("User-agent: *\nCrawl-delay: 10\nDisallow: /search/\n", "jobhuntpa")
        self.assertEqual(r.crawl_delay, 10)
        self.assertFalse(r.allowed("https://x.com/search/foo"))
        r = RobotsRules("User-agent: JobHuntPA\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "jobhuntpa")
        self.assertFalse(r.allowed("https://x.com/careers"))

    def test_disallow_all_and_empty(self):
        self.assertFalse(RobotsRules("User-agent: *\nDisallow: /\n", "jobhuntpa").allowed("https://a.com/v1/x"))
        self.assertTrue(RobotsRules("User-agent: *\nDisallow:\n", "jobhuntpa").allowed("https://a.com/x"))
        self.assertTrue(RobotsRules("", "jobhuntpa").allowed("https://a.com/x"))


def _seek_html(payload: dict) -> str:
    return f"<html><script>\n    window.SEEK_REDUX_DATA = {json.dumps(payload)};\n</script></html>"


class SeekTest(unittest.TestCase):
    def test_search_page(self):
        html = _seek_html({"results": {"totalPages": 3, "results": {"jobs": [{
            "id": "94849505", "title": "Senior Product Manager", "companyName": "Acme",
            "locations": [{"countryCode": "AU", "label": "Sydney NSW"}],
            "salaryLabel": "$150,000 – $170,000 per year", "teaser": "Lead the roadmap.",
            "bulletPoints": ["Hybrid", "Great team"], "workTypes": ["Full time"],
            "workArrangements": {"data": [{"id": "2", "label": {"text": "Hybrid"}}]},
            "classifications": [{"classification": {"description": "ICT"}, "subclassification": {"description": "Product Management"}}],
            "listingDate": "2026-09-24T03:36:05.000Z",
        }]}}})
        postings, pages = parse_search_page(html)
        self.assertEqual(pages, 3)
        p = postings[0]
        self.assertEqual(p.url, "https://www.seek.com.au/job/94849505")
        self.assertEqual((p.company, p.location_text, p.country, p.work_mode), ("Acme", "Sydney NSW", "AU", "hybrid"))
        self.assertEqual(p.detail_status, "summary")
        self.assertIn("Lead the roadmap.", p.description)
        self.assertIn("- Great team", p.description)

    def test_job_page(self):
        html = _seek_html({"jobdetails": {"result": {
            "workArrangements": {"arrangements": [{"type": "HYBRID", "label": "Hybrid"}]},
            "job": {"id": "1", "title": "PM", "content": "<p>About the role</p><ul><li>Own delivery</li></ul>",
                    "location": {"label": "North Ryde, Sydney NSW"}, "advertiser": {"name": "PRA"},
                    "salary": {"label": "$88 per hour"}, "listedAt": {"dateTimeUtc": "2026-09-24"},
                    "classifications": [{"label": "Developers"}]}}}})
        p = parse_job_page(html)
        self.assertEqual((p.title, p.company, p.work_mode, p.detail_status), ("PM", "PRA", "hybrid", "full"))
        self.assertIn("- Own delivery", p.description)


CANVA_LISTING = """
<div class="grid job-listing">
  <div class="card card-job"><div class="card-body">
    <h2 class="card-title"><a href="/en/jobs/6000000001435611/strategic-customer-success-manager/">Strategic Customer Success Manager</a></h2>
    <button>Save</button>
    <ul class="job-meta"><li>Austin, TX, United States</li><li>Sales &amp; Success</li></ul>
  </div></div>
  <div class="card card-job"><div class="card-body">
    <h2 class="card-title"><a href="/en/jobs/6000000001431656/product-manager-education/">Product Manager, Education</a></h2>
    <ul class="job-meta"><li>Sydney, Australia</li><li>Product</li></ul>
  </div></div>
</div>
<a href="/en/jobs/saved-jobs/">Your jobs</a>
<a href="/en/careers-and-growth/">Careers and growth</a>
<a href="/en/jobs/?page=2#results">2</a>
"""


class GenericTest(unittest.TestCase):
    def test_job_links(self):
        idx = "https://www.lifeatcanva.com/en/jobs/"
        self.assertTrue(is_job_link("https://www.lifeatcanva.com/en/jobs/6000000001435611/strategic-csm/", idx))
        self.assertFalse(is_job_link("https://www.lifeatcanva.com/en/jobs/saved-jobs/", idx))
        self.assertFalse(is_job_link("https://www.lifeatcanva.com/en/careers-and-growth/", idx))
        self.assertFalse(is_job_link("https://www.lifeatcanva.com/en/early-careers/", idx))
        self.assertFalse(is_job_link(idx, idx))
        jan = "https://www.janison.com/about/careers/"
        self.assertTrue(is_job_link("https://www.janison.com/careers/implementation-manager-enterprise-saas/", jan))
        self.assertFalse(is_job_link("https://www.janison.com/about/careers/", jan))

    def test_listing_cards_and_pagination(self):
        postings, pages = parse_listing(CANVA_LISTING, "https://www.lifeatcanva.com/en/jobs/", "Canva")
        self.assertEqual([p.title for p in postings], ["Strategic Customer Success Manager", "Product Manager, Education"])
        self.assertEqual(postings[0].location_text, "Austin, TX, United States")
        self.assertEqual(postings[0].country, "US")
        self.assertEqual(postings[1].country, "AU")
        self.assertEqual(pages, ["https://www.lifeatcanva.com/en/jobs/?page=2"])

    def test_title_first_line_of_link(self):
        html = ('<ul><li><a href="/jobs/8333730-executive-assistant"><span>Executive Assistant</span>'
                '<div><span>People &amp; Operations</span> · <span>Remote</span></div></a></li>'
                '<li><a href="/jobs/8341125-it-manager"><span>IT Manager</span><div>London, GB</div></a></li></ul>')
        postings, _ = parse_listing(html, "https://careers.tryhackme.com/jobs", "TryHackMe")
        self.assertEqual([p.title for p in postings], ["Executive Assistant", "IT Manager"])
        self.assertEqual(postings[1].location_text, "London, GB")

    def test_detail_main_content(self):
        html = """<html><body><nav>Home Products Careers</nav>
        <div class="content-wrapper"><div class="job-info"><div class="single-job-info">Full Time</div>
        <div class="single-job-info">Sydney, NSW</div></div>
        <h1>Implementation Manager, Enterprise SaaS</h1>
        <h2>About the role</h2><p>""" + ("You will lead complex implementations for enterprise customers. " * 12) + """</p>
        <h2>About you</h2><ul><li>5+ years experience in SaaS delivery</li></ul></div>
        <footer>Privacy Terms</footer></body></html>"""
        p = parse_detail(html, Posting(url="https://www.janison.com/careers/im/", title="x"))
        self.assertIsNotNone(p)
        self.assertEqual(p.title, "Implementation Manager, Enterprise SaaS")
        self.assertEqual(p.location_text, "Sydney, NSW")
        self.assertNotIn("Privacy Terms", p.description)
        self.assertNotIn("Home Products", p.description)
        self.assertIn("- 5+ years experience", p.description)

    def test_detail_rejects_landing_pages(self):
        html = "<html><body><h1>Design your future</h1><p>Join our early careers programme.</p></body></html>"
        self.assertIsNone(parse_detail(html, Posting(url="https://x.com/careers/early/", title="x")))

    def test_detail_jsonld(self):
        ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Program Manager",
              "description": "<p>" + "Own the program roadmap and stakeholder reporting. " * 8 + "</p>",
              "hiringOrganization": {"name": "Acme"}, "jobLocationType": "TELECOMMUTE",
              "jobLocation": {"@type": "Place", "address": {"addressLocality": "Melbourne", "addressRegion": "VIC", "addressCountry": "AU"}}}
        html = f'<script type="application/ld+json">{json.dumps(ld)}</script><h1>ignored</h1>'
        p = parse_detail(html, Posting(url="https://acme.com/careers/pm-1", title="stub"))
        self.assertEqual((p.title, p.country, p.work_mode, p.detail_status), ("Program Manager", "AU", "remote", "full"))

    def test_aggregator_not_hijacked_by_one_ats_link(self):
        from unittest.mock import MagicMock
        from backend.adapters.generic import GenericAdapter
        from backend.scraping.http import Response
        html = ('<a href="https://jobs.lever.co/binance">Featured</a>'
                + "".join(f'<div><a href="/jobs/{i}-product-manager">Product Manager {i}</a><p>Sydney NSW</p></div>' for i in range(5)))
        client = MagicMock()
        client.get.return_value = Response(url="https://board.example/jobs/pm", status=200, text=html)
        adapter = GenericAdapter(client, "https://board.example/jobs/pm", company="Board")
        postings = adapter.list_postings()
        self.assertIsNone(adapter.delegate)
        self.assertEqual(len(postings), 5)

    def test_job_board_keeps_postings_not_categories(self):
        cards = "".join(
            f'<div><a href="/company/co{i}/jobs/pm-{i}/">Product Manager {i}</a>'
            f'<a href="/company/co{i}/">Company {i}</a><p>Sydney, Australia</p>'
            f'<a href="/jobs/full-time/">Full Time</a><a href="/jobs/product-manager/">product manager jobs</a></div>'
            for i in range(8))
        postings, _ = parse_listing(cards, "https://board.example/country/australia/jobs/product-manager/", "Board")
        self.assertEqual(len(postings), 8)
        self.assertEqual(postings[0].company, "Company 0")
        self.assertTrue(all("/company/" in p.url for p in postings))

    def test_topic_pages_dropped_when_postings_have_ids(self):
        links = "".join(f'<div><a href="/jobs/d04765c9-e0c4-4e92-ae2e-32d587f1f75{i}-product-manager-{i}">Product Manager {i}</a></div>' for i in range(4))
        links += '<a href="/jobs/edtech-product">Edtech Product</a><a href="/jobs/learning-design">Learning Design</a>'
        postings, _ = parse_listing(links, "https://edtechjobs.io/jobs/product-management", "Board")
        self.assertEqual(len(postings), 4)
        self.assertTrue(all("d04765c9" in p.url for p in postings))

    def test_detail_rejects_topic_listing_page(self):
        body = "".join(f'<li><a href="/jobs/{i}0000-pm-role">PM role {i}</a> Experience with requirements and stakeholders.</li>' for i in range(6))
        html = f"<html><body><h1>Edtech Product EdTech Jobs</h1><ul>{body}</ul><p>{'You will find responsibilities here. ' * 20}</p></body></html>"
        self.assertIsNone(parse_detail(html, Posting(url="https://edtechjobs.io/jobs/edtech-product", title="x")))
        html2 = html.replace("Edtech Product EdTech Jobs", "Edtech Product")
        self.assertIsNone(parse_detail(html2, Posting(url="https://edtechjobs.io/jobs/edtech-product", title="x")))

    def test_detect_ats(self):
        html = '<a href="https://apply.workable.com/learnosity/">Jobs</a> <iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>'
        self.assertEqual(detect_ats(html), [("workable", ("learnosity",)), ("greenhouse", ("acme",))])
        self.assertEqual(detect_ats("https://acme.wd3.myworkdayjobs.com/en-US/External"),
                         [("workday", ("acme.wd3.myworkdayjobs.com", "acme", "External"))])
        self.assertEqual(detect_ats("https://apply.workable.com/api/v3/accounts/x/jobs"), [])


PINS = [
    {"kind": "home", "lat": -33.4246, "lng": 151.3401, "radius_km": 20},
    {"kind": "hybrid", "lat": -33.8707, "lng": 151.2079, "radius_km": 1},  # Town Hall
]


def _job(**kw):
    base = {"title": "Product Manager", "description": "Build SaaS products.", "company": "Acme", "industry": "",
            "work_mode": "hybrid", "country": "AU", "location_text": "Sydney NSW", "lat": -33.8688, "lng": 151.2093,
            "salary_text": "", "salary_min": None, "salary_max": None}
    base.update(kw)
    return base


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.c = Criteria(titles=["Product Manager", "Program Manager"], keywords_include=["SaaS"],
                          salary_floor=140000, dealbreaker_industries=["war", "gambling"], pins=PINS)

    def test_word_boundaries(self):
        self.assertIsNone(exclusion_reason(_job(title="Software Product Manager"), self.c))
        self.assertIn("gambling", exclusion_reason(_job(company="Gambling Co"), self.c))

    def test_include_title_or_keyword(self):
        self.assertIsNone(exclusion_reason(_job(title="Delivery Lead", description="Our SaaS platform"), self.c))
        self.assertIsNotNone(exclusion_reason(_job(title="Delivery Lead", description="Banking"), self.c))
        # Listing stage without a description: keyword might still appear later.
        self.assertIsNone(exclusion_reason(_job(title="Delivery Lead", description=""), self.c, stage="listing"))

    def test_salary(self):
        self.assertIn("salary", exclusion_reason(_job(salary_text="$90 - $100k"), self.c))
        self.assertIsNone(exclusion_reason(_job(salary_text="$800-1000 per day"), self.c))
        self.assertEqual(parse_salary("$88 per hour plus super"), (88 * 2080, 88 * 2080))
        self.assertIsNone(exclusion_reason(_job(salary_text="Competitive"), self.c))

    def test_location(self):
        self.assertIsNone(exclusion_reason(_job(), self.c))  # hybrid near Town Hall pin
        far = _job(location_text="Parramatta NSW", lat=-33.8150, lng=151.0011)
        self.assertIn("outside your pin radii", exclusion_reason(far, self.c))
        self.assertIn("outside Australia", exclusion_reason(_job(country="US", location_text="Austin, TX"), self.c))
        self.assertIsNone(exclusion_reason(_job(work_mode="remote", lat=None, lng=None), self.c))
        self.assertIn("remote-global", exclusion_reason(_job(work_mode="remote", country="US"), self.c))
        onsite_cbd = _job(work_mode="onsite")
        self.assertIn("outside your pin radii", exclusion_reason(onsite_cbd, self.c))
        self.assertIsNone(exclusion_reason(_job(lat=None, lng=None, country=""), self.c))
        # A planet-sized pin must not switch the commute check off.
        self.c.pins = PINS + [{"kind": "home", "lat": -33.39, "lng": 151.35, "radius_km": 999999999}]
        self.assertIn("outside your pin radii", exclusion_reason(far, self.c))


class GeoTest(unittest.TestCase):
    def test_country(self):
        self.assertEqual(guess_country("North Ryde, Sydney NSW"), "AU")
        self.assertEqual(guess_country("Berlin, , Germany"), "DE")
        self.assertEqual(guess_country("Austin, TX"), "US")
        self.assertEqual(guess_country("Hawthorn, Victoria, Australia"), "AU")
        self.assertEqual(guess_country(""), "")

    def test_geocode_query_cleanup(self):
        from backend.geo import _geocode_query
        self.assertEqual(_geocode_query("Melbourne, Australia, AU"), "Melbourne, Australia")
        self.assertEqual(_geocode_query("Sydney, , Australia"), "Sydney, Australia")
        self.assertEqual(_geocode_query("Perth, WA"), "Perth, WA")
        self.assertEqual(_geocode_query("Sydney NSW; Melbourne VIC"), "Sydney NSW")
        self.assertEqual(_geocode_query("\U0001F1E6\U0001F1FA Australia \u2013 Remote"), "Australia")

    def test_geocode_prefers_most_important_place(self):
        from unittest import mock
        import backend.geo as geo
        hits = [{"lat": "-20.378", "lon": "115.55", "importance": 0.2, "display_name": "Melbourne Point, WA",
                 "address": {"country_code": "au"}},
                {"lat": "-37.814", "lon": "144.963", "importance": 0.8, "display_name": "Melbourne, Victoria",
                 "address": {"country_code": "au"}}]
        resp = mock.Mock(json=lambda: hits, raise_for_status=lambda: None)
        with mock.patch.object(geo.httpx, "get", return_value=resp), mock.patch.object(geo.time, "sleep"):
            self.assertEqual(geo._nominatim("Melbourne, Australia")["lat"], -37.814)

    def test_work_mode(self):
        self.assertEqual(classify_work_mode("PM", "Sydney", "We offer hybrid working, 3 days in office."), "hybrid")
        self.assertEqual(classify_work_mode("PM (Remote)", "", ""), "remote")
        self.assertEqual(classify_work_mode("PM", "Sydney", "Our remote tools team builds great stuff."), "unknown")


class ScorerParseTest(unittest.TestCase):
    def test_parse(self):
        raw = '```json\n{"score": 82.4, "requirements": [{"point": "5y PM", "matched": true, "must_have": true,' \
              ' "evidence": [{"bullet": "7 years PM at X", "sub_bullets": ["2018-2025"]}]},' \
              ' {"point": "Jira", "matched": false, "evidence": [{"bullet": "should be dropped"}]}],' \
              ' "gaps": ["Jira"], "summary": "Good fit.",}\n```'
        r = parse_result(raw)
        self.assertEqual(r["score"], 82)
        self.assertEqual(r["requirements"][1]["evidence"], [])
        self.assertEqual(r["gaps"], ["Jira"])

    def test_parse_garbage(self):
        with self.assertRaises(ScorerError):
            parse_result("I cannot help with that.")


if __name__ == "__main__":
    unittest.main()


class ResearchTest(unittest.TestCase):
    RESPONSE = {
        "id": "resp_1", "status": "completed",
        "output": [
            {"type": "web_search_call", "status": "completed", "results": [
                {"type": "text_result", "title": "About Acme", "url": "https://www.acme.com/about/"},
                {"type": "text_result", "title": "Acme fined over data breach", "url": "https://news.example.com/acme-fine"},
            ]},
            {"type": "message", "content": [{"type": "output_text", "annotations": [
                {"type": "url_citation", "url": "https://abc.net.au/news/acme-layoffs", "title": "Acme layoffs", "start_index": 0, "end_index": 5}],
                "text": json.dumps({
                    "official_name": "Acme Pty Ltd", "is_company": True, "is_recruiter": False,
                    "business_model": "Sells SaaS subscriptions.", "ownership": "Private, VC-backed.",
                    "headquarters": "Sydney, Australia",
                    "controversies": [
                        {"title": "Data breach fine", "year": "2024", "summary": "Fined by OAIC.",
                         "sources": [{"title": "Fine", "url": "https://news.example.com/acme-fine/"}]},
                        {"title": "Layoffs", "year": "2025", "summary": "Cut 10% of staff.",
                         "sources": [{"title": "ABC", "url": "https://www.abc.net.au/news/acme-layoffs"}]},
                        {"title": "Invented scandal", "year": "2023", "summary": "Hallucinated.",
                         "sources": [{"title": "Fake", "url": "https://made-up.example/story"}]},
                    ],
                    "controversy_note": "Two notable issues.",
                    "sources": [{"title": "About", "url": "https://acme.com/about"}, {"title": "x", "url": "https://nowhere.example"}],
                })}]},
        ],
    }

    def test_only_searched_links_survive(self):
        from backend.research import extract, parse_profile
        text, seen = extract(self.RESPONSE)
        profile, dropped = parse_profile(text, seen)
        self.assertEqual([c["title"] for c in profile["controversies"]], ["Data breach fine", "Layoffs"])
        self.assertEqual(len(profile["sources"]), 1)
        self.assertEqual(dropped, 2)
        self.assertEqual(profile["headquarters"], "Sydney, Australia")

    def test_research_saves_and_aborts_on_auth(self):
        from unittest import mock
        import backend.research as r
        from backend.db import CompanyProfile, SessionLocal

        cfg = {"api_key": "k", "base_url": "https://api.meta.ai/v1", "model": "m", "concurrency": 2}
        with mock.patch.object(r, "get_config", return_value=cfg), \
                mock.patch.object(r, "run_agent", return_value=self.RESPONSE):
            out = r.research_companies([{"name": "Acme", "key": "acme", "jobs": []}])
        self.assertEqual(out["researched"], 1)
        db = SessionLocal()
        row = db.get(CompanyProfile, "acme")
        self.assertEqual((row.status, len(row.to_dict()["controversies"])), ("done", 2))
        db.close()

        def rejected(company, cfg):
            raise ScorerError("HTTP 401: rejected", auth=True)
        with mock.patch.object(r, "get_config", return_value={**cfg, "concurrency": 1}), \
                mock.patch.object(r, "run_agent", side_effect=rejected):
            out = r.research_companies([{"name": f"C{i}", "key": f"c{i}", "jobs": []} for i in range(5)])
        self.assertIn("401", out["aborted"])
        self.assertEqual(out["errors"], 1)
        self.assertEqual(out["skipped"], 4)


class ResearchV2Test(unittest.TestCase):
    def _response(self, glassdoor_url):
        profile = {
            "official_name": "Acme", "is_company": True, "business_model": "SaaS.", "ownership": "Private.",
            "headquarters": "Sydney, Australia", "employee_count": "about 250 (2025, LinkedIn)",
            "glassdoor_url": glassdoor_url,
            "employee_sentiment": {
                "summary": "Mostly positive; some workload complaints.", "rating": "4.1/5 on Glassdoor (80 reviews)",
                "positives": ["Flexible hours", "Good team"], "negatives": ["Long hours before releases"],
                "sources": [{"title": "Reviews", "url": "https://www.glassdoor.com.au/Reviews/Acme-Reviews-E123.htm"},
                            {"title": "Made up", "url": "https://fake.example/reviews"}]},
            "controversies": [], "controversy_note": "None found.", "sources": [],
        }
        return {"status": "completed", "output": [
            {"type": "web_search_call", "results": [
                {"title": "Acme Reviews", "url": "https://www.glassdoor.com.au/Reviews/Acme-Reviews-E123.htm"}]},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(profile), "annotations": []}]}]}

    def test_new_fields_verified(self):
        from backend.research import extract, parse_profile
        p, dropped = parse_profile(*extract(self._response("https://www.glassdoor.com.au/Reviews/Acme-Reviews-E123.htm")))
        self.assertEqual(p["employee_count"], "about 250 (2025, LinkedIn)")
        self.assertIn("glassdoor.com.au/Reviews/Acme", p["glassdoor_url"])
        self.assertEqual(p["sentiment"]["rating"], "4.1/5 on Glassdoor (80 reviews)")
        self.assertEqual(len(p["sentiment"]["sources"]), 1)
        self.assertEqual(dropped, 1)

    def test_invented_glassdoor_url_replaced_by_searched_one(self):
        from backend.research import extract, parse_profile
        p, _ = parse_profile(*extract(self._response("https://www.glassdoor.com/Overview/Made-Up-E999.htm")))
        self.assertIn("E123", p["glassdoor_url"])

    def test_old_profiles_need_update(self):
        from datetime import datetime
        from backend.db import CompanyProfile, Job, SessionLocal
        from backend.research import RESEARCH_VERSION, companies_to_research
        db = SessionLocal()
        db.add(Job(url="https://x.example/job/1", title="PM", company="Oldco", status="to_review", detail_status="full"))
        db.add(CompanyProfile(key="oldco", name="Oldco", status="done", research_version=RESEARCH_VERSION - 1,
                              researched_at=datetime.utcnow()))
        db.commit()
        self.assertIn("oldco", [c["key"] for c in companies_to_research(db, 1)])
        db.close()


class LlmConfigTest(unittest.TestCase):
    def test_missing_settings_named(self):
        from unittest import mock
        from backend.scorer import config_problem, get_config
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": "", "LLM_MODEL": ""}):
            self.assertEqual(config_problem(get_config()), "Set LLM_BASE_URL, LLM_MODEL in .env and restart the server.")
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://x/v1/", "LLM_MODEL": "m"}):
            cfg = get_config()
            self.assertIsNone(config_problem(cfg))
            self.assertEqual(cfg["base_url"], "https://x/v1")

    def test_reasoning_effort_only_sent_when_set(self):
        from unittest import mock
        import backend.scorer as sc
        sent = []

        def fake_post(url, json=None, headers=None, timeout=None):
            sent.append(json)
            return mock.Mock(status_code=200, text="{}", headers={},
                             json=lambda: {"choices": [{"message": {"content": "{}"}}]})
        cfg = {"api_key": "k", "base_url": "https://x/v1", "model": "m", "timeout_s": 5}
        with mock.patch.object(sc.httpx, "post", side_effect=fake_post):
            sc.call_model([], {**cfg, "reasoning_effort": ""})
            sc.call_model([], {**cfg, "reasoning_effort": "low"})
        self.assertNotIn("reasoning_effort", sent[0])
        self.assertEqual(sent[1]["reasoning_effort"], "low")



class OfficeDaysTest(unittest.TestCase):
    def test_parse(self):
        from backend.geo import parse_office_days as p
        cases = {"We offer hybrid working with 3 days in the office.": 3, "2-3 days per week in office": 3,
                 "minimum of three days onsite": 3, "Hybrid: in the office 2 days a week": 2, "hybrid, 2 days WFH": 3,
                 "Flexible hybrid working": None, "Our 10 office locations": None,
                 "You will be in our Sydney CBD office 3 days": 3, "Work from home 1 day a week (hybrid)": 4}
        for text, expected in cases.items():
            self.assertEqual(p(text), expected, text)

    def test_max_office_days_rule(self):
        c = Criteria(max_office_days=2, pins=PINS)
        self.assertIn("3 office days", exclusion_reason(_job(office_days=3), c))
        self.assertIsNone(exclusion_reason(_job(office_days=2), c))
        self.assertIsNone(exclusion_reason(_job(office_days=None), c))  # unstated passes
        self.assertIsNone(exclusion_reason(_job(office_days=3), Criteria(pins=PINS)))  # no max set


class WorkspaceApiTest(unittest.TestCase):
    def test_isolation_and_copy(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import Job, SessionLocal

        with TestClient(app) as c:
            ws2 = c.post("/api/workspaces", json={"name": "Contract roles"}).json()["id"]
            h1, h2 = {"X-Workspace": "1"}, {"X-Workspace": str(ws2)}
            db = SessionLocal()
            db.add(Job(workspace_id=1, url="https://iso.example/job/1", title="Only in ws1", status="to_review", detail_status="full"))
            db.commit(); db.close()
            self.assertIn("Only in ws1", [j["title"] for j in c.get("/api/jobs", headers=h1).json()])
            self.assertNotIn("Only in ws1", [j["title"] for j in c.get("/api/jobs", headers=h2).json()])
            job_id = [j for j in c.get("/api/jobs", headers=h1).json() if j["title"] == "Only in ws1"][0]["id"]
            self.assertEqual(c.get(f"/api/jobs/{job_id}", headers=h2).status_code, 404)

            c.put("/api/profile", headers=h1, json={"titles": ["Product Manager"], "max_office_days": 2})
            self.assertEqual(c.get("/api/profile", headers=h2).json()["titles"], [])
            self.assertEqual(c.get("/api/profile", headers=h1).json()["max_office_days"], 2)
            c.post("/api/pins", headers=h1, json={"label": "Home", "kind": "home", "lat": -33.4, "lng": 151.3, "radius_km": 20})
            self.assertEqual(c.get("/api/pins", headers=h2).json(), [])
            url = "https://jobs.lever.co/iso-test"
            self.assertEqual(c.post("/api/sources", headers=h1, json={"url": url}).status_code, 201)
            self.assertEqual(c.post("/api/sources", headers=h2, json={"url": url}).status_code, 201)  # same URL, other workspace
            self.assertEqual(c.post("/api/sources", headers=h2, json={"url": url}).status_code, 409)

            ws3 = c.post("/api/workspaces", json={"name": "Copy", "copy_from": 1}).json()["id"]
            h3 = {"X-Workspace": str(ws3)}
            self.assertEqual(c.get("/api/profile", headers=h3).json()["titles"], ["Product Manager"])
            self.assertEqual(len(c.get("/api/pins", headers=h3).json()), len(c.get("/api/pins", headers=h1).json()))
            self.assertEqual(c.get("/api/jobs", headers=h3).json(), [])

            self.assertEqual(c.delete(f"/api/workspaces/{ws3}").status_code, 200)
            self.assertEqual(c.get("/api/profile", headers=h3).status_code, 404)
            for w in c.get("/api/workspaces").json():
                if w["id"] != 1:
                    c.delete(f"/api/workspaces/{w['id']}")
            self.assertEqual(c.delete("/api/workspaces/1").status_code, 409)

    def test_research_targets_selected_jobs(self):
        from backend.db import Job, SessionLocal
        from backend.research import companies_to_research
        db = SessionLocal()
        a = Job(workspace_id=1, url="https://sel.example/1", title="PM", company="Alpha Co", status="to_review", detail_status="full")
        b = Job(workspace_id=1, url="https://sel.example/2", title="PM", company="Beta Co", status="to_review", detail_status="full")
        db.add_all([a, b]); db.commit()
        picked = [x["key"] for x in companies_to_research(db, 1, "missing", [a.id])]
        self.assertIn("alpha co", picked)
        self.assertNotIn("beta co", picked)
        db.close()


class ReasoningLevelsTest(unittest.TestCase):
    def test_detect_and_effort_for(self):
        from unittest import mock
        import backend.llm_settings as ls

        def fake_post(url, json=None, headers=None, timeout=None):
            ok = json["reasoning_effort"] in ("low", "medium", "high")
            return mock.Mock(status_code=200 if ok else 400, text="" if ok else "unsupported reasoning_effort")
        cfg = {"api_key": "k", "base_url": "https://llm.example/v1", "model": "m1"}
        with mock.patch.object(ls.httpx, "post", side_effect=fake_post):
            out = ls.detect_levels(cfg)
        self.assertEqual(out["levels"], ["low", "medium", "high"])
        self.assertEqual(ls.supported_levels(cfg["base_url"], "m1"), ["low", "medium", "high"])
        ls._set("reasoning", {"scoring": "high", "research": "", "cover_letter": "xhigh"})
        with mock.patch.dict(os.environ, {"LLM_REASONING_EFFORT": ""}):
            self.assertEqual(ls.effort_for("scoring", cfg["base_url"], "m1"), "high")
            self.assertEqual(ls.effort_for("research", cfg["base_url"], "m1"), "")
            self.assertEqual(ls.effort_for("cover_letter", cfg["base_url"], "m1"), "")  # not accepted by m1
            self.assertEqual(ls.effort_for("scoring", cfg["base_url"], "never-detected"), "high")


class CoverLetterTest(unittest.TestCase):
    def test_draft_save_download(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import Document, FitResult, Job, SessionLocal
        import backend.scorer as sc

        db = SessionLocal()
        job = Job(workspace_id=1, url="https://cl.example/job/1", title="Program Manager", company="Acme",
                  description="Lead programs. Stakeholder management required.", status="to_review", detail_status="full")
        db.add(job)
        db.add(Document(workspace_id=1, filename="cv.pdf", kind="resume", text="Jane Doe. Program lead at Foo 2019-2025."))
        db.add(Document(workspace_id=1, filename="old.pdf", kind="cover_letter", text="Dear team, I build things."))
        db.commit()
        db.add(FitResult(job_id=job.id, status="ok", score=80, evidence_json=json.dumps(
            {"requirements": [{"point": "Stakeholders", "matched": True, "evidence": [{"bullet": "Led steering groups"}]}],
             "gaps": ["No PMP"]})))
        db.commit()
        job_id = job.id
        from backend.cover_letters import build_prompt
        prompt = build_prompt(db, db.get(Job, job_id), "Mention I start in 2 weeks")
        db.close()
        self.assertLess(prompt.index("RESUME"), prompt.index("EARLIER COVER LETTER"))
        for part in ("Led steering groups", "No PMP", "Mention I start in 2 weeks", "Stakeholder management"):
            self.assertIn(part, prompt)

        sent = {}

        def fake_call(messages, cfg, json_mode=True):
            sent.update(json_mode=json_mode, cfg=cfg)
            return "```\nDear Hiring Manager,\n\nI led steering groups.\n\nJane Doe\n```"
        with TestClient(app) as c, mock.patch.object(sc, "call_model", side_effect=fake_call), \
                mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://x/v1", "LLM_MODEL": "m"}):
            r = c.post(f"/api/jobs/{job_id}/cover-letter", json={"instructions": "short"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["text"].startswith("Dear Hiring Manager,"))
            self.assertFalse(sent["json_mode"])
            r = c.put(f"/api/jobs/{job_id}/cover-letter", json={"text": "Dear Hiring Manager,\n\nEdited.\n\nJane"})
            self.assertTrue(r.json()["edited"])
            r = c.get(f"/api/jobs/{job_id}/cover-letter.docx?ws=1")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.content[:2] == b"PK")  # a .docx is a zip file
            self.assertTrue([j for j in c.get("/api/jobs").json() if j["id"] == job_id][0]["has_cover_letter"])
            ws2 = c.post("/api/workspaces", json={"name": "Other"}).json()["id"]
            self.assertEqual(c.get(f"/api/jobs/{job_id}/cover-letter", headers={"X-Workspace": str(ws2)}).status_code, 404)
            c.delete(f"/api/workspaces/{ws2}")


class TitleSuggestionTest(unittest.TestCase):
    def test_parse_and_flag_existing(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import Document, SessionLocal
        import backend.scorer as sc

        db = SessionLocal()
        db.add(Document(workspace_id=1, filename="cv2.pdf", kind="resume", text="Ran programs for 8 years."))
        db.commit(); db.close()
        raw = json.dumps({"titles": [
            {"title": "Program  Manager", "fit": "strong", "reason": "8 years running programs"},
            {"title": "program manager", "fit": "good", "reason": "duplicate"},
            {"title": "Head of Delivery", "fit": "weird", "reason": "next step"}, "junk"]})
        with TestClient(app) as c, mock.patch.object(sc, "call_model", return_value=raw), \
                mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://x/v1", "LLM_MODEL": "m"}):
            c.put("/api/profile", json={"titles": ["Program Manager"]})
            r = c.post("/api/documents/suggest-titles").json()
            ws2 = c.post("/api/workspaces", json={"name": "No docs"}).json()["id"]
            empty = c.post("/api/documents/suggest-titles", headers={"X-Workspace": str(ws2)})
            c.delete(f"/api/workspaces/{ws2}")
        self.assertEqual([t["title"] for t in r["titles"]], ["Program Manager", "Head of Delivery"])
        self.assertTrue(r["titles"][0]["in_search"])
        self.assertEqual(r["titles"][1]["fit"], "good")
        self.assertEqual(empty.status_code, 400)


class StatusAndHideTest(unittest.TestCase):
    def test_new_statuses_and_bulk_hide(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import Job, SessionLocal
        from backend.research import companies_to_research
        from backend.scorer import select_jobs

        db = SessionLocal()
        jobs = [Job(workspace_id=1, url=f"https://hide.example/{i}", title="PM", company=f"Hide Co {i}",
                    status="to_review", detail_status="full") for i in range(3)]
        db.add_all(jobs); db.commit()
        ids = [j.id for j in jobs]
        db.close()
        with TestClient(app) as c:
            self.assertEqual(c.patch(f"/api/jobs/{ids[0]}/status", json={"status": "interviewing"}).status_code, 200)
            self.assertEqual(c.patch("/api/jobs/status", json={"ids": ids[1:], "status": "rejected"}).json()["updated"], 2)
            self.assertEqual(c.patch(f"/api/jobs/{ids[0]}/status", json={"status": "ghosted"}).status_code, 422)
            ws2 = c.post("/api/workspaces", json={"name": "Other"}).json()["id"]
            self.assertEqual(c.patch("/api/jobs/hidden", headers={"X-Workspace": str(ws2)},
                                     json={"ids": ids, "hidden": True}).json()["updated"], 0)
            c.delete(f"/api/workspaces/{ws2}")
            self.assertEqual(c.patch("/api/jobs/hidden", json={"ids": ids[:2], "hidden": True}).json()["updated"], 2)
            by_id = {j["id"]: j for j in c.get("/api/jobs").json()}
        self.assertEqual((by_id[ids[0]]["status"], by_id[ids[1]]["status"]), ("interviewing", "rejected"))
        self.assertEqual([by_id[i]["hidden"] for i in ids], [True, True, False])
        db = SessionLocal()
        pending = select_jobs(db, "pending", "x", 1)
        researched = [c["key"] for c in companies_to_research(db, 1)]
        db.close()
        self.assertNotIn(ids[0], pending)
        self.assertIn(ids[2], pending)
        self.assertNotIn("hide co 0", researched)
        self.assertIn("hide co 2", researched)


class SeekJobUploadTest(unittest.TestCase):
    PAGE = {"jobdetails": {"result": {
        "workArrangements": {"arrangements": [{"type": "HYBRID", "label": "Hybrid"}]},
        "job": {"id": "77777777", "title": "Senior Program Manager",
                "content": "<p>About the role</p><ul><li>Run the delivery program</li><li>3 days in the office</li></ul>",
                "location": {"label": "Surry Hills, Sydney NSW"}, "advertiser": {"name": "Acme"},
                "salary": {"label": "$160,000 - $180,000 per year"}, "listedAt": {"dateTimeUtc": "2026-09-28"},
                "classifications": [{"label": "Program & Project Management"}]}}}}

    def _mhtml(self, html: str) -> bytes:
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = "<Saved by Blink>"
        msg["Subject"] = "SEEK"
        msg.set_content(html, subtype="html", cte="quoted-printable")
        return bytes(msg)

    def test_decode_saved_page(self):
        from backend.scraping.saved_pages import decode_saved_page
        html = _seek_html(self.PAGE)
        self.assertEqual(decode_saved_page(html.encode()), html)
        self.assertIn("SEEK_REDUX_DATA", decode_saved_page(self._mhtml(html)))
        self.assertEqual(parse_job_page(decode_saved_page(self._mhtml(html))).external_id, "77777777")

    def test_upload_updates_the_right_job_only(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import Job, SessionLocal

        db = SessionLocal()
        job = Job(workspace_id=1, url="https://www.seek.com.au/job/77777777", external_id="77777777", source="seek",
                  title="Senior Program Manager", company="Acme", description="Short teaser.", detail_status="summary",
                  country="AU", status="to_review")
        other = Job(workspace_id=1, url="https://www.seek.com.au/job/11111111", external_id="11111111", source="seek",
                    title="Other role", company="Beta", description="Teaser.", detail_status="summary", status="to_review")
        db.add_all([job, other]); db.commit()
        job_id, other_id = job.id, other.id
        db.close()
        page = _seek_html(self.PAGE).encode()
        from unittest import mock
        import backend.pipeline as pipeline
        with TestClient(app) as c, mock.patch.object(pipeline, "geocode", return_value=None):  # no network in tests
            wrong = c.post(f"/api/jobs/{other_id}/import-page", files={"file": ("ad.html", page, "text/html")})
            self.assertEqual(wrong.status_code, 422)
            self.assertIn("different SEEK job", wrong.json()["detail"])
            search = _seek_html({"results": {"totalPages": 1, "results": {"jobs": [{"id": "1", "title": "X"}]}}}).encode()
            r = c.post(f"/api/jobs/{job_id}/import-page", files={"file": ("s.html", search, "text/html")})
            self.assertIn("search results page", r.json()["detail"])
            r = c.post(f"/api/jobs/{job_id}/import-page", files={"file": ("x.html", b"<html>hi</html>", "text/html")})
            self.assertEqual(r.status_code, 422)
            ws2 = c.post("/api/workspaces", json={"name": "Other"}).json()["id"]
            r = c.post(f"/api/jobs/{job_id}/import-page", headers={"X-Workspace": str(ws2)},
                       files={"file": ("ad.mhtml", self._mhtml(page.decode()), "multipart/related")})
            self.assertEqual(r.status_code, 404)
            c.delete(f"/api/workspaces/{ws2}")
            r = c.post(f"/api/jobs/{job_id}/import-page", files={"file": ("ad.mhtml", self._mhtml(page.decode()), "multipart/related")})
            self.assertEqual(r.status_code, 200, r.text)
            j = r.json()
        self.assertEqual(j["detail_status"], "full")
        self.assertIn("- Run the delivery program", j["description"])
        self.assertEqual((j["work_mode"], j["office_days"], j["salary_text"]), ("hybrid", 3, "$160,000 - $180,000 per year"))
        self.assertEqual(j["location_text"], "Surry Hills, Sydney NSW")


class OfficeDaySofteningTest(unittest.TestCase):
    def test_temporary_softening_is_a_what_if(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import Job, SessionLocal
        from backend.scorer import select_jobs

        with TestClient(app) as c:
            ws = c.post("/api/workspaces", json={"name": "Soften"}).json()["id"]
            h = {"X-Workspace": str(ws)}
            c.put("/api/profile", headers=h, json={"max_office_days": 2})
            db = SessionLocal()
            spec = {"2d": (2, "AU"), "3d": (3, "AU"), "4d": (4, "AU"), "abroad": (3, "US")}
            jobs = {k: Job(workspace_id=ws, url=f"https://soft.example/{k}", title="PM", company="Soft Co",
                           work_mode="hybrid", office_days=d, country=country, status="to_review", detail_status="full")
                    for k, (d, country) in spec.items()}
            db.add_all(jobs.values()); db.commit()
            ids = {k: j.id for k, j in jobs.items()}
            db.close()
            c.post("/api/jobs/refilter", headers=h)

            def listed(slack):
                return {j["id"]: j for j in c.get(f"/api/jobs?office_slack={slack}", headers=h).json()}

            strict, plus1, plus2 = listed(0), listed(1), listed(2)
            self.assertIsNone(strict[ids["2d"]]["excluded_reason"])
            self.assertIn("3 office days", strict[ids["3d"]]["excluded_reason"])
            self.assertNotIn("softened_reason", strict[ids["3d"]])
            self.assertIsNone(plus1[ids["3d"]]["excluded_reason"])
            self.assertIn("3 office days", plus1[ids["3d"]]["softened_reason"])
            self.assertIn("4 office days", plus1[ids["4d"]]["excluded_reason"])
            self.assertIsNone(plus2[ids["4d"]]["excluded_reason"])
            self.assertIsNotNone(plus2[ids["abroad"]]["excluded_reason"])  # other rules still apply
            self.assertNotIn("softened_reason", plus2[ids["abroad"]])
            self.assertIsNone(c.get(f"/api/jobs/{ids['3d']}?office_slack=1", headers=h).json()["excluded_reason"])
            self.assertEqual(c.get("/api/score/status?office_slack=1", headers=h).json()["softened_pending"], 1)
            self.assertEqual(c.get("/api/jobs?office_slack=9", headers=h).status_code, 422)

            db = SessionLocal()
            self.assertNotIn(ids["3d"], select_jobs(db, "pending", "x", ws))
            self.assertEqual(set(select_jobs(db, "pending", "x", ws, 1)), {ids["2d"], ids["3d"]})
            self.assertIn("3 office days", db.get(Job, ids["3d"]).excluded_reason)  # nothing saved
            db.close()
            c.delete(f"/api/workspaces/{ws}")


class ApsJobsTest(unittest.TestCase):
    JOB = {"jobId": "a05X1", "jobName": "ICT Project Manager", "departmentName": "Services Australia",
           "jobLocation": "Adelaide SA, Sydney NSW, Various locations - VIC VIC", "officeArrangement": "On Site;Flexible",
           "officeArrangementDetails": "Negotiable", "jobSalaryFrom": 122493.0, "jobSalaryTo": 135731.0,
           "jobClassification": "Executive Level 1", "jobStatus": "Ongoing;Non-Ongoing", "jobType": "Full-Time",
           "jobDuties": "<p>Lead delivery of ICT projects.</p>", "jobPostedDate": "2026-09-29",
           "jobCloseDate": "2026-10-12", "vacancyNumber": "VN-1"}

    def test_listing_to_posting(self):
        from backend.adapters.apsjobs import is_apsjobs_url, search_url, split_locations, to_posting, work_mode
        p = to_posting(self.JOB)
        self.assertEqual(p.url, "https://www.apsjobs.gov.au/s/job-details?Id=a05X1")
        self.assertEqual(p.location_text, "Adelaide SA; Sydney NSW; VIC")
        self.assertEqual((p.work_mode, p.salary_min, p.salary_text), ("hybrid", 122493.0, "$122,493 - $135,731"))
        self.assertEqual((p.detail_status, p.country, p.external_id), ("full", "AU", "VN-1"))
        self.assertIn("Lead delivery of ICT projects.", p.description)
        self.assertIn("Employment: Ongoing, Non-Ongoing, Full-Time", p.description)
        self.assertIsNone(to_posting({"jobName": "no id"}))
        self.assertEqual(split_locations("Canberra ACT"), ["Canberra ACT"])
        self.assertEqual([work_mode(v) for v in ("Work From Home", "On Site", "Hybrid", "")],
                         ["remote", "onsite", "hybrid", "unknown"])
        self.assertTrue(is_apsjobs_url(search_url("Services Australia")))
        self.assertFalse(is_apsjobs_url("https://www.apsjobs.gov.au/s/job-details?Id=1"))

    def test_adapter_pages_and_label(self):
        from unittest import mock
        from backend.adapters.apsjobs import ApsJobsAdapter, search_url
        from backend.adapters.generic import GenericAdapter, source_adapter
        from backend.scraping.http import Response
        from backend.sources import default_label

        page = Response(url="", status=200, text='x fwuid%22%3A%22FW%22 y communityApp%22%3A%22APP%22 z')
        seen = []

        def answer(offset):
            jobs = [{**self.JOB, "jobId": f"id{offset + i}"} for i in range(15 if offset == 0 else 5)]
            value = {"jobListingCount": 20, "jobListings": jobs, "newOffset": offset + len(jobs)}
            return Response(url="", status=200, text=json.dumps({"actions": [{"state": "SUCCESS", "returnValue": {"returnValue": value}}]}))

        def post_form(url, data, ttl=None):
            filt = json.loads(json.loads(data["message"])["actions"][0]["params"]["params"]["filter"])
            seen.append((filt["department"], filt["offset"], json.loads(data["aura.context"])["fwuid"]))
            return answer(filt["offset"])

        client = mock.Mock(get=mock.Mock(return_value=page), post_form=mock.Mock(side_effect=post_form))
        url = search_url("Services Australia")
        adapter = source_adapter(client, url)
        self.assertIsInstance(adapter, ApsJobsAdapter)
        postings = adapter.list_postings()
        self.assertEqual(len(postings), 20)
        self.assertEqual(seen, [(["Services Australia"], 0, "FW"), (["Services Australia"], 15, "FW")])
        self.assertTrue(adapter.complete_listing)
        self.assertFalse(adapter.has_details)
        self.assertEqual(default_label(url), "Services Australia")
        self.assertIsInstance(source_adapter(client, "https://example.com/careers"), GenericAdapter)


class FeedAndChallengeTest(unittest.TestCase):
    def test_bot_challenge_is_a_fetch_error(self):
        from backend.scraping.http import bot_challenge
        self.assertTrue(bot_challenge(202, {"x-amzn-waf-action": "challenge"}, ""))
        self.assertTrue(bot_challenge(403, {"cf-mitigated": "challenge"}, "<html>"))
        self.assertTrue(bot_challenge(202, {}, "  "))
        self.assertFalse(bot_challenge(200, {}, "<html>jobs</html>"))

    def test_rss_feed_listing(self):
        from backend.adapters.generic import parse_feed
        rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>Jobs</title>
          <item><title><![CDATA[Product Manager, Streaming]]></title>
            <link>https://careers.example.com/job-details/1/</link>
            <description><![CDATA[<p>Own the <b>roadmap</b></p>]]></description>
            <pubDate>Mon, 28 Sep 2026</pubDate>
            <category><![CDATA[Australia - NSW/Sydney/North Shore]]></category></item>
          <item><title>No link</title></item></channel></rss>"""
        [p] = parse_feed(rss, "Example")
        self.assertEqual((p.title, p.location_text, p.company, p.country), ("Product Manager, Streaming", "Sydney NSW", "Example", "AU"))
        self.assertIn("Own the roadmap", p.description)
        self.assertEqual(parse_feed("<html><body>hi</body></html>", "X"), [])

    def test_detail_prefers_heading_matching_title(self):
        html = ("<html><body><div class='search'><h1>Job Search</h1><form><input></form></div>"
                "<main><h1>Scheduler, Planning and Operations</h1><p>" + "You will plan campaigns and lead scheduling. " * 20
                + "</p><p>Apply now. Responsibilities include managing the schedule.</p></main></body></html>")
        p = parse_detail(html, Posting(url="https://careers.example.com/job/1", title="Scheduler, Planning and Operations"))
        self.assertIsNotNone(p)
        self.assertEqual(p.title, "Scheduler, Planning and Operations")
        self.assertIn("plan campaigns", p.description)


class MultiPlaceTest(unittest.TestCase):
    def test_nearest_place_or_unplaced(self):
        from unittest import mock
        from backend import pipeline
        coords = {"Adelaide SA": (-34.93, 138.6), "Sydney NSW": (-33.87, 151.21), "Canberra ACT": (-35.28, 149.13)}
        fake = lambda q: ({"lat": coords[q][0], "lng": coords[q][1], "country": "AU"} if q in coords else None)
        with mock.patch.object(pipeline, "geocode", side_effect=fake):
            hit = pipeline.locate("Adelaide SA; Sydney NSW; Canberra ACT", PINS)
            self.assertEqual((hit["lat"], hit["lng"]), coords["Sydney NSW"])
            self.assertIsNone(pipeline.locate("Canberra ACT; NSW", PINS))  # anywhere in NSW: don't rule it out
            self.assertEqual(pipeline.locate("Canberra ACT", PINS)["lat"], coords["Canberra ACT"][0])


def _ad_job(**kw):
    """An unsaved Job for comparing ads (scan fields filled in like the pipeline does)."""
    from backend.db import Job
    from backend.scraping.text import description_hash
    kw.setdefault("company", "Acme")
    kw.setdefault("detail_status", "full")
    kw.setdefault("country", "AU")
    kw.setdefault("status", "to_review")
    job = Job(**kw)
    job.description_hash = description_hash(job.description or "")
    return job


LONG_AD = ("You will own the rollout of our platform to enterprise schools, run implementation plans, "
           "coordinate stakeholders across product, support and engineering, and report progress to executives. ") * 4


class DuplicateCompareTest(unittest.TestCase):
    def compare(self, a, b):
        from backend.duplicates import Ad, compare
        return compare(Ad(_ad_job(**a)), Ad(_ad_job(**b)))

    def test_names_and_titles(self):
        from backend.duplicates import company_names, same_company, title_words
        self.assertTrue(same_company(company_names("Commonwealth Scientific and Industrial Research Organisation (CSIRO)"),
                                     company_names("CSIRO")))
        self.assertTrue(same_company(company_names("Canva Pty Ltd"), company_names("Canva")))
        self.assertTrue(same_company(company_names("Profitable Tradie - Trades Business Specialist"), company_names("Profitable Tradie")))
        self.assertFalse(same_company(company_names("Australian Payments Plus"), company_names("Australian Taxation Office")))
        self.assertEqual(title_words("Program Manager - Sydney (12 month contract)", "Sydney NSW"), {"program", "manager"})
        self.assertEqual(title_words("B2B Product Manager (Growth)"), title_words("B2B Product Manager – Growth"))

    def test_tiers(self):
        site = dict(source="site:acme.com", title="Implementation Manager, Enterprise SaaS", location_text="Sydney, NSW",
                    description=LONG_AD, url="https://acme.com/jobs/1")
        seek = dict(source="seek", title="Implementation Manager, Enterprise SaaS", location_text="Eveleigh, Sydney NSW",
                    description="Lead enterprise rollouts.", detail_status="summary", url="https://seek/1")
        self.assertEqual(self.compare(site, seek)["tier"], "sure")
        # A generic title could be a different opening: ask.
        generic = self.compare({**site, "title": "Product Manager"}, {**seek, "title": "Product Manager"})
        self.assertEqual(generic["tier"], "possible")
        # Same title at different employers: different jobs.
        self.assertIsNone(self.compare({**seek, "title": "Product Manager"},
                                       {**seek, "company": "Other Co", "title": "Product Manager", "url": "https://seek/2"}))
        # One ad per office on the same site, even in the same city.
        self.assertIsNone(self.compare({**seek, "location_text": "Bella Vista, Sydney NSW"},
                                       {**seek, "location_text": "Chullora, Sydney NSW", "url": "https://seek/2"}))
        # The same ad posted twice on one site.
        twice = dict(seek, description="Lead enterprise rollouts across our schools. " * 4)
        self.assertEqual(self.compare(twice, {**twice, "url": "https://seek/2"})["tier"], "sure")
        # Senior vs not, other country: never sure.
        senior = self.compare(site, {**seek, "title": "Senior Implementation Manager, Enterprise SaaS"})
        self.assertNotEqual((senior or {}).get("tier"), "sure")
        self.assertIsNone(self.compare(site, {**seek, "location_text": "London, United Kingdom", "country": "GB"}))
        # Retitled copy on a job board with the same ad text.
        board = dict(site, source="site:board.example", title="Implementation Manager", url="https://board/1",
                     description="Apply on our board. " + LONG_AD)
        self.assertEqual(self.compare(site, board)["tier"], "possible")


class DuplicateMergeTest(unittest.TestCase):
    def test_scan_merge_decide_split(self):
        from datetime import datetime
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import CoverLetter, FitResult, Job, SessionLocal
        from backend.duplicates import scan
        from backend.scorer import select_jobs

        with TestClient(app) as c:
            ws = c.post("/api/workspaces", json={"name": "Dupes"}).json()["id"]
            h = {"X-Workspace": str(ws)}
            db = SessionLocal()
            spec = {
                "site": dict(source="site:acme.com", title="Implementation Manager, Enterprise SaaS", location_text="Sydney, NSW",
                             description=LONG_AD),
                "seek": dict(source="seek", title="Implementation Manager, Enterprise SaaS", location_text="Eveleigh, Sydney NSW",
                             description="Lead enterprise rollouts.", detail_status="summary", status="applied",
                             salary_text="$150k", salary_min=150000, salary_max=150000),
                "pm_seek": dict(source="seek", title="Product Manager", location_text="Sydney NSW", description="Own the roadmap.",
                                detail_status="summary"),
                "pm_site": dict(source="site:acme.com", title="Product Manager", location_text="Sydney, NSW",
                                description="Own our maths roadmap with teachers. " * 12),
                "pm_other": dict(source="seek", company="Other Co", title="Product Manager", location_text="Sydney NSW",
                                 description="Own the roadmap.", detail_status="summary"),
            }
            ids = {}
            for name, kw in spec.items():
                job = _ad_job(**kw)
                job.workspace_id, job.url, job.first_seen = ws, f"https://dupes.example/{name}", datetime.utcnow()
                db.add(job); db.commit()
                ids[name] = job.id
            db.add(CoverLetter(job_id=ids["seek"], text="Dear Hiring Manager"))
            db.add(FitResult(job_id=ids["seek"], status="ok", score=70, evidence_json="{}"))
            db.commit()
            self.assertEqual(scan(db, ws), {"merged": 1, "suggested": 1})
            self.assertEqual(scan(db, ws), {"merged": 0, "suggested": 1})  # stable
            db.close()

            jobs = {j["id"]: j for j in c.get("/api/jobs", headers=h).json()}
            self.assertNotIn(ids["seek"], jobs)
            merged = jobs[ids["site"]]  # the employer's full ad is the one listed
            self.assertEqual([x["id"] for x in merged["also_on"]], [ids["seek"]])
            self.assertEqual((merged["status"], merged["fit"]["score"], merged["has_cover_letter"], merged["salary_text"]),
                             ("applied", 70, True, "$150k"))
            dupes = c.get("/api/duplicates", headers=h).json()
            self.assertEqual({dupes["suggested"][0]["a"]["id"], dupes["suggested"][0]["b"]["id"]}, {ids["pm_seek"], ids["pm_site"]})
            self.assertEqual(len(dupes["auto_merged"]), 1)

            # The user says the two Product Manager ads are different jobs: never suggested again.
            r = c.post("/api/duplicates/decide", headers=h, json={"a": ids["pm_seek"], "b": ids["pm_site"], "decision": "distinct"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(c.post("/api/duplicates/scan", headers=h).json(), {"merged": 0, "suggested": 0})
            self.assertEqual(c.post("/api/duplicates/decide", json={"a": ids["pm_seek"], "b": ids["pm_site"],
                                                                     "decision": "merge"}).status_code, 404)  # other workspace

            # Manual merge, then split: split copies stay apart.
            r = c.post("/api/jobs/merge", headers=h, json={"ids": [ids["pm_seek"], ids["pm_other"]]})
            self.assertEqual(r.status_code, 200, r.text)
            listed = {j["id"] for j in c.get("/api/jobs", headers=h).json()}
            self.assertEqual(len(listed & {ids["pm_seek"], ids["pm_other"]}), 1)
            db = SessionLocal()
            pending = select_jobs(db, "pending", "x", ws)
            db.close()
            self.assertEqual(len(set(pending) & {ids["pm_seek"], ids["pm_other"]}), 1)
            self.assertEqual(c.post(f"/api/jobs/{ids['seek']}/unmerge", headers=h).status_code, 200)
            self.assertEqual(c.post(f"/api/jobs/{ids['seek']}/unmerge", headers=h).status_code, 400)
            c.post("/api/duplicates/scan", headers=h)
            listed = {j["id"]: j for j in c.get("/api/jobs", headers=h).json()}
            self.assertIn(ids["seek"], listed)
            self.assertEqual(listed[ids["site"]]["also_on"], [])
            c.delete(f"/api/workspaces/{ws}")


class JobChatTest(unittest.TestCase):
    def test_chat_context_links_history_and_web_search(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import CompanyProfile, CoverLetter, Document, FitResult, Job, SessionLocal
        import backend.research as rs
        import backend.scorer as sc

        env = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "m"}
        with TestClient(app) as c, mock.patch.dict(os.environ, env):
            ws = c.post("/api/workspaces", json={"name": "Chat"}).json()["id"]
            h = {"X-Workspace": str(ws)}
            db = SessionLocal()
            db.add(Document(workspace_id=ws, filename="cv.pdf", kind="resume", text="Jane Doe. Ran Mathletics releases."))
            job = Job(workspace_id=ws, url="https://chat.example/job/1", title="Product Owner", company="Chatco Learning",
                      description="Own the backlog for our maths platform.", status="shortlisted", detail_status="full")
            db.add(job); db.commit()
            copy = Job(workspace_id=ws, url="https://board.example/9", title="Product Owner", company="Chatco Learning",
                       duplicate_of=job.id, detail_status="summary", status="to_review")
            db.add(copy)
            db.add(FitResult(job_id=job.id, status="ok", score=77, evidence_json=json.dumps(
                {"summary": "Strong backlog evidence.", "requirements": [{"point": "Backlog", "matched": True}], "gaps": ["No SAFe"]})))
            db.add(CoverLetter(job_id=job.id, text="Dear Hiring Manager, my letter."))
            db.merge(CompanyProfile(key="chatco learning", name="Chatco Learning", status="done", business_model="Sells maths software.",
                                    sources_json=json.dumps([{"title": "About", "url": "https://chatco.example/about"}])))
            db.commit()
            job_id, copy_id = job.id, copy.id
            db.close()

            sent = []

            def fake_call(messages, cfg, json_mode=True):
                sent.append(messages)
                return ("You fit well. See [the ad](https://chat.example/job/1), [About](https://chatco.example/about/) "
                        "and [a guess](https://made-up.example/x) or https://unknown.example/y.")
            with mock.patch.object(sc, "call_model", side_effect=fake_call):
                r = c.post("/api/chats", headers=h, json={"job_ids": [copy_id], "message": "How do I fit?"})
                self.assertEqual(r.status_code, 200, r.text)
                chat = r.json()
                self.assertEqual(chat["job_ids"], [job_id])  # a merged copy means its job
                answer = chat["messages"][1]["content"]
                self.assertIn("[the ad](https://chat.example/job/1)", answer)
                self.assertIn("[About](https://chatco.example/about/)", answer)
                self.assertIn("a guess", answer)
                self.assertNotIn("made-up.example", answer)
                self.assertNotIn("unknown.example", answer)
                context = sent[0][0]["content"]
                for part in ("Jane Doe", "Own the backlog", "77/100", "No SAFe", "Sells maths software", "my letter",
                             "https://board.example/9", "shortlisted"):
                    self.assertIn(part, context)
                r = c.post(f"/api/chats/{chat['id']}/messages", headers=h, json={"message": "And the gaps?"})
                self.assertEqual(len(r.json()["messages"]), 4)
                self.assertEqual([m["role"] for m in sent[1][1:]], ["user", "assistant", "user"])

            calls = []

            def fake_response(payload, cfg, what="web search"):
                calls.append(payload)
                return {"status": "completed", "output": [
                    {"type": "web_search_call", "results": [{"url": "https://news.example/a", "title": "Chatco news"}]},
                    {"type": "message", "content": [{"type": "output_text",
                                                     "text": "Recent [news](https://news.example/a) and [fake](https://fake.example/b)."}]}]}
            with mock.patch.object(rs, "run_response", side_effect=fake_response):
                r = c.post(f"/api/chats/{chat['id']}/messages", headers=h, json={"message": "Any news?", "web_search": True})
            self.assertEqual(r.status_code, 200, r.text)
            last = r.json()["messages"][-1]
            self.assertEqual(last["sources"], [{"title": "Chatco news", "url": "https://news.example/a", "cited": True}])
            self.assertNotIn("fake.example", last["content"])
            self.assertEqual(calls[0]["tools"][0]["type"], "web_search")
            self.assertEqual(len(calls[0]["input"]), 5)
            # No citations at all (some providers): list the pages the search returned instead.
            from backend.job_chat import web_sources
            searched = {"output": [{"type": "web_search_call", "results": [{"url": "https://a.example/1", "title": "A"}]}]}
            self.assertEqual(web_sources(searched, "No links here.", {"https://a.example/1": "A"}),
                             [{"title": "A", "url": "https://a.example/1", "cited": False}])

            self.assertEqual([t["id"] for t in c.get(f"/api/chats?job_ids={job_id}", headers=h).json()], [chat["id"]])
            self.assertEqual(c.get(f"/api/chats/{chat['id']}").status_code, 404)  # other workspace
            self.assertEqual(c.post("/api/chats", headers=h, json={"job_ids": list(range(1, 14)), "message": "x"}).status_code, 400)
            self.assertEqual(c.delete(f"/api/chats/{chat['id']}", headers=h).status_code, 200)
            self.assertEqual(c.get(f"/api/chats/{chat['id']}", headers=h).status_code, 404)
            c.delete(f"/api/workspaces/{ws}")


class GeocoderPrecisionTest(unittest.TestCase):
    def test_suburb_beats_council_area(self):
        from unittest import mock
        import backend.geo as geo
        council = {"addresstype": "administrative", "place_rank": 12, "importance": 0.46, "lat": "-33.57", "lon": "151.13",
                   "display_name": "The Council of the Shire of Hornsby, Sydney", "address": {"country_code": "au"}}
        town = {"addresstype": "town", "place_rank": 18, "importance": 0.2, "lat": "-33.70", "lon": "151.10",
                "display_name": "Hornsby, New South Wales", "address": {"country_code": "au"}}
        self.assertEqual(geo._pick([council, town])["addresstype"], "town")
        self.assertIsNone(geo._pick([{"addresstype": "state", "place_rank": 8, "importance": 0.9}]))
        answers = {"Hornsby, Sydney NSW": [council], "Hornsby, NSW": [council, town]}

        def fake_get(url, params=None, headers=None, timeout=None):
            return mock.Mock(json=lambda: answers[params["q"]], raise_for_status=lambda: None)
        with mock.patch.object(geo.httpx, "get", side_effect=fake_get), mock.patch.object(geo, "_geo_last", 0.0):
            hit = geo.geocode("Hornsby, Sydney NSW")
        self.assertEqual((hit["lat"], hit["type"]), (-33.70, "town"))

    def test_city_only(self):
        from backend.geo import is_city_only
        for text in ("Sydney NSW", "Sydney, , Australia", "Australia - Sydney - New South Wales",
                     "Sydney - Australia - Sydney, 2000 Australia; Remote - Remote", "Adelaide SA; Sydney NSW"):
            self.assertTrue(is_city_only(text), text)
        for text in ("Eveleigh, Sydney NSW", "Sydney CBD", "Remote", "", "Hornsby, Sydney NSW"):
            self.assertFalse(is_city_only(text), text)
        # An area of a city (SEEK, when the ad names no suburb) doesn't say where the office is either.
        for text in ("North West & Hills District, Sydney NSW", "CBD, Inner West & Eastern Suburbs, Sydney NSW",
                     "Bayside & South Eastern Suburbs, Melbourne VIC", "Northern Suburbs & Joondalup, Perth WA"):
            self.assertTrue(is_city_only(text), text)
        self.assertFalse(is_city_only("Terrigal, Gosford & Central Coast NSW"))

    def test_seek_regions_and_areas(self):
        from unittest import mock
        import backend.geo as geo

        def place(kind, lat, name, rank=16):
            return {"addresstype": kind, "place_rank": rank, "importance": 0.3, "lat": str(lat), "lon": "151.0",
                    "display_name": name, "address": {"country_code": "au"}}
        bus_stop = place("highway", -33.43, "Central Coast Hwy opp Woy Woy Rd, Kariong", rank=30)
        answers = {
            "Woy Woy, NSW": [place("town", -33.48, "Woy Woy, New South Wales")],
            "Woy Woy, Gosford & Central Coast NSW": [bus_stop],
            "Geraldton, WA": [place("city", -28.78, "Geraldton, Western Australia")],
            "Gosford, NSW": [place("suburb", -33.42, "Gosford, New South Wales")],
            "Alkimos, Perth WA": [],
            "Alkimos, WA": [place("suburb", -31.63, "Alkimos, Western Australia")],
            "Castle Hill, NSW": [place("suburb", -33.73, "Castle Hill, New South Wales")],
            "Erina, NSW": [place("amenity", -33.44, "Erina High School, Erina", rank=30)],
            "Erina, Gosford & Central Coast NSW": [place("suburb", -33.44, "Erina, New South Wales")],
        }
        asked = []

        def fake_get(url, params=None, headers=None, timeout=None):
            asked.append(params["q"])
            return mock.Mock(json=lambda: answers.get(params["q"], []), raise_for_status=lambda: None)
        with mock.patch.object(geo.httpx, "get", side_effect=fake_get), mock.patch.object(geo, "_geo_last", 0.0):
            # A regional suburb is looked up in its state: the region's words found a bus stop.
            self.assertEqual(geo.geocode("Woy Woy, Gosford & Central Coast NSW")["lat"], -33.48)
            self.assertEqual(asked, ["Woy Woy, NSW"])
            self.assertEqual(geo.geocode("Geraldton, Geraldton, Gascoyne & Midwest WA")["lat"], -28.78)
            self.assertEqual(geo.geocode("Gosford & Central Coast NSW")["lat"], -33.42)  # the region's first town
            # In a big city the full form goes first; when it finds nothing, the suburb in its state.
            asked.clear()
            self.assertEqual(geo.geocode("Alkimos, Perth WA")["lat"], -31.63)
            self.assertEqual(asked, ["Alkimos, Perth WA", "Alkimos, WA"])
            # An area of a city is placed at its main centre.
            self.assertEqual(geo.geocode("North West & Hills District, Sydney NSW")["lat"], -33.73)
            # A building that only shares the suburb's name isn't the suburb: the full form decides.
            self.assertEqual(geo.geocode("Erina, Gosford & Central Coast NSW")["type"], "suburb")

    def test_seek_geocode_repair(self):
        from unittest import mock
        import backend.geo as geo
        from backend.db import AppSetting, GeocodeCache, Job, SessionLocal
        from backend.filters import load_criteria
        from backend.pipeline import repair_geocodes

        db = SessionLocal()
        db.add_all([
            GeocodeCache(query="umina beach, gosford & central coast nsw", lat=None),
            GeocodeCache(query="kariong, gosford & central coast nsw", lat=-33.43, lng=151.29,
                         display_name="Central Coast Highway, Kariong, Gosford"),
            GeocodeCache(query="lane cove, sydney nsw", lat=-33.81, lng=151.17, display_name="Lane Cove, Sydney, NSW"),
            GeocodeCache(query="parramatta & western suburbs, sydney nsw", lat=None),
            GeocodeCache(query="tas", lat=None),
        ])
        job = Job(workspace_id=1, url="https://repair.example/1", title="PM", location_text="Umina Beach, Gosford & Central Coast NSW",
                  work_mode="onsite", status="to_review", detail_status="full")
        db.add(job); db.commit()
        db.query(AppSetting).filter(AppSetting.key.like("geocode_repair_seek%")).delete(synchronize_session=False)
        db.commit()
        town = {"addresstype": "town", "place_rank": 16, "importance": 0.3, "lat": "-33.52", "lon": "151.31",
                "display_name": "Umina Beach, New South Wales", "address": {"country_code": "au"}}
        with mock.patch.object(geo.httpx, "get", return_value=mock.Mock(json=lambda: [town], raise_for_status=lambda: None)), \
                mock.patch.object(geo, "_geo_last", 0.0):
            repair_geocodes(db, 1, load_criteria(db, 1), lambda stage, info: None)
        db.refresh(job)
        self.assertEqual((job.lat, job.lng), (-33.52, 151.31))
        cached = {r.query for r in db.query(GeocodeCache)}
        self.assertNotIn("kariong, gosford & central coast nsw", cached)  # a highway, not the suburb
        self.assertNotIn("parramatta & western suburbs, sydney nsw", cached)
        self.assertTrue({"lane cove, sydney nsw", "tas", "umina beach, gosford & central coast nsw"} <= cached)
        self.assertIn(1, json.loads(db.get(AppSetting, "geocode_repair_seek_workspaces").value_json))
        db.delete(job); db.commit(); db.close()

    def test_location_verdict(self):
        c = Criteria(allow_hybrid=True, pins=PINS)
        far = {"title": "PM", "work_mode": "hybrid", "lat": -37.8, "lng": 144.9, "location_text": "Melbourne VIC", "country": "AU"}
        self.assertIn("outside your pin radii", exclusion_reason(far, c, stage="full"))
        self.assertIsNone(exclusion_reason({**far, "location_verdict": "ok"}, c, stage="full"))
        self.assertEqual(exclusion_reason({**far, "lat": -33.87, "lng": 151.21, "location_verdict": "too_far"}, c, stage="full"),
                         "you marked the office as too far")


def _fake_geocode(places):
    """geocode() stand-in: {query: (lat, lng)}; anything else is unknown."""
    def geocode(text):
        for key, (lat, lng) in places.items():
            if key.lower() in (text or "").lower():
                return {"lat": lat, "lng": lng, "country": "AU", "type": "suburb"}
        return None
    return geocode


class OfficeTest(unittest.TestCase):
    PLACES = {"North Sydney": (-33.839, 151.207), "Macquarie Park": (-33.781, 151.126), "Sydney NSW": (-33.87, 151.21),
              "Perth": (-31.95, 115.86)}

    def patched(self):
        from unittest import mock
        import backend.offices as of
        import backend.pipeline as pl
        fake = _fake_geocode(self.PLACES)
        return mock.patch.object(of, "geocode", side_effect=fake), mock.patch.object(pl, "geocode", side_effect=fake)

    def test_queries_and_ad_check(self):
        from backend.db import Job
        from backend.offices import office_in_ad, office_queries
        job = Job(location_text="Sydney NSW")
        self.assertEqual(office_queries("Suite 2, L1, 30-32 Market Street, Sydney NSW 2000", job),
                         ["30-32 Market Street, Sydney NSW 2000", "30 Market Street, Sydney NSW 2000", "Sydney NSW 2000"])
        self.assertEqual(office_queries("Level 5, 1 Denison St, North Sydney", job)[0], "1 Denison St, North Sydney, NSW")
        self.assertEqual(office_in_ad("Macquarie Park", "Join us at our Macquarie Park campus."), "Macquarie Park")
        self.assertEqual(office_in_ad("Parramatta", "Join us in Sydney."), "")  # not in the ad: invented

    def test_sources_api_and_flags(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import Job, SessionLocal
        from backend.offices import set_office

        a, b = self.patched()
        with a, b, TestClient(app) as c:
            ws = c.post("/api/workspaces", json={"name": "Offices"}).json()["id"]
            h = {"X-Workspace": str(ws)}
            c.post("/api/pins", headers=h, json={"label": "Home", "kind": "home", "lat": -33.84, "lng": 151.21, "radius_km": 5})
            db = SessionLocal()
            job = Job(workspace_id=ws, url="https://office.example/1", title="PM", company="Acme", location_text="Sydney NSW",
                      work_mode="hybrid", country="AU", status="to_review", detail_status="full", lat=-33.87, lng=151.21)
            db.add(job); db.commit()
            job_id = job.id
            self.assertTrue([j for j in c.get("/api/jobs", headers=h).json() if j["id"] == job_id][0]["office_unknown"])
            self.assertTrue(set_office(db, job, "Macquarie Park", "company"))
            self.assertEqual((job.lat, job.office_source), (-33.781, "company"))
            self.assertIn("outside your pin radii", job.excluded_reason)  # 7 km from the 5 km home pin
            set_office(db, job, "North Sydney", "ad")  # the ad beats company research
            self.assertEqual((job.office_text, job.excluded_reason), ("North Sydney", None))
            self.assertFalse(set_office(db, job, "Perth", "user"))  # 3,300 km from Sydney: not this job's office
            db.commit(); db.close()

            r = c.put(f"/api/jobs/{job_id}/office", headers=h, json={"office": "Nowhere Street"})
            self.assertEqual(r.status_code, 422)
            r = c.put(f"/api/jobs/{job_id}/office", headers=h, json={"office": "Macquarie Park"}).json()
            self.assertEqual((r["office_source"], r["office_unknown"]), ("user", False))
            self.assertIn("outside your pin radii", r["excluded_reason"])
            r = c.put(f"/api/jobs/{job_id}/office", headers=h, json={"verdict": "ok"}).json()
            self.assertEqual((r["location_verdict"], r["excluded_reason"], r["office_text"]), ("ok", None, "Macquarie Park"))
            r = c.put(f"/api/jobs/{job_id}/office", headers=h, json={"office": None, "verdict": None}).json()
            self.assertEqual((r["office_text"], r["location_verdict"], r["office_unknown"]), (None, None, True))
            self.assertEqual(c.put(f"/api/jobs/{job_id}/office", json={"verdict": "ok"}).status_code, 404)  # other workspace
            c.delete(f"/api/workspaces/{ws}")

    def test_scoring_reads_the_office(self):
        from unittest import mock
        from backend.db import Job, SessionLocal
        import backend.scorer as sc

        a, b = self.patched()
        db = SessionLocal()
        job = Job(workspace_id=1, url="https://office.example/scored", title="PM", company="Acme", location_text="Sydney NSW",
                  work_mode="hybrid", country="AU", status="to_review", detail_status="full",
                  description="You'll be based in our North Sydney office three days a week.")
        db.add(job); db.commit()
        reply = json.dumps({"score": 70, "requirements": [], "gaps": [], "summary": "ok", "office_location": "North Sydney"})
        with a, b, mock.patch.object(sc, "call_model", return_value=reply):
            result = sc.score_one(job.id, "cv", "h", {"model": "m"})
        self.assertNotIn("office_location", result)
        db.expire_all()
        job = db.get(Job, job.id)
        self.assertEqual((job.office_text, job.office_source, job.lat), ("North Sydney", "ad", -33.839))
        self.assertIsNotNone(job.office_checked_at)
        db.close()

    def test_fill_missing_info_finds_offices(self):
        from unittest import mock
        from backend.db import CompanyProfile, Job, SessionLocal
        import backend.offices as of

        a, b = self.patched()
        db = SessionLocal()
        jobs = {name: Job(workspace_id=1, url=f"https://office.example/find/{name}", title="PM", company=name,
                          location_text="Sydney NSW", work_mode="hybrid", country="AU", status="to_review",
                          detail_status="summary", lat=-33.87, lng=151.21)
                for name in ("Solo Office Co", "Hays Recruitment", "Agency Pty")}
        db.add_all(jobs.values()); db.commit()
        ids = [j.id for j in jobs.values()]
        db.close()
        answers = {"Solo Office Co": {"is_recruiter": False, "offices": [{"name": "HQ", "address": "1 Miller St, North Sydney", "url": "u"}]},
                   "Agency Pty": {"is_recruiter": True, "offices": [{"name": "Us", "address": "Macquarie Park", "url": "u"}]}}
        with a, b, mock.patch.object(of, "research_offices", side_effect=lambda company, city, cfg: answers[company]), \
                mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://x/v1", "LLM_MODEL": "m"}):
            out = of.find_offices(1, ids)
        self.assertEqual((out["from_companies"], out["recruiters"], out["unknown"]), (1, 2, 2))
        db = SessionLocal()
        self.assertEqual(db.get(Job, jobs["Solo Office Co"].id).office_source, "company")
        self.assertIsNone(db.get(Job, jobs["Agency Pty"].id).office_text)  # the agency's own office isn't the job's
        self.assertTrue(db.get(CompanyProfile, "agency pty").is_recruiter)
        db.close()


TFNSW_TRIP = {"journeys": [
    {"legs": [
        {"origin": {"disassembledName": "Gosford Station", "departureTimePlanned": "2026-10-05T20:10:00Z"},
         "destination": {"disassembledName": "Central Station", "arrivalTimePlanned": "2026-10-05T21:35:00Z"},
         "transportation": {"disassembledName": "CCN", "name": "Central Coast & Newcastle Line",
                            "product": {"class": 1}, "destination": {"name": "Central"}},
         "duration": 5100, "coords": [[-33.4253, 151.3417], [-33.8833, 151.2061]]},
        {"origin": {"name": "Central Station", "departureTimePlanned": "2026-10-05T21:38:00Z"},
         "destination": {"name": "Office", "arrivalTimePlanned": "2026-10-05T21:50:00Z"},
         "transportation": {"product": {"class": 100}}, "duration": 720, "coords": [[-33.8833, 151.2061], [-33.87, 151.21]]}]},
    {"legs": [
        {"origin": {"disassembledName": "Gosford Station", "departureTimePlanned": "2026-10-05T20:40:00Z"},
         "destination": {"disassembledName": "Office", "arrivalTimePlanned": "2026-10-05T22:20:00Z"},
         "transportation": {"disassembledName": "CCN", "product": {"class": 1}}, "duration": 6000, "coords": []}]},
]}


class CommuteTest(unittest.TestCase):
    def test_peak_day_and_journeys(self):
        from datetime import date, datetime
        from backend.commute import TZ, best_journey, peak_day
        self.assertEqual(peak_day(date(2026, 10, 1)), date(2026, 10, 6))  # Thursday -> next Tuesday
        self.assertEqual(peak_day(date(2026, 10, 6)), date(2026, 10, 13))
        best = best_journey(TFNSW_TRIP, datetime(2026, 10, 6, 9, 0, tzinfo=TZ))
        self.assertEqual((best["depart"], best["arrive"], best["minutes"], best["changes"]), ("07:10", "08:50", 100, 0))
        self.assertEqual(best["summary"], "Train CCN")
        self.assertEqual([l["mode"] for l in best["legs"]], ["Train", "Walk"])
        later = best_journey(TFNSW_TRIP)  # leaving after a time: the first to arrive
        self.assertEqual(later["arrive"], "08:50")

    def test_commute_endpoint(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.db import Job, SessionLocal
        import backend.commute as cm

        def fake_get(url, params, headers=None, what=""):
            if "stop_finder" in url:
                return {"locations": [{"type": "stop", "isBest": True, "id": "2250", "disassembledName": "Gosford Station",
                                       "coord": [-33.4253, 151.3417]}]}
            if url.endswith("trip"):
                self.assertEqual(headers["Authorization"], "apikey t-key")
                return TFNSW_TRIP
            return {"routes": [{"duration": 3960, "distance": 73200,
                                "geometry": {"coordinates": [[151.34, -33.42], [151.21, -33.87]]}}]}
        with TestClient(app) as c, mock.patch.object(cm, "_get", side_effect=fake_get), \
                mock.patch.dict(os.environ, {"TFNSW_API_KEY": "t-key", "TOMTOM_API_KEY": ""}):
            ws = c.post("/api/workspaces", json={"name": "Commute"}).json()["id"]
            h = {"X-Workspace": str(ws)}
            prof = c.get("/api/profile", headers=h).json()
            self.assertEqual(c.put("/api/profile", headers=h, json={**prof, "commute_arrive_by": "9am"}).status_code, 422)
            c.put("/api/profile", headers=h, json={**prof, "commute_from": ["Gosford Station"], "commute_arrive_by": "08:30"})
            db = SessionLocal()
            job = Job(workspace_id=ws, url="https://commute.example/1", title="PM", company="Acme", location_text="Sydney NSW",
                      work_mode="hybrid", status="to_review", detail_status="full", lat=-33.87, lng=151.21)
            db.add(job); db.commit()
            job_id = job.id
            db.close()
            self.assertEqual(c.get(f"/api/jobs/{job_id}/commute", headers=h).status_code, 409)  # no home pin yet
            c.post("/api/pins", headers=h, json={"label": "Home", "kind": "onsite", "lat": -33.39, "lng": 151.34, "radius_km": 10})
            r = c.get(f"/api/jobs/{job_id}/commute", headers=h)
            self.assertEqual(r.status_code, 200, r.text)
            out = r.json()
            self.assertEqual((out["arrive_by"], out["from_stations"], out["office"]["precise"]), ("08:30", True, False))
            self.assertEqual(out["transit"][0]["from"], "Gosford Station")
            self.assertEqual(out["car"]["there"]["minutes"], 66)
            self.assertFalse(out["car"]["there"]["traffic"])
            self.assertTrue(any("only a city" in n for n in out["notes"]))
            self.assertEqual(c.get("/api/commute/status").json(), {"transit": True, "traffic": False})
            c.delete(f"/api/workspaces/{ws}")


class ResearchOfficesTest(unittest.TestCase):
    def test_research_offices_verified(self):
        from backend.research import extract, parse_profile
        profile = {"official_name": "Acme", "is_company": True, "controversies": [], "sources": [],
                   "offices": [{"name": "Sydney office", "address": "1 Miller St, North Sydney NSW 2060",
                                "source_url": "https://acme.example/contact"},
                               {"name": "Guess", "address": "9 Nowhere Rd, Parramatta NSW", "source_url": "https://made.up/x"}]}
        response = {"output": [
            {"type": "web_search_call", "results": [{"title": "Contact", "url": "https://acme.example/contact"}]},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(profile), "annotations": []}]}]}
        p, dropped = parse_profile(*extract(response))
        self.assertEqual([o["address"] for o in p["offices"]], ["1 Miller St, North Sydney NSW 2060"])
        self.assertEqual(dropped, 1)

    def test_closest_office_unless_the_ad_names_one(self):
        from unittest import mock
        from backend.db import CompanyProfile, Job, Pin, SessionLocal, Workspace
        import backend.offices as of
        import backend.pipeline as pl

        places = {"North Sydney": (-33.839, 151.207), "Macquarie Park": (-33.781, 151.126), "Sydney NSW": (-33.87, 151.21),
                  "Parramatta": (-33.815, 151.003)}
        fake = _fake_geocode(places)
        db = SessionLocal()
        ws = Workspace(name="Closest")
        db.add(ws); db.commit()
        # Commute pins: home and a station near North Sydney.
        db.add_all([Pin(workspace_id=ws.id, label="Home", kind="onsite", lat=-33.42, lng=151.34, radius_km=10),
                    Pin(workspace_id=ws.id, label="Station", kind="hybrid", lat=-33.840, lng=151.206, radius_km=1)])
        db.merge(CompanyProfile(key="multi office co", name="Multi Office Co", status="done", offices_json=json.dumps([
            {"name": "West", "address": "Parramatta", "url": "u"},
            {"name": "North", "address": "North Sydney", "url": "u"},
            {"name": "Park", "address": "Macquarie Park", "url": "u"}])))
        jobs = [Job(workspace_id=ws.id, url=f"https://closest.example/{i}", title="PM", company="Multi Office Co",
                    location_text="Sydney NSW", work_mode="hybrid", country="AU", status="to_review", detail_status="full",
                    lat=-33.87, lng=151.21) for i in range(2)]
        db.add_all(jobs); db.commit()
        with mock.patch.object(of, "geocode", side_effect=fake), mock.patch.object(pl, "geocode", side_effect=fake):
            of.set_office(db, jobs[1], "Parramatta", "ad")  # the ad names the site
            db.commit()
            self.assertEqual(of.assign_company(ws.id, "Multi Office Co"), 1)
        db.expire_all()
        self.assertEqual((db.get(Job, jobs[0].id).office_text, db.get(Job, jobs[0].id).office_source), ("North Sydney", "company"))
        self.assertEqual(db.get(Job, jobs[1].id).office_text, "Parramatta")
        db.delete(db.get(Workspace, ws.id)); db.commit(); db.close()


class ScheduleTest(unittest.TestCase):
    def test_preset_slots_and_holidays(self):
        from datetime import datetime, timezone
        from backend.schedule import ScheduleSchema, due_slot, next_run, skipped_holidays

        s = ScheduleSchema()
        self.assertEqual((s.times, s.days, s.state), (["10:00", "12:00", "14:00", "16:00"], [0, 1, 2, 3, 4], "NSW"))
        utc = timezone.utc
        # Fri 2 Oct 2026 16:30 Sydney: Mon 5 Oct is Labour Day, and daylight saving starts on the 4th.
        fri = datetime(2026, 10, 2, 6, 30, tzinfo=utc)
        self.assertEqual(next_run(s, fri).isoformat(), "2026-10-06T10:00:00+11:00")
        self.assertEqual(next_run(s.model_copy(update={"skip_holidays": False}), fri).isoformat(), "2026-10-05T10:00:00+11:00")
        # Christmas (Fri) and the Boxing Day substitute (Mon 28 Dec) are skipped.
        self.assertEqual(next_run(s, datetime(2026, 12, 24, 6, 0, tzinfo=utc)).isoformat(), "2026-12-29T10:00:00+11:00")
        self.assertEqual([h["date"] for h in skipped_holidays(s, fri.date())], ["2026-10-05", "2026-12-25", "2026-12-28"])
        # Times are in the state's time zone: Perth is 3 hours behind Sydney in summer.
        self.assertEqual(next_run(s.model_copy(update={"state": "WA"}), datetime(2026, 10, 6, 0, 0, tzinfo=utc)).isoformat(),
                         "2026-10-06T10:00:00+08:00")

        slot = due_slot(s, datetime(2026, 10, 2, 0, 20, tzinfo=utc), None)  # 10:20 Sydney
        self.assertEqual(slot.isoformat(), "2026-10-02T10:00:00+10:00")
        self.assertIsNone(due_slot(s, datetime(2026, 10, 2, 0, 20, tzinfo=utc), slot))  # already handled
        self.assertIsNone(due_slot(s, datetime(2026, 10, 2, 1, 30, tzinfo=utc), None))  # 11:30: over an hour late
        self.assertIsNone(due_slot(s, datetime(2026, 10, 3, 0, 20, tzinfo=utc), None))  # Saturday
        self.assertIsNone(due_slot(s, datetime(2026, 10, 4, 23, 20, tzinfo=utc), None))  # Labour Day 10:20
        self.assertIsNone(due_slot(s.model_copy(update={"enabled": False}), datetime(2026, 10, 2, 0, 20, tzinfo=utc), None))

    def test_tick_runs_each_slot_once(self):
        from datetime import datetime, timedelta, timezone
        from unittest import mock
        from backend import schedule
        from backend.db import CompanySource, SearchRun, SearchSchedule, SessionLocal, UserProfile, Workspace

        db = SessionLocal()
        ws = Workspace(name="Scheduled")
        db.add(ws); db.commit()
        db.add(UserProfile(id=ws.id, titles=["Product Manager"], seek_enabled=True)); db.commit()
        ws_id = ws.id
        db.close()
        started: list[int] = []

        def fake_start(w):
            started.append(w)
            return {"id": 1000 + len(started)}

        def tick(now):
            # Only the new workspace counts here; the others belong to other tests.
            with mock.patch.object(schedule, "nothing_to_search", side_effect=lambda d, w: w != ws_id):
                return schedule.tick(fake_start, now)

        ten20 = datetime(2026, 10, 2, 0, 20, tzinfo=timezone.utc)  # Fri 10:20 Sydney
        try:
            with mock.patch.object(schedule.tasks, "active_run", return_value={"id": 1}):
                self.assertIsNone(tick(ten20))  # waits for the busy worker
            self.assertEqual(started, [])
            self.assertEqual(tick(ten20)["id"], 1001)
            self.assertIsNone(tick(ten20 + timedelta(minutes=5)))  # 10:00 slot handled
            self.assertEqual(started, [ws_id])
            # Run search clicked at 12:01: the 12:00 slot is passed over.
            db = SessionLocal()
            db.add(SearchRun(workspace_id=ws_id, kind="search", state="done",
                             started_at=datetime(2026, 10, 2, 2, 1)))
            db.commit(); db.close()
            self.assertIsNone(tick(datetime(2026, 10, 2, 2, 10, tzinfo=timezone.utc)))
            self.assertEqual(started, [ws_id])
            self.assertEqual(tick(datetime(2026, 10, 2, 4, 0, 30, tzinfo=timezone.utc))["id"], 1002)  # 14:00
            db = SessionLocal()
            self.assertEqual(db.get(SearchSchedule, ws_id).last_slot, datetime(2026, 10, 2, 4, 0))
            db.close()
        finally:
            db = SessionLocal()
            db.delete(db.get(UserProfile, ws_id)); db.delete(db.get(Workspace, ws_id)); db.commit()
            self.assertIsNone(db.get(SearchSchedule, ws_id))  # cascades with the workspace
            db.close()

        # Nothing to search: SEEK without titles and no company sources.
        db = SessionLocal()
        ws = Workspace(name="Empty"); db.add(ws); db.commit()
        self.assertTrue(schedule.nothing_to_search(db, ws.id))
        db.add(CompanySource(workspace_id=ws.id, label="Acme", careers_url="https://acme.example/careers", enabled=True))
        db.commit()
        self.assertFalse(schedule.nothing_to_search(db, ws.id))
        db.delete(ws); db.commit(); db.close()

    def test_api(self):
        from datetime import datetime, timezone
        from unittest import mock
        from fastapi.testclient import TestClient
        from backend import schedule
        from backend.app import app
        from backend.db import SearchSchedule, SessionLocal

        with TestClient(app) as c:
            ws = c.post("/api/workspaces", json={"name": "Schedule API"}).json()["id"]
            h = {"X-Workspace": str(ws)}
            out = c.get("/api/schedule", headers=h).json()
            self.assertTrue(out["enabled"] and out["skip_holidays"])
            self.assertEqual((out["times"], out["days"], out["state"], out["timezone"]),
                             (["10:00", "12:00", "14:00", "16:00"], [0, 1, 2, 3, 4], "NSW", "Australia/Sydney"))
            self.assertEqual(len(out["states"]), 8)
            self.assertTrue(out["nothing_to_search"])
            self.assertIsNone(out["last_run"])
            self.assertEqual(c.put("/api/schedule", headers=h, json={**out, "times": ["9am"]}).status_code, 422)
            self.assertEqual(c.put("/api/schedule", headers=h, json={**out, "days": [7]}).status_code, 422)
            self.assertEqual(c.put("/api/schedule", headers=h, json={**out, "state": "XX"}).status_code, 422)

            # Switching the schedule on at Fri 10:30 Sydney doesn't run the 10:00 search there and then.
            off = c.put("/api/schedule", headers=h, json={"enabled": False}).json()
            self.assertIsNone(off["next_run"])
            ten30 = datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc)
            with mock.patch.object(schedule, "datetime", wraps=datetime) as dt:
                dt.now.return_value = ten30
                out = c.put("/api/schedule", headers=h, json={"enabled": True, "times": ["16:00", "10:00", "10:00"],
                                                              "days": [4, 0, 4], "state": "VIC"}).json()
            self.assertEqual((out["times"], out["days"], out["timezone"]), (["10:00", "16:00"], [0, 4], "Australia/Melbourne"))
            db = SessionLocal()
            self.assertEqual(db.get(SearchSchedule, ws).last_slot, datetime(2026, 10, 2, 0, 0))
            db.close()
            self.assertIsNone(schedule.due_slot(schedule.ScheduleSchema(**out), ten30,
                                                datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)))

            copy = c.post("/api/workspaces", json={"name": "Schedule copy", "copy_from": ws}).json()["id"]
            self.assertEqual(c.get("/api/schedule", headers={"X-Workspace": str(copy)}).json()["times"], ["10:00", "16:00"])
            for w in (copy, ws):
                c.delete(f"/api/workspaces/{w}")

