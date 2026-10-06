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

A client also works without ``with``: call ``c.__enter__()`` and ``addCleanup(c.__exit__, None, None, None)``.
"""
from __future__ import annotations

import contextlib
import itertools
import os
from dataclasses import dataclass
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
        ratelimit.reset_all()  # every test signs in from the same address
        r = self.post("/api/auth/login", json={"email": self.email, "password": self.password,
                                                "remember": self.remember})
        if r.status_code != 200:
            raise AssertionError(f"signing in as {self.email} failed: {r.status_code} {r.text}")
        self.headers["X-CSRF-Token"] = r.json()["csrf"]
        return r.json()


def client(email: str = ADMIN_EMAIL, password: Optional[str] = None, **kwargs) -> SignedInClient:
    """A client signed in as ``email`` (the admin by default) once started."""
    return SignedInClient(email, password or _passwords[email], **kwargs)


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


@contextlib.contextmanager
def as_admin() -> Iterator[crypto.Scope]:
    """The admin's key scope, for writing rows (sealed ones, from Phase D) directly in a test."""
    uid, dek = unlock()
    with crypto.as_user(uid, dek) as scope:
        yield scope


def new_workspace(name: str, owner: Optional[int] = None) -> int:
    """A workspace made directly in the database, owned by ``owner`` (the admin by default)."""
    owner = admin_id() if owner is None else owner
    db = SessionLocal()
    try:
        ws = Workspace(name=name, owner_id=owner)
        db.add(ws)
        db.commit()
        return ws.id
    finally:
        db.close()
