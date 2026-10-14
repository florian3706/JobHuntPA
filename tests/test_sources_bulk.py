"""Careers pages in bulk (docs/design/open-source.md §1.14): the CSV reader, the address flags (one test per rule),
the preview that fetches nothing, the add through the single add's path, the export, and who may call them.

Run:  python3 -m unittest tests.test_sources_bulk -v
"""
from __future__ import annotations

import contextlib
import socket
import unittest
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import source_bulk  # noqa: E402
from backend.app import app  # noqa: E402
from backend.scraping import http as scraping_http  # noqa: E402
from backend.source_bulk import address_flags, parse_csv, to_csv  # noqa: E402

ENDPOINTS = (("post", "/api/sources/bulk/preview", {"text": "Acme,https://careers.acme.example/jobs"}),
             ("post", "/api/sources/bulk", {"rows": [{"company": "Acme", "url": "https://careers.acme.example/jobs"}]}),
             ("get", "/api/sources/export", None))


def codes(url: str) -> list[str]:
    return [f.code for f in address_flags(url).flags]


@contextlib.contextmanager
def no_network():
    """Every way out of this process refused, and counted: a name look-up, a socket, an HTTP request (httpx, and the
    scraping layer's own client). Yields the mocks, so a test can assert none of them was called."""
    boom = OSError("network use in a test that must make none")
    with mock.patch.object(socket, "getaddrinfo", side_effect=boom) as getaddrinfo, \
            mock.patch.object(socket, "gethostbyname", side_effect=boom) as gethostbyname, \
            mock.patch.object(socket, "create_connection", side_effect=boom) as create_connection, \
            mock.patch.object(httpx.Client, "send", side_effect=boom) as send, \
            mock.patch.object(scraping_http.PoliteClient, "request", side_effect=boom) as request, \
            mock.patch.object(scraping_http.PoliteClient, "robots_for", side_effect=boom) as robots:
        yield (getaddrinfo, gethostbyname, create_connection, send, request, robots)


