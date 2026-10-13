"""Builds the database in this folder with the code of origin/main at cb5d0fe (the last code with memberships,
digests, email and two-step sign-in). It is kept for the record: it does not run with today's code, only in a
checkout of cb5d0fe. See README.md.

    git worktree add /tmp/main-code cb5d0fe
    JOBHUNT_FIXTURE_OUT=/tmp/fixture PYTHONPATH=/tmp/main-code .venv/bin/python \
        tests/fixtures/upgrade_from_main/build_fixture.py
"""
import io
import os
import sqlite3
import sys
from datetime import datetime, timedelta

OUT = os.environ["JOBHUNT_FIXTURE_OUT"]
os.makedirs(OUT, exist_ok=True)
for name in ("jobhunt.db", "jobhunt.db-wal", "jobhunt.db-shm", "server-keys.json", ".env"):
    if os.path.exists(os.path.join(OUT, name)):
        os.remove(os.path.join(OUT, name))

OWNER, OWNER_PW = "owner@example.invalid", "fixture-owner-password"
MEMBER, MEMBER_PW = "member@example.invalid", "fixture-member-password"
os.environ.update({
    "JOBHUNT_DATA_DIR": OUT, "JOBHUNT_DB_PATH": os.path.join(OUT, "jobhunt.db"),
    "JOBHUNT_ENV_PATH": os.path.join(OUT, ".env"), "JOBHUNT_KEY_FILE": os.path.join(OUT, "server-keys.json"),
    "JOBHUNT_SCHEDULER": "off", "JOBHUNT_TESTING": "1", "JOBHUNT_KDF_PROFILE": "test",
    "JOBHUNT_ADMIN_EMAIL": OWNER, "JOBHUNT_ADMIN_PASSWORD": OWNER_PW,
})
os.environ.pop("DATABASE_URL", None)
# The .env an install with email, payments and an AI key had.
with open(os.environ["JOBHUNT_ENV_PATH"], "w", encoding="utf-8") as f:
    f.write("LLM_BASE_URL=https://api.anthropic.com\nLLM_MODEL=claude-fixture-model\nLLM_API_KEY=sk-ant-fixture-key\n"
            "PUBLIC_BASE_URL=https://jobs.example.invalid\nSMTP_HOST=smtp.example.invalid\nSMTP_PORT=587\n"
            "SMTP_SECURITY=starttls\nSMTP_USER=mailer\nSMTP_PASSWORD_B64=c2VjcmV0\nSMTP_FROM=jobs@example.invalid\n"
            "STRIPE_SECRET_KEY=sk_test_fixture\nSTRIPE_PRICE_LOOKUP_KEY=jobhunt_monthly\n")

from fastapi.testclient import TestClient  # noqa: E402

from backend import pool, ratelimit, tokens, totp, users  # noqa: E402
from backend.adapters.base import Posting as Scraped  # noqa: E402
from backend.app import app  # noqa: E402
from backend.db import AuthSession, Job, Membership, SessionLocal, Workspace, utcnow  # noqa: E402

BASE = "https://jobs.example.invalid"


def sign_in(c, email, password, code=None):
    ratelimit.reset_all()
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    if r.json().get("mfa_required"):
        r = c.post("/api/auth/login/mfa", json={"code": code})
        assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return r.json()


def enrol(c):
    secret = c.post("/api/auth/mfa/setup").json()["secret"]
    r = c.post("/api/auth/mfa/enable", json={"code": totp.code_at(secret, totp.current_step())})
    assert r.status_code == 200, r.text
    return secret


def add_jobs(email, password, ws):
    db = SessionLocal()
    try:
        user = users.find_by_email(db, email)
        account = users.unlock(db, user, password)
        from backend import crypto
        with crypto.as_user(user.id, account.dek):
            now = datetime.utcnow()
            posting, _ = pool.upsert_scraped(db, Scraped(
                url="https://www.seek.com.au/job/80000001", title="Fixture Delivery Lead", company="Fixtureco",
                location_text="Sydney NSW", country="AU", external_id="80000001",
                description="Lead delivery for the fixture."), "seek", None, now)
            job, _ = pool.link(db, ws, posting, now)
            job.status = "shortlisted"
            job.notes = "Fixture note"
            private, _ = pool.import_private(db, ws, Scraped(
                url="https://careers.example.invalid/jobs/7", title="Fixture Private Role", company="Privateco",
                location_text="Melbourne VIC", country="AU", description="A saved page."), "saved", now)
            private.status = "applied"
            db.commit()
    finally:
        db.close()


