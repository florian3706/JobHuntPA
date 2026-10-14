"""The reset tool (backend/reset.py; open-source spec §1.2): run on the computer itself, with the app stopped, after
the word RESET (exactly, in capitals), it deletes the accounts, their encrypted data, the uploads and the key material, and keeps .env, the
pooled job ads and the company research. The next start is a first run. Nothing reachable over HTTP does any of this.

Every test runs the tool in its own process on a throwaway data folder (helpers.scenario), never on the test
database: a populated install is a copy of tests/fixtures/upgrade_from_main/ (two accounts, one of them the former
admin, with background access, jobs, a document and its sealed upload) started once with today's code, plus a
company profile, a map lookup, AI usage and a reasoning level.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests import helpers
from tests.helpers import remove_scratch_folders, scenario, scenario_env, scratch_folder

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "upgrade_from_main"
OWNER = ("owner@example.invalid", "fixture-owner-password")

# Start the app once on the copied fixture (the upgrade runs), sign in, and add what the reset must keep.
POPULATE = '''
from fastapi.testclient import TestClient
from backend.app import app
from backend.db import CompanyProfile, GeocodeCache, LlmUsage, SessionLocal

with TestClient(app, base_url="https://jobs.example.invalid") as c:
    r = c.post("/api/auth/login", json={"name": %r, "password": %r})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    ws = c.get("/api/workspaces").json()[0]["id"]
    assert c.put("/api/llm/reasoning", json={"scoring": "high"}).status_code == 200
    db = SessionLocal()
    db.add(CompanyProfile(key="fixtureco", name="Fixtureco", status="done", official_name="Fixtureco Pty Ltd"))
    db.add(GeocodeCache(query="sydney nsw", lat=-33.87, lng=151.21, country="AU"))
    db.add(LlmUsage(workspace_id=ws, user_id=1, purpose="scoring", provider="anthropic", model="m", input_tokens=10,
                    output_tokens=5))
    db.commit()
    db.close()
'''

# What is in the folder now: row counts, files, and the bytes of the account keys (to look for after the reset).
INSPECT = '''
import sqlite3
from pathlib import Path

folder = Path(FOLDER)
con = sqlite3.connect(folder / "jobhunt.db")
tables = {t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
count = lambda t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in tables else None
out["counts"] = {t: count(t) for t in ("users", "user_keys", "recovery_codes", "sessions", "workspaces", "jobs",
                                        "documents", "pins", "dealbreakers", "user_profile", "search_runs",
                                        "postings", "company_profiles", "geocode_cache", "llm_usage")}
out["usage_owners"] = con.execute("SELECT COUNT(*) FROM llm_usage WHERE user_id IS NOT NULL "
                                  "OR workspace_id IS NOT NULL").fetchone()[0]
out["settings"] = sorted(k for (k,) in con.execute("SELECT key FROM app_settings"))
out["reasoning"] = (con.execute("SELECT value_json FROM app_settings WHERE key = 'reasoning'").fetchone() or [None])[0]
blobs = [bytes(b) for (b,) in con.execute("SELECT pw_wrapped_dek FROM user_keys")]
blobs += [bytes(b) for (b,) in con.execute("SELECT server_wrapped_dek FROM user_keys WHERE server_wrapped_dek IS NOT NULL")]
blobs += [bytes(b) for (b,) in con.execute("SELECT pii_self FROM users WHERE pii_self IS NOT NULL")]
blobs += [bytes(b) for (b,) in con.execute("SELECT wrapped_dek FROM recovery_codes WHERE wrapped_dek IS NOT NULL")]
con.close()
out["blobs"] = [b.hex() for b in blobs]
out["uploads"] = sorted(p.name for p in (folder / "uploads").iterdir()) if (folder / "uploads").is_dir() else []
out["key_file"] = (folder / "server-keys.json").exists()
out["first_run_file"] = (folder / "first-run.txt").exists()
out["env"] = (folder / ".env").read_text()
'''

LEFT_IN_FILES = '''
from pathlib import Path
blobs = [bytes.fromhex(h) for h in BLOBS]
files = [Path(FOLDER) / name for name in ("jobhunt.db", "jobhunt.db-wal") if (Path(FOLDER) / name).exists()]
out["left"] = sum(1 for p in files for b in blobs if b in p.read_bytes())
'''

FIRST_RUN_AFTER = '''
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app import app

with TestClient(app, base_url="https://jobs.example.invalid") as c:
    out["state"] = c.get("/api/auth/state").json()
    out["old_login"] = c.post("/api/auth/login", json={"name": %r, "password": %r}).status_code
    out["workspaces_signed_out"] = c.get("/api/workspaces").status_code
    token = (Path(FOLDER) / "first-run.txt").read_text().strip()
    r = c.post("/api/auth/first-run", json={"token": token, "name": "New owner", "password": "a brand new password"})
    out["first_run"] = r.status_code
    out["codes"] = len(r.json().get("recovery_codes", []))
    c.headers["X-CSRF-Token"] = r.json().get("csrf", "")
    out["me"] = c.get("/api/auth/me").json()["user"]["name"]
    out["workspaces"] = [w["name"] for w in c.get("/api/workspaces").json()]
    out["model"] = c.get("/api/settings/llm").json().get("model")
    out["again"] = c.post("/api/auth/first-run", json={"token": token, "name": "Someone else",
                                                       "password": "another long password"}).status_code
out["key_file"] = (Path(FOLDER) / "server-keys.json").exists()
'''


def reset_command(folder: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """``python -m backend.reset`` on ``folder``, as reset.sh runs it (stdin a pipe: not a terminal)."""
    return subprocess.run([sys.executable, "-m", "backend.reset", *args], cwd=helpers.ROOT, text=True,
                          env=scenario_env(folder), input=stdin if stdin is not None else "", capture_output=True,
                          timeout=120)


def populated() -> str:
    folder = scratch_folder()
    for name in ("jobhunt.db", "server-keys.json"):
        shutil.copy(FIXTURE / name, Path(folder) / name)
    shutil.copy(FIXTURE / "env.txt", Path(folder) / ".env")
    shutil.copytree(FIXTURE / "uploads", Path(folder) / "uploads")
    scenario(POPULATE % OWNER, folder)
    return folder


class ResetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = populated()
        cls.before = scenario(INSPECT, cls.folder)
        cls.refusals = {
            "no terminal, no word": reset_command(cls.folder),
            "the wrong word": reset_command(cls.folder, "--confirm", "yes"),
            "the word in small letters": reset_command(cls.folder, "--confirm", "reset"),
            "typed, but not the word": reset_command(cls.folder, stdin="RESET\n"),  # (stdin isn't a terminal)
        }
        cls.after_refusals = scenario(INSPECT, cls.folder)
        # What a crash can leave (crypto.write_private's temporary files): a half-written sealed upload, and copies of
        # the key file (the KEK and pepper in plaintext) and of the first-run token beside it.
        cls.crash_left = [Path(cls.folder) / "uploads" / ".a1b2c3.sealed-x7y8.tmp",
                          Path(cls.folder) / ".server-keys.json-k3j4.tmp",
                          Path(cls.folder) / ".server-keys.json.new-m5n6.tmp",
                          Path(cls.folder) / ".first-run.txt-p7q8.tmp"]
        for path in cls.crash_left:
            path.write_bytes(b"left by a crash")
        cls.done = reset_command(cls.folder, "--confirm", "RESET")
        cls.after = scenario(INSPECT, cls.folder)
        cls.left = scenario(f"BLOBS = {cls.before['blobs']!r}\n" + LEFT_IN_FILES, cls.folder)
        cls.next_start = scenario(FIRST_RUN_AFTER % OWNER, cls.folder)

    @classmethod
    def tearDownClass(cls):
        remove_scratch_folders()

    def test_the_folder_was_populated(self):
        counts = self.before["counts"]
        self.assertEqual(counts["users"], 2)
        self.assertGreaterEqual(counts["jobs"], 4)
        self.assertEqual(counts["documents"], 1)
        self.assertEqual((counts["company_profiles"], counts["geocode_cache"], counts["llm_usage"]), (1, 1, 1))
        self.assertGreaterEqual(counts["postings"], 1)
        self.assertTrue(self.before["uploads"])
        self.assertTrue(self.before["key_file"])
        self.assertGreater(len(self.before["blobs"]), 10)

    def test_nothing_is_changed_without_the_word(self):
        for why, done in self.refusals.items():
            self.assertEqual(done.returncode, 1, (why, done.stdout, done.stderr))
            self.assertIn("Nothing was changed", done.stderr, why)
        self.assertIn("--confirm RESET", self.refusals["no terminal, no word"].stderr)
        self.assertEqual(self.after_refusals, self.before)

    def test_it_says_what_will_be_lost_and_what_is_kept(self):
        said = self.done.stdout
        for words in ("2 account(s)", "workspace(s)", "document(s)", "server key file", "keeps your settings",
                      "shared job ads", "company research", "Download your data"):
            self.assertIn(words, said)
        self.assertEqual(self.done.returncode, 0, (self.done.stdout, self.done.stderr))

    def test_the_accounts_their_data_uploads_and_keys_are_gone(self):
        counts = self.after["counts"]
        for table in ("users", "user_keys", "recovery_codes", "sessions", "workspaces", "jobs", "documents", "pins",
                      "dealbreakers", "user_profile", "search_runs"):
            self.assertEqual(counts[table], 0, table)
        self.assertEqual(self.after["uploads"], [])
        self.assertFalse(self.after["key_file"])
        self.assertEqual([p.name for p in self.crash_left if p.exists()], [], "crash-left temporary files")
        self.assertEqual(self.after["usage_owners"], 0, "usage is kept without saying whose it was")
        for key in ("server_keys_fingerprint", "encryption_migration"):
            self.assertNotIn(key, self.after["settings"])
        self.assertEqual(self.left["left"], 0, "a key or sealed detail is still in the database or its log")

    def test_the_settings_ads_and_company_research_are_kept(self):
        self.assertEqual(self.after["env"], self.before["env"])
        self.assertIn("LLM_API_KEY=", self.after["env"])
        for table in ("postings", "company_profiles", "geocode_cache", "llm_usage"):
            self.assertEqual(self.after["counts"][table], self.before["counts"][table], table)
        self.assertEqual(json.loads(self.after["reasoning"])["scoring"], "high")

    def test_the_next_start_is_a_first_run(self):
        got = self.next_start
        self.assertEqual(got["state"], {"setup_required": True, "folder_required": False, "signed_in": False})
        self.assertEqual(got["workspaces_signed_out"], 401)
        self.assertIn(got["old_login"], (401, 403))
        self.assertEqual((got["first_run"], got["codes"]), (201, 8))
        self.assertEqual(got["me"], "New owner")
        self.assertEqual(got["workspaces"], ["My job search"])
        self.assertEqual(got["model"], "claude-fixture-model", "the AI settings in .env were kept")
        self.assertEqual(got["again"], 409, "and once more, no second account")
        self.assertTrue(got["key_file"], "a new key file is made")


class ResetWhileRunningTest(unittest.TestCase):
    """Refused while JobHuntPA runs: the app holds the key file's lock, and the launcher's data/running.json names a
    live process."""

    def tearDown(self):
        remove_scratch_folders()

    def start_app(self, folder: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", "from backend import users\nfrom backend.db import init_db\ninit_db()\n"
             "users.startup()\nprint('READY', flush=True)\nimport sys\nsys.stdin.readline()"],
            cwd=helpers.ROOT, env=scenario_env(folder), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)

        def stop():
            if proc.poll() is None:
                proc.stdin.close()
                proc.wait(timeout=30)
            for pipe in (proc.stdout, proc.stderr, proc.stdin):
                if pipe and not pipe.closed:
                    pipe.close()

        self.addCleanup(stop)
        self.assertEqual(proc.stdout.readline().strip(), "READY")
        return proc

    def test_refused_while_the_app_holds_the_key_file(self):
        folder = populated()
        before = scenario(INSPECT, folder)
        app = self.start_app(folder)
        done = reset_command(folder, "--confirm", "RESET")
        self.assertEqual(done.returncode, 1, (done.stdout, done.stderr))
        self.assertIn("using the key file", done.stderr)
        self.assertIn("Nothing was changed", done.stderr)
        app.stdin.close()
        app.wait(timeout=30)
        self.assertEqual(scenario(INSPECT, folder), before)

    def test_refused_while_the_launcher_says_it_runs(self):
        folder = populated()
        before = scenario(INSPECT, folder)
        # The launcher's file, naming a live process (this test's own).
        (Path(folder) / "running.json").write_text(json.dumps({"port": 8000, "pid": os.getpid()}))
        done = reset_command(folder, "--confirm", "RESET")
        self.assertEqual(done.returncode, 1, (done.stdout, done.stderr))
        self.assertIn("JobHuntPA is running", done.stderr)
        self.assertEqual(scenario(INSPECT, folder), before)
        # A file a crash left behind (its process is gone) doesn't stand in the way.
        (Path(folder) / "running.json").write_text(json.dumps({"port": 8000, "pid": 2 ** 22 + 12345}))
        done = reset_command(folder, "--confirm", "RESET")
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))


class ResetAskedTest(unittest.TestCase):
    """At a terminal it asks for the word: anything else (or nothing) stops it."""

    def tearDown(self):
        remove_scratch_folders()

    ASK = '''
import io
from backend import reset, users
from backend.db import SessionLocal, User, init_db
init_db()
users.startup()
users.create_account("Owner", "a long enough password")
for typed in ("", "reset please", "no", "reset", "Reset"):
    err = io.StringIO()
    out[repr(typed)] = reset.run(ask=lambda prompt, t=typed: t, interactive=True, out=io.StringIO(), err=err)
    out[repr(typed) + " said"] = err.getvalue()
db = SessionLocal()
out["accounts_after_refusals"] = db.query(User).count()
db.close()
said = io.StringIO()
out["typed"] = reset.run(ask=lambda prompt: " RESET ", interactive=True, out=said)
out["said"] = said.getvalue()
db = SessionLocal()
out["accounts_after"] = db.query(User).count()
db.close()
'''

    def test_only_the_word_goes_ahead(self):
        got = scenario(self.ASK)
        for typed in ("''", "'reset please'", "'no'", "'reset'", "'Reset'"):
            self.assertEqual(got[typed], 1, typed)
            self.assertIn("Nothing was changed", got[typed + " said"])
        self.assertEqual(got["accounts_after_refusals"], 1)
        self.assertEqual(got["typed"], 0)  # (exactly the word in capitals; spaces around it are fine)
        self.assertEqual(got["accounts_after"], 0)
        self.assertIn("first run", got["said"])


class NothingOverHttpTest(unittest.TestCase):
    def test_every_table_is_wiped_or_kept_on_purpose(self):
        from backend import reset
        from backend.db import Base

        tables = set(Base.metadata.tables)
        self.assertEqual(tables - set(reset.WIPED_TABLES) - set(reset.KEPT_TABLES), set(),
                         "a new table must go into WIPED_TABLES or KEPT_TABLES in backend/reset.py")
        self.assertFalse(set(reset.WIPED_TABLES) & set(reset.KEPT_TABLES))
        self.assertEqual(set(reset.KEPT_TABLES), {"postings", "company_profiles", "geocode_cache", "app_settings",
                                                  "llm_usage"})

    def test_the_web_app_never_loads_the_reset_tool(self):
        got = scenario('''
import sys
from backend.app import app
out["loaded"] = "backend.reset" in sys.modules
out["modules"] = sorted({getattr(r, "endpoint", None).__module__ for r in app.routes if getattr(r, "endpoint", None)})
out["paths"] = sorted({getattr(r, "path", "") for r in app.routes})
''')
        self.assertFalse(got["loaded"])
        self.assertNotIn("backend.reset", got["modules"])
        for path in got["paths"]:
            self.assertNotRegex(path.lower(), r"reset|wipe|start-over|factory", path)
        remove_scratch_folders()

    def test_no_backend_module_but_the_tool_calls_it(self):
        backend = helpers.ROOT / "backend"
        for path in backend.rglob("*.py"):
            if path.name == "reset.py":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"from backend import [^\n]*\breset\b|import backend\.reset|backend\.reset import"
                                      r"|reset\.wipe\(", path.name)


if __name__ == "__main__":
    unittest.main()
