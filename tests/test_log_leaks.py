"""No log line may hold anything of a person's (accounts design §2.5: "Logs must never contain plaintext"; Phase L).

One account goes through the whole app, end to end, with the network and the AI service mocked: sign-up, the search
profile, documents, a search (mocked sources returning planted ads), scoring, chat, a cover letter, company research,
the browser extension, a data export, a scheduled search, and a password reset with a recovery code. Every personal
field holds a planted marker (``ZQX``), and every secret the run makes (passwords, recovery codes, session and CSRF
tokens, extension keys) is collected. Logging is captured at DEBUG for every logger, with the
text of each record formatted the way a log file would show it, tracebacks included, and then none of the markers or
secrets may be in it. The test also checks that the markers really are in the person's data (so a pass isn't just an
empty run) and that the log isn't empty.

Run:  python3 -m unittest tests.test_log_leaks -v
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import time
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402

from backend import app as app_module, ratelimit, users  # noqa: E402
from backend.adapters.base import Adapter, Posting  # noqa: E402
from backend.db import SessionLocal, User, Workspace  # noqa: E402

MARK = "ZQX"
PASSWORD = "zqx-first-password-1"
NEW_PASSWORD = "zqx-second-password-2"
LLM_ENV = {"LLM_API_KEY": "zqx-llm-key-never-logged", "LLM_BASE_URL": "https://api.openai.com/v1", "LLM_MODEL": "m",
           "LLM_REASONING_EFFORT": ""}
NOW = datetime.now(timezone.utc)

SCORE_REPLY = json.dumps({
    "score": 81, "summary": "ZQX fit summary: strong delivery background.", "gaps": ["ZQX gap: no cloud"],
    "requirements": [{"point": "ZQX requirement", "matched": True, "must_have": True,
                      "evidence": [{"bullet": "ZQX evidence from the CV", "sub_bullets": []}]}]})
RESEARCH_REPLY = json.dumps({
    "official_name": "ZQX Corp Pty Ltd", "is_company": True, "is_recruiter": False,
    "business_model": "ZQX sells delivery software.", "ownership": "Private.", "headquarters": "Zqxtown, Australia",
    "controversies": [], "controversy_note": "ZQX note.",
    "sources": [{"title": "About ZQX", "url": "https://zqx-corp.example/about"}]})


def _tiny_pdf(text: str) -> bytes:
    """A one-page PDF with ``text`` (no library needed), enough for pypdf to read it back."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)


def _docx(text: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class _Response:
    """What ``httpx.post`` gives back, as far as the AI service code reads it."""

    def __init__(self, status: int, data: dict):
        self.status_code, self._data, self.headers = status, data, {}
        self.text = json.dumps(data)

    def json(self):
        return self._data


class _Source(Adapter):
    """A source with planted ads, listed and detailed from memory."""

    source, method = "greenhouse:zqx-corp", "test"

    def __init__(self, postings: list[Posting]):
        self.postings = postings

    failure: Exception | None = None

    def list_postings(self):
        if self.failure:
            raise self.failure
        return [replace(p) for p in self.postings]

    def fetch_detail(self, posting):
        return replace(posting, description=posting.description + " " + MARK + " the full advertisement.",
                       detail_status="full")


def _ad(n: int, **fields) -> Posting:
    base = dict(url=f"https://jobs.zqx.example/{n}", title="ZQX Delivery Lead", company="ZQX Corp", external_id=str(n),
                location_text="Zqxtown NSW", country="AU", description="ZQX include: lead the delivery program.")
    return Posting(**{**base, **fields})


class Everything(logging.Handler):
    """Every record, as a log file would show it (traceback included)."""

    def __init__(self):
        super().__init__(logging.NOTSET)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record):
        try:
            line = self.format(record)
        except Exception as exc:  # noqa: BLE001  (a bad format string is a bug of its own)
            line = f"{record.name}: could not format ({exc!r}) {record.msg!r}"
        if record.name.startswith(("httpx", "httpcore")) and "testserver" in line:
            return  # the test client's own request lines, not the app's
        self.lines.append(line)


