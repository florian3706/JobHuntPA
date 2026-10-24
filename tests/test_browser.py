"""The browser for JavaScript-only careers pages (backend/browser.py): downloaded into the data folder only when a page
first needs it and the person agrees; declined, those pages say what they need. Nothing is downloaded here: the
download command is replaced by a stand-in that makes the folder Playwright would make."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests import helpers
from backend import browser
from backend.app import app
from backend.db import AppSetting, SessionLocal
from backend.scraping import http


def forget_setting():
    db = SessionLocal()
    try:
        db.query(AppSetting).filter(AppSetting.key == browser.SETTING).delete()
        db.commit()
    finally:
        db.close()


class BrowserBase(unittest.TestCase):
    def setUp(self):
        helpers.started()
        self.top = Path(tempfile.mkdtemp(prefix="jobhunt-browser-"))
        self.addCleanup(shutil.rmtree, self.top, True)
        self.data = self.top / "browsers"
        for patch in (mock.patch.object(browser, "folder", return_value=self.data),
                      mock.patch.object(browser, "_playwright_cache", return_value=self.top / "no-cache"),
                      mock.patch.dict(os.environ, {}, clear=False)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(browser.ENV, None)
        forget_setting()
        self.addCleanup(forget_setting)
        browser._installing, browser._error = False, None

    def fake_download(self, ok=True):
        def run(cmd, env, **kwargs):
            self.assertEqual(cmd[1:], ["-m", "playwright", "install", "chromium"])
            self.assertEqual(env[browser.ENV], str(self.data))
            if ok:
                (self.data / "chromium-1234").mkdir(parents=True)
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, b"", b"")
        return mock.patch.object(browser.subprocess, "run", side_effect=run)

    def wait_done(self):
        for _ in range(100):
            if not browser._installing:
                return
            time.sleep(0.05)
        self.fail("the download didn't finish")


class FindingItTest(BrowserBase):
    def test_where_it_counts_as_there(self):
        self.assertFalse(browser.available())
        (self.top / "no-cache" / "chromium-1100").mkdir(parents=True)  # an older setup's download
        self.assertTrue(browser.available())
        shutil.rmtree(self.top / "no-cache")
        (self.data / "chromium_headless_shell-1200").mkdir(parents=True)
        self.assertTrue(browser.available())
        with mock.patch.dict(os.environ, {browser.ENV: str(self.top / "empty")}):
            self.assertFalse(browser.available())  # whoever runs the app chose another folder
        with mock.patch.dict(os.environ, {browser.ENV: "0"}):
            self.assertTrue(browser.available())

    def test_a_download_in_the_data_folder_is_used_at_start_up(self):
        browser.use_data_folder()
        self.assertNotIn(browser.ENV, os.environ)
        (self.data / "chromium-1234").mkdir(parents=True)
        browser.use_data_folder()
        self.assertEqual(os.environ[browser.ENV], str(self.data))


class NeededTest(BrowserBase):
    def test_a_javascript_page_without_the_browser_says_what_it_needs_and_downloads_nothing(self):
        client = http.PoliteClient()
        self.addCleanup(client.close)
        with mock.patch.object(client, "check_allowed"), mock.patch.object(browser.subprocess, "run") as ran, \
                mock.patch.object(browser.threading, "Thread") as thread:
            with self.assertRaises(http.FetchError) as caught:
                client.render("https://careers.example.invalid/jobs")
        self.assertEqual(str(caught.exception), browser.NEEDED)
        self.assertIn("Settings > Browser for JavaScript pages", browser.NEEDED)
        ran.assert_not_called()
        thread.assert_not_called()
        self.assertTrue(browser.state()["needed"])

    def test_a_chromium_for_an_older_playwright_counts_as_missing(self):
        (self.data / "chromium-1000").mkdir(parents=True)
        client = http.PoliteClient()
        self.addCleanup(client.close)

        class Old:
            def __enter__(self):
                raise RuntimeError("BrowserType.launch: Executable doesn't exist at /x/chromium-1234/chrome")

            def __exit__(self, *exc):
                return False

        with mock.patch.object(client, "check_allowed"), mock.patch.object(client, "_throttle"), \
                mock.patch.object(client, "_interval_for", return_value=0), \
                mock.patch("playwright.sync_api.sync_playwright", Old):
            with self.assertRaises(http.FetchError) as caught:
                client.render("https://careers.example.invalid/jobs")
        self.assertEqual(str(caught.exception), browser.NEEDED)

    def test_nothing_but_the_install_endpoint_starts_a_download(self):
        root = Path(browser.__file__).resolve().parent
        for path in root.rglob("*.py"):
            if path.name != "browser.py":
                self.assertNotIn("start_download", path.read_text(encoding="utf-8"), path.name)


class EndpointsTest(BrowserBase):
    def test_who_may_call(self):
        anon = TestClient(app)
        with anon:
            for method, path in (("get", "/api/settings/browser"), ("post", "/api/settings/browser/install"),
                                 ("post", "/api/settings/browser/decline")):
                self.assertEqual(getattr(anon, method)(path).status_code, 401, path)
        with helpers.client() as c:
            self.assertEqual(c.get("/api/settings/browser").status_code, 200)

    def test_agreeing_downloads_it_into_the_data_folder(self):
        browser.mark_needed()
        with helpers.client() as c, self.fake_download():
            st = c.get("/api/settings/browser").json()
            self.assertEqual((st["available"], st["needed"], st["declined"]), (False, True, False))
            r = c.post("/api/settings/browser/install")
            self.assertEqual(r.status_code, 202)  # (started in the background)
            self.wait_done()
            st = c.get("/api/settings/browser").json()
        self.assertEqual((st["available"], st["installing"], st["needed"], st["error"]), (True, False, False, None))
        self.assertEqual(os.environ[browser.ENV], str(self.data))
        with helpers.client() as c, mock.patch.object(browser.subprocess, "run") as ran:
            self.assertEqual(c.post("/api/settings/browser/install").status_code, 200)  # there already
            ran.assert_not_called()

    def test_a_failed_download_says_so_and_can_be_tried_again(self):
        with helpers.client() as c, self.fake_download(ok=False):
            c.post("/api/settings/browser/install")
            self.wait_done()
            st = c.get("/api/settings/browser").json()
        self.assertFalse(st["available"])
        self.assertIn("couldn't be downloaded", st["error"])
        self.assertNotIn(browser.ENV, os.environ)

    def test_one_download_at_a_time(self):
        with mock.patch.object(browser.threading, "Thread") as thread:
            self.assertTrue(browser.start_download())
            self.assertFalse(browser.start_download())
        thread.assert_called_once()

    def test_declining_hides_the_offer_and_the_pages_still_say_what_they_need(self):
        browser.mark_needed()
        with helpers.client() as c:
            st = c.post("/api/settings/browser/decline").json()
        self.assertTrue(st["declined"])
        self.assertTrue(st["needed"])
        self.assertFalse(st["available"])


class PageTest(unittest.TestCase):
    def test_the_offer_and_the_card_are_on_the_dashboard(self):
        html = (helpers.ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        for needle in ('id="browser-offer"', 'id="browser-offer-yes"', 'id="browser-offer-no"', 'id="browser-card"',
                       'id="browser-state" class="u-small u-muted" role="status"', 'src="./browser.js"'):
            self.assertIn(needle, html)
        js = (helpers.ROOT / "frontend" / "browser.js").read_text(encoding="utf-8")
        self.assertIn("/api/settings/browser/install", js)
        self.assertIn("/api/settings/browser/decline", js)


if __name__ == "__main__":
    unittest.main()
