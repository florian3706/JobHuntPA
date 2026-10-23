"""The account: creating it with its keys and recovery codes, unlocking it with a password, the server key file at
start-up, and the first run. Design: docs/design/accounts.md, sections 1, 4 and 5; docs/design/open-source.md §1.2.

**One account per install.** The first run creates it (``create_account``); once an account exists, nothing creates
another: the first-run page, the start-up path from the environment and every other caller get ``AlreadySetUp``. An
install from before the open-source version may hold several accounts: each keeps signing in and using its own
workspaces, and none can be added.

**A name and a password.** The account is found by ``email_hash``, HMAC(pepper, normalised name) (backend.keys), and
its own copy of the name is sealed under its data key (``pii_self``). On an older install the email address an account
was made with is its sign-in name; ``change_name`` replaces it.

**Keys of an account** (all made here, at creation):
  * a random data key (DEK) for all of the user's data, wrapped under the password (``pw|{uid}``) and under each of
    8 recovery codes (``rc|{uid}|{code_id}``); a session wraps it again under its cookie (backend.sessions);
  * a second random key, the identity key (IK), which keeps the recovery codes not yet confirmed as saved
    (``pending-codes|{uid}``). It is wrapped like the DEK, under the password (``pw-ik|{uid}``), each recovery code
    (``rc-ik|{uid}|{code_id}``) and each session's cookie (backend.sessions), but **never under the server KEK**: a
    code opens the account, so background access (the DEK under the KEK) must not reach the codes. An X25519 key pair,
    its private half under the IK (``ik-priv|{uid}``), checks that a key is this account's IK;
  * ``pii_self`` ({name}) under the DEK;
  * with background access, the DEK under the server KEK as well (``srv|{uid}|{kek_id}``).

**First run** (no account yet). Every API except /api/health, /api/auth/state and /api/auth/first-run answers 401
``setup_required``. At start-up:
  * if the process environment (not .env) has JOBHUNT_ADMIN_EMAIL (the account's name, despite the old variable
    name) and JOBHUNT_ADMIN_PASSWORD, the account is created at once (CI and headless servers);
  * otherwise a one-time token is written to ``first-run.txt`` beside the database (owner-only) and logged as
    ``/first-run.html#token=…``; installer/launch.py opens that address. The token is in the fragment, so it never
    reaches a server log. The launcher makes the token itself and hands it over (JOBHUNT_FIRST_RUN_TOKEN), so the
    same one also works for the step before, choosing the data folder (backend/data_folder.py), when it is kept in
    memory only.
``create_account`` then makes the account and claims every workspace that has no owner (all the data of an install
from before accounts), in one transaction, and records in ``app_settings["encryption_migration"]`` that this account's
key encrypts that data (backend.migrate_encryption: at once for the first run and the environment account, otherwise
at its next sign-in). The reset tool (backend/reset.py, run on the computer itself) is the only way back to the first
run.
"""
from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from backend import config, crypto, keys, ratelimit
from backend.db import (
    DB_PATH,
    UPLOAD_DIR,
    AppSetting,
    AuthSession,
    ChatMessage,
    ChatThread,
    CompanySource,
    CoverLetter,
    DealbreakerSet,
    Document,
    LetterBlock,
    LetterTemplate,
    FitResult,
    Job,
    JobDuplicate,
    LlmUsage,
    Pin,
    RecoveryCode,
    SearchProfile,
    SearchRun,
    SearchSchedule,
    SessionLocal,
    SiteMapping,
    User,
    UserKeys,
    UserProfile,
    Workspace,
    scrub_wal,
    utcnow,
)

log = logging.getLogger(__name__)

PASSWORD_MIN = 10
PASSWORD_MAX = 1024
NAME_MAX = 254  # (an older install's sign-in names are email addresses, up to 254 characters)
RECOVERY_CODES = 8
# Crockford's base32: no I, L, O or U, so a code read aloud or copied by hand can't be mistaken (O is read as 0,
# I and L as 1). 16 characters carry 80 random bits.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_CHARS = 16
# The account made at start-up when there is none (CI, headless servers): its name and password, from the process
# environment only. The names are older than the name/password account; they make an ordinary account.
ADMIN_EMAIL_ENV = "JOBHUNT_ADMIN_EMAIL"
ADMIN_PASSWORD_ENV = "JOBHUNT_ADMIN_PASSWORD"
FIRST_RUN_FILE = "first-run.txt"
# The first-run token installer/launch.py makes and hands to the app (process environment only, never .env).
FIRST_RUN_TOKEN_ENV = "JOBHUNT_FIRST_RUN_TOKEN"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,100}")
DEFAULT_WORKSPACE = "My job search"

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class AccountError(ValueError):
    """A value that can't be used for an account; ``field`` names the form field it belongs to."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


class NameTaken(AccountError):
    """Another account of this install already signs in with that name."""


class AlreadySetUp(RuntimeError):
    """An account already exists: this install has its one account, and no other can be made."""


@dataclass(frozen=True, repr=False)
class NewAccount:
    user_id: int
    dek: bytes
    ik: bytes
    recovery_codes: list[str]

    def __repr__(self) -> str:  # never the keys or the codes
        return f"<users.NewAccount user_id={self.user_id}>"


# ---------------------------------------------------------------------------
# Checking what people type
# ---------------------------------------------------------------------------

def clean_name(name: Any) -> str:
    """The sign-in name as it is kept (NFKC, trimmed), or AccountError: 1 to 254 characters, no control characters or
    line breaks. Capitals and spaces around it don't matter when signing in (``keys.normalise_email``)."""
    value = unicodedata.normalize("NFKC", name if isinstance(name, str) else "").strip()
    if not value:
        raise AccountError("name", "Enter a name to sign in with.")
    if len(value) > NAME_MAX:
        raise AccountError("name", f"Use at most {NAME_MAX} characters for your name.")
    if _CONTROL.search(value):
        raise AccountError("name", "Use letters, digits, spaces and punctuation only (no line breaks or tabs).")
    return value


def check_password(password: Any) -> str:
    if not isinstance(password, str) or not password:
        raise AccountError("password", "Enter a password.")
    if len(password) < PASSWORD_MIN:
        raise AccountError("password", f"Use at least {PASSWORD_MIN} characters. A few random words work well.")
    if len(password) > PASSWORD_MAX:
        raise AccountError("password", f"Use at most {PASSWORD_MAX} characters.")
    return password


