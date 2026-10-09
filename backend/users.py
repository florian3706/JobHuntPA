"""Accounts: creating them with their keys and recovery codes, unlocking them with a password, the server key file at
start-up, and the first run. Design: docs/design/accounts.md, sections 1, 4 and 5.

**Keys of an account** (all made here, at creation):
  * a random data key (DEK) for all of the user's data, wrapped under the password (``pw|{uid}``) and under each of
    8 recovery codes (``rc|{uid}|{code_id}``); a session wraps it again under its cookie (backend.sessions);
  * an X25519 key pair, so an admin details key can be shared with this user, and its private half under a second
    random key, the identity key (IK: ``ik-priv|{uid}``). The IK is wrapped like the DEK, under the password
    (``pw-ik|{uid}``), each recovery code (``rc-ik|{uid}|{code_id}``) and each session's cookie (backend.sessions),
    but **never under the server KEK**: background access opens the DEK, not the way to the admin details key;
  * ``pii_self`` ({email, name}) under the DEK, and ``pii_admin`` (the same) sealed to the admin details key (APK),
    which needs only its public half, so no admin has to be signed in;
  * with background access, the DEK under the server KEK as well (``srv|{uid}|{kek_id}``; used from Phase G).

**First run** (no account yet). Every API except /api/health, /api/auth/state and /api/auth/first-run answers 401
``setup_required``. At start-up:
  * if the process environment (not .env) has JOBHUNT_ADMIN_EMAIL and JOBHUNT_ADMIN_PASSWORD, that admin is created
    at once (CI and headless servers);
  * otherwise a one-time token is written to ``first-run.txt`` beside the database (owner-only) and logged as
    ``/first-run.html#token=…``; installer/launch.py opens that address. The token is in the fragment, so it never
    reaches a server log.
``create_first_admin`` then makes the account, the APK and the admin's grant of it, and claims every workspace
that has no owner (all the data of an install from before accounts), in one transaction, and records in
``app_settings["encryption_migration"]`` that this admin's key encrypts that data (backend.migrate_encryption: at
once for the first run and the environment admin, otherwise at their next sign-in).
"""
from __future__ import annotations

import base64
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.orm import Session

