"""The server's own keys, in ``data/server-keys.json`` (``JOBHUNT_KEY_FILE`` points elsewhere, mainly for tests).
Design: docs/design/accounts.md, section 1.3.

    {"version": 1,
     "kek":     {"current": "k1", "keys": {"k1": "<base64>"}},
     "pepper":  {"current": "p1", "keys": {"p1": "<base64>"}},
     "signing": {"current": "s1", "keys": {"s1": "<base64>"}, "retired": {"s0": <unix time>}},
     "created_at": "2026-10-05T00:00:00Z",
     "encryption_done_at": "2026-10-05T00:00:00Z"}     (optional: see below)

* **kek** wraps what the server must open with nobody signed in: the DEKs of users who turned on background access
  (``srv|{uid}``). The wrap's label always ends in ``|{kek_id}``, and the envelope's
  kid names the KEK, so the file can hold several KEKs while a rotation is under way.
* **pepper** keys ``email_hash = HMAC-SHA256(pepper, normalised sign-in name)``, so an account is found by its name
  without decrypting anything (on an older install the name is the email address the account was made with; the
  column keeps its old name). It isn't a decryption key: leaking it only lets someone confirm guessed names.
* **signing** signs tokens such as the lockout's device cookie. A retired key keeps verifying for 30 days.
* **encryption_done_at** is written once, when the one-time encryption of the data from before accounts has finished
  (backend/migrate_encryption.py), and never removed. It turns strict mode on at every start whatever the database
  says: someone who can only write the database can't turn it off by editing the migration's state there, which would
  make the next run seal planted plaintext as if it were real.

The file is owner-only (0600) and only ever replaced atomically (crypto.write_private). It lives in ``data/``, so the
uninstaller's backup includes it and the Windows setup keeps it on update. It is created only while the database has
no users: once there are users, a missing file or one whose fingerprint (``HMAC(pepper, "fp")[:8]``, kept in
``app_settings["server_keys_fingerprint"]``) doesn't match stops start-up, because a new file would silently break
signing in and orphan every background-access key. (The reset tool, backend/reset.py, deletes the accounts and the
file together, with the app stopped.)

**Rotating the KEK** (crash-safe; the old KEK stays in the file until nothing uses it):
  1. ``start_kek_rotation``: write the file with a new KEK as current and the old ones kept. From here on, new wraps
     use the new KEK and old wraps still open, because each blob's kid says which KEK made it.
  2. ``finish_kek_rotation``: re-wrap every value not under the current KEK (the caller stores them, in one
     transaction), check that none is left, then write the file with only the current KEK.
A crash anywhere leaves a file with every KEK still in use; at start-up, ``kek_rotation_pending`` says to run step 2
again. (The design sets the new KEK current after step 2; setting it in step 1 is as safe and means a wrap made
after step 1 is under the new key.) A wrap made *before* step 1 but stored after step 2 would be left under a dropped
key, so whoever wraps holds ``wrapping()`` from the wrap until the row is committed: a rotation waits for it.

**One process at a time.** The app takes an exclusive lock on ``server-keys.json.lock`` (``hold_key_file``) when it
starts and keeps it until it stops; the commands below take it too and refuse while someone else holds it. (The
launcher's ``running.json`` only knows about apps it started itself, so it can't be the check.) A command that
replaced the key file under a running app would leave it wrapping with the retired key. Besides that, ``kek_wrap``
refuses when the file on disk is no longer the one this process loaded.

Command line (run with the app stopped; the commands refuse while it is running)::

    python -m backend.keys bench-kdf [--profile default|low|all] [--runs N]
    python -m backend.keys rotate-kek                 replace the server key (backend/key_rotation.py)
    python -m backend.keys recover-lost --name X      (key file lost: backend/key_recovery.py; --name once per account)
"""
from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import statistics
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Union

from backend import config, crypto

log = logging.getLogger(__name__)

KEY_FILE_ENV = "JOBHUNT_KEY_FILE"
FILE_VERSION = 1
SIGNING_GRACE_S = 30 * 24 * 3600  # a retired signing key still verifies for 30 days (links in old emails)
MAC_CHARS = 22                     # 132 bits of HMAC-SHA256, base64url
RESTORE_MESSAGE = "Restore data/server-keys.json from your backup (it belongs with this database)."