# ---------------------------------------------------------------------------
# Recovery codes: 80 random bits each, shown once, typed back with any case, spaces or hyphens
# ---------------------------------------------------------------------------

def new_recovery_code() -> str:
    """``XXXX-XXXX-XXXX-XXXX`` from CODE_ALPHABET (80 random bits)."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_CHARS))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_CHARS, 4))


def normalise_recovery_code(text: Any) -> Optional[str]:
    """The canonical 16 characters of a typed code, or None if it can't be one. Case, spaces and hyphens don't
    matter, and the letters people confuse with digits are read as those digits (O as 0, I and L as 1)."""
    if not isinstance(text, str):
        return None
    code = re.sub(r"[\s\-_.]", "", unicodedata.normalize("NFKC", text)).upper()
    code = code.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
    if len(code) != CODE_CHARS or any(c not in CODE_ALPHABET for c in code):
        return None
    return code


def _codes_label(uid: int) -> str:
    """The label of the unconfirmed codes sealed under the DEK (accounts from before the identity key)."""
    return f"user_keys.pending_codes|{uid}"


# Wrap labels of the identity key (IK) and of what is under it (accounts design §1.2, §15).
def _ik_priv_label(uid: int) -> str:
    return f"ik-priv|{uid}"


def _pw_ik_label(uid: int) -> str:
    return f"pw-ik|{uid}"


def _rc_ik_label(uid: int, code_id: int) -> str:
    return f"rc-ik|{uid}|{code_id}"


def _pending_codes_label(uid: int) -> str:
    return f"pending-codes|{uid}"


def has_pending_recovery_codes(db: Session, user_id: int) -> bool:
    """Whether this user has recovery codes they haven't yet confirmed saving. Needs no key."""
    row = db.get(UserKeys, user_id)
    return row is not None and row.pending_codes_enc is not None


def pending_recovery_codes(db: Session, user_id: int, ik: Optional[bytes] = None) -> Optional[list[str]]:
    """The recovery codes this user hasn't yet confirmed saving, or None. They are kept under the identity key (a code
    opens it, so the DEK, which the server key can open, must not hold them): pass ``ik``. Codes made before the IK
    existed are under the DEK instead (the user's key scope)."""
    row = db.get(UserKeys, user_id)
    if row is None or row.pending_codes_enc is None:
        return None
    if ik is not None:
        try:
            return json.loads(crypto.unwrap(ik, row.pending_codes_enc, _pending_codes_label(user_id)))
        except crypto.CryptoError:
            pass  # (the older form, below)
    return json.loads(crypto.unseal(row.pending_codes_enc, _codes_label(user_id)))


def acknowledge_recovery_codes(db: Session, user_id: int) -> None:
    """The user saved their codes: forget the copy kept to show them."""
    db.execute(update(UserKeys).where(UserKeys.user_id == user_id).values(pending_codes_enc=None))
    db.commit()


# ---------------------------------------------------------------------------
# The server key file and app settings
# ---------------------------------------------------------------------------

_server_keys: Optional[keys.ServerKeys] = None
_users_exist = False
_first_run_token: Optional[str] = None
_create_lock = threading.Lock()


def server_keys() -> keys.ServerKeys:
    """The key file loaded at start-up (``startup``)."""
    if _server_keys is None:
        raise RuntimeError("The server keys aren't loaded: backend.users.startup() runs when the app starts")
    return _server_keys


