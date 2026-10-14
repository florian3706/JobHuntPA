"""Security headers and the Host allowlist (accounts design, Phase L).

* Every page's inline script has its hash in the Content-Security-Policy; no page or script-built markup uses an inline
  event handler, a ``javascript:`` URL, a ``style`` attribute or a ``<style>`` element; everything a page loads from
  another site is in the policy.
* Each header is on a page, a static file, an /api answer, an error answer and an unexpected 500.
* A request for a Host that isn't allowed gets 400 before anything else runs.

No browser is needed. (The policy was also loaded in Chromium over every page, in two themes, when it was written,
with no violation: a page edit that needs more than it allows fails the static checks here first.)

Run:  python3 -m unittest tests.test_security_headers -v
"""
from __future__ import annotations

import os
import re
import socket
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import hosts, ratelimit, security_headers  # noqa: E402
from backend.app import FRONTEND_DIR, app  # noqa: E402

PAGES = sorted(FRONTEND_DIR.glob("*.html"))
SCRIPTS = sorted(FRONTEND_DIR.glob("*.js"))
POLICY = security_headers.build_csp(FRONTEND_DIR)
EXPECTED = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "permissions-policy": security_headers.PERMISSIONS_POLICY,
    "content-security-policy": POLICY,
}


def directive(policy: str, name: str) -> list[str]:
    for part in policy.split(";"):
        words = part.split()
        if words and words[0] == name:
            return words[1:]
    return []


