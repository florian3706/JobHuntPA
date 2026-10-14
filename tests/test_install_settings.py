"""The install's settings and upkeep in the Settings tab (open-source spec §1.4; they were in the admin panel): the AI
provider and reasoning levels, the public address and extra host names, AI usage, the unencrypted
copies left from before encryption and replacing the server key. There are no roles: any signed-in account may use
them, signed out nobody may, and the two that change keys or delete files need a recent password.

AI errors a person sees name the problem (the provider's status and message) but never a key.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import unittest
from datetime import timedelta
from unittest import mock

from tests import helpers  # noqa: F401  (sets the test environment first)

from fastapi.testclient import TestClient  # noqa: E402

from backend import app_settings, config, crypto, docs, key_rotation, keys, llm_providers, migrate_encryption  # noqa: E402
from backend import ratelimit, users  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import (  # noqa: E402
    DB_PATH,
    AuthSession,
    Document,
    Job,
    LlmUsage,
    SessionLocal,
    User,
    UserKeys,
    Workspace,
    utcnow,
)

PASSWORD = "user-password-123"
LLM_ENV = {"LLM_API_KEY": "sk-secret-key-1234", "LLM_BASE_URL": "https://llm.example.com/v1", "LLM_MODEL": "model-xyz"}

# (method, path, body, needs a recent password): every endpoint that was the admin panel's and survives.
ENDPOINTS = (
    ("get", "/api/settings/llm", None, False),
    ("put", "/api/settings/llm", {}, False),
    ("get", "/api/settings/address", None, False),
    ("put", "/api/settings/address", {"allowed_hosts": ["stale.example.invalid"]}, True),
    ("get", "/api/settings/usage", None, False),
    ("get", "/api/settings/leftovers", None, False),
    ("get", "/api/llm", None, False),
    ("put", "/api/llm/reasoning", {}, False),
    ("post", "/api/llm/detect-levels", None, False),
    ("post", "/api/score/test", None, False),
    ("post", "/api/settings/leftovers/delete", None, True),
    ("post", "/api/settings/server-key/rotate", None, True),
)


def db_rows(fn):
    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


class Base(unittest.TestCase):
    def setUp(self):
        helpers.started()
        ratelimit.reset_all()
        self.addCleanup(ratelimit.reset_all)

    def anonymous(self) -> TestClient:
        c = TestClient(app)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def signed_in(self, account: helpers.Account | None = None) -> helpers.SignedInClient:
        c = helpers.client(account.email) if account else helpers.client()
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    @staticmethod
    def make_stale(c: TestClient) -> None:
        sid = crypto.session_id(crypto.b64d(c.cookies.get("jh_session")))
        db = SessionLocal()
        try:
            db.get(AuthSession, sid).reauth_at = utcnow() - timedelta(minutes=20)
            db.commit()
        finally:
            db.close()

    def env_file(self) -> None:
        """A throwaway .env for this test (the settings endpoints write it), and os.environ put back afterwards."""
        saved = {k: os.environ.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "PUBLIC_BASE_URL",
                                                "JOBHUNT_ALLOWED_HOSTS")}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        before = config.ENV_PATH.read_bytes() if config.ENV_PATH.exists() else None
        self.addCleanup(lambda: config.ENV_PATH.write_bytes(before) if before is not None
                        else config.ENV_PATH.unlink(missing_ok=True))


class WhoMayCallTest(Base):
    def test_signed_out_every_one_is_refused(self):
        c = self.anonymous()
        for method, path, body, _ in ENDPOINTS:
            r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 401, f"{method} {path}: {r.text}")

    def test_any_signed_in_account_reads_them_including_an_older_installs_second_one(self):
        self.env_file()
        bob = helpers.make_user()
        for c in (self.signed_in(), self.signed_in(bob)):
            with mock.patch.dict(os.environ, LLM_ENV):
                for method, path, body, recent in ENDPOINTS:
                    if method != "get":
                        continue
                    r = getattr(c, method)(path)
                    self.assertEqual(r.status_code, 200, f"{path}: {r.text}")
                    self.assertNotIn("sk-secret-key-1234", r.text, path)
                r = c.put("/api/settings/llm", json={"model": "model-changed"})
                self.assertEqual((r.status_code, r.json()["model"]), (200, "model-changed"))
                self.assertEqual(c.put("/api/llm/reasoning", json={"scoring": "low"}).status_code, 200)
                self.assertEqual(c.put("/api/settings/address", json={"allowed_hosts": ["pi.example.invalid"]})
                                 .status_code, 200)
                self.assertEqual(c.put("/api/settings/address", json={"allowed_hosts": []}).status_code, 200)

    def test_changing_keys_deleting_files_or_widening_the_address_need_a_recent_password(self):
        bob = helpers.make_user(password=PASSWORD)
        c = self.signed_in(bob)
        self.make_stale(c)
        self.env_file()
        with mock.patch.object(key_rotation, "rotate") as rotate, \
                mock.patch.object(migrate_encryption, "delete_leftovers", return_value=[]) as delete:
            for method, path, body, recent in ENDPOINTS:
                if recent:
                    r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
                    self.assertEqual((r.status_code, r.json()["code"]), (403, "reauth_required"), path)
            rotate.assert_not_called()
            delete.assert_not_called()
            self.assertNotIn("stale.example.invalid", c.get("/api/settings/address").json()["allowed_hosts"])
            self.assertEqual(c.post("/api/auth/reauth", json={"password": PASSWORD}).status_code, 200)
            r = c.put("/api/settings/address", json={"allowed_hosts": ["fresh.example.invalid"]})
            self.assertEqual(r.json()["allowed_hosts"], ["fresh.example.invalid"])
            self.assertEqual(c.put("/api/settings/address", json={"allowed_hosts": []}).status_code, 200)
            rotate.return_value = 0
            self.assertEqual(c.post("/api/settings/server-key/rotate").status_code, 200)
            self.assertEqual(c.post("/api/settings/leftovers/delete").status_code, 200)
            rotate.assert_called_once()
            delete.assert_called_once()

    def test_nothing_here_resets_or_wipes(self):
        routes = {(m, getattr(r, "path", "")) for r in app.routes for m in getattr(r, "methods", ()) or ()}
        for method, path in routes:
            if path.startswith("/api/settings") or path.startswith("/api/llm"):
                self.assertNotRegex(path, r"reset|wipe|start-over|delete-all", path)


class AddressTest(Base):
    def test_the_public_address_and_host_names_are_checked_saved_and_used_at_once(self):
        self.env_file()
        c = self.signed_in()
        for bad in ("http://jobs.example.com", "https://user:pw@jobs.example.com", "https://jobs.example.com/?x",
                    "ftp://jobs.example.com", "https://jobs example.com", "https://jobs.example.com\nX=1"):
            r = c.put("/api/settings/address", json={"public_base_url": bad})
            self.assertEqual(r.status_code, 422, bad)
        for bad in (["http://pi.example"], ["pi.example:8000"], ["10.0.0.1"], ["*.example.com"], ["a b"],
                    ["ok.example"] * 0 + [f"h{n}.example" for n in range(21)], ["pi.example\nX=1"]):
            r = c.put("/api/settings/address", json={"allowed_hosts": bad})
            self.assertEqual(r.status_code, 422, bad)
        r = c.put("/api/settings/address", json={"public_base_url": "https://jobs.example.invalid/",
                                                 "allowed_hosts": ["Pi.Tail1234.ts.net.", "pi.tail1234.ts.net"]})
        self.assertEqual(r.json(), {"public_base_url": "https://jobs.example.invalid",
                                    "allowed_hosts": ["pi.tail1234.ts.net"]})
        text = config.ENV_PATH.read_text()
        self.assertIn("PUBLIC_BASE_URL=https://jobs.example.invalid\n", text)
        self.assertIn("JOBHUNT_ALLOWED_HOSTS=pi.tail1234.ts.net\n", text)
        # The host allowlist lets the names through at once.
        anon = TestClient(app, base_url="http://pi.tail1234.ts.net")
        self.assertEqual(anon.get("/api/health").status_code, 200)
        self.assertEqual(TestClient(app, base_url="http://elsewhere.example").get("/api/health").status_code, 400)
        r = c.put("/api/settings/address", json={"public_base_url": "", "allowed_hosts": []})
        self.assertEqual(r.json(), {"public_base_url": "", "allowed_hosts": []})
        self.assertEqual(TestClient(app, base_url="http://pi.tail1234.ts.net").get("/api/health").status_code, 400)

    def test_unknown_fields_and_other_settings_cannot_be_written(self):
        self.env_file()
        c = self.signed_in()
        self.assertEqual(c.put("/api/settings/address", json={"LLM_API_KEY": "x"}).status_code, 422)
        self.assertNotIn("SMTP_PASSWORD", app_settings.ENV_RULES)
        with self.assertRaises(ValueError):
            app_settings.update_env_file(config.ENV_PATH, {"STRIPE_SECRET_KEY": "sk_live_x"})


class SettingsLogTest(Base):
    SECRET = "sk-super-secret-key-12345"

    def test_changes_are_logged_by_name_never_by_value(self):
        self.env_file()
        c = self.signed_in()
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "", "LLM_BASE_URL": "https://api.openai.com/v1",
                                          "LLM_MODEL": "old-model"}), \
                self.assertLogs("backend.app_settings", level=logging.INFO) as logs:
            self.assertEqual(c.put("/api/settings/llm", json={"model": "new-model", "api_key": self.SECRET})
                             .status_code, 200)
            self.assertEqual(c.put("/api/settings/address", json={"public_base_url": "https://jobs.example.invalid"})
                             .status_code, 200)
        said = "\n".join(logs.output)
        for name in ("LLM_API_KEY set", "LLM_MODEL set", "PUBLIC_BASE_URL set"):
            self.assertIn(name, said)
        for value in (self.SECRET, "new-model", "jobs.example.invalid"):
            self.assertNotIn(value, said)


class RetiredEnvTest(unittest.TestCase):
    def test_the_old_mail_payment_and_commute_lines_are_removed_and_nothing_else(self):
        folder = helpers.scratch_folder()
        self.addCleanup(helpers.remove_scratch_folders)
        path = os.path.join(folder, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# my settings\nLLM_MODEL=keep-me\nSMTP_PASSWORD_B64=c2VjcmV0\nexport STRIPE_SECRET_KEY=sk_live_x\n"
                    "SMTP_HOST = smtp.example\n# SMTP_USER=commented out, kept\nTFNSW_API_KEY=k\nPUBLIC_BASE_URL=\n")
        from pathlib import Path
        removed = app_settings.remove_retired_env(Path(path))
        self.assertEqual(removed, ["SMTP_HOST", "SMTP_PASSWORD_B64", "STRIPE_SECRET_KEY", "TFNSW_API_KEY"])
        self.assertEqual(Path(path).read_text(), "# my settings\nLLM_MODEL=keep-me\n# SMTP_USER=commented out, kept\n"
                                                 "PUBLIC_BASE_URL=\n")
        self.assertEqual(app_settings.remove_retired_env(Path(path)), [])
        self.assertFalse(app_settings.RETIRED_ENV_NAMES & set(app_settings.ENV_RULES), "retired names are never set")


class UsageTest(Base):
    def test_totals_the_last_7_and_30_days_and_by_task(self):
        bob = helpers.make_user()
        model = f"usage-model-{os.getpid()}"
        now = utcnow()
        rows = [(now, "scoring", 1000, 100, 600), (now - timedelta(days=3), "scoring", 500, 50, 0),
                (now - timedelta(days=10), "chat", 200, 20, None), (now - timedelta(days=60), "research", 9000, 900, 0)]
        before = self.signed_in().get("/api/settings/usage").json()
        db = SessionLocal()
        ids = []
        for when, purpose, i, o, cached in rows:
            row = LlmUsage(created_at=when, user_id=bob.id, purpose=purpose, provider="openai", model=model,
                           input_tokens=i, output_tokens=o, cached_tokens=cached)
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
        db.close()
        self.addCleanup(lambda: db_rows(lambda s: (s.query(LlmUsage).filter(LlmUsage.id.in_(ids)).delete(
            synchronize_session=False), s.commit())))
        out = self.signed_in(bob).get("/api/settings/usage").json()

        def grew(window, key):
            return out[window][key] - before[window][key]
        self.assertEqual((grew("totals", "requests"), grew("totals", "input")), (4, 10700))
        self.assertEqual((grew("last_30_days", "requests"), grew("last_30_days", "input")), (3, 1700))
        self.assertEqual((grew("last_7_days", "requests"), grew("last_7_days", "input"), grew("last_7_days", "cached")),
                         (2, 1500, 600))
        tasks = {t["task"]: t for t in out["by_task"]}
        self.assertIn("research", tasks)
        self.assertGreaterEqual(tasks["scoring"]["last_7_days"]["requests"], 2)
        self.assertIsNotNone(out["since"])
        self.assertNotIn(model, json.dumps(out), "figures by task, not by model or person")
        self.assertNotIn(bob.email, json.dumps(out))

    def test_usage_is_recorded_for_the_requests_user_else_the_workspaces_owner(self):
        bob = helpers.make_user()
        model = f"recorded-{os.getpid()}"
        cfg = {"base_url": "https://llm.example.com/v1", "model": model, "purpose": "chat"}
        with helpers.as_account(bob):
            llm_providers.record_usage({**cfg, "workspace_id": None}, {"input_tokens": 5})
        llm_providers.record_usage({**cfg, "workspace_id": bob.workspace_id}, {"input_tokens": 6})   # no key scope
        llm_providers.record_usage({**cfg, "workspace_id": None}, {"input_tokens": 7})
        got = db_rows(lambda s: sorted((r.input_tokens, r.user_id) for r in s.query(LlmUsage).filter(
            LlmUsage.model == model)))
        self.assertEqual(got, [(5, bob.id), (6, bob.id), (7, None)])
        db_rows(lambda s: (s.query(LlmUsage).filter(LlmUsage.model == model).delete(), s.commit()))

    def test_an_older_database_gets_the_usage_user_column(self):
        out = helpers.scenario("""
            from sqlalchemy import text
            from backend.db import engine, init_db
            with engine.begin() as conn:  # llm_usage as the version before accounts made it
                conn.execute(text("CREATE TABLE llm_usage (id INTEGER PRIMARY KEY, created_at DATETIME, "
                                  "workspace_id INTEGER, purpose VARCHAR, provider VARCHAR, model VARCHAR, "
                                  "input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, "
                                  "cache_write_tokens INTEGER)"))
                conn.execute(text("INSERT INTO llm_usage (created_at, model) VALUES (CURRENT_TIMESTAMP, 'old')"))
            init_db()
            with engine.connect() as conn:
                out["columns"] = [r[1] for r in conn.execute(text("PRAGMA table_info(llm_usage)"))]
                out["rows"] = conn.execute(text("SELECT COUNT(*), COUNT(user_id) FROM llm_usage")).one()[:]
        """)
        helpers.remove_scratch_folders()
        self.assertIn("user_id", out["columns"])
        self.assertEqual(out["rows"], [1, 0])


class LeftoversTest(Base):
    def test_they_are_listed_by_path_and_size_and_deleted_on_request(self):
        c = self.signed_in()
        out = c.get("/api/settings/leftovers").json()
        self.assertEqual(out["encryption"], "done")
        backup = DB_PATH.parent / f"{DB_PATH.name}.bak-before-test"
        backup.write_bytes(b"old plaintext")
        self.addCleanup(lambda: backup.unlink(missing_ok=True))
        out = c.get("/api/settings/leftovers").json()
        self.assertIn({"path": backup.name, "bytes": 13, "kind": "backup"}, out["leftovers"])
        self.assertNotIn("_abs", json.dumps(out))
        r = c.post("/api/settings/leftovers/delete")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(backup.name, r.json()["deleted"])
        self.assertFalse(backup.exists())
        self.assertEqual(r.json()["leftovers"], [])
        with mock.patch.object(migrate_encryption, "is_done", return_value=False):
            self.assertEqual(c.post("/api/settings/leftovers/delete").json()["code"], "encrypting")


class KekRotationTest(Base):
    """Replacing the server key (the Settings tab's Server key card, or ``python -m backend.keys rotate-kek``)."""

    def background_user(self) -> users.NewAccount:
        return helpers.extra_account(f"rot-{os.urandom(4).hex()}@example.invalid", PASSWORD, background=True)

    def kek_ids(self, user_id: int):
        row = db_rows(lambda s: s.get(UserKeys, user_id))
        return row.server_kek_id, users.server_keys().kek_id_of(row.server_wrapped_dek)

    def test_rotation_rewraps_every_server_wrapped_value(self):
        c = self.signed_in()
        a, b = self.background_user(), self.background_user()
        old_id = users.server_keys().current_kek_id
        r = c.post("/api/settings/server-key/rotate")
        self.assertEqual(r.status_code, 200, r.text)
        new_id = r.json()["key"]
        self.assertNotEqual(new_id, old_id)
        self.assertGreaterEqual(r.json()["rewrapped"], 2)
        self.assertEqual(users.server_keys().kek_ids, [new_id])  # the old key is gone from the file
        self.assertEqual(keys.ServerKeys.load().kek_ids, [new_id])
        for acct in (a, b):
            self.assertEqual(self.kek_ids(acct.user_id), (new_id, new_id))
            self.assertEqual(db_rows(lambda s: users.background_key(s, acct.user_id)), acct.dek)

    def test_an_older_installs_second_account_may_replace_it_too(self):
        bob = helpers.make_user()
        a = self.background_user()
        r = self.signed_in(bob).post("/api/settings/server-key/rotate")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db_rows(lambda s: users.background_key(s, a.user_id)), a.dek)

    def test_rows_still_under_the_old_key_after_a_crash_are_rewrapped_at_startup(self):
        a = self.background_user()
        server = users.server_keys()
        old_id = server.current_kek_id
        server.start_kek_rotation()  # ... and the process dies here: the file has both keys, the rows have the old one
        self.assertTrue(keys.ServerKeys.load().kek_rotation_pending)
        self.assertEqual(self.kek_ids(a.user_id)[1], old_id)
        with TestClient(app):  # a start-up: users.startup() finishes the rotation first
            pass
        new_id = users.server_keys().current_kek_id
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(self.kek_ids(a.user_id), (new_id, new_id))
        self.assertEqual(keys.ServerKeys.load().kek_ids, [new_id])
        self.assertEqual(db_rows(lambda s: users.background_key(s, a.user_id)), a.dek)

    def test_a_crash_after_the_rows_were_stored_only_drops_the_old_key(self):
        a = self.background_user()
        server = users.server_keys()
        server.start_kek_rotation()
        rows, seen = key_rotation._rows_factory(server)
        todo = []
        for row in rows():  # step 2 stored everything but the process died before the file was pruned
            secret = server.kek_unwrap(row.blob, row.label)
            todo.append((row.ref, *server.kek_wrap(secret, row.label)))
        key_rotation._store_factory(seen)(todo)
        self.assertTrue(server.kek_rotation_pending)
        self.assertEqual(key_rotation.finish_pending(server), 0)
        self.assertFalse(server.kek_rotation_pending)
        self.assertEqual(db_rows(lambda s: users.background_key(s, a.user_id)), a.dek)

    def test_a_wrap_not_yet_committed_holds_the_rotation_back(self):
        """A wrap made before a rotation but committed after it would sit under a dropped key for good."""
        a = helpers.extra_account(f"wrap-{os.urandom(4).hex()}@example.invalid", PASSWORD)
        server = users.server_keys()
        wrapped, release, result = threading.Event(), threading.Event(), {}

        def writer():  # what users.enable_background does
            db = SessionLocal()
            try:
                with server.wrapping():
                    kek_id, blob = server.kek_wrap(a.dek, f"srv|{a.user_id}")
                    wrapped.set()
                    release.wait(10)
                    db.query(UserKeys).filter(UserKeys.user_id == a.user_id).update(
                        {UserKeys.server_wrapped_dek: blob, UserKeys.server_kek_id: kek_id})
                    db.commit()
            finally:
                db.close()

        def rotator():
            result["moved"] = key_rotation.rotate(server)

        w = threading.Thread(target=writer)
        w.start()
        self.assertTrue(wrapped.wait(10))
        r = threading.Thread(target=rotator)
        r.start()
        r.join(0.5)
        self.assertTrue(r.is_alive(), "the rotation waits for the wrap's commit")
        release.set()
        w.join(10)
        r.join(30)
        self.assertEqual(server.kek_ids, [server.current_kek_id])
        blob = db_rows(lambda s: bytes(s.get(UserKeys, a.user_id).server_wrapped_dek))
        self.assertEqual(server.kek_unwrap(blob, f"srv|{a.user_id}"), a.dek)
        self.assertEqual(server.kek_id_of(blob), server.current_kek_id)

    def test_the_wrap_paths_hold_the_lock_until_they_commit(self):
        a = helpers.extra_account(f"hold-{os.urandom(4).hex()}@example.invalid", PASSWORD)
        seen = []
        real = keys.ServerKeys.wrapping

        def spy(self_):
            seen.append(1)
            return real(self_)

        with mock.patch.object(keys.ServerKeys, "wrapping", spy):
            db = SessionLocal()
            try:
                users.enable_background(db, a.user_id, a.dek)
            finally:
                db.close()
            helpers.extra_account(f"hold-{os.urandom(4).hex()}@example.invalid", PASSWORD, background=True)
        self.assertEqual(len(seen), 2)

    def test_rotation_leaves_no_old_copy_in_the_log(self):
        a = self.background_user()
        old = db_rows(lambda s: bytes(s.get(UserKeys, a.user_id).server_wrapped_dek))
        key_rotation.rotate(users.server_keys())
        self.assertNotEqual(db_rows(lambda s: bytes(s.get(UserKeys, a.user_id).server_wrapped_dek)), old)
        for suffix in ("", "-wal"):  # raw bytes, no checkpoint first
            path = DB_PATH.with_name(DB_PATH.name + suffix)
            if path.exists():
                self.assertNotIn(old, path.read_bytes(), f"the retired key's copy is in {path.name}")

    def test_the_command_line_uses_the_same_code(self):
        a = self.background_user()
        old_id = users.server_keys().current_kek_id
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(keys.main(["rotate-kek"]), 0)
        self.assertIn("Done", out.getvalue())
        fresh = keys.ServerKeys.load()
        self.assertNotEqual(fresh.current_kek_id, old_id)
        self.assertEqual(fresh.kek_ids, [fresh.current_kek_id])
        users._server_keys = fresh  # (the command ran in this process on the same file)
        self.assertEqual(db_rows(lambda s: users.background_key(s, a.user_id)), a.dek)

    def test_a_running_app_with_an_old_copy_never_overwrites_a_newer_key_file(self):
        c = self.signed_in()
        a = self.background_user()
        app_copy = users.server_keys()
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(keys.main(["rotate-kek"]), 0)  # another process rotates; this one still has the old copy
        on_disk = keys.ServerKeys.load()
        self.assertNotEqual(on_disk.current_kek_id, app_copy.current_kek_id)
        before = on_disk.path.read_bytes()
        try:
            r = c.post("/api/settings/server-key/rotate")
            self.assertEqual((r.status_code, r.json()["code"]), (409, "key_file_changed"))
            self.assertEqual(on_disk.path.read_bytes(), before)  # nothing was written
            for change in (app_copy.rotate_pepper, app_copy.drop_old_peppers, app_copy.rotate_signing):
                with self.assertRaises(keys.KeyFileError):
                    change()
                self.assertEqual(on_disk.path.read_bytes(), before)
        finally:
            users._server_keys = on_disk  # what a restart does
        self.assertEqual(db_rows(lambda s: users.background_key(s, a.user_id)), a.dek)

    def test_the_command_line_refuses_while_the_app_is_running(self):
        import tempfile
        from pathlib import Path
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        run_file = os.path.join(folder, "running.json")
        with mock.patch.object(key_rotation, "DATA_DIR", Path(folder)):
            key_rotation.refuse_while_running()  # no file: fine
            with open(run_file, "w") as f:
                f.write(json.dumps({"port": 8000, "pid": os.getppid(), "launcher_pid": 1}))
            with self.assertRaises(keys.KeyFileError) as caught:
                key_rotation.server_keys_for_cli()
            self.assertIn("running", str(caught.exception))
            with mock.patch("sys.stderr", new=io.StringIO()) as err:
                self.assertEqual(key_rotation.rotate_kek_command(), 2)
            self.assertIn("running", err.getvalue())
            with open(run_file, "w") as f:
                f.write(json.dumps({"port": 8000, "pid": 2 ** 22 + 12345}))  # a process that is gone
            key_rotation.refuse_while_running()
            with open(run_file, "w") as f:
                f.write("not json")
            key_rotation.refuse_while_running()