def _setting(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(AppSetting, key)
    return json.loads(row.value_json) if row else default


def _put_setting(db: Session, key: str, value: Any) -> None:
    db.merge(AppSetting(key=key, value_json=json.dumps(value)))


def users_exist() -> bool:
    """Whether any account exists. Cached once true: the last account can't be deleted (only the reset tool, with the
    app stopped, removes it)."""
    global _users_exist
    if not _users_exist:
        db = SessionLocal()
        try:
            _users_exist = db.query(User.id).first() is not None
        finally:
            db.close()
    return _users_exist


def known_to_exist() -> bool:
    """users_exist() without asking the database: True once an account is known to exist."""
    return _users_exist


def first_run_path() -> Path:
    """``first-run.txt`` beside the database (in the data folder)."""
    return Path(DB_PATH).parent / FIRST_RUN_FILE


def check_first_run_token(given: Any) -> bool:
    token = _first_run_token
    if not token or not isinstance(given, str) or len(given) > 200:
        return False
    return hmac.compare_digest(token.encode("ascii"), given.strip().encode("utf-8", "replace"))


def _launcher_token() -> Optional[str]:
    """The token installer/launch.py made and opened the page with (FIRST_RUN_TOKEN_ENV, process environment only)."""
    token = config.process_env(FIRST_RUN_TOKEN_ENV).strip()
    return token if TOKEN_RE.fullmatch(token) else None


def folder_step_token() -> None:
    """Before the data folder is chosen (backend/data_folder.py): the launcher's token, or a new one, in memory only
    (nothing may be written before the choice), shown in this server's own log as the first-run address."""
    global _first_run_token
    _first_run_token = _launcher_token() or secrets.token_urlsafe(32)
    log.warning("First run: choose where JobHuntPA keeps its data, then create your account. Open "
                "/first-run.html#token=%s on this server.", _first_run_token)


def _write_first_run_token() -> None:
    """The launcher's token (the page it opened carries on with it, also after the folder step's restart), else the
    one in first-run.txt (launch.py may already have opened it), else a new one; kept in first-run.txt."""
    global _first_run_token
    path = first_run_path()
    token = _launcher_token()
    if token is None:
        try:
            token = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            pass
    try:
        on_file = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        on_file = None
    if not token or not TOKEN_RE.fullmatch(token):
        token = secrets.token_urlsafe(32)
    if token != on_file:
        crypto.write_private(path, (token + "\n").encode("ascii"))
    _first_run_token = token
    # The operator needs this to create the first account; it is only ever written to this server's own log.
    # Not the folder's path: it can hold the person's name (a home folder), and logs never carry personal data.
    log.warning("First run: no account exists yet. Open /first-run.html#token=%s on this server to create your "
                "account (the token is also in %s in the data folder).", token, FIRST_RUN_FILE)


def _remove_first_run_token() -> None:
    global _first_run_token
    _first_run_token = None
    try:
        first_run_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not delete %s: %s", FIRST_RUN_FILE, exc.__class__.__name__)


def _finish_key_rotation(loaded: keys.ServerKeys) -> None:
    """A server-key rotation the last process didn't finish (the file still holds the old KEK): carry on with it
    (backend.key_rotation). Start-up goes on whatever happens: both keys are still in the file, so nothing is
    locked."""
    if not loaded.kek_rotation_pending:
        return
    from backend import key_rotation

    try:
        moved = key_rotation.finish_pending(loaded)
        log.info("Finished the server key rotation a previous run left (%d value(s) re-wrapped)", moved)
    except Exception as exc:  # noqa: BLE001
        log.error("The server key rotation a previous run left could not be finished (%s); both keys stay in the "
                  "key file. Run: python -m backend.keys rotate-kek", exc.__class__.__name__)


def startup() -> None:
    """At every start, after init_db: load (or, with no accounts, create) the server key file and check it belongs
    to this database, clear expired sessions, then, with no account yet, either create it from the process environment
    or write the first-run token. Once an account exists nothing here creates another. Raises keys.KeyFileError
    (start-up stops) for a missing or wrong key file."""
    global _server_keys, _users_exist
    keys.hold_key_file()  # for as long as this process runs: a key command can't replace the file under it
    db = SessionLocal()
    try:
        has_users = db.query(User.id).first() is not None
        loaded = keys.load_or_create(has_users, _setting(db, "server_keys_fingerprint"))
        if _setting(db, "server_keys_fingerprint") != loaded.fingerprint():
            _put_setting(db, "server_keys_fingerprint", loaded.fingerprint())
        db.query(AuthSession).filter((AuthSession.absolute_expires_at <= utcnow())
                                     | (AuthSession.idle_expires_at <= utcnow())).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    _server_keys = loaded
    _users_exist = has_users
    if has_users:
        _remove_first_run_token()
        _finish_key_rotation(loaded)
        return
    name, password = config.process_env(ADMIN_EMAIL_ENV).strip(), config.process_env(ADMIN_PASSWORD_ENV)
    if name and password:
        try:
            account = create_account(name, password, created_via="bootstrap", background=True)
        except AccountError as exc:
            log.error("%s/%s can't be used (%s); open the first-run page instead.", ADMIN_EMAIL_ENV,
                      ADMIN_PASSWORD_ENV, exc.message)
        except AlreadySetUp:
            return
        else:
            from backend import migrate_encryption

            # The account's key is at hand now (and not again until it signs in): encrypt the existing data during
            # start-up. On a new install there is next to nothing to do.
            migrate_encryption.run(account.user_id, account.dek)
            return
    elif name or password:
        log.error("Set both %s (the name) and %s (in the process environment, not .env) to create the account at "
                  "start-up.", ADMIN_EMAIL_ENV, ADMIN_PASSWORD_ENV)
    _write_first_run_token()


# ---------------------------------------------------------------------------
# Creating the account
# ---------------------------------------------------------------------------

def _details(name: str) -> bytes:
    return json.dumps({"name": name}, ensure_ascii=False).encode("utf-8")


def _wrap_for_server(row: UserKeys, user_id: int, dek: bytes) -> None:
    """Background access: the DEK under the current server KEK (``srv|{uid}|{kek_id}``), set on ``row``."""
    row.server_kek_id, row.server_wrapped_dek = server_keys().kek_wrap(dek, f"srv|{user_id}")
    row.background_since = utcnow()


def _wrapping_if(wanted: bool):
    """``server_keys().wrapping()`` when something will be wrapped under the server key, else nothing to hold."""
    return server_keys().wrapping() if wanted else contextlib.nullcontext()


def _insert_account(db: Session, *, name: str, password: str, created_via: str, background: bool) -> NewAccount:
    """The user row with every key of section 1.2, and 8 recovery codes, in ``db``'s transaction (not committed).
    ``name`` and ``password`` must already be checked. Only ``create_account`` calls this (it holds the write lock and
    has checked that no account exists); the test suite also uses it to stand in for the extra accounts an older
    install may already hold."""
    server = server_keys()
    if db.query(User.id).filter(User.email_hash.in_(server.email_hash_candidates(name))).first():
        raise AccountError("name", "An account with this name already exists.")
    params = crypto.kdf_profile()
    salt = crypto.new_salt()
    derived = crypto.derive_password(password, salt, params)
    dek = crypto.new_key()
    # onboarding_step 1: the sign-up walkthrough (frontend/welcome.html) is next; NULL (older accounts) means done.
    user = User(email_hash=server.email_hash(name), status="active", created_via=created_via, kdf_salt=salt,
                kdf_params=params.to_json(), pw_verifier=derived.auth, onboarding_step=1)
    db.add(user)
    db.flush()
    uid = user.id
    ik = crypto.new_key()  # the identity key: keeps the unconfirmed codes, and is never under the server KEK
    priv, pub = crypto.new_keypair()
    codes = [new_recovery_code() for _ in range(RECOVERY_CODES)]
    recovery_salt = crypto.new_salt()
    with crypto.as_user(uid, dek):
        user.pii_self = crypto.seal(_details(name), f"users.pii_self|{uid}")
    user_keys = UserKeys(user_id=uid, dek_kid=crypto.key_id(dek), pw_wrapped_dek=crypto.wrap(derived.kek, dek, f"pw|{uid}"),
                         x25519_pub=pub, x25519_priv_enc=crypto.wrap(ik, priv, _ik_priv_label(uid)),
                         pw_wrapped_ik=crypto.wrap(derived.kek, ik, _pw_ik_label(uid)),
                         recovery_salt=recovery_salt,
                         pending_codes_enc=crypto.wrap(ik, json.dumps(codes).encode("ascii"),
                                                       _pending_codes_label(uid)))
    if background:  # the DEK under the server KEK (scheduled searches run with it)
        _wrap_for_server(user_keys, uid, dek)
    db.add(user_keys)
    for code in codes:
        row = RecoveryCode(user_id=uid)
        db.add(row)
        db.flush()
        code_kek = crypto.recovery_kek(normalise_recovery_code(code), recovery_salt)
        row.wrapped_dek = crypto.wrap(code_kek, dek, f"rc|{uid}|{row.id}")
        row.wrapped_ik = crypto.wrap(code_kek, ik, _rc_ik_label(uid, row.id))
    return NewAccount(uid, dek, ik, codes)


def create_account(name: str, password: str, *, created_via: str = "first_run", background: bool = True) -> NewAccount:
    """This install's account (section 5.3), in one transaction: the user and keys and its recovery codes; every
    workspace without an owner becomes its own (or a first workspace is made). **AlreadySetUp whenever an account
    exists**, checked under SQLite's write lock, so two first runs at once can't both make one. AccountError for a bad
    value."""
    global _users_exist
    name, password = clean_name(name), check_password(password)
    with _wrapping_if(background), _create_lock:  # (key lock first, as a rotation takes it)
        db = SessionLocal()
        try:
            db.execute(text("BEGIN IMMEDIATE"))  # no other connection can add a user meanwhile
            if db.query(User.id).first() is not None:
                raise AlreadySetUp("An account already exists")
            account = _insert_account(db, name=name, password=password, created_via=created_via,
                                      background=background)
            uid = account.user_id
            claimed = db.execute(update(Workspace).where(Workspace.owner_id.is_(None)).values(owner_id=uid)).rowcount
            if not db.query(Workspace.id).filter(Workspace.owner_id == uid).first():
                with crypto.as_user(uid, account.dek):
                    from backend.schedule import start_off

                    first = Workspace(name=DEFAULT_WORKSPACE, owner_id=uid)
                    db.add(first)
                    db.flush()
                    start_off(db, first.id)  # a new workspace: scheduled searches start off
                    db.flush()
            # The existing data is encrypted with this account's key (backend.migrate_encryption): right away at the
            # first run, or at its next sign-in.
            _put_setting(db, "encryption_migration", {"state": "pending", "owner": uid})
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        _users_exist = True
    _remove_first_run_token()
    log.info("Account created: user %s (%s); %d existing workspace(s) are now its own", uid, created_via, claimed)
    return account


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------

_DUMMY_SALT = os.urandom(crypto.SALT_LEN)


def find_by_name(db: Session, name: Any) -> Optional[User]:
    """The account whose sign-in name is ``name`` (looked up by its hash; nothing is decrypted), or None. On an older
    install that is the email address the account was made with, until the name is changed. A match found under a
    previous pepper is rewritten under the current one."""
    if not isinstance(name, str) or not name.strip() or len(name) > 4 * NAME_MAX:
        return None
    server = server_keys()
    candidates = server.email_hash_candidates(name)
    user = db.query(User).filter(User.email_hash.in_(candidates)).first()
    if user is not None and user.email_hash != candidates[0]:
        user.email_hash = candidates[0]
        db.commit()
    return user


def dummy_derivation(password: Any) -> None:
    """The same work as checking a real password, so an unknown name takes as long to refuse."""
    crypto.derive_password(password if isinstance(password, str) else "", _DUMMY_SALT, crypto.kdf_profile())


@dataclass(frozen=True, repr=False)
class Unlocked:
    """What a password opens: the data key, and the identity key (None only when an account from before the IK
    couldn't be given one: ``unlock`` logs why)."""
    dek: bytes
    ik: Optional[bytes]

    def __repr__(self) -> str:  # never the keys
        return "<users.Unlocked>"


def unlock(db: Session, user: User, password: Any) -> Optional[Unlocked]:
    """The user's DEK and IK if ``password`` is theirs, else None. When the KDF profile has changed since the password
    was set, it is derived again with the new settings and stored (one extra derivation, this time only). An account
    that has no IK yet (made before it existed) gets one now (``migrate_identity``): the password is what wraps it."""
    if not isinstance(password, str) or not password or len(password) > PASSWORD_MAX:
        dummy_derivation("")
        return None
    params = crypto.KdfParams.from_json(user.kdf_params)
    derived = crypto.derive_password(password, user.kdf_salt, params)
    if not crypto.verify_password(user.pw_verifier, derived):
        return None
    row = db.get(UserKeys, user.id)
    dek = crypto.unwrap(derived.kek, row.pw_wrapped_dek, f"pw|{user.id}")
    ik = crypto.unwrap(derived.kek, row.pw_wrapped_ik, _pw_ik_label(user.id)) if row.pw_wrapped_ik is not None else None
    changed = False
    if ik is None:
        ik, changed = _give_identity(db, row, user.id, dek, derived.kek)
    current = crypto.kdf_profile()
    if params != current:
        salt = crypto.new_salt()
        again = crypto.derive_password(password, salt, current)
        user.kdf_salt, user.kdf_params, user.pw_verifier = salt, current.to_json(), again.auth
        row.pw_wrapped_dek = crypto.wrap(again.kek, dek, f"pw|{user.id}")
        if ik is not None:
            row.pw_wrapped_ik = crypto.wrap(again.kek, ik, _pw_ik_label(user.id))
        changed = True
        log.info("user %s: password key settings updated to the current profile", user.id)
    if changed:
        db.commit()
        scrub_wal()  # the earlier settings' copy of the keys, and the DEK-sealed private key, must not linger in the log
    return Unlocked(dek, ik)


def unlock_with_password(db: Session, user: User, password: Any) -> Optional[bytes]:
    """The user's DEK if ``password`` is theirs, else None (``unlock`` gives the IK as well)."""
    opened = unlock(db, user, password)
    return opened.dek if opened is not None else None


def _give_identity(db: Session, row: UserKeys, user_id: int, dek: bytes, password_kek: bytes
                   ) -> tuple[Optional[bytes], bool]:
    """``(ik, changed)`` for an account whose password just opened its DEK but not an IK (none is wrapped for the
    password): another request may have just made one, so the row is read again under the write lock; otherwise
    ``migrate_identity`` makes it. (None, False) when that fails: signing in goes on without an IK."""
    try:
        _write_lock(db)
        db.refresh(row)
        if row.pw_wrapped_ik is not None:
            return crypto.unwrap(password_kek, row.pw_wrapped_ik, _pw_ik_label(user_id)), False
        ik = migrate_identity(db, row, user_id, dek, password_kek)
    except (crypto.CryptoError, ValueError):
        db.rollback()
        log.warning("user %s: the identity key could not be made; it is tried again at the next sign-in", user_id)
        return None, False
    log.info("user %s: identity key made; the private key moved from the data key to it", user_id)
    return ik, True


def migrate_identity(db: Session, row: UserKeys, user_id: int, dek: bytes, password_kek: bytes) -> bytes:
    """Gives an account from before the identity key one, in the caller's transaction (not committed): a new IK and a
    **new X25519 key pair** whose private key is wrapped under the IK (the old private key is not reused: copies of
    the DEK-sealed form, in a backup or an old file, would still open it), the IK wrapped under ``password_kek`` (the
    password key) and, unless the server key opens the data key (background access), under each unconfirmed recovery
    code (their text is in hand here), and those codes moved from the DEK to the IK. The DEK-sealed copies are
    overwritten; the caller scrubs the write-ahead log after committing. Everything is computed before the row
    changes, and the changes are one savepoint, so a failure leaves them as they were (CryptoError)."""
    with crypto.as_user(user_id, dek):
        crypto.unseal(row.x25519_priv_enc, f"user_keys.x25519_priv|{user_id}")  # (it must be this account's)
        pending = crypto.unseal(row.pending_codes_enc, _codes_label(user_id)) if row.pending_codes_enc else None
    ik = crypto.new_key()
    priv, pub = crypto.new_keypair()
    wrapped_priv = crypto.wrap(ik, priv, _ik_priv_label(user_id))
    wrapped_ik = crypto.wrap(password_kek, ik, _pw_ik_label(user_id))
    wrapped_pending = crypto.wrap(ik, pending, _pending_codes_label(user_id)) if pending is not None else None
    # (a code whose text the server key could read, through the data key, must not open the new IK: it stays without)
    code_iks = _code_iks(db, row, user_id, ik, pending) if row.server_wrapped_dek is None else {}
    with db.begin_nested():
        row.x25519_pub, row.x25519_priv_enc, row.pw_wrapped_ik = pub, wrapped_priv, wrapped_ik
        if wrapped_pending is not None:
            row.pending_codes_enc = wrapped_pending
        for code_id, wrapped in code_iks.items():
            db.execute(update(RecoveryCode).where(RecoveryCode.id == code_id).values(wrapped_ik=wrapped))
    return ik


def _code_iks(db: Session, row: UserKeys, user_id: int, ik: bytes, pending: Optional[bytes]) -> dict[int, bytes]:
    """``{recovery code id: the IK wrapped under that code}`` for the unused codes whose text is among the
    unconfirmed ones (``pending``, the JSON list): they carried no IK, and the text is the only thing that can give
    them one. Codes already confirmed saved are not here, and stay without (``recovery_codes_without_ik``)."""
    if pending is None:
        return {}
    try:
        typed = json.loads(pending)
    except ValueError:
        return {}
    waiting = list(db.query(RecoveryCode).filter(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None),
                                                 RecoveryCode.wrapped_dek.isnot(None),
                                                 RecoveryCode.wrapped_ik.is_(None)).order_by(RecoveryCode.id))
    out: dict[int, bytes] = {}
    for text_ in typed if isinstance(typed, list) else []:
        code = normalise_recovery_code(text_)
        if code is None:
            continue
        kek = crypto.recovery_kek(code, row.recovery_salt)
        for rc in waiting:
            if rc.id in out:
                continue
            try:
                crypto.unwrap(kek, rc.wrapped_dek, f"rc|{user_id}|{rc.id}")
            except crypto.CryptoError:
                continue
            out[rc.id] = crypto.wrap(kek, ik, _rc_ik_label(user_id, rc.id))
            break
    return out


