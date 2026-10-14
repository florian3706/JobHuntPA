"""Where the data lives (open-source spec §1.5; backend/paths.py, backend/data_folder.py).

The rules: JOBHUNT_DATA_DIR (service installs) is used and never asked about; then the pointer the first run writes in
the per-user configuration folder; then a ``data`` folder next to the app that already holds JobHuntPA data (an
existing install: used where it is, nobody asked); otherwise the first run's step 1 asks, and until then nothing but
the pointer is written. Step 1 is bound to the first-run token and refuses a path that isn't an absolute path of a
folder the app may create.

The start-up tests run the app in fresh processes on throwaway folders: a made-up home, configuration folder and app
folder (``paths.APP_ROOT`` is pointed at it before the app is imported, so the real checkout's data/ is never seen),
with no JOBHUNT_DATA_DIR, database, key file or .env variable.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401  (sets the test environment first)
from backend import paths

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "upgrade_from_main"
OWNER = ("owner@example.invalid", "fixture-owner-password")
TOKEN = "first-run-token-for-the-tests-0123456789abc"
BASE = "http://127.0.0.1:8000"


class Sandbox:
    """A made-up computer: home, configuration folder and app folder, all throwaway."""

    def __init__(self):
        self.top = Path(tempfile.mkdtemp(prefix="jobhunt-folder-"))
        self.home = self.top / "home"
        self.config = self.top / "config"
        self.app = self.top / "app"
        self.home.mkdir()
        (self.app / "data").mkdir(parents=True)
        (self.app / "data" / ".gitkeep").write_text("")  # as in a checkout

    def remove(self):
        shutil.rmtree(self.top, ignore_errors=True)

    def env(self, extra: dict | None = None) -> dict:
        keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG", "PLAYWRIGHT_BROWSERS_PATH")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.update({"HOME": str(self.home), "USERPROFILE": str(self.home), "JOBHUNT_CONFIG_DIR": str(self.config),
                    "XDG_DATA_HOME": str(self.home / ".local" / "share"), "JOBHUNT_TESTING": "1",
                    "JOBHUNT_KDF_PROFILE": "test", "JOBHUNT_SCHEDULER": "off", "PYTHONPATH": str(ROOT),
                    "JOBHUNT_FIRST_RUN_TOKEN": TOKEN})
        env.update(extra or {})
        return env

    def run(self, code: str, extra: dict | None = None, base: str = BASE) -> dict:
        """``code`` in a fresh process with the app imported and a TestClient ``c`` open; it fills ``out``."""
        program = textwrap.dedent('''
            import json, os
            from pathlib import Path
            import backend.paths as paths
            paths.APP_ROOT = Path(%r)
            out = {}
            from fastapi.testclient import TestClient
            from backend import data_folder
            data_folder.restart_soon = lambda: out.__setitem__("restarted", True)
            from backend.app import app
            with TestClient(app, base_url=%r) as c:
            %s
            print("RESULT " + json.dumps(out, default=str))
        ''') % (str(self.app), base, textwrap.indent(textwrap.dedent(code), "    "))
        done = subprocess.run([sys.executable, "-c", program], cwd=str(self.top), env=self.env(extra),
                              capture_output=True, text=True, timeout=180)
        lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
        if done.returncode != 0 or not lines:
            raise AssertionError(f"failed ({done.returncode}):\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}")
        result = json.loads(lines[-1][len("RESULT "):])
        result["_log"] = done.stderr
        return result

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.top)) for p in self.top.rglob("*") if "__pycache__" not in p.parts)


def copy_fixture(folder: Path, env_name: str = ".env") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("jobhunt.db", "server-keys.json"):
        shutil.copy(FIXTURE / name, folder / name)
    shutil.copytree(FIXTURE / "uploads", folder / "uploads")
    if env_name:
        shutil.copy(FIXTURE / "env.txt", folder / env_name)


def post(c, path, body):
    return "r = c.post(%r, json=%r)" % (path, body)


class FreshStartTest(unittest.TestCase):
    """No pointer, no JOBHUNT_DATA_DIR and no data next to the app: the first run asks for the folder, and nothing is
    written before the choice."""

    @classmethod
    def setUpClass(cls):
        cls.box = Sandbox()
        cls.before = cls.box.files()
        (cls.box.top / "a-file").write_text("not a folder")
        (cls.box.top / "busy").mkdir()
        (cls.box.top / "busy" / "holiday.jpg").write_text("mine")
        cls.chosen = cls.box.home / "Job data"
        cls.step1 = cls.box.run(f'''
            out["health"] = c.get("/api/health").json()
            out["state"] = c.get("/api/auth/state").json()
            out["workspaces"] = c.get("/api/workspaces").json()
            out["first_run"] = c.post("/api/auth/first-run", json={{"token": {TOKEN!r}, "name": "Me",
                                                                   "password": "a long password"}}).status_code
            out["login"] = c.post("/api/auth/login", json={{"name": "Me", "password": "a long password"}}).status_code
            check = "/api/auth/data-folder/check"
            out["no_token"] = c.post(check, json={{}}).status_code
            out["bad_token"] = c.post(check, json={{"token": "x" * 43}}).json()
            out["offer"] = c.post(check, json={{"token": {TOKEN!r}}}).json()
            out["relative"] = c.post(check, json={{"token": {TOKEN!r}, "path": "my data"}}).json()
            out["a_file"] = c.post(check, json={{"token": {TOKEN!r}, "path": {str(cls.box.top / "a-file")!r}}}).json()
            out["busy"] = c.post(check, json={{"token": {TOKEN!r}, "path": {str(cls.box.top / "busy")!r}}}).json()
            out["dots"] = c.post(check, json={{"token": {TOKEN!r}, "path": {str(cls.box.top) + "/home/../busy"!r}}}).json()
            out["not_json"] = c.post("/api/auth/data-folder", content="token={TOKEN}",
                                     headers={{"Content-Type": "application/x-www-form-urlencoded"}}).status_code
            out["other_site"] = c.post("/api/auth/data-folder", json={{"token": {TOKEN!r}, "path": {str(cls.chosen)!r}}},
                                       headers={{"Origin": "https://evil.example"}}).status_code
            out["choose_no_token"] = c.post("/api/auth/data-folder", json={{"path": {str(cls.chosen)!r}}}).status_code
            out["choose_relative"] = c.post("/api/auth/data-folder", json={{"token": {TOKEN!r}, "path": "data"}}).json()
            out["choose_busy"] = c.post("/api/auth/data-folder",
                                        json={{"token": {TOKEN!r}, "path": {str(cls.box.top / "busy")!r}}}).status_code
            out["files_before"] = sorted(str(p) for p in Path({str(cls.box.top)!r}).rglob("*") if "__pycache__" not in p.parts)
            out["chosen_before"] = out.get("restarted", False)
            r = c.post("/api/auth/data-folder", json={{"token": {TOKEN!r}, "path": {str(cls.chosen)!r}}})
            out["choose"] = [r.status_code, r.json()]
            out["again"] = c.post("/api/auth/data-folder", json={{"token": {TOKEN!r}, "path": {str(cls.chosen)!r}}}).json()
        ''')
        cls.pointer = paths.pointer_file({"JOBHUNT_CONFIG_DIR": str(cls.box.config)})
        cls.after_choice = sorted(p.name for p in cls.chosen.iterdir()) if cls.chosen.exists() else None
        cls.pointer_text = cls.pointer.read_text() if cls.pointer.exists() else None
        # The launcher starts the app again: it finds the pointer and asks for the account, with the same token.
        cls.step2 = cls.box.run(f'''
            out["state"] = c.get("/api/auth/state").json()
            out["folder_step"] = c.post("/api/auth/data-folder/check", json={{"token": {TOKEN!r}}}).json()
            r = c.post("/api/auth/first-run", json={{"token": {TOKEN!r}, "name": "Me", "password": "a long password"}})
            out["first_run"] = r.status_code
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            out["llm_before"] = c.get("/api/llm").json()["configured"]
            out["onboarding"] = c.get("/api/auth/me").json()["onboarding_step"]
        ''')

    @classmethod
    def tearDownClass(cls):
        cls.box.remove()

    def test_the_first_run_asks_where_the_data_lives(self):
        got = self.step1
        self.assertEqual(got["health"], {"ok": True})
        self.assertEqual(got["state"], {"setup_required": True, "folder_required": True, "signed_in": False})
        self.assertEqual(got["workspaces"]["code"], "setup_required")
        self.assertEqual(got["first_run"], 401)  # the account comes after the folder
        self.assertEqual(got["login"], 401)
        self.assertEqual(got["offer"]["default"], str(self.box.home / ".local" / "share" / "JobHuntPA"))
        self.assertEqual(got["offer"]["kind"], "new")
        self.assertIsNone(got["offer"]["previous"])
        self.assertIn("token=" + TOKEN, got["_log"])  # the address with the token, in the app's own log

    def test_nothing_is_written_before_the_choice(self):
        files = [str(Path(p).relative_to(self.box.top)) for p in self.step1["files_before"]]
        self.assertEqual(sorted(files), sorted(self.before + ["a-file", "busy", os.path.join("busy", "holiday.jpg")]))
        self.assertFalse(self.step1["chosen_before"])

    def test_the_folder_step_needs_the_token(self):
        self.assertEqual(self.step1["no_token"], 403)
        self.assertEqual(self.step1["bad_token"]["code"], "bad_token")
        self.assertEqual(self.step1["choose_no_token"], 403)
        self.assertEqual(self.step1["other_site"], 403)
        self.assertEqual(self.step1["not_json"], 415)

    def test_a_bad_path_is_refused(self):
        for name in ("relative", "a_file", "busy", "dots"):
            self.assertIsNone(self.step1[name]["kind"], name)
            self.assertTrue(self.step1[name]["problem"], name)
        self.assertIn("whole path", self.step1["relative"]["problem"])
        self.assertIn("file, not a folder", self.step1["a_file"]["problem"])
        self.assertIn("other files", self.step1["busy"]["problem"])
        self.assertEqual(self.step1["choose_relative"]["code"], "bad_folder")
        self.assertEqual(self.step1["choose_busy"], 422)
        self.assertEqual((self.box.top / "busy" / "holiday.jpg").read_text(), "mine")

    def test_choosing_makes_the_folder_writes_the_pointer_and_restarts(self):
        status, body = self.step1["choose"]
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "new")
        self.assertTrue(self.step1["restarted"])
        self.assertEqual(json.loads(self.pointer_text)["data_folder"], str(self.chosen))
        self.assertEqual(self.after_choice, [])  # the folder only: the database and keys come with the restart
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.chosen.stat().st_mode), 0o700)
        self.assertEqual(self.step1["again"]["code"], "folder_chosen")
        self.assertNotIn(str(self.chosen), self.step1["_log"])  # no path in the log (it can hold a name)

    def test_after_the_restart_the_account_is_next_with_the_same_token(self):
        got = self.step2
        self.assertEqual(got["state"], {"setup_required": True, "folder_required": False, "signed_in": False})
        self.assertEqual(got["folder_step"]["code"], "folder_chosen")
        self.assertEqual(got["first_run"], 201)
        names = sorted(p.name for p in self.chosen.iterdir())
        self.assertIn("jobhunt.db", names)
        self.assertIn("server-keys.json", names)
        self.assertNotIn("first-run.txt", names)  # gone with the account made
        self.assertEqual(got["onboarding"], 1)  # the walkthrough, which starts with the AI provider, is next
        self.assertFalse(got["llm_before"])


class AiStepTest(unittest.TestCase):
    """The first run's step 3 (frontend/welcome.js ``ai``) saves through PUT /api/settings/llm into the data folder's
    .env, or is skipped and nothing is written."""

    @classmethod
    def setUpClass(cls):
        cls.box = Sandbox()
        cls.folder = cls.box.top / "data"
        cls.got = cls.box.run(f'''
            r = c.post("/api/auth/first-run", json={{"token": {TOKEN!r}, "name": "Me", "password": "a long password"}})
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            out["skipped_env"] = Path({str(cls.folder)!r}, ".env").exists()
            out["skipped"] = c.get("/api/llm").json()["configured"]
            out["signed_out"] = c.put("/api/settings/llm", headers={{"Cookie": ""}},
                                      json={{"base_url": "https://api.openai.com/v1", "model": "m",
                                            "api_key": "sk-test-key-1234"}}).status_code
            r = c.put("/api/settings/llm", json={{"base_url": "https://api.openai.com/v1", "model": "gpt-6-luna",
                                                 "api_key": "sk-test-key-1234"}})
            out["saved"] = r.status_code
            out["state"] = c.get("/api/llm").json()["configured"]
        ''', {"JOBHUNT_DATA_DIR": str(cls.folder)})

    @classmethod
    def tearDownClass(cls):
        cls.box.remove()

    def test_skipped_writes_nothing_and_completed_writes_the_data_folders_env(self):
        self.assertFalse(self.got["skipped_env"])
        self.assertFalse(self.got["skipped"])
        self.assertEqual(self.got["saved"], 200)
        self.assertTrue(self.got["state"])
        text = (self.folder / ".env").read_text()
        self.assertIn("LLM_MODEL=gpt-6-luna", text)
        self.assertIn("LLM_API_KEY=sk-test-key-1234", text)
        self.assertFalse((self.box.app / ".env").exists())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((self.folder / ".env").stat().st_mode), 0o600)

    def test_signed_out_is_refused(self):
        self.assertIn(self.got["signed_out"], (401, 403))


class ChosenFolderWithDataTest(unittest.TestCase):
    """Step 1 names a folder that already holds JobHuntPA data: it is used as it is, and the app goes to sign-in."""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.remove)

    def test_used_as_it_is_then_sign_in(self):
        folder = self.box.home / "Old JobHuntPA data"
        copy_fixture(folder)
        before = (folder / "jobhunt.db").stat().st_size
        got = self.box.run(f'''
            out["check"] = c.post("/api/auth/data-folder/check", json={{"token": {TOKEN!r}, "path": {str(folder)!r}}}).json()
            out["choose"] = c.post("/api/auth/data-folder", json={{"token": {TOKEN!r}, "path": {str(folder)!r}}}).json()
        ''')
        self.assertEqual(got["check"]["kind"], "existing")
        self.assertEqual(got["choose"]["kind"], "existing")
        self.assertEqual((folder / "jobhunt.db").stat().st_size, before)  # untouched until the restart
        again = self.box.run(f'''
            out["state"] = c.get("/api/auth/state").json()
            r = c.post("/api/auth/login", json={{"name": {OWNER[0]!r}, "password": {OWNER[1]!r}}})
            out["login"] = r.status_code
            out["first_run"] = c.post("/api/auth/first-run", json={{"token": {TOKEN!r}, "name": "x",
                                                                   "password": "a long password"}}).status_code
        ''', base="https://jobs.example.invalid")
        self.assertEqual(again["state"], {"setup_required": False, "folder_required": False, "signed_in": False})
        self.assertEqual(again["login"], 200)
        self.assertEqual(again["first_run"], 409)


class NotAskedTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.remove)

    def test_jobhunt_data_dir_is_used_and_nobody_is_asked(self):
        folder = self.box.top / "service-data"
        got = self.box.run('''
            out["state"] = c.get("/api/auth/state").json()
        ''', {"JOBHUNT_DATA_DIR": str(folder)})
        self.assertEqual(got["state"], {"setup_required": True, "folder_required": False, "signed_in": False})
        self.assertTrue((folder / "jobhunt.db").exists())
        self.assertTrue((folder / "server-keys.json").exists())
        self.assertFalse(self.box.config.exists())

    def test_a_pointer_to_a_folder_that_has_gone_asks_again_with_it_filled_in(self):
        gone = self.box.top / "unplugged-drive" / "JobHuntPA"
        paths.write_pointer(gone, {"JOBHUNT_CONFIG_DIR": str(self.box.config)})
        got = self.box.run(f'''
            out["state"] = c.get("/api/auth/state").json()
            out["offer"] = c.post("/api/auth/data-folder/check", json={{"token": {TOKEN!r}}}).json()
        ''')
        self.assertTrue(got["state"]["folder_required"])
        self.assertEqual(got["offer"]["previous"], str(gone))
        self.assertEqual(got["offer"]["default"], str(gone))
        self.assertFalse(gone.exists())


class CommandsBeforeTheFolderTest(unittest.TestCase):
    def test_the_reset_tool_and_the_key_commands_have_nothing_to_do_before_the_folder_is_chosen(self):
        box = Sandbox()
        self.addCleanup(box.remove)
        program = ("import sys\nfrom pathlib import Path\nimport backend.paths as paths\n"
                   f"paths.APP_ROOT = Path({str(box.app)!r})\nfrom backend import reset\n"
                   "sys.exit(reset.run(confirm='RESET'))\n")
        done = subprocess.run([sys.executable, "-c", program], cwd=str(box.top), env=box.env(), capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("isn't chosen yet", done.stderr)
        program = program.replace("from backend import reset", "from backend import keys").replace(
            "reset.run(confirm='RESET')", "keys.main(['rotate-kek'])")
        done = subprocess.run([sys.executable, "-c", program], cwd=str(box.top), env=box.env(), capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("isn't chosen yet", done.stderr)
        self.assertEqual(box.files(), ["app", os.path.join("app", "data"), os.path.join("app", "data", ".gitkeep"),
                                       "home"])


class PathsTest(unittest.TestCase):
    """backend/paths.py on its own."""

    def setUp(self):
        self.top = Path(tempfile.mkdtemp(prefix="jobhunt-paths-"))
        self.addCleanup(shutil.rmtree, self.top, True)
        self.app = self.top / "app"
        (self.app / "data").mkdir(parents=True)
        self.env = {"HOME": str(self.top / "home"), "JOBHUNT_CONFIG_DIR": str(self.top / "config")}

    def test_the_order_env_then_data_next_to_the_app_then_pointer(self):
        self.assertEqual(paths.locate(self.app, self.env, "Linux"), paths.Place(None, ""))
        chosen = self.top / "chosen"
        chosen.mkdir()
        paths.write_pointer(chosen, self.env)
        self.assertEqual(paths.locate(self.app, self.env, "Linux"), paths.Place(chosen, "pointer"))
        # A copy of the app with its own data keeps it, whatever the (one per user) pointer says.
        (self.app / "data" / "jobhunt.db").write_bytes(b"")
        self.assertEqual(paths.locate(self.app, self.env, "Linux"), paths.Place(self.app / "data", "app-folder"))
        service = {**self.env, "JOBHUNT_DATA_DIR": str(self.top / "service")}
        self.assertEqual(paths.locate(self.app, service, "Linux"), paths.Place(self.top / "service", "env"))
        relative = {**self.env, "JOBHUNT_DATA_DIR": "elsewhere"}
        self.assertEqual(paths.locate(self.app, relative, "Linux").folder, self.app / "elsewhere")
        self.assertTrue(paths.remove_pointer(self.env))
        self.assertFalse((self.top / "config").exists())
        self.assertFalse(paths.remove_pointer(self.env))

    def test_a_data_folder_named_in_an_older_env_next_to_the_app_is_still_used(self):
        """An install from before this version that moved its data with a JOBHUNT_DATA_DIR line in the .env next to the
        app keeps that folder (the line is read here, before the settings file is loaded); only while it holds data."""
        moved = self.top / "moved data"
        moved.mkdir()
        (self.app / ".env").write_text(f'# mine\nLLM_MODEL=x\nexport JOBHUNT_DATA_DIR="{moved}"  # moved\n')
        self.assertEqual(paths.locate(self.app, self.env, "Linux"), paths.Place(None, ""))  # no data there: asked
        (moved / "jobhunt.db").write_bytes(b"")
        (self.app / "data" / "jobhunt.db").write_bytes(b"")
        self.assertEqual(paths.locate(self.app, self.env, "Linux"), paths.Place(moved, "env-file"))
        service = {**self.env, "JOBHUNT_DATA_DIR": str(self.top / "service")}
        self.assertEqual(paths.locate(self.app, service, "Linux").how, "env")  # the environment still comes first
        (self.app / ".env").write_text("JOBHUNT_DATA_DIR=\n")
        self.assertEqual(paths.locate(self.app, self.env, "Linux").how, "app-folder")

    def test_foreign_entries(self):
        folder = self.top / "f"
        (folder / "uploads").mkdir(parents=True)
        for name in ("jobhunt.db", "jobhunt.db-journal", "jobhunt.db.bak-before-job-pool", ".env", ".cv-1.tmp",
                     "taxes.pdf", ".hidden", "jobhunt.db-other"):
            (folder / name).write_text("")
        # SQLite's rollback journal is the database's own; another name that only starts like it isn't.
        self.assertEqual(paths.foreign_entries(folder), [".hidden", "jobhunt.db-other", "taxes.pdf"])

    def test_a_tilde_path_means_this_persons_home_only(self):
        """"~/x" is in this person's home; "~bob/x" would be another user's folder, so it is refused."""
        with mock.patch.dict(os.environ, {"HOME": str(self.top / "home")}):
            path, kind = paths.check_folder("~/JobHuntPA data")
            self.assertEqual(path, self.top / "home" / "JobHuntPA data")
            for text in ("~root/JobHuntPA", "~bob", "~nobody/x"):
                with self.assertRaises(paths.FolderProblem, msg=text) as caught:
                    paths.check_folder(text)
                self.assertIn("~/", str(caught.exception))

    def test_an_unreadable_pointer_is_ignored(self):
        pointer = paths.pointer_file(self.env)
        pointer.parent.mkdir(parents=True)
        for text in ("not json", '{"data_folder": "relative/path"}', '["x"]', '{"data_folder": 3}'):
            pointer.write_text(text)
            self.assertIsNone(paths.read_pointer(self.env), text)

    def test_the_pointer_is_owner_only(self):
        target = paths.write_pointer(self.top / "x", self.env)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_defaults_and_configuration_folders(self):
        home = Path(self.env["HOME"])
        bare = {"HOME": str(home)}
        self.assertEqual(paths.default_folder(self.app, bare, "Linux"), home / ".local" / "share" / "JobHuntPA")
        self.assertEqual(paths.default_folder(self.app, {**bare, "XDG_DATA_HOME": "/xdg/data"}, "Linux"),
                         Path("/xdg/data/JobHuntPA"))
        self.assertEqual(paths.default_folder(self.app, bare, "Windows"), self.app / "data")
        self.assertEqual(paths.default_folder(self.app, bare, "Darwin"), self.app / "data")
        self.assertEqual(paths.config_dir(bare, "Linux"), home / ".config" / "JobHuntPA")
        self.assertEqual(paths.config_dir({**bare, "XDG_CONFIG_HOME": "/xdg/cfg"}, "Linux"), Path("/xdg/cfg/JobHuntPA"))
        self.assertEqual(paths.config_dir({**bare, "APPDATA": "C:/Users/me/AppData/Roaming"}, "Windows"),
                         Path("C:/Users/me/AppData/Roaming") / "JobHuntPA")
        self.assertEqual(paths.config_dir(bare, "Darwin"), home / "Library" / "Application Support" / "JobHuntPA")
        self.assertEqual(paths.config_dir({**bare, "JOBHUNT_CONFIG_DIR": "/t/c"}, "Darwin"), Path("/t/c"))

    def test_older_layouts_keep_working(self):
        folder = self.top / "moved"
        folder.mkdir()
        self.assertEqual(paths.env_file(folder, self.app), folder / ".env")
        (self.app / ".env").write_text("LLM_MODEL=x\n")
        self.assertEqual(paths.env_file(folder, self.app), self.app / ".env")  # next to the app, while none here
        (folder / ".env").write_text("")
        self.assertEqual(paths.env_file(folder, self.app), folder / ".env")
        self.assertEqual(paths.key_file(folder, self.app), folder / "server-keys.json")
        (self.app / "data" / "server-keys.json").write_text("{}")
        self.assertEqual(paths.key_file(folder, self.app), self.app / "data" / "server-keys.json")
        (folder / "server-keys.json").write_text("{}")
        self.assertEqual(paths.key_file(folder, self.app), folder / "server-keys.json")

    def test_checking_a_folder(self):
        self.assertEqual(paths.check_folder(str(self.top / "new" / "deeper")), (self.top / "new" / "deeper", "new"))
        (self.app / "data" / ".gitkeep").write_text("")
        (self.app / "data" / "sources.json").write_text("[]")
        (self.app / "data" / "uploads").mkdir()
        self.assertEqual(paths.check_folder(str(self.app / "data")), (self.app / "data", "empty"))
        (self.app / "data" / "jobhunt.db").write_bytes(b"")
        self.assertEqual(paths.check_folder(str(self.app / "data") + "/"), (self.app / "data", "existing"))
        with mock.patch.dict(os.environ, {"HOME": str(self.top / "home")}):
            self.assertEqual(paths.check_folder("~/JobHuntPA")[0], self.top / "home" / "JobHuntPA")
        for bad in ("", "   ", None, 7, "relative", "data/x", "x" * 1001, str(self.top) + "/a\x00b",
                    str(self.top) + "/a\nb", str(self.top / "a" / ".." / "b")):
            with self.assertRaises(paths.FolderProblem, msg=repr(bad)):
                paths.check_folder(bad)
        (self.top / "afile").write_text("x")
        with self.assertRaises(paths.FolderProblem):
            paths.check_folder(str(self.top / "afile"))
        with self.assertRaises(paths.FolderProblem):
            paths.check_folder(str(self.top / "afile" / "below"))
        (self.top / "mine").mkdir()
        (self.top / "mine" / "notes.txt").write_text("x")
        with self.assertRaises(paths.FolderProblem):
            paths.check_folder(str(self.top / "mine"))

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "permissions don't bind the root account")
    def test_a_folder_it_may_not_create_is_refused(self):
        locked = self.top / "locked"
        locked.mkdir(mode=0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        with self.assertRaises(paths.FolderProblem):
            paths.check_folder(str(locked / "JobHuntPA"))

    def test_preparing_makes_it_owner_only(self):
        target = self.top / "made" / "here"
        paths.prepare_folder(target)
        self.assertEqual(list(target.iterdir()), [])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_the_test_suite_never_sees_the_real_folders(self):
        self.assertTrue(os.environ["JOBHUNT_CONFIG_DIR"].startswith(tempfile.gettempdir()))
        self.assertEqual(paths.current().how, "env")


if __name__ == "__main__":
    unittest.main()
