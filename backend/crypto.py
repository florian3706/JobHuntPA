"""Encryption at rest: sealed columns and files under each user's data key, key wrapping, sealed boxes, and the
password key derivation. Design: docs/design/accounts.md, sections 1, 2.1 and 3.

Nothing here invents a primitive. AES-256-GCM, X25519 and HKDF-SHA256 come from ``cryptography``; argon2id comes from
``argon2-cffi``; HMAC-SHA256 and the random numbers come from the standard library (``hmac``, ``os.urandom``,
``secrets``).

**Envelope v1** (sealed values, wrapped keys and sealed files), 33 bytes longer than the plaintext::

    0x01 | kid (4) | nonce (12) | AES-256-GCM ciphertext + tag (16)

* ``kid = HMAC-SHA256(key, "kid")[:4]`` names the key without revealing it, so opening a value under another user's
  key fails at once with ``KeyMismatch`` instead of a confusing tag failure.
* The nonce is 96 random bits per value. One key would need billions of values before that is a concern (design 1.2).
* The AES-GCM associated data is the 5-byte header followed by a label. The header binds the version and kid to the
  ciphertext; the label binds the value to where it belongs, so a value copied somewhere else fails to open.

**Sealed box** (X25519, for the admin personal-details key), 85 bytes longer than the plaintext::

    0x02 | recipient kid (4) | ephemeral X25519 public key (32) | nonce (12) | ciphertext + tag (16)

**Associated data, one namespace per use.** Callers pass the design's short labels; the functions add a prefix of
their own, so a blob made for one purpose can never be opened as another, even under the same key:

========================  ==========================  ======================================================
function                  AAD after the header        why
========================  ==========================  ======================================================
Sealed* column types      ``jh/<table>.<column>``     a value moved to another column (or table) won't open
``seal``/``unseal``       ``jh-value|<label>``        values kept outside a sealed column, under the DEK
``wrap``/``unwrap``       ``jh-wrap|<label>``         keys under a key: ``pw|{uid}``, ``rc|{uid}|{code_id}``,
                                                      ``sess|{sid}``, ``srv|{uid}|{kek_id}``, ``totp|{uid}``
``seal_file``/``open_*``  ``jh-file|<label>``         an upload, labelled ``file|{stored_name}``: a file
                                                      renamed onto another document's name won't open
``seal_to``/``open_*``    ``jh-box|<label>``          admin-readable details, labelled by table and column
========================  ==========================  ======================================================

What the labels can't do: a column label doesn't name the row (the id doesn't exist yet when a row is inserted), so
someone who can *write* the database could swap one user's sealed values between two of that user's rows in the
same column. The threat model is someone who can *read* the database (design, "Threat model").

**Key scope.** The data key (DEK) of the user a request or background run acts for is kept in a context variable:
``with as_user(user_id, dek): ...``. Sealing or opening outside one raises ``Locked``. Thread pools must start work
through ``submit`` (re-exported from backend/threads.py), which copies the context into the worker.

**Sealed columns.** ``SealedText``, ``SealedJSON``, ``SealedInt``, ``SealedFloat``, ``SealedBool`` and
``SealedDateTime`` encrypt on write and decrypt on read under the current scope. NULL stays NULL; a value that isn't
an envelope (plaintext from before encryption) is read as it is and coerced to the column's type, until the
encryption migration has finished: from then on **strict mode** (``set_strict``) refuses it with ``Plaintext`` (a
``KeyMismatch``, so the API answers 404), because someone who can write the database could otherwise swap a sealed
value for a plaintext one of their choosing. Never give a sealed column a ``server_default``.

``SealedAlways(SealedText)`` (and the other kinds) is for columns where even whether a value is set is personal
(``jobs.hidden``, ``jobs.excluded_reason``, the office override, a job's own content): NULL is written as a sealed
marker, every value is padded (to ``pad`` bytes, then to a multiple of 32), so the stored bytes don't show whether
it is set, and SQL can't test it at all, not even with IS NULL. A model gives such a column ``default=always_none``
so an INSERT that leaves it out still writes the marker.

SQL can't compare ciphertexts, so any operator other than IS NULL / IS NOT NULL raises ``TypeError`` when the query
is built. The comparator can't see a bare ``order_by(col)``, ``group_by(col)``, ``select(...).distinct()`` or
``func.x(col)``, so a compile hook (``_refuse_sql_on_sealed``) refuses those with ``SealedSqlError`` the first time
a statement of that shape is compiled: sort and de-duplicate in Python, or compare a ``blind_index``.

**The process key.** ``http_cache`` holds listing pages whose URLs carry search terms. Its rows are keyed by
``process_index(url)`` and its bodies sealed with ``ProcessSealedText``, both under a random key that exists only in
this process's memory, so they can't be read after a restart (startup deletes them).
"""
from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from argon2.low_level import ARGON2_VERSION, Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from datetime import date, datetime

from sqlalchemy import Column, LargeBinary, Table, event
from sqlalchemy.exc import DontWrapMixin
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.elements import ColumnClause, Null
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import Select
from sqlalchemy.types import TypeDecorator

from backend import config  # noqa: F401  (loads .env first: JOBHUNT_KDF_PROFILE may be set there)
from backend.threads import run_in_context, submit  # noqa: F401  (part of this module's API, see the docstring)

log = logging.getLogger(__name__)