def open_private_key(row: UserKeys, user_id: int, ik: bytes) -> bytes:
    """The account's X25519 private key, opened with its identity key (CryptoError when it isn't the account's)."""
    return crypto.unwrap(ik, row.x25519_priv_enc, _ik_priv_label(user_id))


def replace_identity(db: Session, user_id: int, row: UserKeys, password_kek: bytes) -> bytes:
    """A new X25519 key pair and a new IK for an account whose old IK can't be opened (a reset with a recovery code made
    before the IK). In the caller's transaction (not committed). The IK is wrapped under ``password_kek``; the caller
    wraps it under the recovery codes and the session. The unconfirmed recovery codes (kept under the old IK, which
    nothing opens now) are dropped: the caller makes new ones or leaves the account's other codes as they are. Nothing
    else is under the IK, so no data is lost."""
    ik = crypto.new_key()
    priv, pub = crypto.new_keypair()
    row.x25519_pub, row.x25519_priv_enc = pub, crypto.wrap(ik, priv, _ik_priv_label(user_id))
    row.pw_wrapped_ik = crypto.wrap(password_kek, ik, _pw_ik_label(user_id))
    row.pending_codes_enc = None  # (under the old IK: it can't be shown again, and would stay "unsaved" for good)
    # The codes that wrapped the old IK are of no use for it any more (and must not be taken for the new one).
    db.execute(update(RecoveryCode).where(RecoveryCode.user_id == user_id).values(wrapped_ik=None))
    return ik


