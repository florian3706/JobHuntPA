"""The admin details key (APK): how admins read personal details, and how the key is shared and replaced
(accounts design §1.4).

Personal details of other people (email and name in ``users.pii_admin``, billing details, a membership grant's note,
an audit entry's detail) are sealed to the public half of one X25519 key pair, so a sign-up or a Stripe sync can write
them with no admin signed in. The private half lives in ``admin_key_grants``, one row per admin, sealed to that
admin's own X25519 public key. In an admin's request it is opened like this, every time, and kept nowhere:

    the admin's identity key (IK: the request's session, ``crypto.open_ik``) -> their X25519 private key
        (``user_keys.x25519_priv_enc``, label ``ik-priv|{admin id}``) -> their grant (``admin_key_grants.sealed_priv``)
        -> the APK's private key

The IK is wrapped under the password, the recovery codes and the sessions, never under the server KEK, and the data
key does not open the private key. So background access (the data key under the server key) can't reach the admin
details key: only an admin who is signed in can (accounts design §1.2 and §1.4). A session made before the IK existed
has none: ``ApkUnavailable.sign_in_again`` says so, and the admin panel asks the admin to sign in again.

Labels (the AAD of each sealed value, so one can't be moved to another place): ``users.pii_admin|{user id}``,
``memberships.billing_admin|{user id}``, ``membership_grants.note_admin|{grant id}``,
``admin_audit.detail_admin|{audit id}`` and ``grant|{admin id}|{apk version}`` for a grant.

**Replacing the key** (``new_key``) makes a new pair, re-seals every value that is sealed to the old one (a value that
doesn't open is marked stale or dropped, never kept as something unreadable that looks fine), grants the new key to
the admins and updates ``app_settings`` (``apk_public``, ``apk_version``). It never commits: the caller does, so the
whole change is one transaction. Demoting an admin does it (they must not keep a key that still opens everything
written from then on); so does ``python -m backend.keys new-admin-key`` after the last admin lost theirs.

**Stale details.** ``users.pii_admin_version`` says which key sealed the value. When it is behind the current
version the admin list says "Details unavailable until this person signs in", and ``heal_details`` (run at every
sign-in, ``sessions.create``) re-seals them from the person's own copy (``pii_self``).
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from backend import crypto, users
from backend.db import (
    AdminAudit,
    AdminKeyGrant,
    Membership,
    MembershipGrant,
    SessionLocal,
    User,
    UserKeys,
    scrub_wal,
    utcnow,
)

log = logging.getLogger(__name__)

STALE = 0  # pii_admin_version of a value no current key opens


class ApkUnavailable(Exception):
    """This admin can't open the admin details key (no grant, an old one, or one that doesn't open).
    ``sign_in_again``: the request's session has no identity key to open it with (it was made before the IK existed),
    so signing in again fixes it."""

    def __init__(self, message: str = "", *, sign_in_again: bool = False):
        super().__init__(message)
        self.sign_in_again = sign_in_again


class NeedsSignIn(ApkUnavailable):
    """The account hasn't signed in with its password since the identity key was introduced, and has background
    access: its private key is still sealed under the data key, which the server key opens, so a grant sealed to it
    would be open to anyone with the database and the key file. It can be shared once they have signed in."""


def pii_label(user_id: int) -> str:
    return f"users.pii_admin|{user_id}"


def billing_label(user_id: int) -> str:
    return f"memberships.billing_admin|{user_id}"


def note_label(grant_id: int) -> str:
    return f"membership_grants.note_admin|{grant_id}"


def audit_label(audit_id: int) -> str:
    return f"admin_audit.detail_admin|{audit_id}"


def grant_label(admin_id: int, version: int) -> str:
    return f"grant|{admin_id}|{version}"


@dataclass(frozen=True, repr=False)
class Apk:
    """The admin details key as one admin's request has opened it. Never stored, never logged."""
    private: bytes
    public: bytes
    version: int

    def __repr__(self) -> str:  # never the key
        return f"<admin_key.Apk v{self.version}>"


def current_public(db: Session) -> Optional[tuple[bytes, int]]:
    """The public half and version of the current key, or None before the first admin exists."""
    return users.admin_public_key(db)