class ErrorHygieneTest(Base):
    """Whoever is signed in manages the AI provider, so an AI error names the problem; never the key."""
    REJECTION = "invalid api key sk-secret-key-1234 for https://llm.example.com/v1 (Bearer abcdef123456)"

    def job_for(self, account) -> int:
        with helpers.as_account(account):
            db = SessionLocal()
            job = Job(workspace_id=account.workspace_id, url=f"https://hygiene.example/{os.urandom(3).hex()}",
                      origin="private", title="Analyst", company="Acme", description="Do things", status="to_review")
            db.add(job)
            db.commit()
            job_id = job.id
            db.close()
        return job_id

    def rejected(self, status=401):
        response = mock.Mock(status_code=status, text=self.REJECTION, headers={})
        return mock.patch.object(llm_providers.httpx, "post", return_value=response)

    def test_a_scoring_error_says_what_went_wrong_without_the_key(self):
        bob = helpers.make_user()
        b = self.signed_in(bob)
        job = self.job_for(bob)
        with mock.patch.dict(os.environ, LLM_ENV), self.rejected(), mock.patch.object(llm_providers.time, "sleep"):
            r = b.post(f"/api/score/{job}", headers={"X-Workspace": str(bob.workspace_id)})
            self.assertEqual(r.status_code, 502)
            self.assertIn("401", r.json()["detail"])
            for hidden in ("sk-secret-key-1234", "abcdef123456"):
                self.assertNotIn(hidden, r.text)
            stored = b.get(f"/api/jobs/{job}", headers={"X-Workspace": str(bob.workspace_id)}).text
            self.assertNotIn("sk-secret-key-1234", stored)

    def test_the_connection_test_and_level_detection_never_show_the_key(self):
        c = self.signed_in()
        with mock.patch.dict(os.environ, LLM_ENV), self.rejected(), mock.patch.object(llm_providers.time, "sleep"):
            r = c.post("/api/score/test")
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["ok"])
            self.assertNotIn("sk-secret-key-1234", r.text)
            r = c.post("/api/llm/detect-levels")
            self.assertNotIn("sk-secret-key-1234", r.text)

    def test_redact_takes_out_the_saved_key_and_anything_that_looks_like_one(self):
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "my-own-odd-key-value"}):
            said = llm_providers.user_message(RuntimeError(
                "failed with my-own-odd-key-value, sk-proj-abcdef123, AIzaSyA1234567890abc, Bearer tok.en-12345, "
                "https://x.example/v1?key=hunter22&x=1 api_key: zzz999"))
        for secret in ("my-own-odd-key-value", "sk-proj-abcdef123", "AIzaSyA1234567890abc", "tok.en-12345", "hunter22",
                       "zzz999"):
            self.assertNotIn(secret, said)
        self.assertIn("failed with", said)
        self.assertLessEqual(len(llm_providers.user_message("x" * 5000)), llm_providers.MESSAGE_MAX)

    def test_a_shared_company_profile_keeps_only_a_plain_sentence(self):
        self.assertNotIn("admin", llm_providers.USER_PROBLEM.lower())
        self.assertIn("Settings", llm_providers.USER_PROBLEM)

    def test_web_search_problem_points_to_the_settings_tab(self):
        cfg = {"base_url": "https://api.deepseek.com/v1", "model": "m"}
        said = llm_providers.web_search_problem(cfg, "Company research")
        self.assertIn("Settings (AI provider)", said)
        self.assertNotIn("admin", said.lower())