_SECTIONS = {"kek": "k", "pepper": "p", "signing": "s"}  # section -> key id prefix
_PURPOSE_RE = re.compile(r"[a-z0-9_]{1,32}")
_PAYLOAD_RE = re.compile(r"[A-Za-z0-9_-]{1,512}")
_EXPIRY_RE = re.compile(r"[0-9]{1,12}")


def key_file_path() -> Path:
    """Where the key file is: JOBHUNT_KEY_FILE (relative to the app folder unless absolute), else
    data/server-keys.json in the app folder."""
    override = os.environ.get(KEY_FILE_ENV, "").strip()
    if not override:
        return config.PROJECT_ROOT / "data" / "server-keys.json"
    path = Path(override).expanduser()
    return path if path.is_absolute() else config.PROJECT_ROOT / path


class KeyFileError(RuntimeError):
    """The key file can't be used; start-up must stop. The message says what to do."""


class KeyFileMissing(KeyFileError):
    pass


class KeyFileMismatch(KeyFileError):
    pass


class KeyFileInvalid(KeyFileError):
    pass


class RotationIncomplete(KeyFileError):
    """Some wrapped values couldn't be moved to the new KEK; the old KEKs are kept, so nothing was lost."""

    def __init__(self, message: str, refs: list):
        super().__init__(message)
        self.refs = refs


def normalise_email(email: str) -> str:
    """``NFKC(email).strip().lower()``: the form every email hash is computed from."""
    return unicodedata.normalize("NFKC", email).strip().lower()


@dataclass(frozen=True)
class KekWrapped:
    """One KEK-wrapped value, as a rotation sees it."""
    ref: Any      # whatever the caller needs to write the new blob back, for example ("user_keys", user_id)
    label: str    # the label it was wrapped with: "srv|{uid}" (the "|{kek_id}" is added here)
    blob: bytes


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_id(prefix: str, ids: Iterable[str]) -> str:
    return f"{prefix}{max((int(i[len(prefix):]) for i in ids), default=0) + 1}"


