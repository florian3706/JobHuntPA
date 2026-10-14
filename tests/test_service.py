"""service.sh (open-source spec §1.6): a systemd user service that starts JobHuntPA at boot, with options for the port
and the data folder. The script runs for real on a copy of the app folder, with stand-ins for systemctl and loginctl
that record what they are asked to do (nothing is installed on this computer)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
case "$*" in *show-user*) echo "Linger=no" ;; esac
exit 0
"""


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "service.sh is for Linux")
class ServiceScriptTest(unittest.TestCase):
    def setUp(self):
        self.top = Path(tempfile.mkdtemp(prefix="jobhunt-service-"))
        self.addCleanup(shutil.rmtree, self.top, True)
        self.app = self.top / "My Apps" / "JobHuntPA"
        (self.app / "backend").mkdir(parents=True)
        (self.app / ".venv" / "bin").mkdir(parents=True)
        shutil.copy(ROOT / "service.sh", self.app / "service.sh")
        shutil.copy(ROOT / "backend" / "paths.py", self.app / "backend" / "paths.py")
        (self.app / "backend" / "__init__.py").write_text("")
        os.symlink(sys.executable, self.app / ".venv" / "bin" / "python")
        self.bin = self.top / "bin"
        self.bin.mkdir()
        for name in ("systemctl", "loginctl"):
            (self.bin / name).write_text(STUB)
            (self.bin / name).chmod(0o755)
        self.home = self.top / "home"
        self.home.mkdir()
        self.log = self.top / "calls.log"
        self.unit = self.home / ".config" / "systemd" / "user" / "jobhuntpa.service"

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(self.home),
               "USER": "someone", "STUB_LOG": str(self.log), "JOBHUNT_CONFIG_DIR": str(self.top / "config")}
        return subprocess.run(["bash", str(self.app / "service.sh"), *args], env=env, capture_output=True, text=True,
                              timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_install_with_a_port_and_a_data_folder(self):
        data = self.top / "data 100%"
        done = self.run_script("install", "--port", "8123", "--data-dir", str(data))
        self.assertEqual(done.returncode, 0, done.stderr)
        unit = self.unit.read_text()
        self.assertIn(f'Environment="JOBHUNT_DATA_DIR={self.top}/data 100%%"', unit)
        self.assertIn(f'WorkingDirectory="{self.app}"', unit)
        self.assertIn('"--host" "127.0.0.1"', unit)  # only this computer: other devices come through a proxy
        self.assertIn('"--forwarded-allow-ips" "127.0.0.1"', unit)
        self.assertIn('"--port" "8123"', unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertEqual(oct(self.unit.stat().st_mode & 0o777), "0o600")
        self.assertTrue(data.is_dir())
        self.assertIn("systemctl --user enable jobhuntpa.service", self.calls())
        self.assertIn("systemctl --user restart jobhuntpa.service", self.calls())
        self.assertIn("loginctl enable-linger someone", self.calls())
        self.assertIn("first-run.txt", done.stdout)
        self.assertFalse((self.top / "config").exists(), "the service passes the folder; no pointer is written")

    def test_the_default_data_folder_is_the_one_in_use_else_the_linux_default(self):
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertIn(f'JOBHUNT_DATA_DIR={self.home}/.local/share/JobHuntPA"', self.unit.read_text())
        self.assertIn('"--port" "8000"', self.unit.read_text())
        (self.app / "data").mkdir()
        (self.app / "data" / "jobhunt.db").write_bytes(b"")
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertIn(f'JOBHUNT_DATA_DIR={self.app}/data"', self.unit.read_text())

    def test_bad_options_are_refused(self):
        for args in (("--port", "80"), ("--port", "eighty"), ("--data-dir", "relative/folder"), ("--data-dir", "~root/x"),
                     ("--colour", "red")):
            done = self.run_script("install", *args)
            self.assertNotEqual(done.returncode, 0, args)
            self.assertFalse(self.unit.exists(), args)
        self.assertNotEqual(self.run_script("start").returncode, 0)

    def test_a_data_folder_of_other_files_is_refused(self):
        """The same rule as the first run's folder step: the uninstaller removes the data folder, so it never takes a
        folder of the person's own files; a file is refused too. One that already holds JobHuntPA data is fine."""
        mine = self.top / "Documents"
        mine.mkdir()
        (mine / "taxes.pdf").write_text("taxes")
        done = self.run_script("install", "--data-dir", str(mine))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("other files", done.stderr)
        self.assertFalse(self.unit.exists())
        self.assertEqual(sorted(p.name for p in mine.iterdir()), ["taxes.pdf"])
        self.assertNotEqual(self.run_script("install", "--data-dir", str(mine / "taxes.pdf")).returncode, 0)
        self.assertFalse(self.unit.exists())
        (mine / "jobhunt.db").write_bytes(b"")
        self.assertEqual(self.run_script("install", "--data-dir", str(mine)).returncode, 0)
        self.assertIn(f'JOBHUNT_DATA_DIR={mine}"', self.unit.read_text())

    def test_a_dollar_in_the_app_path_is_kept_in_execstart(self):
        """systemd replaces $NAME in ExecStart=: the script writes $$ for a plain $, and the uninstaller reads it back."""
        moved = self.top / "Price $HOME" / "JobHuntPA"
        moved.parent.mkdir()
        shutil.move(str(self.app), str(moved))
        self.app = moved
        done = self.run_script("install", "--data-dir", str(self.top / "d"))
        self.assertEqual(done.returncode, 0, done.stderr)
        unit = self.unit.read_text()
        self.assertIn(f'ExecStart="{self.top}/Price $$HOME/JobHuntPA/.venv/bin/python"', unit)
        self.assertIn(f'WorkingDirectory="{moved}"', unit)  # (no $ expansion there)
        sys.path.insert(0, str(ROOT / "installer"))
        import uninstaller
        from unittest import mock

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config")}):
            self.assertEqual(uninstaller.service_data_folder(moved), self.top / "d")
            self.assertTrue(uninstaller.service_is_ours(moved, unit))
            self.assertFalse(uninstaller.service_is_ours(self.top / "Price $HOME", unit))

    def test_status_and_remove(self):
        self.assertIn("isn't installed", self.run_script("status").stdout)
        self.run_script("install", "--data-dir", str(self.top / "d"))
        done = self.run_script("status")
        self.assertEqual(done.returncode, 0)
        self.assertIn("doesn't answer", done.stdout)
        done = self.run_script("remove")
        self.assertEqual(done.returncode, 0)
        self.assertFalse(self.unit.exists())
        self.assertIn("systemctl --user disable --now jobhuntpa.service", self.calls())
        self.assertTrue((self.top / "d").is_dir(), "the data stays")

    def test_remove_leaves_a_pointer_to_the_data_so_start_sh_keeps_using_it(self):
        """The service passed its folder in JOBHUNT_DATA_DIR and wrote no pointer; once it is removed, starting
        JobHuntPA by hand must not ask where the data lives again (nor start empty somewhere else)."""
        data = self.top / "data \"100%\""
        self.assertEqual(self.run_script("install", "--data-dir", str(data)).returncode, 0)
        (data / "jobhunt.db").write_bytes(b"")  # what the service's first start makes
        self.assertFalse((self.top / "config").exists())
        done = self.run_script("remove")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"Your data stays in {data}", done.stdout)
        sys.path.insert(0, str(ROOT))
        from backend import paths
        env = {"JOBHUNT_CONFIG_DIR": str(self.top / "config"), "HOME": str(self.home)}
        self.assertEqual(paths.read_pointer(env), data)
        self.assertEqual(paths.locate(self.app, env).folder, data)

    def test_remove_keeps_a_pointer_that_names_another_folder(self):
        sys.path.insert(0, str(ROOT))
        from backend import paths
        env = {"JOBHUNT_CONFIG_DIR": str(self.top / "config"), "HOME": str(self.home)}
        other = self.top / "other"
        other.mkdir()
        (other / "jobhunt.db").write_bytes(b"")
        paths.write_pointer(other, env)
        data = self.top / "d"
        self.assertEqual(self.run_script("install", "--data-dir", str(data)).returncode, 0)
        (data / "jobhunt.db").write_bytes(b"")
        done = self.run_script("remove")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"Your data stays in {data}", done.stdout)
        self.assertEqual(paths.read_pointer(env), other)

    def test_the_uninstaller_finds_this_service(self):
        sys.path.insert(0, str(ROOT / "installer"))
        import uninstaller
        from unittest import mock

        data = self.top / "service-data"
        self.run_script("install", "--data-dir", str(data))
        (data / "jobhunt.db").write_bytes(b"")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config")}):
            self.assertEqual(uninstaller.service_data_folder(self.app), data)
            self.assertIn(data, uninstaller.data_folders(self.app))


if __name__ == "__main__":
    unittest.main()
