"""Who may call every endpoint (CLAUDE.md: "Every new endpoint states who may call it ... including the refusal").

ACCESS below names each /api route of the app and its access: ``open`` (no session), ``token`` (the first run's
one-time token, no session), ``signed_in``, ``recent`` (a password entered in the last 15 minutes) and ``key`` (a
browser-extension key may call it too, besides a signed-in browser). The tests walk the app's routes, included routers
too (``helpers.api_routes``), so a new endpoint fails here until it is listed, and check the refusal of each kind:
signed out, every route but the open ones answers 401; with an extension key, every route but the key ones answers 403
``token_scope``; with a stale sign-in, every recent route answers 403 ``reauth_required`` and changes nothing.
"""
from __future__ import annotations

import re
import unittest
from datetime import timedelta

from tests import helpers

from fastapi.testclient import TestClient  # noqa: E402

from backend import crypto, ratelimit, sessions  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import AuthSession, SessionLocal, utcnow  # noqa: E402

OPEN, TOKEN, SIGNED_IN, RECENT, KEY = "open", "token", "signed_in", "recent", "key"

ACCESS = {
    # Open, and the first run (the one-time token in the body; refused once the account or the folder exists).
    ("GET", "/api/health"): OPEN, ("GET", "/api/auth/state"): OPEN, ("POST", "/api/auth/login"): OPEN,
    ("POST", "/api/auth/logout"): OPEN, ("POST", "/api/auth/recover"): OPEN,
    ("POST", "/api/auth/first-run"): TOKEN, ("POST", "/api/auth/data-folder/check"): TOKEN,
    ("POST", "/api/auth/data-folder"): TOKEN,
    # A recent sign-in: what could lock the person out or hand their data to someone else.
    ("POST", "/api/account/background"): RECENT, ("POST", "/api/account/delete"): RECENT,
    ("POST", "/api/account/export"): RECENT, ("POST", "/api/account/import"): RECENT,
    ("POST", "/api/account/import/check"): RECENT, ("PUT", "/api/auth/name"): RECENT,
    ("POST", "/api/auth/recovery-codes"): RECENT, ("POST", "/api/extension/keys"): RECENT,
    ("PUT", "/api/settings/address"): RECENT, ("POST", "/api/settings/leftovers/delete"): RECENT,
    ("POST", "/api/settings/server-key/rotate"): RECENT,
    # An extension key too (sessions.KEY_ENDPOINTS), or a signed-in browser.
    ("GET", "/api/extension/status"): KEY, ("POST", "/api/extension/preview"): KEY,
    ("POST", "/api/extension/jobs"): KEY, ("POST", "/api/extension/jobs/batch"): KEY,
    ("POST", "/api/extension/work-out"): KEY, ("GET", "/api/extension/mappings"): KEY,
    ("GET", "/api/extension/mappings/{site}"): KEY, ("PUT", "/api/extension/mappings/{site}"): KEY,
    ("DELETE", "/api/extension/mappings/{site}"): KEY, ("GET", "/api/sources"): KEY,
    ("POST", "/api/sources"): KEY, ("POST", "/api/sources/{source_id}/test"): KEY,
}
SIGNED_IN_ROUTES = """
GET /api/account/background; GET /api/account/delete; GET /api/account/onboarding; PUT /api/account/onboarding; GET /api/account/prefs;
PUT /api/account/prefs; GET /api/auth/me; POST /api/auth/password; POST /api/auth/reauth;
POST /api/auth/recovery-codes/ack; POST /api/auth/recovery-codes/show; GET /api/auth/security;
POST /api/auth/sessions/end-others; GET /api/chats; POST /api/chats; DELETE /api/chats/{chat_id};
GET /api/chats/{chat_id}; POST /api/chats/{chat_id}/messages; GET /api/companies; POST /api/companies/research;
GET /api/dealbreakers; PUT /api/dealbreakers; GET /api/documents; POST /api/documents; POST /api/documents/notes;
POST /api/documents/suggest-titles; POST /api/documents/summary; DELETE /api/documents/{doc_id};
PATCH /api/documents/{doc_id}; POST /api/documents/{doc_id}/concise; GET /api/documents/{doc_id}/text;
GET /api/duplicates; POST /api/duplicates/decide; POST /api/duplicates/scan; GET /api/encryption/status;
GET /api/extension/keys; DELETE /api/extension/keys/{key_id}; GET /api/geo/lookup; POST /api/import/page;
GET /api/jobs; PATCH /api/jobs/hidden; POST /api/jobs/merge; POST /api/jobs/refilter; PATCH /api/jobs/status;
GET /api/jobs/{job_id}; GET /api/jobs/{job_id}/commute;
GET /api/jobs/{job_id}/cover-letter; POST /api/jobs/{job_id}/cover-letter; PUT /api/jobs/{job_id}/cover-letter;
GET /api/jobs/{job_id}/cover-letter.docx; POST /api/jobs/{job_id}/import-page; PUT /api/jobs/{job_id}/office;
PATCH /api/jobs/{job_id}/status; POST /api/jobs/{job_id}/unmerge; GET /api/llm; POST /api/llm/detect-levels;
PUT /api/llm/reasoning; GET /api/pins; POST /api/pins; DELETE /api/pins/{pin_id}; PUT /api/pins/{pin_id};
GET /api/profile; PUT /api/profile; GET /api/runs/latest; GET /api/runs/{run_id}; GET /api/schedule;
PUT /api/schedule; POST /api/score/run; GET /api/score/status; POST /api/score/test; POST /api/score/{job_id};
POST /api/search/run; GET /api/settings/address; GET /api/settings/browser; POST /api/settings/browser/decline;
POST /api/settings/browser/install; GET /api/settings/leftovers; GET /api/settings/llm; PUT /api/settings/llm;
GET /api/settings/usage; POST /api/sources/bulk; POST /api/sources/bulk/preview; GET /api/sources/export;
DELETE /api/sources/{source_id}; PATCH /api/sources/{source_id}; GET /api/tasks; GET /api/workspaces;
POST /api/workspaces; DELETE /api/workspaces/{ws_id}; PATCH /api/workspaces/{ws_id}
"""
for _item in SIGNED_IN_ROUTES.split(";"):
    _method, _path = _item.split()
    ACCESS[(_method, _path)] = SIGNED_IN


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path.replace("{site}", "jobs.example.com"))


