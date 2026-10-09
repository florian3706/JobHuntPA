"""No log line may hold anything of a person's (accounts design §2.5: "Logs must never contain plaintext"; Phase L).

One account goes through the whole app, end to end, with the network, the mail server, Stripe and the AI service
mocked: sign-up, the search profile, documents, a search (mocked sources returning planted ads), scoring, chat, a cover
letter, company research, a subscription, a data export, a change of email address, a password reset by email, and a
digest. Every personal field holds a planted marker (``ZQX``), and every secret the run makes (passwords, recovery
codes, session and CSRF tokens, links in emails) is collected. Logging is captured at DEBUG for every logger, with the
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
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests import helpers  # noqa: E402  (sets the test environment first)
from tests.mail_helpers import MailTestCase, token_in  # noqa: E402

from backend import app as app_module, billing, digest, mail_queue, mailer, membership, users  # noqa: E402
from backend.adapters.base import Adapter, Posting  # noqa: E402
from backend.db import SessionLocal, User, Workspace  # noqa: E402

MARK = "ZQX"
PASSWORD = "zqx-first-password-1"
NEW_PASSWORD = "zqx-second-password-2"
NEW_EMAIL = "zqxmoved.address@example.invalid"
STRIPE_KEY = "sk_test_" + "a1B2c3D4" * 4
STRIPE_IDS = {"customer": "cus_ZqxCustomer1", "subscription": "sub_ZqxSub1", "session": "cs_test_ZqxSession1",
              "method": "pm_ZqxMethod1"}
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


class FullRun(MailTestCase):
    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.dict(os.environ, {**LLM_ENV, "STRIPE_SECRET_KEY": STRIPE_KEY, "STRIPE_PRICE_LOOKUP_KEY": "",
                                         "STRIPE_WEBHOOK_SECRET": ""}),
            mock.patch("backend.pipeline.source_adapter", side_effect=lambda *a, **k: self.source),
            mock.patch("backend.llm_providers.httpx.post", side_effect=self.llm),
            mock.patch("backend.geo.httpx.get", side_effect=self.nominatim),
            mock.patch("backend.geo.time.sleep"),
            mock.patch("backend.research.run_response", side_effect=self.web_search),
            mock.patch("backend.billing._client", return_value=self.stripe_client()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        billing.clear_price_cache()
        self.addCleanup(billing.clear_price_cache)
        membership._tried.clear()
        self.source = _Source([])
        self.secrets: dict[str, str] = {}
        self.llm_calls = 0
        self.llm_status = 200  # an error status: the provider answers it, echoing the request

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

    def stripe_client(self):
        client = mock.MagicMock()
        client.v1.prices.list.return_value = {"data": [
            {"id": "price_m", "lookup_key": "jobhunt_monthly", "unit_amount": 900, "currency": "aud",
             "recurring": {"interval": "month"}}]}
        client.v1.checkout.sessions.create.return_value = {"id": STRIPE_IDS["session"],
                                                           "url": "https://checkout.stripe.test/zqx"}
        self.stripe = client
        return client

    # -- the person ---------------------------------------------------------------------------------------------

    @staticmethod
    def restore_signup():
        with helpers.client() as admin:
            admin.put("/api/admin/signup", json={"mode": "invite", "free_searches": 3})

    def signed_up(self):
        admin = self.signed_in()
        self.assertEqual(admin.put("/api/admin/signup", json={"mode": "open", "free_searches": 20}).status_code, 200)
        self.addCleanup(self.restore_signup)
        email = "zqxowner.private@example.invalid"
        c = self.anonymous()
        r = c.post("/api/auth/signup", json={"email": email, "password": PASSWORD, "name": "Zqx Quillon"})
        self.assertEqual(r.status_code, 201, r.text)
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.secrets.update(password=PASSWORD, csrf=r.json()["csrf"], session=c.cookies.get("jh_session") or "",
                            device=c.cookies.get("jh_device") or "",
                            **{f"recovery_code_{i}": code for i, code in enumerate(r.json()["recovery_codes"])})
        helpers._passwords[email] = PASSWORD
        self.email = email
        return c

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
            self.sign_up_and_set_up(c)
            grant = self.free_period(c)  # the AI features are for members (a free period, as an admin grants it)
            ads = self.search_and_score(c)
            self.talk_to_the_ai(c, ads)
            self.browser_extension(c)
            self.when_things_fail(c)
            self.with_claude(c)
            self.end_free_period(grant)  # so the account pays for its membership next
            self.pay_export_and_change(c)
            self.reset_and_digest(c, notice)
        everything = "\n".join(log.lines + [printed.getvalue()])
        self.assertGreater(len(log.lines), 100, "the capture saw almost nothing")
        self.assertIn("the capture works", everything)
        planted = {"marker": MARK.lower(), "email": "zqxowner", "new email": "zqxmoved", "name": "quillon",
                   **{k: v.lower() for k, v in self.secrets.items() if v}}
        for what, value in planted.items():
            found = [line[:300] for line in log.lines if value in line.lower()]
            self.assertEqual(found, [], f"{what} ({value!r}) is in the log")
            self.assertNotIn(value, printed.getvalue().lower(), f"{what} was printed")
        self.assertTrue(notice, "the steps didn't all run")

    # -- the steps ----------------------------------------------------------------------------------------------

    def free_period(self, c) -> tuple:
        user_id = c.get("/api/auth/me").json()["user"]["id"]
        admin = self.signed_in()
        r = admin.post(f"/api/admin/users/{user_id}/grants",
                       json={"kind": "free_period", "days": 1, "note": f"{MARK} free period for {self.email}"})
        self.assertEqual(r.status_code, 201, r.text)
        return admin, user_id, r.json()["grant"]["id"]

    def end_free_period(self, grant: tuple) -> None:
        admin, user_id, grant_id = grant
        self.assertEqual(admin.delete(f"/api/admin/users/{user_id}/grants/{grant_id}").status_code, 200)

    def sign_up_and_set_up(self, c):
        # The address check mail, then the search profile, a pin, a source and documents.
        token = token_in(self.last_to(self.email), "verify-email.html")
        self.secrets["verify_link_token"] = token
        self.assertEqual(c.post("/api/auth/verify-email", json={"token": token}).status_code, 200)
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
        self.assertEqual(c.get("/api/auth/email").json()["verified"], True)

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
        """The browser extension: a key named by the person, then pages sent from their browser (the page's text, its
        HTML and a selection), saved as jobs with their edits, and a source added and tested through the key."""
        from backend import ratelimit
        from backend.adapters.base import SourceError

        r = c.post("/api/extension/keys", json={"label": "ZQX Firefox on Zqx's laptop"})
        self.assertEqual(r.status_code, 201, r.text)
        self.secrets["extension_key"] = r.json()["key"]
        self.assertIn("ZQX", c.get("/api/extension/keys").json()[0]["label"])
        k = self.anonymous()
        k.headers["Authorization"] = f"Bearer {r.json()['key']}"
        self.assertIn("ZQX", k.get("/api/extension/status").json()["label"])
        ad = ("<p>ZQX include: lead the ZQX delivery program for Zqx Quillon's team. " * 6) + "</p>"
        html = (f"<html><head><title>ZQX Delivery Lead | ZQX Corp</title></head><body><h1>ZQX Delivery Lead</h1>"
                f'<div class="job-location">Zqxtown NSW</div><div class="job-description">{ad}</div></body></html>')
        page = {"url": "https://careers.zqx.example/jobs/zqx-77", "page_title": "ZQX Delivery Lead | ZQX Corp",
                "html": html, "text": "ZQX visible text of the page", "selection": "",
                "fields": {"title": "ZQX Delivery Lead (edited)", "company": "ZQX Corp", "salary_text": "ZQX $150k"}}
        self.assertEqual(k.post("/api/extension/preview", json=page).status_code, 200)
        self.assertEqual(k.post("/api/extension/jobs", json=page).status_code, 201)
        self.assertEqual(k.post("/api/extension/jobs", json={**page, "selection": "ZQX selected ad text"}).status_code, 200)
        for bad in ({**page, "html": "<p>ZQX nothing</p>", "fields": {}, "page_title": ""},
                    {**page, "url": "ftp://zqx.example/zqx"}, {**page, "fields": {"work_mode": "ZQX moon"}}):
            self.assertEqual(k.post("/api/extension/jobs", json=bad).status_code, 422)
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
        # Stripe fails with an error that names the customer; the mail server fails with the address.
        self.stripe.v1.checkout.sessions.create.side_effect = RuntimeError(f"ZQX {STRIPE_IDS['customer']} {self.email}")
        self.assertGreaterEqual(c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"}).status_code, 400)
        self.stripe.v1.checkout.sessions.create.side_effect = None
        with mock.patch.object(mailer, "send", side_effect=RuntimeError(f"ZQX smtp refused {self.email}")):
            self.anonymous().post("/api/auth/forgot", json={"email": self.email})
            mail_queue.drain(20)
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

    def pay_export_and_change(self, c):
        self.assertEqual(c.post("/api/billing/checkout", json={"plan": "jobhunt_monthly"}).status_code, 200)
        ref = self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"]["client_reference_id"]
        self.secrets["stripe_ref"] = ref
        ts = int(time.time())
        subscription = {
            "id": STRIPE_IDS["subscription"], "status": "active", "customer": STRIPE_IDS["customer"],
            "created": ts - 100, "cancel_at_period_end": False, "metadata": {"jh_ref": ref},
            "items": {"data": [{"current_period_start": ts - 100, "current_period_end": ts + 86400 * 29,
                                "price": {"id": "price_m", "lookup_key": "jobhunt_monthly"}}]},
            "default_payment_method": {"id": STRIPE_IDS["method"], "card": {"brand": "visa", "last4": "4242"}}}
        self.stripe.v1.checkout.sessions.retrieve.return_value = {
            "id": STRIPE_IDS["session"], "client_reference_id": ref, "status": "complete", "subscription": subscription}
        r = c.post("/api/billing/verify", json={"session_id": STRIPE_IDS["session"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["me"]["member"])
        self.secrets.update({f"stripe_{k}": v for k, v in STRIPE_IDS.items()})
        self.assertEqual(c.get("/api/billing/status").status_code, 200)

        r = c.post("/api/account/export")
        self.assertEqual(r.status_code, 200, r.text)
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        text = b"".join(zipfile.ZipFile(io.BytesIO(r.content)).read(n) for n in names)
        self.assertIn(b"ZQX", text, "the export should hold the planted data")
        self.assertIn(b"zqxowner", text.lower())

        r = c.post("/api/auth/email/change", json={"new_email": NEW_EMAIL, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        token = token_in(self.last_to(NEW_EMAIL), "verify-email.html")
        self.secrets["change_link_token"] = token
        r = self.anonymous().post("/api/auth/verify-email", json={"token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get("/api/auth/email").json()["email"], NEW_EMAIL)

    def reset_and_digest(self, c, notice):
        # Background access (so the reset keeps the data and scheduled runs can open it), the digest, a scheduled run.
        self.assertEqual(c.post("/api/account/background", json={"enabled": True}).status_code, 200)
        self.assertEqual(c.put("/api/account/digest", json={"enabled": True, "every_days": 1}).status_code, 200)
        db = SessionLocal()
        try:
            uid = users.find_by_email(db, NEW_EMAIL).id
            ws = db.query(Workspace.id).filter(Workspace.owner_id == uid).order_by(Workspace.id).scalar()
        finally:
            db.close()
        helpers._passwords[NEW_EMAIL] = PASSWORD
        acct = helpers.Account(uid, NEW_EMAIL, PASSWORD, ws)
        self.source.postings = [_ad(10, title="ZQX Delivery Lead scheduled one"),
                                _ad(11, title="ZQX Delivery Lead scheduled two")]
        with helpers.as_account(acct):
            run = app_module.start_search_run(ws, scheduled=True)
        self.assertEqual(self.run_to_end(c, run)["state"], "done")
        now = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=3, minute=0, second=0, microsecond=0)
        self.assertEqual(digest.tick(now), 1)
        message = self.last_to(NEW_EMAIL)
        self.assertIn("ZQX", message.as_string() + message.get_body(preferencelist=("plain",)).get_content())
        self.secrets["unsubscribe_token"] = parse_qs(urlsplit(message["List-Unsubscribe"].strip("<>")).query)["token"][0]
        anon = self.anonymous()
        self.assertEqual(anon.post(f"/unsubscribe?token={self.secrets['unsubscribe_token']}",
                                   data={"List-Unsubscribe": "One-Click"}).status_code, 200)

        # A password reset by email: the address the link goes to is the new one.
        forgot = self.anonymous()
        self.assertEqual(forgot.post("/api/auth/forgot", json={"email": NEW_EMAIL}).status_code, 202)
        token = token_in(self.last_to(NEW_EMAIL), "reset-password.html")
        self.secrets["reset_link_token"] = token
        self.assertEqual(forgot.post("/api/auth/reset/info", json={"token": token}).json()["keeps_data"], True)
        r = forgot.post("/api/auth/reset", json={"token": token, "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["kept_data"])
        self.secrets["new_password"] = NEW_PASSWORD
        self.assertEqual(forgot.post("/api/auth/login", json={"email": NEW_EMAIL, "password": "zqx wrong password"}
                                     ).status_code, 401)
        self.flush()
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
                self.assertTrue(db.query(User.id, User.role).all())
            finally:
                db.close()
            sql.warning("a warning still comes through")
        finally:
            sql.removeHandler(handler)
            sql.setLevel(old)
        self.assertEqual(handler.lines, ["sqlalchemy.engine.Engine WARNING a warning still comes through"])


if __name__ == "__main__":
    unittest.main()