class AddressFlagsTest(unittest.TestCase):
    """address_flags: one test per rule. The rules judge the shape of an address only."""

    def test_an_ordinary_careers_page_has_no_flags(self):
        for url in ("https://careers.example.com/jobs", "https://jobs.lever.co/acme", "https://x.com:443/jobs",
                    "https://exa_mple.com/", "https://example.com./jobs"):
            check = address_flags(url)
            self.assertEqual((check.flags, check.blocked), ([], False), url)

    def test_an_address_without_a_scheme_gets_https_as_a_single_add_does(self):
        self.assertEqual(address_flags("example.com/jobs").url, "https://example.com/jobs")
        self.assertEqual(address_flags("  //example.com/jobs ").url, "https://example.com/jobs")
        from backend.sources import normalize_url
        self.assertEqual(normalize_url("example.com/jobs"), address_flags("example.com/jobs").url)

    def test_not_a_web_link_is_refused(self):
        for url in ("", "mailto:jobs@example.com", "javascript:alert(1)", "data:text/html,hi", "tel:+61200000000",
                    "ftp://example.com/jobs", "file:///etc/passwd", "https://", "http:example.com",
                    "https://exa mple.com/", "https:\\\\example.com\\jobs", "https://example.com/\tjobs",
                    "https://example.com:99999/", "https://a@b@example.com/", "https://ex*ample.com/",
                    "https://[zz]/", "https://example.123/", "https://1.2.3.4.5/", "https://x.com\uff0f.y.org/",
                    "chrome://settings", "https://example..com/"):
            check = address_flags(url)
            self.assertIn("not_web", codes(url), url)
            self.assertTrue(check.blocked, url)

    def test_a_raw_ip_address_is_flagged(self):
        for url in ("https://8.8.8.8/jobs", "https://[2606:4700:4700::1111]/"):
            check = address_flags(url)
            self.assertEqual(codes(url), ["ip"], url)
            self.assertFalse(check.blocked, url)

    def test_the_local_network_or_this_computer_is_refused(self):
        for url in ("https://127.0.0.1/", "http://localhost:8000/", "localhost", "https://app.localhost/",
                    "https://10.1.2.3/", "https://172.16.0.9/", "https://192.168.1.1/admin", "https://169.254.169.254/",
                    "https://100.64.0.1/", "https://0.0.0.0/", "https://224.0.0.1/", "https://[::1]/",
                    "https://[fe80::1]/", "https://[fd00::1]/", "https://[::ffff:127.0.0.1]/",
                    "https://[64:ff9b::7f00:1]/", "https://[2002:c0a8:101::]/",
                    # the forms a browser or the C library reads as 127.0.0.1
                    "http://2130706433/", "http://127.1/", "http://0x7f.0.0.1/", "http://0177.0.0.1/",
                    "https://%31%32%37.0.0.1/", "https://\uff11\uff12\uff17.0.0.1/",
                    # names that only mean something on a local network
                    "https://router/", "https://nas.local/jobs", "https://hr.corp/", "https://wiki.internal/",
                    "https://printer.lan/", "https://box.home.arpa/", "https://intranet.localdomain/"):
            check = address_flags(url)
            self.assertIn("local", codes(url), url)
            self.assertTrue(check.blocked, url)

    def test_sign_in_details_are_flagged_and_the_real_site_named(self):
        check = address_flags("https://careers.example.com@evil.example/jobs")
        self.assertEqual(codes(check.url), ["credentials"])
        self.assertIn("evil.example", check.flags[0].reason)
        self.assertFalse(check.blocked)
        self.assertEqual(codes("https://user:secret@example.com/"), ["credentials"])

    def test_a_link_shortener_is_flagged(self):
        for url in ("https://bit.ly/3abc", "https://www.tinyurl.com/x", "https://t.co/x", "https://lnkd.in/x"):
            self.assertEqual(codes(url), ["shortener"], url)
            self.assertFalse(address_flags(url).blocked)

    def test_a_lookalike_or_encoded_name_is_flagged(self):
        self.assertEqual(codes("https://xn--pple-43d.com/"), ["lookalike"], "punycode")
        mixed = address_flags("https://\u0430pple.com/jobs")  # a Cyrillic "a"
        self.assertEqual([f.code for f in mixed.flags], ["lookalike"])
        self.assertIn("Cyrillic", mixed.flags[0].reason)
        self.assertIn("Latin", mixed.flags[0].reason)
        self.assertEqual(codes("https://\u0430\u0440\u0440\u04cf\u0435.com/"), ["lookalike"], "a whole-script lookalike")
        self.assertEqual(codes("https://%65xample.com/"), ["lookalike"], "percent-encoding in the name")
        self.assertEqual(codes("https://example\u3002com/"), ["lookalike"], "an ideographic full stop")
        self.assertFalse(address_flags("https://xn--pple-43d.com/").blocked)

    def test_plain_http_is_flagged(self):
        self.assertEqual(codes("http://careers.example.com/"), ["http"])
        self.assertFalse(address_flags("http://careers.example.com/").blocked)

    def test_an_odd_port_is_flagged(self):
        self.assertEqual(codes("https://careers.example.com:8443/"), ["port"])
        self.assertEqual(codes("http://careers.example.com:80/"), ["http"], "http's own port is not odd")
        self.assertEqual(codes("https://careers.example.com:443/"), [], "nor is https's")
        self.assertIn("8443", address_flags("https://careers.example.com:8443/").flags[0].reason)

    def test_several_flags_at_once(self):
        self.assertEqual(codes("http://u:p@8.8.8.8:8080/"), ["credentials", "ip", "http", "port"])

    def test_the_rules_use_no_network_not_even_a_name_look_up(self):
        with no_network() as calls:
            for url in ("https://careers.example.com/jobs", "https://bit.ly/x", "https://\u0430pple.com/",
                        "https://localhost/", "http://2130706433/", "https://8.8.8.8:8080/", "https://u@x.com/"):
                address_flags(url)
        for call in calls:
            call.assert_not_called()