class ServerKeys:
    """The parsed key file. Reads are lock-free (each change swaps in a whole new state); changes are written to
    disk first and adopted only once the write succeeded, so memory never runs ahead of the file."""

    def __init__(self, path: Path, doc: Any):
        self.path = Path(path)
        self.created_now = False  # True when load_or_create just made the file: store its fingerprint
        self._lock = threading.RLock()
        self._sections, self._retired, self.created_at = _parse(doc, self.path)
        done = doc.get("encryption_done_at") if isinstance(doc, dict) else None
        self.encryption_done_at: Optional[str] = done if isinstance(done, str) and done else None

    def __repr__(self) -> str:  # never the keys
        return f"<keys.ServerKeys {self.path.name}>"

    # -- the file -------------------------------------------------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Union[str, Path]] = None) -> "ServerKeys":
        path = Path(path) if path else key_file_path()
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise KeyFileMissing(f"{RESTORE_MESSAGE} (Not found: {path})") from None
        try:
            doc = json.loads(raw)
        except ValueError:
            raise KeyFileInvalid(f"{path} is damaged (not JSON). {RESTORE_MESSAGE}") from None
        keys = cls(path, doc)
        keys._tighten_permissions()
        return keys

    @classmethod
    def create(cls, path: Optional[Union[str, Path]] = None) -> "ServerKeys":
        """A new key file with fresh keys. Never replaces an existing file (FileExistsError)."""
        path = Path(path) if path else key_file_path()
        doc = {"version": FILE_VERSION,
               "kek": {"current": "k1", "keys": {"k1": _b64(crypto.new_key())}},
               "pepper": {"current": "p1", "keys": {"p1": _b64(crypto.new_key())}},
               "signing": {"current": "s1", "keys": {"s1": _b64(crypto.new_key())}, "retired": {}},
               "created_at": _now_iso()}
        keys = cls(path, doc)
        crypto.write_private(path, keys._dump(keys._sections, keys._retired), exclusive=True)
        keys.created_now = True
        log.info("Created a new server key file, %s", path.name)
        return keys

    def _tighten_permissions(self) -> None:
        """A file restored from a zip can come back readable by everyone: make it owner-only again (POSIX)."""
        if os.name != "posix":
            return
        try:
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
            if mode & 0o077:
                os.chmod(self.path, 0o600)
                log.warning("%s was readable by other accounts on this computer; made it owner-only",
                            self.path.name)
        except OSError as exc:
            log.warning("Could not check the permissions of %s: %s", self.path.name, exc.__class__.__name__)

    def _dump(self, sections: dict, retired: dict) -> bytes:
        doc: dict[str, Any] = {"version": FILE_VERSION}
        for name in _SECTIONS:
            sec = sections[name]
            doc[name] = {"current": sec["current"], "keys": {k: _b64(v) for k, v in sec["keys"].items()}}
        doc["signing"]["retired"] = dict(retired)
        doc["created_at"] = self.created_at
        if self.encryption_done_at:
            doc["encryption_done_at"] = self.encryption_done_at
        return (json.dumps(doc, indent=2) + "\n").encode("utf-8")

    def _check_unchanged(self) -> None:
        """Refuse when the file on disk isn't what this process loaded: another process (``python -m backend.keys
        rotate-kek``) replaced it, and writing from this stale copy would drop its keys and orphan everything wrapped
        under them. Nothing is written or changed."""
        stale = KeyFileError("The key file was changed by something else since JobHuntPA started, so it wasn't "
                             "touched. Restart JobHuntPA and try again.")
        try:
            doc = json.loads(self.path.read_bytes())
            sections, retired, _created = _parse(doc, self.path)
        except (OSError, ValueError, KeyFileError):
            raise stale from None
        if not hmac.compare_digest(self._dump(sections, retired), self._dump(self._sections, self._retired)):
            raise stale

    def _commit(self, sections: dict, retired: dict) -> None:
        self._check_unchanged()
        crypto.write_private(self.path, self._dump(sections, retired))
        self._sections, self._retired = sections, retired

    def mark_encryption_done(self) -> bool:
        """Records in the file that the one-time encryption has finished (it is never taken back). True when this call
        wrote it. Refuses (KeyFileError) when the file isn't what this process loaded."""
        with self._lock:
            if self.encryption_done_at:
                return False
            self._check_unchanged()
            self.encryption_done_at = _now_iso()
            try:
                crypto.write_private(self.path, self._dump(self._sections, self._retired))
            except BaseException:
                self.encryption_done_at = None
                raise
            return True

    def _current(self, name: str) -> tuple[str, bytes]:
        sec = self._sections[name]
        return sec["current"], sec["keys"][sec["current"]]

    # -- fingerprint ----------------------------------------------------------------------------------------------

    @staticmethod
    def _fingerprint_of(pepper: bytes) -> str:
        return hmac.new(pepper, b"fp", hashlib.sha256).digest()[:8].hex()

    def fingerprint(self) -> str:
        """``HMAC(pepper, "fp")[:8]`` (hex) of the current pepper: store it in app_settings. It changes when the pepper
        rotates; store it again then."""
        return self._fingerprint_of(self._current("pepper")[1])

    def matches_fingerprint(self, stored: Optional[str]) -> bool:
        """Whether ``stored`` was made from this file (any of its peppers, so a crash between rotating the pepper and
        storing the new fingerprint can't lock the app out)."""
        if not isinstance(stored, str):
            return False
        return any(hmac.compare_digest(self._fingerprint_of(p).encode(), stored.encode("utf-8", "replace"))
                   for p in self._sections["pepper"]["keys"].values())

    # -- KEK ------------------------------------------------------------------------------------------------------

    @property
    def current_kek_id(self) -> str:
        return self._sections["kek"]["current"]

    @property
    def kek_ids(self) -> list[str]:
        return sorted(self._sections["kek"]["keys"])

    @property
    def kek_rotation_pending(self) -> bool:
        """More than one KEK in the file: a rotation was started and not finished. Run finish_kek_rotation."""
        return len(self._sections["kek"]["keys"]) > 1

    @contextmanager
    def wrapping(self) -> Iterator[None]:
        """Hold this from a ``kek_wrap`` until the row it goes into is committed. A rotation takes the same lock, so
        it can't finish (and drop the KEK) between the wrap and the commit and leave the stored blob under a key
        that is gone. Take it before touching the database, as a rotation does, and keep what's inside short."""
        with self._lock:
            yield

    def kek_wrap(self, secret: bytes, label: str) -> tuple[str, bytes]:
        """``secret`` wrapped under the current KEK, labelled ``{label}|{kek_id}``: ``(kek_id, blob)``. Store the
        kek_id beside the blob where there is a column for it (``user_keys.server_kek_id``). KeyFileError when the file
        was changed by another process since this one loaded it (the KEK here may be one it has retired: a value
        wrapped under it would not open after the next restart)."""
        self._check_unchanged()
        return self._wrap_unchecked(secret, label)

    def _wrap_unchecked(self, secret: bytes, label: str) -> tuple[str, bytes]:
        kek_id, kek = self._current("kek")
        return kek_id, crypto.wrap(kek, secret, f"{label}|{kek_id}")

    def kek_id_of(self, blob: bytes) -> Optional[str]:
        """Which of this file's KEKs wrapped ``blob`` (from its kid), or None."""
        kid = crypto.envelope_kid(blob)
        if kid is None:
            return None
        for kek_id, kek in self._sections["kek"]["keys"].items():
            if hmac.compare_digest(crypto.key_id(kek), kid):
                return kek_id
        return None

    def kek_unwrap(self, blob: bytes, label: str) -> bytes:
        """Opens a ``kek_wrap`` blob with whichever KEK in the file made it. KeyMismatch if none did."""
        kek_id = self.kek_id_of(blob)
        if kek_id is None:
            raise crypto.KeyMismatch("Wrapped under a server key that isn't in the key file")
        return crypto.unwrap(self._sections["kek"]["keys"][kek_id], blob, f"{label}|{kek_id}")

    def start_kek_rotation(self) -> str:
        """Step 1: a new KEK becomes current; the old ones stay to open what they wrapped. Returns the new id. If a
        rotation is already under way, nothing changes: finish that one first."""
        with self._lock:
            if self.kek_rotation_pending:
                return self.current_kek_id
            sections = copy.deepcopy(self._sections)
            sec = sections["kek"]
            kids = {crypto.key_id(k) for k in sec["keys"].values()}
            new = crypto.new_key()
            while crypto.key_id(new) in kids:  # a 4-byte kid could collide (1 in 4 billion): kids must be unique
                new = crypto.new_key()
            new_id = _next_id("k", sec["keys"])
            sec["keys"][new_id] = new
            sec["current"] = new_id
            self._commit(sections, self._retired)
            log.info("Server key rotation started: %s is the current key", new_id)
            return new_id

    def finish_kek_rotation(self, rows: Callable[[], Iterable[KekWrapped]],
                            store: Callable[[list[tuple[Any, str, bytes]]], None]) -> int:
        """Step 2: re-wrap every value from ``rows()`` that isn't under the current KEK, hand them to ``store`` as
        ``[(ref, kek_id, blob)]`` (write them in one transaction), check with a second ``rows()`` call that none is
        left, then drop the old KEKs from the file. Returns how many were re-wrapped. Does nothing when no rotation
        is pending. Raises RotationIncomplete (keeping the old KEKs) if a value can't be opened or is still left."""
        with self._lock:
            if not self.kek_rotation_pending:
                return 0
            self._check_unchanged()  # before anything is re-wrapped in the database
            current = self.current_kek_id
            todo: list[tuple[Any, str, bytes]] = []
            failed: list[Any] = []
            for row in rows():
                if row.blob is None or self.kek_id_of(row.blob) == current:
                    continue
                try:
                    secret = self.kek_unwrap(row.blob, row.label)
                except crypto.CryptoError:
                    failed.append(row.ref)
                    continue
                todo.append((row.ref, *self._wrap_unchecked(secret, row.label)))
            if todo:
                store(todo)
            if failed:
                raise RotationIncomplete(f"{len(failed)} value(s) wrapped with the server key couldn't be opened; the "
                                         "old keys stay in the key file.", failed)
            left = [row.ref for row in rows() if row.blob is not None and self.kek_id_of(row.blob) != current]
            if left:
                raise RotationIncomplete(f"{len(left)} value(s) are still under an old server key; the old keys stay "
                                         "in the key file.", left)
            sections = copy.deepcopy(self._sections)
            sections["kek"]["keys"] = {current: sections["kek"]["keys"][current]}
            self._commit(sections, self._retired)
            log.info("Server key rotation finished: %d value(s) re-wrapped", len(todo))
            return len(todo)

    def rotate_kek(self, rows: Callable[[], Iterable[KekWrapped]],
                   store: Callable[[list[tuple[Any, str, bytes]]], None]) -> int:
        """Both steps (see the module docstring)."""
        with self._lock:
            self.start_kek_rotation()
            return self.finish_kek_rotation(rows, store)

    # -- pepper ---------------------------------------------------------------------------------------------------

    def email_hash(self, email: str) -> bytes:
        """``users.email_hash``: HMAC-SHA256(current pepper, normalised email), 32 bytes."""
        return hmac.new(self._current("pepper")[1], normalise_email(email).encode("utf-8"), hashlib.sha256).digest()

    def email_hash_candidates(self, email: str) -> list[bytes]:
        """The email's hash under the current pepper first, then under each previous one (newest first): look an
        account up by each in turn, and rewrite a match found under an old pepper."""
        sec = self._sections["pepper"]
        ids = [sec["current"]] + sorted((i for i in sec["keys"] if i != sec["current"]),
                                        key=lambda i: int(i[1:]), reverse=True)
        data = normalise_email(email).encode("utf-8")
        return [hmac.new(sec["keys"][i], data, hashlib.sha256).digest() for i in ids]

    def rotate_pepper(self) -> str:
        """A new pepper becomes current; the old ones stay for lookups until drop_old_peppers (after every hash has
        been recomputed). The fingerprint changes: store the new one."""
        with self._lock:
            sections = copy.deepcopy(self._sections)
            sec = sections["pepper"]
            new_id = _next_id("p", sec["keys"])
            sec["keys"][new_id] = crypto.new_key()
            sec["current"] = new_id
            self._commit(sections, self._retired)
            return new_id

    def drop_old_peppers(self) -> None:
        with self._lock:
            sections = copy.deepcopy(self._sections)
            sec = sections["pepper"]
            sec["keys"] = {sec["current"]: sec["keys"][sec["current"]]}
            self._commit(sections, self._retired)

    # -- signed tokens --------------------------------------------------------------------------------------------

    @staticmethod
    def _mac(key: bytes, purpose: str, parts: list[str]) -> str:
        # "<purpose>|<payload>", or "<purpose>|<payload>|<expiry>" when it expires.
        msg = "|".join([purpose, *parts]).encode("ascii")
        return crypto.b64e(hmac.new(key, msg, hashlib.sha256).digest())[:MAC_CHARS]

    def _live_signing_keys(self, now: float) -> list[bytes]:
        sec = self._sections["signing"]
        live = [sec["keys"][sec["current"]]]
        for key_id, retired_at in self._retired.items():
            if key_id in sec["keys"] and key_id != sec["current"] and now < retired_at + SIGNING_GRACE_S:
                live.append(sec["keys"][key_id])
        return live

    def sign(self, purpose: str, payload: str, *, ttl: Optional[float] = None, now: Optional[float] = None) -> str:
        """A token carrying ``payload`` (letters, digits, - and _ only): ``payload.mac``, or ``payload.expiry.mac``
        with a ``ttl`` in seconds. ``purpose`` (for example "device") is part of the MAC, so a token made for one
        purpose is refused for another."""
        if not _PURPOSE_RE.fullmatch(purpose):
            raise ValueError("A token purpose is 1-32 lowercase letters, digits or _")
        if not isinstance(payload, str) or not _PAYLOAD_RE.fullmatch(payload):
            raise ValueError("A token payload is 1-512 letters, digits, - or _")
        parts = [payload]
        if ttl is not None:
            if ttl <= 0:
                raise ValueError("ttl must be positive")
            parts.append(str(int((time.time() if now is None else now) + ttl)))
        return ".".join([*parts, self._mac(self._current("signing")[1], purpose, parts)])

    def verify(self, purpose: str, token: Any, *, now: Optional[float] = None) -> Optional[str]:
        """The payload of a token made by ``sign`` for ``purpose``, signed by the current key or one retired less
        than 30 days ago and not expired; else None. MACs are compared in constant time."""
        if not _PURPOSE_RE.fullmatch(purpose):
            raise ValueError("A token purpose is 1-32 lowercase letters, digits or _")
        if not isinstance(token, str) or len(token) > 600:
            return None
        parts = token.split(".")
        if len(parts) not in (2, 3) or not _PAYLOAD_RE.fullmatch(parts[0]) or len(parts[-1]) != MAC_CHARS:
            return None
        if len(parts) == 3 and not _EXPIRY_RE.fullmatch(parts[1]):
            return None
        now = time.time() if now is None else now
        given = parts[-1].encode("utf-8", "replace")
        if not any(hmac.compare_digest(self._mac(key, purpose, parts[:-1]).encode("ascii"), given)
                   for key in self._live_signing_keys(now)):
            return None
        if len(parts) == 3 and now >= int(parts[1]):
            return None
        return parts[0]

    def rotate_signing(self, *, now: Optional[float] = None) -> str:
        """A new signing key becomes current. The old one keeps verifying for 30 days; keys retired longer ago than
        that are deleted from the file."""
        now = time.time() if now is None else now
        with self._lock:
            sections = copy.deepcopy(self._sections)
            sec = sections["signing"]
            retired = dict(self._retired)
            retired[sec["current"]] = int(now)
            new_id = _next_id("s", sec["keys"])
            sec["keys"][new_id] = crypto.new_key()
            sec["current"] = new_id
            for key_id, retired_at in list(retired.items()):
                if now >= retired_at + SIGNING_GRACE_S:
                    retired.pop(key_id)
                    sec["keys"].pop(key_id, None)
            self._commit(sections, retired)
            return new_id