def _sign_in_name(info: Any) -> str:
    """The sign-in name from the details' JSON: ``name``, or ``email`` in an account made before the open-source
    version (its email address is its sign-in name until it is changed)."""
    if not isinstance(info, dict):
        return ""
    value = info.get("email") if "email" in info else info.get("name")
    return value if isinstance(value, str) else ""


def details(db: Session, user_id: int) -> dict:
    """{name}: the sign-in name, from the user's own copy. Needs their key scope."""
    user = db.get(User, user_id)
    if user is None or user.pii_self is None:
        return {"name": ""}
    return {"name": _sign_in_name(json.loads(crypto.unseal(user.pii_self, f"users.pii_self|{user_id}")))}


def change_name(db: Session, user_id: int, new_name: Any) -> str:
    """Gives the account a new sign-in name: its look-up hash and its own sealed copy of the name, in one transaction
    (committed here; the write-ahead log is scrubbed after, so the old name's copies don't linger). Needs the account's
    key scope; the caller has checked recent sign-in. AccountError for a name that can't be used or that another
    account has. Returns the name as kept."""
    name = clean_name(new_name)
    server = server_keys()
    try:
        _write_lock(db)
        user = db.get(User, user_id, populate_existing=True)
        if user is None:
            raise LookupError("No such account")
        taken = db.query(User.id).filter(User.email_hash.in_(server.email_hash_candidates(name)),
                                         User.id != user_id).first()
        if taken:
            raise NameTaken("name", "Another account on this JobHuntPA has that name. Choose another.")
        user.email_hash = server.email_hash(name)
        user.pii_self = crypto.seal(_details(name), f"users.pii_self|{user_id}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    scrub_wal()
    log.info("user %s: sign-in name changed", user_id)
    return name


def background_key(db: Session, user_id: int) -> Optional[bytes]:
    """The user's DEK through the server KEK, when they allowed background access (scheduled searches run with it);
    else None. Unwrapped on each call, never cached (design §3.1)."""
    row = db.get(UserKeys, user_id)
    user = db.get(User, user_id)
    if row is None or row.server_wrapped_dek is None or user is None or user.status != "active":
        return None
    return _server_dek(row, user_id)


def _server_dek(row: UserKeys, user_id: int) -> Optional[bytes]:
    """The DEK from ``row.server_wrapped_dek``, or None when it doesn't open with the key file or isn't this user's
    key (its kid must match ``dek_kid``, so a blob copied from another row is refused)."""
    try:
        dek = server_keys().kek_unwrap(row.server_wrapped_dek, f"srv|{user_id}")
    except crypto.CryptoError:
        log.warning("user %s: the background-access key doesn't open with the key file", user_id)
        return None
    if not hmac.compare_digest(crypto.key_id(dek), bytes(row.dek_kid)):
        log.warning("user %s: the background-access key isn't this account's key", user_id)
        return None
    return dek


def background_enabled(db: Session, user_id: int) -> bool:
    """Whether this account has allowed background access (the server holds a wrapped copy of its key)."""
    row = db.get(UserKeys, user_id)
    return row is not None and row.server_wrapped_dek is not None


def background_block(db: Session, user_id: int) -> str:
    """Why ``background_key`` gave nothing: ``account_off`` (turned off or gone), ``background_off`` (not allowed) or
    ``key_unusable`` (allowed, but the key file can't open it). Used to tell the user why a slot was passed over."""
    user = db.get(User, user_id)
    if user is None or user.status != "active":
        return "account_off"
    if not background_enabled(db, user_id):
        return "background_off"
    return "key_unusable"


def enable_background(db: Session, user_id: int, dek: bytes) -> None:
    """Allow background access: wrap ``dek`` (the signed-in user's key, from the request's key scope) under the
    current server KEK. Refuses a key that isn't this account's (ValueError). Commits. The caller has checked recent
    sign-in (sessions.require_recent_auth)."""
    with server_keys().wrapping():  # a key rotation can't finish between the wrap and the commit
        row = db.get(UserKeys, user_id)
        if row is None:
            raise ValueError("No such account")
        if not hmac.compare_digest(crypto.key_id(dek), bytes(row.dek_kid)):
            raise ValueError("That key isn't this account's data key")
        _wrap_for_server(row, user_id, dek)
        _clear_skipped(db, user_id)
        db.commit()
    log.info("user %s: background access turned on", user_id)


def disable_background(db: Session, user_id: int) -> None:
    """Withdraw background access: the server's wrapped copy of the key is deleted (not just ignored). Commits."""
    row = db.get(UserKeys, user_id)
    if row is None:
        return
    row.server_wrapped_dek = row.server_kek_id = row.background_since = None
    db.commit()
    scrub_wal()  # secure_delete zeroes the main file; the log would still hold the old row until emptied
    log.info("user %s: background access turned off", user_id)


def _clear_skipped(db: Session, user_id: int) -> None:
    """Forget the "scheduled search skipped" notes of the user's workspaces (background access is back)."""
    ids = select(Workspace.id).where(Workspace.owner_id == user_id)
    db.execute(update(SearchSchedule).where(SearchSchedule.id.in_(ids)).values(skipped_reason=None,
                                                                              skipped_slot=None))


def workspace_ids(db: Session, user_id: int) -> list[int]:
    return [ws for (ws,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id).order_by(Workspace.id)]


# ---------------------------------------------------------------------------
# Wrong passwords: counted per source in memory (backend/lockout.py, backend/ratelimit.py), not in the database
# ---------------------------------------------------------------------------

PAD_MARGIN_S = 0.05
_floors: dict[str, float] = {}
_floors_lock = threading.Lock()


def derivation_floor() -> float:
    """How long a refused sign-in takes at least: the slowest of three password derivations at the current settings,
    plus a margin. Measured once per setting (``startup`` does it in the background). Padding every refusal to this
    keeps the answer for an address with no account as slow as for a real one (accounts design §3.6)."""
    key = crypto.kdf_profile().to_json()
    with _floors_lock:
        floor = _floors.get(key)
    if floor is None:
        times = []
        for _ in range(3):
            began = time.monotonic()
            dummy_derivation("")
            times.append(time.monotonic() - began)
        floor = max(times) + PAD_MARGIN_S
        with _floors_lock:
            _floors[key] = floor
    return floor


def pad_failure(began: float) -> None:
    """Waits until ``derivation_floor()`` seconds have passed since ``began`` (a ``time.monotonic()``): called on
    every refused sign-in or recovery before the answer goes out."""
    time.sleep(max(0.0, began + derivation_floor() - time.monotonic()))


@dataclass(frozen=True, repr=False)
class NewPassword:
    """A new password's salt, settings and keys (derived once: it costs about a second on a Raspberry Pi)."""
    salt: bytes
    params: crypto.KdfParams
    derived: crypto.PasswordKeys

    def __repr__(self) -> str:  # never the keys
        return "<users.NewPassword>"


def new_password_keys(password: str) -> NewPassword:
    salt, params = crypto.new_salt(), crypto.kdf_profile()
    return NewPassword(salt, params, crypto.derive_password(password, salt, params))


def apply_new_password(db: Session, user: User, dek: bytes, ik: Optional[bytes], new: NewPassword) -> None:
    """The DEK and the IK under the new password, in ``db``'s transaction (the caller commits). Nothing is
    re-encrypted: the data is under the DEK, which doesn't change. ``ik`` may be None only for an account that has
    none yet; one that has an IK needs it (KeyMismatch otherwise, and when it isn't the account's), because the old
    password's wrap of it is replaced and the IK would be lost."""
    row = db.get(UserKeys, user.id)
    if not hmac.compare_digest(crypto.key_id(dek), bytes(row.dek_kid)):
        raise crypto.KeyMismatch("That isn't this account's data key")
    if row.pw_wrapped_ik is not None:
        if ik is None:
            raise crypto.KeyMismatch("That account's identity key is needed to change its password")
        crypto.unwrap(ik, row.x25519_priv_enc, _ik_priv_label(user.id))  # it must be this account's
        row.pw_wrapped_ik = crypto.wrap(new.derived.kek, ik, _pw_ik_label(user.id))
    user.kdf_salt, user.kdf_params, user.pw_verifier = new.salt, new.params.to_json(), new.derived.auth
    row.pw_wrapped_dek = crypto.wrap(new.derived.kek, dek, f"pw|{user.id}")
    ratelimit.forget_account(user.id)  # the account starts again with no wrong tries


# ---------------------------------------------------------------------------
# Recovery codes in use (Phase E)
# ---------------------------------------------------------------------------

def recovery_codes_left(db: Session, user_id: int) -> int:
    return db.query(RecoveryCode).filter(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None),
                                         RecoveryCode.wrapped_dek.isnot(None)).count()


