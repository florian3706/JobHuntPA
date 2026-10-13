# A database made by the code at origin/main (cb5d0fe)

`tests/test_upgrade_from_main.py` opens this install with today's code, starts the app and signs in. It is the
check that an existing install survives Phase A of `docs/design/open-source.md` (memberships, digests, email and
two-step sign-in taken out).

What is here, all throwaway test data (the test KDF profile, `JOBHUNT_TESTING=1`; no real person, key or address):

| File | What it is |
|---|---|
| `jobhunt.db` | The database, checkpointed into one file (journal mode DELETE; the app turns WAL back on) |
| `server-keys.json` | The server key file that belongs to it |
| `uploads/*.sealed` | The sealed document files |
| `env.txt` | The install's `.env` (copied to `.env` by the test): an AI provider, `PUBLIC_BASE_URL=https://jobs.example.invalid`, and the old `SMTP_*` and `STRIPE_*` lines |
| `build_fixture.py` | The script that made it, kept for the record (it runs only against cb5d0fe's code) |

The accounts and rows:

- `owner@example.invalid` / `fixture-owner-password`: the first admin. Two-step sign-in on, background access on,
  a job digest every 2 days, a CV, a pin, dealbreakers, a shortlisted pool job with a note and an applied private job.
- `member@example.invalid` / `fixture-member-password`: an ordinary account. Two-step sign-in on, a membership row
  marked as an active Stripe subscription, a 30-day free period (grant) with a note sealed to the admin key, two jobs.
- Queued mail: unused `auth_tokens` links of kinds `reset`, `verify_email` and `email_change`.
- A sign-in stopped at the two-step code (a `pending_mfa` session, made to expire in 2099 so start-up keeps it).

How it was made:

```sh
git worktree add /tmp/main-code cb5d0fe
JOBHUNT_FIXTURE_OUT=/tmp/fixture PYTHONPATH=/tmp/main-code .venv/bin/python \
    tests/fixtures/upgrade_from_main/build_fixture.py
cp /tmp/fixture/jobhunt.db /tmp/fixture/server-keys.json tests/fixtures/upgrade_from_main/
cp /tmp/fixture/.env tests/fixtures/upgrade_from_main/env.txt
cp /tmp/fixture/uploads/* tests/fixtures/upgrade_from_main/uploads/
git worktree remove /tmp/main-code
```