KEY_LEN = 32          # AES-256 and X25519 keys, DEKs and KEKs
NONCE_LEN = 12        # 96-bit AES-GCM nonces
KID_LEN = 4
TAG_LEN = 16
_V1 = 0x01            # envelope v1: AES-256-GCM
_V1_BYTE = bytes([_V1])
_BOX_V1 = 0x02        # sealed box v1: X25519 + HKDF-SHA256 + AES-256-GCM
_HEADER_LEN = 1 + KID_LEN
OVERHEAD = _HEADER_LEN + NONCE_LEN + TAG_LEN                 # 33
_BOX_HEADER_LEN = 1 + KID_LEN + KEY_LEN
BOX_OVERHEAD = _BOX_HEADER_LEN + NONCE_LEN + TAG_LEN         # 85
INDEX_LEN = 16

Label = Union[str, bytes]


# ---------------------------------------------------------------------------
# Errors. DontWrapMixin: SQLAlchemy raises these as they are, not wrapped in a StatementError, when they come out of
# a column type during a query, so handlers (KeyMismatch -> 404) see them.
# ---------------------------------------------------------------------------

class CryptoError(DontWrapMixin, Exception):
    """Base class of Locked, KeyMismatch and Corrupt; raised itself for a sealed type that has no label."""


class Locked(CryptoError):
    """No key scope: the code isn't running inside ``as_user`` (or a thread was started without ``submit``)."""


class KeyMismatch(CryptoError):
    """The value was sealed under a different key (another user's, or a key that no longer exists)."""


class Corrupt(KeyMismatch):
    """The value doesn't open under this key and label: it was moved from where it was sealed (another column, file
    name or label), changed, or truncated. A subclass of KeyMismatch, so callers that turn a mismatch into a 404 cover
    this too."""


class Plaintext(Corrupt):
    """Strict mode (after the encryption migration): a sealed column holds plaintext, or an always-sealed column
    holds NULL. Either was written past the app (or by a bug), so the value is refused rather than trusted."""


class SealedValueError(DontWrapMixin, TypeError):
    """A value of the wrong type for a sealed column (a bug). Not wrapped by SQLAlchemy, whose StatementError would
    print the plaintext parameters in its message, and so in logs."""


class SealedSqlError(DontWrapMixin, TypeError):
    """SQL that would sort, group, de-duplicate or run a function over a sealed column (a bug: the database only
    sees ciphertexts, so the result would be silently wrong)."""


# ---------------------------------------------------------------------------
# Small building blocks
# ---------------------------------------------------------------------------

def new_key() -> bytes:
    """A fresh random 256-bit key (a DEK, KEK, cookie secret, pepper or signing key)."""
    return secrets.token_bytes(KEY_LEN)


def key_id(key: bytes) -> bytes:
    """``HMAC-SHA256(key, "kid")[:4]``: names a key in an envelope without revealing anything about it."""
    return hmac.new(key, b"kid", hashlib.sha256).digest()[:KID_LEN]


def hkdf(ikm: bytes, info: bytes, *, salt: Optional[bytes] = None, length: int = KEY_LEN) -> bytes:
    """HKDF-SHA256 (RFC 5869) of ``ikm``, separated by the ``info`` label."""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def b64e(data: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    """The reverse of ``b64e``. Strict: binascii.Error (a ValueError) for any character outside the alphabet."""
    return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)


def _key(key: Any, what: str = "key") -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
        raise ValueError(f"The {what} must be {KEY_LEN} bytes")
    return bytes(key)


def _label(prefix: bytes, label: Label) -> bytes:
    """The associated data for ``label`` in the ``prefix`` namespace (see the table in the module docstring)."""
    if isinstance(label, str):
        label = label.encode("utf-8")
    elif not isinstance(label, (bytes, bytearray)):
        raise TypeError("A label must be str or bytes")
    return prefix + bytes(label)