class Policy(unittest.TestCase):
    def test_there_are_pages(self):
        self.assertGreaterEqual(len(PAGES), 6)
        self.assertIn("login.html", [p.name for p in PAGES])

    def test_every_inline_script_has_its_hash(self):
        allowed = directive(POLICY, "script-src")
        for page in PAGES:
            scripts = [s for s in security_headers.inline_scripts(page.read_text(encoding="utf-8")) if s.strip()]
            self.assertEqual(len(scripts), 1, f"{page.name} should have its one theme script")
            for script in scripts:
                self.assertIn(security_headers.script_hash(script), allowed, page.name)
        self.assertNotIn("'unsafe-inline'", allowed)
        self.assertNotIn("'unsafe-eval'", allowed)

    def test_the_hash_is_the_one_a_browser_computes(self):
        # Known value: base64(sha256("alert(1)")).
        self.assertEqual(security_headers.script_hash("alert(1)"), "'sha256-bhHHL3z2vDgxUt0W3dWQOrprscmda2Y5pLsLg4GF+pI='")
        # The HTML parser turns CRLF into LF before a script is hashed, so a checkout with Windows line endings must
        # hash like one without.
        self.assertEqual(security_headers.script_hash("a();\r\nb();\r\n"), security_headers.script_hash("a();\nb();\n"))
        self.assertNotEqual(security_headers.script_hash("a();"), security_headers.script_hash("a(); "))

    def test_a_changed_page_changes_the_policy(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "a.html").write_text("<html><script>one()</script><script src='x.js'></script></html>")
            before = security_headers.build_csp(Path(folder))
            Path(folder, "a.html").write_text("<html><script>two()</script></html>")
            after = security_headers.build_csp(Path(folder))
        self.assertIn(security_headers.script_hash("one()"), before)
        self.assertNotIn(security_headers.script_hash("one()"), after)
        self.assertIn(security_headers.script_hash("two()"), after)
        self.assertNotIn("x.js", before)  # a script with a source needs no hash

    def test_the_policy(self):
        self.assertEqual(directive(POLICY, "default-src"), ["'self'"])
        for name, value in (("connect-src", ["'self'"]), ("form-action", ["'self'"]), ("frame-ancestors", ["'none'"]),
                            ("base-uri", ["'none'"]), ("object-src", ["'none'"]), ("frame-src", ["'none'"])):
            self.assertEqual(directive(POLICY, name), value, name)
        self.assertNotIn("'unsafe-inline'", directive(POLICY, "style-src"))
        for source in ("*", "https:", "http:", "data:"):
            self.assertNotIn(source, directive(POLICY, "script-src"))
            self.assertNotIn(source, directive(POLICY, "style-src"))

    def test_no_inline_handlers_javascript_urls_style_attributes_or_style_elements(self):
        handler = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.I)
        for path in [*PAGES, *SCRIPTS]:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(handler.search(text), f"{path.name} has an inline event handler")
            self.assertNotRegex(text, r"(?i)javascript\s*:", path.name)
            self.assertNotRegex(text, r"""(?i)\sstyle\s*=\s*["']""", f"{path.name} has a style attribute")
            self.assertNotRegex(text, r"(?i)<style[\s>]", f"{path.name} has a <style> element")
            self.assertNotRegex(text, r"""(?i)setAttribute\(\s*["']style["']""", path.name)
            self.assertNotRegex(text, r"\beval\s*\(|new\s+Function\s*\(", path.name)

    def test_everything_loaded_from_another_site_is_in_the_policy(self):
        scripts, styles, images, fonts = (directive(POLICY, n) for n in ("script-src", "style-src", "img-src", "font-src"))
        for page in PAGES:
            html = page.read_text(encoding="utf-8")
            for url in re.findall(r"""<script[^>]+src=["'](https?://[^"']+)""", html):
                self.assertIn(url, scripts, f"{page.name} loads {url}")
            for tag in re.findall(r"<link[^>]+>", html):
                match = re.search(r"""href=["'](https?://[^"']+)""", tag)
                if match:
                    self.assertIn(match.group(1), styles, f"{page.name} loads {match.group(1)}")
            self.assertEqual(re.findall(r"""<(?:img|iframe|embed|object|video|audio|source)\b[^>]*\ssrc=["']https?:""", html), [],
                             page.name)
            self.assertNotRegex(html, r"<form[^>]+action=", page.name)  # nothing posts to another place
        everything = "\n".join(p.read_text(encoding="utf-8") for p in SCRIPTS)
        # The dyslexia themes' font (stylesheet, then the files it names) and the map tiles.
        self.assertIn("https://fonts.googleapis.com", styles)
        self.assertIn("https://fonts.googleapis.com/css2?", everything)
        self.assertEqual(fonts, ["'self'", "https://fonts.gstatic.com"])
        tiles = set(re.findall(r"https://\{s\}\.(tile\.[a-z.]+)/", everything))
        self.assertEqual(tiles, {"tile.openstreetmap.org"})
        self.assertIn("https://*.tile.openstreetmap.org", images)
        # Nothing the pages fetch is outside this site.
        self.assertNotRegex(everything, r"""fetch\(\s*[`'"]https?:""")
        self.assertNotRegex(everything, r"XMLHttpRequest|WebSocket|EventSource|sendBeacon")
        self.assertNotIn("stripe", POLICY.lower())

    def test_the_map_tiles_say_which_site_they_are_for(self):
        """``Referrer-Policy: same-origin`` sends no Referer to other sites, and OpenStreetMap's tile servers answer a
        request without one with an "Access blocked" picture. Each tile layer sets its own policy, ``origin``: this
        site's address, never the page's."""
        layers = [(script.name, m.group(0)) for script in SCRIPTS
                  for m in re.finditer(r"L\.tileLayer\(.*?\}\)", script.read_text(encoding="utf-8"), re.S)]
        self.assertEqual(sorted(name for name, _ in layers), ["app.js"])  # (the job page has no map: commute links)
        for name, call in layers:
            self.assertIn("tile.openstreetmap.org", call, name)
            self.assertRegex(call, r"referrerPolicy: 'origin'", name)

    def test_the_permissions_policy_turns_off_what_the_app_does_not_use(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in SCRIPTS)
        policy = security_headers.PERMISSIONS_POLICY
        for feature, api in (("camera", "getUserMedia"), ("microphone", "getUserMedia"), ("geolocation", "geolocation"),
                             ("payment", "PaymentRequest"), ("usb", "navigator.usb"), ("serial", "navigator.serial"),
                             ("fullscreen", "requestFullscreen"), ("display-capture", "getDisplayMedia"),
                             ("clipboard-read", "clipboard.read(")):
            self.assertNotIn(api, text, f"the app uses {api}")
            self.assertIn(f"{feature}=()", policy)
        self.assertIn("clipboard.writeText", text)  # the copy buttons
        self.assertIn("clipboard-write=(self)", policy)