def open_apk(db: Session, admin_id: int) -> Apk:
    """The current APK, opened through ``admin_id``'s own grant. Needs that admin's key scope with an identity key (the
    request they are signed in with). ApkUnavailable when they have no usable grant or the session has no IK."""
    current = current_public(db)
    if current is None:
        raise ApkUnavailable("There is no admin key yet")
    public, version = current
    keys = db.get(UserKeys, admin_id)
    grant = db.get(AdminKeyGrant, admin_id)
    if keys is None or grant is None or grant.apk_version != version:
        raise ApkUnavailable("This admin has no grant of the current admin key")
    if not crypto.has_scope() or crypto.current().user_id != admin_id:
        raise ApkUnavailable("This isn't that admin's request")
    try:
        ik = crypto.open_ik()
    except crypto.NoIdentityKey:
        raise ApkUnavailable("This session has no identity key; sign in again", sign_in_again=True) from None
    try:
        own_private = users.open_private_key(keys, admin_id, ik)
        private = crypto.open_sealed(own_private, grant.sealed_priv, grant_label(admin_id, version))
        if not hmac.compare_digest(crypto.public_key_of(private), public):
            raise ApkUnavailable("The granted key isn't the current admin key")
    except crypto.CryptoError:
        raise ApkUnavailable("This admin's grant doesn't open") from None
    except ValueError:
        raise ApkUnavailable("This admin's grant is damaged") from None
    return Apk(private, public, version)


# ---------------------------------------------------------------------------
# Sealing and opening admin-readable values
# ---------------------------------------------------------------------------

def seal_json(public: bytes, data: Any, label: str) -> bytes:
    return crypto.seal_to(public, json.dumps(data, ensure_ascii=False).encode("utf-8"), label)


def open_json(apk: Optional[Apk], blob: Optional[bytes], label: str) -> Optional[Any]:
    """What ``seal_json`` sealed, or None (no key, no value, or one this key doesn't open)."""
    if apk is None or blob is None:
        return None
    try:
        return json.loads(crypto.open_sealed(apk.private, blob, label))
    except (crypto.CryptoError, ValueError):
        return None


def audit(db: Session, admin_id: Optional[int], action: str, target_id: Optional[int] = None,
          detail: Optional[dict] = None, *, public: Optional[bytes] = None) -> AdminAudit:
    """An ``admin_audit`` row in ``db``'s transaction, its detail sealed to the admin details key (the current public
    key, or ``public``). Details say what was done, never anyone's content."""
    row = AdminAudit(admin_id=admin_id, action=action, target_user_id=target_id)
    db.add(row)
    db.flush()
    if public is None:
        current = current_public(db)
        public = current[0] if current else None
    if detail and public:
        row.detail_admin = seal_json(public, detail, audit_label(row.id))
    return row


def audit_settings(db: Session, admin_id: int, changed: dict[str, bool]) -> None:
    """Record that an admin changed app-wide settings (the AI provider, model and key, the commute keys, reasoning
    levels) and commit. ``changed`` maps each setting's name to whether it was set (False: cleared). Names only,
    never a value."""
    if changed:
        audit(db, admin_id, "server_settings_changed", None,
              {"fields": sorted(changed), "cleared": sorted(n for n, was_set in changed.items() if not was_set)})
        db.commit()


def holds_key(db: Session, admin_id: int) -> bool:
    """Whether ``admin_id`` is an admin with a grant of the current key (it may still not open)."""
    current = current_public(db)
    grant = db.get(AdminKeyGrant, admin_id)
    user = db.get(User, admin_id)
    return (current is not None and grant is not None and grant.apk_version == current[1]
            and user is not None and user.role == "admin")


def other_holders(db: Session, admin_id: int) -> bool:
    """Whether an active admin besides ``admin_id`` has a grant of the current key (so can share it again)."""
    current = current_public(db)
    if current is None:
        return False
    return db.query(AdminKeyGrant.user_id).join(User, User.id == AdminKeyGrant.user_id).filter(
        AdminKeyGrant.user_id != admin_id, AdminKeyGrant.apk_version == current[1],
        User.role == "admin", User.status == "active").first() is not None


def needs_sign_in(keys: UserKeys) -> bool:
    """Whether the account's private key is still under the data key (it has no identity key yet): a grant sealed to
    it would not be protected from the key file, or from an old copy of data/ if background access was on (and has
    since been turned off: nothing records that). Its next password step gives it an identity key."""
    return keys.pw_wrapped_ik is None


def open_grant(db: Session, admin_id: int, private: bytes) -> Optional[Apk]:
    """The current APK as ``admin_id``'s grant holds it, opened with their X25519 private key; None when they have
    no grant of the current key or it doesn't open. Used when the private key is replaced (``users.migrate_identity``)."""
    current = current_public(db)
    grant = db.get(AdminKeyGrant, admin_id)
    if current is None or grant is None or grant.apk_version != current[1]:
        return None
    try:
        key = crypto.open_sealed(private, grant.sealed_priv, grant_label(admin_id, grant.apk_version))
        if not hmac.compare_digest(crypto.public_key_of(key), current[0]):
            return None
    except (crypto.CryptoError, ValueError):
        return None
    return Apk(key, current[0], current[1])


