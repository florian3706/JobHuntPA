#!/usr/bin/env python3
"""Time the job list (GET /api/jobs) on sealed data: N made-up jobs in one workspace (accounts design §2.4, §14.6).

    python3 tools/bench_jobs_list.py [--jobs 5000] [--listed 0.2] [--repeat 5] [--json]

The target is under 1.5 s for 5,000 jobs on a Raspberry Pi 4; run this on the Pi itself to check it there.

Everything happens in a NEW temporary folder (database, key file, uploads; .env is never read): the app starts there
with a made-up admin, which encrypts the empty database, and the jobs are written through the app's models under
that admin's key, as the app writes them. Postings are shared and plaintext; each job is a link with its personal
fields sealed (status, hidden, exclusion, placement, office, own content). A fraction ``--listed`` of the jobs pass
the filters and aren't hidden (the list sends only those unless asked); the rest are excluded or hidden. Listed jobs
have a fit score. Nothing touches the network.

Prints, for the default list and for the full list (``include=excluded,hidden``), the median and best time of the
request as the browser would make it (signed in, through every middleware), plus the time to seal and open one
small value, which is what the job list mostly costs on top of the plaintext version.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_DATA = (ROOT / "data").resolve()
EMAIL, PASSWORD = "bench@example.invalid", "bench-password-not-real"


def isolate(folder: Path) -> None:
    """Point the app at ``folder`` before any of it is imported; never the real data folder or .env."""
    folder = folder.resolve()
    if folder == REAL_DATA or REAL_DATA in folder.parents:
        sys.exit("Refusing to use the real data folder.")
    os.environ.update({
        "JOBHUNT_DATA_DIR": str(folder), "JOBHUNT_DB_PATH": str(folder / "bench.db"),
        "JOBHUNT_ENV_PATH": str(folder / "bench.env"), "JOBHUNT_KEY_FILE": str(folder / "server-keys.json"),
        "JOBHUNT_SCHEDULER": "off", "JOBHUNT_ADMIN_EMAIL": EMAIL, "JOBHUNT_ADMIN_PASSWORD": PASSWORD,
        "JOBHUNT_KDF_PROFILE": "low", "LLM_API_KEY": "", "LLM_BASE_URL": "", "LLM_MODEL": "",
    })
    os.environ.pop("DATABASE_URL", None)
    sys.path.insert(0, str(ROOT))


TITLES = ["Product Manager", "Senior Product Manager", "Program Manager", "Delivery Lead", "Business Analyst",
          "Product Owner", "Technical Program Manager", "Project Manager", "Service Designer", "Scrum Master"]
COMPANIES = ["Wattle Analytics", "Saltbush Health", "Copperleaf Energy", "Kestrel Logistics", "Ironbark Systems",
             "Gumnut Learning", "Banksia Bank", "Redgum Talent", "Bluegum Retail", "Waratah Insurance"]
PLACES = [("Sydney NSW", -33.8688, 151.2093), ("North Sydney NSW", -33.8390, 151.2070),
          ("Parramatta NSW", -33.8150, 151.0011), ("Macquarie Park NSW", -33.7759, 151.1209)]
AD = ("About the role. You will lead discovery with customers, own the roadmap with engineering, and measure what "
      "ships. We offer hybrid working with 2 days in the office. ") * 6


def seed(n: int, listed: float) -> int:
    """N jobs in the admin's workspace, written through the models under the admin's key. Returns the workspace."""
    from backend import crypto, users
    from backend.db import FitResult, Job, Posting, SessionLocal, Workspace
    from backend.scraping.text import description_hash

    db = SessionLocal()
    try:
        user = users.find_by_email(db, EMAIL)
        dek = users.unlock_with_password(db, user, PASSWORD)
        ws = db.query(Workspace.id).filter(Workspace.owner_id == user.id).scalar()
        with crypto.as_user(user.id, dek):
            every = max(1, round(1 / listed)) if listed > 0 else n + 1
            for start in range(0, n, 500):
                links = []
                for i in range(start, min(n, start + 500)):
                    where, lat, lng = PLACES[i % len(PLACES)]
                    posting = Posting(url=f"https://jobs.bench.example/{i}", source="greenhouse:bench",
                                      external_id=str(i), company=COMPANIES[i % len(COMPANIES)],
                                      title=f"{TITLES[i % len(TITLES)]} {i}", location_text=where, country="AU",
                                      work_mode="hybrid", employment_type="full_time", office_days=2,
                                      salary_text="$150k - $170k", salary_min=150000, salary_max=170000,
                                      description=AD, description_hash=description_hash(AD), detail_status="full",
                                      rev=1)
                    shown = i % every == 0
                    job = Job(workspace_id=ws, origin="pool", posting=posting, posting_rev=1, status="to_review",
                              lat=lat, lng=lng, distance_km=4.2 + i % 30,
                              hidden=(not shown and i % 5 == 1),
                              excluded_reason=None if shown or i % 5 == 1 else "no target title or include keyword")
                    links.append(job)
                db.add_all(links)
                db.flush()
                for job in links:
                    if not job.hidden and not job.excluded_reason:
                        db.add(FitResult(job_id=job.id, status="ok", score=60 + job.id % 40, profile_hash="bench",
                                         model="bench-model", evidence_json=json.dumps(
                                             {"summary": "A good match on product discovery and roadmaps.",
                                              "requirements": [], "gaps": ["Healthcare domain"]})))
                db.commit()
        return ws
    finally:
        db.close()


def time_requests(client, url: str, repeat: int) -> dict:
    times, count, omitted = [], 0, 0
    for _ in range(repeat):
        t0 = time.perf_counter()
        r = client.get(url)
        times.append(time.perf_counter() - t0)
        if r.status_code != 200:
            sys.exit(f"{url}: HTTP {r.status_code} {r.text[:300]}")
        count, omitted = len(r.json()), int(r.headers.get("X-Omitted-Count", "0"))
    return {"median_s": round(statistics.median(times), 3), "best_s": round(min(times), 3), "jobs_sent": count,
            "omitted": omitted}


def value_cost(rounds: int = 20000) -> dict:
    """Microseconds to seal and to open one short value (a status, a title) under a user's key."""
    from backend import crypto
    from backend.crypto import SealedText

    from sqlalchemy import Column, Integer, MetaData, Table
    sealed = SealedText()
    Table("bench_values", MetaData(), Column("id", Integer, primary_key=True), Column("v", sealed))
    with crypto.as_user(1, crypto.new_key()):
        t0 = time.perf_counter()
        blobs = [sealed.process_bind_param("Senior Product Manager", None) for _ in range(rounds)]
        t1 = time.perf_counter()
        for blob in blobs:
            sealed.process_result_value(blob, None)
        t2 = time.perf_counter()
    return {"seal_us": round((t1 - t0) / rounds * 1e6, 2), "open_us": round((t2 - t1) / rounds * 1e6, 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs", type=int, default=5000, help="jobs in the workspace (default 5000)")
    parser.add_argument("--listed", type=float, default=0.2, help="share of jobs that are listed (default 0.2)")
    parser.add_argument("--repeat", type=int, default=5, help="requests per measurement (default 5)")
    parser.add_argument("--json", action="store_true", help="print only the results, as JSON")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = parser.parse_args()

    folder = Path(tempfile.mkdtemp(prefix="jobhunt-bench-"))
    isolate(folder)
    try:
        import logging

        logging.disable(logging.INFO)
        from fastapi.testclient import TestClient

        from backend.app import app

        out = {"jobs": args.jobs, "listed_share": args.listed}
        # An address as the Host (backend/hosts.py allows any); start-up makes the admin and encrypts the empty database.
        with TestClient(app, base_url="http://127.0.0.1") as client:
            t0 = time.perf_counter()
            ws = seed(args.jobs, args.listed)
            out["seed_s"] = round(time.perf_counter() - t0, 1)
            r = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
            if r.status_code != 200:
                sys.exit(f"Signing in failed: {r.status_code} {r.text[:300]}")
            client.headers.update({"X-Workspace": str(ws)})
            client.get("/api/jobs")  # warm-up: imports, compiled statements
            out["default_list"] = time_requests(client, "/api/jobs", args.repeat)
            out["full_list"] = time_requests(client, "/api/jobs?include=excluded,hidden", args.repeat)
        out["value"] = value_cost()
        if args.json:
            print(json.dumps(out))
            return
        d, f, v = out["default_list"], out["full_list"], out["value"]
        print(f"{args.jobs} jobs ({d['jobs_sent']} listed, {d['omitted']} excluded or hidden), seeded in "
              f"{out['seed_s']} s")
        print(f"  GET /api/jobs                           median {d['median_s']:.3f} s, best {d['best_s']:.3f} s")
        print(f"  GET /api/jobs?include=excluded,hidden   median {f['median_s']:.3f} s, best {f['best_s']:.3f} s "
              f"({f['jobs_sent']} jobs)")
        print(f"  one short value: seal {v['seal_us']} µs, open {v['open_us']} µs")
        print("Target: under 1.5 s for the default list of 5,000 jobs on a Raspberry Pi 4.")
    finally:
        if args.keep:
            print(f"Kept {folder}", file=sys.stderr)
        else:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
