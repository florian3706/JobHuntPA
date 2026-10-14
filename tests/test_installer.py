"""Tests for the setup helpers (no network, no package installs)."""
from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend.llm_providers, to compare the menu with the app
import installer  # noqa: E402


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"

    def test_update_keeps_other_lines(self):
        self.env.write_text("# my note\nLLM_API_KEY=old\nJOBHUNT_USER_AGENT=custom\n")
        lines, values = installer.read_env(self.env)
        installer.write_env(self.env, lines, {"LLM_API_KEY": "new", "LLM_MODEL": "m"})
        text = self.env.read_text()
        self.assertIn("# my note", text)
        self.assertIn("LLM_API_KEY=new", text)
        self.assertIn("JOBHUNT_USER_AGENT=custom", text)
        self.assertIn("LLM_MODEL=m", text)
        self.assertNotIn("old", text)

    def _configure(self, env: dict, non_interactive: bool = True, root: Path | None = None):
        said = []
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "JOBHUNT_DATA_DIR"))}
        with mock.patch.object(installer, "ROOT", root or self.dir), mock.patch.dict(os.environ, {**clean, **env}, clear=True), \
                mock.patch.object(installer, "say", side_effect=said.append), \
                mock.patch("builtins.input", side_effect=AssertionError("setup asks nothing")):
            installer.configure_llm(non_interactive)
        self.said = "\n".join(said)

    def test_setup_asks_nothing(self):
        """The AI provider is the first run's step 3, in the browser (spec §1.5): no prompt, and nothing written."""
        data = self.dir / "data-folder"
        self._configure({"JOBHUNT_DATA_DIR": str(data)}, non_interactive=False)
        self.assertIn("asked for in the browser", self.said)
        self.assertFalse((data / ".env").exists())
        src = Path(installer.__file__).read_text(encoding="utf-8")
        self.assertNotIn("input(", src)
        self.assertFalse(hasattr(installer, "ask"))

    def test_menu_list_matches_the_app(self):
        from backend.llm_providers import PRESETS
        self.assertEqual([p["id"] for p in installer.load_presets()], [p["id"] for p in PRESETS])

    def test_built_in_list_if_the_app_list_cannot_be_read(self):
        with mock.patch.object(installer, "PRESETS_FILE", self.dir / "missing" / "llm_providers.py"):
            presets = installer.load_presets()
            self.assertEqual(presets, installer.FALLBACK_PRESETS)
            self.assertIn("ollama", [p["id"] for p in presets])
        broken = self.dir / "broken.py"
        broken.write_text("PRESETS = [{'label': 'x'}]\n")  # wrong shape
        with mock.patch.object(installer, "PRESETS_FILE", broken):
            self.assertEqual(installer.load_presets(), installer.FALLBACK_PRESETS)

    LLM = {"LLM_API_KEY": "test-key", "LLM_BASE_URL": "https://example.invalid/v1", "LLM_MODEL": "test-model"}

    def test_non_interactive_writes_the_data_folders_env(self):  # the path a scripted install with a service takes
        data = self.dir / "data-folder"
        admin = {"JOBHUNT_ADMIN_EMAIL": "ci@example.invalid", "JOBHUNT_ADMIN_PASSWORD": "ci-test-password-123"}
        self._configure({**self.LLM, **admin, "JOBHUNT_DATA_DIR": str(data)})
        values = installer.read_env(data / ".env")[1]
        self.assertEqual({k: values[k] for k in self.LLM}, self.LLM)
        # The account's details are for the process environment only, never written to .env.
        self.assertNotIn("JOBHUNT_ADMIN", (data / ".env").read_text())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((data / ".env").stat().st_mode), 0o600)
        self.assertFalse((self.dir / ".env").exists())

    def test_non_interactive_before_the_folder_is_chosen_writes_nothing(self):
        """Nothing but the pointer is written before the first run chooses the folder (spec §1.5): the app reads the
        LLM_* variables from its environment instead."""
        root = self.dir / "app"
        (root / "data").mkdir(parents=True)
        self._configure({**self.LLM, "JOBHUNT_CONFIG_DIR": str(self.dir / "config")}, root=root)
        self.assertIn("nothing is written", self.said)
        self.assertEqual([p.name for p in root.rglob("*")], ["data"])
        self.assertFalse((self.dir / "config").exists())

    def test_an_existing_install_keeps_its_env_next_to_the_app(self):
        root = self.dir / "app"
        (root / "data").mkdir(parents=True)
        (root / ".env").write_text("# my note\nLLM_API_KEY=old\n")
        self._configure({**self.LLM, "JOBHUNT_CONFIG_DIR": str(self.dir / "config")}, root=root)
        text = (root / ".env").read_text()
        self.assertIn("# my note", text)
        self.assertIn("LLM_MODEL=test-model", text)
        self.assertIn("LLM_API_KEY=test-key", text)

    def test_the_commute_keys_are_not_asked_any_more(self):
        """The job page links to Google Maps (spec §1.8): setup asks for no commute key and writes none."""
        self.assertFalse(hasattr(installer, "configure_commute"))
        src = Path(installer.__file__).read_text(encoding="utf-8")
        for gone in ("TFNSW", "TOMTOM", "commute"):
            self.assertNotIn(gone, src)

    def test_setup_no_longer_downloads_the_browser(self):
        """The app downloads it into the data folder when a careers page first needs it and the person agrees."""
        src = Path(installer.__file__).read_text(encoding="utf-8")
        self.assertNotIn("playwright", src)
        self.assertFalse(hasattr(installer, "install_browser"))

    def test_the_import_check_never_touches_a_real_data_folder(self):
        with mock.patch.object(installer, "run") as ran:
            installer.check_app()
        env = ran.call_args.kwargs["env"]
        self.assertIn("jobhuntpa-check-", env["JOBHUNT_DATA_DIR"])
        self.assertFalse(Path(env["JOBHUNT_DATA_DIR"]).exists())  # (a temporary folder, gone afterwards)