from backend import config, crypto, keys, ratelimit, totp
from backend.db import (
    DB_PATH,
    UPLOAD_DIR,
    AdminAudit,
    AdminKeyGrant,
    AppSetting,
    AuthSession,
    AuthToken,
    ChatMessage,
    ChatThread,
    CommuteCache,
    CompanySource,
    CoverLetter,
    DealbreakerSet,
    Document,
    EmailPrefs,
    FitResult,
    Job,
    JobDuplicate,
    LlmUsage,
    Membership,
    MembershipGrant,
    Pin,
    RecoveryCode,
    SearchProfile,
    SearchRun,
    SearchSchedule,
    SessionLocal,
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
EMAIL_MAX = 254
NAME_MAX = 100
RECOVERY_CODES = 8
# Crockford's base32: no I, L, O or U, so a code read aloud or copied by hand can't be mistaken (O is read as 0,
# I and L as 1). 16 characters carry 80 random bits.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_CHARS = 16
ADMIN_EMAIL_ENV = "JOBHUNT_ADMIN_EMAIL"
ADMIN_PASSWORD_ENV = "JOBHUNT_ADMIN_PASSWORD"
FIRST_RUN_FILE = "first-run.txt"
DEFAULT_WORKSPACE = "My job search"

# One address, nothing a mail header or an SMTP envelope would read as a list or a name: no , ; < > ( ) [ ] \ " or :
_EMAIL_RE = re.compile(r"[^@\s,;<>()\[\]\\\":]{1,64}@[^@\s.,;<>()\[\]\\\":]+(\.[^@\s.,;<>()\[\]\\\":]+)+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class AccountError(ValueError):
    """A value that can't be used for an account; ``field`` names the form field it belongs to."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


class AlreadySetUp(RuntimeError):
    """create_first_admin was called when an account already exists."""


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

def clean_email(email: Any) -> str:
    """The email address as it is stored in the personal details (trimmed), or AccountError."""
    value = unicodedata.normalize("NFKC", email if isinstance(email, str) else "").strip()
    if not value:
        raise AccountError("email", "Enter your email address.")
    if len(value) > EMAIL_MAX or not _EMAIL_RE.fullmatch(value):
        raise AccountError("email", "Enter an email address like name@example.com.")
    return value


def check_password(password: Any) -> str:
    if not isinstance(password, str) or not password:
        raise AccountError("password", "Enter a password.")
    if len(password) < PASSWORD_MIN:
        raise AccountError("password", f"Use at least {PASSWORD_MIN} characters. A few random words work well.")
    if len(password) > PASSWORD_MAX:
        raise AccountError("password", f"Use at most {PASSWORD_MAX} characters.")
    return password


def clean_name(name: Any) -> str:
    value = _CONTROL.sub("", unicodedata.normalize("NFKC", name if isinstance(name, str) else "")).strip()
    if len(value) > NAME_MAX:
        raise AccountError("name", f"Use at most {NAME_MAX} characters for your name.")
    return value


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
    """Whether any account exists. Cached once true: the last admin can't be deleted."""
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
    """``first-run.txt`` beside the database (data/first-run.txt in an install)."""
    return Path(DB_PATH).parent / FIRST_RUN_FILE


def check_first_run_token(given: Any) -> bool:
    token = _first_run_token
    if not token or not isinstance(given, str) or len(given) > 200:
        return False
    return hmac.compare_digest(token.encode("ascii"), given.strip().encode("utf-8", "replace"))


def _write_first_run_token() -> None:
    """Reuse the token in first-run.txt (launch.py may already have opened it), or write a new one."""
    global _first_run_token
    path = first_run_path()
    token = None
    try:
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        pass
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", token):
        token = secrets.token_urlsafe(32)
        crypto.write_private(path, (token + "\n").encode("ascii"))
    _first_run_token = token
    # The operator needs this to create the first account; it is only ever written to this server's own log.
    log.warning("First run: no account exists yet. Open /first-run.html#token=%s on this server to create the "
                "admin account (the token is also in %s).", token, path)


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
    to this database, clear expired sessions, then either create the admin from the process environment, or write
    the first-run token. Raises keys.KeyFileError (start-up stops) for a missing or wrong key file."""
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
    email, password = config.process_env(ADMIN_EMAIL_ENV).strip(), config.process_env(ADMIN_PASSWORD_ENV)
    if email and password:
        try:
            account = create_first_admin(email, password, created_via="bootstrap", background=True)
        except AccountError as exc:
            log.error("%s/%s can't be used (%s); open the first-run page instead.", ADMIN_EMAIL_ENV,
                      ADMIN_PASSWORD_ENV, exc.message)
        except AlreadySetUp:
            return
        else:
            from backend import migrate_encryption

            # The admin's key is at hand now (and not again until they sign in): encrypt the existing data during
            # start-up. On a new install there is next to nothing to do.
            migrate_encryption.run(account.user_id, account.dek)
            return
    elif email or password:
        log.error("Set both %s and %s (in the process environment, not .env) to create the admin at start-up.",
                  ADMIN_EMAIL_ENV, ADMIN_PASSWORD_ENV)
    _write_first_run_token()


# ---------------------------------------------------------------------------
# Creating accounts
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def admin_public_key(db: Session) -> Optional[tuple[bytes, int]]:
    """The admin details key's public half and its version, or None before the first admin exists."""
    pub, version = _setting(db, "apk_public"), _setting(db, "apk_version")
    if not pub or not version:
        return None
    return base64.b64decode(pub), int(version)


def _details(email: str, name: str) -> bytes:
    return json.dumps({"email": email, "name": name}, ensure_ascii=False).encode("utf-8")


def _wrap_for_server(row: UserKeys, user_id: int, dek: bytes) -> None:
    """Background access: the DEK under the current server KEK (``srv|{uid}|{kek_id}``), set on ``row``."""
    row.server_kek_id, row.server_wrapped_dek = server_keys().kek_wrap(dek, f"srv|{user_id}")
    row.background_since = utcnow()


def _wrapping_if(wanted: bool):
    """``server_keys().wrapping()`` when something will be wrapped under the server key, else nothing to hold."""
    return server_keys().wrapping() if wanted else contextlib.nullcontext()


def _create_user(db: Session, *, email: str, password: str, name: str, role: str, created_via: str,
                 background: bool) -> NewAccount:
    """The user row with every key of section 1.2, 8 recovery codes, a membership and email preferences, in ``db``'s
    transaction (not committed). ``email``, ``password`` and ``name`` must already be checked."""
    server = server_keys()
    email_hash = server.email_hash(email)
    if db.query(User.id).filter(User.email_hash.in_(server.email_hash_candidates(email))).first():
        raise AccountError("email", "An account with this email already exists. Sign in or reset your password.")
    params = crypto.kdf_profile()
    salt = crypto.new_salt()
    derived = crypto.derive_password(password, salt, params)
    dek = crypto.new_key()
    # onboarding_step 1: the sign-up walkthrough (frontend/welcome.html) is next; NULL (older accounts) means done.
    user = User(email_hash=email_hash, role=role, status="active", created_via=created_via, kdf_salt=salt,
                kdf_params=params.to_json(), pw_verifier=derived.auth, onboarding_step=1)
    db.add(user)
    db.flush()
    uid = user.id
    ik = crypto.new_key()  # the identity key: opens only the X25519 private key, and is never under the server KEK
    priv, pub = crypto.new_keypair()
    codes = [new_recovery_code() for _ in range(RECOVERY_CODES)]
    recovery_salt = crypto.new_salt()
    with crypto.as_user(uid, dek):
        user.pii_self = crypto.seal(_details(email, name), f"users.pii_self|{uid}")
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
    apk = admin_public_key(db)
    if apk is not None:
        user.pii_admin = crypto.seal_to(apk[0], _details(email, name), f"users.pii_admin|{uid}")
        user.pii_admin_version = apk[1]
    db.add(Membership(user_id=uid, stripe_ref=secrets.token_urlsafe(16)))
    db.add(EmailPrefs(user_id=uid, unsubscribe_id=secrets.token_urlsafe(16)))
    return NewAccount(uid, dek, ik, codes)


def create_user(email: str, password: str, *, name: str = "", role: str = "user", created_via: str = "open",
                background: bool = False) -> NewAccount:
    """A new account with its own "My job search" workspace (what sign-up does, Phase K; tests use it now).
    Making someone an admin also needs the admin details key shared with them (Phase F)."""
    from backend import migrate_encryption

    email, password, name = clean_email(email), check_password(password), clean_name(name)
    if role not in ("user", "admin"):
        raise ValueError("role must be user or admin")
    if not migrate_encryption.is_done():  # sign-ups wait for the one-time encryption (accounts design §5.4)
        raise AccountError("email", "JobHuntPA is encrypting its data. Try again in a few minutes.")
    db = SessionLocal()
    try:
        with _wrapping_if(background):  # the server-key wrap is committed with the account
            account = _create_user(db, email=email, password=password, name=name, role=role, created_via=created_via,
                                   background=background)
            with crypto.as_user(account.user_id, account.dek):  # the workspace's name is sealed under the new key
                db.add(Workspace(name=DEFAULT_WORKSPACE, owner_id=account.user_id))
                db.commit()
        log.info("Account created: user %s (%s)", account.user_id, created_via)
        return account
    finally:
        db.close()


def create_first_admin(email: str, password: str, *, name: str = "", created_via: str = "first_run",
                       background: bool = True) -> NewAccount:
    """The first account (section 5.3), in one transaction: the user and keys, a new admin details key and this
    admin's grant of it, recovery codes, membership and email preferences; every workspace without an owner becomes
    theirs (or a first workspace is made). AlreadySetUp if an account exists; AccountError for a bad value."""
    global _users_exist
    email, password, name = clean_email(email), check_password(password), clean_name(name)
    with _wrapping_if(background), _create_lock:  # (key lock first, as a rotation takes it)
        db = SessionLocal()
        try:
            db.execute(text("BEGIN IMMEDIATE"))  # no other connection can add a user meanwhile
            if db.query(User.id).first() is not None:
                raise AlreadySetUp("An account already exists")
            apk_priv, apk_pub = crypto.new_keypair()
            apk_version = int(_setting(db, "apk_version") or 0) + 1
            _put_setting(db, "apk_public", _b64(apk_pub))
            _put_setting(db, "apk_version", apk_version)
            db.flush()
            account = _create_user(db, email=email, password=password, name=name, role="admin",
                                   created_via=created_via, background=background)
            uid = account.user_id
            own_pub = db.get(UserKeys, uid).x25519_pub
            db.add(AdminKeyGrant(user_id=uid, apk_version=apk_version, granted_by=uid,
                                 sealed_priv=crypto.seal_to(own_pub, apk_priv, f"grant|{uid}|{apk_version}")))
            claimed = db.execute(update(Workspace).where(Workspace.owner_id.is_(None)).values(owner_id=uid)).rowcount
            if not db.query(Workspace.id).filter(Workspace.owner_id == uid).first():
                with crypto.as_user(uid, account.dek):
                    db.add(Workspace(name=DEFAULT_WORKSPACE, owner_id=uid))
                    db.flush()
            # The existing data is encrypted with this admin's key (backend.migrate_encryption): right away at the
            # first run, or at their next sign-in.
            _put_setting(db, "encryption_migration", {"state": "pending", "owner": uid})
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        _users_exist = True
    _remove_first_run_token()
    log.info("First admin account created: user %s (%s); %d existing workspace(s) are now theirs", uid,
             created_via, claimed)
    return account


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------

_DUMMY_SALT = os.urandom(crypto.SALT_LEN)


def find_by_email(db: Session, email: Any) -> Optional[User]:
    """The account for ``email`` (looked up by its hash; nothing is decrypted), or None. A match found under a
    previous pepper is rewritten under the current one."""
    if not isinstance(email, str) or not email.strip() or len(email) > 4 * EMAIL_MAX:
        return None
    server = server_keys()
    candidates = server.email_hash_candidates(email)
    user = db.query(User).filter(User.email_hash.in_(candidates)).first()
    if user is not None and user.email_hash != candidates[0]:
        user.email_hash = candidates[0]
        db.commit()
    return user


def dummy_derivation(password: Any) -> None:
    """The same work as checking a real password, so an unknown email address takes as long to refuse."""
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
        ik = migrate_identity(row, user_id, dek, password_kek)
    except (crypto.CryptoError, ValueError):
        db.rollback()
        log.warning("user %s: the identity key could not be made; the admin key stays closed until the next sign-in",
                    user_id)
        return None, False
    log.info("user %s: identity key made; the private key moved from the data key to it", user_id)
    return ik, True


def migrate_identity(row: UserKeys, user_id: int, dek: bytes, password_kek: bytes) -> bytes:
    """Gives an account from before the identity key one, in the caller's transaction (not committed): a new IK, the
    X25519 private key moved from its DEK-sealed form to a wrap under the IK, the IK wrapped under ``password_kek``
    (the password key), and the unconfirmed recovery codes (if any) moved from the DEK to the IK. The DEK-sealed
    copies are overwritten; the caller scrubs the write-ahead log after committing. Everything is computed before the
    row changes, so a failure leaves it as it was (CryptoError)."""
    with crypto.as_user(user_id, dek):
        priv = crypto.unseal(row.x25519_priv_enc, f"user_keys.x25519_priv|{user_id}")
        pending = crypto.unseal(row.pending_codes_enc, _codes_label(user_id)) if row.pending_codes_enc else None
    ik = crypto.new_key()
    wrapped_priv = crypto.wrap(ik, priv, _ik_priv_label(user_id))
    wrapped_ik = crypto.wrap(password_kek, ik, _pw_ik_label(user_id))
    wrapped_pending = crypto.wrap(ik, pending, _pending_codes_label(user_id)) if pending is not None else None
    row.x25519_priv_enc, row.pw_wrapped_ik = wrapped_priv, wrapped_ik
    if wrapped_pending is not None:
        row.pending_codes_enc = wrapped_pending
    return ik


def open_private_key(row: UserKeys, user_id: int, ik: bytes) -> bytes:
    """The account's X25519 private key, opened with its identity key (CryptoError when it isn't the account's)."""
    return crypto.unwrap(ik, row.x25519_priv_enc, _ik_priv_label(user_id))


def replace_identity(db: Session, user_id: int, row: UserKeys, password_kek: bytes, reason: str) -> tuple[bytes, bool]:
    """A new X25519 key pair and a new IK for an account whose old IK can't be opened (a reset by email that keeps
    the data, a reset with a recovery code made before the IK, or a reset that deletes the data). In the caller's
    transaction (not committed). The IK is wrapped under ``password_kek``; the caller wraps it under the recovery codes
    and the session. Whatever was sealed to the old public key is lost to this account: an admin's grant of the admin
    key is deleted (and audited with ``reason``) so another admin can share it again. Returns ``(ik, grant_lost)``."""
    from backend import admin_key

    ik = crypto.new_key()
    priv, pub = crypto.new_keypair()
    row.x25519_pub, row.x25519_priv_enc = pub, crypto.wrap(ik, priv, _ik_priv_label(user_id))
    row.pw_wrapped_ik = crypto.wrap(password_kek, ik, _pw_ik_label(user_id))
    # The codes that wrapped the old IK are of no use for it any more (and must not be taken for the new one).
    db.execute(update(RecoveryCode).where(RecoveryCode.user_id == user_id).values(wrapped_ik=None))
    lost = db.execute(delete(AdminKeyGrant).where(AdminKeyGrant.user_id == user_id)).rowcount > 0
    if lost:
        admin_key.audit(db, user_id, "admin_key_lost", user_id, {"reason": reason})
    return ik, lost


def details(db: Session, user_id: int) -> dict:
    """{email, name} from the user's own copy. Needs their key scope."""
    user = db.get(User, user_id)
    if user is None or user.pii_self is None:
        return {"email": "", "name": ""}
    return json.loads(crypto.unseal(user.pii_self, f"users.pii_self|{user_id}"))


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


@dataclass(frozen=True, repr=False)
class KeptReset:
    """What ``reset_password_keeping_data`` made: the new recovery codes (shown once), the new identity key (to sign
    the person in) and whether the account lost its share of the admin details key."""
    recovery_codes: list[str]
    ik: bytes
    admin_key_lost: bool

    def __repr__(self) -> str:  # never the key or the codes
        return "<users.KeptReset>"


def reset_password_keeping_data(db: Session, user_id: int, new_password: Any) -> Optional[KeptReset]:
    """The "keep data" half of a password reset by email (accounts design 6.5, 13 item 1). For an account with
    background access the server unwraps the data key with its KEK and wraps it again under ``new_password`` (new
    salt, KDF settings and verifier), forgets the wrong tries, ends every session, and **replaces the recovery
    codes**, in one transaction. The server can't open the identity key (it is never under the server KEK), so the
    account gets **a new key pair and a new identity key** (``replace_identity``): an admin loses their share of the
    admin details key and another admin shares it again. Returns the eight new codes (kept sealed until the person
    confirms saving them, and shown by the caller once) with the new IK. For an account without background access, or
    whose server key can't be opened, nothing changes: None (the caller then offers the reset that deletes the data).

    The old codes must go: each one opens the same data key. Whoever held the account before the person who just
    proved the mailbox (someone who signed up with this address first, who turned background access on and kept the
    codes) could otherwise use one to set a password of their own and read the data. The two-step secret needs no
    such care: the reset asked for a code from it, and a setup that was never finished is dropped here.

    The caller (password_reset.py) has already checked the reset token and, with two-step sign-in on, the code, and decides
    what to do about a disabled account (this works for one too: it can't sign in afterwards). AccountError for a
    password that's too short or too long, before anything is touched."""
    row = db.get(UserKeys, user_id)
    user = db.get(User, user_id)
    if row is None or user is None or row.server_wrapped_dek is None:
        return None
    new_password = check_password(new_password)
    dek = _server_dek(row, user_id)
    if dek is None:
        return None
    params = crypto.kdf_profile()
    salt = crypto.new_salt()
    derived = crypto.derive_password(new_password, salt, params)
    try:
        user.kdf_salt, user.kdf_params, user.pw_verifier = salt, params.to_json(), derived.auth
        row.pw_wrapped_dek = crypto.wrap(derived.kek, dek, f"pw|{user_id}")
        user.totp_pending_enc = None
        db.query(AuthSession).filter(AuthSession.user_id == user_id).delete(synchronize_session=False)
        ik, lost = replace_identity(db, user_id, row, derived.kek, "password reset by email")
        codes = replace_recovery_codes(db, user_id, dek, ik)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    ratelimit.forget_account(user_id)
    scrub_wal()  # the old password's and the old recovery codes' copies of the keys
    log.info("user %s: password reset by email; the data was kept (background access), the recovery codes "
             "replaced and a new identity key made%s", user_id, " (the admin key share is gone)" if lost else "")
    return KeptReset(codes, ik, lost)


@dataclass(frozen=True, repr=False)
class WipedReset:
    """What ``reset_password_wiping_data`` made: the new data key and identity key (to sign the person in), the new
    recovery codes (shown once), and whether the account lost its share of the admin details key."""
    dek: bytes
    ik: bytes
    recovery_codes: list[str]
    admin_key_lost: bool

    def __repr__(self) -> str:  # never the keys or the codes
        return "<users.WipedReset>"


def reset_password_wiping_data(db: Session, user_id: int, new_password: Any, email: Any) -> WipedReset:
    """The other half of a password reset by email (design 6.5), for an account the server can't open (no background
    access): the saved data is locked with the old password and can't be brought back, so it is thrown away and the
    account starts again under the new password. In one transaction: the workspaces and everything in them, the
    sealed upload files (after the commit), the recovery codes and ``billing_self`` go (the same removal as deleting
    the account, ``_remove_workspace_data``); the account gets a new data key, identity key, key pair and eight new recovery codes
    (kept sealed until the person confirms saving them), a fresh "My job search" workspace and ``pii_self`` rebuilt
    from ``email`` (the name is lost: only an admin's copy has it). Kept: the account row, ``pii_admin``, the
    membership, email preferences, the two-step secret and the admin role. Every session ends. An admin loses their
    share of the admin details key (its copy was sealed to the old key pair): the grant is deleted, an entry goes in
    the audit log, and another admin can share the key again.

    The caller has checked the reset token and, with two-step sign-in on, the code. AccountError for a bad password
    or address, AccountBusy while a run of the account is active, LookupError for an unknown account."""
    from backend import tasks

    new_password, email = check_password(new_password), clean_email(email)
    new = new_password_keys(new_password)  # about a second on a Pi: before the database's write lock
    dek = crypto.new_key()
    with tasks.start_lock():  # no run can be queued for these workspaces while their rows are removed
        try:
            _write_lock(db)
            user = db.get(User, user_id, populate_existing=True)
            row = db.get(UserKeys, user_id, populate_existing=True)
            if user is None or row is None:
                raise LookupError("No such account")
            ws_ids = [i for (i,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id)]
            _refuse_active_runs(db, ws_ids)
            files = _remove_workspace_data(db, user_id, ws_ids)
            user.kdf_salt, user.kdf_params, user.pw_verifier = new.salt, new.params.to_json(), new.derived.auth
            user.totp_pending_enc = None  # (an authenticator setup the previous holder started, never finished)
            user.prefs_enc = None  # sealed under the old key
            user.onboarding_step = 1  # the search setup is gone: the sign-up walkthrough runs again
            row.dek_kid, row.pw_wrapped_dek = crypto.key_id(dek), crypto.wrap(new.derived.kek, dek, f"pw|{user_id}")
            row.server_wrapped_dek = row.server_kek_id = row.background_since = None  # (a copy of the old key)
            ik, lost = replace_identity(db, user_id, row, new.derived.kek, "password reset by email")
            with crypto.as_user(user_id, dek):
                user.pii_self = crypto.seal(_details(email, ""), f"users.pii_self|{user_id}")
                db.add(Workspace(name=DEFAULT_WORKSPACE, owner_id=user_id))
                codes = replace_recovery_codes(db, user_id, dek, ik)  # the old codes and their wraps of the old keys go
                db.flush()
            db.execute(update(Membership).where(Membership.user_id == user_id).values(billing_self=None))
            db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    removed = _remove_uploads(user_id, files)
    ratelimit.forget_account(user_id)
    scrub_wal()  # the old password's, the old recovery codes' and the old key's copies of the data key
    log.info("user %s: password reset by email without a key; the saved data was deleted (%d workspace(s), %d "
             "file(s))", user_id, len(ws_ids), removed)
    return WipedReset(dek, ik, codes, lost)


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
    """How many unused codes can't open the identity key: made before it existed. A reset with one keeps the data but
    gives the account a new key pair, so an admin needs the admin key shared again (Settings suggests new codes)."""
    return db.query(RecoveryCode).filter(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None),
                                         RecoveryCode.wrapped_dek.isnot(None),
                                         RecoveryCode.wrapped_ik.is_(None)).count()


def open_recovery_code(db: Session, user_id: int, typed: Any, *, not_id: Optional[int] = None
                       ) -> Optional[tuple[int, bytes, Optional[bytes]]]:
    """``(code id, DEK, IK)`` if ``typed`` is one of this user's unused recovery codes (the code's own wrap opens it, so
    nothing else is compared), else None. The IK is None for a code made before the identity key existed. Nothing is
    used up yet: ``spend_recovery_code`` does that. ``not_id`` skips one code (the first of two typed)."""
    code = normalise_recovery_code(typed)
    row = db.get(UserKeys, user_id)
    if code is None or row is None:
        return None
    kek = crypto.recovery_kek(code, row.recovery_salt)
    for rc in db.query(RecoveryCode).filter(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None),
                                            RecoveryCode.wrapped_dek.isnot(None)).order_by(RecoveryCode.id):
        if rc.id == not_id:
            continue
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
# Authenticator-app codes (Phase E). The secret is wrapped with the server KEK, labelled ``totp|{uid}|{kek_id}``
# (design §3 item 6 and §15): not with the DEK, so a password reset by email can still ask for a code.
# ---------------------------------------------------------------------------