class Headers(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)

    def client(self, base_url: str = "http://testserver", **kwargs) -> TestClient:
        c = TestClient(app, base_url=base_url, **kwargs)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def assertHeaders(self, response, what: str, https: bool = False, **different):
        expected = {**EXPECTED, **different}
        for name, value in expected.items():
            self.assertEqual(response.headers.get(name), value, f"{what}: {name}")
        self.assertEqual(response.headers.get("strict-transport-security"),
                         security_headers.HSTS if https else None, f"{what}: strict-transport-security")

    def test_a_page_a_static_file_an_api_answer_and_errors(self):
        c = self.client()
        for path, status in (("/", 200), ("/index.html", 200), ("/login.html", 200), ("/styles.css", 200),
                             ("/common.js", 200), ("/api/health", 200),
                             ("/api/jobs", 401),                       # an error answer from the sign-in check
                             ("/api/no-such-thing", 401), ("/no-such-page.html", 404),
                             ("/docs", 404), ("/openapi.json", 404), ("/redoc", 404)):
            r = c.get(path)
            self.assertEqual(r.status_code, status, path)
            self.assertHeaders(r, path)
        self.assertEqual(c.get("/api/health").headers["cache-control"], "no-store")  # still there beside them

    def test_answers_to_a_signed_in_person_and_validation_errors(self):
        with helpers.client() as c:
            for method, path, body, status in (("get", "/api/workspaces", None, 200), ("get", "/api/jobs/99999999", None, 404),
                                              ("put", "/api/profile", {"titles": "not a list"}, 422),
                                              ("delete", "/api/health", None, 405)):
                r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
                self.assertEqual(r.status_code, status, path)
                self.assertHeaders(r, path)
            r = c.post("/api/search/run", headers={"Origin": "http://evil.example"})
            self.assertEqual(r.status_code, 403)  # refused by the same-site check
            self.assertHeaders(r, "bad origin")

    def test_strict_transport_security_only_over_https(self):
        self.assertHeaders(self.client().get("/"), "http")
        self.assertHeaders(self.client("https://testserver").get("/"), "https", https=True)
        self.assertHeaders(self.client("https://testserver").get("/api/health"), "https api", https=True)
        # A proxy that ends TLS without saying so: the https public address decides (sessions.is_secure).
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://jobs.example.com"}):
            r = self.client("http://jobs.example.com").get("/")
        self.assertHeaders(r, "public address", https=True)
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://jobs.example.com"}):
            self.assertHeaders(self.client("http://testserver").get("/"), "other host", https=False)

    def test_a_refused_host_has_them_too(self):
        r = self.client().get("/", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 400)
        self.assertHeaders(r, "bad host")

    def test_an_unexpected_error_has_them_too(self):
        small = FastAPI()

        @small.get("/boom")
        def boom():
            raise RuntimeError("something broke")

        security_headers.install(small, FRONTEND_DIR)
        c = TestClient(small, raise_server_exceptions=False, base_url="https://testserver")
        r = c.get("/boom")
        self.assertEqual(r.status_code, 500)
        self.assertHeaders(r, "500", https=True)

    def test_a_route_can_ask_for_a_stricter_header(self):
        small = FastAPI()

        @small.get("/own")
        def own():
            return PlainTextResponse(
                "x", headers={"Content-Security-Policy": "default-src 'none'", "Referrer-Policy": "no-referrer"})

        security_headers.install(small, FRONTEND_DIR)
        r = TestClient(small).get("/own")
        self.assertEqual(r.headers["content-security-policy"], "default-src 'none'")
        self.assertEqual(r.headers["referrer-policy"], "no-referrer")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")