if __name__ == "__main__":
    unittest.main()


import uninstaller  # noqa: E402


class UninstallerTest(unittest.TestCase):
    def _app(self) -> Path:
        root = Path(tempfile.mkdtemp()) / "JobHuntPA"
        (root / "installer").mkdir(parents=True)
        (root / "installer" / "launch.py").write_text("")
        (root / "backend").mkdir()
        (root / "data" / "uploads").mkdir(parents=True)
        (root / "data" / "uploads" / "cv.pdf").write_text("cv")
        (root / "data" / "server-keys.json").write_text("{}")
        (root / "data" / "first-run.txt").write_text("token\n")
        (root / ".env").write_text("LLM_API_KEY=k\n")
        return root

    def test_only_real_app_folders(self):
        self.assertTrue(uninstaller.is_app_folder(self._app()))
        self.assertFalse(uninstaller.is_app_folder(Path(tempfile.mkdtemp())))

    def test_refuses_non_app_folder(self):
        other = Path(tempfile.mkdtemp())
        (other / "precious.txt").write_text("keep me")
        with mock.patch.object(sys, "argv", ["uninstaller", "--yes", "--folder", str(other)]), \
                mock.patch.object(uninstaller, "say"):
            self.assertEqual(uninstaller.main(), 1)
        self.assertTrue((other / "precious.txt").exists())

    def test_backup_and_remove(self):
        app = self._app()
        home = Path(tempfile.mkdtemp())
        (home / "Documents").mkdir()
        with mock.patch.object(sys, "argv", ["uninstaller", "--yes", "--folder", str(app)]), \
                mock.patch.object(uninstaller, "say"), mock.patch.object(uninstaller, "running_ports", return_value=[]), \
                mock.patch.object(uninstaller, "playwright_cache", return_value=home / "no-cache"), \
                mock.patch.object(Path, "home", return_value=home):
            self.assertEqual(uninstaller.main(), 0)
        self.assertFalse(app.exists())
        backups = list((home / "Documents").glob("JobHuntPA-backup-*.zip"))
        self.assertEqual(len(backups), 1)
        import zipfile
        # The key file goes with the database; the first-run token is only for the copy that made it.
        self.assertEqual(sorted(zipfile.ZipFile(backups[0]).namelist()),
                         [".env", "data/server-keys.json", "data/uploads/cv.pdf"])
        if os.name == "posix":  # it holds the server keys and the API keys: owner-only (accounts design §15)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def _run(self, app: Path, home: Path, env: dict, *args: str):
        clean = {k: v for k, v in os.environ.items() if k not in ("JOBHUNT_DATA_DIR", "APPIMAGE", "XDG_CONFIG_HOME")}
        with mock.patch.object(sys, "argv", ["uninstaller", "--yes", "--folder", str(app), *args]), \
                mock.patch.object(uninstaller, "say"), mock.patch.object(uninstaller, "running_ports", return_value=[]), \
                mock.patch.object(uninstaller, "playwright_cache", return_value=home / "no-cache"), \
                mock.patch.object(uninstaller.subprocess, "run") as systemctl, \
                mock.patch.object(Path, "home", return_value=home), mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            code = uninstaller.main()
        return code, systemctl

    def _app_with_paths(self) -> Path:
        app = self._app()
        shutil.copy(Path(__file__).resolve().parent.parent / "backend" / "paths.py", app / "backend" / "paths.py")
        return app

    def test_a_data_folder_elsewhere_is_backed_up_and_removed_with_its_pointer(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from backend import paths
        app = self._app_with_paths()
        shutil.rmtree(app / "data")
        top = Path(tempfile.mkdtemp())
        home, config, data = top / "home", top / "config", top / "elsewhere" / "JobHuntPA"
        (home / "Documents").mkdir(parents=True)
        (data / "uploads").mkdir(parents=True)
        (data / "jobhunt.db").write_bytes(b"db")
        (data / "server-keys.json").write_text("{}")
        (data / "uploads" / "cv.pdf").write_text("cv")
        (data / "browsers" / "chromium-1").mkdir(parents=True)
        (data / "browsers" / "chromium-1" / "chrome").write_text("big")
        env = {"JOBHUNT_CONFIG_DIR": str(config)}
        paths.write_pointer(data, env)
        code, _ = self._run(app, home, env)
        self.assertEqual(code, 0)
        self.assertFalse(data.exists())
        self.assertFalse(config.exists(), "the pointer (and its folder) is gone")
        self.assertFalse(app.exists())
        import zipfile
        backup = next((home / "Documents").glob("JobHuntPA-backup-*.zip"))
        self.assertEqual(sorted(zipfile.ZipFile(backup).namelist()),
                         [".env", "data/jobhunt.db", "data/server-keys.json", "data/uploads/cv.pdf"])

    def test_the_service_and_its_data_folder_go_too(self):
        app = self._app_with_paths()
        top = Path(tempfile.mkdtemp())
        home, data = top / "home", top / "srv" / "jobhunt"
        (home / "Documents").mkdir(parents=True)
        data.mkdir(parents=True)
        (data / "jobhunt.db").write_bytes(b"db")
        unit = top / "xdg" / "systemd" / "user" / "jobhuntpa.service"
        unit.parent.mkdir(parents=True)
        unit.write_text(f"[Service]\nWorkingDirectory={app}\nEnvironment=JOBHUNT_DATA_DIR={data}\n"
                        f"ExecStart={app}/.venv/bin/python -m uvicorn backend.app:app\n")
        code, systemctl = self._run(app, home, {"XDG_CONFIG_HOME": str(top / "xdg"),
                                                "JOBHUNT_CONFIG_DIR": str(top / "config")})
        self.assertEqual(code, 0)
        self.assertFalse(unit.exists())
        self.assertFalse(data.exists())
        if platform.system() == "Linux":
            calls = [c.args[0] for c in systemctl.call_args_list]
            self.assertIn(["systemctl", "--user", "disable", "--now", "jobhuntpa.service"], calls)

    def test_only_a_jobhuntpa_data_folder_is_ever_deleted(self):
        top = Path(tempfile.mkdtemp())
        app, home = top / "app", top / "home"
        app.mkdir()
        home.mkdir()
        mine = top / "mine"
        mine.mkdir()
        (mine / "photo.jpg").write_text("x")
        with mock.patch.object(Path, "home", return_value=home):
            self.assertFalse(uninstaller.safe_to_delete(mine, app))
            (home / "jobhunt.db").write_bytes(b"")
            self.assertFalse(uninstaller.safe_to_delete(home, app))  # never the home folder, even with data in it
            self.assertFalse(uninstaller.safe_to_delete(top, app))  # nor one that holds the app
            (mine / "jobhunt.db").write_bytes(b"")
            self.assertTrue(uninstaller.safe_to_delete(mine, app))

    def test_the_appimage_deletes_its_file_not_its_mount(self):
        app = self._app()
        top = Path(tempfile.mkdtemp())
        home = top / "home"
        (home / "Documents").mkdir(parents=True)
        image = top / "JobHuntPA-x86_64.AppImage"
        image.write_text("image")
        with mock.patch.object(uninstaller, "delete_folder") as deleted:
            code, _ = self._run(app, home, {"APPIMAGE": str(image), "JOBHUNT_CONFIG_DIR": str(top / "config")})
        self.assertEqual(code, 0)
        self.assertFalse(image.exists())
        deleted.assert_not_called()

    def test_refuses_while_running(self):
        app = self._app()
        with mock.patch.object(sys, "argv", ["uninstaller", "--yes", "--folder", str(app)]), \
                mock.patch.object(uninstaller, "say"), mock.patch.object(uninstaller, "running_ports", return_value=[8000]):
            self.assertEqual(uninstaller.main(), 1)
        self.assertTrue(app.exists())


import launch  # noqa: E402


class LaunchTest(unittest.TestCase):
    """installer/launch.py opens the first-run page while no account exists."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        patch = mock.patch.dict(os.environ, {"JOBHUNT_DB_PATH": str(self.dir / "jobhunt.db")})
        patch.start()
        self.addCleanup(patch.stop)

    def test_first_run_page_while_the_token_file_exists(self):
        self.assertEqual(launch.first_run_file(), self.dir / "first-run.txt")
        self.assertIsNone(launch.first_run_url("http://127.0.0.1:8000"))
        token = "A" * 20 + "b-_" * 6
        (self.dir / "first-run.txt").write_text(token + "\n")
        url = launch.first_run_url("http://127.0.0.1:8000")
        self.assertEqual(url, f"http://127.0.0.1:8000/first-run.html#token={token}")  # in the fragment: no server log
        with mock.patch.object(launch.webbrowser, "open") as opened, mock.patch.object(launch, "print") as printed:
            launch.open_app("http://127.0.0.1:8000", no_browser=False)
            opened.assert_called_once_with(url)
            self.assertIn(url, " ".join(str(c.args[0]) for c in printed.call_args_list))
            opened.reset_mock()
            launch.open_app("http://127.0.0.1:8000", no_browser=True)
            opened.assert_not_called()
        (self.dir / "first-run.txt").write_text("not a token\n")
        with mock.patch.object(launch, "setup_required", return_value=False):
            self.assertIsNone(launch.first_run_url("http://127.0.0.1:8000"))
        with mock.patch.dict(os.environ, {"JOBHUNT_DB_PATH": "", "JOBHUNT_DATA_DIR": str(self.dir / "elsewhere")}):
            self.assertEqual(launch.first_run_file(), self.dir / "elsewhere" / "first-run.txt")
            self.assertEqual(launch.run_file(), self.dir / "elsewhere" / "running.json")

    def test_before_the_data_folder_is_chosen_nothing_is_read_or_written_and_its_own_token_is_opened(self):
        """No data folder yet (no JOBHUNT_DATA_DIR, no pointer, no data next to the app): the launcher opens the page
        with the token it handed to the app, while the app says it isn't set up."""
        empty = self.dir / "app"
        (empty / "data").mkdir(parents=True)
        token = "T" * 43
        with mock.patch.dict(os.environ, {"JOBHUNT_DB_PATH": "", "JOBHUNT_DATA_DIR": "",
                                          "JOBHUNT_CONFIG_DIR": str(self.dir / "config")}), \
                mock.patch.object(launch.paths, "APP_ROOT", empty), \
                mock.patch.object(launch.paths, "locate", lambda: launch.paths.Place(None, "")):
            self.assertIsNone(launch.first_run_file())
            self.assertIsNone(launch.run_file())
            with mock.patch.object(launch, "setup_required", return_value=True):
                self.assertEqual(launch.first_run_url("http://127.0.0.1:8000", token),
                                 f"http://127.0.0.1:8000/first-run.html#token={token}")
                self.assertIsNone(launch.first_run_url("http://127.0.0.1:8000", "short"))  # not a token
            with mock.patch.object(launch, "setup_required", return_value=False):
                self.assertIsNone(launch.first_run_url("http://127.0.0.1:8000", token))
        self.assertFalse((self.dir / "config").exists())

    def test_the_app_is_started_again_after_the_folder_step(self):
        """Exit status 3 (backend/data_folder.py RESTART_CODE): started again with the same token, the browser opened
        once; any other status ends the launcher with it."""
        self.assertEqual(launch.RESTART_CODE, 3)
        from backend import data_folder
        self.assertEqual(data_folder.RESTART_CODE, launch.RESTART_CODE)
        started = []

        class Server:
            pid = 4242

            def __init__(self, code):
                self.code = code

            def wait(self):
                return self.code

        codes = iter([3, 0])

        def start(port, env):
            started.append(env["JOBHUNT_FIRST_RUN_TOKEN"])
            return Server(next(codes))

        with mock.patch.object(sys, "argv", ["launch", "--no-browser", "--port", "8765"]), \
                mock.patch.object(launch, "healthy", return_value=False), \
                mock.patch.object(launch, "port_free", return_value=True), \
                mock.patch.object(launch, "start_server", side_effect=start), \
                mock.patch.object(launch, "wait_until_up", return_value=None), \
                mock.patch.object(launch, "write_run_file", return_value=None), \
                mock.patch.object(launch, "open_app") as opened, mock.patch.object(launch, "print"), \
                mock.patch.object(launch.signal, "signal"), \
                mock.patch.dict(os.environ, {"JOBHUNT_FIRST_RUN_TOKEN": ""}):
            self.assertEqual(launch.main(), 0)
        self.assertEqual(len(started), 2)
        self.assertEqual(started[0], started[1])
        self.assertRegex(started[0], r"^[A-Za-z0-9_-]{32,100}$")
        opened.assert_called_once()

    def test_the_server_trusts_proxy_headers_from_this_computer_only(self):
        source = Path(launch.__file__).read_text(encoding="utf-8")
        self.assertIn('"--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"', source)
