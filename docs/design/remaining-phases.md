# Remaining accounts work: briefs for cloud sessions

This file breaks the rest of the accounts work into phases, one cloud session each, with a ready-to-paste brief for
every phase. The design is `docs/design/accounts.md`. This file doesn't repeat it, but each brief names the sections
that phase must follow.

## Where things stand (2026-10-07)

The last release is **v1.13.0** (tag), which has no accounts. The Raspberry Pi's live app (port 8040) still runs
**v1.12.3**, as the user asked. The Pi's test instance (port 8001) runs v1.13.0, with its own copy of the data.

Everything since v1.13.0 is on GitHub `main` (pushed 2026-10-07, not released or tagged). The work branch
**`wip/accounts`** points at the same commit; cloud sessions start from it:

| Phase | What it built | State |
|---|---|---|
| A | Multi-user groundwork: ownership-safe `owned()`, run queue per workspace, logs without user data | done |
| A2 | Shared job pool (`postings` plus per-workspace `jobs` links); 30-day company profiles with daily sentiment and news | done |
| B | Crypto core: `backend/crypto.py` and `backend/keys.py` | done |
| C | Accounts and sessions: first run, sign-in, cookie-wrapped data key, CSRF, rate limits, admin-only global settings | done |
| D | Encrypting user data and migrating it | done (see the commit log on `wip/accounts`) |
| K | Sign-up wizard (`welcome.html`): theme first, then search setup | done |
| Email core | `backend/mailer.py`, `backend/email_templates.py` | done, wired in by H |
| Billing core | `backend/access.py` (membership gate) and `backend/billing.py` (Stripe) | done, wired in by I |
| E, F, G | Below. Built in parallel and merged | done (merged 2026-10-07) |
| H | Email: settings, address checks, forgot password, notices | done (merged at 7d74fd7) |
| I | Memberships and Stripe | done (merged at 7d74fd7) |
| X | Data export (added on 2026-10-07) | done (merged at 7d74fd7) |
| J | Job digest emails | in progress (worktree `phase-j`) |
| L | Hardening and final review | **to do** |

## Order and what can run in parallel

```
            ┌── E  (MFA, password and account management) ──┐
D (done) ───┼── F  (admin panel) ─────────────────────────────┼── H (email wiring) ──┐
            └── G  (background access) ─────────────────────┘   I (memberships) ─┼── J (digests) ── L (hardening)
```

- **Wave 1, in parallel:** E, F and G. E and G both add cards to the Account part of the Settings tab, so merge E
  first and give G's UI a quick rebase.
