# Open-source JobHuntPA: the consolidated spec, and briefs for cloud sessions

**What the user asked for (2026-10-12 and 13):** "I want to open source this. I want an appimage and a windows
installer, as well as a manual mac install script like at present. There will also be the companion browser extensions
(which are yet to be published on the Firefox and Chrome extension stores). The app will be BYO LLM key. Add a way to
host it on your network as I currently do with the Pi." And, about building it: "I want to run this on claude cloud."

This file is the one place to build from. Part 1 is the spec: the user's final "Redefined spec" plus the answers they
gave in the conversation, with nothing from the earlier drafts that they later dropped. Parts 2 to 5 break it into
phases, one cloud session each, with a ready-to-paste brief for every phase (the layout of
`docs/design/remaining-phases.md`). Part 6 is what only the user or the local session can do. Part 7 describes the
import file of a data export (Phase E).

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
  verdict, fit results, cover letters), chats and the schedule; plus the research on those jobs' companies. Never the
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
| `pins` | `[{label, kind, lat, lng, radius_km}]`; `kind` is `home`, `hybrid` or `onsite` |
| `sources` | `[{ref, label, url, type, enabled}]`: careers pages and feeds; `url` is http(s) |
| `schedule` | `{enabled, times: ["10:00", …], days: [0-6, Monday = 0], skip_holidays, state: "NSW" …}` |
| `documents` | below |
| `jobs` | below |
| `chats` | below |

**`search_setup`:** `titles`, `keywords_include`, `keywords_exclude`, `seek_locations` (lists of text);
`salary_floor`, `max_office_days` (integers or null); `remote_aus_ok`, `remote_global_ok`, `allow_hybrid`,
`allow_onsite`, `seek_enabled` (true/false); `dealbreakers: {industries, keywords}`; and **`starting_points`**: whatever
the search setup stores for its starting points (§1.8), in the shape it stores them. At format version 1 as first
written, that is the list of "Commute from" stations (text); once starting points are stored as objects
(`{label, place, use}`, Phase C), the same field carries those objects. A reader takes either shape: a text item is
a place, an object's `place` (else `label`) is.

**`documents`:** `{ref, filename, filetype, kind, text, concise_text, use_concise, use_for_scoring, concise_model,
concise_at, created_at, updated_at, summary_of, original}`. `filetype` is the upload's extension (`.pdf` …), or
`note` / `summary` for a document written in the app; `kind` is `resume`, `cover_letter`, `preferences` or
`other`; `summary_of` lists the `ref`s a summary was made from; `original` is the path of the uploaded file inside
the export's folder (`Workspaces/<workspace>/Documents/Original files/<name>`), or null. The original files are
plain bytes in the zip, exactly as uploaded.

**`jobs`:** one entry per job, merged copies included:

| Field | Meaning |
|---|---|
| `ref` | the job's number in this file |
| `origin` | `pool` (linked to an ad in the exporting install's shared pool) or `private` |
| `ad` | the ad as the workspace saw it: `url`, `source`, `external_id`, `company`, `title`, `location_text`, `country`, `work_mode`, `employment_type`, `office_days`, `salary_min`, `salary_max`, `salary_text`, `description`, `description_hash`, `detail_status`, `detail_fetched_at`, `posted_at`, `industry` |
| `own` | the names of the `ad` fields that were the workspace's own values (an uploaded full ad, details from a merged copy); all of them for a private job |
| `status` | `to_review`, `shortlisted`, `applied`, `interviewing`, `rejected` or `not_interested` |
| `first_seen`, `last_seen`, `closed_at` | when the job was first linked to the workspace, last listed by its search, and when the ad closed |
| `hidden` | the person hid it |
| `excluded_reason` | why the search filters left it out, or null |
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
single spaces (`company_key`); `controversies`, `sources`, `sentiment`, `offices` and `offices_cities` are JSON
values as the research stored them.

## What an import does with it

- Each picked workspace becomes a new workspace of the signed-in account, with the naming rule of §1.7.
- A job whose `ad.url` the importing install's pool already has is linked to that posting, and only its `own`
  fields are set on top; any other job is a private job with the whole `ad` and its `closed_at`. Nothing is written
  to the pool.
- A company's research is taken only for the companies of the imported jobs, and only where the install has no
  research on it or older research (`researched_at`).
- Values are checked against the types and limits above (lengths, ranges, the listed words); a file that breaks them
  is refused as damaged, naming only where (`workspaces.0.jobs.12.status`), never the value. Addresses that are not
  http(s) are dropped from sources and from a chat answer's pages. Everything else is stored as data and shown
  escaped, like any other job or document.
- The zip limits: 250 MB packed, the import file at most 120 MB unpacked, the import file and the originals at most
  320 MB unpacked together, and no entry over 1 MB unpacking to more than 200 times its packed size; a zip with an
  entry whose path leaves its folder is refused whole. An upload still arriving after ten minutes plus the time the
  largest zip needs at 100 KB/s is cut off. One import runs at a time.