def _as_bytes(value: Any, what: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{what} must be bytes")
    return bytes(value)


def _seal_with(aead: AESGCM, kid: bytes, plain: bytes, aad: bytes) -> bytes:
    header = bytes([_V1]) + kid
    nonce = os.urandom(NONCE_LEN)
    return header + nonce + aead.encrypt(nonce, _as_bytes(plain, "The plaintext"), header + aad)


def _open_with(aead: AESGCM, kid: bytes, blob: Any, aad: bytes) -> bytes:
    blob = _as_bytes(blob, "A sealed value")
    if len(blob) < OVERHEAD or blob[0] != _V1:
        raise Corrupt("Not a sealed value (unknown version, or truncated)")
    if not hmac.compare_digest(blob[1:_HEADER_LEN], kid):
        raise KeyMismatch("Sealed under a different key")
    try:
        return aead.decrypt(blob[_HEADER_LEN:_HEADER_LEN + NONCE_LEN], blob[_HEADER_LEN + NONCE_LEN:],
                            blob[:_HEADER_LEN] + aad)
    except InvalidTag:
        raise Corrupt("The sealed value doesn't open here (moved from where it was sealed, or changed)") from None


def is_sealed(value: Any) -> bool:
    """Whether ``value`` looks like an envelope v1 (a blob starting with 0x01, long enough to hold one)."""
    return (isinstance(value, (bytes, bytearray, memoryview)) and len(value) >= OVERHEAD
            and bytes(value[:1]) == bytes([_V1]))


def envelope_kid(blob: Any) -> Optional[bytes]:
    """The kid in an envelope v1 (which key sealed or wrapped it), or None when ``blob`` isn't one."""
    return bytes(blob[1:_HEADER_LEN]) if is_sealed(blob) else None


# ---------------------------------------------------------------------------
# Key scope
# ---------------------------------------------------------------------------

class Scope:
    """The key a request or background run acts with: one user's DEK, plus what is derived from it."""

    __slots__ = ("user_id", "dek", "kid", "_aead", "_index_key")

    def __init__(self, user_id: int, dek: bytes):
        self.user_id = user_id
        self.dek = _key(dek, "data key")
        self.kid = key_id(self.dek)
        self._aead = AESGCM(self.dek)
        # Blind indexes get their own key, so an index value never equals any MAC or ciphertext made with the DEK.
        self._index_key = hkdf(self.dek, b"jh index v1")

    def __repr__(self) -> str:  # never the key
        return f"<crypto.Scope user_id={self.user_id}>"


_scope: ContextVar[Optional[Scope]] = ContextVar("jobhunt_crypto_scope", default=None)


@contextlib.contextmanager
def as_user(user_id: int, dek: bytes) -> Iterator[Scope]:
    """Seal and open with ``user_id``'s data key inside the ``with`` block (and in threads started with ``submit``
    from it). Scopes nest; leaving one restores the one before."""
    token = _scope.set(Scope(user_id, dek))
    try:
        yield _scope.get()
    finally:
        _scope.reset(token)


def current() -> Scope:
    """The current key scope, or ``Locked`` when there is none."""
    scope = _scope.get()
    if scope is None:
        raise Locked("No key is unlocked here (not inside crypto.as_user)")
    return scope


def has_scope() -> bool:
    return _scope.get() is not None


def _seal_scope(plain: bytes, aad: bytes) -> bytes:
    scope = current()
    return _seal_with(scope._aead, scope.kid, plain, aad)


def _open_scope(blob: Any, aad: bytes) -> bytes:
    scope = current()
    return _open_with(scope._aead, scope.kid, blob, aad)


# ---------------------------------------------------------------------------
# Values, indexes, wrapped keys and files
# ---------------------------------------------------------------------------

def seal(plain: bytes, label: Label) -> bytes:
    """``plain`` sealed under the current user's DEK, bound to ``label``. For values outside a sealed column."""
    return _seal_scope(plain, _label(b"jh-value|", label))


def unseal(blob: bytes, label: Label) -> bytes:
    """Opens what ``seal`` made with the same ``label``. Locked, KeyMismatch or Corrupt otherwise."""
    return _open_scope(blob, _label(b"jh-value|", label))


def blind_index(purpose: str, value: Union[str, bytes]) -> bytes:
    """``HMAC(index_key, purpose \\0 value)[:16]``: the same input gives the same 16 bytes for this user only, so SQL
    can find a row by a sealed value (``jobs.url_index``, ``company_sources.careers_url_index``, commute cache keys)
    without the operator learning the value. ``purpose`` keeps one index from matching another's."""
    if "\0" in purpose:
        raise ValueError("The purpose can't contain a NUL character")
    data = value.encode("utf-8") if isinstance(value, str) else _as_bytes(value, "The value")
    msg = purpose.encode("utf-8") + b"\0" + data
    return hmac.new(current()._index_key, msg, hashlib.sha256).digest()[:INDEX_LEN]


def wrap(kek: bytes, secret: bytes, label: Label) -> bytes:
    """``secret`` (usually a DEK) encrypted under ``kek``, bound to ``label``: ``pw|{uid}`` under the password KEK,
    ``rc|{uid}|{code_id}`` under a recovery code's, ``sess|{sid}`` under a session's, ``srv|{uid}|{kek_id}`` and
    ``totp|{uid}`` under the server KEK. The label stops a wrap being moved to another user, code or session."""
    kek = _key(kek, "key-encryption key")
    return _seal_with(AESGCM(kek), key_id(kek), secret, _label(b"jh-wrap|", label))


def unwrap(kek: bytes, blob: bytes, label: Label) -> bytes:
    """Opens what ``wrap`` made. KeyMismatch for another key, Corrupt for another label or a changed blob."""
    kek = _key(kek, "key-encryption key")
    return _open_with(AESGCM(kek), key_id(kek), blob, _label(b"jh-wrap|", label))


def write_private(path: Union[str, Path], data: bytes, *, exclusive: bool = False) -> None:
    """Replace ``path`` with ``data`` atomically and owner-only (0600): a temp file in the same folder, flushed to
    disk, then ``os.replace``, then the folder flushed. A crash leaves the old file or the new one, never half of
    one. (Like app_settings._replace_file, but always 0600 and for bytes.)

    ``exclusive``: create ``path`` only if it doesn't exist (FileExistsError otherwise), still atomically: the temp
    file is hard-linked into place, which fails rather than replace an existing file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")  # created 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        if not exclusive:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise
            except OSError:  # a file system without hard links (FAT, some network shares)
                if os.path.lexists(path):
                    raise FileExistsError(errno.EEXIST, "File exists", str(path)) from None
                os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)  # left over after os.link or an error; already gone after os.replace
    fsync_dir(path.parent)


def fsync_dir(folder: Path) -> None:
    """Flush a folder's entries (a rename) to disk. POSIX only; Windows can't open a folder this way."""
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def seal_file(data: bytes, path: Union[str, Path], label: Label) -> None:
    """Write ``data`` sealed under the current user's DEK to ``path`` (atomic, 0600). Label it
    ``file|{stored_name}``, so the file opens only under its own name."""
    write_private(path, _seal_scope(data, _label(b"jh-file|", label)))


def open_file(path: Union[str, Path], label: Label) -> bytes:
    """The plaintext of a file written by ``seal_file`` with the same label, kept in memory only."""
    return _open_scope(Path(path).read_bytes(), _label(b"jh-file|", label))


# ---------------------------------------------------------------------------
# Sealed boxes (X25519): anyone with the public key can seal; only the private key opens. Used for the admin
# personal-details key (design 1.4), so sign-ups and the Stripe sync can write admin-readable details with no admin
# signed in.
# ---------------------------------------------------------------------------

def new_keypair() -> tuple[bytes, bytes]:
    """A new X25519 key pair as raw bytes: ``(private, public)``."""
    private = X25519PrivateKey.generate()
    return private.private_bytes_raw(), private.public_key().public_bytes_raw()


def public_key_of(private: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(_key(private, "private key")).public_key().public_bytes_raw()


def _box_kid(public: bytes) -> bytes:
    return hashlib.sha256(b"jh box kid|" + public).digest()[:KID_LEN]


def _box_key(shared: bytes, ephemeral_public: bytes, public: bytes) -> bytes:
    # HKDF over the shared secret, with both public keys in the label (design 1.1), so the key is tied to this
    # exchange and this recipient.
    return hkdf(shared, b"jh box v1|" + ephemeral_public + public)


def seal_to(public: bytes, plain: bytes, label: Label) -> bytes:
    """``plain`` sealed to the X25519 ``public`` key: a fresh ephemeral key pair per value, so each value has its own
    AES key. Label it with where it's stored (for example ``users.pii_admin|{uid}``)."""
    public = _key(public, "public key")
    recipient = X25519PublicKey.from_public_bytes(public)
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes_raw()
    shared = ephemeral.exchange(recipient)  # ValueError for a low-order public key (an all-zero shared secret)
    header = bytes([_BOX_V1]) + _box_kid(public) + ephemeral_public
    nonce = os.urandom(NONCE_LEN)  # the key is already single-use; a random nonce costs nothing
    aead = AESGCM(_box_key(shared, ephemeral_public, public))
    return header + nonce + aead.encrypt(nonce, _as_bytes(plain, "The plaintext"),
                                         header + _label(b"jh-box|", label))


def open_sealed(private: bytes, blob: bytes, label: Label) -> bytes:
    """Opens what ``seal_to`` made for this private key's public key, with the same label."""
    key = X25519PrivateKey.from_private_bytes(_key(private, "private key"))
    public = key.public_key().public_bytes_raw()
    blob = _as_bytes(blob, "A sealed box")
    if len(blob) < BOX_OVERHEAD or blob[0] != _BOX_V1:
        raise Corrupt("Not a sealed box (unknown version, or truncated)")
    if not hmac.compare_digest(blob[1:1 + KID_LEN], _box_kid(public)):
        raise KeyMismatch("Sealed to a different key")
    ephemeral_public = blob[1 + KID_LEN:_BOX_HEADER_LEN]
    try:
        shared = key.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
    except ValueError:
        raise Corrupt("The sealed box's ephemeral key is invalid") from None
    aead = AESGCM(_box_key(shared, ephemeral_public, public))
    try:
        return aead.decrypt(blob[_BOX_HEADER_LEN:_BOX_HEADER_LEN + NONCE_LEN], blob[_BOX_HEADER_LEN + NONCE_LEN:],
                            blob[:_BOX_HEADER_LEN] + _label(b"jh-box|", label))
    except InvalidTag:
        raise Corrupt("The sealed box doesn't open here (moved, or changed)") from None


# ---------------------------------------------------------------------------
# Sessions (design 3): the cookie holds a 32-byte secret; the database keeps only its SHA-256 (the session id) and
# the DEK wrapped under a key derived from it, so the database alone can't unwrap anything.
# ---------------------------------------------------------------------------

def session_id(cookie_secret: bytes) -> str:
    """``sessions.id``: SHA-256 of the cookie secret, as hex."""
    return hashlib.sha256(_key(cookie_secret, "cookie secret")).hexdigest()


def session_kek(cookie_secret: bytes) -> bytes:
    return hkdf(_key(cookie_secret, "cookie secret"), b"jh session dek v1")


def wrap_session_dek(cookie_secret: bytes, dek: bytes) -> bytes:
    """``sessions.wrapped_dek``, bound to this session's id."""
    return wrap(session_kek(cookie_secret), _key(dek, "data key"), f"sess|{session_id(cookie_secret)}")


def unwrap_session_dek(cookie_secret: bytes, wrapped: bytes) -> bytes:
    return unwrap(session_kek(cookie_secret), wrapped, f"sess|{session_id(cookie_secret)}")


def csrf_token(cookie_secret: bytes) -> str:
    """``b64url(HMAC(cookie_secret, "csrf"))``: nothing extra is stored; the page sends it back as X-CSRF-Token."""
    return b64e(hmac.new(_key(cookie_secret, "cookie secret"), b"csrf", hashlib.sha256).digest())


def check_csrf(cookie_secret: bytes, token: Optional[str]) -> bool:
    """Whether ``token`` is this session's CSRF token (constant-time)."""
    if not isinstance(token, str):
        return False
    return hmac.compare_digest(csrf_token(cookie_secret).encode("ascii"), token.encode("utf-8", "replace"))


def recovery_kek(code: str, salt: bytes) -> bytes:
    """The key a recovery code wraps the DEK with: ``HKDF(code, salt=user's recovery_salt)``. Recovery codes carry
    80 random bits, so they need no slow derivation. ``code`` must already be normalised (case, spaces, look-alike
    characters), which is the caller's job."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) < 16:
        raise ValueError("The recovery salt must be at least 16 bytes")
    return hkdf(code.encode("utf-8"), b"jh recovery v1", salt=bytes(salt))


# ---------------------------------------------------------------------------
# Passwords (design 1.1, 1.2 and 3.7): one argon2id run gives a 32-byte master value; HKDF splits it into the
# verifier (stored, compared in constant time) and the KEK that wraps the DEK. Neither reveals the other.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KdfParams:
    """argon2id cost settings, stored per user (``users.kdf_params``) so a profile change applies at next login."""
    time_cost: int
    memory_kib: int
    parallelism: int

    # Limits for values read back from the database: a hand-edited row mustn't make sign-in allocate gigabytes.
    _LIMITS = {"t": (1, 16), "m": (1024, 1024 * 1024), "p": (1, 16)}

    def to_json(self) -> str:
        return json.dumps({"alg": "argon2id", "v": ARGON2_VERSION, "t": self.time_cost, "m": self.memory_kib,
                           "p": self.parallelism}, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "KdfParams":
        try:
            d = json.loads(text)
        except (TypeError, ValueError):
            raise ValueError("Unreadable key-derivation settings") from None
        if not isinstance(d, dict) or d.get("alg") != "argon2id" or d.get("v") != ARGON2_VERSION:
            raise ValueError("Unsupported key-derivation settings")
        for name, (low, high) in cls._LIMITS.items():
            value = d.get(name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Key-derivation setting {name} is out of range")
        if d["m"] < 8 * d["p"]:
            raise ValueError("Key-derivation memory is too small for its parallelism")
        return cls(time_cost=d["t"], memory_kib=d["m"], parallelism=d["p"])

    def describe(self) -> str:
        return f"m={self.memory_kib / 1024:g} MiB, t={self.time_cost}, p={self.parallelism}"


KDF_PROFILES: dict[str, KdfParams] = {
    # RFC 9106's second recommendation and argon2-cffi's default. About 0.4-0.9 s on a Pi 4, 0.3 s on a Pi 5.
    "default": KdfParams(time_cost=3, memory_kib=64 * 1024, parallelism=4),
    # The OWASP minimum, for a Pi 3 or Zero 2 W.
    "low": KdfParams(time_cost=2, memory_kib=19 * 1024, parallelism=1),
    # Tests only: honoured only when JOBHUNT_TESTING=1.
    "test": KdfParams(time_cost=1, memory_kib=1024, parallelism=1),
}

SALT_LEN = 16
# At most two derivations at once: caps sign-in memory at 128 MiB on the default profile.
_KDF_SLOTS = threading.BoundedSemaphore(2)


def kdf_profile(name: Optional[str] = None) -> KdfParams:
    """The settings for new derivations: ``name`` or JOBHUNT_KDF_PROFILE (default|low); "test" only when
    JOBHUNT_TESTING=1. Anything else falls back to default, with a warning."""
    chosen = (name if name is not None else os.environ.get("JOBHUNT_KDF_PROFILE", "")).strip().lower() or "default"
    if chosen == "test" and os.environ.get("JOBHUNT_TESTING") != "1":
        log.warning("JOBHUNT_KDF_PROFILE=test is only for tests (JOBHUNT_TESTING=1); using default")
        chosen = "default"
    if chosen not in KDF_PROFILES:
        log.warning("Unknown JOBHUNT_KDF_PROFILE %r; using default", chosen[:20])
        chosen = "default"
    return KDF_PROFILES[chosen]


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_LEN)


@dataclass(frozen=True, repr=False)
class PasswordKeys:
    """What a password derives: ``auth`` (stored as ``users.pw_verifier``) and ``kek`` (wraps the DEK)."""
    auth: bytes
    kek: bytes

    def __repr__(self) -> str:  # never the keys
        return "<crypto.PasswordKeys>"


def derive_password(password: str, salt: bytes, params: KdfParams) -> PasswordKeys:
    """argon2id(password, salt, params) -> 32-byte master -> HKDF "auth" and "kek". The password is NFKC-normalised
    first (NIST SP 800-63B), so the same password typed on another keyboard or system gives the same keys."""
    if not isinstance(password, str):
        raise TypeError("The password must be a str")
    if not isinstance(salt, (bytes, bytearray)) or len(salt) < SALT_LEN:
        raise ValueError(f"The salt must be at least {SALT_LEN} bytes")
    secret = unicodedata.normalize("NFKC", password).encode("utf-8")
    with _KDF_SLOTS:
        master = hash_secret_raw(secret=secret, salt=bytes(salt), time_cost=params.time_cost,
                                 memory_cost=params.memory_kib, parallelism=params.parallelism, hash_len=KEY_LEN,
                                 type=Type.ID, version=ARGON2_VERSION)
    return PasswordKeys(auth=hkdf(master, b"jh auth v1"), kek=hkdf(master, b"jh kek v1"))


def verify_password(stored_verifier: bytes, keys: PasswordKeys) -> bool:
    """Whether a derivation matches the stored verifier, compared in constant time."""
    if not isinstance(stored_verifier, (bytes, bytearray)):
        return False
    return hmac.compare_digest(bytes(stored_verifier), keys.auth)


# ---------------------------------------------------------------------------
# Sealed column types
# ---------------------------------------------------------------------------

_strict = False


def set_strict(on: bool) -> None:
    """Strict mode (accounts design §15): once the encryption migration has finished, a sealed column that holds
    plaintext (or an always-sealed column that holds NULL) raises ``Plaintext`` instead of being read."""
    global _strict
    _strict = bool(on)


def strict() -> bool:
    return _strict


def _is_null(value: Any) -> bool:
    return value is None or isinstance(value, Null)


class _SealedComparator(TypeDecorator.Comparator):
    """Lets SQL test a sealed column only with IS NULL / IS NOT NULL (``== None`` and ``!= None`` compile to those),
    and an always-sealed one not at all. Everything else would compare random ciphertexts, which never match: raise
    at once instead of failing quietly."""

    __slots__ = ()

    def operate(self, op, *other, **kwargs):
        if op in (operators.is_, operators.is_not, operators.eq, operators.ne) and len(other) == 1 \
                and _is_null(other[0]) and not getattr(self.type, "always", False):
            return super().operate(op, *other, **kwargs)
        raise TypeError(_refusal(self.expr, op))

    def reverse_operate(self, op, other, **kwargs):
        raise TypeError(_refusal(self.expr, op))


def _refusal(expr: Any, op: Any) -> str:
    if getattr(getattr(expr, "type", None), "always", False):
        return f"{expr} is always sealed, so SQL can't test it at all (not even IS NULL): decide in Python."
    return (f"{expr} is encrypted, so SQL can only test it with IS NULL / IS NOT NULL (not "
            f"{getattr(op, '__name__', op)}). Filter, sort or de-duplicate in Python, or use a blind index.")


# An always-sealed value: a tag byte (none, or a value), then ISO/IEC 7816-4 padding (0x80, then zeros) up to the
# column's ``pad`` bytes or the next multiple of PAD_BLOCK beyond that, so the stored length doesn't show whether
# it's set (nor, for values shorter than ``pad``, how long it is).
_ALWAYS_NONE = b"\x00"
_ALWAYS_VALUE = b"\x01"
PAD_BLOCK = 32


def _pad(data: bytes, minimum: int) -> bytes:
    size = max(minimum, -(-(len(data) + 1) // PAD_BLOCK) * PAD_BLOCK)
    return data + b"\x80" + b"\x00" * (size - len(data) - 1)


def _unpad(data: bytes) -> bytes:
    body = data.rstrip(b"\x00")
    if not body.endswith(b"\x80"):
        raise Corrupt("A sealed value's padding is damaged")
    return body[:-1]


def always_none() -> None:
    """The ``default`` of an always-sealed column: an INSERT that leaves the column out still writes the sealed
    marker for NULL (``default=None`` would leave SQL NULL, which shows)."""
    return None


class _Sealed(TypeDecorator):
    """Base for the sealed column types: ``LargeBinary`` underneath; SQLite's dynamic typing lets an existing TEXT,
    INTEGER or REAL column hold the blobs without rebuilding the table."""

    impl = LargeBinary
    cache_ok = True
    comparator_factory = _SealedComparator

    def __init__(self, label: Optional[str] = None, *, always: bool = False, pad: int = PAD_BLOCK):
        """``label``: the AAD label, normally left out: attaching the column to a table sets ``<table>.<column>``.
        Give it to keep reading old values after renaming a table or column (use the old name).
        ``always`` and ``pad``: see ``SealedAlways``."""
        super().__init__()
        self.label = label
        self.always = bool(always)
        self.pad = int(pad)
        self._aad: Optional[bytes] = None if label is None else b"jh/" + label.encode("utf-8")

    def _column_aad(self) -> bytes:
        if self._aad is None:
            raise CryptoError(f"{type(self).__name__} isn't attached to a table column, so its values have no label "
                              "to be bound to. Use it in a Column of a Table, or give it label=...")
        return self._aad

    def column_name(self) -> str:
        """``table.column`` (for messages; never a value)."""
        return self._aad[3:].decode("utf-8", "replace") if self._aad else type(self).__name__

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[bytes]:
        if not self.always:
            if value is None:
                return None  # NULL stays NULL, so IS NULL filters keep working
            payload = self._encode(value)
        else:
            payload = _pad(_ALWAYS_NONE if value is None else _ALWAYS_VALUE + self._encode(value), self.pad)
        aad = self._column_aad()
        scope = _scope.get()  # (_seal_scope, inlined, as in process_result_value)
        if scope is None:
            raise Locked("No key is unlocked here (not inside crypto.as_user)")
        header = _V1_BYTE + scope.kid
        nonce = os.urandom(NONCE_LEN)
        return header + nonce + scope._aead.encrypt(nonce, payload, header + aad)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            if self.always and _strict:
                raise Plaintext(f"{self.column_name()} is NULL where a sealed value belongs")
            return None
        if isinstance(value, (bytearray, memoryview)):
            value = bytes(value)
        if type(value) is bytes and value[:1] == _V1_BYTE:
            # _open_scope, inlined: this runs for every sealed value of every row read (the job list opens tens of
            # thousands), so the per-call overhead matters on a Raspberry Pi.
            scope = _scope.get()
            if scope is None:
                raise Locked("No key is unlocked here (not inside crypto.as_user)")
            if len(value) < OVERHEAD:
                raise Corrupt("Not a sealed value (unknown version, or truncated)")
            if value[1:_HEADER_LEN] != scope.kid:  # (the kid is public: no need for a constant-time compare)
                raise KeyMismatch("Sealed under a different key")
            try:
                data = scope._aead.decrypt(value[_HEADER_LEN:_HEADER_LEN + NONCE_LEN],
                                           value[_HEADER_LEN + NONCE_LEN:],
                                           value[:_HEADER_LEN] + (self._aad or self._column_aad()))
            except InvalidTag:
                raise Corrupt("The sealed value doesn't open here (moved from where it was sealed, or changed)") \
                    from None
            if self.always:
                body = data.rstrip(b"\x00")
                if body[-1:] != b"\x80":
                    raise Corrupt("A sealed value's padding is damaged")
                if body[:1] == _ALWAYS_VALUE:
                    return self._decode(body[1:-1])
                if body == _ALWAYS_NONE + b"\x80":
                    return None
                raise Corrupt(f"{self.column_name()}: unknown sealed value tag")
            return self._decode(data)
        # Not an envelope: plaintext from before encryption (SQLite returns it as TEXT, INTEGER or REAL, never as a
        # BLOB starting with 0x01). Read it as it is, unless the migration has finished (strict mode).
        if _strict:
            raise Plaintext(f"{self.column_name()} holds a value that isn't sealed")
        return self._legacy(value)

    def coerce_legacy(self, value: Any) -> Any:
        """A plaintext value from before encryption, as this column's Python type (the encryption migration seals
        it). Best effort, like reading it: what can't be coerced is returned unchanged."""
        return None if value is None else self._legacy(value)

    # Subclasses: plaintext <-> bytes, and coercion of legacy plaintext (best effort: what can't be coerced is
    # returned unchanged, which is what the plain column type did before).
    def _encode(self, value: Any) -> bytes:
        raise NotImplementedError

    def _decode(self, data: bytes) -> Any:
        raise NotImplementedError

    def _legacy(self, value: Any) -> Any:
        raise NotImplementedError


