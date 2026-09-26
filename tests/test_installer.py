"""Tests for the setup helpers (no network, no package installs)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
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

    def _configure(self, answers, reconfigure=False):
        with mock.patch.object(installer, "ROOT", self.dir), \
                mock.patch("builtins.input", side_effect=answers), mock.patch.object(installer, "say"):
            installer.configure_llm(interactive=True, reconfigure=reconfigure)
        return installer.read_env(self.env)[1]

    def test_interactive_preset(self):
        values = self._configure(["1", "", "", "secret-key"])
        self.assertEqual(values["LLM_BASE_URL"], "https://api.meta.ai/v1")
        self.assertEqual(values["LLM_MODEL"], "muse-spark-1.3")
        self.assertEqual(values["LLM_API_KEY"], "secret-key")

    def test_existing_settings_kept(self):
        self.env.write_text("LLM_API_KEY=k\nLLM_BASE_URL=b\nLLM_MODEL=m\n")
        values = self._configure([])  # must not prompt
        self.assertEqual(values["LLM_API_KEY"], "k")

    def test_skip(self):
        values = self._configure(["s"])
        self.assertEqual(values.get("LLM_API_KEY"), "")


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
        self.assertEqual(sorted(zipfile.ZipFile(backups[0]).namelist()), [".env", "data/uploads/cv.pdf"])

    def test_refuses_while_running(self):
        app = self._app()
        with mock.patch.object(sys, "argv", ["uninstaller", "--yes", "--folder", str(app)]), \
                mock.patch.object(uninstaller, "say"), mock.patch.object(uninstaller, "running_ports", return_value=[8000]):
            self.assertEqual(uninstaller.main(), 1)
        self.assertTrue(app.exists())
