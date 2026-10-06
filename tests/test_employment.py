"""Tests for the employment type field: normaliser, adapters, pipeline, scorer fallback and API. No network.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("JOBHUNT_DB_PATH", os.path.join(_TMP, "test.db"))  # never the real database
os.environ.setdefault("JOBHUNT_SCHEDULER", "off")
os.environ.setdefault("JOBHUNT_ENV_PATH", os.path.join(_TMP, "test.env"))

from tests import helpers  # noqa: E402  (signed-in clients; sets the test environment)

import backend.scorer as sc  # noqa: E402
from backend.adapters.ats import AshbyAdapter, LeverAdapter, SmartRecruitersAdapter, WorkableAdapter, WorkdayAdapter  # noqa: E402
from backend.adapters.base import Posting  # noqa: E402
from backend.adapters.generic import parse_detail  # noqa: E402
from backend.adapters.seek import parse_search_page  # noqa: E402
from backend.db import Job, SessionLocal, init_db  # noqa: E402
from backend.employment import EMPLOYMENT_TYPES, normalise_employment_type as norm  # noqa: E402
from backend.pool import merge_posting  # noqa: E402

init_db()


class NormaliserTest(unittest.TestCase):
    def test_every_vocabulary_value_maps_to_itself(self):
        for value in EMPLOYMENT_TYPES:
            self.assertEqual(norm(value), value)
            self.assertEqual(norm(value.upper()), value)
            self.assertEqual(norm(value.replace("_", " ")), value)

    def test_common_spellings(self):
        cases = {
            "Full time": "full_time", "Full-time": "full_time", "FULL_TIME": "full_time", "fulltime": "full_time",
            "Part time": "part_time", "Part-Time": "part_time", "PART_TIME": "part_time", "parttime": "part_time",
            "Contract": "contract", "CONTRACTOR": "contract", "Contract/Temp": "contract", "Freelance": "contract",
            "Fixed-term": "contract", "Casual": "casual", "Casual/Vacation": "casual",
            "TEMPORARY": "temporary", "Temp": "temporary", "Seasonal": "temporary",
            "INTERN": "internship", "Internship": "internship", "Interns": "internship", "Trainee": "internship",
        }
        for raw, expected in cases.items():
            self.assertEqual(norm(raw), expected, raw)

    def test_lists_and_dicts(self):
        self.assertEqual(norm(["PART_TIME", "CONTRACTOR"]), "part_time")
        self.assertEqual(norm(["OTHER", "FULL_TIME"]), "full_time")
        self.assertEqual(norm({"id": "permanent", "label": "Full-time"}), "full_time")
        self.assertEqual(norm({"id": "contract"}), "contract")

    def test_junk_is_none(self):
        for junk in (None, "", "   ", "OTHER", "Permanent", "Ongoing", 42, 3.5, True, [], {}, ["", None, "x"], object()):
            self.assertIsNone(norm(junk), junk)


class AdapterFieldTest(unittest.TestCase):
    def test_seek_search_result(self):
        def page(work_types):
            job = {"id": "1", "title": "PM", "workTypes": work_types}
            blob = json.dumps({"results": {"totalPages": 1, "results": {"jobs": [job]}}})
            return f"<html><script>\n    window.SEEK_REDUX_DATA = {blob};\n</script></html>"

        for labels, expected in ((["Full time"], "full_time"), (["Part time"], "part_time"), (["Contract/Temp"], "contract"),
                                 (["Casual/Vacation"], "casual"), ([], None)):
            postings, _ = parse_search_page(page(labels))
            self.assertEqual(postings[0].employment_type, expected, labels)

    def test_apsjobs(self):
        from backend.adapters.apsjobs import to_posting
        self.assertEqual(to_posting({"jobId": "a1", "jobType": "Full-Time"}).employment_type, "full_time")
        self.assertEqual(to_posting({"jobId": "a1", "jobType": "Part-Time"}).employment_type, "part_time")
        self.assertIsNone(to_posting({"jobId": "a1"}).employment_type)

    def test_generic_jsonld(self):
        body = "Own the program roadmap and stakeholder reporting. " * 8
        for raw, expected in (("FULL_TIME", "full_time"), (["PART_TIME", "CONTRACTOR"], "part_time"),
                              ("CONTRACTOR", "contract"), ("TEMPORARY", "temporary"), ("INTERN", "internship"),
                              ("OTHER", None), (None, None)):
            ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Program Manager",
                  "description": f"<p>{body}</p>", "hiringOrganization": {"name": "Acme"}}
            if raw is not None:
                ld["employmentType"] = raw
            html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
            p = parse_detail(html, Posting(url="https://acme.com/careers/pm-1", title="stub"))
            self.assertEqual(p.employment_type, expected, raw)

    def test_jsonld_keeps_a_stub_value_when_the_page_has_none(self):
        body = "Own the program roadmap and stakeholder reporting. " * 8
        ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": "PM", "description": f"<p>{body}</p>"}
        html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        p = parse_detail(html, Posting(url="https://acme.com/careers/pm-1", title="stub", employment_type="casual"))
        self.assertEqual(p.employment_type, "casual")

    def _client(self, *payloads):
        client = mock.MagicMock()
        responses = []
        for data in payloads:
            r = mock.MagicMock()
            r.json.return_value = data
            responses.append(r)
        client.get.side_effect = responses
        client.post_json.side_effect = responses
        return client

    def test_lever(self):
        job = {"id": "1", "text": "PM", "hostedUrl": "https://jobs.lever.co/acme/1", "categories": {"commitment": "Part time"},
               "descriptionPlain": "x"}
        other = {**job, "id": "2", "hostedUrl": "https://jobs.lever.co/acme/2", "categories": {}}
        out = LeverAdapter(self._client([job, other]), "acme").list_postings()
        self.assertEqual([p.employment_type for p in out], ["part_time", None])

    def test_ashby(self):
        data = {"jobs": [{"id": "1", "title": "PM", "jobUrl": "https://jobs.ashbyhq.com/a/1", "employmentType": "FullTime"},
                         {"id": "2", "title": "PM", "jobUrl": "https://jobs.ashbyhq.com/a/2", "employmentType": "Intern"},
                         {"id": "3", "title": "PM", "jobUrl": "https://jobs.ashbyhq.com/a/3"}]}
        out = AshbyAdapter(self._client(data), "a").list_postings()
        self.assertEqual([p.employment_type for p in out], ["full_time", "internship", None])

    def test_smartrecruiters(self):
        data = {"totalFound": 2, "content": [
            {"id": "1", "name": "PM", "typeOfEmployment": {"id": "permanent", "label": "Full-time"}},
            {"id": "2", "name": "PM"}]}
        out = SmartRecruitersAdapter(self._client(data), "acme").list_postings()
        self.assertEqual([p.employment_type for p in out], ["full_time", None])

    def test_workable(self):
        data = {"results": [{"shortcode": "AB1", "title": "PM", "employment_type": "Part-time"},
                            {"shortcode": "AB2", "title": "PM"}]}
        out = WorkableAdapter(self._client(data), "acme").list_postings()
        self.assertEqual([p.employment_type for p in out], ["part_time", None])

    def test_workday_detail(self):
        adapter = WorkdayAdapter(self._client({"jobPostingInfo": {"jobDescription": "<p>x</p>", "timeType": "Full time"}}),
                                 "acme.wd3.myworkdayjobs.com", "acme", "site")
        p = adapter.fetch_detail(Posting(url="u", title="t", detail_ref="/job/x_R1"))
        self.assertEqual(p.employment_type, "full_time")
        adapter = WorkdayAdapter(self._client({"jobPostingInfo": {"jobDescription": "<p>x</p>"}}),
                                 "acme.wd3.myworkdayjobs.com", "acme", "site")
        p = adapter.fetch_detail(Posting(url="u", title="t", detail_ref="/job/x_R1", employment_type="contract"))
        self.assertEqual(p.employment_type, "contract")


class PipelineTest(unittest.TestCase):
    def test_apply_posting_stores_and_keeps(self):
        from datetime import datetime

        job = Job()
        merge_posting(job, Posting(url="u", title="PM", employment_type="contract"), datetime.utcnow())
        self.assertEqual(job.employment_type, "contract")
        merge_posting(job, Posting(url="u", title="PM"), datetime.utcnow())  # a later pass without the field keeps it
        self.assertEqual(job.employment_type, "contract")
        merge_posting(job, Posting(url="u", title="PM", employment_type="full_time"), datetime.utcnow())
        self.assertEqual(job.employment_type, "full_time")


class ScorerTest(unittest.TestCase):
    def test_parse_result(self):
        def parse(value):
            obj = {"score": 50}
            if value is not ...:
                obj["employment_type"] = value
            return sc.parse_result(json.dumps(obj))["employment_type"]

        self.assertEqual(parse("part_time"), "part_time")
        self.assertEqual(parse("Full time"), "full_time")
        self.assertIsNone(parse("permanent"))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse(None))
        self.assertIsNone(parse(["x"]))
        self.assertIsNone(parse(...))  # key missing

    def _score(self, existing, from_model):
        db = SessionLocal()
        job = Job(workspace_id=1, url=f"https://emp.example/{existing}/{from_model}", title="PM", company="Acme",
                  status="to_review", detail_status="summary", employment_type=existing)
        db.add(job); db.commit()
        job_id = job.id
        db.close()
        reply = json.dumps({"score": 60, "requirements": [], "gaps": [], "summary": "ok", "employment_type": from_model})
        with mock.patch.object(sc, "call_model", return_value=reply):
            result = sc.score_one(job_id, "cv", "h", {"model": "m"})
        self.assertNotIn("employment_type", result)  # not kept in the stored fit result
        db = SessionLocal()
        value = db.get(Job, job_id).employment_type
        db.close()
        return value

    def test_fallback_fills_only_when_unknown(self):
        self.assertEqual(self._score(None, "contract"), "contract")
        self.assertIsNone(self._score(None, ""))
        self.assertIsNone(self._score(None, "weekly"))

    def test_adapter_data_is_not_overwritten(self):
        self.assertEqual(self._score("full_time", "contract"), "full_time")


class ApiTest(unittest.TestCase):
    def test_field_in_list_and_detail(self):
        db = SessionLocal()
        db.add_all([Job(workspace_id=1, url="https://emp.example/api/1", title="With type", status="to_review",
                        detail_status="full", employment_type="casual"),
                    Job(workspace_id=1, url="https://emp.example/api/2", title="Without type", status="to_review",
                        detail_status="full")])
        db.commit(); db.close()
        with helpers.client() as c:
            jobs = {j["title"]: j for j in c.get("/api/jobs").json()}
            self.assertEqual(jobs["With type"]["employment_type"], "casual")
            self.assertIn("employment_type", jobs["Without type"])
            self.assertIsNone(jobs["Without type"]["employment_type"])
            detail = c.get(f"/api/jobs/{jobs['With type']['id']}").json()
            self.assertEqual(detail["employment_type"], "casual")

    def test_older_database_gains_the_column(self):
        from sqlalchemy import inspect
        from backend.db import engine
        self.assertIn("employment_type", {c["name"] for c in inspect(engine).get_columns("jobs")})


if __name__ == "__main__":
    unittest.main()
