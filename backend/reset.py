"""The reset tool: start JobHuntPA over when the password and every recovery code are lost (open-source spec §1.2).

Run on the computer JobHuntPA is installed on, with the app stopped::

    ./reset.sh                     (Linux, macOS; "Reset JobHuntPA.command" on a Mac)
    Start menu > Reset JobHuntPA   (Windows: JobHuntPA-Reset.exe)
    python -m backend.reset [--confirm RESET]

It says what will be lost, asks for the word RESET to be typed (``--confirm RESET`` stands for typing it, for scripts;
without a terminal and without it, nothing is done), and refuses while the app is running (the app holds the key
file's lock for as long as it runs, and ``data/running.json`` names the launcher's process: backend/key_rotation.py
``refuse_while_running``). Then, in **one transaction**, it deletes every account and everything only an account
could read: the users with their keys, recovery codes, sessions and browser-extension keys, every workspace and what
is in it (jobs, scores, cover letters, documents, chats, search setup, pins, schedules, runs), the commute cache and
the page cache (both sealed); usage counts are kept without saying whose they were. After the commit the
write-ahead log is scrubbed (``db.scrub_wal``), then the files go: the uploads (sealed), the plaintext copies left
from before encryption (overwritten first), the server key file (its KEK, pepper and signing key) and the first-run
token. The next start is a first run, with a new key file.

**Kept:** the settings (``.env``: the AI provider, model and key, the commute keys, the public address and host
names), the reasoning levels, the shared pool of public job ads with their ghost-job signs, the company research, the
map lookups and ``data/sources.json``.

**No endpoint does any of this**: it is a command run on the computer itself, and nothing the web server serves can
start it. The uninstaller removes the Start-menu entry that the Windows setup adds for it.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import inspect, text

from backend import keys
from backend.db import DB_PATH, UPLOAD_DIR, engine, scrub_wal

log = logging.getLogger(__name__)

WORD = "RESET"

# Every table and what the reset does with it. tests/test_reset.py checks that every table the app defines is here,
# so a new table has to be put in one list or the other.
WIPED_TABLES = (  # children before parents (foreign keys are on)
    "chat_messages", "chat_threads", "cover_letters", "fit_results", "job_duplicates", "jobs", "documents", "pins",
    "company_sources", "search_runs", "search_schedules", "user_profile", "dealbreakers", "search_profile",
    "commute_cache", "http_cache", "workspaces", "sessions", "recovery_codes", "user_keys",
    # an install that wasn't started since the update may still have these (backend/db.py RETIRED_TABLES)
    "auth_tokens", "admin_key_grants", "admin_audit", "membership_grants", "memberships", "email_prefs",
    "users",
)
KEPT_TABLES = ("postings", "company_profiles", "geocode_cache", "app_settings", "llm_usage")
# Settings that belong to the accounts and the key file: the next first run starts them again.
WIPED_SETTINGS = ("server_keys_fingerprint", "encryption_migration", "apk_public", "apk_version", "signup_mode")
FIRST_RUN_FILE = "first-run.txt"


class ResetRefused(Exception):
    """The reset can't go ahead; the message says why (and that nothing was changed)."""


def _counts(conn, tables: set[str]) -> dict[str, int]:
    def count(table: str) -> int:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() if table in tables else 0

    return {"accounts": count("users"), "workspaces": count("workspaces"), "jobs": count("jobs"),
            "documents": count("documents")}


def _upload_files() -> list[Path]:
    if not UPLOAD_DIR.is_dir():
        return []
    return [p for p in sorted(UPLOAD_DIR.iterdir()) if p.is_file() and not p.name.startswith(".")]


def summary(out) -> dict[str, int]:
    """Says what will be lost and what is kept. Returns the counts."""
    with engine.connect() as conn:
        found = _counts(conn, set(inspect(conn).get_table_names()))
    files = len(_upload_files())
    print("Resetting JobHuntPA deletes, for good:", file=out)
    print(f"  * {found['accounts']} account(s), with their passwords, recovery codes, signed-in devices, browser-"
          "extension keys and background access;", file=out)
    print(f"  * {found['workspaces']} workspace(s) with everything in them: {found['jobs']} saved job(s) with their "
          f"scores, notes and cover letters, {found['documents']} document(s) ({files} uploaded file(s)), chats, "
          "search setup, pins and scheduled searches;", file=out)
    print("  * the server key file, and any unencrypted backups left from before encryption.", file=out)
    print("It keeps your settings (.env: the AI provider, model and key, the commute keys, the public address), the "
          "shared job ads and the company research.", file=out)
    print("Nothing can bring the deleted data back. If you can still sign in, use Settings > Download your data first.",
          file=out)
    return {**found, "files": files}