def call(client: TestClient, method: str, path: str, body=None, **kw):
    if method in ("POST", "PUT", "PATCH"):
        kw["json"] = {} if body is None else body
    return client.request(method, concrete(path), **kw)


class RouteListTest(unittest.TestCase):
    def test_every_route_states_its_access(self):
        found = {(m, p) for m, p, _ in helpers.api_routes() if p.startswith("/api") and m != "HEAD"}
        self.assertGreater(len(found), 100, "the walk reaches the included routers")
        self.assertEqual(sorted(found - set(ACCESS)), [], "a new endpoint: say who may call it in ACCESS")
        self.assertEqual(sorted(set(ACCESS) - found), [], "an endpoint in ACCESS that no longer exists")

    def test_the_kinds_match_what_the_app_enforces(self):
        for (method, path), kind in ACCESS.items():
            self.assertEqual(path in sessions.OPEN_PATHS, kind in (OPEN, TOKEN), path)
            self.assertEqual(sessions.key_may_call(method, concrete(path)), kind == KEY, f"{method} {path}")


    def test_every_route_that_asks_for_a_recent_sign_in_is_listed_so(self):
        """An endpoint (or a dependency of it) that calls require_recent_auth is a recent route, and the other way round
        (the stale-session test below then checks each of them)."""
        import inspect

        def sources(fn, seen=None) -> str:
            seen = seen if seen is not None else set()
            if fn is None or fn in seen:
                return ""
            seen.add(fn)
            try:
                text = inspect.getsource(fn)
            except (OSError, TypeError):
                return ""
            for param in inspect.signature(fn).parameters.values():
                text += sources(getattr(param.default, "dependency", None), seen)
            return text

        for method, path, endpoint in helpers.api_routes():
            if (method, path) in ACCESS:
                asks = "require_recent_auth(" in sources(endpoint)
                self.assertEqual(asks, ACCESS[(method, path)] == RECENT, f"{method} {path}")


class RefusalTest(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)

    def test_signed_out_only_the_open_routes_answer(self):
        with TestClient(app) as anonymous:
            for (method, path), kind in sorted(ACCESS.items()):
                if kind in (OPEN, TOKEN):
                    continue
                r = call(anonymous, method, path)
                self.assertEqual((r.status_code, r.json().get("code")), (401, "signed_out"), f"{method} {path}")

    def test_an_extension_key_reaches_only_its_routes(self):
        with helpers.client() as c:
            made = c.post("/api/extension/keys", json={"label": "Route check"})
            self.assertEqual(made.status_code, 201, made.text)
            try:
                with TestClient(app) as k:
                    k.headers["Authorization"] = f"Bearer {made.json()['key']}"
                    for (method, path), kind in sorted(ACCESS.items()):
                        r = call(k, method, path)
                        code = (r.status_code, r.json().get("code") if r.headers.get("content-type", "").startswith(
                            "application/json") and isinstance(r.json(), dict) else None)
                        if kind == KEY:
                            self.assertNotIn(r.status_code, (401, 403), f"{method} {path}: {r.text[:200]}")
                        elif kind in (OPEN, TOKEN):
                            continue  # (a key isn't a sign-in: the open routes answer as for anyone)
                        else:
                            self.assertEqual(code, (403, "token_scope"), f"{method} {path}")
            finally:
                c.delete(f"/api/extension/keys/{made.json()['id']}")

    def test_a_stale_sign_in_is_asked_for_the_password_on_every_recent_route(self):
        with helpers.client() as c:
            sid = crypto.session_id(crypto.b64d(c.cookies.get("jh_session")))
            db = SessionLocal()
            try:
                db.get(AuthSession, sid).reauth_at = utcnow() - timedelta(minutes=20)
                db.commit()
            finally:
                db.close()
            bodies = {"/api/account/background": {"enabled": True}, "/api/account/delete": {"password": "x" * 12},
                      "/api/auth/name": {"name": "Somebody else"}, "/api/extension/keys": {"label": "Stale"},
                      "/api/settings/address": {"allowed_hosts": ["stale.example.invalid"]},
                      "/api/account/export": {"formats": ["md"]}}
            for (method, path), kind in sorted(ACCESS.items()):
                if kind != RECENT:
                    continue
                if path.startswith("/api/account/import"):
                    r = c.post(path, files={"file": ("export.zip", b"PK\x05\x06" + bytes(18), "application/zip")})
                else:
                    r = call(c, method, path, bodies.get(path))
                self.assertEqual((r.status_code, r.json().get("code")), (403, "reauth_required"),
                                 f"{method} {path}: {r.text[:200]}")
            self.assertEqual(c.get("/api/auth/me").status_code, 200, "still signed in")
            self.assertEqual(c.get("/api/extension/keys").json(), [], "no key was made")


if __name__ == "__main__":
    unittest.main()