def seal_totp(user_id: int, secret: str) -> bytes:
    return server_keys().kek_wrap(secret.encode("ascii"), f"totp|{user_id}")[1]


def open_totp(user_id: int, blob: Optional[bytes]) -> Optional[str]:
    """The base32 secret in ``blob``, or None (nothing stored, or it doesn't open with the key file: fail closed)."""
    if not blob:
        return None
    try:
        return server_keys().kek_unwrap(bytes(blob), f"totp|{user_id}").decode("ascii")
    except (crypto.CryptoError, UnicodeError):
        log.warning("user %s: the two-step secret doesn't open with the key file", user_id)
        return None


def start_totp_enrolment(db: Session, user: User) -> str:
    """A new pending secret (replacing any earlier one). Returns it for the QR code and the written key."""
    secret = totp.new_secret()
    with server_keys().wrapping():
        user.totp_pending_enc = seal_totp(user.id, secret)
        db.commit()
    return secret


def verify_totp(db: Session, user: User, code: Any) -> bool:
    """Whether ``code`` is a current code for this user's secret: within a step of now, and not for a step already
    used (``totp_last_step``, updated atomically, so one code can't be used twice, not even by two requests at the
    same moment). Committed at once."""
    secret = open_totp(user.id, user.totp_secret_enc)
    if secret is None:
        return False
    step = totp.match_step(secret, code)
    if step is None:
        return False
    claimed = db.execute(update(User).where(User.id == user.id, or_(User.totp_last_step.is_(None),
                                                                   User.totp_last_step < step))
                         .values(totp_last_step=step)).rowcount
    db.commit()
    db.expire(user, ["totp_last_step"])
    return claimed == 1