@contextlib.contextmanager
def capture_all():
    """DEBUG for the root and every logger that exists (SQLAlchemy, multipart and the rest included), one handler on
    all of them, and what is printed or written to stderr. Yields (the handler, the printed text). The loggers the
    app deliberately holds quiet (app.QUIET_LOGGERS: httpx, the Claude SDK, pypdf) keep that level: it is what
    protects a person whatever the root logger is set to, and ``test_the_quiet_loggers_stay_quiet`` checks it."""
    handler, printed = Everything(), io.StringIO()
    root = logging.getLogger()
    quiet = tuple(app_module.QUIET_LOGGERS)
    named = [lg for name, lg in list(logging.Logger.manager.loggerDict.items()) if isinstance(lg, logging.Logger)
             and not any(name == q or name.startswith(q + ".") for q in quiet)]
    saved = [(lg, lg.level, lg.propagate) for lg in [root, *named]]
    for lg in [root, *named]:
        lg.setLevel(logging.DEBUG)
    root.addHandler(handler)
    for lg in named:
        if not lg.propagate:
            lg.addHandler(handler)
    try:
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            yield handler, printed
    finally:
        root.removeHandler(handler)
        for lg in named:
            lg.removeHandler(handler)
        for lg, level, propagate in saved:
            lg.setLevel(level)


