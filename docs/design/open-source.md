# Open-source JobHuntPA: the consolidated spec, and briefs for cloud sessions

**What the user asked for (2026-10-12 and 13):** "I want to open source this. I want an appimage and a windows
installer, as well as a manual mac install script like at present. There will also be the companion browser extensions
(which are yet to be published on the Firefox and Chrome extension stores). The app will be BYO LLM key. Add a way to
host it on your network as I currently do with the Pi." And, about building it: "I want to run this on claude cloud."

This file is the one place to build from. Part 1 is the spec: the user's final "Redefined spec" plus the answers they
gave in the conversation, with nothing from the earlier drafts that they later dropped. Parts 2 to 5 break it into
phases, one cloud session each, with a ready-to-paste brief for every phase (the layout of
`docs/design/remaining-phases.md`). Part 6 is what only the user or the local session can do. Part 7 describes the
import file of a data export (Phase E). Part 8 is the browser extension's contract with the app, `"api": 3` (Phase
I1), which Phase I2 builds the extensions against.

It changes direction from `docs/design/accounts.md`: that design turned the app into a service for many people with
memberships. This one makes it a free program each person runs for themselves. Encryption, sessions and the
shared-pool rules of that design stay in force; its sections on memberships (§8), the admin panel, two-step sign-in,
email (§16) and digests are what Phase A and B take out.

## Where things stand (2026-10-13)

