"""Keys and storage on disk (Phase L review): strict mode that a database edit can't turn off, the write-ahead log
that is emptied again when a scrub was blocked, the database and its backups readable by their owner only, and one
process at a time on the key file. Each scenario runs in a fresh process on a throwaway data folder
(``helpers.scenario``), so nothing here touches the shared test database.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: E402  (sets the test environment first)

from backend import keys  # noqa: E402

MAKE_ADMIN = '''
from backend import keys, migrate_encryption, users
from backend.db import init_db
init_db()
users.startup()
account = users.create_account("owner@example.com", "correct horse battery")
out["user_id"] = account.user_id
migrate_encryption.run(account.user_id, account.dek)
'''

RESTART = '''
from sqlalchemy import text
from backend import crypto, keys, migrate_encryption, users
from backend.db import SessionLocal, engine, init_db
init_db()
users.startup()
'''


class StrictModeTest(unittest.TestCase):
    """The encryption run's "done" lives in the key file too: editing the database can't turn strict mode off."""

    def tamper(self, folder: str) -> dict:
        return helpers.scenario(RESTART + '''
PLANTED = "PLANTED-PLAINTEXT-NAME"
db = SessionLocal()
user = users.find_by_name(db, "owner@example.com")
dek = users.unlock_with_password(db, user, "correct horse battery")
db.close()
with engine.begin() as conn:  # what someone with write access to the database does
    conn.execute(text("UPDATE workspaces SET name = :n WHERE id = (SELECT MIN(id) FROM workspaces)"), {"n": PLANTED})
    conn.execute(text("UPDATE app_settings SET value_json = :v WHERE key = 'encryption_migration'"),
                 {"v": json.dumps({"state": "pending"})})
try:                                    # a run started by hand while the database says pending
    migrate_encryption.run(user.id, dek)
    out["run"] = "ran"
except migrate_encryption.MigrationError as exc:
    out["run"] = "refused"
migrate_encryption.at_startup()
out["strict"] = crypto.strict()
out["state"] = migrate_encryption.state().get("state")
with engine.connect() as conn:
    out["db_says_done"] = __import__("backend.db").db.encryption_done(conn)
out["ensure_started"] = migrate_encryption.ensure_started(user.id, dek, wait_s=5)
with engine.connect() as conn:
    out["name"] = conn.execute(text("SELECT name FROM workspaces WHERE id = (SELECT MIN(id) FROM workspaces)")).scalar()
out["planted"] = PLANTED
''', folder)

    def test_setting_the_state_back_does_not_turn_strict_mode_off_or_seal_planted_plaintext(self):
        folder = helpers.scratch_folder()
        first = helpers.scenario(MAKE_ADMIN + '''
out["marked"] = keys.encryption_marked()
out["file_has_it"] = "encryption_done_at" in open(os.path.join(FOLDER, "server-keys.json")).read()
''', folder)
        self.assertTrue(first["marked"] and first["file_has_it"])
        out = self.tamper(folder)
        self.assertTrue(out["strict"])                      # on at start-up, whatever the database says
        self.assertTrue(out["db_says_done"])
        self.assertTrue(out["ensure_started"])              # "done": nothing is started
        self.assertEqual(out["run"], "refused")
        self.assertEqual(out["state"], "done")              # the database is put right again
        self.assertEqual(out["name"], out["planted"])       # and the planted value was not sealed as if it were real

    def test_a_database_set_back_to_pending_gets_no_second_run_even_without_the_start_up_step(self):
        folder = helpers.scratch_folder()
        helpers.scenario(MAKE_ADMIN, folder)
        out = helpers.scenario(RESTART + '''
with engine.begin() as conn:
    conn.execute(text("DELETE FROM app_settings WHERE key = 'encryption_migration'"))
out["is_done"] = migrate_encryption.is_done()      # (no at_startup(): the key file alone decides)
out["strict"] = crypto.strict()
''', folder)
        self.assertEqual((out["is_done"], out["strict"]), (True, True))

    def test_an_install_from_before_the_marker_gets_it_at_its_next_start(self):
        folder = helpers.scratch_folder()
        helpers.scenario(MAKE_ADMIN, folder)
        path = Path(folder) / "server-keys.json"
        doc = json.loads(path.read_text())
        del doc["encryption_done_at"]                       # a key file written by the version before this one
        path.write_text(json.dumps(doc))
        out = helpers.scenario(RESTART + '''
out["before"] = keys.encryption_marked()
migrate_encryption.at_startup()
out["after"] = keys.encryption_marked()
out["strict"] = crypto.strict()
''', folder)
        self.assertEqual((out["before"], out["after"], out["strict"]), (False, True, True))

    def test_the_marker_survives_every_change_to_the_key_file(self):
        path = Path(tempfile.mkdtemp()) / "server-keys.json"
        self.addCleanup(lambda: __import__("shutil").rmtree(path.parent, ignore_errors=True))
        server = keys.ServerKeys.create(path)
        self.assertFalse(keys.encryption_marked(path))
        self.assertTrue(server.mark_encryption_done())
        self.assertFalse(server.mark_encryption_done())     # once
        server.rotate_signing()
        server.start_kek_rotation()
        server.finish_kek_rotation(lambda: [], lambda todo: None)
        server.rotate_pepper()
        self.assertTrue(keys.encryption_marked(path))
        again = keys.ServerKeys.load(path)
        self.assertTrue(again.encryption_done_at)
        again.rotate_signing()
        self.assertTrue(keys.encryption_marked(path))
        self.assertFalse(keys.encryption_marked(path.with_name("missing.json")))


class WalTest(unittest.TestCase):
    def test_a_blocked_scrub_is_remembered_and_the_log_is_emptied_when_the_reader_is_gone(self):
        out = helpers.scenario('''
import sqlite3
from pathlib import Path
from backend import db as dbm
from backend.db import DB_PATH, engine, init_db
init_db()
MARK = b"MARK-" + b"Q" * 40
with engine.begin() as conn:
    conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS scratch (id INTEGER PRIMARY KEY, v BLOB)")
    conn.exec_driver_sql("INSERT INTO scratch (id, v) VALUES (1, ?)", (MARK,))
reader = sqlite3.connect(str(DB_PATH), isolation_level=None)   # a request that is still reading
reader.execute("BEGIN")
reader.execute("SELECT COUNT(*) FROM scratch").fetchall()
with engine.begin() as conn:
    conn.exec_driver_sql("UPDATE scratch SET v = NULL WHERE id = 1")
wal = Path(str(DB_PATH) + "-wal")
out["wal_holds_it"] = MARK in wal.read_bytes()
out["blocked"] = dbm.scrub_wal() is False
out["flag"] = dbm._wal_blocked
out["still_there"] = MARK in wal.read_bytes()
reader.execute("COMMIT")
reader.close()
out["again"] = dbm.scrub_wal_again()
out["emptied"] = dbm.scrub_wal_again()      # nothing more is due
out["wal_size"] = wal.stat().st_size if wal.exists() else 0
out["in_main_file"] = MARK in DB_PATH.read_bytes()
with engine.connect() as conn:
    out["size_limit"] = conn.exec_driver_sql("PRAGMA journal_size_limit").scalar()
    out["secure_delete"] = conn.exec_driver_sql("PRAGMA secure_delete").scalar()
    out["busy_timeout"] = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
''')
        self.assertTrue(out["wal_holds_it"])                # the old value is in the log (the problem)
        self.assertTrue(out["blocked"] and out["flag"])     # a reader blocks the checkpoint, and it is remembered
        self.assertTrue(out["still_there"])
        self.assertTrue(out["again"])                       # the retry empties it
        self.assertFalse(out["emptied"])
        self.assertEqual(out["wal_size"], 0)
        self.assertFalse(out["in_main_file"])               # (secure_delete zeroed the page)
        self.assertEqual(out["size_limit"], 0)              # every reset of the log cuts the file
        self.assertEqual(out["secure_delete"], 1)
        self.assertEqual(out["busy_timeout"], 30000)        # the usual wait was put back after the scrub

    def test_the_clean_up_tick_tries_a_blocked_scrub_again(self):
        from datetime import datetime, timedelta, timezone

        from backend import db as dbm, schedule

        with mock.patch.object(schedule, "scrub_wal_again") as again, mock.patch.object(schedule, "_last_cleanup", None):
            with mock.patch("backend.auth.cleanup_tick"):
                self.assertTrue(schedule.housekeeping(datetime.now(timezone.utc) + timedelta(days=1)))
        again.assert_called_once()
        with mock.patch.object(dbm, "scrub_wal") as scrub, mock.patch.object(dbm, "_wal_blocked", False):
            self.assertFalse(dbm.scrub_wal_again())
        scrub.assert_not_called()

    def test_start_up_empties_the_log(self):
        out = helpers.scenario('''
from pathlib import Path
from fastapi.testclient import TestClient
from backend import db as dbm
calls = []
real = dbm.scrub_wal
import backend.app as appmod
appmod.scrub_wal = lambda: calls.append(1) or real()
with TestClient(appmod.app):
    pass
out["calls"] = len(calls)
''')
        self.assertGreaterEqual(out["calls"], 1)


@unittest.skipIf(os.name != "posix", "file modes are a POSIX thing")
class PermissionsTest(unittest.TestCase):
    def test_the_data_folder_the_database_and_its_log_are_owner_only(self):
        folder = helpers.scratch_folder()
        os.chmod(folder, 0o755)                                 # a folder made by an earlier version
        out = helpers.scenario('''
import os, stat
os.umask(0o022)
from backend.db import DATA_DIR, DB_PATH, engine, init_db
init_db()
with engine.begin() as conn:
    conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS scratch (id INTEGER PRIMARY KEY)")
    conn.exec_driver_sql("INSERT INTO scratch (id) VALUES (1)")
mode = lambda p: oct(stat.S_IMODE(os.stat(p).st_mode)) if os.path.exists(p) else None
out["dir"] = mode(DATA_DIR)
out["db"] = mode(DB_PATH)
out["wal"] = mode(str(DB_PATH) + "-wal")
out["shm"] = mode(str(DB_PATH) + "-shm")
''', folder)
        self.assertEqual(out["dir"], "0o700")
        self.assertEqual(out["db"], "0o600")
        self.assertEqual(out["wal"], "0o600")
        self.assertEqual(out["shm"], "0o600")

    def test_a_database_that_was_world_readable_is_tightened_at_the_next_start(self):
        folder = helpers.scratch_folder()
        helpers.scenario('''
from backend.db import engine, init_db
init_db()
''', folder)
        os.chmod(Path(folder) / "jobhunt.db", 0o644)
        out = helpers.scenario('''
import os, stat
from backend.db import DB_PATH, init_db
init_db()
out["db"] = oct(stat.S_IMODE(os.stat(DB_PATH).st_mode))
''', folder)
        self.assertEqual(out["db"], "0o600")

    def test_both_plaintext_backups_are_owner_only_from_the_first_byte(self):
        out = helpers.scenario('''
import os, sqlite3, stat
os.umask(0o022)
from backend import migrate_encryption, pool_migration
from backend.db import engine, init_db
init_db()
seen = []
real = sqlite3.connect
def watching(path, *a, **k):
    seen.append(oct(stat.S_IMODE(os.stat(path).st_mode)) if os.path.exists(path) else "missing")
    return real(path, *a, **k)
sqlite3.connect = watching
a = migrate_encryption.backup_database()
b = pool_migration._backup()
mode = lambda p: oct(stat.S_IMODE(os.stat(p).st_mode))
out["before_write"] = seen
out["files"] = [mode(a), mode(b)]
out["names"] = [a.name, b.name]
out["tmp_left"] = [n for n in os.listdir(FOLDER) if n.endswith(".tmp")]
''')
        self.assertEqual(out["before_write"], ["0o600", "0o600"])  # the copy exists, owner-only, before SQLite writes
        self.assertEqual(out["files"], ["0o600", "0o600"])
        self.assertEqual(out["tmp_left"], [])
        self.assertTrue(all(".bak-before-" in n for n in out["names"]))


class KeyFileLockTest(unittest.TestCase):
    """The app holds a lock on the key file for as long as it runs, however it was started."""

    def command(self, folder: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "backend.keys", *args], cwd=helpers.ROOT, text=True,
                              env=helpers.scenario_env(folder), capture_output=True, timeout=120)

    def start_app(self, folder: str) -> subprocess.Popen:
        """An app started with nothing but ``users.startup()`` (what uvicorn does): no launcher, no running.json."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "from backend import users\nfrom backend.db import init_db\ninit_db()\n"
             "users.startup()\nprint('READY', flush=True)\nimport sys\nsys.stdin.readline()"],
            cwd=helpers.ROOT, env=helpers.scenario_env(folder), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, proc)
        line = proc.stdout.readline()
        self.assertEqual(line.strip(), "READY", proc.stderr.read() if proc.poll() is not None else line)
        return proc

    @staticmethod
    def stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.stdin.close()
            proc.wait(timeout=30)
        for pipe in (proc.stdout, proc.stderr, proc.stdin):
            if pipe and not pipe.closed:
                pipe.close()

    def test_rotate_kek_refuses_while_an_app_without_the_launcher_runs_and_works_after(self):
        folder = helpers.scratch_folder()
        helpers.scenario(MAKE_ADMIN, folder)
        before = (Path(folder) / "server-keys.json").read_bytes()
        app = self.start_app(folder)
        self.assertFalse((Path(folder) / "running.json").exists())     # (the launcher's file isn't there)
        for args in (("rotate-kek",),):
            done = self.command(folder, *args)
            self.assertEqual(done.returncode, 2, (args, done.stdout, done.stderr))
            self.assertIn("using the key file", done.stderr)
        self.assertEqual((Path(folder) / "server-keys.json").read_bytes(), before)  # nothing was rewritten
        self.stop(app)
        done = self.command(folder, "rotate-kek")
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))

    def test_the_app_does_not_start_under_a_running_command(self):
        folder = helpers.scratch_folder()
        helpers.scenario(MAKE_ADMIN, folder)
        holder = subprocess.Popen(
            [sys.executable, "-c", "from backend import keys\nkeys.hold_key_file()\nprint('READY', flush=True)\nimport sys\nsys.stdin.readline()"],
            cwd=helpers.ROOT, env=helpers.scenario_env(folder), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True)
        self.addCleanup(self.stop, holder)
        self.assertEqual(holder.stdout.readline().strip(), "READY")
        out = helpers.scenario('''
from backend import keys, users
from backend.db import init_db
init_db()
try:
    users.startup()
    out["refused"] = None
except keys.KeyFileError as exc:
    out["refused"] = str(exc)
''', folder)
        self.assertIn("using the key file", out["refused"])

    def test_a_process_may_ask_again_and_the_lock_goes_with_the_process(self):
        folder = helpers.scratch_folder()
        out = helpers.scenario('''
from backend import keys
keys.hold_key_file()
keys.hold_key_file()            # asking again in the process that holds it is fine
out["held"] = True
''', folder)
        self.assertTrue(out["held"])
        out = helpers.scenario('''
from backend import keys
keys.hold_key_file()            # (the first process is gone, so is its lock)
out["held"] = True
''', folder)
        self.assertTrue(out["held"])

    def test_a_wrap_is_refused_when_another_process_changed_the_key_file(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        running = keys.ServerKeys.create(folder / "server-keys.json")
        running.kek_wrap(b"s" * 32, "srv|1")                     # fine while the file is as loaded
        elsewhere = keys.ServerKeys.load(folder / "server-keys.json")
        elsewhere.rotate_kek(lambda: [], lambda todo: None)       # what a command running beside the app did
        with self.assertRaises(keys.KeyFileError):
            running.kek_wrap(b"s" * 32, "srv|1")                 # it would have used the retired key
        # the app that was restarted (loaded again) wraps under the new one, and the other process still opens it
        restarted = keys.ServerKeys.load(folder / "server-keys.json")
        _kid, blob = restarted.kek_wrap(b"s" * 32, "srv|1")
        self.assertEqual(elsewhere.kek_unwrap(blob, "srv|1"), b"s" * 32)


if __name__ == "__main__":
    unittest.main()