def SealedAlways(kind: type, pad: int = PAD_BLOCK, label: Optional[str] = None) -> "_Sealed":
    """An always-sealed column of ``kind`` (``SealedText``, ``SealedBool``, ...): NULL is written as a sealed marker
    and every value is padded to at least ``pad`` bytes (then to a multiple of 32), so neither whether it is set nor
    (below ``pad``) its length shows, and SQL can't test it at all (accounts design §2.1 and §14.6). Give the Column
    ``default=always_none`` (or a real default) so an INSERT that leaves it out still seals."""
    if not (isinstance(kind, type) and issubclass(kind, _Sealed)):
        raise TypeError("SealedAlways takes a sealed column type, such as SealedText")
    return kind(label, always=True, pad=pad)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class SealedText(_Sealed):
    cache_ok = True  # SQLAlchemy reads it from each class itself, not inherited

    @property
    def python_type(self):
        return str

    def _encode(self, value: Any) -> bytes:
        if isinstance(value, str):
            return value.encode("utf-8")
        if _number(value):
            return str(value).encode("utf-8")
        raise SealedValueError(f"SealedText takes a str, not {type(value).__name__}")

    def _decode(self, data: bytes) -> str:
        return data.decode("utf-8")

    def _legacy(self, value: Any) -> Any:
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return value
        return str(value) if _number(value) else value