def enable_totp(db: Session, user: User, code: Any) -> Optional[bool]:
    """Finishes enrolment: ``code`` must fit the pending secret, which then becomes the account's. None if no
    enrolment was started, False for a wrong code."""
    secret = open_totp(user.id, user.totp_pending_enc)
    if secret is None:
        return None
    step = totp.match_step(secret, code)
    if step is None:
        return False
    with server_keys().wrapping():
        user.totp_secret_enc, user.totp_pending_enc = seal_totp(user.id, secret), None
        user.totp_last_step, user.mfa_enabled = step, True  # the code that finished enrolment can't be used again
        db.commit()
    return True


def disable_totp(db: Session, user: User) -> None:
    user.mfa_enabled, user.totp_secret_enc, user.totp_pending_enc, user.totp_last_step = False, None, None, None
    db.commit()
    scrub_wal()  # the secret must not linger in the write-ahead log


# ---------------------------------------------------------------------------
# Deleting an account (one routine: Settings calls it for your own, the admin panel for anyone's)
# ---------------------------------------------------------------------------

class AccountBusy(Exception):
    """One of the account's runs is still queued or running."""


class AccountProtected(Exception):
    """The account is the last active admin's: JobHuntPA needs one, and nobody could take the admin details key over."""


def is_last_admin(db: Session, user_id: int) -> bool:
    """Whether this is an active admin and no other active admin exists."""
    user = db.get(User, user_id)
    if user is None or user.role != "admin" or user.status != "active":
        return False
    return not db.query(User.id).filter(User.role == "admin", User.status == "active", User.id != user_id).first()


