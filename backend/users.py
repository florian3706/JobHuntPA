"""Accounts: creating them with their keys and recovery codes, unlocking them with a password, the server key file at
start-up, and the first run. Design: docs/design/accounts.md, sections 1, 4 and 5.

**Keys of an account** (all made here, at creation):
  * a random data key (DEK) for all of the user's data, wrapped under the password (``pw|{uid}``) and under each of
    8 recovery codes (``rc|{uid}|{code_id}``); a session wraps it again under its cookie (backend.sessions);
  * an X25519 key pair, its private half under the DEK, so an admin details key can be shared with this user;
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
import hmac
import json
import logging
import os
import re
import secrets
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from backend import config, crypto, keys
from backend.db import (
    DB_PATH,
    AdminKeyGrant,
    AppSetting,
    AuthSession,
    EmailPrefs,
    Membership,
    RecoveryCode,
    SessionLocal,
    User,
    UserKeys,
    Workspace,
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

_EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+")
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
    recovery_codes: list[str]

    def __repr__(self) -> str:  # never the key or the codes
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
    return f"user_keys.pending_codes|{uid}"


def pending_recovery_codes(db: Session, user_id: int) -> Optional[list[str]]:
    """The recovery codes this user hasn't yet confirmed saving, or None. Needs the user's key scope."""
    row = db.get(UserKeys, user_id)
    if row is None or row.pending_codes_enc is None:
        return None
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


def startup() -> None:
    """At every start, after init_db: load (or, with no accounts, create) the server key file and check it belongs
    to this database, clear expired sessions, then either create the admin from the process environment, or write
    the first-run token. Raises keys.KeyFileError (start-up stops) for a missing or wrong key file."""
    global _server_keys, _users_exist
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
    priv, pub = crypto.new_keypair()
    codes = [new_recovery_code() for _ in range(RECOVERY_CODES)]
    recovery_salt = crypto.new_salt()
    with crypto.as_user(uid, dek):
        user.pii_self = crypto.seal(_details(email, name), f"users.pii_self|{uid}")
        user_keys = UserKeys(user_id=uid, dek_kid=crypto.key_id(dek), pw_wrapped_dek=crypto.wrap(derived.kek, dek, f"pw|{uid}"),
                             x25519_pub=pub, x25519_priv_enc=crypto.seal(priv, f"user_keys.x25519_priv|{uid}"),
                             recovery_salt=recovery_salt,
                             pending_codes_enc=crypto.seal(json.dumps(codes).encode("ascii"), _codes_label(uid)))
    if background:  # the DEK under the server KEK (Phase G runs scheduled searches with it)
        user_keys.server_kek_id, user_keys.server_wrapped_dek = server.kek_wrap(dek, f"srv|{uid}")
        user_keys.background_since = utcnow()
    db.add(user_keys)
    for code in codes:
        row = RecoveryCode(user_id=uid)
        db.add(row)
        db.flush()
        row.wrapped_dek = crypto.wrap(crypto.recovery_kek(normalise_recovery_code(code), recovery_salt), dek,
                                      f"rc|{uid}|{row.id}")
    apk = admin_public_key(db)
    if apk is not None:
        user.pii_admin = crypto.seal_to(apk[0], _details(email, name), f"users.pii_admin|{uid}")
        user.pii_admin_version = apk[1]
    db.add(Membership(user_id=uid, stripe_ref=secrets.token_urlsafe(16)))
    db.add(EmailPrefs(user_id=uid, unsubscribe_id=secrets.token_urlsafe(16)))
    return NewAccount(uid, dek, codes)


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
    with _create_lock:
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


def unlock_with_password(db: Session, user: User, password: Any) -> Optional[bytes]:
    """The user's DEK if ``password`` is theirs, else None. When the KDF profile has changed since the password was
    set, it is derived again with the new settings and stored (one extra derivation, this time only)."""
    if not isinstance(password, str) or not password or len(password) > PASSWORD_MAX:
        dummy_derivation("")
        return None
    params = crypto.KdfParams.from_json(user.kdf_params)
    derived = crypto.derive_password(password, user.kdf_salt, params)
    if not crypto.verify_password(user.pw_verifier, derived):
        return None
    row = db.get(UserKeys, user.id)
    dek = crypto.unwrap(derived.kek, row.pw_wrapped_dek, f"pw|{user.id}")
    current = crypto.kdf_profile()
    if params != current:
        salt = crypto.new_salt()
        again = crypto.derive_password(password, salt, current)
        user.kdf_salt, user.kdf_params, user.pw_verifier = salt, current.to_json(), again.auth
        row.pw_wrapped_dek = crypto.wrap(again.kek, dek, f"pw|{user.id}")
        db.commit()
        log.info("user %s: password key settings updated to the current profile", user.id)
    return dek


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
    try:
        return server_keys().kek_unwrap(row.server_wrapped_dek, f"srv|{user_id}")
    except crypto.CryptoError:
        log.warning("user %s: the background-access key doesn't open with the key file", user_id)
        return None


def workspace_ids(db: Session, user_id: int) -> list[int]:
    return [ws for (ws,) in db.query(Workspace.id).filter(Workspace.owner_id == user_id).order_by(Workspace.id)]


def is_locked(user: User, now: Optional[datetime] = None) -> float:
    """Seconds until this account may try a password again (0 when it may now)."""
    now = now or utcnow()
    return max(0.0, (user.locked_until - now).total_seconds()) if user.locked_until else 0.0
