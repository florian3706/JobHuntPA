"""``python -m backend.keys recover-lost --admin-email X``: carry on after ``data/server-keys.json`` was lost and
there's no backup of it (accounts design 1.3, "Lost key file"). Run it with JobHuntPA stopped.

The key file holds the server KEK (it wraps the data keys of background-access accounts),
the pepper (email addresses are found by ``HMAC(pepper, email)``) and the signing key. Without it nobody can sign in,
because no email address finds its account. What it can't give back is the KEK's wraps, which are copies of keys
people can also unlock with their passwords or recovery codes. So:

  1. An admin proves who they are: the password is checked against the admin accounts (the email can't be used to
     look one up: that needs the lost pepper) and the account's own copy of its details must name ``--admin-email``. A turned-off admin is refused like any
     other mismatch.
  2. Their password opens their identity key, which opens their X25519 private key, which opens their copy of the
     admin details key (APK), which opens every account's ``pii_admin``. (An admin who hasn't signed in since the
     identity key was introduced still has the private key under the data key: that form is read too.)
  3. A new key file is made. Every ``email_hash`` is computed again with the new pepper from the decrypted email
     address and every ``server_wrapped_dek`` is deleted (background access is off until each person turns it on
     again), in one transaction. The new file's fingerprint is stored.
  4. The new file is put in place last. A crash before that leaves a database that matches no key file; running the
     command again starts over from step 1.

Accounts whose ``pii_admin`` can't be opened (stale after a new admin key, see 1.4) keep their old hash, which no
email address finds any more; the summary says how many. The summary never names anyone.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import func, update

from backend import crypto, key_rotation, keys, users
from backend.db import (DB_PATH, AdminKeyGrant, AppSetting, SessionLocal, User, UserKeys, encryption_done,
                        scrub_wal)

log = logging.getLogger(__name__)

NO_MATCH = "No admin account matches that email address and password. Nothing was changed."


class RecoveryError(Exception):
    """The recovery can't go ahead; the message says why (and that nothing was changed)."""


def _find_admin(db, admin_email: str, password: str) -> tuple[User, bytes, Optional[bytes]]:
    """The admin row whose password this is and whose own copy of its details names ``admin_email``, with its data
    key and identity key (None before the account had one). The password is checked against every admin row (there
    are few), then the email against the match."""
    wanted = keys.normalise_email(admin_email)
    for user in db.query(User).filter(User.role == "admin", User.status == "active").order_by(User.id):
        derived = crypto.derive_password(password, user.kdf_salt, crypto.KdfParams.from_json(user.kdf_params))
        if not crypto.verify_password(user.pw_verifier, derived):
            continue
        row = db.get(UserKeys, user.id)
        try:
            dek = crypto.unwrap(derived.kek, row.pw_wrapped_dek, f"pw|{user.id}")
            ik = crypto.unwrap(derived.kek, row.pw_wrapped_ik, f"pw-ik|{user.id}") if row.pw_wrapped_ik is not None \
                else None
            with crypto.as_user(user.id, dek):
                details = json.loads(crypto.unseal(user.pii_self, f"users.pii_self|{user.id}"))
        except (crypto.CryptoError, ValueError, TypeError):
            continue
        email = details.get("email") if isinstance(details, dict) else None
        if isinstance(email, str) and keys.normalise_email(email) == wanted:
            return user, dek, ik
    raise RecoveryError(NO_MATCH)


def _admin_details_key(db, admin: User, dek: bytes, ik: Optional[bytes]) -> bytes:
    """The admin details key's private half, opened through this admin's grant of it."""
    grant = db.get(AdminKeyGrant, admin.id)
    if grant is None:
        raise RecoveryError("This admin doesn't hold the admin details key, so email addresses can't be worked out "
                            "again. Try another admin. Nothing was changed.")
    row = db.get(UserKeys, admin.id)
    try:
        if ik is not None:
            private = users.open_private_key(row, admin.id, ik)
        else:  # an account from before the identity key
            with crypto.as_user(admin.id, dek):
                private = crypto.unseal(row.x25519_priv_enc, f"user_keys.x25519_priv|{admin.id}")
        return crypto.open_sealed(private, grant.sealed_priv, f"grant|{admin.id}|{grant.apk_version}")
    except crypto.CryptoError:
        raise RecoveryError("This admin's copy of the admin details key can't be opened. Try another admin. "
                            "Nothing was changed.") from None


def _new_hashes(db, apk_private: bytes, new: keys.ServerKeys, admin: User, admin_email: str) -> tuple[dict, int]:
    """``({user id: new email_hash}, how many accounts couldn't be worked out)``."""
    hashes: dict[int, bytes] = {}
    missing = 0
    for user in db.query(User).order_by(User.id):
        email = None
        if user.pii_admin is not None:
            try:
                details = json.loads(crypto.open_sealed(apk_private, user.pii_admin, f"users.pii_admin|{user.id}"))
                email = details.get("email") if isinstance(details, dict) else None
            except (crypto.CryptoError, ValueError, TypeError):
                email = None
        if user.id == admin.id and not isinstance(email, str):
            email = admin_email  # this admin's own copy was opened (and named this address) to get here
        if isinstance(email, str) and email.strip():
            hashes[user.id] = new.email_hash(email)
        else:
            missing += 1
    return hashes, missing


def run(admin_email: str, *, ask_password: Optional[Callable[[str], str]] = None, out=None, err=None,
        path: Optional[Path] = None) -> int:
    """Recover from a lost key file. Returns the exit status: 0 done, 1 refused or failed (nothing changed)."""
    out, err = out or sys.stdout, err or sys.stderr
    ask_password = ask_password or getpass.getpass
    path = Path(path) if path else keys.key_file_path()
    try:
        return _recover(admin_email, ask_password, out, path)
    except RecoveryError as exc:
        print(str(exc), file=err)
        return 1


def _recover(admin_email: str, ask_password: Callable[[str], str], out, path: Path) -> int:
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
        if db.query(User.id).first() is None:
            raise RecoveryError("There are no accounts, so there is nothing to recover: start JobHuntPA normally, "
                                "which makes a new key file.")
        password = ask_password(f"Password of the admin account {admin_email}: ")
        if not isinstance(password, str) or not password:
            raise RecoveryError("No password was entered. Nothing was changed.")
        admin, dek, ik = _find_admin(db, admin_email, password)
        apk_private = _admin_details_key(db, admin, dek, ik)

        # The new key file is written beside the final one and moved into place after the database has changed.
        staged = path.with_name(path.name + ".new")
        staged.unlink(missing_ok=True)  # left by an earlier try that stopped half way: never adopted
        new = keys.ServerKeys.create(staged)
        try:
            if encryption_done(db.connection()):  # the new file must keep saying so (strict mode stays on)
                new.mark_encryption_done()
            hashes, missing = _new_hashes(db, apk_private, new, admin, admin_email)
            background = db.query(func.count()).select_from(UserKeys).filter(
                UserKeys.server_wrapped_dek.isnot(None)).scalar()
            try:
                for uid, value in hashes.items():
                    db.execute(update(User).where(User.id == uid).values(email_hash=value))
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
    log.info("recover-lost: new key file written; %d account(s) re-indexed, %d not", len(hashes), missing)
    print(f"A new key file was written: {path}", file=out)
    print(f"Email lookup was rebuilt for {len(hashes)} account(s)."
          + (f" {missing} could not be worked out, so they can't sign in by email address (see the README)."
             if missing else ""), file=out)
    print(f"Background access was switched off for {background} account(s): each person turns it on again in "
          "Settings. Back up the new file together with the database.", file=out)
    return 0