def _write_lock(db: Session) -> None:
    """Take SQLite's write lock now (it is kept until the commit), unless this session already holds it, so what is
    read next (the admins, the key, the uploads) can't change before the commit."""
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
    if is_last_admin(db, user_id):
        raise AccountProtected("This is the last admin")
    ws_ids = [i for (i,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id)]
    _refuse_active_runs(db, ws_ids)


def _remove_workspace_data(db: Session, user_id: int, ws_ids: list[int]) -> list[str]:
    """Everything the account saved in its workspaces, in ``db``'s transaction (not committed): jobs with their
    scores, cover letters and merge decisions, documents, chats, profile, dealbreakers, pins, sources, schedules, runs,
    the workspaces themselves, the commute cache and the pre-accounts search profile when this account holds it.
    Returns the stored names of the sealed upload files: delete them (``_remove_uploads``) after the commit. The
    caller holds ``tasks.start_lock`` and the database's write lock. Shared by deleting an account and by a password
    reset that has to throw the saved data away (``reset_password_wiping_data``)."""
    from backend import migrate_encryption

    jobs = select(Job.id).where(Job.workspace_id.in_(ws_ids))
    threads = select(ChatThread.id).where(ChatThread.workspace_id.in_(ws_ids))
    files = [name for (name,) in db.query(Document.stored_name).filter(Document.workspace_id.in_(ws_ids)) if name]
    db.execute(delete(ChatMessage).where(ChatMessage.thread_id.in_(threads)))
    db.execute(delete(ChatThread).where(ChatThread.workspace_id.in_(ws_ids)))
    db.execute(delete(CoverLetter).where(CoverLetter.job_id.in_(jobs)))
    db.execute(delete(FitResult).where(FitResult.job_id.in_(jobs)))
    db.execute(delete(JobDuplicate).where(JobDuplicate.workspace_id.in_(ws_ids)))
    db.execute(delete(Job).where(Job.workspace_id.in_(ws_ids)))
    for model in (Document, Pin, CompanySource, SearchRun):
        db.execute(delete(model).where(model.workspace_id.in_(ws_ids)))
    for model in (SearchSchedule, UserProfile, DealbreakerSet):  # one row per workspace, id = workspace id
        db.execute(delete(model).where(model.id.in_(ws_ids)))
    db.execute(update(LlmUsage).where(LlmUsage.workspace_id.in_(ws_ids)).values(workspace_id=None))
    db.execute(delete(Workspace).where(Workspace.owner_id == user_id))
    db.execute(delete(CommuteCache).where(CommuteCache.owner_id == user_id))
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