def recovery_codes_without_ik(db: Session, user_id: int) -> int:
    """How many unused codes can't open the identity key: made before it existed. A reset with one keeps the data and
    gives the account a new identity key (``replace_identity``)."""
    return db.query(RecoveryCode).filter(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None),
                                         RecoveryCode.wrapped_dek.isnot(None),
                                         RecoveryCode.wrapped_ik.is_(None)).count()


def open_recovery_code(db: Session, user_id: int, typed: Any) -> Optional[tuple[int, bytes, Optional[bytes]]]:
    """``(code id, DEK, IK)`` if ``typed`` is one of this user's unused recovery codes (the code's own wrap opens it, so
    nothing else is compared), else None. The IK is None for a code made before the identity key existed. Nothing is
    used up yet: ``spend_recovery_code`` does that."""
    code = normalise_recovery_code(typed)
    row = db.get(UserKeys, user_id)
    if code is None or row is None:
        return None
    kek = crypto.recovery_kek(code, row.recovery_salt)
    for rc in db.query(RecoveryCode).filter(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None),
                                            RecoveryCode.wrapped_dek.isnot(None)).order_by(RecoveryCode.id):
        try:
            dek = crypto.unwrap(kek, rc.wrapped_dek, f"rc|{user_id}|{rc.id}")
        except crypto.CryptoError:
            continue
        ik = None
        if rc.wrapped_ik is not None:
            try:
                ik = crypto.unwrap(kek, rc.wrapped_ik, _rc_ik_label(user_id, rc.id))
            except crypto.CryptoError:
                log.warning("user %s: a recovery code's identity key doesn't open", user_id)
        return rc.id, dek, ik
    return None


