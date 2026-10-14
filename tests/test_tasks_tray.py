"""The tasks tray (docs/design/open-source.md §1.13): GET /api/tasks lists the AI work running for the signed-in account.

Every kind of AI work is in the list while it runs and gone once it ends, whether it finished or failed. The AI is a
stand-in whose calls wait until the test lets them go, so the list can be read while a call is under way. The list
holds kinds, ids and numbers, never text. Another account never sees this account's tasks; signed out (or with a
browser-extension key) the list is refused. Nothing can be cancelled (§1.16): there is no route to do it.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import unittest
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402

from backend import tasks  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import Document, Job, SessionLocal  # noqa: E402
from backend.scorer import ScorerError  # noqa: E402

LLM_ENV = {"LLM_API_KEY": "k-tray-test", "LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "m"}
MARK = "ZQXTRAY"  # in every text the test stores: it must never come back in the list
ANSWER = json.dumps({"score": 70, "summary": "fine", "requirements": [], "gaps": [], "titles": [],
                     "office_location": ""})


class Gate:
    """A stand-in AI call: each call waits until the test lets it go (``release``), then answers or fails. Once
    ``open``, calls answer at once."""

    def __init__(self, answer):
        self.answer = answer
        self.waiting: queue.Queue = queue.Queue()
        self.open = False
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append(tasks.current_task.get())  # the task this call is made for (None: not in the tray)
        if self.open:
            return self.answer
        call = {"go": threading.Event(), "fail": False}
        self.waiting.put(call)
        if not call["go"].wait(30):
            raise ScorerError("the test never released this call")
        if call["fail"]:
            raise ScorerError("stand-in failure")
        return self.answer

    def next(self) -> dict:
        try:
            return self.waiting.get(timeout=15)
        except queue.Empty:
            raise AssertionError("no AI call arrived") from None

    @staticmethod
    def release(call: dict, fail: bool = False) -> None:
        call["fail"] = fail
        call["go"].set()


def search_answer(text: str = "{}") -> dict:
    return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}


class TasksTrayTest(unittest.TestCase):
    def setUp(self):
        helpers.started()
        env = mock.patch.dict(os.environ, LLM_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.chat = Gate(ANSWER)
        self.search = Gate(search_answer())
        for target, gate in (("backend.llm_providers.chat", self.chat), ("backend.llm_providers.web_search", self.search)):
            patcher = mock.patch(target, side_effect=gate)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.open_gates)
        self.addCleanup(self.every_call_was_a_task)
        self.c = helpers.client()
        self.c.__enter__()
        self.addCleanup(self.c.__exit__, None, None, None)
        self.ws = self.c.post("/api/workspaces", json={"name": f"{MARK} tray"}).json()["id"]
        self.c.headers["X-Workspace"] = str(self.ws)
        with helpers.as_admin():
            db = SessionLocal()
            try:
                job = Job(workspace_id=self.ws, url="https://tray.example/job/1", title=f"{MARK} Delivery Lead",
                          company=f"{MARK} Corp", location_text="Sydney NSW", description=f"{MARK} ad text. " * 20,
                          status="to_review", detail_status="full")
                doc = Document(workspace_id=self.ws, filename=f"{MARK}-cv.txt", kind="resume",
                               text=f"{MARK} resume text", use_for_scoring=True)
                db.add_all([job, doc])
                db.commit()
                self.job_id, self.doc_id = job.id, doc.id
            finally:
                db.close()

    def open_gates(self):
        for gate in (self.chat, self.search):
            gate.open = True
            while not gate.waiting.empty():
                gate.release(gate.waiting.get_nowait())

    def every_call_was_a_task(self):
        for gate in (self.chat, self.search):
            self.assertNotIn(None, gate.calls, "every AI call is made inside a task of the tray")

    # ---- helpers

    def listed(self, c=None) -> dict:
        r = (c or self.c).get("/api/tasks")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(MARK, r.text, "the list holds ids and kinds, never text")
        return r.json()

    def kinds(self, c=None) -> list[str]:
        return [t["kind"] for t in self.listed(c)["tasks"]]

    def wait_until_gone(self, kind: str) -> None:
        for _ in range(300):
            if kind not in self.kinds():
                return
            time.sleep(0.02)
        self.fail(f"the {kind} task stayed in the list")

    def in_background(self, method: str, path: str, body=None) -> dict:
        """Sends a request that waits for the AI from another thread; ``result["response"]`` once it's answered."""
        result: dict = {}

        def send():
            result["response"] = getattr(self.c, method)(path, **({"json": body} if body is not None else {}))

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        result["thread"] = thread
        return result

    def finish(self, result: dict) -> None:
        result["thread"].join(30)
        self.assertFalse(result["thread"].is_alive(), "the request didn't finish")

    def run_to_end(self, run: dict) -> dict:
        for _ in range(600):
            got = self.c.get(f"/api/runs/{run['id']}").json()
            if got["state"] not in ("queued", "running"):
                return got
            time.sleep(0.05)
        self.fail("the run didn't finish")

    def one(self, kind: str) -> dict:
        found = [t for t in self.listed()["tasks"] if t["kind"] == kind]
        self.assertEqual(len(found), 1, self.listed())
        return found[0]

    # ---- each kind, while it runs and after

    def check_request(self, kind: str, method: str, path: str, body=None, gate=None, fail: bool = False,
                      subject=None, **expected):
        gate = gate or self.chat
        sent = self.in_background(method, path, body)
        call = gate.next()
        task = self.one(kind)
        self.assertEqual(task["what"], tasks.KINDS[kind])
        self.assertEqual(task["workspace_id"], self.ws)
        self.assertEqual(task["subject"], subject)
        self.assertIsNone(task["parent_id"])
        self.assertTrue(task["started_at"].endswith("Z"))
        for name, value in expected.items():
            self.assertEqual(task[name], value, name)
        self.assertEqual(self.listed()["count"], 1)
        gate.release(call, fail=fail)
        self.finish(sent)
        if fail:
            self.assertGreaterEqual(sent["response"].status_code, 400)
        else:
            self.assertLess(sent["response"].status_code, 300, sent["response"].text)
        self.assertEqual(self.listed(), {"count": 0, "tasks": []})

    def test_a_cover_letter_while_it_is_written_and_after_it_failed(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.check_request("cover_letter", "post", f"/api/jobs/{self.job_id}/cover-letter", {}, fail=fail,
                                   subject="job", job_ids=[self.job_id])

    def test_a_chat_answer_with_and_without_web_search(self):
        self.check_request("chat", "post", "/api/chats", {"job_ids": [self.job_id], "message": f"{MARK} question"},
                           subject="job", job_ids=[self.job_id])
        chat = self.c.get(f"/api/chats?job_ids={self.job_id}").json()[0]
        self.search.answer = search_answer(f"{MARK} answer")
        self.check_request("chat", "post", f"/api/chats/{chat['id']}/messages",
                           {"message": f"{MARK} again", "web_search": True}, gate=self.search, subject="job",
                           job_ids=[self.job_id])
        self.check_request("chat", "post", f"/api/chats/{chat['id']}/messages", {"message": f"{MARK} fails"},
                           fail=True, subject="job")

    def test_document_summary_concise_version_and_title_suggestions(self):
        self.check_request("concise", "post", f"/api/documents/{self.doc_id}/concise", subject="document",
                           document_id=self.doc_id)
        self.check_request("summary", "post", "/api/documents/summary", subject=None, document_id=None)
        self.check_request("titles", "post", "/api/documents/suggest-titles")
        self.check_request("titles", "post", "/api/documents/suggest-titles", fail=True)

    def test_scoring_one_job(self):
        self.check_request("rescore", "post", f"/api/score/{self.job_id}", subject="job", job_ids=[self.job_id])
        self.check_request("rescore", "post", f"/api/score/{self.job_id}", fail=True, subject="job")

    def test_a_score_run_with_its_progress(self):
        run = self.c.post("/api/score/run", json={"mode": "all"}).json()
        call = self.chat.next()
        task = self.one("rescore")
        self.assertEqual((task["subject"], task["job_ids"], task["parent_id"]), (None, [], None))
        self.chat.release(call, fail=True)  # one job's failure doesn't end the run, and the task ends with it
        self.assertEqual(self.run_to_end(run)["state"], "done")
        self.assertEqual(self.listed()["tasks"], [])

    def test_a_search_run_and_its_scoring_step(self):
        listing = threading.Event()
        go_on = threading.Event()

        def fake_search(ws, progress):
            progress("Some source: 2/5", {"source": f"{MARK} source", "done": 2, "total": 5})
            listing.set()
            go_on.wait(15)
            return {"job_ids": [], "per_source": []}

        with mock.patch("backend.pipeline.run_search", side_effect=fake_search):
            run = self.c.post("/api/search/run").json()
            self.assertTrue(listing.wait(15))
            task = self.one("search")
            self.assertEqual(task["progress"], {"done": 2, "total": 5})
            self.assertEqual(self.listed()["count"], 1)
            go_on.set()
            call = self.chat.next()  # scoring the new job
            listed = self.listed()
            self.assertEqual([t["kind"] for t in listed["tasks"]], ["search", "scoring"])
            search, scoring = listed["tasks"]
            self.assertEqual(scoring["parent_id"], search["id"])
            self.assertIsNone(search["progress"], "the run's steps after the listing have no numbers")
            self.assertEqual(listed["count"], 1, "a step isn't a task of its own")
            self.chat.release(call)
            self.assertEqual(self.run_to_end(run)["state"], "done")
        self.assertEqual(self.listed(), {"count": 0, "tasks": []})

    def test_a_failed_search_run_leaves_no_task(self):
        started = threading.Event()

        def broken(ws, progress):
            started.set()
            raise RuntimeError("source broke")

        with mock.patch("backend.pipeline.run_search", side_effect=broken):
            run = self.c.post("/api/search/run").json()
            self.assertTrue(started.wait(15))
            self.assertEqual(self.run_to_end(run)["state"], "failed")
        self.assertEqual(self.listed()["tasks"], [])

    def test_research_of_one_company_profile_and_a_section(self):
        run = self.c.post("/api/companies/research", json={"name": f"{MARK} Corp"}).json()
        call = self.search.next()
        listed = self.listed()
        self.assertEqual(len(listed["tasks"]), 1, listed)
        task = listed["tasks"][0]
        self.assertEqual((task["kind"], task["subject"], task["job_ids"], task["part"], task["part_label"]),
                         ("research", "company", [self.job_id], "profile", "profile"))
        self.search.release(call)
        self.assertEqual(self.run_to_end(run)["state"], "done")
        self.assertEqual(self.listed()["tasks"], [])
        # A section of a researched profile, which fails.
        with helpers.as_admin():
            from backend.db import CompanyProfile, company_key
            from backend.research import RESEARCH_VERSION
            db = SessionLocal()
            try:
                row = db.get(CompanyProfile, company_key(f"{MARK} Corp"))
                row.status, row.research_version = "done", RESEARCH_VERSION
                db.commit()
            finally:
                db.close()
        run = self.c.post("/api/companies/research", json={"name": f"{MARK} Corp", "part": "news"}).json()
        call = self.search.next()
        task = self.one("research")
        self.assertEqual((task["part"], task["part_label"]), ("news", "news"))
        self.search.release(call, fail=True)
        self.assertEqual(self.run_to_end(run)["state"], "done")
        self.assertEqual(self.listed()["tasks"], [])

    def test_fill_missing_info_researches_companies_then_looks_up_offices(self):
        run = self.c.post("/api/companies/research", json={"mode": "all"}).json()
        call = self.search.next()  # the company's research
        listed = self.listed()
        self.assertEqual([t["kind"] for t in listed["tasks"]], ["research", "research"])
        batch, step = listed["tasks"]
        self.assertEqual((batch["subject"], batch["progress"]), (None, {"done": 0, "total": 1}))
        self.assertEqual((step["parent_id"], step["subject"], step["job_ids"], step["part"]),
                         (batch["id"], "company", [self.job_id], "profile"))
        self.assertEqual(listed["count"], 1)
        self.search.release(call)
        call = self.chat.next()  # then the office named in the ad ("Sydney NSW" only names a city)
        listed = self.listed()
        self.assertEqual([t["kind"] for t in listed["tasks"]], ["research", "offices"])
        self.assertEqual(listed["tasks"][1]["parent_id"], listed["tasks"][0]["id"])
        self.assertEqual(listed["tasks"][1]["progress"], {"done": 1, "total": 1})
        self.chat.release(call, fail=True)
        self.open_gates()  # (an office search may follow)
        self.assertEqual(self.run_to_end(run)["state"], "done")
        self.assertEqual(self.listed()["tasks"], [])

    # ---- who sees what

    def test_another_account_never_sees_these_tasks(self):
        bob = helpers.make_user()
        sent = self.in_background("post", f"/api/jobs/{self.job_id}/cover-letter", {})
        call = self.chat.next()
        try:
            self.assertEqual(self.kinds(), ["cover_letter"])
            with helpers.client(bob.email) as other:
                self.assertEqual(self.listed(other), {"count": 0, "tasks": []})
                # Bob's own task is his alone, in his workspace; the first account's list doesn't show it.
                with tasks.track("titles", bob.workspace_id):
                    self.assertEqual(self.kinds(other), ["titles"])
                    self.assertEqual(self.kinds(), ["cover_letter"])
        finally:
            self.chat.release(call)
            self.finish(sent)

    def test_signed_out_and_extension_keys_are_refused(self):
        with TestClient(app) as anonymous:
            r = anonymous.get("/api/tasks")
            self.assertEqual(r.status_code, 401)
            key = self.c.post("/api/extension/keys", json={"label": "tray test"}).json()["key"]
            anonymous.headers["Authorization"] = f"Bearer {key}"
            self.assertEqual(anonymous.get("/api/tasks").status_code, 403)

    def test_there_is_no_cancelling(self):
        with tasks.track("titles", self.ws):
            task = self.listed()["tasks"][0]
            for method in ("delete", "post", "put", "patch"):
                r = getattr(self.c, method)(f"/api/tasks/{task['id']}")
                self.assertIn(r.status_code, (404, 405), method)
            self.assertIn(self.c.delete("/api/tasks").status_code, (404, 405))
            self.assertEqual(len(self.listed()["tasks"]), 1)


class CallSitesTest(unittest.TestCase):
    def test_the_modules_that_call_the_ai_are_the_ones_whose_work_is_registered(self):
        """A new module that calls the AI (Phase I1's mappings, say) must wrap its work in tasks.track, then go on
        this list (and get a case above)."""
        import re
        from pathlib import Path

        backend = Path(__file__).resolve().parent.parent / "backend"
        calling = {p.name for p in backend.rglob("*.py")
                   if p.name not in ("llm_providers.py", "llm_anthropic.py")
                   and re.search(r"\bcall_model\(|llm_providers\.(chat|web_search)\(", p.read_text(encoding="utf-8"))}
        self.assertEqual(calling, {"cover_letters.py", "docs.py", "job_chat.py", "offices.py", "research.py",
                                   "scorer.py", "title_suggestions.py"})
        # scorer.py's own calls: score_one (inside the scoring tasks) and the Settings tab's connection test, which
        # is a check of the settings, not AI work.
        scorer = (backend / "scorer.py").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"\bcall_model\(", scorer)), 3)  # (its definition, score_one, test_connection)