class CsvTest(unittest.TestCase):
    def test_names_with_commas_and_quotes(self):
        rows = parse_csv('"Smith, Jones & Co",https://sj.example/jobs\n'
                         '"The ""Best"" Bakery, Pty Ltd",https://bakery.example/careers\n')
        self.assertEqual([(r.company, r.url, r.problem) for r in rows],
                         [("Smith, Jones & Co", "https://sj.example/jobs", ""),
                          ('The "Best" Bakery, Pty Ltd', "https://bakery.example/careers", "")])

    def test_the_header_is_optional_and_may_list_the_columns_in_either_order(self):
        body = "Acme,https://acme.example/jobs\n"
        self.assertEqual([(r.company, r.url) for r in parse_csv(body)], [("Acme", "https://acme.example/jobs")])
        self.assertEqual([(r.company, r.url) for r in parse_csv("company,url\n" + body)],
                         [("Acme", "https://acme.example/jobs")])
        self.assertEqual([(r.company, r.url) for r in parse_csv("URL , Company\nhttps://acme.example/jobs,Acme\n")],
                         [("Acme", "https://acme.example/jobs")])

    def test_utf8_bom_crlf_blank_lines_and_line_numbers(self):
        rows = parse_csv("\ufeffcompany,url\r\n\r\nCaf\u00e9 Zo\u00eb,https://cafe.example/jobs\r\n\r\nB,b.example\r\n")
        self.assertEqual([(r.line, r.company, r.url) for r in rows],
                         [(3, "Caf\u00e9 Zo\u00eb", "https://cafe.example/jobs"), (5, "B", "b.example")])

    def test_rows_that_cannot_be_read_say_why(self):
        rows = parse_csv("Smith, Jones & Co,https://sj.example/\nOnly a name\n,\nAcme,\n" + "A," + "x" * 3000 + "\n")
        self.assertIn("3 columns", rows[0].problem)
        self.assertIn("double quotes", rows[0].problem)
        self.assertEqual(rows[1].problem, "It has a company but no address.")
        self.assertEqual(rows[2].problem, "It has a company but no address.")
        self.assertIn("longer than", rows[3].problem)
        self.assertEqual(len(rows), 4, "a row of empty cells is a blank line")

    def test_an_address_alone_is_a_row_without_a_name(self):
        self.assertEqual([(r.company, r.url, r.problem) for r in parse_csv("https://acme.example/jobs\n")],
                         [("", "https://acme.example/jobs", "")])

    def test_a_name_is_kept_on_one_line(self):
        self.assertEqual(parse_csv('"Acme\nHoldings\x07",https://acme.example/\n')[0].company, "Acme Holdings")

    def test_limits_and_text_that_is_not_csv(self):
        with self.assertRaises(source_bulk.FormatError):
            parse_csv("a,https://a.example/\n" * (source_bulk.MAX_ROWS + 1))
        with self.assertRaises(source_bulk.FormatError):
            parse_csv("x" * (source_bulk.MAX_TEXT + 1))
        with self.assertRaises(source_bulk.FormatError):
            parse_csv("a,b\x00c\n")
        self.assertEqual(len(parse_csv("a,https://a.example/\n" * source_bulk.MAX_ROWS)), source_bulk.MAX_ROWS)

    def test_the_export_reads_back_and_keeps_spreadsheets_from_running_formulas(self):
        rows = [("Smith, Jones & Co", "https://sj.example/jobs"), ('Say "hi"', "https://hi.example/"),
                ("=HYPERLINK(\"https://evil.example\")", "https://f.example/"), ("-minus", "https://m.example/"),
                ("@at", "https://a.example/"), ("Zo\u00eb", "https://z.example/")]
        text = to_csv(rows)
        self.assertTrue(text.startswith("company,url\r\n"))
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("'-minus", text)
        self.assertEqual([(r.company, r.url) for r in parse_csv(text)], rows)


class BulkEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.acct = helpers.make_user()
        self.c = helpers.client(self.acct.email)
        self.c.__enter__()
        self.addCleanup(self.c.__exit__, None, None, None)

    def sources(self, c=None, ws=None):
        r = (c or self.c).get("/api/sources", headers={"X-Workspace": str(ws)} if ws else {})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def preview(self, text, **kw):
        r = self.c.post("/api/sources/bulk/preview", json={"text": text}, **kw)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["rows"]

    def add(self, rows, **kw):
        r = self.c.post("/api/sources/bulk", json={"rows": rows}, **kw)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_the_preview_reads_and_flags_and_fetches_nothing(self):
        text = ('company,url\n"Smith, Jones & Co",https://careers.sj.example/jobs\n'
                "Shortened,https://bit.ly/abc\nRouter,http://192.168.1.1/\nMail,mailto:jobs@x.example\n"
                "Again,https://careers.sj.example/jobs\nBroken, Co, https://b.example/\n")
        with no_network() as calls:
            rows = self.preview(text)
        for call in calls:
            call.assert_not_called()
        self.assertEqual(self.sources(), [], "the preview stores nothing")
        self.assertEqual([(r["line"], r["status"]) for r in rows],
                         [(2, "new"), (3, "new"), (4, "refused"), (5, "refused"), (6, "repeat"), (7, "unreadable")])
        self.assertEqual(rows[0]["company"], "Smith, Jones & Co")
        self.assertEqual(rows[0]["flags"], [])
        self.assertEqual([f["code"] for f in rows[1]["flags"]], ["shortener"])
        self.assertFalse(rows[1]["blocked"])
        self.assertTrue(rows[2]["blocked"])
        self.assertIn("local", [f["code"] for f in rows[2]["flags"]])
        self.assertEqual(rows[4]["repeat_of"], 2)
        self.assertIn("double quotes", rows[5]["problem"])

    def test_the_preview_refuses_text_that_is_not_csv(self):
        r = self.c.post("/api/sources/bulk/preview", json={"text": "a,b\x00"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("NUL", r.json()["detail"])

    def test_confirming_adds_new_leaves_existing_and_ignores_repeats(self):
        made = self.c.post("/api/sources", json={"url": "https://jobs.lever.co/acme", "label": "Acme (mine)"}).json()
        self.assertEqual(self.c.patch(f"/api/sources/{made['id']}", json={"enabled": False}).status_code, 200)
        before = self.sources()
        with no_network() as calls:
            body = self.add([{"company": "Acme renamed", "url": "https://jobs.lever.co/acme"},
                             {"company": "Smith, Jones & Co", "url": "careers.sj.example/jobs"},
                             {"company": "Smith again", "url": "https://careers.sj.example/jobs"},
                             {"company": "", "url": "https://apply.workable.com/bakery/"},
                             {"company": "Too long", "url": "https://x.example/" + "a" * 3000}])
        for call in calls:
            call.assert_not_called()
        self.assertEqual([r["result"] for r in body["results"]], ["already", "added", "repeat", "added", "unreadable"])
        self.assertEqual(body["counts"], {"added": 2, "already": 1, "repeat": 1, "refused": 0, "unreadable": 1})
        after = self.sources()
        self.assertEqual(after[0], before[0], "the existing source is left exactly as it was")
        self.assertEqual([(s["label"], s["url"], s["enabled"]) for s in after[1:]],
                         [("Smith, Jones & Co", "https://careers.sj.example/jobs", True),
                          ("Bakery", "https://apply.workable.com/bakery/", True)])
        self.assertEqual(body["results"][1]["id"], after[1]["id"])

    def test_a_local_network_address_is_refused_outright(self):
        for url in ("http://192.168.1.1/", "https://localhost:8000/jobs", "http://2130706433/", "https://nas.local/",
                    "https://[::1]/", "javascript:alert(1)"):
            body = self.add([{"company": "Local", "url": url}])
            self.assertEqual(body["results"][0]["result"], "refused", url)
            self.assertTrue(body["results"][0]["detail"], url)
        self.assertEqual(self.sources(), [])

    def test_a_flagged_address_the_person_ticked_is_added(self):
        body = self.add([{"company": "Short", "url": "https://bit.ly/abc"},
                         {"company": "Plain", "url": "http://careers.plain.example/"}])
        self.assertEqual([r["result"] for r in body["results"]], ["added", "added"])

    def test_too_many_rows_are_refused(self):
        rows = [{"company": "", "url": f"https://c{i}.example/"} for i in range(source_bulk.MAX_ROWS + 1)]
        r = self.c.post("/api/sources/bulk", json={"rows": rows})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.sources(), [])

    def test_export_then_import_gives_the_same_list(self):
        for url, label in (("https://careers.sj.example/jobs", "Smith, Jones & Co"),
                           ("https://bakery.example/careers", 'The "Best" Bakery'),
                           ("https://jobs.lever.co/acme", None), ("http://plain.example/jobs", "=Formula Pty"),
                           ("https://caf\u00e9.example/jobs", "Caf\u00e9 Zo\u00eb")):
            self.assertEqual(self.c.post("/api/sources", json={"url": url, "label": label}).status_code, 201)
        r = self.c.get("/api/sources/export")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["content-type"], "text/csv; charset=utf-8")
        self.assertIn('attachment; filename="sources.csv"', r.headers["content-disposition"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        text = r.content.decode("utf-8")
        self.assertTrue(text.startswith("company,url\r\n"))
        second = self.c.post("/api/workspaces", json={"name": "Copy"}).json()["id"]
        h = {"X-Workspace": str(second)}
        rows = self.preview(text, headers=h)
        self.assertEqual([r["status"] for r in rows], ["new"] * 5)
        kept = [{"company": r["company"], "url": r["url"]} for r in rows]  # flagged ones ticked by the person
        body = self.add(kept, headers=h)
        self.assertEqual(body["counts"]["added"], 5)
        pairs = lambda srcs: [(s["label"], s["url"]) for s in srcs]  # noqa: E731
        self.assertEqual(pairs(self.sources(ws=second)), pairs(self.sources()))
        self.assertEqual(self.c.get("/api/sources/export", headers=h).text, text)
        # and into the workspace it came from: every row is already there, nothing changes
        self.assertEqual([r["status"] for r in self.preview(text)], ["already"] * 5)
        self.assertEqual(self.add(kept)["counts"]["already"], 5)
        self.assertEqual(len(self.sources()), 5)

    def test_an_empty_workspace_exports_the_header(self):
        self.assertEqual(self.c.get("/api/sources/export").text, "company,url\r\n")

    def test_signed_out_is_refused(self):
        nobody = TestClient(app)
        nobody.__enter__()
        self.addCleanup(nobody.__exit__, None, None, None)
        for method, path, body in ENDPOINTS:
            r = getattr(nobody, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 401, f"{method} {path}")
        self.assertEqual(self.sources(), [])

    def test_another_accounts_workspace_is_refused(self):
        other = helpers.make_user()
        h = {"X-Workspace": str(other.workspace_id)}
        for method, path, body in ENDPOINTS:
            r = getattr(self.c, method)(path, headers=h, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 404, f"{method} {path}")
        with helpers.client(other.email) as oc:
            self.assertEqual(self.sources(oc), [], "nothing was added to the other account's workspace")
            r = oc.post("/api/sources/bulk", json=ENDPOINTS[1][2], headers={"X-Workspace": str(self.acct.workspace_id)})
            self.assertEqual(r.status_code, 404, "nor the other way round")
        for method, path, body in ENDPOINTS:  # the owner may
            r = getattr(self.c, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")

    def test_an_extension_key_is_refused(self):
        key = self.c.post("/api/extension/keys", json={"label": "Laptop"}).json()["key"]
        k = TestClient(app)
        k.__enter__()
        self.addCleanup(k.__exit__, None, None, None)
        k.headers["Authorization"] = f"Bearer {key}"
        for method, path, body in ENDPOINTS:
            r = getattr(k, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual((r.status_code, r.json().get("code")), (403, "token_scope"), f"{method} {path}")
        self.assertEqual(self.sources(), [])

    def test_the_posts_need_the_csrf_token(self):
        token = self.c.headers.pop("X-CSRF-Token")
        try:
            for method, path, body in ENDPOINTS[:2]:
                r = self.c.post(path, json=body)
                self.assertEqual(r.status_code, 403, path)
        finally:
            self.c.headers["X-CSRF-Token"] = token
        self.assertEqual(self.sources(), [])


if __name__ == "__main__":
    unittest.main()