with TestClient(app, base_url=BASE) as c:
    sign_in(c, OWNER, OWNER_PW)
    owner_secret = enrol(c)
    owner_ws = c.get("/api/workspaces").json()[0]["id"]
    c.headers["X-Workspace"] = str(owner_ws)
    r = c.post("/api/documents", files={"file": ("cv.txt", io.BytesIO(b"Fixture CV: ten years of delivery."), "text/plain")},
               data={"kind": "resume"})
    assert r.status_code in (200, 201), r.text
    r = c.put("/api/dealbreakers", json={"industries": ["Gambling"], "keywords": ["fixture-dealbreaker"]})
    assert r.status_code == 200, r.text
    r = c.post("/api/pins", json={"label": "Fixture home", "kind": "home", "lat": -33.87, "lng": 151.21,
                                  "radius_km": 15})
    assert r.status_code == 201, r.text
    r = c.post("/api/account/background", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"], r.text
    assert c.put("/api/account/digest", json={"enabled": True, "every_days": 2}).status_code == 200
    add_jobs(OWNER, OWNER_PW, owner_ws)

    # A second account, with two-step sign-in on, a membership paid at Stripe and a free period (grant).
    member = users.create_user(MEMBER, MEMBER_PW, name="Fixture Member", created_via="open")
    r = c.post(f"/api/admin/users/{member.user_id}/grants", json={"kind": "free_period", "days": 30,
                                                                   "note": "Fixture grant note"})
    assert r.status_code == 201, r.text
    c.post("/api/auth/logout")

    c.headers.pop("X-Workspace", None)
    c.headers.pop("X-CSRF-Token", None)
    sign_in(c, MEMBER, MEMBER_PW)
    enrol(c)
    member_ws = c.get("/api/workspaces").json()[0]["id"]
    add_jobs(MEMBER, MEMBER_PW, member_ws)
    c.post("/api/auth/logout")

    db = SessionLocal()
    try:
        m = db.get(Membership, member.user_id)
        m.stripe_status, m.plan, m.stripe_mode = "active", "monthly", "test"
        m.current_period_end = utcnow() + timedelta(days=20)
        # Queued mail: links that were emailed and not used yet (a reset, an address check, an address change).
        for kind, uid in (("reset", member.user_id), ("verify_email", member.user_id), ("email_change", 1)):
            tokens.create(db, kind, ttl=timedelta(days=1), user_id=uid, payload={"email": "new@example.invalid"})
        db.commit()
    finally:
        db.close()

    # A sign-in stopped at the two-step code (a pending_mfa session), kept alive for the upgrade test.
    c.headers.pop("X-CSRF-Token", None)
    ratelimit.reset_all()
    r = c.post("/api/auth/login", json={"email": MEMBER, "password": MEMBER_PW})
    assert r.json().get("mfa_required"), r.text

db = SessionLocal()
try:
    far = datetime(2099, 1, 1)
    for s in db.query(AuthSession).filter(AuthSession.state == "pending_mfa"):
        s.idle_expires_at = s.absolute_expires_at = far
    db.commit()
finally:
    db.close()

from backend.db import engine  # noqa: E402

engine.dispose()
con = sqlite3.connect(os.environ["JOBHUNT_DB_PATH"])
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.execute("PRAGMA journal_mode=DELETE")
con.execute("VACUUM")
con.close()
for name in ("jobhunt.db-wal", "jobhunt.db-shm", "server-keys.json.lock", "first-run.txt"):
    if os.path.exists(os.path.join(OUT, name)):
        os.remove(os.path.join(OUT, name))
print("built", sorted(os.listdir(OUT)), file=sys.stderr)
