"""Signed-in test clients and accounts (accounts design, section 5.7).

tests/__init__.py sets the environment first: throwaway database, .env and key file, ``JOBHUNT_TESTING=1`` with
the quick ``test`` password key derivation, and the first admin (ADMIN_EMAIL / ADMIN_PASSWORD), whom the app creates
from the environment when it starts.

    from tests import helpers

    with helpers.client() as c:              # the admin, signed in; POST/PUT/PATCH/DELETE carry the CSRF token
        c.get("/api/jobs")
    bob = helpers.make_user()                # an ordinary account with its own workspace (bob.workspace_id)
    with helpers.client(bob.email) as c:     # signed in as bob
        ...
    with helpers.as_admin():                 # the admin's key scope, to write rows directly
        ...
    helpers.use_admin_scope(self)            # ... or for the rest of a test (setUp or the test itself)

Columns are sealed under their owner's key (backend/crypto.py, docs/design/accounts.md §2.2): reading or writing
them outside a key scope raises ``crypto.Locked``. The encryption migration ran when the admin was created, so
strict mode is on: plaintext planted in a sealed column with raw SQL is refused when read.

A client also works without ``with``: call ``c.__enter__()`` and ``addCleanup(c.__exit__, None, None, None)``.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

ADMIN_EMAIL = os.environ.setdefault("JOBHUNT_ADMIN_EMAIL", "admin@example.invalid")
ADMIN_PASSWORD = os.environ.setdefault("JOBHUNT_ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("JOBHUNT_TESTING", "1")
os.environ.setdefault("JOBHUNT_KDF_PROFILE", "test")
USER_PASSWORD = "user-password-123"

from fastapi.testclient import TestClient  # noqa: E402

from backend import crypto, ratelimit, users  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import SessionLocal, Workspace, init_db  # noqa: E402

_passwords = {ADMIN_EMAIL: ADMIN_PASSWORD}
_numbers = itertools.count(1)


def started() -> None:
    """What the app's start does for accounts (the database, the key file, the first admin), if it hasn't run yet
    in this process. TestClient's ``with`` runs the whole start; this is for tests that need accounts without it."""
    init_db()
    if not users.users_exist() or users._server_keys is None:
        users.startup()


class SignedInClient(TestClient):
    """A TestClient that signs in when it starts (``with`` or ``__enter__``) and sends the CSRF token after."""

    def __init__(self, email: str, password: str, *, remember: bool = False, **kwargs):
        super().__init__(app, **kwargs)
        self.email, self.password, self.remember = email, password, remember

    def __enter__(self) -> "SignedInClient":
        super().__enter__()
        self.sign_in()
        return self

    def sign_in(self) -> dict:
        ratelimit.AUTH.reset()  # every test signs in from the same address
        r = self.post("/api/auth/login", json={"email": self.email, "password": self.password,
                                                "remember": self.remember})
        if r.status_code != 200:
            raise AssertionError(f"signing in as {self.email} failed: {r.status_code} {r.text}")
        self.headers["X-CSRF-Token"] = r.json()["csrf"]
        return r.json()


def client(email: str = ADMIN_EMAIL, password: Optional[str] = None, **kwargs) -> SignedInClient:
    """A client signed in as ``email`` (the admin by default) once started."""
    return SignedInClient(email, password or _passwords[email], **kwargs)


def show_codes(c, password: Optional[str] = None, **fields):
    """The answer of ``POST /api/auth/recovery-codes/show`` for ``c`` (the unsaved recovery codes need the password
    again, and a code with two-step sign-in on)."""
    return c.post("/api/auth/recovery-codes/show", json={"password": password or getattr(c, "password", USER_PASSWORD),
                                                         **fields})


def lock_out(account_id: int, source: str = "testclient", failures: int = 5) -> None:
    """Five wrong passwords in a row from ``source`` (the test client's network address by default; a signed-in
    client is a browser the account knows: ``d:<its id>``): that source now waits."""
    for _ in range(failures):
        try:
            ratelimit.guesses.begin(account_id, source, ceiling=False)
        except ratelimit.Waiting:
            pass


def wrong_tries(account_id: int) -> int:
    """Wrong tries in a row, all sources together."""
    return sum(ratelimit.guesses.failures(account_id).values())


@dataclass(frozen=True)
class Account:
    id: int
    email: str
    password: str
    workspace_id: int


def make_user(email: Optional[str] = None, password: str = USER_PASSWORD, *, role: str = "user",
              name: str = "") -> Account:
    """A new account with a "My job search" workspace of its own."""
    started()
    email = email or f"user{next(_numbers)}-{os.getpid()}@example.invalid"
    account = users.create_user(email, password, name=name, role=role, created_via="open")
    _passwords[email] = password
    _keys[email] = (account.user_id, account.dek)
    _iks[email] = account.ik
    db = SessionLocal()
    try:
        ws = db.query(Workspace.id).filter(Workspace.owner_id == account.user_id).scalar()
    finally:
        db.close()
    return Account(account.user_id, email, password, ws)


def unlock(email: str = ADMIN_EMAIL, password: Optional[str] = None) -> tuple[int, bytes]:
    """(user id, data key) of an account, as signing in finds them."""
    started()
    db = SessionLocal()
    try:
        user = users.find_by_email(db, email)
        dek = users.unlock_with_password(db, user, password or _passwords[email])
        return user.id, dek
    finally:
        db.close()


def admin_id() -> int:
    started()
    db = SessionLocal()
    try:
        return users.find_by_email(db, ADMIN_EMAIL).id
    finally:
        db.close()


_keys: dict[str, tuple[int, bytes]] = {}
_iks: dict[str, bytes] = {}