class RegistryTest(unittest.TestCase):
    def test_a_task_ends_with_its_block_and_holds_no_text(self):
        with self.assertRaises(RuntimeError):
            with tasks.track("chat", 99001, jobs=[5]) as task:
                self.assertIn(task.id, {t["id"] for t in tasks.running([99001])})
                raise RuntimeError("boom")
        self.assertEqual(tasks.running([99001]), [])
        self.assertIsNone(tasks.current_task.get())
        with tasks.track("research", 99001, company_job=7, part="sentiment") as task:
            task.progress(3, 2)
            self.assertEqual(tasks.running([99001])[0]["progress"], {"done": 2, "total": 2})
            task.progress("3", 4)
            self.assertIsNone(tasks.running([99001])[0]["progress"])
            item = tasks.running([99001])[0]
            self.assertEqual(set(item), {"id", "kind", "what", "part", "part_label", "workspace_id", "subject",
                                         "job_ids", "document_id", "parent_id", "started_at", "progress"})
            self.assertEqual((item["subject"], item["job_ids"]), ("company", [7]))
            self.assertEqual(tasks.running([99002]), [])

    def test_a_new_kind_is_one_line(self):
        with self.assertRaises(ValueError):
            with tasks.track("mapping", 99001):
                pass
        with mock.patch.dict(tasks.KINDS, {"mapping": "Working out a page"}):
            with tasks.track("mapping", 99001):
                self.assertEqual(tasks.running([99001])[0]["what"], "Working out a page")
        with self.assertRaises(ValueError):
            with tasks.track("chat", 99001, part="not a part"):
                pass

    def test_steps_follow_their_task_and_threads_carry_it(self):
        from concurrent.futures import ThreadPoolExecutor
        from backend.threads import submit

        with tasks.track("research", 99003) as outer:
            def step():
                with tasks.track("offices", 99003) as inner:
                    return inner.parent_id, [t["kind"] for t in tasks.running([99003])]

            with ThreadPoolExecutor(1) as pool:
                parent, kinds = submit(pool, step).result()
            self.assertEqual((parent, kinds), (outer.id, ["research", "offices"]))
        self.assertEqual(tasks.running([99003]), [])


if __name__ == "__main__":
    unittest.main()
