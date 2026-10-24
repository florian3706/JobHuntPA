# JobHuntPA features

Everything the product does, taken from the code on `main` after v2.0.0 (0651197), 23 October 2026.
Each section names the files that implement it. The browser extensions themselves live in their own
repositories; this repository holds the server side they talk to.

## Contents

1. [Finding jobs](#1-finding-jobs)
2. [Filtering](#2-filtering)
3. [The Jobs tab](#3-the-jobs-tab)
4. [Fit scoring](#4-fit-scoring)
5. [Ghost-job signs](#5-ghost-job-signs)
6. [Duplicate ads](#6-duplicate-ads)
7. [Locations, offices and the map](#7-locations-offices-and-the-map)
8. [The job page and commute](#8-the-job-page-and-commute)
9. [Company research](#9-company-research)
10. [Documents and notes](#10-documents-and-notes)
11. [Cover letters](#11-cover-letters)
12. [Chat about jobs](#12-chat-about-jobs)
13. [Suggested job titles](#13-suggested-job-titles)
14. [Workspaces](#14-workspaces)
15. [Scheduled searches](#15-scheduled-searches)
16. [Job digest emails](#16-job-digest-emails)
17. [Browser extension (server side)](#17-browser-extension-server-side)
18. [Accounts and signing in](#18-accounts-and-signing-in)
19. [Encryption and keys](#19-encryption-and-keys)
20. [Your data: export and deletion](#20-your-data-export-and-deletion)
21. [Admin panel](#21-admin-panel)
22. [Memberships and payments](#22-memberships-and-payments)
23. [Email](#23-email)
24. [AI providers](#24-ai-providers)
25. [Themes](#25-themes)
26. [Accessibility](#26-accessibility)
27. [Security hardening](#27-security-hardening)
28. [Install, start, update and uninstall](#28-install-start-update-and-uninstall)
29. [Developer tooling](#29-developer-tooling)

---

## 1. Finding jobs

`backend/adapters/`, `backend/pipeline.py`, `backend/pool.py`, `backend/scraping/`, `backend/tasks.py`

**Sources**

- **SEEK** search results: each target title is searched in each SEEK location (empty means all of
  Australia). Only the first results page is read, as SEEK's robots.txt allows, so SEEK jobs carry a
  listing summary. SEEK can be switched off per workspace.
- **APSJobs** (Australian Public Service): a search URL by agency (`department=`, repeatable) or by
  search words (`searchString=`). Every result carries the whole ad.
- **Hosted job boards**, read through their public feeds: Workable, Greenhouse, Lever, Ashby,
  SmartRecruiters, Workday, and Atlassian's careers site.
- **Any company careers page**, tried in this order:
  1. a link on the page to one of the boards above;
  2. an RSS or Atom job feed;
  3. schema.org `JobPosting` data embedded in the page;
  4. plain HTML job links, following "next page" links;
  5. a hidden headless browser (Chromium) for pages that only build their list with JavaScript.
- **Saved pages**: SEEK search or job pages saved from the user's own browser (HTML or MHTML) can be
  imported; a saved job page gives the full description.
- **Upload full ad**: a SEEK job marked *summary only* can be completed with a saved copy of its page.
  The job is then re-filtered and rescored.
- **Browser extension**: adds the ad in the current tab ([section 17](#17-browser-extension-server-side)).

**Company sources** (`backend/sources.py`)

- Add a careers page by URL with an optional company name; enable, disable, rename or delete it.
- **Test** does a dry run: how many jobs it finds, how it read them, and a few titles, storing nothing.
- Each source shows its last result, including why it failed (refused automated access, bot check,
  robots.txt disallows the page, no postings found).

**The search run**

- **Run search** starts a background run on one worker thread; the page polls its stage and progress.
- Runs never overlap.
- The last run's report lists, per source: listed, new, descriptions fetched, kept, excluded, closed
  and status.
- Listing pages are cached for 30–60 minutes, so repeated runs cost nothing.
- A detail page is fetched only when no full description is stored, or the stored one is older than
  30 days, and only for ads that pass the cheap filters.
- At most 60 new full ads per source per run; the rest follow on the next run.

**Shared job pool**

- Every scraped ad is stored once and shared by all workspaces, so a page is fetched once.
- A search also links in open ads that other searches found, when they match the workspace's titles
  or include keywords and pass its filters (up to 300 a run). A workspace with no titles or keywords
  gets none.
- Everything personal stays with the workspace: status, hidden, exclusion, score, office, location
  call, cover letter, chats.
- Hand-added content is private: an uploaded full ad, an imported page, and an extension-added job
  stay with the workspace unless a search already found the same ad.

**Closed jobs**

- A job closes when its page answers 404 or 410, when a complete listing no longer shows it, or when
  no listing has shown it for 45 days.
- A closed job that is listed again reopens.
- One search's results lacking an ad never close it.

**Scraping rules** (`backend/scraping/http.py`, `robots.py`)

- robots.txt is checked for every URL and every redirect hop, with wildcard and longest-match rules.
- `Crawl-delay` is honoured, with at least 2 seconds between requests to a host (capped at 30 s).
- The User-Agent names the app honestly; there is no browser impersonation.
- Bot checks (Cloudflare, AWS WAF) and CAPTCHAs are reported as errors and never worked around.

## 2. Filtering

`backend/filters.py`, `backend/profile.py`, `backend/geo.py`

Filters decide whether a job is excluded before any AI scoring. Excluded jobs are kept with their
reason and can be shown with *Show excluded*.

- **Target job titles**: a job passes if its title contains one. Also used as SEEK search terms.
- **Include keywords**: or if its title or description mentions one. With neither set, everything passes.
- **Exclude keywords**: whole-word match in the title or description.
- **Dealbreaker industries**: matched against the job's industry, company and title.
- **Dealbreaker keywords**: matched in the title and description.
- **Salary floor** (AUD per year): applied only when the ad states a salary that can be read. Hourly
  and daily rates are annualised.
- **Work modes wanted**: remote (Australia), remote (overseas employers), hybrid, onsite.
- **Hybrid office days**: a maximum number of office days a week (1–4, or any). Ads that don't state
  the days are kept.
- **Location**: roles outside Australia are excluded unless remote (global) is on. With map pins,
  hybrid roles must fall inside a home, hybrid or onsite pin radius; onsite roles inside a home or
  onsite pin; roles of unknown mode inside any pin. Roles that can't be placed are kept.
- All phrase matching is case-insensitive and on word boundaries ("war" does not match "software").
- **Re-filter**: changing the search setup re-applies the filters to stored jobs.
- **Office days (temporary)**: a what-if menu on the Jobs tab that allows up to 5 extra office days
  without saving anything. Jobs it brings back are marked, and **Score them** scores any never scored.
  Reloading or switching workspace restores the saved rule.
  

## 3. The Jobs tab

`frontend/index.html`, `frontend/app.js`, `frontend/common.js`, `backend/app.py`

**Job card**

- Company (with a company-info icon), title linked to the original ad, location, work mode badge,
  employment type badge, distance from home, salary, source and posted date.
- Flags: *summary only*, *no description*, *closed*, *hidden*, *N days in office*, *office unknown*
  (with *recruiter* when it is an agency ad), *shown by temporary +N office days*.
- Fit score with 🎯, and the ghost-job circle under it.
- *Also on*: the other sites that advertise the same job, each with **split**.
- The exclusion reason, or the scoring error, when there is one.
- Actions: status menu, **Score / Rescore**, **Description**, **Upload full ad** (SEEK summaries),
  **Hide / Unhide**, **Draft cover letter**, **Chat**, **Open ↗** (the full page in a new tab).
- Expandable **Fit: requirements → evidence** and **Gaps** sections.

**Statuses**: to review, shortlisted, applied, interviewing, rejected, not interested.

**Filters and sorting**

- Text search over company, title and location.
- Minimum score slider (0–100 in steps of 5).
- Work mode: all, remote, hybrid, onsite, unknown.
- Maximum distance in km.
- Status chips.
- Sort by score, newest first, or distance.
- *Show excluded*, *Show closed*, *Show hidden*, *Hide possible ghost jobs*.
- A count of jobs shown and how many the filters hide.

**Bulk actions**

- Tick jobs, or **Select all shown**, then: set status, hide or unhide, chat about them (up to 12),
  **Merge as one job** (two or more), or **Fill missing info** for just those.
- Only jobs on screen are ever affected.

**Run card**

- **Run search**, **Score pending**, **Fill missing info**, **Duplicates**.
- Progress bar, the next scheduled search, and the last run's per-source report.
- A note saying when a search uses one of the account's free searches.

**Hidden jobs** stay hidden when a search finds them again, and aren't scored or researched.

## 4. Fit scoring

`backend/scorer.py`

- Each job that passes the filters gets a **score from 0 to 100** against the user's documents.
- The AI lists every requirement in the ad, marks it must-have or nice-to-have, and quotes the
  evidence from the documents for each.
- **Gaps**: the must-haves that are missing or weakly evidenced.
- A 2–4 sentence **summary** of the fit.
- **Preferences** never count as experience and don't change the score; a job that conflicts with one
  is named in the summary.
- The scorer also reads the **office address** named in the ad, and quotes any wording that says the
  ad isn't a current vacancy (a ghost-job input).
- Merged copies and hidden or closed jobs aren't scored.
- **Stale scores**: each score records a hash of the documents it used, so jobs scored before the
  documents changed show as stale.
- Buttons: **Score pending** (never scored or failed), **Rescore stale**, **Rescore all**, and
  **Score / Rescore** on one job.
- Failures are stored as errors, not as 0, and retried on the next pass. A rejected API key stops the
  batch after one request.
- Requests run in parallel (3 by default, `LLM_CONCURRENCY`) with a per-request timeout
  (`LLM_TIMEOUT_S`, 180 s).
- A header badge shows whether the AI is ready, not set up, or failing.

## 5. Ghost-job signs

`backend/ghost.py`, `docs/design/ghost-jobs.md`

The app reports **signs** that an ad may not be a real, current vacancy. It never hides, scores or
ranks a job down because of them, and never puts a number on it.

**The eight signs**

| Sign | Fires when |
|---|---|
| Long open | Listed for 45 days or more and still showing (stronger at 60 and 90 days) |
| Reposted | The same role from the employer was listed before, ended, and appeared again |
| Date refreshed | The posted date moved 21+ days after the ad was first seen |
| Evergreen wording | "Talent pool", "talent community", "register your interest" and similar, weighted by where it appears |
| Recruiter, no employer | A recruiter's ad that says "our client" or "confidential client" |
| Many similar | Five or more open ads with the same title at one company |
| Company news of cuts | Company research found layoffs or a hiring freeze in the last 12 months |
| AI reading | The scorer quoted wording that says the job isn't a current vacancy |

**How it shows**

- 👻 and a circle under the fit score on every card, the map list and the job page: 🟢 no strong
  signs, 🟡 some signs, 🔴 several signs.
- Hover text lists the signs found.
- Flagged cards add **Possible ghost job (some / several signs): why?**
- The job page's **Is this a real vacancy?** section lists all eight checks and whether each was
  found, not found, or could not be checked, explains the colours, and suggests questions to ask the
  employer.
- **Hide possible ghost jobs** filter (off by default), with a count of how many it hid.
- Digest emails and the data export carry the signs too.

## 6. Duplicate ads

`backend/duplicates.py`

- The same job advertised on several sites becomes one card, with the others under *Also on*.
- **Sure** duplicates are merged automatically after each search: same employer, location and title,
  and either matching ad text or a title specific enough to identify one job.
- **Possible** duplicates are listed in the **Duplicates** window for the user to decide: *Same job:
  merge* or *Different jobs*. A "different" answer is remembered and never suggested again.
- The copy with the best data stays listed; the user's status, cover letter and score move to it, and
  details only a copy has (salary, location, work mode) fill its gaps.
- **split** undoes a merge; **Merge as one job** merges ads the app missed.
- **Check for duplicates now** runs a scan on demand; automatic merges can be confirmed in bulk.
- Merged copies keep their rows, so later searches recognise their URLs and keep them merged.

## 7. Locations, offices and the map

`backend/geo.py`, `backend/offices.py`, `backend/geo_lookup.py`, `frontend/app.js`

**Placing jobs**

- Locations are geocoded with OpenStreetMap Nominatim (at most one request a second, cached).
- The country is guessed from the location text; SEEK area names such as "North West & Hills
  District" are placed at their main centre.
- Work mode (remote, hybrid, onsite, unknown) and days in the office are read from the ad.
- Distance from the home pin is worked out for each job.

**Offices**

- An ad that names only a city is flagged **office unknown** instead of being placed in the CBD.
- The office is looked up, best source first: the user's own entry, the office named in the full ad,
  then the employer's offices from company research (or a web search for that city).
- With several offices in a city, the one closest to the user's commute pins is used.
- On the job page the user can type an address or suburb, pick one of the employer's offices, clear
  it, or make the call with **Location OK** / **Too far** (with Undo).
- The office then drives the location filter and the distance.
- Recruiter ads are left to the user, since they rarely name the client.

**Map tab**

- **Jobs mode**: the jobs the Jobs tab filters show, the pins with their radii, a legend, and a list
  of the jobs in view under the map. Click a dot or **Show** to see a place's jobs; **Open ↗** opens
  a job's page.
- **Edit pins mode**: click or tap to drop a pin, drag to move it. Three pin kinds (home, hybrid,
  onsite), a radius from 1 to 100 km, and an optional label.
- Keyboard routes: **Drop a pin at the cross**, **Move to the cross**, **Locate**, and arrow keys
  and + / − to pan and zoom.
- *Only list pins in view* shortens the pin list.
- Excluded jobs show as hollow dashed dots; pin kinds have solid, dashed and dotted rings.

## 8. The job page and commute

`frontend/job.html`, `frontend/job.js`, `backend/commute.py`

- **Open ↗** on any job opens a full page: the heading with status, **Is this a real vacancy?**,
  **Office and commute**, **Company**, **Fit** and **The ad**.
- A map of the office, the home pin, the stations and the quickest route.
- **Public transport** (Transport for NSW Trip Planner): a journey from each station listed under
  Search setup > Commute, or from the home pin, including changes, metro, buses, light rail and
  walking at the far end.
- **Driving**: TomTom with predicted peak traffic when a key is set, otherwise OSRM's public server
  without traffic (the page says so). Driving starts at the home pin.
- Times are for arriving by the morning time and leaving at the evening time (09:00 and 17:00 by
  default) on the next Tuesday.
- Each route links to Google Maps directions.
- Answers are cached for a week per account, encrypted.
- Commute settings: stations to start from, arrive-by time, leave-at time.

## 9. Company research

`backend/research.py`, `backend/app.py`

- One AI web-search agent per company builds a profile: official name, what the business does and how
  it makes money, ownership, headquarters, employee count, office addresses, employee sentiment
  (rating, recurring positives and negatives, Glassdoor link) and up to 5 controversies with news
  links.
- It also records whether the "company" is a recruiter, or not a researchable company at all.
- **Link integrity**: every link shown was returned by the search tool; anything else is dropped, and
  a controversy left without a source is dropped with it.
- Profiles are shared by all workspaces, so a company is researched once.
- **Fill missing info** researches companies with no profile or one older than 30 days, and finds the
  office of ads that name only a city, for the ticked jobs or the whole list.
- **Research again** refreshes employee reviews and news when they are more than a day old and keeps
  the rest; the two parts carry their own research dates.
- Shown from the ⓘ icon on a card and in the job page's Company section.
- Needs a provider with web search.

## 10. Documents and notes

`backend/docs.py`, `frontend/app.js`

- **Upload** PDF, DOCX, ODT, ODS, ODP, TXT or Markdown files, several at once, up to 20 MB each.
- **Write a note** in the app: title, text and type. Notes are plain text.
- **Types**: Resume / CV, Cover letter, Preferences (private), Other (supporting).
- **Use for scoring** tick box keeps a document in or out of every AI prompt.
- **Open** a document to read its text, rename it, change its type, or edit it (a note's or summary's
  text; for an uploaded file, only its concise version).
- **Concise version**: the AI writes a shorter version; edit it, save it, write it again, and choose
  whether the AI gets the original or the concise one.
- **Summary of ticked documents**: merges every ticked document (not preferences) into one new
  document with each fact once, then offers *Untick the originals* or *Keep both ticked*.
- **Preferences** are read by the scorer, the chat and title suggestions, and never sent when
  drafting cover letters.
- Changing what the AI gets marks existing scores as stale.
- Uploaded files are stored encrypted; their text is read in memory and never written out.
- **Cover letter blocks** (a card on the Documents tab): paragraphs in your own words that cover letters are
  built from. Each block has a title, a kind (*opening*, *body* or *closing*), tags and its text. The text may
  use the placeholders `{company}`, `{role}`, `{location}` and `{date}`, which are filled in from the job when
  a letter is put together; a warning names any other `{...}` word you saved (a typo such as `{compny}`).
  The list is grouped under Opening, Body and Closing; each block can be edited, moved up or down, or deleted
  (with a confirmation). Blocks are stored encrypted and copied with a new workspace's documents.
- **Letter templates**: upload a Word (`.docx`) or OpenDocument (`.odt`) file with your letterhead and signature
  as the type *Letter template*. It must contain a paragraph with `{letter}` (`{{letter}}` works too): that
  paragraph is replaced by the letter, one paragraph per letter paragraph in its style and font. `{date}`,
  `{company}`, `{job_title}` (or `{role}`), `{location}` and `{name}` (the name the letter signs off with) are
  filled in anywhere in the file (body, tables, headers, footers), the same markers as in blocks. An upload
  without `{letter}` is refused with a message. Several templates can be uploaded; the one marked *current
  template* is used, and *Use the plain layout* goes back to an A4 letter. A template is never sent to the AI.
  The file is stored encrypted like other uploads.

## 11. Cover letters

`backend/cover_letters.py`, `backend/letter_blocks.py`, `backend/letter_files.py`, `backend/letter_pdf.py`,
`backend/textclean.py`

- **Draft cover letter** on any job, written from the résumé, earlier cover letters as style samples,
  the ad, the fit result and the company profile.
- Optional **extra instructions** for the draft.
- Edit and **Save edits**, **Copy**, **Redraft** (with a confirmation), **Print** and **Download** (below).
- The card shows *Cover letter ✓* once a letter exists, drafted or assembled.
- **Build from my blocks**: the AI chooses which of your blocks fit the job and in what order (one opening,
  two to four body blocks, one closing) and says why for each; it never rewrites them. You tick or untick
  blocks and move them up or down, then *Assemble letter*. Assembly is verbatim: your block texts joined with
  blank lines, with the placeholders filled in from the job and today's date. A placeholder that can't be
  filled (unknown name or empty value) stays as written and a notice says *Left as written* with the names, to
  fill in before sending. The letter is marked as built from your blocks. Without an AI key the same list
  appears with every block unticked, to choose yourself. Replacing an existing letter asks first.
- **Blocks worth writing**: requirements of the ad that your documents show real experience in but no block
  covers (all of them when you have no blocks yet). Each shows a proposed title and *Write this block*, which
  opens a form with bullet points of facts taken from your documents beside the text box. You write the
  paragraph in your own words; *Save block* adds it to the list, ticked, and to the Documents tab. *Skip*
  closes the form.
- **AI drafts use your blocks** as material when you have any (their sentences kept where they fit, the
  placeholders filled, only transitions and gaps written) and imitate your voice, studied from your blocks,
  earlier cover letters and, without letters, your notes.
- **Plain characters**: what the model writes (drafts, reasons, suggested titles and bullets) loses invisible
  characters, has curly quotes, dashes and special spaces turned into plain ones, accented letters
  transliterated (`é` becomes `e`, `ß` becomes `ss`) and anything outside a fixed set of characters dropped.
  This is never applied to the text of your own blocks.
- **Downloads** as `.pdf`, OpenDocument `.odt` or Word `.docx`. With a letter template the letter is poured
  into it (a `.docx` template gives `.docx` and PDF, an `.odt` one `.odt` and PDF; the other format is greyed
  out with the reason); without one it is a plain A4 document in the font and size chosen in the window's
  *Template* section (a font this computer has, fonts installed while the app runs included; the default is a sans at
  11 pt). The PDF is made from the filled file: by
  LibreOffice when it is installed, otherwise drawn by the app with the fonts this computer has (the window
  names any font that stood in). The window says which template is used and which way PDFs are made.
- **Discard my edits**: the model's draft is kept beside your edits, so you can go back to it. *Mark this job
  as applied when I download* is an option in the window.

## 12. Chat about jobs

`backend/job_chat.py`

- **Chat** on one job, or on up to 12 ticked jobs, in a panel beside the cards.
- The AI sees the ads, the user's documents, the fit assessment, the company profile and any
  cover-letter draft.
- Suggested opening questions, such as "What would I actually be doing day to day?"
- **Search the web** tick box; the pages used are listed under the answer.
- Links that aren't in the material or the search results are removed from answers.
- Chats are saved per job set: pick an earlier chat, start a **New chat**, or delete one.
- **Copy** an answer. Enter sends, Shift+Enter starts a new line.

## 13. Suggested job titles

`backend/title_suggestions.py`

- The AI reads the workspace's documents and suggests titles, each rated strong fit, good fit or
  stretch with a reason.
- Titles already in the search are flagged.
- **Add selected to this search**, or **Start a new search with selected** (a new workspace).

## 14. Workspaces

`backend/workspaces.py`

- Separate job searches, each with its own search settings, dealbreakers, SEEK settings, company
  sources, documents, map pins, jobs, scores, runs and schedule.
- Create, rename, delete (not the last one) and switch from the header.
- A new workspace can copy another's settings, sources and pins, and optionally its documents.
- Each workspace belongs to one account; anyone else's answers 404.

## 15. Scheduled searches

`backend/schedule.py`

- A workspace's search runs by itself at set times: 10:00, 12:00, 14:00 and 16:00, Monday to Friday,
  by default.
- Times and days can be changed, or the schedule switched off, per workspace.
- **Skip public holidays** for the chosen Australian state or territory, substitute days included.
  The state also sets the time zone of the times.
- A slot missed because the app was closed, or because another run was busy, runs if it can start
  within the hour.
- A slot is passed over when a search was started by hand since its time, or when there is nothing to
  search.
- Scheduled searches need **background access** and, when memberships are on, a membership. A skipped
  slot shows its reason on the schedule card.
- The card shows the next and last scheduled search.
- Each run scores the new jobs it finds.

## 16. Job digest emails

`backend/digest.py`, `backend/email_templates.py`

- One email with the new jobs that scheduled searches found since the last digest, best fit first.
- Each job card shows the title, company, pay, place, work mode, employment type, a short summary,
  the fit score, links to the job in the app and to the original posting, and a "Possible ghost job"
  line when flagged.
- Up to 60 jobs; the email says how many more there are.
- Sent around 07:00 in the schedule's time zone, every 1 to 7 days as chosen. Nothing is sent when
  there are no new jobs.
- The Settings card lists what is missing (email not set up, address not confirmed, background access
  off, no membership).
- **Unsubscribe** link that needs no sign-in and changes nothing until a button is pressed; mail
  programs' one-click unsubscribe works too. *Subscribe again* is offered for an hour.
- Failed sends are retried a few minutes later.

## 17. Browser extension (server side)

`backend/extension.py`, `frontend/extension.js`

- **Add as a job**: the extension reads the ad in the user's own browser and sends only the job's
  fields and its description as plain text. The server stores it as the workspace's own private job.
- A **preview** call shows the address a save would store and whether the workspace already has that
  job; an existing job is updated, not added twice.
- The server tells the extension when the user's filters exclude the saved job, and why.
- **Add as a company source**, with **Test**.
- **Extension keys**: made in Settings > Browser extension with a name, shown once, up to 20 per
  account. The card lists them with when each was last used; **Disconnect** stops one at once.
- A key expires after 60 days without use or a year after it was made, and whenever the account is
  signed out everywhere.
- A key can only add jobs and sources and list workspaces and sources. It can't read jobs, documents
  or settings, or change the account.

## 18. Accounts and signing in

`backend/users.py`, `auth.py`, `sessions.py`, `security.py`, `totp.py`, `lockout.py`, `ratelimit.py`,
`password_reset.py`, `account_email.py`, `account.py`, `signup.py`, `tokens.py`

**Creating accounts**

- **First run**: a page with a one-time setup code creates the first account, which is the admin.
  Anything already in the app moves into it.
- A headless install can create the admin from `JOBHUNT_ADMIN_EMAIL` and `JOBHUNT_ADMIN_PASSWORD`.
- **Sign-up modes**: invitation only (the default), open, or closed.
- **Invitation links**: single use, 7 days, optionally tied to an email address and emailed.
- Passwords need at least 10 characters.

**Setup walkthrough** (`frontend/welcome.html`)

- Steps: choose a theme; job titles; where to work (places, work modes, office days, home suburb);
  salary and words to include or avoid; automatic searches (background access and digests); an
  optional résumé.
- Back, Next and Skip on every step; it saves as it goes and resumes where the person left off.
- A short version for the first admin of an existing install: choose a theme, then keep or review
  the settings that moved in.

**Signing in**

- Email and password, with *Keep me signed in*.
- Sessions end after 24 hours unused or 7 days; with *Keep me signed in*, 30 days unused or 90 days.
- **Two-step sign-in** with any authenticator app: QR code or setup key, codes that work once, and a
  recovery code as a fallback.
- **8 recovery codes**, shown once to copy, download or print, with a reminder until confirmed saved.
- **Wrong-password wait**: after 5 wrong tries from one place, 30 seconds, doubling up to 15 minutes,
  for that place only. An account-wide ceiling of 100 wrong tries an hour applies to browsers that
  have never signed in. No permanent lockout.
- Each network address is held to 10 sign-in requests in 5 minutes and 60 in an hour.

**Recovering access**

- **Recovery code reset**: email, a recovery code and a new password. Data is kept. Works without
  email set up.
- **Email reset link**: single use, 1 hour, only the newest works. The answer is the same whether or
  not the address has an account. With background access the data is kept and new recovery codes are
  issued; without it the person must tick that their saved data will be deleted.

**Managing the account** (Settings tab)

- Change password (other browsers are signed out unless unticked).
- Turn two-step sign-in on or off.
- Make new recovery codes, or show unconfirmed ones again.
- **Sign out everywhere else**, which also disconnects browser extensions.
- Confirm or change the email address. A change sends a link to the new address and takes effect
  only when its owner presses the button on the page; the old address is told.
- Sensitive actions ask for the password again if it wasn't entered in the last 15 minutes.
- Security notices by email: password changed, new recovery codes, two-step sign-in turned off by an
  admin, email address changed.
- Theme saved with the account.
- Account menu in the header: who is signed in, Membership, Admin (for admins), Sign out.

## 19. Encryption and keys

`backend/crypto.py`, `keys.py`, `admin_key.py`, `background.py`, `key_rotation.py`, `key_recovery.py`,
`migrate_encryption.py`

- **Per-account encryption at rest**: each account has a random data key that only its password or a
  recovery code unlocks. Searches, jobs' personal fields, scores, cover letters, chats, documents,
  uploaded files, pins, sources, commute lookups and the theme are sealed under it.
- Whether a job is hidden or excluded doesn't show in the database either.
- Public job ads and company research are stored unencrypted and shared.
- **Background access**: an opt-in switch that stores a copy of the data key under the server's key,
  so scheduled searches and digests can run while the person is signed out. Turning it off deletes
  the copy at once.
- **Admin key**: people's emails, names, notes and billing details are sealed so only admins can open
  them, through a separate identity key the server's key can't reach.
- Sharing, replacing and re-sharing the admin key when admins are added, removed or disabled.
- **Server key** (`data/server-keys.json`): rotation from the admin panel or the command line, with
  crash-safe resumption.
- **One-time encryption** of an older install's data, with a backup first, a progress display, and
  resumption if stopped halfway.
- **Command line** (`python -m backend.keys`): `bench-kdf`, `rotate-kek`, `new-admin-key`,
  `recover-lost` (rebuild a lost key file with an admin's password).
- Primitives: AES-256-GCM, X25519, HKDF-SHA256 and argon2id, from standard libraries.

## 20. Your data: export and deletion

`backend/export.py`, `backend/account.py`

- **Download your data**: a zip of Markdown files in folders, made in memory.
  - `Account.md` and a README.
  - Per workspace: search setup, sources, documents (with the original uploads byte for byte), jobs
    in folders by status (each with fit score, description, cover letter, ghost-job signs and other
    sites), excluded jobs, chats and search history.
  - Also per workspace: `Cover letter blocks.md` (your blocks in letter order with kind and tags, only when
    you have some). Letter templates are documents, so their original files are with the other uploads.
  - The company research behind the jobs.
  - Links between the files work offline.
- The import file carries the cover letter blocks (`letter_blocks`) and which document is the letter template
  (`letter_template`), and the reset tool wipes the blocks.
- Limits: 200 MB of files, one export per account and two at once for the whole app.
- **Delete account**: removes workspaces, jobs, documents, uploads, chats, cover letters, keys and
  sessions, and cancels a Stripe subscription first. The last admin can't delete themselves.

## 21. Admin panel

`backend/admin.py`, `frontend/admin.html`, `frontend/admin.js`, `server-settings.js`, `stripe-settings.js`

The panel never shows anyone's jobs, documents or searches.

- **Summary and AI use**: people, admins, active in 7 and 30 days, joined in 30 days, and AI use over
  7 to 365 days (requests, tokens in and out, cached share) by model, purpose, person and day.
- **People**: a sortable table of email, name, role, status, last active day and membership, which
  becomes cards on narrow screens. Actions per person:
  - make admin or regular user;
  - disable or enable;
  - unlock after wrong passwords;
  - turn off two-step sign-in;
  - send a reset email;
  - delete the account;
  - share the admin key again;
  - open their membership: exempt from payment, free searches used, free periods with a start, end
    and admin-only note, and *End now*.
- **Sign-up and invitations**: the sign-up mode, the number of free searches, and creating, listing
  and revoking invitation links.
- **Server**:
  - AI provider, model, API key, reasoning level per task, *Detect supported levels*, *Test connection*;
  - commute keys (Transport for NSW, TomTom) with *Test saved keys*;
  - email settings with *Send a test email*;
  - Payments (Stripe) with *Sync now*;
  - **Replace the server key**;
  - a report of unencrypted copies of data with *Delete these files now*.
- **Audit log**: every admin action, newest first, with who did it, to whom, and details that are
  never anyone's content.
- Settings changes are logged by name, never by value. A saved key is never shown again in full.

## 22. Memberships and payments

`backend/access.py`, `backend/billing.py`, `backend/membership.py`, `frontend/billing.html`

- Optional and off by default: with no Stripe key, or no price found, everyone has full access.
- Prices are found in Stripe by lookup key (`jobhunt_monthly`, optional `jobhunt_yearly`).
- **Free searches**: 3 by default for each new account. A free search finds and scores jobs, plus up
  to 5 other jobs waiting for a first score. It is given back if the run fails while still searching.
- **Members only**: rescoring, chat, cover letters, company research, concise versions and summaries,
  title suggestions and scheduled searches.
- Without a membership, existing jobs, notes, documents and settings stay available.
- A membership comes from a Stripe subscription, an admin's free period, or an exemption. Admins need
  none.
- A failed payment keeps access for 3 days.
- **Membership page**: status and why, free searches left, plan, renewal or end date, **Become a
  member** (Stripe Checkout) and **Manage payment and plan** (Stripe's billing portal).
- A free period for someone who pays moves their next payment to its end.
- Status is kept current by an optional webhook, on return from checkout, when the membership page is
  opened, and every 6 hours.
- Test mode is recognised and badged.
- Card numbers never reach the app; admins see the card brand and last four digits.

## 23. Email

`backend/mailer.py`, `mail_queue.py`, `mail_route.py`, `email_settings.py`, `email_templates.py`

- Optional SMTP sending (STARTTLS, SSL or none), set up by an admin with a From address and the
  app's public address.
- Messages go out from one background thread, so a slow mail server never slows a page.
- HTML and plain-text parts, written to be accessible.
- Kinds of email: address confirmation, address change, password reset, invitation, security
  notices, test email, job digest.
- Links that give access carry their token in the URL fragment, so it never reaches a log.
- **Guarded mail route**: changing the mail server or public address needs the admin's password,
  tells the other admins through the old server, and for 24 hours afterwards no admin can turn off
  anyone's two-step sign-in or send them a reset link.

## 24. AI providers

`backend/llm_providers.py`, `backend/llm_anthropic.py`, `backend/llm_settings.py`, `backend/app_settings.py`

- **Provider presets**: OpenAI, Anthropic (Claude), Google Gemini, Meta (Muse Spark), Mistral, Groq,
  xAI (Grok), DeepSeek, OpenRouter, Ollama (local, no key), and any custom OpenAI-compatible service.
- Three provider-neutral settings: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`.
- **Web search** (company research, finding offices, chat's *Search the web*) with OpenAI, Claude,
  Gemini, Meta, xAI and OpenRouter. With the others, the app says when a feature needs it.
- **Reasoning level** set separately for scoring, research, cover letters and chat, from the levels
  the model accepts (*Detect supported levels*).
- **Prompt caching** of the documents, by cache marker or cache key depending on the provider.
- **Usage tracking** per account and purpose, shown in the admin panel.
- Moving to a provider at a different address needs the key typed again, so a saved key can't be sent
  somewhere new.
- The cover letter reasoning level also applies to *Build from my blocks* (choosing and ordering blocks,
  naming blocks worth writing).
- Without an AI key the app still collects and filters jobs.

## 25. Themes

`frontend/themes.css`, `frontend/common.js`

- 28 themes plus *System*, picked from live previews and applied at once (open job pages follow):
  - Dark and Light;
  - Catppuccin: Latte, Frappé, Macchiato, Mocha;
  - Rosé Pine: Rosé Pine, Moon, Dawn;
  - Nord: dark and light;
  - Kanagawa: Wave, Dragon, Lotus;
  - Everforest: dark and light;
  - Dyslexia, light and dark (Atkinson Hyperlegible font, wider spacing);
  - Colour-vision themes, each in light and dark: Protanomaly, Deuteranomaly, Tritanomaly,
    Dichromacy, Monochromacy.
- Saved with the account, encrypted, and remembered by the browser; applied before first paint.

## 26. Accessibility

`frontend/`, `tools/a11y_audit.py`, `tools/theme_contrast.py`, `tests/test_accessibility.py`

- Aims at **WCAG 2.2 AA** in every theme, on desktop and phone.
- **Keyboard**: skip link, arrow-key tabs, dialogs that trap and return focus and close with Escape,
  a visible focus ring, and keyboard routes for everything on the map.
- **Screen readers**: landmarks, one heading outline, labelled fields, spoken status messages, and
  form errors under their field.
- **Colour**: 4.5:1 for text and 3:1 for borders and focus rings in every theme. Nothing relies on
  colour alone.
- **Phones and zoom**: works from 320 px wide and at 200% zoom; wide tables stack; 44 px touch
  targets.
- **Motion**: spinners, progress bars and map animations stop when the device asks for less motion.
- Toast messages wait while hovered or focused and have a Dismiss button.

## 27. Security hardening

`backend/hosts.py`, `backend/security_headers.py`, `backend/sessions.py`, `backend/db.py`

- **Host allowlist** against DNS rebinding: only IP addresses, `localhost`, the computer's own names,
  the public address and names listed in `JOBHUNT_ALLOWED_HOSTS` are answered.
- **Content-Security-Policy** built at start-up from the pages, with no inline styles or event
  handlers, plus `nosniff`, `Referrer-Policy`, `Permissions-Policy` and HSTS over HTTPS.
- Every `/api` route needs a session except health and sign-in; changes also need a CSRF token and a
  same-site origin.
- Session cookies are named per port, so two copies on one computer don't sign each other out.
- The session cookie holds a random secret; the database alone can't unwrap anything, and a restart
  signs nobody out.
- The data folder, database and backups are readable by the owner only.
- **Logs never contain user data**: only the kind of failure and an account number.
- Pages and scripts are sent with `Cache-Control: no-cache`, so an update is picked up at once.
- The API documentation pages are off.
- `GET /api/health` for the launcher and CI.
- `.env` is written only through one module with a fixed list of names and validated values.

## 28. Install, start, update and uninstall

`installer/`, `setup.sh`, `start.sh`, `uninstall.sh`, the `.command` files, `.github/workflows/`

- **Windows**: `JobHuntPA-Setup.exe` installs Python if needed (no admin rights), adds Desktop and
  Start-menu shortcuts and registers an uninstaller under Installed apps. `JobHuntPA.exe` starts it.
- **macOS**: `Setup.command` and `Start JobHuntPA.command`.
- **Linux**: `./setup.sh` (adds an applications-menu entry) and `./start.sh`.
- **Setup's six steps**: create a private Python environment, install packages, download the headless
  browser, ask for the AI provider and commute keys, make a shortcut, check the app.
- `--reconfigure` asks the AI questions again.
- **Launcher**: starts the server on port 8000 or the next free one, opens the browser, opens the
  first-run page until an account exists, and just opens the browser if the app is already running.
  Options: `--port`, `--no-browser`.
- **Updating** keeps settings, documents, jobs and keys. Older databases gain missing tables and
  columns automatically, and one-time migrations (accounts, encryption, the job pool) run with a
  backup first.
- **Uninstaller**: offers a backup zip in the Documents folder, removes shortcuts and the app
  folder, asks before removing the shared browser component, and leaves Python installed.
- **Reaching it from other devices**: LAN and Tailscale addresses work; behind `tailscale serve` or
  another proxy, set the public address.
- Environment switches: `JOBHUNT_DATA_DIR`, `JOBHUNT_DB_PATH`, `JOBHUNT_KEY_FILE`,
  `JOBHUNT_ALLOWED_HOSTS`, `JOBHUNT_SCHEDULER=off`.

## 29. Developer tooling

`tools/`, `tests/`, `.github/workflows/`

- **Tests**: `python3 -m unittest discover -s tests -t .`, on a throwaway database, `.env` and key file.
- **CI** (`ci.yml`): runs the real setup and launch on Linux, macOS and Windows on every push.
- **Release** (`release.yml`): pushing tag `v<VERSION>` builds the Windows programs and attaches them
  to a GitHub Release.
- `tools/a11y_audit.py`: drives every page and dialog in headless Chromium and runs axe-core at two
  widths in four themes, with reflow, text-spacing, console and keyboard checks.
- `tools/theme_contrast.py`: checks 71 colour pairs per theme against WCAG AA.
- `tools/readme_screenshots.py` and `tools/demo_data.py`: rebuild the README screenshots from a
  made-up job hunt.
- `tools/bench_jobs_list.py`: times the job list on 5,000 encrypted jobs.
- `tests/test_log_leaks.py`: runs a whole account through the app and checks nothing personal is
  logged.
- Design notes: `docs/design/accounts.md`, `docs/design/ghost-jobs.md`, `docs/design/remaining-phases.md`.