class ParseHost(unittest.TestCase):
    def test_what_a_host_header_means(self):
        for value, name in (("localhost", "localhost"), ("LocalHost:8040", "localhost"), ("localhost.", "localhost"),
                            ("127.0.0.1:8123", "127.0.0.1"), ("[::1]:8040", "::1"), ("[::1]", "::1"),
                            ("[2001:DB8::1]:80", "2001:db8::1"), ("pi.local", "pi.local"), ("a-b.c_d.example:443", "a-b.c_d.example")):
            self.assertEqual(hosts.parse_host(value), name, value)
        for value in ("", " ", "evil.example:", "evil.example:abc", "evil.example:123456", "user@evil.example", "a b",
                      "evil.example/x", "evil.example?x", "evil.example#x", "[::1", "[::1]x", "[not-an-address]", "::1",
                      "-a.example", "a..example", ".example", "a" * 64 + ".example", "x" * 300, "ev\til.example",
                      "evil.example\r\nX: y", "ünï.example", "*.example"):
            self.assertIsNone(hosts.parse_host(value), repr(value))


class Allowlist(unittest.TestCase):
    ENV = {"JOBHUNT_TESTING": "", "PUBLIC_BASE_URL": "", "JOBHUNT_ALLOWED_HOSTS": ""}

    def setUp(self):
        hosts.machine_names.cache_clear()
        self.addCleanup(hosts.machine_names.cache_clear)
        for patch in (mock.patch("socket.gethostname", return_value="Pi-Zqx.lan"),
                      mock.patch("socket.getfqdn", return_value="pi-zqx.home.example")):
            patch.start()
            self.addCleanup(patch.stop)

    def allowed(self, host: str, **env) -> bool:
        with mock.patch.dict(os.environ, {**self.ENV, **env}):
            return hosts.is_allowed(host)

    def test_addresses_localhost_and_this_computers_names(self):
        for host in ("127.0.0.1", "127.0.0.1:8123", "192.168.1.20:8040", "10.0.0.5", "100.101.102.103:8040", "[::1]:8040",
                     "[fe80::1]", "[2001:db8::1]:8040", "0.0.0.0",
                     "localhost", "localhost:8040", "LOCALHOST", "app.localhost:8040", "a.b.localhost", "localhost.",
                     "pi-zqx.lan", "pi-zqx.lan:8040", "pi-zqx", "pi-zqx.local", "pi-zqx.lan.local", "pi-zqx.home.example",
                     "PI-ZQX.HOME.EXAMPLE"):
            self.assertTrue(self.allowed(host), host)

    def test_other_names_are_refused_the_way_a_rebinding_attack_asks(self):
        for host in ("evil.example", "evil.example:8040", "127.0.0.1.evil.example", "localhost.evil.example",
                     "evil-localhost", "xlocalhost", "pi-zqx.evil.example", "evil.pi-zqx.lan", "pi.local", "local",
                     "192.168.1.20.nip.io", "127.1", "2130706433", "0x7f.0.0.1", "testserver", "", "user@localhost",
                     "localhost@evil.example", "evil.example\\localhost"):
            self.assertFalse(self.allowed(host), repr(host))

    def test_testserver_only_while_testing(self):
        self.assertFalse(self.allowed("testserver"))
        self.assertTrue(self.allowed("testserver", JOBHUNT_TESTING="1"))
        self.assertTrue(self.allowed("testserver:80", JOBHUNT_TESTING="1"))
        self.assertFalse(self.allowed("other-test-name", JOBHUNT_TESTING="1"))

    def test_the_public_address(self):
        self.assertFalse(self.allowed("jobs.example.com"))
        for url in ("https://jobs.example.com", "https://jobs.example.com/app", "http://jobs.example.com:8040",
                    "https://Jobs.Example.com"):
            self.assertTrue(self.allowed("jobs.example.com", PUBLIC_BASE_URL=url), url)
            self.assertTrue(self.allowed("jobs.example.com:8443", PUBLIC_BASE_URL=url), url)
            self.assertFalse(self.allowed("www.jobs.example.com", PUBLIC_BASE_URL=url), url)
            self.assertFalse(self.allowed("evil.example", PUBLIC_BASE_URL=url), url)
        self.assertFalse(self.allowed("jobs.example.com", PUBLIC_BASE_URL="not a url"))

    def test_the_extra_names_setting(self):
        value = " host.tail1234.ts.net ,Other.Example,, bad host, http://url.example, 10.1.2.3, under_score.example,*.wild.example"
        for host in ("host.tail1234.ts.net", "host.tail1234.ts.net:8040", "other.example", "under_score.example"):
            self.assertTrue(self.allowed(host, JOBHUNT_ALLOWED_HOSTS=value), host)
        for host in ("bad", "host", "url.example", "x.wild.example", "wild.example", "tail1234.ts.net", "evil.example"):
            self.assertFalse(self.allowed(host, JOBHUNT_ALLOWED_HOSTS=value), host)
        self.assertEqual(hosts.configured_names({"JOBHUNT_ALLOWED_HOSTS": value}),
                         {"host.tail1234.ts.net", "other.example", "under_score.example"})
        self.assertFalse(self.allowed("host.tail1234.ts.net"))  # nothing set: nothing extra

    def test_a_bad_entry_is_said_once_and_the_name_is_not_in_the_log(self):
        hosts._warned.clear()
        with self.assertLogs("backend.hosts", "WARNING") as logs:
            for _ in range(3):
                self.allowed("x.example", JOBHUNT_ALLOWED_HOSTS="http://secret-looking.example/path,evil.example")
        self.assertEqual(len(logs.records), 1)
        self.assertNotIn("secret-looking", logs.output[0])