class SealedJSON(_Sealed):
    """Any JSON value. Python None is SQL NULL (not JSON null)."""

    cache_ok = True

    @property
    def python_type(self):
        return object

    def _encode(self, value: Any) -> bytes:
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SealedValueError(f"SealedJSON can't store this value ({exc.__class__.__name__})") from None

    def _decode(self, data: bytes) -> Any:
        return json.loads(data)

    def _legacy(self, value: Any) -> Any:
        if isinstance(value, (str, bytes)):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value


class SealedInt(_Sealed):
    cache_ok = True  # SQLAlchemy reads it from each class itself, not inherited

    @property
    def python_type(self):
        return int

    def _encode(self, value: Any) -> bytes:
        if isinstance(value, int):  # bool included, as int(True) == 1, like Integer
            return str(int(value)).encode("ascii")
        if isinstance(value, float) and value.is_integer():
            return str(int(value)).encode("ascii")
        raise SealedValueError(f"SealedInt takes an int, not {type(value).__name__}")

    def _decode(self, data: bytes) -> int:
        return int(data.decode("ascii"))

    def _legacy(self, value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if value.is_integer() else value
        if isinstance(value, (str, bytes)):
            text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
            with contextlib.suppress(ValueError):
                return int(text.strip())
            with contextlib.suppress(ValueError, OverflowError):
                number = float(text.strip())
                if number.is_integer():
                    return int(number)
        return value


class SealedFloat(_Sealed):
    cache_ok = True  # SQLAlchemy reads it from each class itself, not inherited

    @property
    def python_type(self):
        return float

    def _encode(self, value: Any) -> bytes:
        if _number(value):
            return repr(float(value)).encode("ascii")  # repr round-trips every float, inf and nan included
        raise SealedValueError(f"SealedFloat takes a float, not {type(value).__name__}")

    def _decode(self, data: bytes) -> float:
        return float(data.decode("ascii"))

    def _legacy(self, value: Any) -> Any:
        if _number(value):
            return float(value)
        if isinstance(value, (str, bytes)):
            text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
            with contextlib.suppress(ValueError):
                return float(text.strip())
        return value


class SealedBool(_Sealed):
    cache_ok = True  # SQLAlchemy reads it from each class itself, not inherited

    @property
    def python_type(self):
        return bool

    def _encode(self, value: Any) -> bytes:
        if isinstance(value, bool) or (isinstance(value, int) and value in (0, 1)):
            return b"1" if value else b"0"
        raise SealedValueError(f"SealedBool takes a bool, not {type(value).__name__}")

    def _decode(self, data: bytes) -> bool:
        if data not in (b"0", b"1"):
            raise Corrupt("A sealed bool holds something else")
        return data == b"1"

    def _legacy(self, value: Any) -> Any:
        if isinstance(value, (bool, int, float)):
            return bool(value)
        if isinstance(value, (str, bytes)):
            text = (value.decode("utf-8", "replace") if isinstance(value, bytes) else value).strip().lower()
            if text in ("1", "true", "t", "yes"):
                return True
            if text in ("0", "false", "f", "no", ""):
                return False
        return value


class SealedDateTime(_Sealed):
    """A ``datetime`` (the app keeps naive UTC), sealed as its ISO 8601 text."""

    cache_ok = True  # SQLAlchemy reads it from each class itself, not inherited

    @property
    def python_type(self):
        return datetime

    def _encode(self, value: Any) -> bytes:
        if isinstance(value, datetime):
            return value.isoformat().encode("ascii")
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day).isoformat().encode("ascii")
        raise SealedValueError(f"SealedDateTime takes a datetime, not {type(value).__name__}")

    def _decode(self, data: bytes) -> datetime:
        return datetime.fromisoformat(data.decode("ascii"))

    def _legacy(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value
        if isinstance(value, (str, bytes)):  # SQLAlchemy's SQLite format: "2026-10-07 13:42:46.025000"
            text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(text.strip())
        return value


@event.listens_for(Column, "after_parent_attach")
def _bind_column_label(column: Column, parent: Any) -> None:
    """Give each sealed column its AAD label, ``jh/<table>.<column>``, when it is attached to its table (so models
    don't repeat their own names). If one type object ends up on two columns (a Column copied from a mixin shares
    its type), the second gets a copy of its own: one type, one label."""
    sealed = column.type
    if not isinstance(sealed, _Sealed) or sealed.label is not None or not isinstance(parent, Table):
        return
    aad = b"jh/" + f"{parent.name}.{column.name}".encode("utf-8")
    if sealed._aad is None:
        sealed._aad = aad
    elif sealed._aad != aad:
        copy = sealed.copy()
        copy._aad = aad
        column.type = copy


def is_sealed_type(type_: Any) -> bool:
    """Whether a column type holds ciphertext: the sealed types, and the process-sealed http cache body."""
    return isinstance(type_, (_Sealed, ProcessSealedText))


def sealed_columns(table: Table) -> list[Column]:
    """The table's columns under a user's key (the ``Sealed*`` types), in table order."""
    return [c for c in table.columns if isinstance(c.type, _Sealed)]


# ---------------------------------------------------------------------------
# The process key: random, and only ever in this process's memory, for the shared http cache (design 2.2).
# ---------------------------------------------------------------------------

_PROCESS_KEY = new_key()
_PROCESS_AEAD = AESGCM(_PROCESS_KEY)
_PROCESS_KID = key_id(_PROCESS_KEY)
_PROCESS_INDEX_KEY = hkdf(_PROCESS_KEY, b"jh process index v1")


def process_index(purpose: str, value: str) -> str:
    """``HMAC(process key, purpose \\0 value)`` as hex: a lookup key that means nothing after a restart."""
    msg = purpose.encode("utf-8") + b"\0" + value.encode("utf-8")
    return hmac.new(_PROCESS_INDEX_KEY, msg, hashlib.sha256).hexdigest()


class ProcessSealedText(TypeDecorator):
    """Text sealed with the process key (no user scope needed). A row from an earlier process, or plaintext, raises
    ``KeyMismatch``: callers treat that as a cache miss."""

    impl = LargeBinary
    cache_ok = True
    comparator_factory = _SealedComparator
    always = False

    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self._aad = b"jh-process|" + label.encode("utf-8")

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[bytes]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise SealedValueError(f"ProcessSealedText takes a str, not {type(value).__name__}")
        return _seal_with(_PROCESS_AEAD, _PROCESS_KID, value.encode("utf-8"), self._aad)

    def process_result_value(self, value: Any, dialect: Any) -> Optional[str]:
        if value is None:
            return None
        if not is_sealed(value):
            raise KeyMismatch("Not sealed with this process's key")
        return _open_with(_PROCESS_AEAD, _PROCESS_KID, value, self._aad).decode("utf-8")


# ---------------------------------------------------------------------------
# No SQL over sealed columns (accounts design §15). The comparator can't see ORDER BY, GROUP BY, DISTINCT or a
# function call, so statements are checked when they are compiled. SQLAlchemy caches compiled statements by their
# shape, so this runs once per shape, not once per query.
# ---------------------------------------------------------------------------

def _sealed_in(clause: Any) -> Optional[str]:
    """The first sealed column in ``clause`` (as ``table.column``), or None."""
    for element in visitors.iterate(clause):
        if isinstance(element, ColumnClause) and is_sealed_type(element.type):
            return f"{getattr(element.table, 'name', '?')}.{element.name}"
    return None


def check_statement(statement: Any) -> None:
    """``SealedSqlError`` if ``statement`` sorts, groups, de-duplicates or calls an SQL function over a sealed
    column."""
    if isinstance(statement, Select):
        for what, clauses in (("ORDER BY", statement._order_by_clauses),
                              ("GROUP BY", statement._group_by_clauses)):
            for clause in clauses:
                name = _sealed_in(clause)
                if name:
                    raise SealedSqlError(f"{what} {name}: the column is encrypted. Sort or group in Python.")
        if statement._distinct or statement._distinct_on:
            for column in statement.selected_columns:
                name = _sealed_in(column)
                if name:
                    raise SealedSqlError(f"DISTINCT over {name}: the column is encrypted. De-duplicate in Python.")
    for element in visitors.iterate(statement):
        if isinstance(element, FunctionElement):
            name = _sealed_in(element.clauses)
            if name:
                raise SealedSqlError(f"SQL function {getattr(element, 'name', '?')}() over {name}: the column is "
                                     "encrypted. Compute it in Python.")


@compiles(Select)
def _refuse_sql_on_sealed(select: Select, compiler: Any, **kw: Any) -> str:
    check_statement(select)
    return compiler.visit_select(select, **kw)


@compiles(Update)
def _refuse_update_over_sealed(statement: Update, compiler: Any, **kw: Any) -> str:
    check_statement(statement)
    return compiler.visit_update(statement, **kw)


@compiles(Delete)
def _refuse_delete_over_sealed(statement: Delete, compiler: Any, **kw: Any) -> str:
    check_statement(statement)
    return compiler.visit_delete(statement, **kw)