_held_locks: dict[str, Any] = {}  # lock file -> the open file holding the lock (kept open until the process ends)


def lock_file_path(path: Optional[Union[str, Path]] = None) -> Path:
    path = Path(path) if path else key_file_path()
    return path.with_name(path.name + ".lock")


def _try_lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def hold_key_file(path: Optional[Union[str, Path]] = None) -> None:
    """Takes the exclusive lock on the key file for the rest of this process (the app at start-up, a key command
    before it changes anything). KeyFileError when another process holds it: the app, or another key command. The
    operating system lets go of it when the process ends, however it ends; asking again in a process that holds it
    is fine."""
    lock = lock_file_path(path)
    name = str(lock)
    if name in _held_locks:
        return
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "r+b")
    if not _try_lock(handle):
        handle.close()
        raise KeyFileError("JobHuntPA, or one of its commands (rotate-kek, recover-lost, the reset tool), is using the "
                           "key file. Stop it first, then try again.")
    try:
        handle.seek(0)
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.truncate()
        handle.flush()
    except OSError:
        pass  # (the pid is only a hint for people)
    _held_locks[name] = handle


def encryption_marked(path: Optional[Union[str, Path]] = None) -> bool:
    """Whether the key file says the one-time encryption has finished. Reads the file itself (a start-up step that
    needs nothing else loaded); False when the file is missing or damaged."""
    try:
        doc = json.loads(Path(path or key_file_path()).read_bytes())
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and bool(doc.get("encryption_done_at"))


