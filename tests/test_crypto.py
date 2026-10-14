"""Tests for the crypto core (accounts design, Phases B and D): backend/crypto.py and backend/keys.py, on databases
of their own (tests/test_encryption.py covers the app's). Key derivation uses the "low" argon2 profile, so these
stay fast.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import threading
import unicodedata
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("JOBHUNT_ENV_PATH", os.path.join(_TMP, "test.env"))  # never read or write the real .env
os.environ.setdefault("JOBHUNT_KEY_FILE", os.path.join(_TMP, "default-server-keys.json"))  # never data/

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert, select, text, update  # noqa: E402
from sqlalchemy.orm import Session, declarative_base  # noqa: E402

from backend import crypto, keys, threads  # noqa: E402
from backend.crypto import (  # noqa: E402
    Corrupt, KeyMismatch, Locked, SealedFloat, SealedInt, SealedJSON, SealedText,
)

LOW = crypto.KDF_PROFILES["low"]
POSIX = os.name == "posix"


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class EnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.dek = crypto.new_key()

    def test_seal_round_trip_and_format(self):
        with crypto.as_user(7, self.dek) as scope:
            self.assertEqual(scope.user_id, 7)
            blob = crypto.seal(b"secret cv", "documents.text")
            self.assertEqual(len(blob), len(b"secret cv") + crypto.OVERHEAD)
            self.assertEqual(blob[0], 0x01)
            self.assertEqual(blob[1:5], crypto.key_id(self.dek))
            self.assertTrue(crypto.is_sealed(blob))
            self.assertEqual(crypto.unseal(blob, "documents.text"), b"secret cv")
            self.assertNotEqual(crypto.seal(b"secret cv", "documents.text"), blob)  # random nonce each time
            self.assertEqual(crypto.unseal(crypto.seal(b"", "x"), "x"), b"")
        self.assertEqual(repr(scope), "<crypto.Scope user_id=7>")  # never the key

    def test_no_scope_is_locked(self):
        with crypto.as_user(1, self.dek):
            blob = crypto.seal(b"x", "l")
        self.assertFalse(crypto.has_scope())
        for call in (lambda: crypto.seal(b"x", "l"), lambda: crypto.unseal(blob, "l"),
                     lambda: crypto.blind_index("url", "https://x"), crypto.current,
                     lambda: crypto.seal_file(b"x", Path(_TMP) / "never", "file|never")):
            with self.assertRaises(Locked):
                call()
        self.assertFalse((Path(_TMP) / "never").exists())

    def test_wrong_key_is_key_mismatch(self):
        with crypto.as_user(1, self.dek):
            blob = crypto.seal(b"mine", "l")
        with crypto.as_user(2, crypto.new_key()):
            with self.assertRaises(KeyMismatch) as caught:
                crypto.unseal(blob, "l")
            self.assertNotIsInstance(caught.exception, Corrupt)  # caught by the kid, before decrypting

    def test_label_and_tampering_are_corrupt(self):
        with crypto.as_user(1, self.dek):
            blob = crypto.seal(b"value", "jobs.title")
            with self.assertRaises(Corrupt):
                crypto.unseal(blob, "jobs.company")
            flipped = bytearray(blob)
            flipped[-1] ^= 1
            with self.assertRaises(Corrupt):
                crypto.unseal(bytes(flipped), "jobs.title")
            header = bytearray(blob)
            header[0] = 0x07
            with self.assertRaises(Corrupt):
                crypto.unseal(bytes(header), "jobs.title")
            with self.assertRaises(Corrupt):
                crypto.unseal(blob[:20], "jobs.title")
            self.assertTrue(issubclass(Corrupt, KeyMismatch))

    def test_scopes_nest_and_reset(self):
        other = crypto.new_key()
        with crypto.as_user(1, self.dek):
            with crypto.as_user(2, other):
                self.assertEqual(crypto.current().user_id, 2)
            self.assertEqual(crypto.current().user_id, 1)
        self.assertFalse(crypto.has_scope())
        with self.assertRaises(ValueError):
            with crypto.as_user(1, b"short"):
                pass

    def test_blind_index(self):
        with crypto.as_user(1, self.dek):
            a = crypto.blind_index("url", "https://jobs.example/1")
            self.assertEqual(len(a), 16)
            self.assertEqual(a, crypto.blind_index("url", "https://jobs.example/1"))
            self.assertNotEqual(a, crypto.blind_index("url", "https://jobs.example/2"))
            self.assertNotEqual(a, crypto.blind_index("careers_url", "https://jobs.example/1"))
            with self.assertRaises(ValueError):
                crypto.blind_index("bad\0purpose", "x")
        with crypto.as_user(2, crypto.new_key()):
            self.assertNotEqual(a, crypto.blind_index("url", "https://jobs.example/1"))  # keyed per user

    def test_wrap_unwrap(self):
        kek, dek = crypto.new_key(), crypto.new_key()
        blob = crypto.wrap(kek, dek, "pw|5")
        self.assertEqual(crypto.unwrap(kek, blob, "pw|5"), dek)
        with self.assertRaises(KeyMismatch):
            crypto.unwrap(crypto.new_key(), blob, "pw|5")
        with self.assertRaises(Corrupt):
            crypto.unwrap(kek, blob, "pw|6")  # another user's label
        with crypto.as_user(1, kek):  # same key and label, other namespace: a wrap is not a sealed value
            with self.assertRaises(Corrupt):
                crypto.unseal(blob, "pw|5")
        with self.assertRaises(ValueError):
            crypto.wrap(b"short", dek, "pw|5")

    def test_session_helpers(self):
        secret, dek = crypto.new_key(), crypto.new_key()
        sid = crypto.session_id(secret)
        self.assertEqual(len(sid), 64)
        wrapped = crypto.wrap_session_dek(secret, dek)
        self.assertEqual(crypto.unwrap_session_dek(secret, wrapped), dek)
        with self.assertRaises(KeyMismatch):
            crypto.unwrap_session_dek(crypto.new_key(), wrapped)
        self.assertEqual(crypto.unwrap(crypto.session_kek(secret), wrapped, f"sess|{sid}"), dek)
        token = crypto.csrf_token(secret)
        self.assertTrue(crypto.check_csrf(secret, token))
        self.assertFalse(crypto.check_csrf(secret, token[:-1] + ("A" if token[-1] != "A" else "B")))
        self.assertFalse(crypto.check_csrf(crypto.new_key(), token))
        self.assertFalse(crypto.check_csrf(secret, None))

    def test_b64(self):
        for data in (b"", b"\xff\xfe", os.urandom(33)):
            self.assertEqual(crypto.b64d(crypto.b64e(data)), data)
            self.assertNotIn("=", crypto.b64e(data))
        with self.assertRaises(ValueError):
            crypto.b64d("ab$c")

    def test_recovery_kek(self):
        salt = os.urandom(16)
        self.assertEqual(crypto.recovery_kek("ABCD-EFGH", salt), crypto.recovery_kek("ABCD-EFGH", salt))
        self.assertNotEqual(crypto.recovery_kek("ABCD-EFGH", salt), crypto.recovery_kek("ABCD-EFGH", os.urandom(16)))


class FileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.dek = crypto.new_key()

    def test_seal_file_round_trip(self):
        path = self.dir / "abc.sealed"
        data = b"%PDF-1.7 my CV with MARKER-12345 inside"
        with crypto.as_user(1, self.dek):
            crypto.seal_file(data, path, "file|abc.sealed")
            self.assertNotIn(b"MARKER-12345", path.read_bytes())
            self.assertEqual(crypto.open_file(path, "file|abc.sealed"), data)
            with self.assertRaises(Corrupt):  # a file renamed onto another document's name
                crypto.open_file(path, "file|other.sealed")
            with self.assertRaises(Corrupt):  # a file is not a sealed value, even with the same key and label
                crypto.unseal(path.read_bytes(), "file|abc.sealed")
        with crypto.as_user(2, crypto.new_key()):
            with self.assertRaises(KeyMismatch):
                crypto.open_file(path, "file|abc.sealed")
        if POSIX:
            self.assertEqual(_mode(path), 0o600)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["abc.sealed"])  # no temp file left


class SealedBoxTest(unittest.TestCase):
    def test_seal_to_and_open(self):
        priv, pub = crypto.new_keypair()
        self.assertEqual(crypto.public_key_of(priv), pub)
        blob = crypto.seal_to(pub, b'{"email":"a@b.c"}', "users.pii_admin|5")
        self.assertEqual(len(blob), len(b'{"email":"a@b.c"}') + crypto.BOX_OVERHEAD)
        self.assertNotEqual(blob, crypto.seal_to(pub, b'{"email":"a@b.c"}', "users.pii_admin|5"))  # fresh ephemeral
        self.assertEqual(crypto.open_sealed(priv, blob, "users.pii_admin|5"), b'{"email":"a@b.c"}')
        with self.assertRaises(Corrupt):
            crypto.open_sealed(priv, blob, "users.pii_admin|6")
        other_priv, _ = crypto.new_keypair()
        with self.assertRaises(KeyMismatch):
            crypto.open_sealed(other_priv, blob, "users.pii_admin|5")
        tampered = bytearray(blob)
        tampered[40] ^= 1
        with self.assertRaises(Corrupt):
            crypto.open_sealed(priv, bytes(tampered), "users.pii_admin|5")
        with self.assertRaises(ValueError):  # a low-order point gives an all-zero shared secret
            crypto.seal_to(bytes(32), b"x", "l")


class ContextTest(unittest.TestCase):
    def test_submit_carries_the_scope_into_the_pool(self):
        self.assertIs(crypto.submit, threads.submit)
        self.assertIs(crypto.run_in_context, threads.run_in_context)
        dek = crypto.new_key()

        def work(label):
            blob = crypto.seal(b"from a worker", label)
            return crypto.current().user_id, threading.current_thread().name, crypto.unseal(blob, label)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="jh-test") as pool:
            with crypto.as_user(42, dek):
                uid, thread_name, plain = crypto.submit(pool, work, "l").result()
                self.assertEqual((uid, plain), (42, b"from a worker"))
                self.assertTrue(thread_name.startswith("jh-test"))
                with self.assertRaises(Locked):  # a plain submit runs with an empty context
                    pool.submit(work, "l").result()
                wrapped = crypto.run_in_context(lambda: crypto.current().user_id)
            self.assertEqual(pool.submit(wrapped).result(), 42)


class KdfTest(unittest.TestCase):
    def test_deterministic_and_split(self):
        salt = crypto.new_salt()
        a = crypto.derive_password("correct horse battery", salt, LOW)
        b = crypto.derive_password("correct horse battery", salt, LOW)
        self.assertEqual((a.auth, a.kek), (b.auth, b.kek))
        self.assertEqual(len(a.auth), 32)
        self.assertNotEqual(a.auth, a.kek)
        self.assertNotEqual(a.kek, crypto.derive_password("correct horse battery", crypto.new_salt(), LOW).kek)
        self.assertNotEqual(a.kek, crypto.derive_password("correct horse batterz", salt, LOW).kek)
        self.assertTrue(crypto.verify_password(a.auth, b))
        self.assertFalse(crypto.verify_password(a.auth, crypto.derive_password("wrong password", salt, LOW)))
        self.assertFalse(crypto.verify_password(None, a))
        self.assertNotIn(a.kek.hex(), repr(a))
        # the derived KEK unwraps what it wrapped
        dek = crypto.new_key()
        self.assertEqual(crypto.unwrap(b.kek, crypto.wrap(a.kek, dek, "pw|1"), "pw|1"), dek)

    def test_unicode_normalisation(self):
        salt = crypto.new_salt()
        composed, decomposed = unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "café")
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(crypto.derive_password(composed, salt, LOW).auth,
                         crypto.derive_password(decomposed, salt, LOW).auth)

    def test_params_and_profiles(self):
        self.assertEqual(crypto.KDF_PROFILES["default"], crypto.KdfParams(3, 65536, 4))
        self.assertEqual(LOW, crypto.KdfParams(2, 19456, 1))
        self.assertEqual(crypto.KdfParams.from_json(LOW.to_json()), LOW)
        for bad in ("nope", "[]", '{"alg":"argon2i","v":19,"t":2,"m":19456,"p":1}',
                    '{"alg":"argon2id","v":19,"t":2,"m":99999999,"p":1}', '{"alg":"argon2id","v":19,"t":0,"m":1024,"p":1}'):
            with self.assertRaises(ValueError):
                crypto.KdfParams.from_json(bad)
        with mock.patch.dict(os.environ, {"JOBHUNT_KDF_PROFILE": "low"}):
            self.assertEqual(crypto.kdf_profile(), LOW)
        with mock.patch.dict(os.environ, {"JOBHUNT_KDF_PROFILE": "test", "JOBHUNT_TESTING": "1"}):
            self.assertEqual(crypto.kdf_profile(), crypto.KDF_PROFILES["test"])
        env = {k: v for k, v in os.environ.items() if k != "JOBHUNT_TESTING"}
        with mock.patch.dict(os.environ, {**env, "JOBHUNT_KDF_PROFILE": "test"}, clear=True), \
                self.assertLogs("backend.crypto", "WARNING"):
            self.assertEqual(crypto.kdf_profile(), crypto.KDF_PROFILES["default"])  # test needs JOBHUNT_TESTING=1
        with mock.patch.dict(os.environ, {"JOBHUNT_KDF_PROFILE": "turbo"}), self.assertLogs("backend.crypto", "WARNING"):
            self.assertEqual(crypto.kdf_profile(), crypto.KDF_PROFILES["default"])
        with self.assertRaises(ValueError):
            crypto.derive_password("pw", b"short", LOW)


Base = declarative_base()


class Note(Base):
    """An ORM model with every sealed type, in an in-memory database."""

    __tablename__ = "notes"
    id = Column(Integer, primary_key=True)
    title = Column(SealedText, nullable=True)
    body = Column(SealedText, nullable=True)
    tags = Column(SealedJSON, nullable=True)
    salary = Column(SealedInt, nullable=True)
    lat = Column(SealedFloat, nullable=True)
    kind = Column(String, nullable=True)  # plaintext, for contrast


class Flag(Base):
    """The Phase D types: bool, datetime and always-sealed columns."""

    __tablename__ = "flags"
    id = Column(Integer, primary_key=True)
    seen = Column(crypto.SealedDateTime, nullable=True)
    hidden = Column(crypto.SealedAlways(crypto.SealedBool), nullable=False, default=False)
    reason = Column(crypto.SealedAlways(crypto.SealedText, pad=128), default=crypto.always_none, nullable=True)
    count = Column(crypto.SealedAlways(crypto.SealedInt), default=crypto.always_none, nullable=True)


class PhaseDTypesTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.dek = crypto.new_key()
        strict = mock.patch.object(crypto, "_strict", False)
        strict.start()
        self.addCleanup(strict.stop)

    def test_round_trips_and_padding(self):
        from datetime import datetime

        when = datetime(2026, 10, 7, 13, 42, 46, 25000)
        with crypto.as_user(1, self.dek), Session(self.engine) as s:
            s.add_all([Flag(id=1, seen=when, hidden=True, reason="salary below floor", count=3),
                       Flag(id=2), Flag(id=3, reason="", count=0)])
            s.commit()
            s.expire_all()
            self.assertEqual([(f.seen, f.hidden, f.reason, f.count) for f in s.query(Flag).order_by(Flag.id)],
                             [(when, True, "salary below floor", 3), (None, False, None, None), (None, False, "", 0)])
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT typeof(seen), length(hidden), length(reason), length(count), "
                                     "typeof(reason) FROM flags ORDER BY id")).fetchall()
        self.assertEqual(rows[1][0], "null")  # a plain sealed column keeps SQL NULL
        # Always-sealed: a value, NULL and "" are the same length; nothing shows whether it's set.
        self.assertEqual({r[1:] for r in rows}, {(crypto.OVERHEAD + 32, crypto.OVERHEAD + 128, crypto.OVERHEAD + 32,
                                                  "blob")})

    def test_legacy_values_are_coerced_and_strict_mode_refuses_them(self):
        from datetime import datetime

        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO flags (id, seen, hidden, reason) VALUES "
                              "(1, '2026-09-01 10:00:00.000000', 1, 'old reason')"))
        with crypto.as_user(1, self.dek), Session(self.engine) as s:
            f = s.get(Flag, 1)
            self.assertEqual((f.seen, f.hidden, f.reason, f.count), (datetime(2026, 9, 1, 10), True, "old reason", None))
        with crypto.as_user(1, self.dek), mock.patch.object(crypto, "_strict", True), Session(self.engine) as s:
            with self.assertRaises(crypto.Plaintext) as caught:
                s.get(Flag, 1)
            self.assertIsInstance(caught.exception, KeyMismatch)  # so the API answers 404
            self.assertNotIn("old reason", str(caught.exception))

    def test_always_sealed_columns_refuse_even_null_tests(self):
        for build in (lambda: Flag.reason.is_(None), lambda: Flag.hidden == False,  # noqa: E712
                      lambda: Flag.count.is_not(None), lambda: Flag.reason == None):  # noqa: E711
            with self.assertRaises(TypeError):
                build()
        Flag.seen.is_(None)  # a plain sealed column still can be

    def test_bad_values_are_refused_without_the_value_in_the_message(self):
        with crypto.as_user(1, self.dek), Session(self.engine) as s:
            for wrong in (Flag(hidden=2), Flag(seen="yesterday"), Flag(count="three")):
                s.add(wrong)
                with self.assertRaises(crypto.SealedValueError) as caught:
                    s.flush()
                self.assertNotIn("yesterday", str(caught.exception))
                s.rollback()


class SealedColumnTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.dek = crypto.new_key()
        strict = mock.patch.object(crypto, "_strict", False)  # (the app's own database may have turned it on)
        strict.start()
        self.addCleanup(strict.stop)

    def raw(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).fetchall()

    def add(self, **values) -> int:
        with Session(self.engine) as s:
            note = Note(**values)
            s.add(note)
            s.commit()
            return note.id

    def test_labels_come_from_the_table(self):
        self.assertEqual(Note.__table__.c.title.type._aad, b"jh/notes.title")
        self.assertEqual(Note.__table__.c.lat.type._aad, b"jh/notes.lat")

    def test_round_trip_every_type(self):
        values = dict(title="Senior PM", body="ünïcode ✓", tags={"a": [1, 2.5, None], "b": "x"}, salary=150000,
                      lat=-33.8688, kind="job")
        with crypto.as_user(1, self.dek):
            nid = self.add(**values)
            empty = self.add(kind="empty")
            with Session(self.engine) as s:
                note = s.get(Note, nid)
                for name, value in values.items():
                    self.assertEqual(getattr(note, name), value)
                self.assertIsInstance(note.salary, int)
                self.assertIsInstance(note.lat, float)
                blank = s.get(Note, empty)
                self.assertEqual([blank.title, blank.tags, blank.salary, blank.lat], [None] * 4)
                note.salary = 160000
                note.tags = ["changed"]
                s.commit()
                s.expire_all()
                self.assertEqual((s.get(Note, nid).salary, s.get(Note, nid).tags), (160000, ["changed"]))
        row = self.raw("SELECT title, body, tags, salary, lat, kind, typeof(salary) FROM notes WHERE id=:i", i=nid)[0]
        for stored in row[:5]:
            self.assertIsInstance(stored, bytes)
            self.assertEqual(stored[0], 0x01)
        self.assertNotIn(b"Senior", row[0])
        self.assertEqual(row[5], "job")
        self.assertEqual(row[6], "blob")
        self.assertEqual(self.raw("SELECT title IS NULL, salary IS NULL FROM notes WHERE id=:i", i=empty)[0], (1, 1))

    def test_core_insert_and_update_seal_too(self):
        table = Note.__table__
        with crypto.as_user(1, self.dek):
            with self.engine.begin() as conn:
                nid = conn.execute(insert(table).values(title="core", salary=5)).inserted_primary_key[0]
                conn.execute(update(table).where(table.c.id == nid).values(lat=1.5))
                self.assertEqual(conn.execute(select(table.c.title, table.c.salary, table.c.lat)
                                              .where(table.c.id == nid)).one(), ("core", 5, 1.5))
        self.assertTrue(crypto.is_sealed(self.raw("SELECT lat FROM notes WHERE id=:i", i=nid)[0][0]))

    def test_floats_and_specials(self):
        with crypto.as_user(1, self.dek):
            nid = self.add(lat=float("inf"), salary=True, tags="just a string")
            with Session(self.engine) as s:
                note = s.get(Note, nid)
                self.assertEqual((note.lat, note.salary, note.tags), (float("inf"), 1, "just a string"))
            for wrong in (Note(salary="12"), Note(title=b"bytes"), Note(lat="1.5"), Note(tags={1, 2})):
                with self.assertRaises(crypto.SealedValueError) as caught, Session(self.engine) as s:
                    s.add(wrong)
                    s.flush()
                self.assertIsInstance(caught.exception, TypeError)
                self.assertNotIn("12", str(caught.exception))  # not wrapped: no parameters in the message

    def test_no_scope_is_locked(self):
        with self.assertRaises(Locked):
            self.add(title="x")
        with crypto.as_user(1, self.dek):
            nid = self.add(title="x")
        with self.assertRaises(Locked), Session(self.engine) as s:
            s.get(Note, nid)

    def test_wrong_user_is_key_mismatch(self):
        with crypto.as_user(1, self.dek):
            nid = self.add(title="mine")
        with crypto.as_user(2, crypto.new_key()), self.assertRaises(KeyMismatch), Session(self.engine) as s:
            s.get(Note, nid)

    def test_value_moved_to_another_column_fails(self):
        with crypto.as_user(1, self.dek):
            nid = self.add(title="Senior PM", body="long text")
        with self.engine.begin() as conn:  # someone with the database copies one column into another
            conn.execute(text("UPDATE notes SET body = title WHERE id=:i"), {"i": nid})
        with crypto.as_user(1, self.dek), self.assertRaises(Corrupt), Session(self.engine) as s:
            s.get(Note, nid)

    def test_legacy_plaintext_is_read_and_coerced(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO notes (id, title, body, tags, salary, lat) VALUES "
                              "(1, 'old title', 42, '[\"a\", 1]', '120000', '-33.5'), "
                              "(2, 'x', 'y', 'not json', 'n/a', 'somewhere'), "
                              "(3, NULL, NULL, 'null', 7.0, 3)"))
        with crypto.as_user(1, self.dek), Session(self.engine) as s:
            one, two, three = s.get(Note, 1), s.get(Note, 2), s.get(Note, 3)
            self.assertEqual((one.title, one.body, one.tags, one.salary, one.lat),
                             ("old title", "42", ["a", 1], 120000, -33.5))
            self.assertEqual((two.tags, two.salary, two.lat), ("not json", "n/a", "somewhere"))  # kept as stored
            self.assertEqual((three.title, three.tags, three.salary, three.lat), (None, None, 7, 3.0))
            # writing a legacy row back seals it
            one.title = "new title"
            s.commit()
        self.assertTrue(crypto.is_sealed(self.raw("SELECT title FROM notes WHERE id=1")[0][0]))
        self.assertEqual(self.raw("SELECT body FROM notes WHERE id=1")[0][0], 42)  # untouched stays plaintext

    def test_comparisons_raise_but_null_tests_work(self):
        refused = [lambda: Note.title == "x", lambda: Note.title != "x", lambda: Note.title.in_(["x"]),
                   lambda: Note.title.like("x%"), lambda: Note.title.desc(), lambda: Note.title.asc(),
                   lambda: Note.salary > 5, lambda: Note.title.is_("x"), lambda: Note.title.contains("x"),
                   lambda: Note.title.distinct(), lambda: "x" + Note.title, lambda: Note.lat.between(1, 2),
                   lambda: Note.__table__.c.title == "x", lambda: Note.__table__.c.tags.not_in([1])]
        for build in refused:
            with self.assertRaises(TypeError):
                build()
        with self.assertRaises(TypeError), Session(self.engine) as s:
            s.query(Note).filter_by(title="x").all()
        with crypto.as_user(1, self.dek):
            with_title = self.add(title="t")
            without = self.add(kind="none")
            with Session(self.engine) as s:
                ids = lambda cond: [n.id for n in s.scalars(select(Note).where(cond).order_by(Note.id))]  # noqa: E731
                self.assertEqual(ids(Note.title.is_(None)), [without])
                self.assertEqual(ids(Note.title.is_not(None)), [with_title])
                self.assertEqual(ids(Note.title == None), [without])  # noqa: E711  (compiles to IS NULL)
                self.assertEqual(ids(Note.title != None), [with_title])  # noqa: E711

    def test_shared_type_object_gets_a_label_per_column(self):
        shared = SealedText()
        md = MetaData()
        a = Table("a", md, Column("id", Integer, primary_key=True), Column("v", shared))
        b = Table("b", md, Column("id", Integer, primary_key=True), Column("v", shared))
        self.assertEqual(a.c.v.type._aad, b"jh/a.v")
        self.assertEqual(b.c.v.type._aad, b"jh/b.v")
        self.assertIsNot(a.c.v.type, b.c.v.type)

    def test_explicit_label_survives_a_rename(self):
        md = MetaData()
        old = Table("old_name", md, Column("id", Integer, primary_key=True), Column("v", SealedText))
        new = Table("new_name", MetaData(), Column("id", Integer, primary_key=True),
                    Column("v", SealedText(label="old_name.v")))
        self.assertEqual(new.c.v.type._aad, old.c.v.type._aad)
        md.create_all(self.engine)
        with crypto.as_user(1, self.dek):
            with self.engine.begin() as conn:
                conn.execute(insert(old).values(id=1, v="kept"))
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE old_name RENAME TO new_name"))
            with self.engine.connect() as conn:
                self.assertEqual(conn.execute(select(new.c.v)).scalar_one(), "kept")

    def test_unattached_type_refuses(self):
        with crypto.as_user(1, self.dek), self.assertRaises(crypto.CryptoError):
            SealedText().process_bind_param("x", None)


class KeyFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.path = self.dir / "server-keys.json"

    def test_created_only_without_users(self):
        asked = []
        k = keys.load_or_create(lambda: asked.append(1) or False, path=self.path)
        self.assertEqual(asked, [1])
        self.assertTrue(k.created_now)
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc["version"], 1)
        self.assertEqual({doc[s]["current"] for s in ("kek", "pepper", "signing")}, {"k1", "p1", "s1"})
        if POSIX:
            self.assertEqual(_mode(self.path), 0o600)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["server-keys.json"])  # no temp file left
        fp = k.fingerprint()
        self.assertEqual(len(fp), 16)
        again = keys.load_or_create(True, fp, path=self.path)
        self.assertFalse(again.created_now)
        self.assertEqual(again.fingerprint(), fp)
        self.assertEqual(again.email_hash("A@B.example"), k.email_hash("a@b.example"))

    def test_missing_with_users_stops(self):
        with self.assertRaises(keys.KeyFileMissing) as caught:
            keys.load_or_create(lambda: True, "0011223344556677", path=self.path)
        self.assertIn("Restore data/server-keys.json from your backup", str(caught.exception))
        self.assertFalse(self.path.exists())  # never a new file once there are users

    def test_fingerprint_mismatch_stops(self):
        keys.load_or_create(False, path=self.path)
        other = keys.ServerKeys.create(self.dir / "other.json")
        with self.assertRaises(keys.KeyFileMismatch):
            keys.load_or_create(True, other.fingerprint(), path=self.path)
        with self.assertRaises(keys.KeyFileMismatch):
            keys.load_or_create(True, None, path=self.path)  # users but no fingerprint stored
        self.assertFalse(keys.load_or_create(False, other.fingerprint(), path=self.path).created_now)  # no users: fine

    def test_create_never_replaces(self):
        first = keys.ServerKeys.create(self.path)
        with self.assertRaises(FileExistsError):
            keys.ServerKeys.create(self.path)
        self.assertEqual(keys.ServerKeys.load(self.path).fingerprint(), first.fingerprint())

    def test_writes_are_atomic(self):
        k = keys.ServerKeys.create(self.path)
        before = self.path.read_bytes()
        with mock.patch("backend.crypto.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                k.rotate_signing()
        self.assertEqual(self.path.read_bytes(), before)  # the old file, whole
        self.assertEqual([p.name for p in self.dir.iterdir()], ["server-keys.json"])  # temp file removed
        self.assertEqual(k._sections["signing"]["current"], "s1")  # memory didn't run ahead of the file
        replaced = []
        real_replace = os.replace
        with mock.patch("backend.crypto.os.replace", side_effect=lambda a, b: replaced.append((a, b)) or
                        real_replace(a, b)):
            k.rotate_signing()
        (tmp, dest), = replaced
        self.assertEqual(Path(tmp).parent, self.path.parent)  # temp file in the same folder, then renamed
        self.assertEqual(Path(dest), self.path)
        if POSIX:
            self.assertEqual(_mode(self.path), 0o600)

    @unittest.skipUnless(POSIX, "file modes are POSIX")
    def test_loose_permissions_are_tightened(self):
        keys.ServerKeys.create(self.path)
        os.chmod(self.path, 0o644)  # as after unzipping a backup
        with self.assertLogs("backend.keys", "WARNING"):
            keys.ServerKeys.load(self.path)
        self.assertEqual(_mode(self.path), 0o600)

    def test_damaged_file_is_never_rewritten(self):
        self.path.write_text('{"version": 1, "kek": {}}')
        with self.assertRaises(keys.KeyFileInvalid):
            keys.load_or_create(False, path=self.path)
        self.assertEqual(self.path.read_text(), '{"version": 1, "kek": {}}')
        self.path.write_text("not json")
        with self.assertRaises(keys.KeyFileInvalid):
            keys.ServerKeys.load(self.path)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"JOBHUNT_KEY_FILE": str(self.path)}):
            self.assertEqual(keys.key_file_path(), self.path)
            k = keys.load_or_create(False)
            self.assertEqual(k.path, self.path)
            self.assertTrue(self.path.exists())
        with mock.patch.dict(os.environ, {"JOBHUNT_KEY_FILE": "elsewhere/keys.json"}):
            self.assertEqual(keys.key_file_path(), keys.config.PROJECT_ROOT / "elsewhere" / "keys.json")
        with mock.patch.dict(os.environ, {"JOBHUNT_KEY_FILE": ""}):
            self.assertEqual(keys.key_file_path(), keys.config.PROJECT_ROOT / "data" / "server-keys.json")

    def test_gitignored(self):
        lines = (keys.config.PROJECT_ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("data/server-keys.json", lines)


class KekTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.path = self.dir / "server-keys.json"
        self.keys = keys.ServerKeys.create(self.path)
        # a stand-in for user_keys.server_wrapped_dek and another server-wrapped value: ref -> (label, blob)
        self.secrets = {}
        self.db = {}
        for uid in range(1, 6):
            self.secrets[("srv", uid)] = crypto.new_key()
            self.secrets[("other", uid)] = os.urandom(20)
            for kind in ("srv", "other"):
                kek_id, blob = self.keys.kek_wrap(self.secrets[(kind, uid)], f"{kind}|{uid}")
                self.db[(kind, uid)] = (f"{kind}|{uid}", blob, kek_id)

    def rows(self):
        return [keys.KekWrapped(ref, label, blob) for ref, (label, blob, _) in self.db.items()]

    def store(self, updates):
        for ref, kek_id, blob in updates:
            self.db[ref] = (self.db[ref][0], blob, kek_id)

    def assert_all_open(self, k):
        for ref, (label, blob, _) in self.db.items():
            self.assertEqual(k.kek_unwrap(blob, label), self.secrets[ref])

    def test_wrap_unwrap_and_labels(self):
        label, blob, kek_id = self.db[("srv", 1)]
        self.assertEqual(kek_id, "k1")
        self.assertEqual(self.keys.kek_id_of(blob), "k1")
        self.assertEqual(self.keys.kek_unwrap(blob, "srv|1"), self.secrets[("srv", 1)])
        with self.assertRaises(Corrupt):
            self.keys.kek_unwrap(blob, "srv|2")  # another user's row
        self.assertEqual(crypto.unwrap(self.keys._sections["kek"]["keys"]["k1"], blob, "srv|1|k1"),
                         self.secrets[("srv", 1)])  # the label is "srv|{uid}|{kek_id}", as designed
        other = keys.ServerKeys.create(self.dir / "other.json")
        self.assertIsNone(other.kek_id_of(blob))
        with self.assertRaises(KeyMismatch):
            other.kek_unwrap(blob, "srv|1")

    def test_rotation_rewraps_everything(self):
        n = self.keys.rotate_kek(self.rows, self.store)
        self.assertEqual(n, 10)
        self.assertEqual(self.keys.kek_ids, ["k2"])
        self.assertFalse(self.keys.kek_rotation_pending)
        self.assertTrue(all(kek_id == "k2" for _, _, kek_id in self.db.values()))
        reloaded = keys.ServerKeys.load(self.path)
        self.assertEqual(reloaded.kek_ids, ["k2"])
        self.assert_all_open(reloaded)
        self.assertEqual(self.keys.rotate_kek(self.rows, self.store), 10)  # and again: k3
        self.assertEqual(keys.ServerKeys.load(self.path).kek_ids, ["k3"])

    def test_crash_after_step_one(self):
        self.keys.start_kek_rotation()  # then the process dies
        k = keys.ServerKeys.load(self.path)
        self.assertTrue(k.kek_rotation_pending)
        self.assertEqual(k.current_kek_id, "k2")
        self.assert_all_open(k)  # old wraps still open
        kek_id, blob = k.kek_wrap(b"new while rotating", "other|9")  # new wraps use the new key
        self.assertEqual(kek_id, "k2")
        self.db[("other", 9)] = ("other|9", blob, kek_id)
        self.secrets[("other", 9)] = b"new while rotating"
        self.assertEqual(k.finish_kek_rotation(self.rows, self.store), 10)  # start-up resumes
        self.assertEqual(keys.ServerKeys.load(self.path).kek_ids, ["k2"])
        self.assert_all_open(keys.ServerKeys.load(self.path))

    def test_crash_while_storing(self):
        self.keys.start_kek_rotation()
        original = dict(self.db)

        def crash_midway(updates):  # half the batch was written (separate commits), then the process died
            self.store(updates[: len(updates) // 2])
            raise SystemExit("power cut")

        with self.assertRaises(SystemExit):
            self.keys.finish_kek_rotation(self.rows, crash_midway)
        self.assertNotEqual(self.db, original)
        k = keys.ServerKeys.load(self.path)
        self.assertEqual(k.kek_ids, ["k1", "k2"])  # nothing dropped
        self.assert_all_open(k)  # mixed old and new wraps all open
        self.assertEqual(k.finish_kek_rotation(self.rows, self.store), 5)  # only the rest
        k = keys.ServerKeys.load(self.path)
        self.assertEqual(k.kek_ids, ["k2"])
        self.assert_all_open(k)

    def test_crash_before_dropping_old_key(self):
        real = crypto.write_private
        calls = []

        def fail_second_write(path, data, **kw):
            calls.append(path)
            if len(calls) == 2:  # step 1 wrote the file; the final write (dropping k1) dies
                raise OSError("crash")
            return real(path, data, **kw)

        with mock.patch("backend.crypto.write_private", side_effect=fail_second_write), self.assertRaises(OSError):
            self.keys.rotate_kek(self.rows, self.store)
        k = keys.ServerKeys.load(self.path)
        self.assertEqual(k.kek_ids, ["k1", "k2"])
        self.assert_all_open(k)
        self.assertEqual(k.finish_kek_rotation(self.rows, self.store), 0)  # nothing left to re-wrap
        self.assertEqual(keys.ServerKeys.load(self.path).kek_ids, ["k2"])

    def test_unreadable_value_keeps_old_keys(self):
        self.db[("srv", 99)] = ("srv|99", crypto.wrap(crypto.new_key(), b"x" * 32, "srv|99|k1"), "k1")
        with self.assertRaises(keys.RotationIncomplete) as caught:
            self.keys.rotate_kek(self.rows, self.store)
        self.assertEqual(caught.exception.refs, [("srv", 99)])
        self.assertEqual(keys.ServerKeys.load(self.path).kek_ids, ["k1", "k2"])
        del self.db[("srv", 99)]
        self.assertEqual(self.keys.finish_kek_rotation(self.rows, self.store), 0)  # the rest moved already
        self.assertEqual(keys.ServerKeys.load(self.path).kek_ids, ["k2"])
        self.assert_all_open(keys.ServerKeys.load(self.path))

    def test_value_left_behind_keeps_old_keys(self):
        self.keys.start_kek_rotation()
        with self.assertRaises(keys.RotationIncomplete):
            self.keys.finish_kek_rotation(self.rows, lambda updates: None)  # a store that saved nothing
        self.assertEqual(keys.ServerKeys.load(self.path).kek_ids, ["k1", "k2"])


class PepperTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.keys = keys.ServerKeys.create(self.dir / "server-keys.json")

    def test_email_hash(self):
        h = self.keys.email_hash("Jane.Doe@Example.com")
        self.assertEqual(len(h), 32)
        self.assertEqual(h, self.keys.email_hash("  jane.doe@example.com "))
        self.assertEqual(h, self.keys.email_hash("ｊａｎｅ.doe@example.com".replace("ｊａｎｅ", "Ｊａｎｅ")))  # NFKC
        self.assertNotEqual(h, self.keys.email_hash("jane.doe@example.org"))
        self.assertNotEqual(h, keys.ServerKeys.create(self.dir / "other.json").email_hash("jane.doe@example.com"))

    def test_rotation_keeps_lookups_working(self):
        old_hash, old_fp = self.keys.email_hash("a@b.example"), self.keys.fingerprint()
        self.assertEqual(self.keys.rotate_pepper(), "p2")
        reloaded = keys.ServerKeys.load(self.keys.path)
        candidates = reloaded.email_hash_candidates("a@b.example")
        self.assertEqual(candidates[0], reloaded.email_hash("a@b.example"))
        self.assertEqual(candidates[1], old_hash)
        self.assertNotEqual(reloaded.fingerprint(), old_fp)
        self.assertTrue(reloaded.matches_fingerprint(old_fp))  # a crash before storing the new one is harmless
        reloaded.drop_old_peppers()
        self.assertEqual(len(reloaded.email_hash_candidates("a@b.example")), 1)
        self.assertFalse(reloaded.matches_fingerprint(old_fp))


class SigningTest(unittest.TestCase):
    NOW = 1_800_000_000

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.keys = keys.ServerKeys.create(self.dir / "server-keys.json")

    def test_sign_and_verify(self):
        token = self.keys.sign("unsub", "AbC123_-xyz")
        payload, mac = token.split(".")
        self.assertEqual((payload, len(mac)), ("AbC123_-xyz", 22))  # the design's "id.mac22" unsubscribe format
        self.assertEqual(self.keys.verify("unsub", token), "AbC123_-xyz")
        self.assertIsNone(self.keys.verify("reset", token))  # another purpose
        self.assertIsNone(self.keys.verify("unsub", "AbC123_-xyz." + ("A" * 22)))
        self.assertIsNone(self.keys.verify("unsub", "Other." + mac))
        for junk in (None, "", "a", "a.b.c.d", token + "x", "a|b." + mac):
            self.assertIsNone(self.keys.verify("unsub", junk))
        self.assertIsNone(keys.ServerKeys.create(self.dir / "other.json").verify("unsub", token))
        with self.assertRaises(ValueError):
            self.keys.sign("unsub", "has.dot")

    def test_expiry(self):
        token = self.keys.sign("verify_email", "u42", ttl=3600, now=self.NOW)
        self.assertEqual(token.count("."), 2)
        self.assertEqual(self.keys.verify("verify_email", token, now=self.NOW + 3599), "u42")
        self.assertIsNone(self.keys.verify("verify_email", token, now=self.NOW + 3600))
        payload, expiry, mac = token.split(".")
        self.assertIsNone(self.keys.verify("verify_email", f"{payload}.{int(expiry) + 9999}.{mac}", now=self.NOW))
        self.assertIsNone(self.keys.verify("verify_email", f"{payload}.{mac}", now=self.NOW))  # expiry can't be cut

    def test_previous_key_within_its_window(self):
        old_token = self.keys.sign("unsub", "id1")
        self.assertEqual(self.keys.rotate_signing(now=self.NOW), "s2")
        reloaded = keys.ServerKeys.load(self.keys.path)
        new_token = reloaded.sign("unsub", "id1")
        self.assertNotEqual(new_token, old_token)
        day = 24 * 3600
        self.assertEqual(reloaded.verify("unsub", old_token, now=self.NOW + 29 * day), "id1")
        self.assertIsNone(reloaded.verify("unsub", old_token, now=self.NOW + 30 * day))
        self.assertEqual(reloaded.verify("unsub", new_token, now=self.NOW + 300 * day), "id1")
        # 31 days on, the next rotation deletes s1 from the file
        reloaded.rotate_signing(now=self.NOW + 31 * day)
        doc = json.loads(reloaded.path.read_text())
        self.assertEqual(sorted(doc["signing"]["keys"]), ["s2", "s3"])
        self.assertEqual(doc["signing"]["retired"], {"s2": self.NOW + 31 * day})


class CliTest(unittest.TestCase):
    def test_bench_kdf(self):
        out = io.StringIO()
        results = keys.bench_kdf(["low"], runs=1, out=out)
        self.assertEqual(list(results), ["low"])
        self.assertGreater(results["low"], 0)
        self.assertIn("m=19 MiB, t=2, p=1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