class FullRun(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)
        patches = [
            mock.patch.dict(os.environ, LLM_ENV),
            mock.patch("backend.pipeline.source_adapter", side_effect=lambda *a, **k: self.source),
            mock.patch("backend.llm_providers.httpx.post", side_effect=self.llm),
            mock.patch("backend.geo.httpx.get", side_effect=self.nominatim),
            mock.patch("backend.geo.time.sleep"),
            mock.patch("backend.research.run_response", side_effect=self.web_search),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.source = _Source([])
        self.secrets: dict[str, str] = {}
        self.llm_calls = 0
        self.llm_status = 200  # an error status: the provider answers it, echoing the request

    # -- clients --------------------------------------------------------------------------------------------------

    def anonymous(self, **kwargs) -> TestClient:
        c = TestClient(app_module.app, **kwargs)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def signed_in(self) -> helpers.SignedInClient:
        c = helpers.client()
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    # -- the outside world, mocked ------------------------------------------------------------------------------

    def llm(self, url, json=None, headers=None, timeout=None, **_):
        self.llm_calls += 1
        prompt = " ".join(str(m.get("content")) for m in (json or {}).get("messages", []))
        if self.llm_status != 200:
            return _Response(self.llm_status, {"error": {"message": f"rejected, you sent: {prompt[:200]}"}})
        if "ZQXBOOM" in prompt and "response_format" in (json or {}):  # a provider that echoes the request in its error
            return _Response(400, {"error": {"message": f"bad request, you sent: {prompt[:200]}"}})
        text = SCORE_REPLY if "response_format" in (json or {}) else (
            "Dear hiring team,\n\nZQX answer: I led delivery programs.\n\nZqx Quillon")
        return _Response(200, {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    def nominatim(self, url, params=None, **_):
        import httpx

        if "Zqxfail" in (params or {}).get("q", ""):
            raise httpx.ConnectError(f"cannot reach the geocoder for {params['q']}")
        hits = [{"lat": "-33.8", "lon": "151.2", "importance": 0.5, "addresstype": "suburb", "place_rank": 20,
                 "display_name": "Zqxtown, New South Wales", "address": {"country_code": "au"}}]
        return mock.Mock(json=lambda: hits, raise_for_status=lambda: None)

    def web_search(self, payload, cfg, what="web search"):
        return {"id": "resp_1", "status": "completed", "output": [
            {"type": "web_search_call", "status": "completed", "results": [
                {"type": "text_result", "title": "About ZQX", "url": "https://zqx-corp.example/about"}]},
            {"type": "message", "content": [{"type": "output_text", "text": RESEARCH_REPLY, "annotations": []}]}]}

    # -- the person ---------------------------------------------------------------------------------------------

    def signed_up(self):
        """An account whose sign-in name is personal text (the app makes no second account itself: tests/helpers.py
        writes it as an older install's would be), signed in through the app."""
        email = "Zqx Quillon zqxowner.private@example.invalid"
        account = helpers.extra_account(email, PASSWORD)
        c = self.anonymous()
        r = c.post("/api/auth/login", json={"name": email, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.secrets.update(password=PASSWORD, csrf=r.json()["csrf"], session=c.cookies.get("jh_session") or "",
                            device=c.cookies.get("jh_device") or "",
                            **{f"recovery_code_{i}": code for i, code in enumerate(account.recovery_codes)})
        self.email = email
        return c

    def the_install_settings_and_a_new_name(self, c):
        """The name changed in Settings, and the AI key, the commute key and the public address saved there: none of
        these values may reach the log (only the names of the settings that changed)."""
        from backend import config
        env_file = config.ENV_PATH
        saved = env_file.read_text() if env_file.exists() else None
        self.addCleanup(lambda: env_file.write_text(saved) if saved is not None else env_file.unlink(missing_ok=True))
        new_name = "Zqx Quillon Renamed zqxowner"
        r = c.put("/api/auth/name", json={"name": new_name})
        self.assertEqual(r.status_code, 200, r.text)
        self.email = new_name
        helpers._passwords[new_name] = PASSWORD
        llm = {k: os.environ.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")}
        key = "sk-zqx-secret-ai-key-000111"
        r = c.put("/api/settings/llm", json={"base_url": "https://zqx-llm.example/v1", "model": "zqx-model",
                                              "api_key": key})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.put("/api/settings/keys", json={"tomtom": "zqxtomtomkey123"}).status_code, 200)
        self.assertEqual(c.put("/api/settings/address", json={"public_base_url": "https://zqx-host.example",
                                                               "allowed_hosts": ["zqx-alias.example"]}).status_code, 200)
        self.assertEqual(c.put("/api/settings/address", json={"public_base_url": "", "allowed_hosts": []}).status_code,
                         200)
        self.assertEqual(c.put("/api/settings/keys", json={"tomtom": ""}).status_code, 200)
        r = c.put("/api/settings/llm", json={"base_url": llm["LLM_BASE_URL"] or "", "model": llm["LLM_MODEL"] or "",
                                              "api_key": llm["LLM_API_KEY"] or ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.secrets.update(ai_key=key, tomtom_key="zqxtomtomkey123", host="zqx-host", alias="zqx-alias")

    def run_to_end(self, c, run: dict) -> dict:
        for _ in range(600):
            got = c.get(f"/api/runs/{run['id']}").json()
            if got["state"] not in ("queued", "running"):
                return got
            time.sleep(0.05)
        self.fail("the run didn't finish")

    def test_nothing_personal_reaches_a_log(self):
        notice = []
        with capture_all() as (log, printed):
            logging.getLogger("test").info("the capture works")
            c = self.signed_up()
            self.the_install_settings_and_a_new_name(c)
            self.sign_up_and_set_up(c)
            ads = self.search_and_score(c)
            self.talk_to_the_ai(c, ads)
            self.browser_extension(c)
            self.when_things_fail(c)
            self.with_claude(c)
            self.export(c)
            self.scheduled_search_and_recovery(c, notice)
        everything = "\n".join(log.lines + [printed.getvalue()])
        self.assertGreater(len(log.lines), 100, "the capture saw almost nothing")
        self.assertIn("the capture works", everything)
        planted = {"marker": MARK.lower(), "email": "zqxowner", "name": "quillon",
                   **{k: v.lower() for k, v in self.secrets.items() if v}}
        for what, value in planted.items():
            found = [line[:300] for line in log.lines if value in line.lower()]
            self.assertEqual(found, [], f"{what} ({value!r}) is in the log")
            self.assertNotIn(value, printed.getvalue().lower(), f"{what} was printed")
        self.assertTrue(notice, "the steps didn't all run")

    # -- the steps ----------------------------------------------------------------------------------------------

    def sign_up_and_set_up(self, c):
        # The search profile, a pin, a source and documents.
        self.assertEqual(c.put("/api/profile", json={
            "titles": ["ZQX Delivery Lead"], "keywords_include": ["zqx include"], "keywords_exclude": ["zqx exclude"],
            "seek_enabled": False, "seek_locations": ["Zqxtown NSW"], "salary_floor": 100000}).status_code, 200)
        self.assertEqual(c.put("/api/dealbreakers", json={"industries": ["zqx industry"],
                                                           "keywords": ["zqx dealbreaker"]}).status_code, 200)
        self.assertEqual(c.post("/api/pins", json={"label": "ZQX home", "kind": "home", "lat": -33.8, "lng": 151.2,
                                                   "radius_km": 20}).status_code, 201)
        self.assertEqual(c.post("/api/sources", json={"url": "https://careers.zqx.example/jobs",
                                                      "label": "ZQX dream employer"}).status_code, 201)
        r = c.post("/api/documents/notes", json={"title": "ZQX project notes", "text": "ZQX note: I ran the ZQX launch.",
                                                 "kind": "other"})
        self.assertEqual(r.status_code, 201, r.text)
        for name, data, kind in (("ZQX-cv.docx", _docx("Zqx Quillon. ZQX resume: delivery lead at ZQX Corp."), "resume"),
                                 ("ZQX-letter.pdf", _tiny_pdf("ZQX earlier cover letter"), "cover_letter"),
                                 ("ZQX-extra.txt", b"ZQX extra document text", "other")):
            r = c.post("/api/documents", files={"file": (name, data, "application/octet-stream")}, data={"kind": kind})
            self.assertEqual(r.status_code, 200, r.text)
        texts = " ".join(d["text"] if "text" in d else "" for d in c.get("/api/documents").json())
        self.assertEqual(len(c.get("/api/documents").json()), 4, texts)

    def search_and_score(self, c):
        self.source.postings = [
            _ad(1),
            _ad(2, title="ZQX Delivery Lead (boom)", description="ZQX include. ZQXBOOM please lead."),
            _ad(3, description="ZQX include. ZQX exclude: not for you."),
            _ad(4, location_text="Zqxfail NSW"),
        ]
        run = c.post("/api/search/run")
        self.assertEqual(run.status_code, 200, run.text)
        done = self.run_to_end(c, run.json())
        self.assertEqual(done["state"], "done", done)
        jobs = c.get("/api/jobs?include=excluded,hidden").json()
        self.assertTrue([j for j in jobs if MARK in j["title"]], "the planted ads should be in the job list")
        self.assertTrue([j for j in jobs if MARK in (j.get("excluded_reason") or "")] or
                        [j for j in jobs if j.get("excluded_reason")], "one ad should have been excluded")
        scored = [j for j in jobs if (j.get("fit") or {}).get("status") == "ok"]
        self.assertTrue(scored, "something should have been scored")
        job_id = scored[0]["id"]
        r = c.post(f"/api/score/{job_id}")
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post("/api/score/run", json={"mode": "all"})
        self.assertEqual(self.run_to_end(c, r.json())["state"], "done")
        self.assertEqual(c.patch(f"/api/jobs/{job_id}/status", json={"status": "shortlisted"}).status_code, 200)
        self.assertEqual(c.get(f"/api/jobs/{job_id}").status_code, 200)
        self.assertEqual(c.post("/api/jobs/refilter").status_code, 200)
        self.job_id = job_id
        return jobs

    def talk_to_the_ai(self, c, jobs):
        job_id = self.job_id
        r = c.post("/api/chats", json={"job_ids": [job_id], "message": "ZQX chat question: how do I fit?"})
        self.assertEqual(r.status_code, 200, r.text)
        chat = r.json()
        r = c.post(f"/api/chats/{chat['id']}/messages", json={"message": "ZQX follow-up with the web", "web_search": True})
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post(f"/api/jobs/{job_id}/cover-letter", json={"instructions": "ZQX instruction: mention my launch"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.put(f"/api/jobs/{job_id}/cover-letter", json={"text": "ZQX edited letter"}).status_code, 200)
        self.assertEqual(c.get(f"/api/jobs/{job_id}/cover-letter.docx").status_code, 200)
        r = c.post("/api/companies/research", json={"mode": "all"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.run_to_end(c, r.json())["state"], "done")
        r = c.post("/api/companies/research", json={"name": "ZQX Corp"})
        self.assertEqual(self.run_to_end(c, r.json())["state"], "done")
        self.assertIn("zqx corp", c.get("/api/companies").json())

    def browser_extension(self, c):
        """The browser extension: a key named by the person, then jobs it read from pages in their browser (the
        values, the description and the addresses), previewed and saved, a page of search results (each card's
        address and summary), and a source added and tested through the key."""
        from backend import ratelimit
        from backend.adapters.base import SourceError

        r = c.post("/api/extension/keys", json={"label": "ZQX Firefox on Zqx's laptop"})
        self.assertEqual(r.status_code, 201, r.text)
        self.secrets["extension_key"] = r.json()["key"]
        self.assertIn("ZQX", c.get("/api/extension/keys").json()[0]["label"])
        k = self.anonymous()
        k.headers["Authorization"] = f"Bearer {r.json()['key']}"
        self.assertIn("ZQX", k.get("/api/extension/status").json()["label"])
        ad = "ZQX include: lead the ZQX delivery program for Zqx Quillon's team. " * 6
        page = {"url": "https://careers.zqx.example/jobs/zqx-77?zqx=1", "canonical_url": "https://careers.zqx.example/c/zqx",
                "job": {"title": "ZQX Delivery Lead", "company": "ZQX Corp", "location_text": "Zqxtown NSW",
                        "salary_text": "ZQX $150k", "employment_type": "ZQX full time", "work_mode": "hybrid",
                        "posted_at": "2026-10-01", "url": "https://careers.zqx.example/jobs/zqx-77",
                        "description": ad, "description_from": "site", "method": "site"}}
        self.assertEqual(k.post("/api/extension/preview", json=page).status_code, 200)
        self.assertEqual(k.post("/api/extension/jobs", json=page).status_code, 201)
        edited = {**page, "job": {**page["job"], "description": "ZQX selected ad text", "description_from": "selection"}}
        self.assertEqual(k.post("/api/extension/jobs", json=edited).status_code, 200)
        self.assertEqual(k.post("/api/extension/preview", json=page).json()["existing"]["title"], "ZQX Delivery Lead")
        for bad in ({**page, "job": {**page["job"], "title": ""}}, {**page, "url": "ftp://zqx.example/zqx"},
                    {**page, "job": {**page["job"], "work_mode": "ZQX moon"}}, {**page, "html": "<p>ZQX</p>"},
                    {**page, "job": {**page["job"], "posted_at": "ZQX yesterday"}}):
            self.assertEqual(k.post("/api/extension/jobs", json=bad).status_code, 422)
        too_long = {**page, "job": {**page["job"], "title": "ZQX " * 200}}
        self.assertEqual(k.post("/api/extension/jobs", json=too_long).status_code, 413)
        listed = {"url": "https://jobs.zqx.example/search?q=zqx+delivery&zqx=new",
                  "jobs": [{"url": f"/roles/zqx-{n}?zqx=card", "title": f"ZQX Program Lead {n}", "company": "ZQX Corp",
                            "location_text": "Zqxtown NSW", "salary_text": "ZQX $160k", "employment_type": "ZQX full time",
                            "work_mode": "remote", "posted_at": "2026-10-09",
                            "description": "ZQX include: run the ZQX program for Zqx Quillon.\n\n- ZQX point"}
                           for n in (1, 2)]}
        r = k.post("/api/extension/jobs/batch", json=listed)
        self.assertEqual((r.status_code, r.json()["added"]), (200, 2), r.text)
        self.assertEqual(k.post("/api/extension/jobs/batch", json=listed).json()["existing"], 2)
        for bad in ({**listed, "jobs": [{**listed["jobs"][0], "title": ""}]},
                    {**listed, "jobs": [{**listed["jobs"][0], "url": "https://other.zqx.example/roles/zqx-9"}]},
                    {**listed, "jobs": [{**listed["jobs"][0], "posted_at": "ZQX 3d ago"}]},
                    {**listed, "jobs": [{**listed["jobs"][0], "html": "<article>ZQX</article>"}]}):
            self.assertEqual(k.post("/api/extension/jobs/batch", json=bad).status_code, 422)
        too_long = {**listed, "jobs": [{**listed["jobs"][0], "description": "ZQX " * 2000}]}
        self.assertEqual(k.post("/api/extension/jobs/batch", json=too_long).status_code, 413)
        r = k.post("/api/sources", json={"url": "https://careers.zqx-two.example/jobs", "label": "ZQX second employer"})
        self.assertEqual(r.status_code, 201, r.text)
        self.source.postings = [_ad(20)]
        self.source.notes = []  # (what a source test shows besides the sample)
        with mock.patch("backend.sources.source_adapter", side_effect=lambda *a, **kw: self.source):
            self.assertEqual(k.post(f"/api/sources/{r.json()['id']}/test").json()["count"], 1)
            self.source.failure = SourceError("ZQX source down: https://careers.zqx-two.example/jobs")
            self.assertFalse(k.post(f"/api/sources/{r.json()['id']}/test").json()["ok"])
            self.source.failure = None
        self.assertEqual(k.get("/api/jobs").status_code, 403)
        wrong = self.anonymous()
        wrong.headers["Authorization"] = "Bearer " + self.secrets["extension_key"][:-4] + "zqxq"
        self.secrets["wrong_extension_key"] = wrong.headers["Authorization"][7:]
        self.assertEqual(wrong.get("/api/extension/status").status_code, 401)
        ratelimit.BAD_KEY.reset()

    def when_things_fail(self, c):
        """Errors carry the text that caused them (a provider echoes the prompt, a library names the address): none
        of it may be logged, only what kind of failure it was."""
        from backend.adapters.base import SourceError
        from backend.scorer import ScorerError

        r = c.post("/api/documents", files={"file": ("ZQX-broken.pdf", b"ZQX is not a pdf", "application/pdf")},
                   data={"kind": "resume"})
        self.assertEqual(r.status_code, 400, r.text)
        # The AI service refuses (a bad key, a bad request): scoring, chat, a cover letter and research all fail.
        self.llm_status = 401
        for path, body in (("/api/chats", {"job_ids": [self.job_id], "message": "ZQX failing question"}),
                           (f"/api/jobs/{self.job_id}/cover-letter", {"instructions": "ZQX failing instruction"}),
                           (f"/api/score/{self.job_id}", None)):
            self.assertGreaterEqual(c.post(path, json=body).status_code, 400, path)
        self.assertEqual(self.run_to_end(c, c.post("/api/score/run", json={"mode": "all"}).json())["state"], "done")
        self.llm_status = 200
        with mock.patch("backend.research.run_response", side_effect=ScorerError("ZQX research failed for ZQX Corp")):
            r = c.post("/api/companies/research", json={"name": "ZQX Corp"})
            self.run_to_end(c, r.json())
        # A source breaks, with its address in the message; a whole run breaks, with the person's address in it.
        for failure in (SourceError("ZQX source down: https://careers.zqx.example/jobs"),
                        RuntimeError("ZQX unexpected, for zqxowner.private@example.invalid")):
            self.source.failure = failure
            self.run_to_end(c, c.post("/api/search/run").json())
        self.source.failure = None
        with mock.patch("backend.pipeline.run_search", side_effect=RuntimeError("ZQX run failed for zqxowner.private")):
            self.assertEqual(self.run_to_end(c, c.post("/api/search/run").json())["state"], "failed")
        self.assertEqual(self.anonymous().post("/api/auth/login", json={"email": "zqxnobody@example.invalid",
                                                                        "password": "zqx nobody"}).status_code, 401)

    def with_claude(self, c):
        """The same through the Claude SDK (its own logger, which logs whole requests at DEBUG), over a mock transport."""
        import anthropic
        import httpx2

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.assertIn(b"ZQX", request.content)  # the prompt really carries the person's data
            return httpx2.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": SCORE_REPLY}], "usage": {"input_tokens": 10, "output_tokens": 5}})

        client = anthropic.Anthropic(api_key="zqx-claude-key", max_retries=0,
                                     http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with mock.patch.dict(os.environ, {"LLM_BASE_URL": "https://api.anthropic.com", "LLM_MODEL": "claude-sonnet-4-5",
                                          "LLM_API_KEY": "zqx-claude-key"}), \
                mock.patch("backend.llm_anthropic._client", return_value=client):
            self.assertEqual(c.post(f"/api/score/{self.job_id}").status_code, 200)
            r = c.post("/api/chats", json={"job_ids": [self.job_id], "message": "ZQX Claude question"})
            self.assertEqual(r.status_code, 200, r.text)
            r = c.post(f"/api/jobs/{self.job_id}/cover-letter", json={"instructions": "ZQX Claude instruction"})
            self.assertEqual(r.status_code, 200, r.text)

    def export(self, c):
        r = c.post("/api/account/export")
        self.assertEqual(r.status_code, 200, r.text)
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        text = b"".join(zipfile.ZipFile(io.BytesIO(r.content)).read(n) for n in names)
        self.assertIn(b"ZQX", text, "the export should hold the planted data")
        self.assertIn(b"zqxowner", text.lower())

    def scheduled_search_and_recovery(self, c, notice):
        # Background access (so scheduled runs can open the data), then a scheduled run.
        self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)
        db = SessionLocal()
        try:
            uid = users.find_by_name(db, self.email).id
            ws = db.query(Workspace.id).filter(Workspace.owner_id == uid).order_by(Workspace.id).scalar()
        finally:
            db.close()
        acct = helpers.Account(uid, self.email, PASSWORD, ws)
        self.source.postings = [_ad(10, title="ZQX Delivery Lead scheduled one"),
                                _ad(11, title="ZQX Delivery Lead scheduled two")]
        with helpers.as_account(acct):
            run = app_module.start_search_run(ws, scheduled=True)
        self.assertEqual(self.run_to_end(c, run)["state"], "done")

        # A forgotten password, reset with a recovery code (a wrong one first): the data is kept.
        anon = self.anonymous()
        r = anon.post("/api/auth/recover", json={"name": self.email, "recovery_code": "ZQXQ-ZQXQ-ZQXQ-ZQXQ",
                                                 "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 401, r.text)
        r = anon.post("/api/auth/recover", json={"name": self.email, "recovery_code": self.secrets["recovery_code_0"],
                                                 "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.secrets.update(new_password=NEW_PASSWORD, recovered_csrf=r.json()["csrf"])
        self.assertEqual(anon.post("/api/auth/login", json={"name": self.email, "password": "zqx wrong password"}
                                   ).status_code, 401)
        notice.append(True)


class StripQueryTest(unittest.TestCase):
    """uvicorn's access log: the query string (which can carry a token) is dropped."""

    def test_the_access_log_has_no_query_string(self):
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                   ("127.0.0.1:5", "GET", "/verify?token=ZQXsecret&x=1", "1.1", 200), None)
        logging.getLogger("uvicorn.access").filters[0].filter(record)
        self.assertNotIn("ZQXsecret", record.getMessage())
        self.assertIn("/verify ", record.getMessage())

    def test_the_quiet_loggers_stay_quiet(self):
        """What the app holds back is held back even with the root logger at DEBUG."""
        self.assertTrue({"httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "pypdf"} <= set(app_module.QUIET_LOGGERS))
        root = logging.getLogger()
        handler, old = Everything(), root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            for name in app_module.QUIET_LOGGERS:
                self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING, name)
            logging.getLogger("pypdf._reader").warning("invalid pdf header: b'ZQX private'")  # (made on demand, like pypdf's)
            logging.getLogger("anthropic._base_client").debug("Request options: ZQX prompt")
            logging.getLogger("httpx2").info("HTTP Request: GET https://api.example/?q=ZQX")
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        self.assertEqual(handler.lines, [])

    def test_sqlalchemy_never_logs_statements_or_rows(self):
        """At DEBUG it would log every row it reads, opened (decrypted) by the app's column types."""
        helpers.started()
        sql = logging.getLogger("sqlalchemy.engine.Engine")
        handler, old = Everything(), sql.level
        sql.addHandler(handler)
        sql.setLevel(logging.DEBUG)
        try:
            db = SessionLocal()
            try:
                self.assertTrue(db.query(User.id, User.status).all())
            finally:
                db.close()
            sql.warning("a warning still comes through")
        finally:
            sql.removeHandler(handler)
            sql.setLevel(old)
        self.assertEqual(handler.lines, ["sqlalchemy.engine.Engine WARNING a warning still comes through"])


if __name__ == "__main__":
    unittest.main()