def _b64(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def _parse(doc: Any, path: Path) -> tuple[dict, dict, Optional[str]]:
    """The file's sections with keys decoded, its retired signing keys and created_at; KeyFileInvalid if anything
    is off. Nothing is repaired: a damaged key file is never rewritten."""
    def bad(why: str) -> KeyFileInvalid:
        return KeyFileInvalid(f"{path} is damaged ({why}). {RESTORE_MESSAGE}")

    if not isinstance(doc, dict) or doc.get("version") != FILE_VERSION:
        raise bad("unknown version")
    sections: dict[str, dict] = {}
    for name, prefix in _SECTIONS.items():
        sec = doc.get(name)
        if not isinstance(sec, dict) or not isinstance(sec.get("keys"), dict) or not sec["keys"]:
            raise bad(f"no {name} keys")
        keys: dict[str, bytes] = {}
        for key_id, text in sec["keys"].items():
            if not re.fullmatch(prefix + r"[1-9][0-9]{0,8}", key_id) or not isinstance(text, str):
                raise bad(f"bad {name} key id")
            try:
                raw = base64.b64decode(text, validate=True)
            except (binascii.Error, ValueError):
                raise bad(f"bad {name} key") from None
            if len(raw) != crypto.KEY_LEN:
                raise bad(f"bad {name} key length")
            keys[key_id] = raw
        if sec.get("current") not in keys:
            raise bad(f"no current {name} key")
        sections[name] = {"current": sec["current"], "keys": keys}
    kids = [crypto.key_id(k) for k in sections["kek"]["keys"].values()]
    if len(set(kids)) != len(kids):
        raise bad("two server keys share a kid")
    retired_doc = doc["signing"].get("retired", {})
    if not isinstance(retired_doc, dict):
        raise bad("bad retired signing keys")
    retired: dict[str, int] = {}
    for key_id, at in retired_doc.items():
        if key_id not in sections["signing"]["keys"]:
            continue  # its key is gone already
        if type(at) not in (int, float):
            raise bad("bad retired signing key time")
        retired[key_id] = int(at)
    created_at = doc.get("created_at")
    return sections, retired, created_at if isinstance(created_at, str) else None


def load_or_create(has_users: Union[bool, Callable[[], bool]], stored_fingerprint: Optional[str] = None,
                   path: Optional[Union[str, Path]] = None) -> ServerKeys:
    """The key file for start-up. ``has_users``: whether the database has any users (a bool, or a callable asked
    once). ``stored_fingerprint``: ``app_settings["server_keys_fingerprint"]``, or None if none is stored.

    * No file and no users: a new file is created (``created_now`` is True; store its fingerprint).
    * No file but users: KeyFileMissing. Never a new file, which would break email lookup and background access.
    * Users, and the stored fingerprint is missing or doesn't match the file: KeyFileMismatch.
    The exceptions' messages say to restore the file from the backup. After a match, store ``fingerprint()`` again
    (it changes when the pepper rotates)."""
    path = Path(path) if path else key_file_path()
    users = has_users() if callable(has_users) else bool(has_users)
    if not path.exists():
        if users:
            raise KeyFileMissing(f"{RESTORE_MESSAGE} (Not found: {path})")
        try:
            return ServerKeys.create(path)
        except FileExistsError:
            pass  # created meanwhile by another process: use that one
    keys = ServerKeys.load(path)
    if users and not keys.matches_fingerprint(stored_fingerprint):
        raise KeyFileMismatch(f"{RESTORE_MESSAGE} ({path} doesn't match this database.)")
    return keys


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def bench_kdf(profiles: Iterable[str], runs: int = 3, out=None) -> dict[str, float]:
    """Median seconds per password derivation for each profile, on this machine; also printed to ``out``."""
    out = out or sys.stdout
    salt = crypto.new_salt()
    results: dict[str, float] = {}
    print(f"Password key derivation (argon2id) on this machine, median of {runs} run(s); sign-in waits about "
          "this long:", file=out)
    for name in profiles:
        params = crypto.KDF_PROFILES[name]
        times = []
        for _ in range(runs):
            start = time.perf_counter()
            crypto.derive_password("benchmark password", salt, params)
            times.append(time.perf_counter() - start)
        results[name] = statistics.median(times)
        print(f"  {name:8} {params.describe():24} {results[name]:.2f} s", file=out)
    chosen = (os.environ.get("JOBHUNT_KDF_PROFILE") or "default").strip().lower()
    print(f"JOBHUNT_KDF_PROFILE is {chosen!r}.", file=out)
    if results.get("default", 0) > 1.0:
        print("The default profile takes over a second here. On a slow device (Raspberry Pi 3 or Zero 2 W), put "
              "JOBHUNT_KDF_PROFILE=low in .env; it applies to each account at its next sign-in.", file=out)
    elif "default" in results:
        print("The default profile is fine on this machine.", file=out)
    return results


def main(argv: Optional[list[str]] = None, *, ask_password: Optional[Callable[[str], str]] = None) -> int:
    """``ask_password`` replaces getpass for recover-lost (tests)."""
    parser = argparse.ArgumentParser(prog="python -m backend.keys",
                                     description="JobHuntPA's server keys (data/server-keys.json). Run with the app "
                                                 "stopped.")
    sub = parser.add_subparsers(dest="command", required=True)
    bench = sub.add_parser("bench-kdf", help="time the password key derivation (argon2id) profiles on this machine")
    bench.add_argument("--profile", choices=["default", "low", "all"], default="all")
    bench.add_argument("--runs", type=int, default=3)
    sub.add_parser("rotate-kek", help="replace the server key and re-wrap everything that uses it")
    recover = sub.add_parser("recover-lost", help="make a new key file after losing data/server-keys.json (asks for "
                                                  "the account's password)")
    # (--admin-email is the older spelling: an older install's sign-in names are email addresses)
    recover.add_argument("--name", "--admin-email", dest="names", action="append", required=True,
                         help="the account's sign-in name; give it once for each account to restore")
    args = parser.parse_args(argv)
    if args.command == "bench-kdf":
        if args.runs < 1:
            parser.error("--runs must be at least 1")
        bench_kdf(["default", "low"] if args.profile == "all" else [args.profile], args.runs)
        return 0
    if args.command == "rotate-kek":
        from backend import key_rotation

        return key_rotation.rotate_kek_command()
    if args.command == "recover-lost":
        from backend import key_recovery

        return key_recovery.run(args.names, ask_password=ask_password)
    parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