def spend_recovery_code(db: Session, code_id: int) -> bool:
    """Uses a code up (and drops its wraps of the DEK and the IK), in ``db``'s transaction. False if someone else just used it
    (two requests with the same code: only one gets it)."""
    return db.execute(update(RecoveryCode).where(RecoveryCode.id == code_id, RecoveryCode.used_at.is_(None))
                      .values(used_at=utcnow(), wrapped_dek=None, wrapped_ik=None)).rowcount == 1


def replace_recovery_codes(db: Session, user_id: int, dek: bytes, ik: Optional[bytes]) -> list[str]:
    """Eight new codes in place of all the old ones (used or not), with a new salt, each wrapping the DEK and the IK,
    kept under the IK in ``pending_codes_enc`` until the person confirms saving them. In ``db``'s transaction; the
    caller commits. Refuses (``crypto.KeyMismatch``) a key that isn't the account's current one, as
    ``apply_new_password`` does: a request that held the old key across a wipe reset must not wrap it under new
    codes. The write lock is taken and the row read again first, so the check can't pass against a copy from before
    that wipe. ``ik`` is None only for an account that has no IK yet (its codes then carry none)."""
    _write_lock(db)
    db.flush()  # (a caller's own changes to the row, such as the wipe's new key id, must survive the reload)
    row = db.get(UserKeys, user_id, populate_existing=True)
    if row is None or not hmac.compare_digest(crypto.key_id(dek), bytes(row.dek_kid)):
        raise crypto.KeyMismatch("That isn't this account's data key")
    if row.pw_wrapped_ik is not None:
        if ik is None:
            raise crypto.KeyMismatch("That account's identity key is needed to make recovery codes")
        crypto.unwrap(ik, row.x25519_priv_enc, _ik_priv_label(user_id))  # it must be this account's
    salt = crypto.new_salt()
    codes = [new_recovery_code() for _ in range(RECOVERY_CODES)]
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    for code in codes:
        rc = RecoveryCode(user_id=user_id)
        db.add(rc)
        db.flush()
        code_kek = crypto.recovery_kek(normalise_recovery_code(code), salt)
        rc.wrapped_dek = crypto.wrap(code_kek, dek, f"rc|{user_id}|{rc.id}")
        if ik is not None:
            rc.wrapped_ik = crypto.wrap(code_kek, ik, _rc_ik_label(user_id, rc.id))
    row.recovery_salt = salt
    listed = json.dumps(codes).encode("ascii")
    if ik is not None:  # under the IK: a code opens it, so the DEK (which the server key can open) must not hold them
        row.pending_codes_enc = crypto.wrap(ik, listed, _pending_codes_label(user_id))
    else:
        with crypto.as_user(user_id, dek):
            row.pending_codes_enc = crypto.seal(listed, _codes_label(user_id))
    return codes


# ---------------------------------------------------------------------------
# Deleting an account (Settings, for an account of an older install that has others besides it)
# ---------------------------------------------------------------------------

class AccountBusy(Exception):
    """One of the account's runs is still queued or running."""


class AccountProtected(Exception):
    """The install's only (active) account: deleting it would return the app to its first run, which only the reset
    tool may do, on the computer itself (backend/reset.py)."""


def is_only_account(db: Session, user_id: int) -> bool:
    """Whether no other active account exists besides this one (an install has one account; an older one may have
    several, and each of those but the last can be deleted)."""
    return not db.query(User.id).filter(User.status == "active", User.id != user_id).first()