- **Wave 2, in parallel:** H (needs E, F and G), I (needs F) and X, the data export (needs E's password dialog).
  H and I both add settings to `ENV_RULES` and to the admin panel; those merges are trivial.
- **Wave 3:** J (needs G and H; I is optional).
- **Wave 4:** L, the final hardening and review.

## How each cloud session works

1. Start the session on the repository, then check out the work branch:
   `git fetch origin wip/accounts && git checkout -b cloud/phase-<x> origin/wip/accounts`
2. Paste the phase's brief from below. It begins with the shared rules, so paste those too.
3. The session pushes `cloud/phase-<x>`. It never pushes `main` or `wip/accounts`, never tags and never bumps `VERSION`.
4. **Review before merging.** Fetch the branch, run the full suite in a throwaway `git worktree`, read the
   security-critical files, then merge into `wip/accounts`. A local Claude session can do this; ask it to
   "review and merge cloud/phase-<x>".
5. Merge one wave fully into `wip/accounts` before starting the next wave, so every session starts from the code it
   depends on.

## Shared rules (paste at the top of every brief)

> You're working on JobHuntPA (FastAPI + SQLAlchemy/SQLite, vanilla-JS frontend), on branch `cloud/phase-<x>` made from
> `origin/wip/accounts`.
>
> **Read first:**
> - **CLAUDE.md, which is binding.** Setup and launch scripts keep working; new settings go only through
>   `backend/app_settings.py` and `.env.example`; new dependencies go in `requirements.txt` with version bounds; README
>   updated for anything users see; scraping only through `backend/scraping/http.py`; every query scoped to a
>   workspace; duplicate jobs skipped; LLM calls only through `backend/llm_providers.py`.
> - **`docs/design/accounts.md`:** the sections your brief names, plus §13 (the user's answers), §14 (shared job
>   pool), §15 (crypto review notes) and §16 (email notes).
>
> **Conventions from earlier phases:**
> - Encryption:
>   - Every API request already runs inside `crypto.as_user` for the signed-in user.
>   - Sealed columns can't be filtered, sorted, grouped or DISTINCTed in SQL; do that in Python.
>   - `owned()` checks the plaintext `workspace_id` first.
>   - Thread pools use `backend/threads.submit`.
> - Logs:
>   - Never log user data, email addresses, document text or secrets.
>   - Errors say the exception class only.
> - New UI:
>   - Must meet **WCAG 2.2 AA** and work on phones from 320 px.
>   - Use the existing theme tokens and the live-region helpers in `frontend/common.js`.
>   - Follow the error-summary pattern of `frontend/auth.js`.
>   - Extend `tests/test_accessibility.py` for new pages.
>
> **Tests:**
> - Run `python3 -m unittest discover -s tests -t .`; all tests must pass.
> - `tests/__init__.py` isolates the database, `.env`, key file and uploads. Keep it, and never point anything at a
>   real `data/` folder.
> - Mock the network, SMTP, Stripe and the LLM.
>
> **Commits:**
> - Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>` (keep any other trailer your
>   session adds).
> - Push your branch; never `main`, `wip/accounts` or tags.
>
> **Finish with a report:**
> - the branch and commit
> - the changes, file by file
> - how the feature works from the user's view
> - the test result
> - any deviation from the design and why
> - what a security reviewer should check first

---

## Phase E: MFA, password and account management

**Design:** §3 (sessions, re-authentication), §6 flows, §11 row E.

**Build:**
- **Two-factor sign-in with an authenticator app (TOTP):**
  - Enrolment shows a QR code (`pyotp` plus `segno`) and the secret as text, for people who can't scan.
  - Sign-in asks for the 6-digit code after the password.
  - A code can't be replayed.
  - Turning it off requires the password plus a code.
  - Remove Phase C's temporary 403 for MFA accounts.
- **Recovery codes:**
  - A recovery code works instead of the authenticator code.
  - A recovery code can also reset a forgotten password while *keeping* the data.
  - Each code works once.
  - Regenerating the codes shows the new set once (the same Copy, Download and Print step as first run).
- **Change password:** re-wrap the data key under the new password; the data stays readable.
- **Sign out everywhere:** ends all sessions.
- **Delete account:**
  - Needs the password and recent re-authentication.
  - Removes the account's workspaces, jobs links, documents, sealed upload files, sessions and keys.
  - Shared job postings and company profiles stay.
  - The last admin can't delete themselves.
- **Account cards on the Settings tab:** password, two-factor, recovery codes, sign out everywhere, delete account.
  The theme preference already saves through `/api/account/prefs` (Phase K); reuse it, don't duplicate it.
- **Re-authentication** within 15 minutes for sensitive actions (§3), through Phase C's `has_recent_auth` hook. Phase D
  has no re-authentication screen yet: today, deleting the leftover plaintext files after 15 minutes means signing
  out and back in. Add a "confirm your password" dialog and use it everywhere `has_recent_auth` is required.

**Tests:**
- TOTP enrolment and sign-in.
- Replay refused.
- Each recovery code works once.
- A recovery reset keeps the data, checked by reading a sealed value afterwards.
- A password change keeps the data.
- Sign out everywhere ends other sessions.
- Deletion removes rows and files; another user is untouched.
- Last-admin protection.
- Accessibility checks for the new cards and the code step.

---

## Phase F: admin panel

**Design:** §1.4 (admin details key), §7 (admin panel), §14.8 (admin-owned settings), §11 row F.

**What the user asked for:**
- An admin user type with an admin panel.
- See the users.
- Send password-reset emails. Build the button and the endpoint here; the email itself is sent once Phase H wires
  mail in. Until then, show "email isn't set up yet". (Historical: Phase H has since wired it in.)
- LLM usage stats.
- The number of users, and active users in the past week and month.
- Manually change membership time frames; add free periods for users; exempt users from payment entirely.
- All user details, including names and payment information, are encrypted so that only signed-in admins can see
  them. That is `pii_admin`, sealed to the admin details key.

**Build:**
- **`backend/admin.py` and `frontend/admin.html` + `admin.js`.** The account menu's "Admin" item appears when
  `/api/auth/me` reports `features.admin_panel`.
- **Stats:**
  - Total users.
  - Active users over 7 and 30 days, from the day-level activity date in §4.
  - LLM usage from the `llm_usage` table (added in Phase 1), by day, model, purpose and user, with cached tokens.
  - Add `llm_usage.user_id`; it's filled from the request scope.
- **Users list:**
  - Name and email, decrypted from `pii_admin` with the admin details key, only in admin requests.
  - Role, MFA on or off, background access on or off, last active (by day), and membership state.
  - Never show anything from a user's own data: no searches, documents or jobs.
- **Actions:**
  - promote and demote, which rotates the admin details key as §1.4 describes
  - disable and enable
  - turn MFA off, with a security notice to that user once Phase H lands
  - delete the user
  - send a reset email (the hook for Phase H)
  - edit the membership: start and end dates, free periods (grants), and the exempt flag (store only; Phase I wires
    the gate)
- **Sign-up mode** (invite, open or closed; default invite), invites, and `signup.html` (only the form when sign-up is
  open; invite emails come with Phase H). New users go to the wizard (`welcome.html`) after their recovery codes, as
  first run does.
- **Admin-only global settings move into the admin panel:**
  - the AI provider, model, keys and reasoning levels
  - the TomTom and Transport for NSW keys

  They're already admin-only since Phase C; this gives them a home in the panel.
- **Also:**
  - an audit log of admin actions, with details sealed for admins
  - the "plaintext files" report from Phase D. Today a notice on the dashboard stands in for it; give it a proper
    home in the panel (`backend/migrate_encryption.py` already has `leftover_files` and `delete_leftovers`)
  - the server-key rotation button (it requires recent re-authentication)
- **Error messages:** AI error messages that reach ordinary users must not reveal the provider address or setup
  details. Phase C found such text in `backend/llm_providers.py` around lines 295, 592 and 632, and in
  `config_problem()`. Give ordinary users a plain "The AI service had a problem; your admin can see details" and keep
  the detail for admins.

**Tests:**
- The stats SQL.
- Admin responses contain no content fields.
- Promote then demote rotates the admin details key, and the remaining admins can still read details.
- `new-admin-key` details self-heal at the next sign-in.
- Non-admins get 403 everywhere.
- Error-message hygiene.
- Accessibility checks for the admin pages.

---

## Phase G: background access

**Design:** §1.2–1.3 (server key, `server_wrapped_dek`), §6, §11 row G, §13 item 1.

**Build:**
- **Switching background access on and off** from the Account section of the Settings tab, with a plain explanation:
  - With it on, scheduled searches and digest emails run while you're signed out, and the server holds a key that
    can open your data.
  - With it off, only you can open your data, and scheduled searches wait until you sign in.
- **Turning it on** wraps the data key under the server key. **Turning it off** destroys that copy.
- **The scheduler** (`backend/schedule.py`). Phase D already made it run a workspace's search under its owner's
  background key (`users.background_key`) and skip the slot when there's none. Build on that:
  - runs each workspace's schedule under its owner's key, unwrapped with the server key
  - when there's no key, it skips the slot and records a reason the user sees ("Scheduled search skipped: background
    access is off")
- **The "keep data" hook for email password resets:** a user with background access keeps their data on reset,
  because the server can re-wrap the key (§13 item 1). Phase H calls this hook.
- **Turn on `features.scheduled_searches`** in `backend/account.py`. The wizard (Phase K) then shows its "Automatic
  searches" step; that code exists but is untested, so test it now.

**Tests:**
- A locked user's scheduled slot is skipped with a reason.
- An opted-in user's runs happen.
- Turning it off stops runs and deletes the server-wrapped key.
- The keep-data hook.
- The wizard's automatic-searches step.

---

## Phase H: email wiring

**Design:** §9, §13 items 1–2, §16 (the link names, the SMTP password decision, sending from one background thread).
Uses `backend/mailer.py` and `backend/email_templates.py`, which existed but weren't wired in when this brief was
written (historical: this brief describes the state before H was merged).

**Build:**
- **SMTP and public-address settings** in the admin panel (Phase F): `SMTP_*` and `PUBLIC_BASE_URL`. Add a "send a
  test email" button.
- **The SMTP password and spaces** (§16): either strip spaces in the form, since Gmail shows app passwords with
  spaces, or move to `SMTP_PASSWORD_B64`. Choose one and document it.
- **Email address verification** (`verify-email.html`).
- **Forgot password by email** (`reset-password.html`):
  - The token is single-use and expires.
  - If MFA is on, a code is required.
  - Without a recovery code, locked data is wiped, with a clear warning first.
  - Users with background access keep their data (Phase G's hook).
- **Admin buttons:** "send password reset" (from Phase F) and invite emails (the link goes to `signup.html?invite=`; there is no accept-invite page).
- **Security notices:** password changed, MFA turned off by an admin.
- **Sending:** from one background thread; never block a request; never log addresses or bodies.

**Tests:**
- Settings validation, including injection attempts.
- A reset token is single-use and expires.
- MFA is required during a reset.
- Wipe versus keep.
- An invite flow end to end with SMTP mocked.
- Both parts of every message (HTML and plain text).
- Accessibility checks for the new pages.

---

## Phase I: memberships and Stripe

**Design:** §8, §11 row I, §13 item 3. Uses `backend/access.py` and `backend/billing.py`, which existed with tests but
weren't wired in when this brief was written (historical: this brief describes the state before I was merged). Read
their module docstrings and the notes in their tests.

**The user's rules:**
- Stripe takes payments.
- Admins need no membership.
- Admins can add free periods or exempt someone entirely.
- New users get **3 free searches**.
- Jobs already found stay visible. New searches wait for a membership once the free searches run out. The AI
  features (rescoring, chat, cover letters, research, Fill missing info, title suggestions, document concise
  versions and summary) are members-only from the start; each free search run still scores its own jobs.
- Scheduled searches need a membership (and background access).
- A free search is given back if its run fails.
- Card numbers are never stored.

**Build:**
- **The gate:**
  - Build an `AccessState` from the membership and grant rows.
  - Answer a denied feature with 402 `{code, message}`; common.js already shows 402 notices.
- **Free searches:**
  - Use one when a run is queued, and set `search_runs.free_search`.
  - Give it back when the run fails or is interrupted.
  - Every free search run scores its own jobs inside the run.
- **Stripe pages and endpoints:**
  - `billing.html`
  - checkout
  - verify on return: check `client_reference_id`, then `apply_subscription`, then store the billing details sealed
    for the user and for admins
  - the customer portal
- **Status syncing:** a sync every 6 hours, plus an on-demand sync when the gate reports `needs_sync`. The webhook
  route answers 404 until a webhook secret is set.
- **Admin side:**
  - Stripe settings in the admin panel.
  - Membership editing (from Phase F) wired to the gate.
  - A free period for someone who pays extends their Stripe trial (`trial_end_for_grant`, then `extend_trial`).
  - Exempting someone who pays offers to cancel at period end.
- **Test mode** with an `sk_test_` key, as `billing.status()` reports.

**Tests:**
- The gate's truth table at the endpoint level.
- Free-search use and refund.
- Scheduled searches gated.
- `apply_subscription` with Stripe mocked.
- The trial extension for a grant.
- The webhook route off and on.
- Accessibility checks for `billing.html`.

---

## Phase J: job digest emails

**Design:** §9 (digests), §13 item 2, §16. `email_templates.job_digest()` and `digest_card()` already render the cards.

**What the user asked for:**
- In Settings, users opt in or out of emails listing **new jobs** found by their workspace's scheduled searches.
- The frequency runs from daily to every *n* days, with *n* from 1 to 7.
- Each email lists the new jobs as small cards like the search screen's, without status, rescore, description and
  the like. Each card shows:
  - the job title as the heading
  - company, salary range, office location and number of office days
  - work type (remote, hybrid or onsite) and employment type
  - the fit summary blurb (without the points or evidence), the fit score, and the number of gaps
  - the app's **Open** link and the link to the original posting
- An **unsubscribe link** at the bottom that works.

**Build:**
- `jobs.first_run_id`, so "new since the last digest" is exact.
- `backend/digest.py`:
  - Selection skips merged duplicates, excluded, hidden and closed jobs.
  - Due-date logic.
  - Sent around 07:00 local time.
  - Nothing is sent when there are no new jobs.
  - Only for users with background access (Phase G).
- **Unsubscribe:**
  - GET on `/unsubscribe?token=` shows a confirmation page and changes nothing.
  - POST unsubscribes with no session (RFC 8058 one-click).
  - The `List-Unsubscribe` headers are already produced.
- **Digest preferences** in Settings, and in the wizard by turning on `features.digests`, with the hook already in
  `welcome.js`.

**Tests:**
- The selection rules.
- Due dates for every frequency.
- A mocked SMTP capture has both headers and every card field.
- A one-click POST works with no session.
- A GET doesn't unsubscribe.
- Opt-out stops the emails.

---

## Phase X: download your data (added 2026-10-07)

**What the user asked for:** "a data export feature. I want to create a zip of a folder structure with all the user
data in markdown files in a sensible folder structure for humans."

**Design:** §2.2 (what is sealed, so what has to be opened), §2.5 (logs), §3 (recent sign-in for sensitive actions),
CLAUDE.md's workspace and `duplicate_of` rules.

**Build:**
- **`backend/export.py`, `POST /api/account/export`.** It answers `application/zip` with
  `Content-Disposition: attachment; filename="JobHuntPA-export-YYYY-MM-DD.zip"`.
  - A POST with the CSRF token, not a GET: a link or a prefetch must never start an export.
  - It needs recent sign-in (`sessions.require_recent_auth`), and the page calls it through `withRecentAuth`.
  - One export at a time per account (429 while one is being built).
  - It is built in memory (`BytesIO`) inside the request's key scope. Plaintext never touches the disk: no temporary
    files, no `SpooledTemporaryFile`.
  - Only the account's own workspaces (`owner_id`), never anyone else's. Admins can't export other people's data.
  - Logs say only that user N exported (file count and size), never names or content.
- **The folder layout** is for people, not programs:
  ```
  JobHuntPA export 2026-10-07/
  ├── README.md               what this is, when it was made, how it's organised, what isn't included and why
  ├── Account.md              email, name, account created, admin or not, two-step sign-in and background access
  │                           on or off, theme, membership (whatever the account has when the export runs)
  ├── Companies/              research on the companies of the jobs that have their own file (shared research)
  │   └── Acme Corp.md
  └── Workspaces/
      └── My job search/
          ├── Search setup.md     job titles, keywords, dealbreakers, salary floor, work modes and office days,
          │                       SEEK locations, home and other pins, commute settings, the search schedule
          ├── Sources.md          company career pages and feeds: label, address, type, on or off, last result
          ├── Documents/
          │   ├── Resume.md           the text, plus the concise version as its own section; notes and the summary
          │   └── Original files/     each upload exactly as uploaded (opened from its sealed file)
          ├── Jobs/
          │   ├── Jobs.md             an index table: title, company, status, fit score, closed, link to the file
          │   ├── Shortlisted/, Applied/, Interviewing/, To review/, Rejected/, Not interested/, Hidden/
          │   │   ├── Acme Corp - Senior Analyst.md
          │   │   └── Acme Corp - Senior Analyst - Cover letter.md
          │   └── Filtered out.md     jobs the search filters left out: title, company, reason, link (no file each)
          ├── Chats/
          │   └── 2026-10-01 Ask about Acme Corp.md
          └── Search history.md   each run: when, manual or scheduled, how it ended, what it found
  ```
  - **A job's file:** title and company as the heading; your status, the fit score, where (work mode, office days,
    distance from home), salary, employment type, posted, first seen and closed dates, the office, a link to the
    original ad; then the fit summary, points, evidence and gaps; the description; and "Also advertised at" listing
    its merged copies. Merged copies (`duplicate_of` set) get no file of their own.
  - **Chats** keep each message's role and time, and link the jobs they're about to those jobs' files (relative
    links).
  - **Links between files are relative,** so they work after unzipping, offline.
- **Safe names on Windows, macOS and Linux:** no `<>:"/\|?*` or control characters, no leading or trailing dots or
  spaces, none of Windows' reserved names (CON, PRN, AUX, NUL, COM1–9, LPT1–9), at most 80 characters per part,
  NFC-normalised, and unique within a folder ("Resume (2).md"). Zip entries use forward slashes and UTF-8 names.
- **Safe Markdown:** ads and AI answers are other people's text. Escape raw HTML so a Markdown viewer shows it as
  text, escape `|` and line breaks inside table cells, and only link `http`/`https` addresses.
- **The page:** a "Download your data" card in Settings (`#account-export-slot`). It says what's in the file, that
  the file isn't encrypted (keep it somewhere safe), then "Download my data (.zip)". The download happens through
  `fetch`, a Blob and an object URL, with a busy state and a status message in a live region.
- **Later phases** that add personal data (I's membership details, J's digest settings) add it to the export too.

**Tests:**
- The zip's folders and names.
- A marker planted in every sealed field comes out readable in the right file.
- Another user's data and workspaces never appear.
- Merged copies have no file and show in the main job's file; excluded jobs only in `Filtered out.md`; hidden jobs in
  `Hidden/`.
- Hostile names (`../x`, `CON`, `a/b`, 300 characters, emoji, two documents with one name) become safe, unique names.
- HTML in a description comes out escaped.
- It needs recent sign-in and the CSRF token; a second export while one runs gets 429.
- No file is written to disk while exporting.
- An original upload comes back byte for byte.
- Accessibility checks for the card.

---

## Phase L: hardening and final review

**Design:** §11 row L, §2.1 and §15 (the proposed CLAUDE.md rules), §2.5.

**Build:**
- **Security headers:** a Content-Security-Policy with a hash for the inline theme script, `frame-ancestors 'none'`,
  `X-Content-Type-Options: nosniff`, and a `Referrer-Policy`.
- **A Host allowlist** against DNS rebinding (flagged in Phase C). It's built from `PUBLIC_BASE_URL` plus localhost
  and the machine's own names.
- **A log-leak test across a full run:** sign up, search, score, chat, a cover letter, research, and a digest. None of
  the planted markers may appear in any log.
- **A full WCAG 2.2 AA re-audit:**
  - axe-core on every page and dialog, including the new ones, at 320 and 1280 px in several themes
  - keyboard walkthroughs
  - fix the known axe target-size warning on the Jobs tab's "Show excluded / closed / hidden" check boxes at 320 px
    with an empty list
- **A security review of the whole branch:**
  - sessions and the middleware (the open paths, the Origin rules, the CSRF exemptions)
  - first run
  - the key handling, unwrapping and rotation
  - the admin details key grants
  - the reset flows
  - the unsubscribe tokens
  - the Stripe verification
  - the encryption migration's strict mode
- **Docs:**
  - Regenerate the README screenshots (`tools/readme_screenshots.py`) so the account menu and new pages appear.
  - Final README pass.
  - Propose the CLAUDE.md rules from §2.1 and §15 as a separate commit for the user to approve.

---

## Deploying (for the local session, not for cloud agents)

- **The Pi's systemd unit** runs uvicorn directly. Once accounts ship, it needs
  `--proxy-headers --forwarded-allow-ips 127.0.0.1`, as `installer/launch.py` already passes. Otherwise every tailnet
  client shares one sign-in rate-limit bucket.
- **The GitHub repo is private,** and the Pi has no GitHub credentials. Until it gets a read-only deploy key, deploy by
  pushing from the laptop over SSH (see the "Pi deploy routine" notes).
- **Measure job-list speed on the Pi:** run `python3 tools/bench_jobs_list.py` there. Phase D estimated 1.1–1.6 s for
  5,000 jobs on a Pi 4, around the 1.5 s target. If it misses, use the design's fallback: one sealed blob for a job's
  content columns.
- **Before releasing accounts,** test on the Pi's test instance (port 8001) first. Back up its database with SQLite's
  backup API, sign up as the first admin, check that the existing data was claimed and encrypted, and only then touch
  the live app on 8040.
- **Pi test instance and the sign-in cookie:** browsers keep cookies per host name and ignore the port, so the live app
  (8040) and the test instance (8001) on one host name would have shared one cookie name and signed each other out. The
  cookie names now carry the port (`jh_session_8001`, `jh_session_8040`), so they coexist. Other apps on the same host
  name still receive the cookie, so give JobHuntPA a host name of its own (its own Tailscale name, for example) rather than
  the Pi's shared one.