class Guard(unittest.TestCase):
    """Through the whole app: the Host check comes before sessions, rate limits, routes and files."""

    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)
        hosts._warned.clear()

    def client(self, base_url: str) -> TestClient:
        c = TestClient(app, base_url=base_url)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def test_what_works_and_what_does_not(self):
        for base in ("http://127.0.0.1:8123", "http://localhost:8040", "http://[::1]:8040", "http://192.168.1.20:8040",
                     "http://app.localhost", "http://testserver"):
            r = self.client(base).get("/api/health")
            self.assertEqual((r.status_code, r.json()), (200, {"ok": True}), base)
            self.assertEqual(self.client(base).get("/login.html").status_code, 200, base)
        for base in ("http://evil.example", "http://127.0.0.1.evil.example:8040", "https://localhost.evil.example"):
            for path in ("/api/health", "/login.html", "/", "/styles.css", "/api/jobs"):
                r = self.client(base).get(path)
                self.assertEqual(r.status_code, 400, (base, path))
                self.assertEqual(r.headers["content-type"], "text/plain; charset=utf-8")
                self.assertIn("isn't allowed", r.text)
                self.assertIn("JOBHUNT_ALLOWED_HOSTS", r.text)
                self.assertEqual(r.headers["cache-control"], "no-store")

    def test_the_message_names_the_host_only_when_it_is_a_plain_name(self):
        c = self.client("http://testserver")
        self.assertIn("(evil.example)", c.get("/", headers={"Host": "evil.example:8040"}).text)
        r = c.get("/", headers={"Host": "<script>x</script>"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("script", r.text)

    def test_it_comes_before_everything_else(self):
        c = self.client("http://evil.example")
        # Not a sign-in attempt, not a rate-limit hit, not a missing session: the same 400 every time.
        for _ in range(30):
            r = c.post("/api/auth/login", json={"email": "a@example.invalid", "password": "x" * 12})
            self.assertEqual(r.status_code, 400)
        self.assertEqual(c.post("/api/search/run").status_code, 400)  # (not 403 bad_origin, not 401)
        self.assertEqual(c.get("/api/auth/me", headers={"Origin": "http://evil.example"}).status_code, 400)
        # ... and none of it counted against a real sign-in from a good address.
        good = self.client("http://127.0.0.1:8123")
        r = good.post("/api/auth/login", json={"email": helpers.ADMIN_EMAIL, "password": helpers.ADMIN_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_rebinding_page_cannot_use_the_sign_in_endpoints_with_forged_client_addresses(self):
        """DNS rebinding: the page's Origin and Host are both its own name, so the Origin rule passes. X-Forwarded-For
        is not a forbidden header for fetch, and the launcher trusts it from this computer, so every request could
        name a fresh client address and never meet the per-address limit. The Host check refuses all of it first."""
        c = self.client("http://evil.example:8123")
        body = {"email": helpers.ADMIN_EMAIL, "password": "x" * 12}
        for i in range(14):
            headers = {"Origin": "http://evil.example:8123", "X-Forwarded-For": f"203.0.113.{i}"}
            for path, payload in (("/api/auth/login", body), ("/api/auth/recover", {**body, "recovery_code": "x"}),
                                  ("/api/auth/first-run", {"token": "x", **body})):
                r = c.post(path, json=payload, headers=headers)
                self.assertEqual(r.status_code, 400, (path, i, r.text))
        self.assertEqual(ratelimit.AUTH.hit("testclient"), None)  # nothing was counted for anyone

    def test_a_refused_host_is_logged_once_by_name(self):
        c = self.client("http://testserver")
        with self.assertLogs("backend.hosts", "WARNING") as logs:
            for _ in range(5):
                c.get("/", headers={"Host": "evil.example"})
        self.assertEqual(len(logs.records), 1)
        self.assertIn("evil.example", logs.output[0])

    def test_the_settings_open_it_up(self):
        c = self.client("http://jobs.example.com")
        self.assertEqual(c.get("/api/health").status_code, 400)
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://jobs.example.com"}):
            self.assertEqual(c.get("/api/health").status_code, 200)
        with mock.patch.dict(os.environ, {"JOBHUNT_ALLOWED_HOSTS": "jobs.example.com"}):
            self.assertEqual(c.get("/api/health").status_code, 200)
        self.assertEqual(c.get("/api/health").status_code, 400)  # back as it was: nothing was remembered

    def test_the_test_client_is_allowed_only_while_testing(self):
        c = self.client("http://testserver")
        self.assertEqual(c.get("/api/health").status_code, 200)
        env = {k: v for k, v in os.environ.items() if k != "JOBHUNT_TESTING"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(c.get("/api/health").status_code, 400)

    def test_the_machines_own_names_work_through_the_app(self):
        hosts.machine_names.cache_clear()
        self.addCleanup(hosts.machine_names.cache_clear)
        with mock.patch("socket.gethostname", return_value="raspberrypi"), mock.patch("socket.getfqdn",
                                                                                      return_value="raspberrypi"):
            hosts.machine_names.cache_clear()
            self.assertEqual(self.client("http://raspberrypi:8040").get("/api/health").status_code, 200)
            self.assertEqual(self.client("http://raspberrypi.local:8040").get("/api/health").status_code, 200)
            self.assertEqual(self.client("http://other-pi:8040").get("/api/health").status_code, 400)

    def test_this_computers_real_names_are_allowed(self):
        names = hosts.machine_names()
        self.assertTrue(names, "socket.gethostname() should give this computer a name")
        for name in names:
            self.assertTrue(hosts.is_allowed(name), name)
        short = socket.gethostname().lower().split(".")[0] + ".local"
        if hosts.NAME_RE.fullmatch(short):
            self.assertIn(short, names)


class StartUp(unittest.TestCase):
    def test_the_launchers_and_ci_reach_the_app_by_address(self):
        """installer/launch.py (which every launcher and the Windows program go through) and CI's checks call the app
        at http://127.0.0.1:<port>: an address, so always allowed."""
        root = Path(__file__).resolve().parent.parent
        launch = (root / "installer/launch.py").read_text(encoding="utf-8")
        self.assertRegex(launch, r'(?m)^HOST = "127\.0\.0\.1"$')
        ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        called = set(re.findall(r"http://([^:/\s'\"]+)(?::\d+)?/", ci))
        self.assertEqual(called, {"127.0.0.1"})
        self.assertTrue(hosts.is_allowed("127.0.0.1:8123"))


if __name__ == "__main__":
    unittest.main()