def grant_to(db: Session, apk: Apk, admin_id: int, granted_by: Optional[int]) -> None:
    """Share ``apk`` with ``admin_id``: sealed to their current X25519 public key (they needn't be signed in, unless
    their private key is still under the data key: ``NeedsSignIn``)."""
    keys = db.get(UserKeys, admin_id)
    if keys is None:
        raise ApkUnavailable("That account has no keys")
    if needs_sign_in(keys):
        raise NeedsSignIn("That account has to sign in once first")
    sealed = crypto.seal_to(keys.x25519_pub, apk.private, grant_label(admin_id, apk.version))
    row = db.get(AdminKeyGrant, admin_id)
    if row is None:
        db.add(AdminKeyGrant(user_id=admin_id, apk_version=apk.version, sealed_priv=sealed, granted_by=granted_by))
    else:
        row.apk_version, row.sealed_priv, row.granted_by = apk.version, sealed, granted_by
        row.granted_at = utcnow()
    db.flush()


# ---------------------------------------------------------------------------
# Replacing the key
# ---------------------------------------------------------------------------

@dataclass
class Rotation:
    apk: Apk
    resealed: int = 0   # values moved to the new key
    stale: int = 0      # personal details marked to be re-sealed at their owner's next sign-in
    dropped: int = 0    # other values that no longer opened, and were cleared
    skipped: list = field(default_factory=list)  # admins not given the key yet (``NeedsSignIn``)


def active_admin_ids(db: Session) -> list[int]:
    """The admins who hold the key after a rotation: active ones only. An admin who was turned off is no longer
    trusted with what is written from now on, so a new key is never shared with them (they get a grant again only
    when another admin shares it after turning them on)."""
    return [i for (i,) in db.query(User.id).filter(User.role == "admin", User.status == "active")]


def new_key(db: Session, old: Optional[Apk], admin_ids: Iterable[int], granted_by: Optional[int]) -> Rotation:
    """A new APK for ``admin_ids`` (every remaining admin), in ``db``'s transaction (not committed).

    With ``old`` (the key as an admin who holds it opened it), every value sealed to it is opened and sealed again
    to the new key. Without it (``new-admin-key``: nobody has it any more) personal details become stale and heal at
    each person's next sign-in; the other values that were sealed to it stay as they are (unreadable). An admin who
    has not signed in since the identity key (``needs_sign_in``) is not given the key
    (``Rotation.skipped``, audited): another admin shares it once they have."""
    priv, pub = crypto.new_keypair()
    current = current_public(db)
    version = (current[1] if current else 0) + 1
    apk = Apk(priv, pub, version)
    out = Rotation(apk)
    if old is not None:
        _reseal_everything(db, old, apk, out)
    users._put_setting(db, "apk_public", base64.b64encode(pub).decode("ascii"))
    users._put_setting(db, "apk_version", version)
    recipients = list(dict.fromkeys(admin_ids))
    for admin_id in recipients:  # an admin whose private key is still under the data key waits for their next sign-in
        keys = db.get(UserKeys, admin_id)
        if keys is not None and needs_sign_in(keys):
            out.skipped.append(admin_id)
            audit(db, granted_by, "admin_key_not_shared", admin_id,
                  {"reason": "not signed in since the identity key", "apk_version": version}, public=pub)
    keep = set(recipients) - set(out.skipped)
    for row in db.query(AdminKeyGrant).all():
        if row.user_id not in keep:  # someone who is no longer an admin (or isn't given it yet) keeps nothing
            db.delete(row)
    db.flush()
    for admin_id in recipients:  # the others get the new key (their old grant is replaced)
        if admin_id not in out.skipped:
            grant_to(db, apk, admin_id, granted_by)
    return out


def _reseal_everything(db: Session, old: Apk, new: Apk, out: Rotation) -> None:
    def move(blob: bytes, label: str) -> Optional[bytes]:
        try:
            return crypto.seal_to(new.public, crypto.open_sealed(old.private, blob, label), label)
        except (crypto.CryptoError, ValueError):
            return None

    for user in db.query(User).filter(User.pii_admin.isnot(None)):
        if user.pii_admin_version == old.version:
            moved = move(user.pii_admin, pii_label(user.id))
            if moved is not None:
                user.pii_admin, user.pii_admin_version = moved, new.version
                out.resealed += 1
                continue
        user.pii_admin_version = STALE  # heals from pii_self at the next sign-in
        out.stale += 1
    for row in db.query(Membership).filter(Membership.billing_admin.isnot(None)):
        moved = move(row.billing_admin, billing_label(row.user_id))
        row.billing_admin = moved
        out.resealed, out.dropped = out.resealed + (moved is not None), out.dropped + (moved is None)
    for grant in db.query(MembershipGrant).filter(MembershipGrant.note_admin.isnot(None)):
        moved = move(grant.note_admin, note_label(grant.id))
        grant.note_admin = moved
        out.resealed, out.dropped = out.resealed + (moved is not None), out.dropped + (moved is None)
    for entry in db.query(AdminAudit).filter(AdminAudit.detail_admin.isnot(None)):
        moved = move(entry.detail_admin, audit_label(entry.id))
        entry.detail_admin = moved
        out.resealed, out.dropped = out.resealed + (moved is not None), out.dropped + (moved is None)