def wipe() -> dict[str, int]:
    """Deletes the accounts and their data in one transaction, scrubs the write-ahead log, then removes the files.
    The caller holds the key file's lock (``refuse_while_running``). Returns what was removed."""
    with engine.begin() as conn:
        if not conn.connection.driver_connection.in_transaction:
            conn.execute(text("BEGIN IMMEDIATE"))
        tables = set(inspect(conn).get_table_names())
        found = _counts(conn, tables)
        if "llm_usage" in tables:  # what the AI cost stays; whose request it was doesn't
            conn.execute(text("UPDATE llm_usage SET user_id = NULL, workspace_id = NULL "
                              "WHERE user_id IS NOT NULL OR workspace_id IS NOT NULL"))
        for table in WIPED_TABLES:
            if table in tables:
                conn.execute(text(f"DELETE FROM {table}"))
        if "app_settings" in tables:
            names = ", ".join(f"'{name}'" for name in WIPED_SETTINGS)
            conn.execute(text(f"DELETE FROM app_settings WHERE key IN ({names})"))
    scrub_wal()  # the deleted keys and secrets must not linger in the write-ahead log
    from backend import migrate_encryption  # (plaintext copies: overwritten, then deleted)

    leftovers = len(migrate_encryption.delete_leftovers())
    files = 0
    for path in _upload_files():
        try:
            path.unlink()
            files += 1
        except OSError as exc:
            log.warning("Could not delete an upload: %s", exc.__class__.__name__)
    key_file = keys.key_file_path()
    for path in (key_file, key_file.with_name(key_file.name + ".new"), Path(DB_PATH).parent / FIRST_RUN_FILE):
        path.unlink(missing_ok=True)
    return {**found, "files": files, "leftovers": leftovers}


def run(*, confirm: Optional[str] = None, ask: Optional[Callable[[str], str]] = None, interactive: Optional[bool] = None,
        out=None, err=None) -> int:
    """The reset with its checks. ``confirm``: the word given on the command line; otherwise ``ask`` (input) is used
    when ``interactive``. Returns the exit status: 0 done, 1 refused (nothing changed)."""
    out, err = out or sys.stdout, err or sys.stderr
    ask = ask or input
    interactive = sys.stdin.isatty() if interactive is None else interactive
    try:
        _check_and_reset(confirm, ask, interactive, out)
    except ResetRefused as exc:
        print(str(exc), file=err)
        return 1
    return 0


def _check_and_reset(confirm: Optional[str], ask: Callable[[str], str], interactive: bool, out) -> None:
    from backend import key_rotation

    try:
        key_rotation.refuse_while_running()  # also takes the key file's lock, so the app can't start meanwhile
    except keys.KeyFileError as exc:
        raise ResetRefused(f"{exc} Nothing was changed.") from None
    if not Path(DB_PATH).exists():
        raise ResetRefused(f"There is no database at {DB_PATH}, so there is nothing to reset. Nothing was changed.")
    summary(out)
    if confirm is None:
        if not interactive:
            raise ResetRefused(f"Nothing was changed. To reset without questions, run it with --confirm {WORD}.")
        try:
            confirm = ask(f"\nType {WORD} to delete all of this, or press Enter to stop: ")
        except EOFError:
            confirm = ""
    if (confirm or "").strip().upper() != WORD:
        raise ResetRefused("Nothing was changed.")
    removed = wipe()
    log.info("Reset: %d account(s), %d workspace(s), %d upload(s) and %d leftover file(s) removed",
             removed["accounts"], removed["workspaces"], removed["files"], removed["leftovers"])
    print(f"\nDone. {removed['accounts']} account(s) and {removed['workspaces']} workspace(s) were deleted, with "
          f"{removed['files']} uploaded file(s) and the server key file.", file=out)
    print("Start JobHuntPA again: it opens its first run, where you create a new account.", file=out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.reset",
                                     description="Delete JobHuntPA's account and its data, keeping the settings, and "
                                                 "go back to the first run. Run it with the app stopped.")
    parser.add_argument("--confirm", metavar="WORD", help=f"answer the question with WORD ({WORD} goes ahead)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return run(confirm=args.confirm)


if __name__ == "__main__":
    sys.exit(main())