def delete_account(db: Session, user_id: int, *, actor_id: Optional[int] = None) -> dict:
    """The one way an account is removed (a person deleting their own, or an admin deleting someone's): the account
    and everything of theirs, in one transaction (their sealed upload files after it): workspaces with every row in
    them (jobs, scores, cover letters, documents, chats, profile, dealbreakers, pins, sources, schedules, runs, merge
    decisions), sessions, recovery codes, tokens, keys (the data key's and the identity key's wraps with them),
    membership and grants, email preferences and the admin grant.
    Shared postings, company profiles and the geocode cache stay; so do usage counts (``llm_usage``, no longer tied
    to anyone) and the audit log (its user ids become NULL).

    Deleting an admin replaces the admin details key for the remaining admins, in the same transaction, so what
    they held opens nothing written afterwards. The key is opened through ``actor_id``'s grant (the admin doing the
    deleting; their own request's key scope) or, with no ``actor_id``, through the deleted admin's own. An entry in
    the audit log (no personal details) is written when an admin does the deleting or an admin is deleted.

    Needs no key of the user's: it never opens a sealed value. Refuses (AccountBusy) while one of the account's runs
    is active and (AccountProtected) for the last active admin; ApkUnavailable when the admin key can't be opened.
    The person's Stripe subscription is cancelled (``membership.cancel_for_deletion``; design §6.8 step 2) under the
    lock, after every refusal above (the last one is the admin key) and before the first delete: best effort, so a
    Stripe that can't be reached never keeps an account from being deleted (the result's ``subscription`` says
    ``cancelled``, ``none`` or ``failed``, and a failure is written to the audit log). A refused deletion never
    cancels anything. Explicit deletes, not cascades: a database from before accounts has workspace tables
    without foreign keys."""
    from backend import admin_key, membership, sessions, tasks

    if db.get(User, user_id) is None:
        raise LookupError("No such account")
    _check_deletable(db, user_id)
    subscription = "none"
    with tasks.start_lock():  # no run can be queued for these workspaces while their rows are removed
        try:
            _write_lock(db)
            user = db.get(User, user_id, populate_existing=True)
            if user is None:
                raise LookupError("No such account")
            was_admin = user.role == "admin"
            if is_last_admin(db, user_id):
                raise AccountProtected("This is the last admin")
            ws_ids = [i for (i,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id)]
            _refuse_active_runs(db, ws_ids)
            apk = admin_key.open_apk(db, user_id if actor_id is None else actor_id) if was_admin else None
            subscription = membership.cancel_for_deletion(db, user_id, actor_id)  # the last step that can't be undone
            files = _remove_workspace_data(db, user_id, ws_ids)
            db.execute(update(LlmUsage).where(LlmUsage.user_id == user_id).values(user_id=None))
            db.execute(update(AdminAudit).where(AdminAudit.admin_id == user_id).values(admin_id=None))
            db.execute(update(AdminAudit).where(AdminAudit.target_user_id == user_id).values(target_user_id=None))
            db.execute(update(AdminKeyGrant).where(AdminKeyGrant.granted_by == user_id).values(granted_by=None))
            db.execute(update(MembershipGrant).where(MembershipGrant.created_by == user_id).values(created_by=None))
            db.execute(update(AuthToken).where(AuthToken.created_by == user_id).values(created_by=None))
            for model in (AuthSession, RecoveryCode, AuthToken, AdminKeyGrant, MembershipGrant):
                db.execute(delete(model).where(model.user_id == user_id))
            for model in (Membership, EmailPrefs, UserKeys):
                db.execute(delete(model).where(model.user_id == user_id))
            db.execute(delete(User).where(User.id == user_id))
            detail: dict[str, Any] = {"role": "admin" if was_admin else "user", "workspaces": len(ws_ids),
                                      "files": len(files)}
            if actor_id is not None:
                detail["user_id"] = user_id
            public = None
            if was_admin:
                remaining = admin_key.active_admin_ids(db)
                rotation = admin_key.new_key(db, apk, remaining, actor_id)
                detail.update(apk_version=rotation.apk.version, resealed=rotation.resealed, stale=rotation.stale,
                              dropped=rotation.dropped)
                public = rotation.apk.public
            if subscription != "none":
                detail["subscription"] = subscription
            if was_admin or actor_id is not None or subscription == "failed":
                admin_key.audit(db, actor_id, "user_deleted", None, detail, public=public)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    removed = _remove_uploads(user_id, files)  # after the commit
    scrub_wal()  # the deleted account's key copies and secrets must not linger in the write-ahead log
    sessions._active_on.pop(user_id, None)  # SQLite may give this id to the next account
    log.info("Account deleted: user %s (%d workspace(s), %d file(s))", user_id, len(ws_ids), removed)
    out = {"workspaces": len(ws_ids), "files": removed, "apk_rotated": was_admin}
    if subscription != "none":  # only said when Stripe was involved: "cancelled" or "failed"
        out["subscription"] = subscription
    return out