# ---------------------------------------------------------------------------
# Healing stale details
# ---------------------------------------------------------------------------

def heal_details(db: Session, user: User, dek: bytes, public: Optional[tuple[bytes, int]] = None) -> bool:
    """Re-seal ``users.pii_admin`` from the person's own copy when it isn't sealed to the current key. Needs their
    data key. In ``db``'s transaction (not committed). Never raises: a sign-in must not fail because of this."""
    try:
        public = public or current_public(db)
        if public is None or (user.pii_admin is not None and user.pii_admin_version == public[1]):
            return False
        if user.pii_self is None:
            return False
        with crypto.as_user(user.id, dek):
            info = users.details(db, user.id)
        user.pii_admin = seal_json(public[0], {"email": info.get("email", ""), "name": info.get("name", "")},
                                   pii_label(user.id))
        user.pii_admin_version = public[1]
        log.info("user %s: personal details for admins re-sealed to the current admin key", user.id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("user %s: could not re-seal personal details for admins (%s)", user.id, exc.__class__.__name__)
        return False


def _behind(version: int):
    """SQL: personal details not sealed to key ``version``."""
    return or_(User.pii_admin_version.is_(None), User.pii_admin_version != version)


# ---------------------------------------------------------------------------
# python -m backend.keys new-admin-key --email X
# ---------------------------------------------------------------------------

def new_admin_key_command(email: str, out=None, err=None) -> int:
    """Makes ``email`` an admin if they aren't, creates a new APK shared with every admin, and marks older personal
    details stale (accounts design §1.4). People with background access are re-sealed at once (the server key opens
    their data key); everyone else at their next sign-in. Run with the app stopped."""
    out, err = out or sys.stdout, err or sys.stderr
    from backend import key_rotation

    try:
        server = key_rotation.server_keys_for_cli()
    except Exception as exc:  # noqa: BLE001
        print(f"Can't open the server keys: {exc}", file=err)
        return 2
    db = SessionLocal()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        found = db.query(User).filter(User.email_hash.in_(server.email_hash_candidates(email or ""))).first() \
            if isinstance(email, str) and email.strip() else None
        if found is None:
            print("No account has that email address. Nothing was changed.", file=err)
            return 2
        if found.status != "active":
            print("That account is turned off. Turn it on from the admin panel first. Nothing was changed.", file=err)
            return 2
        became_admin = found.role != "admin"
        found.role = "admin"
        db.flush()
        admin_ids = active_admin_ids(db)
        rotation = new_key(db, None, admin_ids, found.id)
        public = (rotation.apk.public, rotation.apk.version)
        immediately = 0
        for user in db.query(User).filter(_behind(rotation.apk.version)):
            row = db.get(UserKeys, user.id)
            if row is None or row.server_wrapped_dek is None:
                continue
            try:
                dek = server.kek_unwrap(row.server_wrapped_dek, f"srv|{user.id}")
            except crypto.CryptoError:
                continue
            immediately += heal_details(db, user, dek, public)
        audit(db, found.id, "admin_key_replaced", found.id,
              {"by": "command line", "made_admin": became_admin, "healed_now": immediately},
              public=rotation.apk.public)
        db.commit()
        scrub_wal()  # the old key's grants must not linger in the write-ahead log
        waiting = db.query(User).filter(_behind(rotation.apk.version)).count()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    print(f"Done. {'The account is now an admin. ' if became_admin else ''}The new admin key is shared with "
          f"{len(admin_ids) - len(rotation.skipped)} admin(s).", file=out)
    if rotation.skipped:
        print(f"{len(rotation.skipped)} admin(s) have not signed in since the identity key was introduced, so they "
              "were not given the key. Once they have signed in, run this command again.", file=out)
    print(f"{immediately} people's details were re-sealed now; {waiting} more will be when they next sign in "
          "(until then the admin list says \"Details unavailable until this person signs in\").", file=out)
    return 0