class DeleteAccountLockTest(Base):
    def test_deleting_an_older_installs_extra_account_holds_the_start_lock(self):
        """tasks.start takes this lock too: while the rows go, a run can't be queued for the workspace."""
        from backend import tasks
        bob = helpers.make_user(password=PASSWORD)
        with helpers.as_account(bob):
            stored = docs.save_upload(b"upload of bob")
            db = SessionLocal()
            db.add(Document(workspace_id=bob.workspace_id, filename="cv.pdf", text="cv text", filetype=".pdf",
                            kind="resume", stored_name=stored))
            db.commit()
            db.close()
        c = self.signed_in(bob)
        finished = threading.Event()
        statuses = []

        def delete():
            statuses.append(c.post("/api/account/delete", json={"password": PASSWORD}).status_code)
            finished.set()

        with tasks.start_lock():  # a start is in progress
            t = threading.Thread(target=delete)
            t.start()
            self.assertFalse(finished.wait(0.7), "the delete waits for the start lock")
            self.assertEqual(db_rows(lambda s: s.query(User).filter(User.id == bob.id).count()), 1)
        self.assertTrue(finished.wait(20))
        t.join()
        self.assertEqual(statuses, [200])
        self.assertEqual(db_rows(lambda s: s.query(User).filter(User.id == bob.id).count()), 0)
        self.assertEqual(db_rows(lambda s: s.query(Workspace).filter(Workspace.owner_id == bob.id).count()), 0)


if __name__ == "__main__":
    unittest.main()
