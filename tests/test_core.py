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