def key_of(email: str = ADMIN_EMAIL) -> tuple[int, bytes]:
    """(user id, data key) of an account, unlocked once per test run."""
    if email not in _keys:
        _keys[email] = unlock(email)
    return _keys[email]


def ik_of(email: str = ADMIN_EMAIL) -> bytes:
    """The identity key of an account, opened with its password once per test run."""
    if email not in _iks:
        started()
        db = SessionLocal()
        try:
            _iks[email] = users.unlock(db, users.find_by_email(db, email), _passwords[email]).ik
        finally:
            db.close()
    return _iks[email]


@contextlib.contextmanager
def as_admin() -> Iterator[crypto.Scope]:
    """The admin's key scope, for writing and reading rows (sealed columns) directly in a test. Like a signed-in
    request's, it can open the identity key (so ``admin_key.open_apk`` works); a background run's can't."""
    uid, dek = key_of()
    with crypto.as_user(uid, dek, lambda: ik_of()) as scope:
        yield scope


@contextlib.contextmanager
def as_account(account: "Account") -> Iterator[crypto.Scope]:
    """``account``'s key scope (with its identity key, as a signed-in request's)."""
    uid, dek = key_of(account.email)
    with crypto.as_user(uid, dek, lambda: ik_of(account.email)) as scope:
        yield scope


def pending_codes(account: "Account") -> list[str]:
    """The recovery codes ``account`` hasn't confirmed saving (a new account's first eight)."""
    db = SessionLocal()
    try:
        with as_account(account):
            return users.pending_recovery_codes(db, account.id, ik_of(account.email))
    finally:
        db.close()


_module_scopes: list = []


def enter_admin_scope() -> None:
    """The admin's key scope for a whole test module (``setUpModule``): its tests write and read rows directly and
    call background code (searches, scoring, research) as the admin's own runs would. Requests made with a test
    client still run in the scope of whoever signed in (backend/sessions.py)."""
    scope = as_admin()
    scope.__enter__()
    _module_scopes.append(scope)


def exit_admin_scope() -> None:
    """``tearDownModule``'s half of enter_admin_scope."""
    _module_scopes.pop().__exit__(None, None, None)


def use_admin_scope(test) -> None:
    """The admin's key scope for the rest of ``test`` (call it in setUp or the test): code the test calls
    directly, as a background run or the scheduler would, then acts as the admin."""
    scope = as_admin()
    scope.__enter__()
    test.addCleanup(scope.__exit__, None, None, None)


def new_workspace(name: str, owner: Optional[int] = None) -> int:
    """A workspace made directly in the database, owned by ``owner`` (the admin by default)."""
    owner = admin_id() if owner is None else owner
    db = SessionLocal()
    try:
        email = ADMIN_EMAIL if owner == admin_id() else next(e for e, (uid, _) in _keys.items() if uid == owner)
        uid, dek = key_of(email)
        with crypto.as_user(uid, dek):
            ws = Workspace(name=name, owner_id=owner)
            db.add(ws)
            db.commit()
            return ws.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Fresh processes (first run, restarts, the encryption migration): a throwaway data folder of their own
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
_FOLDERS: list[str] = []


def scratch_folder() -> str:
    folder = tempfile.mkdtemp(prefix="jobhunt-scenario-")
    _FOLDERS.append(folder)
    return folder


def remove_scratch_folders() -> None:
    while _FOLDERS:
        shutil.rmtree(_FOLDERS.pop(), ignore_errors=True)


def scenario_env(folder: str, env: Optional[dict] = None) -> dict:
    """The environment of a fresh process on the throwaway data folder ``folder`` (see ``scenario``)."""
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("JOBHUNT_ADMIN_")}
    run_env.update({"JOBHUNT_DATA_DIR": folder, "JOBHUNT_DB_PATH": os.path.join(folder, "jobhunt.db"),
                    "JOBHUNT_ENV_PATH": os.path.join(folder, ".env"),
                    "JOBHUNT_KEY_FILE": os.path.join(folder, "server-keys.json"), "JOBHUNT_SCHEDULER": "off",
                    "JOBHUNT_TESTING": "1", "JOBHUNT_KDF_PROFILE": "test", "PYTHONPATH": str(ROOT)})
    run_env.pop("DATABASE_URL", None)
    run_env.update(env or {})
    return run_env


def scenario(code: str, folder: Optional[str] = None, env: Optional[dict] = None, *,
             returncode: Optional[int] = 0) -> dict:
    """Run ``code`` in a fresh Python process on a throwaway data folder, database, .env and key file in
    ``folder`` (a new one by default; pass the same folder again to "restart"). ``code`` sets ``out``, which comes
    back as a dict. The admin variables from tests/__init__.py are left out unless ``env`` sets them.
    ``returncode``: the exit status expected (None: any, for a process that is made to crash)."""
    folder = folder or scratch_folder()
    run_env = scenario_env(folder, env)
    program = "import json, os\nout = {}\nFOLDER = %r\n%s\nprint('RESULT ' + json.dumps(out, default=str))\n" % (
        folder, textwrap.dedent(code))
    done = subprocess.run([sys.executable, "-c", program], cwd=folder, env=run_env, capture_output=True, text=True,
                          timeout=180)
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    if returncode is not None and (done.returncode != returncode or (returncode == 0 and not lines)):
        raise AssertionError(f"scenario failed ({done.returncode}):\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}")
    result = json.loads(lines[-1][len("RESULT "):]) if lines else {}
    result.update(_folder=folder, _returncode=done.returncode, _stderr=done.stderr[-3000:])
    return result