def _write_lock(db: Session) -> None:
    """Take SQLite's write lock now (it is kept until the commit), unless this session already holds it, so what is
    read next (the other accounts, the key, the uploads) can't change before the commit."""
    if not db.connection().connection.driver_connection.in_transaction:
        db.execute(text("BEGIN IMMEDIATE"))


def has_active_runs(db: Session, user_id: int) -> bool:
    """Whether one of the account's workspaces has a queued or running search (a quick look: deleting data checks
    again under the lock)."""
    ws_ids = [i for (i,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id)]
    try:
        _refuse_active_runs(db, ws_ids)
    except AccountBusy:
        return True
    return False


def _refuse_active_runs(db: Session, ws_ids: list[int]) -> None:
    """AccountBusy while one of these workspaces has a queued or running search."""
    if db.query(SearchRun.id).filter(SearchRun.workspace_id.in_(ws_ids),
                                     SearchRun.state.in_(["queued", "running"])).first():
        raise AccountBusy("A search is still running")


def _check_deletable(db: Session, user_id: int) -> None:
    """The cheap refusals of ``delete_account`` (AccountProtected, AccountBusy), asked before it takes any lock. It asks
    again under the lock, which is what counts."""
    if is_only_account(db, user_id):
        raise AccountProtected("This is the only account")
    ws_ids = [i for (i,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id)]
    _refuse_active_runs(db, ws_ids)


def _remove_workspace_data(db: Session, user_id: int, ws_ids: list[int]) -> list[str]:
    """Everything the account saved in its workspaces, in ``db``'s transaction (not committed): jobs with their
    scores, cover letters and merge decisions, documents, cover letter blocks and template, chats, profile, dealbreakers, pins, sources, schedules, runs,
    the workspaces themselves and the pre-accounts search profile when this account holds it.
    Returns the stored names of the sealed upload files: delete them (``_remove_uploads``) after the commit. The
    caller holds ``tasks.start_lock`` and the database's write lock."""
    from backend import migrate_encryption

    jobs = select(Job.id).where(Job.workspace_id.in_(ws_ids))
    threads = select(ChatThread.id).where(ChatThread.workspace_id.in_(ws_ids))
    files = [name for (name,) in db.query(Document.stored_name).filter(Document.workspace_id.in_(ws_ids)) if name]
    files += [name for (name,) in db.query(LetterTemplate.stored_name).filter(LetterTemplate.workspace_id.in_(ws_ids))
              if name]
    db.execute(delete(ChatMessage).where(ChatMessage.thread_id.in_(threads)))
    db.execute(delete(ChatThread).where(ChatThread.workspace_id.in_(ws_ids)))
    db.execute(delete(CoverLetter).where(CoverLetter.job_id.in_(jobs)))
    db.execute(delete(FitResult).where(FitResult.job_id.in_(jobs)))
    db.execute(delete(JobDuplicate).where(JobDuplicate.workspace_id.in_(ws_ids)))
    db.execute(delete(Job).where(Job.workspace_id.in_(ws_ids)))
    for model in (Document, LetterBlock, LetterTemplate, Pin, CompanySource, SearchRun):
        db.execute(delete(model).where(model.workspace_id.in_(ws_ids)))
    for model in (SearchSchedule, UserProfile, DealbreakerSet):  # one row per workspace, id = workspace id
        db.execute(delete(model).where(model.id.in_(ws_ids)))
    db.execute(update(LlmUsage).where(LlmUsage.workspace_id.in_(ws_ids)).values(workspace_id=None))
    db.execute(delete(Workspace).where(Workspace.owner_id == user_id))
    if migrate_encryption.owner_of(migrate_encryption.state()) == user_id:
        db.execute(delete(SearchProfile))  # the legacy single row, sealed under this account's key
    return files


def _remove_uploads(user_id: int, files: list[str]) -> int:
    """Deletes the sealed upload files named in ``files`` (after the commit: a failed delete leaves an unreadable file,
    never a half-removed account). Returns how many were removed."""
    removed = 0
    for name in files:
        if not name or Path(name).name != name:
            continue
        try:
            (UPLOAD_DIR / name).unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            log.warning("Could not delete an upload of user %s: %s", user_id, exc.__class__.__name__)
    return removed


def delete_account(db: Session, user_id: int) -> dict:
    """The one way an account is removed over the web (a person deleting their own, in Settings): the account and
    everything of theirs, in one transaction (their sealed upload files after it): workspaces with every row in them
    (jobs, scores, cover letters, documents, chats, profile, dealbreakers, pins, sources, schedules, runs, merge
    decisions), the job sites' page layouts (``site_mappings``), sessions, recovery codes and keys (the data key's and the identity key's wraps with them).
    Shared postings, company profiles and the geocode cache stay; so do usage counts (``llm_usage``, no longer tied
    to anyone).

    **Never the only account** (AccountProtected): that would put the app back at its first run, which only the reset
    tool may do, run on the computer itself. Needs no key of the user's: it never opens a sealed value. Refuses
    (AccountBusy) while one of the account's runs is active. Explicit deletes, not cascades: a database from before
    accounts has workspace tables without foreign keys."""
    from backend import sessions, tasks

    if db.get(User, user_id) is None:
        raise LookupError("No such account")
    _check_deletable(db, user_id)
    with tasks.start_lock():  # no run can be queued for these workspaces while their rows are removed
        try:
            _write_lock(db)
            user = db.get(User, user_id, populate_existing=True)
            if user is None:
                raise LookupError("No such account")
            if is_only_account(db, user_id):
                raise AccountProtected("This is the only account")
            ws_ids = [i for (i,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id)]
            _refuse_active_runs(db, ws_ids)
            files = _remove_workspace_data(db, user_id, ws_ids)
            db.execute(update(LlmUsage).where(LlmUsage.user_id == user_id).values(user_id=None))
            for model in (SiteMapping, AuthSession, RecoveryCode, UserKeys):
                db.execute(delete(model).where(model.user_id == user_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    removed = _remove_uploads(user_id, files)  # after the commit
    scrub_wal()  # the deleted account's key copies and secrets must not linger in the write-ahead log
    sessions._active_on.pop(user_id, None)  # SQLite may give this id to the next account
    ratelimit.forget_user(user_id)  # ... and must not inherit its wrong tries
    log.info("Account deleted: user %s (%d workspace(s), %d file(s))", user_id, len(ws_ids), removed)
    return {"workspaces": len(ws_ids), "files": removed}
