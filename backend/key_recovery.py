"""``python -m backend.keys recover-lost --name X``: carry on after ``data/server-keys.json`` was lost and there's no
backup of it (accounts design 1.3, "Lost key file"). Run it with JobHuntPA stopped.

The key file holds the server KEK (it wraps the data keys of background-access accounts), the pepper (accounts are
found by ``HMAC(pepper, name)``) and the signing key. Without it nobody can sign in, because no name finds its account.
What it can't give back is the KEK's wraps, which are copies of keys people can also unlock with their passwords or
recovery codes. So:

  1. The account's owner proves who they are: the password is checked against every active account (the name can't
     be used to look one up: that needs the lost pepper), and the account's own copy of its name (``pii_self``, under
     the data key the password opens) must be the ``--name`` given. A turned-off account is refused like any other
     mismatch. An install from before the open-source version may hold several accounts: give ``--name`` once for
     each, and each password is asked in turn.
  2. A new key file is made. The ``email_hash`` of each account proven in step 1 is computed again with the new
     pepper, and every ``server_wrapped_dek`` is deleted (background access is off until each person turns it on
     again), in one transaction, and the write-ahead log is scrubbed. The new file's fingerprint is stored.
  3. The new file is put in place last. A crash before that leaves a database that matches no key file; running the
     command again starts over from step 1.

An account not named (or whose password wasn't given) keeps its old hash, which no name finds any more; the summary
says how many. Nobody else can sign them in, since their names are sealed under their own keys. The summary never
names anyone.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import func, update

from backend import crypto, key_rotation, keys, users
from backend.db import DB_PATH, AppSetting, SessionLocal, User, UserKeys, encryption_done, scrub_wal

log = logging.getLogger(__name__)

NO_MATCH = "No account matches that name and password. Nothing was changed."


class RecoveryError(Exception):
    """The recovery can't go ahead; the message says why (and that nothing was changed)."""


def _find_account(db, name: str, password: str, skip: set[int]) -> User:
    """The active account whose password this is and whose own copy of its name is ``name`` (names are compared as
    sign-in compares them). The password is checked against every active account not in ``skip`` (there are few)."""
    wanted = keys.normalise_email(name)
    for user in db.query(User).filter(User.status == "active").order_by(User.id):
        if user.id in skip:
            continue
        derived = crypto.derive_password(password, user.kdf_salt, crypto.KdfParams.from_json(user.kdf_params))
        if not crypto.verify_password(user.pw_verifier, derived):
            continue
        row = db.get(UserKeys, user.id)
        try:
            dek = crypto.unwrap(derived.kek, row.pw_wrapped_dek, f"pw|{user.id}")
            with crypto.as_user(user.id, dek):
                found = users._sign_in_name(json.loads(crypto.unseal(user.pii_self, f"users.pii_self|{user.id}")))
        except (crypto.CryptoError, ValueError, TypeError):
            continue
        if found and keys.normalise_email(found) == wanted:
            return user
    raise RecoveryError(NO_MATCH)


def run(names: Iterable[str], *, ask_password: Optional[Callable[[str], str]] = None, out=None, err=None,
        path: Optional[Path] = None) -> int:
    """Recover from a lost key file. ``names``: the sign-in name of each account to re-index (an older install's are
    email addresses). Returns the exit status: 0 done, 1 refused or failed (nothing changed)."""
    out, err = out or sys.stdout, err or sys.stderr
    ask_password = ask_password or getpass.getpass
    path = Path(path) if path else keys.key_file_path()
    names = [names] if isinstance(names, str) else [n for n in names if isinstance(n, str) and n.strip()]
    try:
        if not names:
            raise RecoveryError("Give the account's name with --name. Nothing was changed.")
        return _recover(names, ask_password, out, path)
    except RecoveryError as exc:
        print(str(exc), file=err)
        return 1


def _recover(names: list[str], ask_password: Callable[[str], str], out, path: Path) -> int:
    try:
        key_rotation.refuse_while_running()  # a running app would go on wrapping under the keys thrown away here
    except keys.KeyFileError as exc:
        raise RecoveryError(str(exc)) from None
    if path.exists():
        raise RecoveryError(f"A key file already exists ({path}). recover-lost is only for a lost one; if this file "
                            "is the wrong one, move it away first. Nothing was changed.")
    if not Path(DB_PATH).exists():
        raise RecoveryError(f"There is no database at {DB_PATH}, so there is nothing to recover.")
    db = SessionLocal()
    try:
        total = db.query(func.count(User.id)).scalar()
        if not total:
            raise RecoveryError("There are no accounts, so there is nothing to recover: start JobHuntPA normally, "
                                "which makes a new key file.")
        proven: dict[int, str] = {}
        for name in names:
            password = ask_password(f"Password of the account {name}: ")
            if not isinstance(password, str) or not password:
                raise RecoveryError("No password was entered. Nothing was changed.")
            user = _find_account(db, name, password, set(proven))
            proven[user.id] = name

        # The new key file is written beside the final one and moved into place after the database has changed.
        staged = path.with_name(path.name + ".new")
        staged.unlink(missing_ok=True)  # left by an earlier try that stopped half way: never adopted
        new = keys.ServerKeys.create(staged)
        try:
            if encryption_done(db.connection()):  # the new file must keep saying so (strict mode stays on)
                new.mark_encryption_done()
            background = db.query(func.count()).select_from(UserKeys).filter(
                UserKeys.server_wrapped_dek.isnot(None)).scalar()
            try:
                for uid, name in proven.items():
                    db.execute(update(User).where(User.id == uid).values(email_hash=new.email_hash(name)))
                db.execute(update(UserKeys).values(server_wrapped_dek=None, server_kek_id=None,
                                                   background_since=None))
                db.merge(AppSetting(key="server_keys_fingerprint", value_json=json.dumps(new.fingerprint())))
                db.commit()
                scrub_wal()  # the old key copies must not linger in the write-ahead log
            except BaseException:
                db.rollback()
                raise
            os.replace(staged, path)
            crypto.fsync_dir(path.parent)
        finally:
            staged.unlink(missing_ok=True)
    finally:
        db.close()
    missing = total - len(proven)
    log.info("recover-lost: new key file written; %d account(s) re-indexed, %d not", len(proven), missing)
    print(f"A new key file was written: {path}", file=out)
    print(f"Signing in was restored for {len(proven)} account(s)."
          + (f" {missing} other account(s) were not named, so they can't sign in (see the README)." if missing else ""),
          file=out)
    print(f"Background access was switched off for {background} account(s): turn it on again in Settings. Back up "
          "the new file together with the database.", file=out)
    return 0