- **App:** GitHub `main` and the Raspberry Pi both run `cb5d0fe`. `VERSION` still says 1.13.0, the last tag; the 59
  commits since then (accounts, encryption, ghost jobs, the extension's API, export) were never released. The
  repository is private and has no licence file.
- **Extensions:** `JobHuntPA-chrome-extension` (`44681d6`) and `JobHuntPA-firefox-extension` (`a4dc7b3`), both 1.2.0,
  same source, tests only in the Chrome repository. Each has one branch, `ccr-b6871b82-e9vz3j`. Both are public on
  GitHub, neither has a licence file, and neither is in a store yet.
- **Tests:** 1,525 in the app's suite, all passing.

---

# Part 1: the spec

## 1.1 Distribution and licence

- The app and both extensions are released under the **GNU General Public License, version 3** ("I want to use the
  GNU GPLv3 licence"; "make the two extensions GNU GPL v3 too").
- **Linux:** an AppImage. **Windows:** an installer, as now. **macOS:** the manual install script, as now.
- **Bring your own AI key.** There is no JobHuntPA service, no payment and nothing hosted by the author.
- The extensions stay companion programs. Publishing them in the Chrome and Firefox stores is the user's step
  (Part 6); the repositories must be ready for it.

## 1.2 The account

The user's answers: "Make it required." "Remove Stripe and Membership, the admin panel, two-step sign-in and email."
"There aren't likely to be any shared installs so ignore this and we'll address it if it becomes an issue."

- **Required.** First run creates the account, as today. Nothing in the app works signed out.
- **One per install.** Once an account exists, no other can be created: no sign-up page and no invitations. Without an
  admin panel nobody could manage other accounts, and anyone who can reach a hosted copy could otherwise sign up and
  spend the owner's AI key. An install that already has several accounts keeps them all; none can be added.
- **A name and a password.** No email address is asked for or stored as one, and no mail is ever sent. On an existing
  install the address an account signed in with stays valid as its sign-in name; it can be changed in Settings.
- **No two-step sign-in.** An account that had it on signs in with name and password after the update.
- **Forgotten password.** One of the recovery codes shown at first run is the way back in (as today). Without one, a
  reset tool that is run on the computer itself wipes the encrypted data and returns the app to first run. Nothing
  reachable from the web can do this. First run and the Settings tab say that **Download your data** is the safety
  net.
- **No roles.** Whoever is signed in may change every setting. Shared installs are not a goal of this spec.
- **Unchanged:** per-user encryption and sealed columns (accounts.md §2), sessions, CSRF, sign-in rate limits and
  lockout, the allowed-hosts check, background access for scheduled searches, workspaces, and the extension's keys.

## 1.3 What is removed

- **Memberships and Stripe:** the gate, free searches, grants, free periods, the billing pages and the Stripe
  settings.
- **Email, all of it:** email settings, the mail queue, address checks, password reset by email, notices.
- **Job digest emails.**
- **The admin panel**, the admin role and the admin key (the key pair that let admins read sealed user details).
- **Two-step sign-in.**
- **Sign-up for further accounts.**
- **The commute services:** Transport for NSW, TomTom and OSRM, with their keys (§1.8).
- **The "Hide possible ghost jobs" filter** (§1.10).

## 1.4 Settings that move to the Settings tab

They lived in the admin panel. "Re-add the LLM settings to the Settings tab for all users."

- **AI provider, model, key and reasoning levels.**
- **The public address** and extra host names (`PUBLIC_BASE_URL`, `JOBHUNT_ALLOWED_HOSTS`): hosting on a network
  depends on them (§1.6).
- **AI usage figures** for the install: totals, the last 7 and 30 days, by task. Useful to anyone paying for their own
  key.

## 1.5 First run, and where the data lives

"Prompt the user where they want their app data to live. As for the AI key, remove this from the terminal and ask at
setup." "By setup, I meant first run."

First run, in the browser, in this order:

1. **Where the data lives.** A folder path with a sensible default filled in. The page checks the folder can be
   created and written. A folder that already holds JobHuntPA data is used as it is, and the app goes to sign-in.
2. **Create the account:** name and password, then the recovery codes, with the note about Download your data.
3. **AI provider, model and key.** May be skipped; the app says which features wait for it.
4. **The existing walkthrough:** theme, then search setup.

Rules:

- **No questions in a terminal.** The setup scripts and the Windows setup ask nothing about AI. `--non-interactive`
  with the `LLM_*` environment variables keeps working for CI.
- **Defaults:** Linux `~/.local/share/JobHuntPA` (honouring `XDG_DATA_HOME`); Windows the `data` folder in the install
  folder, as now; macOS the `data` folder next to the scripts, as now.
- **Existing installs are not asked** and nothing moves: a `data` folder next to the app is used.
- **Service installs** set `JOBHUNT_DATA_DIR` and are not asked.
- The choice is remembered in a small pointer file in the per-user configuration folder. Settings (`.env`) and key
  files live in the data folder. Nothing except the pointer is written before the folder is chosen.
- The uninstaller backs up the chosen folder, wherever it is, and removes the pointer.

## 1.6 Hosting on your own network

"Documented steps plus service script for Linux."

- The app keeps listening on its own computer only. Other devices reach it through a proxy on the same computer that
  provides HTTPS: Tailscale's `serve` (what the user's Pi does) or any reverse proxy.
- **README:** the existing section becomes step-by-step instructions for both routes.
- **`service.sh`** (Linux): `install`, `remove` and `status` for a systemd user service that starts the app at boot,
  with options for the port and the data folder. The uninstaller removes it.

## 1.7 Export and import

**Export** keeps its layout. Two additions:

- A choice of file types, any subset, at least one: **Markdown, DOCX, ODT**. Each text file is written once per chosen
  type. Uploaded originals are included as today.
- One machine-readable file with a format version, holding everything needed to bring the data back: per workspace
  the search setup, sources, pins and starting points, documents (text, concise versions, their flags, and which
  original file in the zip belongs to each), jobs (the ad, status and its dates, notes, hidden, excluded, office and
  verdict, fit results, cover letters), cover letter blocks, which document is the letter template, chats and the
  schedule; plus the research on those jobs' companies. Never the
  password, keys, sessions, extension keys or the AI key.

**Import** (Settings tab):

- The user uploads an export zip; the page lists the workspaces in it with counts; the user picks which to bring in.
- Each one becomes a **new workspace**. Nothing is merged into an existing workspace, so nothing can conflict.
- A taken name gets " (imported 2026-10-13)" appended, with that day's date; if that is taken too, a counter:
  " (imported 2026-10-13, 2)".
- Imported jobs are the user's own content: a job whose address the shared pool already has is linked to it, anything
  else is a private job. Import never writes to the pool.
- Company research is taken only where the install has none, or older.
- A file from a newer version of the format is refused with a plain message. So is an export with no import file,
  saying which version writes one.

## 1.8 Locations and commute links

"Users can set as many locations as they please. The pin map stays." "Allow the user to choose from a list of links
from all their set home and commute locations. Remove arrive-by and leave-at times. These can be customised by the
user in Google Maps after the fact." "Change mapping and routes to prefilled link to Google Maps for each location and
commute type."

- **Pins and the pin map stay as they are.** They drive the location filter and the distance. Sorting by distance
  stays.
- **Starting points, any number.** Each has a label, a place (a pin, or typed text such as an address, a station or a
  bus stop) and a use: driving, public transport, or both. Every home pin is a starting point for both without being
  added. Today's "Commute from" stations become public-transport starting points.
- **On the job page:** for the chosen office, a list of links, one per starting point and commute type ("Drive from
  Home", "Public transport from Central Station"). Each opens Google Maps directions in a new tab
  (`https://www.google.com/maps/dir/?api=1&origin=…&destination=…&travelmode=driving` or `transit`).
- **The app sends nothing anywhere.** A hint says that opening a link shows Google both places.
- **Removed:** the commute services and their keys (setup prompts, `.env.example`, settings, the README section), the
  commute cache, the arrive-by and leave-at times, and the times and route maps on the job page.

## 1.9 Company research and offices

- **"Controversies" is renamed "News/Controversies"** everywhere it is shown, the export included.
- **"Research again", any time.** "Users can re-research the whole page any time": the profile's button researches
  the whole page again (the profile and both sections) whenever it is clicked, whatever its age. News/Controversies
  and Employee sentiment each get their own button too, which researches only that section, also at any time, and
  shows its own date. Age only decides what a batch of companies researches by itself: those with no research, a
  failed one, or one older than 30 days.
- **Offices.** Once a company is researched, the job page lists **all** its known offices, and the user picks the one
  the commute links go to. The pick is remembered for the job and places it for the location filter, as picking an
  office does today.

## 1.10 Ghost jobs

- **On the job page the details are collapsed.** The circle and its level stay visible; the reasons open on request.
- **A disclaimer:** this is worked out from the user's own searches only, and it indicates whether an ad may be a ghost
  job or a real position; it proves neither.
- **The "Hide possible ghost jobs" filter is removed.**

## 1.11 Documents

- **A warning, not a limit.** When the documents ticked for scoring come to more than 50,000 characters together,
  counting the version the AI actually gets, the Documents tab says so: it is a lot to send with every request. It
  points to **Create summary**.
- **Two labels change:** "Other (supporting)" becomes "Other/supporting", "Preferences (private)" becomes
  "Preferences". Stored kinds don't change. The hint that cover letters never read preferences stays.

## 1.12 Jobs

- **Scheduled searches are off by default** for a new workspace. Existing workspaces keep their setting.
- **Merged ads:** "link company page and prefer the fuller ad". The listed copy is still the fullest one. When any
  copy in the group is on the employer's own site, the card's and the job page's main link goes there; the others stay
  under "Also on".
- **Applied date.** When a job is moved to Applied the date is stored. The card shows it while the job is in Applied.
  A job with no stored date shows nothing ("if unavailable, don't show"). Moved away and back, it is the latest move.

## 1.13 Tasks tray

"Show ongoing LLM tasks in a Tasks tray with a number icon."

- A button in the header with a number: how many AI tasks are running. Opening it lists them: what, for which job or
  company, since when, and progress where known.
- Tasks: search runs and their scoring, re-scoring, company research, office look-ups, cover letters, chat answers,
  document summaries and concise versions, title suggestions, and the AI mappings of §1.15.
- It survives a page reload: the list comes from the server.
- **No cancelling.** The user left it out on purpose.

## 1.14 Careers pages in bulk

"On import, add new sources, leave existing, ignore duplicates." "Show the user the list they imported in a clean way
and allow them to remove items from the list prior to import. Flag potentially malicious or dodgy ones with the user
before ever making a request to them."

- **"Bulk add"** opens a dialog that shows the format and takes pasted text or a file.
- **Format:** CSV with the columns `company,url`. A header row is optional. Standard quoting, because company names
  contain commas. UTF-8.
- **Preview before anything is fetched.** The rows are shown as a clean list; each can be removed. No request of any
  kind, not even a name look-up, is made until the user confirms.
- **Flags.** A row is marked "unusual" with the reason when its address: is not a web link; is a raw IP address; is on
  the local network or this computer; carries sign-in details; is a link shortener; has a lookalike or encoded name;
  uses plain `http`; or names an odd port. Flagged rows start unticked. Local-network and non-web addresses cannot be
  added at all. The flags judge the address's shape only, and the dialog says so: it is not a safety verdict.
- **On confirm:** each row goes through the existing way of adding a source. New ones are added, ones the workspace
  has are left, repeats in the file are ignored, and every row gets a result line.
- **"Export sources"** downloads the workspace's sources in the same format, "so others can import the sources".

## 1.15 The browser extension, and AI mappings

- **It connects to the user's own app**, as it already can: an address and a key made in Settings. The address field
  is prefilled with the local app's.
- **A mapping for any job site.** When the extension can't find a job's title and description on a page, the popup
  offers to work the site out. The app asks the AI which parts of the page hold the title, company, location, salary,
  employment type, work mode, date and description. The result is shown to the user as the job it found; once they
  accept, the mapping is saved for that site and used from then on. When a saved mapping stops finding a title, the
  popup offers to work it out again.
- **A mapping for any careers page.** Adding a careers page as a source first tries what the app already knows. When
  that finds no jobs, the same step works out where the page lists them. Before the source is saved, the app reads the
  page **itself** with the mapping and shows the jobs it found: the test is the app's reading, not the browser's.
- **All AI traffic goes through the app.** The extension holds no AI key.
- **What leaves the browser** is a trimmed outline of the page: element names, their attributes and short text
  samples, capped in size. No scripts, styles or form values.
- **A mapping is data, never code:** selectors and attribute names, checked for shape by the app and applied by the
  extension with the browser's own selector functions.
- **The extension still never clicks, scrolls, turns pages or fetches on a job site by itself.**

## 1.16 Not part of this spec

Raised in earlier drafts and dropped by the user: a paid server and a provided AI key; a shared research server; a
shared ghost-job dataset; company accounts; HERE and a self-hosted Nominatim; token allocations and staff permissions;
cancelling AI tasks; refining the "talent pool" ghost sign; hiding chat; limits on searches, documents or stations;
removing workspaces; removing Distance from sorting.

Also unchanged: the public Nominatim server for placing jobs, the OpenStreetMap pin map, the scraping rules, the
shared-pool rules within an install.

## 1.17 Readings and open points

Places where this file had to interpret, for the user to correct:

1. **Starting points (§1.8).** The spec said "one location for driving … a separate location for public transport";
   the later answer said "as many locations as they please". This file follows the later answer and gives each
   starting point a use.
2. **Licence wording (§1.1).** "GPLv3" is taken literally as version 3 only (`GPL-3.0-only`), not "version 3 or
   later".
3. **The name.** Early drafts renamed the app "Easy Job Searcher". The final spec does not, so nothing is renamed.
4. **Installs with several accounts (§1.2)** keep them; nobody is locked out by the update.

Suggested, not decided, and in no brief: a "new version available" notice (an AppImage does not update itself);
making the geocoder's address a setting; `CONTRIBUTING` and `SECURITY` files.

---

# Part 2: order, and what can run in parallel

```
A (billing, digest, email, two-step out) ── B (admin out, one account, settings, reset tool)
     ┌── D (small changes) ── C (commute links)
B ───┼── E (export types, import)
     └── F (careers pages in bulk)
then ┌── G (tasks tray)
     └── H (first run, data folder, packaging, service script)
then I1 (AI mappings: app) ── I2 (AI mappings: extensions) ── J (open-source preparation, final review)
```

- **Wave 1:** A, alone. **Wave 2:** B, alone. They take out about half the backend and touch the same files.
- **Wave 3, in parallel:** C, D, E and F. C and D both change the job page: merge D first and rebase C.
- **Wave 4, in parallel:** G and H.
- **Wave 5:** I1, then I2 (I2 needs I1's contract).
- **Wave 6:** J.

# Part 3: how each cloud session works

1. Start the session on the repository and check out the work branch:
   `git fetch origin wip/open-source && git checkout -b cloud/os-<x> origin/wip/open-source`
   (for I2 and the extension part of J: the extension repository, from `ccr-b6871b82-e9vz3j`).
2. Paste the shared rules, then the phase's brief.
3. The session pushes `cloud/os-<x>`. It never pushes `main` or `wip/open-source`, never tags and never changes
   `VERSION`.
4. **Review before merging.** The local session fetches the branch, runs the full suite in a throwaway
   `git worktree`, reads the security-critical files, then merges into `wip/open-source`.
5. One wave is fully merged before the next starts.

# Part 4: shared rules (paste at the top of every brief)

> You're working on JobHuntPA (FastAPI + SQLAlchemy/SQLite, vanilla-JS frontend), on branch `cloud/os-<x>` made from
> `origin/wip/open-source`.
>
> **Read first:**
> - **CLAUDE.md, which is binding.** Setup and launch scripts keep working and follow every change; new settings go
>   only through `backend/app_settings.py` and `.env.example`; dependencies in `requirements.txt` with version bounds;
>   README updated for anything users see; scraping only through `backend/scraping/http.py`; every query scoped to a
>   workspace; duplicate jobs skipped; AI calls only through `backend/llm_providers.py`.
> - **`docs/design/open-source.md`:** Part 1 is the spec; your brief names its sections. §1.16 lists what is *not*
>   wanted: don't build it. §1.17 lists readings: follow them.
> - **`docs/design/accounts.md`** for what exists: §2 (encryption), §14 (shared pool), and the sections your brief
>   names.
>
> **Conventions:**
> - Sealed columns can't be filtered, sorted, grouped or DISTINCTed in SQL; decide in Python. Check ownership through
>   the plaintext `workspace_id` first. Thread pools use `backend.threads.submit`.
> - Never log user data: names, job text, documents, chats, page outlines, addresses, tokens, keys. Log the kind of
>   failure and an id. Extend `tests/test_log_leaks.py` when a feature handles personal text.
> - Anything that adds, replaces or drops a key changes everything in one transaction, then calls `db.scrub_wal()`.
> - Every new or changed endpoint states who may call it and gets a test for that, including the refusal.
> - Pages keep the Content-Security-Policy: no inline scripts, handler attributes, `<style>` or `style="…"`.
> - New UI meets **WCAG 2.2 AA**, works on phones from 320 px, uses the existing theme tokens and the live-region
>   helpers in `frontend/common.js`; extend `tests/test_accessibility.py`.
> - **Existing installs must survive.** The app must start and keep every job, document and setting on a database
>   made by the code at `origin/main`. Test that with a database built by that code, not only a fresh one.
>
> **Tests:** `python3 -m unittest discover -s tests -t .`; all must pass. `tests/__init__.py` isolates the database,
> `.env`, key file and uploads: keep it, and never point anything at a real `data/` folder. Mock the network and the
> AI.
>
> **Commits:** messages end with `Co-Authored-By: Claude <noreply@anthropic.com>` (keep any other trailer your session
> adds). Push your branch only.
>
> **Finish with a report:** the branch and commit; the changes, file by file; how it works from the user's view; the
> test result; any deviation from the spec and why; what a security reviewer should check first.

---

# Part 5: the briefs

## Phase A: memberships, digests, email and two-step sign-in out

> **Spec:** §1.2, §1.3. **Exists today:** accounts.md §8 (memberships) and §16 (email).
>
> **Take out, with their pages, settings, tests and README sections:**
> - Memberships and Stripe: `backend/billing.py`, `access.py`, `membership.py`, every `membership.gate` dependency,
>   free searches and their refunds, grants, the six-hourly sync; `frontend/billing.*`, `stripe-settings.js`,
>   `free-searches.js`; the `stripe` dependency. Scheduled searches no longer look at membership.
> - Digests: `backend/digest.py`, `frontend/digest.js`, the unsubscribe route.
> - Email: `mailer.py`, `mail_queue.py`, `mail_route.py`, `email_settings.py`, `email_templates.py`,
>   `account_email.py`, password reset by email and its pages, the address-check pages. Check each for a helper
>   something else still uses before deleting it.
> - Two-step sign-in: `backend/totp.py`, its step in sign-in, its Settings card; the `pyotp` and `segno` dependencies.
> - Their names in `.env.example` and `app_settings.py`.
>
> **Keep:** the recovery codes (the only "forgot password" route now; the sign-in page links to it), changing the
> password, background access, sessions, lockout.
>
> **Existing installs:** an account with two-step sign-in on must sign in with name and password afterwards. Delete
> stored two-step secrets and queued mail (they are sensitive), in one transaction, then `db.scrub_wal()`. Drop the
> removed tables in a migration.
>
> **Tests:** every endpoint that had the gate answers without it; a database from `origin/main` holding membership
> rows, grants, a two-step secret and queued mail starts and signs in.
>
> **Not yours:** the admin panel and anything admin-only (Phase B); commute keys (Phase C).

## Phase B: the admin panel out, one account, settings moved, the reset tool

> **Spec:** §1.2, §1.4. Needs Phase A.
>
> - **Take out** the admin panel (`backend/admin.py`, `frontend/admin.*`), the admin role and `require_admin`, the
>   admin key (`backend/admin_key.py`) and the sealed user details it protected, and sign-up for further accounts
>   (`backend/signup.py`, `frontend/signup.*`). The walkthrough for the first account (`welcome.*`) stays.
> - **One account per install:** once an account exists, every way of creating another refuses. Existing extra
>   accounts keep working.
> - **A name, not an address:** first run and sign-in ask for a name. Keep the look-up the account code uses today,
>   so an existing account's address still signs it in. The name can be changed in Settings with a recent password.
> - **Settings tab gains** the AI settings (`backend/llm_settings.py` is admin-only today), the public address and
>   extra host names, and the AI usage figures (from `llm_usage`). Signed in is enough for all of them.
> - **The reset tool:** run on the computer (`reset.sh`, a `.command` for macOS, a Start-menu entry on Windows). It
>   says what will be lost, asks the user to type a word to confirm, refuses while the app is running, then removes
>   the accounts, their encrypted data, uploads and key material in one transaction with `db.scrub_wal()`, and leaves
>   the app at first run. It keeps the settings, the pooled public ads and the company research. No endpoint does any
>   of this.
> - **Dropping the admin key** follows the key rule: one transaction, then `db.scrub_wal()`. The server key stays.
> - Update CLAUDE.md's conventions (no admin; who may call) and the README ("Your account" rewritten, "Sharing
>   JobHuntPA (admins)" gone).
>
> **Tests:** creating a second account is refused; every endpoint that was admin-only answers a signed-in account and
> refuses a signed-out one; the reset tool on a populated database, including the refusal without the confirmation.
>
> **Reviewer first:** account creation after first run; that nothing reachable over HTTP resets or wipes.

## Phase C: commute as Google Maps links

> **Spec:** §1.8, §1.9 (offices). Needs Phase B. Phase D is merged first; rebase onto it.
>
> - **Starting points** replace `commute_from` and the arrive-by and leave-at times in search setup: any number, each
>   with a label, a place (a pin or typed text) and a use. Home pins count without being added. Existing stations
>   become public-transport starting points.
> - **The job page** lists every known office of a researched company; the user picks one (stored as the office pick
>   is today). Under it, one link per starting point and commute type, built in the page with `encodeURIComponent`,
>   opening in a new tab with `rel="noopener noreferrer"`, and the hint about Google.
> - **Take out** the Transport for NSW, TomTom and OSRM code in `backend/commute.py`, the station look-up, the commute
>   cache (sealed, per user: drop it in a migration), `/api/commute/status`, the keys' prompts in
>   `installer/installer.py` (and its `--non-interactive` names), `.env.example`, `app_settings.py`, and the README's
>   "Commute times" section.
> - Pins, the pin map, geocoding, distance and the location filter are untouched.
>
> **Tests:** no endpoint here makes an outside request; links for names with commas, ampersands and non-Latin letters;
> a workspace with old stations and times loads and shows them as starting points.

## Phase D: the small changes

> **Spec:** §1.9 (the rename and the buttons), §1.10, §1.11, §1.12. Needs Phase B.
>
> - "Controversies" becomes "News/Controversies" wherever shown (`frontend/common.js`, the job page, the export, the
>   README).
> - "Research again" on News/Controversies and on Employee sentiment: each researches only its section and shows its
>   own date (`news_researched_at`, `sentiment_researched_at`). The profile's own button researches all three parts.
>   A request the user makes is never held back by age: today `research.plan` skips a section researched within a
>   day and a profile within 30. Keep those rules only for what a batch of companies researches by itself.
> - Ghost section on the job page: collapsed, with the disclaimer of §1.10. Remove the "Hide possible ghost jobs"
>   filter (`#filter-hide-ghost`, `state.filter.hideGhost`, the count text, the README).
> - Scheduled searches off by default for a new workspace.
> - The 50,000-character warning on the Documents tab, counting `Document.text_for_llm` of the ticked documents.
> - The two document labels.
> - Merged ads: the main link goes to the employer's own page when a copy is on one. "Employer's own" is what
>   `duplicates.Ranking` already decides (not a job board). The ranking itself doesn't change.
> - Applied date: a new sealed field set when a job moves to Applied; shown on the card; in the export.
>
> **Tests:** one per bullet; a profile and a section researched a minute ago are researched again on request, and
> a batch still leaves them alone; the applied date for a job moved away and back, and for a job with none.

## Phase E: export file types, and import

> **Spec:** §1.7. Needs Phase B. **Exists today:** `backend/export.py`.
>
> - **Export:** the choice of Markdown, DOCX and ODT (`python-docx` and `odfpy` are already dependencies), and the
>   versioned import file. Write down the file's format in this document as a new Part 7.
> - **Import:** an endpoint and a Settings card. Signed in, CSRF, password entered recently (as export asks). The zip
>   is read in memory and never unpacked to disk by its own names. Guard against oversized and over-compressed
>   archives, and take only the import file and the originals it names. Everything in it is someone else's text:
>   store it as data, escape it where shown.
> - New workspaces only, with the naming rule and its counter. Pool linking and the research rule as §1.7 says.
> - The log says that an import happened, with counts; never a name or any content.
>
> **Tests:** a round trip (export, import, compare); the name counter; a newer format version; an export with no
> import file; a zip with a path that climbs out; a zip that is too large when unpacked; signed out and no recent
> password refused. Add an import step to `tests/test_log_leaks.py`.

## Phase F: careers pages in bulk

> **Spec:** §1.14. Needs Phase B. **Exists today:** `backend/sources.py` (`POST /api/sources`, the dry run).
>
> - The dialog, the CSV reader (standard quoting, optional header, UTF-8), the preview with removal, and the flags.
>   The flags are worked out without any network use, a name look-up included. Put the rules in one function with a
>   test per rule.
> - Confirming sends the kept rows; each is added through the existing path, so the scraping rules and the checks a
>   single add gets apply. Per-row results: added, already there, repeated in the file, could not be read.
> - "Export sources" in the same format.
> - State who may call the new endpoints (signed in, the workspace's owner) and test it.
>
> **Tests:** names with commas and quotes; each flag; a local-network address refused outright; nothing is fetched
> before the confirmation; an existing source left untouched; export then import gives the same list.

## Phase G: the tasks tray

> **Spec:** §1.13. Needs wave 3. **Exists today:** `backend/tasks.py` tracks search runs only.
>
> - One registry for running AI work, with `GET /api/tasks` for the signed-in account: kind, what it is for, start
>   time, progress where known. It holds ids and kinds, not text.
> - Register: search runs and scoring, re-scoring, research (profile and sections), office look-ups, cover letters,
>   chat answers, document summaries and concise versions, title suggestions. Phase I1 adds the mappings.
> - The header button with the count, and the list. Count changes are announced politely to screen readers.
> - No cancelling (§1.16).
>
> **Tests:** each kind appears while running and goes when done or failed; one account never sees another's tasks;
> signed out refused.

## Phase H: first run, the data folder, packaging and the service script

> **Spec:** §1.5, §1.6, §1.1. Needs Phases B and C.
>
> - **The data folder:** `JOBHUNT_DATA_DIR`, the pointer file, the defaults of §1.5, and existing installs left where
>   they are. Nothing but the pointer is written before the choice, so the database must not be created early.
>   `.env` and the key files follow the data folder.
> - **First run** in the order of §1.5. The AI step replaces `configure_llm`'s prompts in `installer/installer.py`;
>   `--non-interactive` with environment variables still works.
> - **AppImage:** built by `.github/workflows/release.yml` on a tag, with Python and the packages inside, on the
>   oldest supported Ubuntu runner. The browser used for JavaScript-only careers pages is downloaded into the data
>   folder when first needed, after the user agrees; declined, those pages say what they need. Add a smoke test to
>   `ci.yml`: build, start without a browser, check health.
> - **Windows:** the setup asks nothing about AI; otherwise as now.
> - **macOS:** the scripts as now. Update the README's steps for current macOS, and mark them for the user to check on
>   a real Mac.
> - **`service.sh`** for Linux, and the README's hosting steps (§1.6).
> - **Uninstaller:** the data folder wherever it is, the pointer, the service.
>
> **Tests:** a fresh start with no pointer; a chosen folder that already holds data; an existing `data` folder next to
> the app; `JOBHUNT_DATA_DIR` set; CI green on Linux, macOS and Windows.

## Phase I1: AI mappings, the app's side

> **Spec:** §1.15. Needs Phases F and G. **Exists today:** `backend/extension.py` (keys, `KEY_ENDPOINTS`, the
> `"api": 2` contract), `backend/adapters/generic.py`, `backend/sources.py`.
>
> - Endpoints, signed in or an extension key: work out a mapping from a page outline; read, replace and delete the
>   saved mapping for a site. `status` says `"api": 3`.
> - The AI is asked through `llm_providers.chat` and must answer with selectors only. Check the answer's shape:
>   strings, length caps, a plain character set. Cap the outline's size. The outline is user data: never logged.
> - Careers pages: a mapping can be stored with a source; the generic adapter uses it when it reads the page through
>   `backend/scraping/http.py`. The dry run with the mapping is what the user confirms.
> - Each mapping task shows in the tasks tray.
> - Write the contract down in this document as a new Part 8; Phase I2 builds against it.
>
> **Tests:** access with a key, a session, and neither; an answer that isn't selectors is refused; an oversized
> outline is refused; a source with a mapping finds postings in a mocked page; the log-leak step.

## Phase I2: AI mappings, the extensions

> **Spec:** §1.15, and Part 8 of this file in the app's repository (the contract from Phase I1). Work in
> `JobHuntPA-chrome-extension`, branch `cloud/os-i2` from `ccr-b6871b82-e9vz3j`; the Firefox repository gets the same
> source afterwards.
>
> - Build the trimmed outline of the open page: element names, attributes, short text samples; no scripts, styles or
>   form values; capped.
> - The popup: offer to work the site out when no title and description are found; show the job the mapping finds;
>   save on acceptance; use a saved mapping; offer again when it stops finding a title.
> - "Add as a source" on a careers page, showing the jobs the **app** found before saving.
> - A mapping is applied only with the browser's selector functions. Nothing from it is ever run as code.
> - The extension still never clicks, scrolls, turns pages or fetches on a job site.
> - Version 1.3.0. Update the mock server to the contract and extend the unit and end-to-end tests
>   (`tests/README.md` says which browser to use).
>
> **Report as well:** every new permission the manifest asks for, and why.

## Phase J: open-source preparation and the final review

> **Spec:** §1.1. Needs everything else. Three repositories.
>
> - **Licence:** add the official GPLv3 text as `LICENSE` to the app and both extensions. Fetch the text from
>   gnu.org; don't write it from memory. State `GPL-3.0-only` in each README and manifest.
> - **Other people's code:** list every dependency with its licence (from the packages' own metadata) in
>   `THIRD-PARTY.md`, and report anything that may not be combined with GPLv3.
> - **History:** scan all three repositories' full history for secrets and personal data. **Report** what you find.
>   Do not rewrite history.
> - **README** for a public reader: what it is, installing on each system, bringing your own AI key, hosting on a
>   network, the extension, the scraping rules, the licence, how to report a security problem.
> - **Store listings:** in each extension repository, drafts of the description, the reason for each permission, and
>   a privacy statement that matches what the code does.
> - **Final review:** who may call every endpoint; the log-leak tests; accessibility tests for every new page; a
>   database from `origin/main` upgraded end to end; CI green on all three systems. Fix what is small, report what
>   is not.

---

# Part 6: for the user and the local session (not for cloud agents)

- **Before wave 1:** push this file and create `wip/open-source` from `main`.
- **Only the user can:** correct the readings in §1.17; make the app's repository public (after Phase J's history
  report; the extensions' are public already); open the store developer accounts and submit the extensions; check the
  macOS steps on a real Mac.
- **Deploying to the Pi:** the update removes two-step sign-in, email and the admin panel from a live install. Take a
  Download-your-data export and a copy of the Pi's data folder first, with the user's go-ahead. Then the usual
  routine, and check: sign-in with the existing account, the jobs list, the Settings tab's AI settings and public
  address.
- **Releasing:** raise `VERSION` (2.0.0 fits: it removes features) and push the tag; `release.yml` builds the Windows
  programs and the AppImage.

---

# Part 7: the import file

Every data export (§1.7, `backend/export.py`) holds one machine-readable file beside the text files:
`JobHuntPA import file.json`, at the top of the export's folder (`JobHuntPA export 2026-10-13/JobHuntPA import
file.json`). **Settings > Import from an export** (`backend/data_import.py`) reads it and nothing else, except the
original files it names. `backend/import_file.py` writes it and holds the reader's typed, bounded model of it.

## The envelope

A JSON object (UTF-8):

| Field | Meaning |
|---|---|
| `format` | always `"jobhuntpa-import"` |
| `format_version` | an integer, `1` for this description. An import refuses a higher number with a plain message ("made by a newer JobHuntPA … update JobHuntPA"). |
| `app_version` | the exporting JobHuntPA's `VERSION` |
| `exported_at` | when the export was made |
| `workspaces` | a list of workspaces, below |
| `companies` | the research on the companies of those workspaces' jobs, below |
| `site_mappings` | the account's saved job-site layouts (Part 8): `[{site, mapping, updated_at}]`, by site; `mapping` is a Part 8 mapping of kind `job` |

An export without this file was made by a JobHuntPA before the first version that writes one (2.0.0,
`import_file.FIRST_APP_VERSION`); an import says so. Times are ISO 8601 in UTC with a `Z`
(`2026-10-13T09:30:00Z`); an import also takes an offset. `null` (or a missing field) means "not set" everywhere.
Fields a reader doesn't know are ignored, so a later version of the same `format_version` may add fields; a change
that an older reader would misread raises `format_version`.

**Never in it:** the password or anything derived from it, encryption keys, recovery codes, sign-in sessions,
browser extension keys, the AI key or any other setting in `.env`, the account's sign-in name, caches and logs.

**References.** Inside one workspace, jobs, documents and sources have a `ref`: a number from 1 that is local to the
file (not a row id). Other fields point at them by that number.

## A workspace

| Field | Meaning |
|---|---|
| `name` | its name (an import renames it when the name is taken: §1.7) |
| `created_at` | when it was made |
| `search_setup` | `null`, or the object below |
| `pins` | `[{ref, label, kind, lat, lng, radius_km}]`; `kind` is `home`, `hybrid` or `onsite`; `ref` is the pin's number in this file |
| `sources` | `[{ref, label, url, type, enabled, mapping}]`: careers pages and feeds; `url` is http(s); `mapping` is the page's layout the AI worked out (a Part 8 mapping of kind `careers`), or null |
| `schedule` | `{enabled, times: ["10:00", …], days: [0-6, Monday = 0], skip_holidays, state: "NSW" …}` |
| `documents` | below |
| `letter_blocks` | below |
| `letter_template` | `null`, or the `ref` of the document cover-letter downloads are filled into (below) |
| `jobs` | below |
| `chats` | below |

**`search_setup`:** `titles`, `keywords_include`, `keywords_exclude`, `seek_locations` (lists of text);
`salary_floor`, `max_office_days` (integers or null); `remote_aus_ok`, `remote_global_ok`, `allow_hybrid`,
`allow_onsite`, `seek_enabled` (true/false); `dealbreakers: {industries, keywords}`; and **`starting_points`**: the
search setup's own starting points (§1.8; the home pins count without being listed), each `{label, pin, place, use}`:
`pin` is the `ref` of one of the workspace's `pins` and `place` is null, or `pin` is null and `place` is typed text;
`use` is `driving`, `transit` or `both`. A search setup saved before starting points came in has its "Commute from"
stations written as the public-transport starting points the app shows for them; a starting point whose pin was
deleted is left out. A reader also takes a plain text item (as the first files wrote the stations) as a
public-transport place, and leaves out a `pin` the file's `pins` don't have.

**`documents`:** `{ref, filename, filetype, kind, text, concise_text, use_concise, use_for_scoring, concise_model,
concise_at, created_at, updated_at, summary_of, original}`. `filetype` is the upload's extension (`.pdf` …), or
`note` / `summary` for a document written in the app; `kind` is `resume`, `cover_letter`, `preferences`, `other`
or `letter_template` (a .docx or .odt letterhead with a `{letter}` marker); `summary_of` lists the `ref`s a summary was made from; `original` is the path of the uploaded file inside
the export's folder (`Workspaces/<workspace>/Documents/Original files/<name>`), or null. The original files are
plain bytes in the zip, exactly as uploaded.

**`letter_blocks`:** the paragraphs cover letters are built from (one list per workspace, in
letter order), each `{kind, title, text, tags, position, created_at, updated_at}`: `kind` is `opening`, `body` or
`closing` (default `body`); `title` and `text` are text; `tags` is a list of at most 30 short texts (an import trims
them and drops empty and repeated ones); `position` is the block's order within its kind. A file without the list
(made before blocks existed) brings in none. Blocks have no `ref`: nothing points at them. They are also written as
`Workspaces/<workspace>/Cover letter blocks.md` when there are any.

**`letter_template`:** the `ref` of the document (of kind `letter_template`) that the workspace's cover-letter
downloads are filled into, or `null` for the plain layout. An import points the new workspace at that document's copy
when it came in with its original file; otherwise the workspace uses the plain layout. Anything but a number (a
development build wrote an object here) counts as `null`.

**`jobs`:** one entry per job, merged copies included:

| Field | Meaning |
|---|---|
| `ref` | the job's number in this file |
| `origin` | `pool` (linked to an ad in the exporting install's shared pool) or `private` |
| `ad` | the ad as the workspace saw it: `url`, `source`, `external_id`, `company`, `title`, `location_text`, `country`, `work_mode`, `employment_type`, `office_days`, `salary_min`, `salary_max`, `salary_text`, `description`, `description_hash`, `detail_status`, `detail_fetched_at`, `posted_at`, `industry` |
| `own` | the names of the `ad` fields that were the workspace's own values (an uploaded full ad, details from a merged copy); all of them for a private job |
| `status` | `to_review`, `shortlisted`, `applied`, `interviewing`, `rejected` or `not_interested` |
| `first_seen`, `last_seen`, `closed_at` | when the job was first linked to the workspace, last listed by its search, and when the ad closed |
| `applied_at` | when the job was last moved to Applied (§1.12), or null when that isn't stored |
| `hidden` | the person hid it |
| `included_by_user` | the person keeps it in their list (added it themselves through the browser extension or a saved page of the one job, or ticked its *Always include*), so the search filters never leave it out; false when left out of the file |
| `excluded_reason` | why the search filters left it out, or null (always null for a job the person added) |
| `location_verdict` | the person's call on the location: `ok`, `too_far` or null |
| `office` | `{text, lat, lng, source, checked_at}`: where the office is (`source`: `ad`, `company` or `user`), or null |
| `lat`, `lng`, `distance_km` | where the job was placed for this workspace |
| `duplicate_of` | for a merged copy, the `ref` of the job it was merged into |
| `source` | the `ref` of the source that found it |
| `fit` | `{status: ok/error, score: 0-100, evidence: {summary, requirements, gaps}, error, profile_hash, model, created_at}` or null |
| `cover_letter` | `{text, instructions, edited, model, reasoning_effort, created_at, updated_at}` or null |

**`chats`:** `{title, created_at, updated_at, jobs: [job refs], messages: [{role: user/assistant, content, sources:
[{title, url}], web_search, model, created_at}]}`.

## Companies

`companies` holds the shared research on the companies of the exported jobs: `{key, name, status, official_name,
is_recruiter, business_model, ownership, headquarters, controversies, controversy_note, news_researched_at, sources,
employee_count, glassdoor_url, sentiment, sentiment_researched_at, research_version, offices, offices_checked_at,
offices_cities, unverified_dropped, error, model, researched_at}`. `key` is the company's name in lower case with
single spaces (`company_key`). The structured values have the shapes research and the office search write
(backend/research.py, backend/offices.py), since the app reads them by these keys:

| Field | Shape |
|---|---|
| `controversies` | `[{title, year, summary, sources: [page]}]`, at most 50 |
| `sources` | `[page]`: the pages behind the business facts, at most 200 |
| `sentiment` | `{summary, rating, positives: [text], negatives: [text], sources: [page]}` or null |
| `offices` | `[{name, address, url, city?}]` or null (none looked up), at most 200 |
| `offices_cities` | `[text]`: the cities the office search looked in (lower case), or null |

A `page` is `{title, url}` with an http(s) `url`. A `year` or `rating` written as a number is taken as text.

## What an import does with it

- Each picked workspace becomes a new workspace of the signed-in account, with the naming rule of §1.7.
- A source whose address is not a web link or is on the local network or this computer (Phase F's blocking flags,
  `source_bulk.BLOCKING`) is left out, as a single add refuses it. A source's `mapping` and each `site_mappings`
  item are checked as Part 8 checks a mapping sent to the app; one of another shape is taken as none (the source
  still comes in) or left out, never repaired. A site mapping is taken only for a site the account has none for
  (its own is kept), up to the account's limit (500); the answer's `site_mappings` says how many came in.
- A job whose `ad.url` the importing install's pool already has is linked to that posting, and only its `own`
  fields are set on top; any other job is a private job with the whole `ad` and its `closed_at`. Nothing is written
  to the pool.
- A company's research is taken only for the companies of the imported jobs, and only where the install has no
  research on it or older research (`researched_at`). The research rows are shared by every account on the install,
  so only the shapes above come in: an item of another shape (a page without a web address, an office without an
  address, a controversy without a title) is left out, and a `sentiment` of another shape is taken as none. Offices
  the install already knows are kept, with the file's added by address; `offices_cities` adds to the install's;
  `research_version` is at most the importing install's. The same goes for a fit result's `evidence`
  (`{score, summary, requirements: [{point, must_have, matched, evidence: [{bullet, sub_bullets: [text]}]}], gaps:
  [text]}`, as backend/scorer.py writes it): a requirement without a `point` or evidence without a `bullet` is left
  out, and evidence of another shape is taken as none.
- No time is later than the moment of the import: one in the future (which no export holds) is taken as now, so
  imported research can't look newer than anything researched later.
- Values are checked against the types and limits above (lengths, ranges, the listed words); a file that breaks them
  is refused as damaged, naming only where (`workspaces.0.jobs.12.status`), never the value. Addresses that are not
  http(s) are dropped wherever a page makes a link of them: a job's address, sources, a chat answer's pages and
  the links in company research. Everything else is stored as data and shown escaped, like any other job or
  document.
- The limits take in whatever an export can hold (at most 200 MB of files before compression, the import file and
  every text file in every type included: `export.MAX_EXPORT_BYTES`): 250 MB packed, the import file at most 200 MB
  unpacked, the import file and the originals at most 220 MB unpacked together, and no entry over 1 MB unpacking to
  more than 200 times its packed size; a zip with an entry whose path leaves its folder is refused whole. A
  document's `text` and `concise_text` may be as long as an export can hold (an uploaded .txt keeps all of its text,
  and a DOCX or PDF can unpack to more text than its upload); an ad, a letter or a message at most 2,000,000
  characters. The whole file holds at most 200,000 jobs, documents, letter blocks, chats, chat messages, sources, pins and companies
  together (each becomes objects in memory before anything is written). An upload still arriving after ten minutes
  plus the time the largest zip needs at 100 KB/s is cut off. One import runs at a time.

---

# Part 8: the extension contract (api 3)

Everything the Chrome and Firefox extensions may call on the person's own JobHuntPA, as of `"api": 3` (Phase I1,
`backend/extension.py`, `backend/mappings.py`, `backend/mapping_format.py`, `backend/sources.py`). Phase I2 builds the
extensions against this text alone. Earlier descriptions (docs/design/accounts.md §19, the extensions' README) are
history; where they differ, this part holds.

## 8.1 Connecting and every request

- **The server address** is what the person pastes (Settings > Browser extension shows it: `location.origin` of the
  app). Every path below is relative to it.
- **The key:** `jhpa_` and 43 base64url characters (`^jhpa_[A-Za-z0-9_-]{43}$`), made in Settings, shown once.
  Every request sends `Authorization: Bearer <key>` and `Accept: application/json`, no cookies
  (`credentials: "omit"`), and `Content-Type: application/json` with a body. No CSRF token and no Origin check apply
  to a key; the server sends no CORS headers (the extension's host permission for the server is what lets it call).
- **The workspace:** `X-Workspace: <id>` (an integer from `status`) on every request that works on one (marked
  *workspace* below). Without it, the account's first workspace is used; an account with no workspace answers
  404 `{"detail": "You have no workspace yet. Create one first."}` (no `code`): say to make one in JobHuntPA. A workspace that isn't the account's answers 404 `{"detail": "Workspace <id> not found"}` (no `code`):
  the extension asks the person to pick one again. A value that isn't an integer is refused by the web framework
  (422 with `detail` a list, 8.2).
- **A key reaches only these endpoints** (method and path; send each path exactly as written, without a trailing
  slash: the key check ignores one, but the app then answers 404 or 405); anything else answers 403 `token_scope`,
  whatever the key:

  | Method | Path | Since |
  |---|---|---|
  | GET | `/api/extension/status` | 1 |
  | POST | `/api/extension/preview` | 1 |
  | POST | `/api/extension/jobs` | 1 |
  | POST | `/api/extension/jobs/batch` | 2 |
  | GET | `/api/sources` | 1 |
  | POST | `/api/sources` | 1 (the `mapping` field: 3) |
  | POST | `/api/sources/{id}/test` | 1 |
  | POST | `/api/extension/work-out` | 3 |
  | GET | `/api/extension/mappings` | 3 |
  | GET, PUT, DELETE | `/api/extension/mappings/{site}`, `{site}` matching `[A-Za-z0-9.-]{1,253}` | 3 |

  A signed-in browser session may call the same endpoints (with its CSRF token on POST, PUT and DELETE); the app's
  own pages use the mapping ones in Settings.
- **Before using an endpoint of a version**, the extension reads `status` and checks `api` (an integer) is at least
  that version; a server with a lower one answers the newer paths 403 `token_scope` (with a key) and the extension
  says to update JobHuntPA.

## 8.2 Errors

Every refusal the app makes itself is JSON `{"detail": <a sentence for the person>, "code": <a word>, "field"?: <the
request's field it is about>}`. `detail` is shown as it is. Some answers carry no `code` (a plain `{"detail": ...}`):
the 404 for a workspace, 409 and 422 from `POST /api/sources` (see 8.5). A body the web framework itself refuses
before the app reads it (a missing required field of `POST /api/sources`, or an `X-Workspace` that isn't an integer
on any *workspace* endpoint) answers 422 with `detail` a list: treat a `detail` that isn't a string as none and show
a general message.

| Status | `code` | When |
|---|---|---|
| 400 | `ai_not_set_up` | work-out: the AI provider isn't set up in JobHuntPA (Settings) |
| 401 | `token_invalid` | the key doesn't work: disconnected, expired, or copied wrong. Reconnect in the options |
| 401 | `setup_required` | JobHuntPA isn't set up yet (no account) |
| 401 | `signed_out` | no key and no session |
| 403 | `token_scope` | an endpoint a key can't use (or a server older than the version that added it) |
| 404 | `not_found` | `GET`/`DELETE /api/extension/mappings/{site}`: no mapping saved for that site |
| 404 | (none) | another account's workspace, an account with no workspace, or a source id that isn't the workspace's (`{"detail": "Source <id> not found"}`) |
| 409 | `too_many_mappings` | `PUT` of a new site past the account's 500 saved mappings |
| 409 | (none) | `POST /api/sources` without a mapping, for a URL the workspace has (`"That URL is already a source"`) |
| 413 | `too_large` | a body or a value over its limit (`field` names the value when one is) |
| 413 | `outline_too_large` | work-out: the outline is over a cap of 8.6 (`field: "outline"`) |
| 422 | `invalid` | a wrong type, an unknown field, a value outside its list, a missing value (`field`) |
| 422 | `invalid_mapping` | a mapping sent to the app isn't in the shape of 8.7 (`field: "mapping"`) |
| 422 | `not_worked_out` | work-out: the AI's answer wasn't a mapping in the shape of 8.7. Say the page couldn't be worked out; trying again may help |
| 422 | `refused_address` | a careers page on the local network or this computer, or not a web link (`field: "url"`) |
| 423 | `encrypting` | JobHuntPA is encrypting the data (after an update); also `state`, `done`, `total`. Try again later |
| 429 | `rate_limited` | too many requests with keys that don't work from this address; `Retry-After` (seconds) |
| 429 | `busy` | work-out: one is already running for this account; `Retry-After: 5` |
| 502 | `ai_failed` | work-out: the AI provider failed; `detail` says what the person can do |
| 504 | `timed_out` | work-out: it ran out of its 10 minutes (8.6). Say so; trying again later may help |

## 8.3 Status

`GET /api/extension/status`: `{"ok": true, "api": 3, "app_version": "1.13.0", "label": <the key's name>,
"expires_at": "2027-10-09T12:00:00Z", "workspaces": [{"id": 1, "name": "My job search"}, ...],
"default_workspace": 1}`. `label` and `expires_at` are null for a session. `default_workspace` is the account's first
workspace (the one used without `X-Workspace`), null when it has none (`workspaces` is then `[]`). `expires_at` moves
on with use (60 days without use, a year at most). `expires_at` here and the mappings' `updated_at` (8.8) are ISO
8601 UTC with `Z` and no fraction; a source's `last_run` (8.5) is not.

## 8.4 Jobs (unchanged from api 2)

**The job a request carries** (`POST /api/extension/preview` and `POST /api/extension/jobs`, *workspace*):

```json
{"url": "<the page's address>", "canonical_url": "<link rel=canonical, or empty>",
 "job": {"title": "", "company": "", "location_text": "", "salary_text": "", "employment_type": "",
         "work_mode": "", "posted_at": "", "url": "", "description": "", "description_from": "", "method": ""}}
```

- `url` (required) and `canonical_url`: http(s), at most 2,048 characters. `job` and each of its fields may be left
  out; null counts as absent. Limits (characters): `title`, `company`, `location_text`, `salary_text` 500;
  `employment_type` 100; `posted_at` 40, an ISO date or date-time (`2026-10-01`, `2026-10-01T09:30:00+10:00`); `url`
  2,048; `description` 100,000, plain text. `work_mode`: `onsite`, `hybrid`, `remote`, `unknown` or `""`.
  `description_from`: `selection`, `structured`, `site`, `main`, `body` or `""`. `method`: `jsonld`, `site`, `page`
  or `""` (a page read with a mapping of 8.7 says `site`). Any other field is 422 `invalid`.
- The body is at most 512 KB (524,288 bytes): past that 413 `too_large` before anything is read.
- **Preview** stores nothing: `{"url": <the address a save would store>, "existing": {"id", "title", "status",
  "link"} | null}`.
- **Save** needs a title (422 `invalid`, `field: "title"`): 201 for a new job, 200 for the workspace's job with that
  address brought up to date: `{"job_id", "created", "title", "company", "link", "excluded_reason",
  "filter_warnings"}`. `link` is relative to the server (`/job.html?id=12&ws=1`). A job the extension adds is the
  person's own pick, so the filters never leave it out of the Jobs list: `excluded_reason` (why they would) is null,
  and `filter_warnings` lists the rules of the person's filters the job doesn't meet, as the Jobs tab says them under
  the job's ⚠️ (`["dealbreaker keyword: crypto", "salary below floor ($90,000 < $120,000)"]`), or `[]`. A job the
  workspace had that the filters left out is listed from then on too. That is the job's *Always include*, ticked; the
  person unticks it in the app (`PATCH /api/jobs/{id}/included` `{"included": false}`, a signed-in browser only, not
  an extension key), and the filters decide on the job again.
- **The address stored:** a SEEK job (`/job/<digits>` or `?jobId=<digits>` on `seek.com.au`, `seek.co.nz`,
  `seek.com` or a subdomain) as `https://www.seek.com.au/job/<id>`; else `job.url`, then `canonical_url`, when on
  exactly the same host and port as `url`; else `url`.

**A page of results** (`POST /api/extension/jobs/batch`, *workspace*): `{"url": <the results page>, "jobs": [{"url",
"title", "company", "location_text", "salary_text", "employment_type", "work_mode", "posted_at", "industry",
"description"}, ...]}`; 1 to 50 jobs (413 `too_large`, `field: "jobs"` past 50); each job's `url` on the results
page's own host and port (relative is fine) and a title, else the whole request is 422 `invalid` (`field` `url` or
`title`); `industry` at most 200 characters, `description` (the card's summary, plain text) at most 5,000; the rest
as for a job. It only adds: a job the workspace has keeps its ad as it is. Every job sent was ticked by the person, so
each is listed whatever the filters say, as for a save. The answer: `{"added", "existing", "jobs": [{"url", "job_id",
"created", "title", "company", "link", "excluded_reason", "filter_warnings"}, ...]}` in the order sent.

## 8.5 Company sources

- `GET /api/sources` (*workspace*): `[{"id", "label", "url", "enabled", "last_run", "last_count", "last_error",
  "last_method", "mapping"}]`. `last_run`: when a search last read it, ISO 8601 in UTC **without** a zone suffix
  (`2026-10-13T21:04:05.123456`; it may have a fraction), or null: read it as UTC (append `Z` before `new Date()`).
  `mapping` (api 3) is the source's careers mapping (8.7) or null. It is also null
  for a saved layout the app no longer accepts (such as after an update tightened the rules of 8.7): treat it as
  none (searches then read the page the usual ways) and offer to work the page out again.
- `POST /api/sources` (*workspace*): `{"url", "label"?, "mapping"?}`. `url` without a scheme gets `https://`. 201
  with the source as listed. A URL the workspace has: 409 (no `code`), unless `mapping` is given: then that source
  gets the mapping and the answer is 200 with the source and `"updated": true`. `mapping` (api 3, optional) is a
  careers mapping (8.7), checked again (422 `invalid_mapping`). A URL that isn't http(s): 422 `{"detail": "Not a
  valid URL: ..."}` (no `code`); on the local network or this computer (a raw private or loopback IP, `localhost`,
  a single-label name, `.local`, `.lan`, `.internal` and the like, judged by its shape): 422 `refused_address`
  (`field: "url"`), since api 3 for every way of adding a source. The address is checked before the mapping: a
  local address with a bad mapping is 422 `refused_address`.
- `POST /api/sources/{id}/test` (*workspace*): the app reads the page now, through its scraping rules (robots.txt,
  crawl delay), and stores nothing: `{"ok": true, "method": <how it was read>, "count", "sample": [{"title",
  "location", "url"}] (at most 8), "notes": [<text>]}` or `{"ok": false, "error": <why>, "notes": [...]}`. It can
  take a minute.

## 8.6 Working a page out

`POST /api/extension/work-out` (*workspace*: the tasks tray of that workspace shows it while it runs, as "Working out
a job site" or "Working out a careers page"). One at a time per account (429 `busy`).

**How long it takes: at most 10 minutes**, all of it together. What takes the time: for a careers page, the app's
reading of it the usual ways first (up to 15 listing pages at the site's crawl delay, 2 s a request at least and up to
30 s when its robots.txt asks; when those find nothing, the page and its listing pages rendered in a headless browser);
then the AI (up to 3 attempts, each up to the AI provider's timeout, `LLM_TIMEOUT_S`, 180 s by default); then, for a
careers page, the page read again with the mapping. Each part cuts its own timeouts to the time left. The 10 minutes
count from when the app has the whole request. When they are up the app stops and answers **504 `timed_out`** (at
most 10 seconds later, whatever is still running), never a partial answer; nothing was saved.

- Give the request a client timeout of **15 minutes** (never less than 11): the app always answers before that.
- Tell the person it may take a few minutes and show that it is still working (the tasks tray shows it too). The
  popup may close meanwhile: the app's answer is then lost (nothing is saved before the person accepts), and the
  person works the page out again.
- While one runs for the account, and for a short while after a `timed_out` (until what was still running has
  stopped), a new work-out answers 429 `busy` (`Retry-After: 5`): say "JobHuntPA is still working out the last
  page; try again in a minute" rather than a general error.

**The request** (all three fields required; any other is 422 `invalid`):

```json
{"kind": "job", "url": "https://jobs.example.org/role/77", "outline": {"title": "...", "root": {...}}}
```

- `kind`: `job` (one job's page on a job site) or `careers` (a company's page that lists its jobs).
- `url`: the page's address (`location.href`), http(s), at most 2,048 characters (422 `invalid`, `field: "url"`).
  The AI is told only its scheme, host and path. The **site** is worked out from it (8.8): the host must be a host
  name of letters, digits, hyphens and dots (as punycode, which `location.hostname` gives), so neither an IPv6
  address in brackets nor a name with `_` has a site (422 `invalid`, `field: "url"`, a `detail` that says the page
  can't be worked out). The popup should not offer to work such a page out. For `kind: "careers"`, an address on the local network or this computer
  (or not a web link) is 422 `refused_address` (`field: "url"`) before that check, IPv6 ones included.
- `outline`: the trimmed outline of the page, below. The outline is the person's data: the app checks it, sends it
  to the AI, and drops it with the request. It is never stored and never logged.

**The outline.** An object `{"title", "root"}` (any other key: 422 `invalid`, `field: "outline"`):

- `title` (optional): `document.title`, at most 300 characters.
- `root`: an element, normally `document.body`. An **element** is an object with these keys, each optional but
  `tag` (any other key is refused):
  - `tag`: the element's name in lower case (`element.localName`): `^[a-z][a-z0-9-]{0,39}$`. **Leave out any
    element whose `localName` doesn't match this pattern, with everything inside it** (Word's `o:p`, custom
    elements with `.` or `_`, MathML or SVG children such as `linearGradient`): one such element in the outline
    refuses the whole outline (422 `invalid`).
  - `attrs`: an object of at most 12 attributes, each a string of at most 200 characters, from this list only:
    `id`, `class`, `role`, `itemprop`, `itemtype`, `itemscope`, `aria-label`, `href`, `datetime`, `title`,
    `data-testid`, `data-test`, `data-qa`, `data-automation`, `data-cy`. Send the value as `getAttribute` gives it
    (a boolean attribute such as `itemscope` as `""`); leave out an attribute whose value is longer than 200
    characters (or cut it, for `class` and `href`). The app drops an `href`'s query and fragment (`?...`, `#...`)
    before the AI sees it, as it does the page's; the extension may drop them before sending. Never `value`, `style`, `src`, an event handler or any other
    attribute.
  - `text`: a sample of the element's **own** text (its text-node children, not its descendants'), whitespace
    collapsed and trimmed, at most 100 characters (cut longer). Empty or left out when it has none.
  - `children`: its child elements, in document order, as elements.
- **Never in it:** `script`, `style`, `noscript`, `template`, `iframe`, `frame`, `frameset`, `object`, `embed`,
  `link`, `meta`, `base`, `head` (an outline holding one is refused: 422 `invalid`); the text of `input`,
  `textarea`, `select` and `option` (sent with no `text`; one with text is refused); comments; anything inside an
  `svg` (send the `svg` element without children) or a shadow root or frame. Leave out elements that aren't shown
  (`display: none`, `hidden`, `aria-hidden="true"`).
- **Caps:** the whole request at most **200,000 bytes** as sent (413
  `outline_too_large`, before any of it is parsed); at most **2,000 elements**, the root included, and at most
  **60 levels** deep, the root being level 1 (413 `outline_too_large`, with the cap in `detail`). The extension
  should fit the page in before sending: stop at the caps in document order and leave out the elements that hold
  nothing a job needs (empty wrappers' leaf children, navigation lists past their first few items), rather than
  send what will be refused. The outline may leave out elements, so the AI is told not to rely on positions.

**The answer, a job page** (200):

```json
{"site": "jobs.example.org", "kind": "job", "worked_out": true,
 "mapping": {"version": 1, "kind": "job", "fields": {"title": {"selector": "h1.job-title"},
             "description": {"selector": "div#ad-body"}, "date": {"selector": "time.posted", "attribute": "datetime"}}}}
```

Nothing is saved. The extension applies the mapping to the page (8.7), shows the person the job it finds, and saves
the mapping with `PUT` (8.8) only once they accept it.

**The answer, a careers page** (200). The app reads the page **itself** (through its scraping rules, never the
browser's copy): first the usual ways (a known job board, the job data sites embed, links that look like postings,
a rendered page); only when they find nothing is the AI asked, and the page read again with the mapping alone:

```json
{"site": "careers.acme.example", "kind": "careers", "url": "https://careers.acme.example/jobs",
 "worked_out": true, "mapping": {"version": 1, "kind": "careers", "fields": {...}},
 "test": {"ok": true, "method": "careers page read with its worked-out layout (careers.acme.example)", "count": 14,
          "sample": [{"title": "Senior Analyst", "location": "Sydney NSW", "url": "https://careers.acme.example/o/a1"}],
          "notes": []}}
```

- `worked_out: false` and `mapping: null` when the usual ways found jobs: no AI was asked, and the source needs no
  mapping. `test` is then their result.
- `worked_out: false` and `mapping: null` also for an address JobHuntPA reads its own way however the page looks
  (an APSJobs search, or a job board's own address on an applicant-tracking system JobHuntPA knows, such as
  Greenhouse, Lever, Ashby or Workday), when that finds nothing: searches never read such an address with a mapping, so
  none is worked out. `test` is `ok: false`, and its `notes` say why. The source can still be saved without one.
- `test` is the shape of `POST /api/sources/{id}/test` (8.5): `ok: false` with `error` when the page can't be read
  at all (robots.txt, a bot check, an error page: then no AI is asked and `mapping` is null) or the mapping finds
  no jobs.
- Show the person `test` (the jobs **the app** found). When they accept, save the source with `POST /api/sources`
  `{"url": <the answer's url>, "label"?, "mapping": <the answer's mapping or none>}`; nothing is saved before. The
  extension never applies a careers mapping itself.
- 422 `refused_address` (before anything is read) for a page on the local network or this computer.

## 8.7 The mapping

A mapping is **data, never code**: CSS selectors and attribute names, checked by the app, applied with the browser's
own selector functions. Nothing in it is ever evaluated, inserted as HTML or used as an address by itself.

```json
{"version": 1, "kind": "job" | "careers", "fields": {"<field>": {"selector": "<CSS selector>", "attribute": "<name>"}}}
```

- `kind` and `fields` are required (missing: 422 `invalid_mapping`); `version` is `1` (the app refuses another: 422
  `invalid_mapping`) and may be left out when sending a mapping. No other key.
- **`kind: "job"`** fields: `title`, `company`, `location`, `salary`, `employment_type`, `work_mode`, `date`,
  `description`. `title` and `description` are required. Applied by the extension: for each field,
  `el = document.querySelector(selector)` (inside `try`: a selector the browser rejects is a field not found); the
  value is `el.getAttribute(attribute)` when `attribute` is given, else the element's text (`innerText` for the
  description, keeping its paragraphs; `textContent`, whitespace collapsed, for the rest). The values then fill the
  job of 8.4: `location` is `location_text`, `salary` is `salary_text`, `date` is `posted_at` (sent only when it is
  an ISO date or date-time; else left out), `work_mode` is mapped to the contract's words or left `""`, and
  `description_from` is `site`, `method` is `site`. **Cut each value to the 8.4 limit of the field it fills before
  sending** (`title`, `company`, `location_text`, `salary_text` 500 characters, `employment_type` 100, `description`
  100,000): a selector that picks a large element would otherwise make preview and save answer 413 `too_large`
  (with `field` naming the value). A saved mapping "stops working" when it no longer finds a
  title: then offer to work the site out again.
- **`kind: "careers"`** fields: `list`, `title`, `link`, `location`, `salary`, `employment_type`, `work_mode`, `date`.
  `list`, `title` and `link` are required. Applied by the app only: `list` picks every job entry
  (`querySelectorAll`); every other selector is matched inside one entry, and `":scope"` means the entry itself;
  `link`'s value is its `attribute`, `href` when none is given. Entries without a title or a link to an http(s)
  page are skipped, as are links on the local network. `list` never has an attribute. The app reads a page with a
  mapping only when it is at most 3,000,000 characters of HTML and at most 80 elements deep, and stops matching
  after 10 seconds (the clock is looked at between elements; within the cost limits below one element's match is
  at most about one pass over the page, a second or two at these sizes), or sooner when the work-out's 10 minutes
  (8.6) run out; then the mapping finds nothing on that page and the usual ways follow (a note says why).
- Each field is `{"selector", "attribute"?}`: no other key. A field that isn't on the page is left out (in the AI's
  answer it may be null). At most the fields listed for the kind; an unknown field refuses the whole mapping.
- **A selector** is a string of 1 to 300 characters, trimmed, made only of ASCII letters, digits, space and
  `_ # . : > + ~ * [ ] = ^ $ | " ' ( ) , / ? & % -`, with `|` only in the `|=` operator inside `[...]` (no
  namespaces: `svg|a`, `*|*` and `[svg|href]` are refused, as browsers refuse an undeclared prefix); quotes
  balanced; no `javascript:`, `vbscript:`, `livescript:`,
  `url(`, `expression(` or `@import` (in any case), and no comment (`/*` or `*/` anywhere); pseudo-classes only
  from `:scope`, `:not()`, `:is()`, `:where()`, `:has()`, `:first-child`, `:last-child`, `:only-child`,
  `:nth-child()`, `:nth-last-child()`, `:first-of-type`, `:last-of-type`, `:only-of-type`, `:nth-of-type()`,
  `:nth-last-of-type()`, `:empty`, and no pseudo-element (`::`); within the cost limits below; and it must parse
  (by the app's selector engine, soupsieve, which is stricter than browsers in places: `:has(a b)` doesn't
  parse). `":scope"` alone only in a careers field other than `list`.
- **The cost limits** (a selector engine tries every ancestor for each descendant combinator and every earlier
  sibling for each `~`, remembering nothing between tries, so a few of them on a deep page can take minutes). The
  selector is read as CSS reads it, outside quoted strings and `[...]`, and counted **in all of it**, the
  arguments of `:not()`, `:is()`, `:where()` and `:has()` included (error: 422 `invalid_mapping`, or
  `not_worked_out` for the AI's answer):
  - at most **4 combinators** (a space between compounds, `>`, `+`, `~`); of them at most **2** a descendant
    combinator (a space) or `~`, and of those at most **one** `~`. In `:has()` a leading `>`, `+` or `~` counts
    as a combinator, and an argument without one counts as a descendant combinator (`li:has(a)` has one);
  - a selector with a `~` has **no descendant combinator** anywhere in it (for the element right of a `~` the
    engine tries every earlier sibling, and a descendant combinator would walk each one's ancestors too:
    `article p ~ p.last` is refused, `article > p ~ p.last` is taken);
  - a compound that is only `*` never stands next to a space or `~` (`div *`, `* ~ p` are refused; `div > *` is
    taken);
  - `:not()`, `:is()`, `:where()`, `:has()` at most **2 deep** inside one another;
  - at most **one `:has()`**, never inside another `:has()`. Inside it, no combinator but its leading (or implied)
    one, not even in a nested `:is()`, `:where()` or `:not()`: `li:has(a.job)`, `li:has(> :not(.ad))` and
    `div:has(~ p)` are taken, `body:has(:is(span div))` and `li:has(> :is(a > b))` are refused. In a compound that
    has more of the selector after it (not the element the selector picks), `:has()` must be one `>` or `+` and one
    compound: `section:has(> h2) p` is taken, `:has(h2) p` and `:has(> div > h2) p` are refused;
  - at most **3 commas** in all (between selectors, in the list or inside a pseudo-class);
  - `:nth-*()` take only `odd`, `even`, a number of up to 4 digits or `an+b` with numbers of up to 4 digits (no
    `of S`).

  Examples taken: `h1.job-title`, `#main .jobs > li.job`, `section#jobs li a`, `ul.list > li + li ~ li`,
  `li:not(:is(.ad, .promo))`, `tr:nth-child(2n+1)`, `a[lang|=en]`. Refused: `section div div span` (3 descendant
  combinators), `x * *`, `a ~ b ~ c`, `ul.list li + li ~ li` (a space and a `~`), `:not(:not(:not(a)))`,
  `a, b, c, d, e`, `svg|a`.
- **An attribute** is null, absent, or a name of 1 to 40 characters matching `^[a-z][a-z0-9_-]*$` (the app lowers
  its case), never one that starts with `on` and a letter (an event handler).
- The AI's answer is checked the same way and refused whole when anything breaks a rule (422 `not_worked_out`); the
  app never repairs one or uses part of it. A mapping the app answers with is always in this checked shape, in
  this field order, with `version` and `kind`.

## 8.8 Saved job-site mappings

A **site** is a host name, normalised: lower case, IDNA (punycode) for a non-ASCII name, no trailing dot, a leading
`www.` taken off; no scheme, port or path (`https://WWW.Seek.com.au:443/job/1` is `seek.com.au`). Send
`location.hostname`; the app normalises it and answers with the normalised `site`. Each account has its own
mappings (an older install's accounts don't share them), at most 500, sealed under the account's key.

For `GET`, `PUT` and `DELETE /api/extension/mappings/{site}` alike: a `{site}` that isn't a host name (such as
`a..b`) is 422 `invalid`, `field: "site"`; with a key, one with a character outside `[A-Za-z0-9.-]` is 403
`token_scope` before that, so send the punycode form `location.hostname` gives.

A saved `mapping` may come back as null (and its `fields` as `[]` in the list): a saved layout the app no longer
accepts, such as after an update tightened the rules of 8.7. Treat it as none and offer to work the site out again.

- `GET /api/extension/mappings`: `{"mappings": [{"site", "fields": [<the field names it has>], "updated_at"}],
  "max": 500}`, by site.
- `GET /api/extension/mappings/{site}`: `{"site", "mapping", "updated_at"}`, or 404 `not_found`.
- `PUT /api/extension/mappings/{site}` with `{"mapping": <a job mapping>}` (nothing else: a body with any other key,
  or without `mapping`, is 422 `invalid`, `field: "mapping"`; a body of at most 16 KB, else 413 `too_large`): 201 when new, 200 when it replaced one: `{"site", "mapping", "updated_at", "created"}`. A
  mapping not in the shape of 8.7 or of another kind: 422 `invalid_mapping`.
- `DELETE /api/extension/mappings/{site}`: `{"ok": true}`, or 404 `not_found`. The person can also forget one in
  Settings > Browser extension (*Job sites the AI worked out*).

**The popup's flow** (§1.15): read the page as before (JSON-LD, the sites the extension knows, the page's heading);
when that finds no title or no description, `GET` the site's mapping and apply it; when there is none, or it finds no
title, offer to work the site out (`work-out` with `kind: "job"`), apply the answer, show the job, and `PUT` it on
acceptance. The extension still never clicks, scrolls, turns pages or fetches anything on a job site by itself; the
outline is built from the page the person has open.

## 8.9 What changed from api 2

- `status` says `"api": 3`.
- New: `POST /api/extension/work-out`, `GET /api/extension/mappings`, `GET`, `PUT` and `DELETE
  /api/extension/mappings/{site}` (8.6 to 8.8), with the codes `ai_not_set_up`, `ai_failed`, `busy`,
  `not_worked_out`, `outline_too_large`, `invalid_mapping`, `too_many_mappings`, `not_found` and `timed_out`.
- `POST /api/sources` takes `mapping`; with one, a URL the workspace has gets it (200, `updated: true`) instead of
  409. Sources list their `mapping`.
- `POST /api/sources` refuses an address on the local network or this computer (422 `refused_address`), as the bulk
  add and the import do, before it looks at a mapping.
- The scraping layer no longer follows a redirect from a page on the web to the local network or this computer
  (the read fails, as a source's Test then says). A rendered page's requests there are aborted; a redirect hop
  there, which the browser follows by itself and sends before it can be stopped, fails the whole rendering, so
  nothing it read is kept. These checks go by the address's shape: a public name that resolves to a local address
  is not caught, nor is a rendered page's WebSocket (`ws://`, `wss://`) connection.
- Unchanged: keys, status's other fields, preview, jobs, jobs/batch and the source test.
