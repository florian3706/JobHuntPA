> [!WARNING]
> This was vibe-coded in its entirety, as a way to get experience with LLM-based
> development (and save my sanity while job hunting).
> 
> I'm making this public so I can more easily share this with friends who are also job hunting.
> This is provided as is with no warranty. Do whatever you want with this, but don't come crying to
> me if it doesn't work. It's a vibe-coded throwaway tool --- nothing more.

# JobHuntPA

A personal job search dashboard that runs on your own computer. It collects jobs from SEEK and
company careers pages, filters them against your search settings and map pins, scores the rest
against your CV with an AI model of your choice, drafts cover letters, suggests job titles to
search for, researches each employer, merges the same job advertised on several sites into one,
and lets you chat with the AI about any job.

![The Jobs tab: job cards with fit scores, statuses and salaries, under the result of the last scheduled search](docs/screenshots/jobs.png)

*All screenshots show a made-up job hunt in a demo workspace: the person, companies and ads are fictional.*

## A quick look

**A job's full page** (*Open ↗* on any job): the office on a map with the commute there and back,
how well the job fits your CV and where the gaps are, what company research found about the
employer, and the whole ad.

<img src="docs/screenshots/job-page.png" alt="A job's full page: office map and commute, fit requirements and gaps, company profile and the ad" width="760">

**Map**: the jobs your filters show, your pins with their radii, and the jobs in view listed
underneath.

![The Map tab in Jobs mode: job dots, a home pin, a hybrid pin and an onsite pin with their radii, and the list of jobs in view](docs/screenshots/map.png)

**Chat about a job**: ask how well you fit, what to prepare for an interview, or compare jobs.

![The job chat panel next to the job cards, answering how well the candidate fits](docs/screenshots/chat.png)

**Cover letter drafts** from your own documents, to edit, copy or download as a Word file.

![The cover letter window with a draft for one job](docs/screenshots/cover-letter.png)

**Settings**: the theme, scheduled searches (by themselves at set times on workdays, skipping public
holidays), the AI status with the Rescore buttons, and your account (password, two-step sign-in,
recovery codes, background access, download your data). The admin sets the AI provider and the
commute keys in the admin panel.

![The Settings tab: scheduled searches, the AI status and the Rescore buttons](docs/screenshots/settings.png)

**Themes**: Dark (above), Light, the Catppuccin, Rosé Pine, Nord, Kanagawa and Everforest
families, a dyslexia-friendly theme and themes for colour vision deficiencies, picked from live
previews on the Settings tab.

![The Theme card on the Settings tab: each theme is a card with a live preview of the app in it, grouped by family](docs/screenshots/theme-picker.png)

![The Jobs tab in six themes: Light, Catppuccin Mocha, Catppuccin Latte, Dyslexia, Deuteranomaly (dark) and Monochromacy](docs/screenshots/themes.png)

<details>
<summary>More: search setup and the per-source run report</summary>

![Search setup: target titles, keywords, salary floor, work modes, office days and dealbreakers](docs/screenshots/search-setup.png)

![The last run's per-source results: listed, new, kept and excluded jobs for each source](docs/screenshots/run-report.png)

</details>

## What you need

- A computer running Windows 10/11, macOS or Linux, and an internet connection.
- An **API key for an AI model (LLM)** from one of the major providers: [OpenAI](https://platform.openai.com/),
  [Anthropic (Claude)](https://platform.claude.com/), [Google Gemini](https://aistudio.google.com/),
  [Meta](https://dev.meta.ai/) (Muse Spark), Mistral, Groq, xAI (Grok), DeepSeek or OpenRouter, or
  any other service with an OpenAI-compatible API. A model running on your own computer with
  [Ollama](https://ollama.com/) needs no key. Setup lets you pick the provider, then asks for the
  model and your key; you can change them any time in the admin panel (Account > Admin > Server). Using the model costs
  money on your provider account. The app still collects and filters jobs without one, but
  scoring, cover letters, title suggestions, chat and company research need it.
- Company research, finding offices and *Search the web* in chats use the provider's web search,
  which OpenAI, Claude, Gemini, Meta, xAI and OpenRouter offer. With the others everything else
  works, and the app says when a feature needs web search.

Setup takes about 5-10 minutes and needs no technical knowledge. Everything stays on your
computer: your documents, jobs and API key are never uploaded anywhere except the requests the
app makes to your chosen AI provider.

## Install on Windows

1. Go to the [latest release](https://github.com/florian3706/JobHuntPA/releases/latest) and
   download **JobHuntPA-Setup.exe**.
2. Double-click it. Windows may say *"Windows protected your PC"* because the program isn't
   signed: click **More info**, then **Run anyway**.
3. Press Enter to accept the install folder, then answer the questions about your AI provider.
   Setup installs Python for you if needed (no admin rights required).
4. When it finishes, start JobHuntPA from the **JobHuntPA** shortcut on your Desktop or in the Start
   menu. A window opens with the app's messages and your browser opens the dashboard. Keep that
   window open while you use JobHuntPA; close it to stop the app.
5. The first time, the browser asks you to [create your account](#your-account). It is the admin
   account, and anything already in JobHuntPA moves into it.

## Install on macOS

1. On this page click the green **Code** button, then **Download ZIP**, and unzip it (for example
   into your Documents folder).
2. In the unzipped folder, **right-click `Setup.command` and choose Open** (the first time macOS
   asks whether you're sure, because it was downloaded: click **Open**).
3. Follow the questions in the Terminal window. If Python is missing, setup offers to install it
   with Homebrew or opens the python.org download page; install it and run `Setup.command` again.
4. Start JobHuntPA by double-clicking **`Start JobHuntPA.command`**. Keep the Terminal window
   open while you use it; close it to stop the app.
5. The first time, the browser asks you to [create your account](#your-account).

If double-clicking doesn't work: open Terminal, type `cd ` (with a space), drag the folder into
the window, press Enter, then run `./setup.sh` once and `./start.sh` to start.

## Install on Linux

1. Download the ZIP (green **Code** button, then **Download ZIP**) and unzip it, or
   `git clone https://github.com/florian3706/JobHuntPA.git`.
2. Open a terminal in the folder and run `./setup.sh`. If Python 3.10+ or its `venv` module is
   missing, setup shows the command to install it and offers to run it.
3. Start JobHuntPA from your applications menu (setup adds it) or with `./start.sh`.
4. The first time, the browser asks you to [create your account](#your-account).

## Your account

JobHuntPA has accounts, so one copy can serve several people, each with their own workspaces.

- **The first start** opens a page that creates the first account: your email address, an optional
  name and a password of at least 10 characters (a few random words work well; your password manager
  can fill it in). That account is the **admin**. Everything already in JobHuntPA (workspaces, jobs,
  documents, searches and settings) moves into it. The page's address carries a one-time setup
  code; if the browser didn't open, the address is printed in the JobHuntPA window, and the code is
  in `data/first-run.txt`. Once the account exists the code stops working and the file is deleted.
- **Recovery codes:** the first-run page then shows 8 recovery codes. Copy, download or print them
  and keep them somewhere safe: if you forget your password, they are how you get back in and keep
  your data. A reminder stays at the top of the dashboard until you confirm you've saved them.
- **The setup walkthrough:** once your recovery codes are saved, a new account opens a short
  walkthrough (`welcome.html`) instead of the dashboard. It starts with **choosing a theme**, which
  changes the page as you pick it, then sets up your search a step at a time: job titles; places,
  remote/hybrid/onsite work, days in the office and your home suburb; a salary floor and words or
  industries to include or avoid; and an optional résumé (on the Documents tab, where it powers the
  fit scores). Every step has Back, Next and Skip for now, saves as you go, and the walkthrough
  picks up where you left off if you close the page. "Skip the walkthrough" at the bottom goes
  straight to the dashboard; everything is on the Search setup tab later. On an existing install the
  first admin gets a short version: choose a theme, then keep the search settings that moved into
  the account or review them step by step. Your theme is saved with your account (encrypted with
  your key), so a new browser or device shows it too.
- **Signing in:** after that, JobHuntPA opens the sign-in page whenever you aren't signed in. Tick
  *Keep me signed in* on your own device to stay signed in for up to 30 days of not using it
  (otherwise 24 hours). **Account > Sign out** at the top right signs this browser out. After 5
  wrong passwords in a row, the next try has to wait 30 seconds, then longer each time, up to 15 minutes.
- **Two-step sign-in** (Settings > Two-step sign-in): add an authenticator app (any app that makes 6-digit
  codes) by scanning the QR code, or by typing the setup key if you can't scan. After that, signing
  in asks for the app's code after your password. A code works once, and a recovery code works instead
  of the app (each recovery code once; the page says how many are left). Turning it off needs your
  password and a code.
- **Forgot your password?** On the sign-in page, *Trouble signing in?* then *I have a recovery code*:
  enter your email, a recovery code and a new password. **Your data is kept**, because the recovery
  code unlocks it; every other browser is signed out. With two-step sign-in on, you also enter the
  app's code or a second recovery code. This works without any email set up.
- **Password, recovery codes and sessions** (Settings > Password, Recovery codes and Signed-in devices): change your password (your data
  stays readable and other browsers are signed out unless you untick that), make 8 new recovery
  codes (the old ones stop working; with two-step sign-in on you also enter the app's code; you save
  the new ones the same way as the first set), and *Sign out
  everywhere else*. Anything sensitive asks you to confirm your password first, if you haven't
  entered it in the last 15 minutes.
- **Delete your account** (Settings > Delete account): enter your password and everything of yours is
  removed: workspaces, jobs, documents and the uploaded files, chats, cover letters, keys and
  sessions. Job postings and company research that everyone shares stay, because they hold nothing of
  yours. It can't be undone, and it waits while one of your searches is running. An admin can delete
  their own account while another admin remains (the admin key is replaced for the others, so the one
  you held opens nothing new); the last admin can't, because nobody would be left to run JobHuntPA.
  Make someone else an admin first. The admin panel's **delete their account** removes exactly the same things.
- **Download your data:** **Settings > Download your data** makes a zip file with everything JobHuntPA
  keeps for your account, as Markdown files in folders made for people: `Account.md`; for each
  workspace its search setup, sources, documents (with the original files you uploaded, byte for byte),
  jobs sorted into folders by your status (each with its fit score, description, cover letter and the
  other sites that advertised it), jobs the filters left out, your chats and search history; and the
  company research behind your jobs. Links between the files work offline. It asks for your password
  again if you haven't entered it in the last 15 minutes, and is made in memory, so nothing is written to
  disk on the server (an export is refused above 200 MB of files, and the server makes only a couple at
  a time: try again in a minute). **The zip is not encrypted**: keep it somewhere safe. It holds only your own data
  (admins can't export other people's), and never your password, recovery codes or keys.
- **What only the admin sees:** the AI provider, model, API key and reasoning levels, and the
  commute API keys, are set by the admin for everyone (admin panel, Server). Other accounts see whether AI
  scoring and commute times are available, and everything of their own: workspaces, search setup,
  documents, schedule and theme. Each workspace belongs to one account; nobody else can open it.
- **Letting other people in (admins):** the admin chooses who can sign up: **by invitation** (the
  default: the admin makes an invite link, good for 7 days and one sign-up), **open** (anyone with
  the address) or **closed**. New people get their own account, recovery codes and walkthrough, as
  above. Admins see each person's email, name, role, last day active (the day only), whether
  two-step sign-in and background access are on, and their membership, and they can turn accounts
  off and on, turn off someone's two-step sign-in, give free periods and exempt people from
  payment, and see the AI usage figures. They **never** see anyone's searches, documents or jobs,
  and people's email addresses and names are encrypted so that only signed-in admins can open them
  (the admin key; see below). Every admin action is written to an audit log.
- **Headless servers and CI:** instead of the first-run page, start JobHuntPA with
  `JOBHUNT_ADMIN_EMAIL` and `JOBHUNT_ADMIN_PASSWORD` in its environment (exported, or in the
  service definition; never in `.env`, which is ignored for these two) and that admin is created
  at start-up. Its recovery codes are shown after its first sign-in.

### How your data is protected

Your searches, documents and jobs are **encrypted with a key that only your password or a recovery
code can unlock**. Someone who copies the database, its backups or the `data/` folder can't read
them, not even the person who runs this JobHuntPA, unless you allow background access (below).

> **Save your recovery codes.** If you forget your password, a recovery code is the only way to get
> your data back. Without one, your encrypted data can't be opened again by anyone.

**Encrypted** (under your key):
- each workspace's name, search setup (titles, keywords, salary, places, commute stations and
  times), dealbreakers and map pins;
- your jobs: their status, whether you hid them, why the filters excluded them, distance and
  place, the office you set or that was found, your call on the location, and anything that is only
  yours, such as a full ad you uploaded or a job imported from a saved page (its address too);
- fit scores and their evidence, cover letters, chats, and what each search run found;
- your documents and notes (title, text and concise version), and **uploaded files**: they are
  stored encrypted (`data/uploads/*.sealed`), and their text is read in memory, never written out;
- company careers pages you added, and your commute lookups (cached per account).

Even whether a job is hidden or excluded doesn't show: those fields are always stored encrypted,
and an excluded job looks the same as one you kept.

**Not encrypted**, because the app needs them without your key or they aren't yours:
- **job ads themselves**: ads are public, so they're stored once and shared by everyone's
  searches. Which ads your workspace has is visible (not what you think of them);
- company research (public, shared by everyone), the place-name cache and AI usage counts;
- scheduled search times, run times and states, the kind of each document, and your account's
  sign-in details (your email address and name are encrypted for admins only);
- sizes and dates: how many jobs or documents you have, how long a document is, when things
  happened.

**The limits.** The AI provider sees what it scores, chats about or writes. The running app holds
your data in memory while you use it. Someone who changes the app's code on the server could capture
your password when you next sign in. With **[background access](#background-access)** on, whoever
runs the server could read your data. On SSDs and SD cards, deleted data may linger in storage the
disk manages itself.

#### Background access

Settings > **Background access** is a switch for your account.

- **Off** (the default for a new account): only you can unlock your data. JobHuntPA can't run
  searches while you're away, so **scheduled searches are skipped** (the schedule card says so).
- **On** (the first account has it on): JobHuntPA keeps a key on the server so it can run scheduled
  searches while you're away. The server's operator could technically read your data.

How it works: turning it on (it asks for your password again if you haven't typed it in the last 15
minutes) stores a copy of your data key locked with the server's own key, in `data/server-keys.json`.
Whoever has the database **and** that file can read the data of every account with it on. Turning it
off deletes that copy at once. The setup walkthrough's *Automatic searches* step turns it on for you
if you tick it. Back up `data/server-keys.json` with the database.

## Sharing JobHuntPA (admins)

Admins open the **admin panel** from **Account > Admin** (`admin.html`). It never shows anyone's jobs,
documents or searches. It has five parts:

- **Summary and AI use:** people, admins, how many were active in 7 and 30 days and how many joined
  in 30 days, and the AI service's use for the last 7 to 365 days: requests, tokens in and out, and
  how much came from the cache, by model and purpose, by person and by day.
- **People:** each account's email, name, role, status, last active day and membership. The columns
  sort (below 640 px wide, each person becomes a card with a "Sort by" menu). Buttons make someone an
  admin or a regular user, disable or enable them, **unlock** someone who has to wait after wrong
  passwords, turn off their two-step sign-in, **delete their account** (with everything in it, their
  uploaded files included; not your own, not the last admin and not while their search is running; deleting
  an admin replaces the admin key like demoting does), and open their
  membership: **exempt from payment**, the number of free searches used, and **free periods**
  (start, end or number of days, a note only admins see; "End now" keeps the period in the history).
  *Send reset email* answers that email isn't set up yet; until it is, the person can use a recovery
  code on the sign-in page to choose a new password.
- **Sign-up and invitations:** choose who can create an account: **only with an invitation link** (the
  default), **anyone** (open sign-up) or **nobody**. An invitation link (`signup.html?invite=…`) is shown
  once when you make it, works for one sign-up and lasts 7 days. Give an email address to fill in the
  form for them; the address then counts as verified. Send the link yourself for now. New people get
  recovery codes and the setup walkthrough, as on first run. Open sign-up lets anyone who can reach the
  server use the AI service you pay for, so use it only on a private network.
- **Server:** the AI provider (provider, model, key, reasoning levels, *Test connection*) and the commute
  keys, which used to be on the Settings tab (changes are logged by name, never the value);
  **Replace the server key**; and the report of
  **unencrypted copies of data** with *Delete these files now* for the backups and old page cache that
  the one-time encryption leaves.
- **Audit log:** every admin action, newest first: what was done, by whom, to whom, and details such as
  dates or counts (never anyone's content). The details are readable while you hold the current admin key.

**Roles and the admin details key.** People's emails, names, notes and billing details are encrypted
so that only admins can open them, with an *admin key* that each admin holds a copy of (sealed to their
own password). Making someone an admin shares the key with them. Making an admin a regular user
**replaces the key** and shares the new one only with the admins who remain, so the person who was
demoted can't open anything new. These actions ask for your password again if you haven't entered it in
the last 15 minutes. JobHuntPA keeps you from removing the last admin and warns while only one exists:
with a single admin who loses their password and recovery codes, nobody can read people's details. If
an admin shows "Admin key not shared with them", use *Share the admin key again* (another admin does
this). **Recovering when no admin can open the key:** stop JobHuntPA and run
`python -m backend.keys new-admin-key --email you@example.com`. That person becomes an admin with a new
key, and everyone's details are sealed to it again as they sign in.

**Rotating the server key.** The server key (`data/server-keys.json`) protects the keys kept for
background searches and two-step sign-in. *Replace the server key* moves them all to a new one and drops
the old one; if something can't be moved, the old key is kept and nothing is lost. You can also run
`python -m backend.keys rotate-kek` with JobHuntPA stopped (these commands refuse while it is running; if
it isn't, delete `data/running.json`). A running JobHuntPA also won't overwrite a key file that something
else changed: it stops with "The key file was changed by something else", and a restart picks up the new file.

## Using it

The dashboard opens at http://127.0.0.1:8000 (sign in first). Start in **Search setup** (job titles, keywords,
salary, work modes and [company careers pages](#adding-company-careers-pages)), upload your CV under **Documents**, drop pins for
where you can commute on the **Map**, then click **Run search** on the Jobs tab. Separate searches
can live in their own **workspaces** (switcher at the top right).

- **Scheduled searches** (Settings > Scheduled searches): the search runs by itself at 10:00,
  12:00, 14:00 and 16:00 Monday to Friday, skipping public holidays, including substitute days
  (a holiday that falls on a weekend moves to the Monday). Change the times and days, or switch it
  off, per workspace. Pick your state: it sets which public holidays apply and the time zone of the
  times. Searches only run while JobHuntPA is running; a search missed because it was closed runs
  if you start it within the hour. Each run scores the new jobs it finds, like **Run search**.
  **Scheduled searches need [background access](#background-access)** (Settings > Background
  access): your jobs are encrypted, so while you're signed out the server can only search for you if
  it holds a key. Without it each slot is skipped, and the schedule card says "Scheduled search
  skipped: background access is off".
- **Tracking and tidying**: set each job's status (to review, shortlisted, applied, interviewing,
  rejected, not interested). Tick jobs, or filter the list and tick **Select all shown**, to change
  status or **Hide** them in bulk (only jobs on screen are ever affected); hidden jobs stay hidden when a search finds them again, aren't scored or
  researched, and come back with *Show hidden*.
- **Softening the office-days rule**: if you set a maximum number of office days for hybrid roles,
  the Jobs tab's **Office days (temporary)** menu lets you allow extra days (up to 5) for now. Jobs that
  only that rule excluded reappear, marked *shown by temporary +N office days*, and **Score them**
  scores any that were never scored. Nothing is saved: *Back to my rule*, reloading the page or
  switching workspace restores your saved limit.
- **Government jobs**: Australian Public Service jobs come from APSJobs. Add a source such as
  `https://www.apsjobs.gov.au/s/job-search?department=Services%20Australia` (the agency name as
  APSJobs lists it) or `...?searchString=product%20manager` for a search across all agencies.
  Careers pages that publish an RSS feed (e.g. SBS) can be added by the feed's URL.
- **Shared job pool**: every job any search finds is stored once and shared by all workspaces, so
  an ad's full page is fetched only once. Each search also brings in jobs that other searches
  already found, if they match your own job titles or include keywords and pass your filters
  (dealbreakers, salary, work mode, pins). A workspace with no titles or keywords gets none.
  Everything you do with a job stays with your workspace: status, hidden, why it's excluded, score,
  office, location call, cover letter and chats.
- **Closed jobs**: a job counts as closed when its page is gone, when its employer's complete job
  list no longer has it, or when no search has listed it for 45 days (how SEEK jobs close, since
  SEEK results are only the first page). A closed job that is listed again reopens. Jobs missing
  from one search's results aren't closed: another search may still list them.
- **Full SEEK ads**: SEEK jobs marked *summary only* have an **Upload full ad** button. Save the
  ad's page from SEEK in your browser and upload it to score the job on the whole ad. The full ad
  you upload stays private to your workspace: other workspaces with the same job keep seeing SEEK's
  summary. Jobs from pages you import (*Search setup → Import saved pages*) are private too,
  unless a search has already found them.
- **Company info** (ⓘ next to the employer, and the job page): what company research found
  (business, ownership, offices, employee reviews and news) is shared by all workspaces, so a
  company is researched once. **Fill missing info** researches companies with no profile yet, or one
  older than 30 days. **Research again** in a company's profile refreshes the employee reviews and
  the news when they're more than a day old and keeps the rest; if everything is recent, it shows
  the saved profile without asking the AI again. A profile older than 30 days is researched in full.
- **Suggested job titles** (Documents tab): the AI reads your documents and suggests titles to
  search for; add them to the current search or start a new workspace with them.
- **Notes** (Documents tab): under *Write a note*, give a note a title, type its text, pick a
  type and click **Save note**. Use notes for things that aren't in your CV, such as a project you
  led, or what you want from your next job. A note works like an uploaded document: tick or untick
  *use for scoring*, **Open** it to edit its text, title or type, or **Delete** it. Notes are
  plain text and are stored only in JobHuntPA.
- **Preferences** (a document type for uploads and notes, shown as *Preferences (private)*): say
  what you want from a job, such as the commute you'll accept or work you want to avoid. The AI uses
  them when scoring jobs (it names jobs that conflict with them), in job chat and in suggested job
  titles. They are never treated as experience, and they are never sent when drafting cover letters.
- **Concise versions**: **Open** a document and use *Create concise version* to have the AI write a
  shorter version of it. Read it against the original and edit it, then *Save concise version*.
  *Write it again* replaces it. Choose whether the AI gets *the original* or *the concise
  version*. Switching, or editing the version in use, marks existing scores as out of date, so
  they're recalculated with the new text. For an uploaded file, only the concise version can be edited.
- **Summary of your documents**: *Create summary of ticked documents* merges every ticked document
  (not preferences) into one new document, with each fact once and every employer, date, number and
  qualification kept. It takes a few minutes. The summary opens so you can read and edit it. Once
  you're happy, click *Untick the originals* so the same facts aren't sent twice (or *Keep both ticked*).
- **What documents cost**: your ticked documents are sent to the AI with every job it scores and
  every chat question, so long ones add up. JobHuntPA asks providers to cache them so repeats cost
  less. For Claude (directly or through OpenRouter) it marks the documents for caching; for
  OpenAI, Meta and Mistral it sends a cache key. OpenAI, Meta, xAI, Gemini, DeepSeek, Groq and OpenRouter's other
  models cache on their own, and Ollama runs on your computer. Either way the documents are still
  sent each time; caching only makes a repeat cheaper and faster. Concise versions and a summary
  cut the size, and with it the cost.
- **Cover letters**: *Draft cover letter* on any job writes a first draft from your documents
  (upload earlier cover letters as "Cover letter" documents to match your style). Edit it, then
  copy it or download it as a Word file. Always check it before sending.
- **Chat about jobs**: *Chat* on a job (or tick several and click **Chat about N jobs**) opens a
  chat on the right. The AI sees the ads, your documents, the fit assessment, the company info and
  any cover-letter draft, so you can ask what the role really involves, how to handle your gaps,
  what to ask in an interview, or which of several jobs suits you best. Tick *Search the web* for
  things your saved data doesn't cover (news, salaries); the pages it used are listed under the
  answer. Chats are saved per job set; *New chat* starts over.
- **Duplicate ads**: the same job advertised on several sites (say SEEK and the employer's own
  careers page) becomes one card, with the other sites under *Also on*. The app merges ads it's
  sure about after each search and lists the doubtful ones under **Duplicates** for you to decide
  (*Same job: merge* / *Different jobs*). Your status, cover letter and score carry over to the
  merged card. *split* under *Also on* undoes a merge; **Merge as one job** (tick two or more
  jobs) merges ads the app missed.
- **Job page**: *Open ↗* on a job opens it in a new tab: the whole ad, the fit and gaps, a map of
  the office, the company profile, and the commute there and back at peak times (arriving by 9:00,
  leaving at 17:00 on a Tuesday; change these under Search setup > Commute). Public transport is
  planned from the stations you list there (e.g. Hornsby Station, Asquith Station), including
  changes, metro, buses and walking at the other end; driving starts at your home pin. See
  [Commute times](#commute-times-optional) for the two free keys this needs.
- **Map**: two modes. **Jobs** shows the jobs your Jobs tab filters show, with your pins and their
  radii, and lists the jobs in view under the map (click a dot, or **Show** in the list, to see a
  place's jobs; **Open ↗** for a job's full page in a new tab); nothing can be changed by accident.
  **Edit pins** drops a pin where you click or tap (drag to move). Without a mouse, move the map
  (arrow keys) until the cross in its middle is on the place and press **Drop a pin at the cross**;
  **Move to the cross** in the pin list moves a pin there. *Only list pins in view* shortens the
  pin list to the area on screen.
- **Office locations**: most ads only say "Sydney NSW", or an area such as "North West & Hills District"
  (placed at its main centre, here Castle Hill, for the location filter). Instead of trusting that, such jobs show
  **office unknown** until the office is found: the scorer reads it from the full ad, and company
  research (**Fill missing info**) finds the employer's office addresses. An office named in the ad
  wins; otherwise, with several offices in that city, the one closest to your commute pins is used
  (you can pick another on the job page). Recruitment agencies rarely name the client, so their ads are left to you: on the job
  page set the office (address or suburb, or one of the employer's offices), or make the call with
  **Location OK** / **Too far**. The office then drives the location filter and distance.
- **Themes** (Settings > Theme, remembered per browser): each theme is a card with a small live
  preview of the app in it; click or tap one, or move through them with the arrow keys, and it
  applies at once (open job pages follow). Dark (the default), Light, or *System* to follow your
  computer; Catppuccin (Latte, Frappé, Macchiato, Mocha); Rosé Pine (Rosé Pine, Moon, Dawn); Nord
  (dark, and a light variant made from Nord's colours); Kanagawa (Wave, Dragon, Lotus); Everforest
  (dark and light, medium contrast); a **Dyslexia** theme (warm low-glare background, Atkinson
  Hyperlegible font, wider letter and word spacing) in light and dark; and colour-vision themes
  (Protanomaly, Deuteranomaly, Tritanomaly, Dichromacy, Monochromacy), each in light and dark. The
  family themes use their projects' official palettes; where a colour fell short of the WCAG AA
  contrast minimums it is a little lighter or darker in the same hue (themes.css lists each change).
  Labels, marks or shapes always accompany colours, so nothing relies on colour alone. See
  [Accessibility](#accessibility).
- **Reasoning level** (admin panel > Server): how long the AI thinks, set separately for scoring,
  cover letters, company research and job chat. *Detect supported levels* shows what your model accepts.

## Adding company careers pages

SEEK has most jobs, but many employers also (or only) advertise on their own careers page. Add
those under **Search setup > Company sources**, once per workspace:

1. Find the company's page that **lists** its open jobs (usually behind "Careers", "Join us" or
   "Open roles"): the list, not a single ad.
2. Paste its address into **Careers page URL**, type the company's name if you like (shown on its
   jobs when the page doesn't say), and click **Add**.
3. Click **Test**. It shows what the app finds without saving anything: how many jobs, how it read
   them and a few titles. It can take a minute, because the app waits between requests to a site.
4. From now on, **Run search** and the scheduled searches collect its jobs. Untick a source to
   skip it for a while; **Delete** removes it (its jobs stay in your list).

![Company sources: careers pages with their last results, one blocked by the site](docs/screenshots/company-sources.png)

**Which address to use.** Many companies run their jobs on a hosted job board. The app reads
those boards' own job feeds, which is quick and complete, so if a careers page links to one, the
board's address works best (the careers page itself usually works too, as the app follows the link):

| Job board | Address looks like |
| --- | --- |
| Workable | `https://apply.workable.com/company/` |
| Greenhouse | `https://job-boards.greenhouse.io/company` |
| Lever | `https://jobs.lever.co/company` |
| Ashby | `https://jobs.ashbyhq.com/company` |
| SmartRecruiters | `https://jobs.smartrecruiters.com/Company` |
| Workday | `https://company.wd3.myworkdayjobs.com/en-US/Careers` |

Any other careers page works if its jobs are on the page or one click away: the app reads the job
data many sites embed for search engines, or else follows the links to each ad (and "next page"
links), and opens pages that only appear in a real browser in a hidden one. A job feed (RSS)
can be added by its address, and for government jobs add an APSJobs search (see *Government
jobs* above).

**When a source doesn't work**, its last result in the list (and **Test**) says why:

- *refused automated access* or *answered with a bot check*: the site blocks tools like this one.
  JobHuntPA never gets around that; look for the company's job board link instead, or check the
  site yourself now and then.
- *robots.txt disallows ...*: the site asks automated tools to stay out of that page, and the
  app respects it.
- *no job postings found*: check you added the page with the list of jobs. Some sites only show
  the list after you search: open it in your browser, click *Search* or *View all jobs*, and add the
  address you end up on.

Each source brings in at most 60 new full ads per search; the rest follow on the next search.

## Updating

- **Windows:** download the newest JobHuntPA-Setup.exe and run it again with the same folder.
- **macOS/Linux:** download the new ZIP and copy its contents over your folder (or `git pull`),
  then run setup again.

Your settings, documents, jobs and API key are kept.

The first start after updating to the version with accounts opens the page that creates your
account (see [Your account](#your-account)): it becomes the admin, and all your existing
workspaces, jobs, documents and settings move into it. Until then JobHuntPA shows nothing else and
scheduled searches wait. It also creates `data/server-keys.json`, the server's key file: it belongs
with your database, so back them up together.

**Encrypting your existing data (once).** The first time the admin signs in to the version that
encrypts data (or right away, on the first-run page), JobHuntPA encrypts everything from before with
the admin's key:

1. It saves a backup first, `data/jobhunt.db.bak-before-encryption` (unencrypted).
2. It encrypts your uploaded files, then your data, in small steps. A few thousand jobs take seconds
   on a computer and up to a minute or two on a Raspberry Pi. Meanwhile the dashboard shows the
   progress and reloads by itself when it's done. If JobHuntPA is stopped halfway, it carries on the
   next time the admin signs in; nothing is lost or encrypted twice.
3. It checks that nothing is left unencrypted, then rebuilds the database file so the old
   unencrypted pages are gone.

Afterwards the dashboard shows admins a notice that links to the admin panel's **Server** section.
Its "Unencrypted copies of data" part lists the files that still hold your data unencrypted (that
backup, older `data/jobhunt.db.bak-*` backups and the `data/cache` pages of an earlier version) with
**Delete these files now**, which asks for your password, until they're gone. Keep the backup only if you might go back to an older
version: **older versions can't read the encrypted database**. To go back, stop JobHuntPA, install
the older version, and replace `data/jobhunt.db` with the backup (delete `data/jobhunt.db-wal` and
`data/jobhunt.db-shm` too); anything you did since the update is lost.

The first start after updating to the version with the shared job pool moves your jobs into it.
This happens once and takes a few seconds; a backup is saved first as
`data/jobhunt.db.bak-before-job-pool`. Copies of the same ad in different workspaces become one
shared job, and each workspace keeps its own status, scores, cover letters and chats. Full SEEK ads
you uploaded stay private to their workspace. SEEK jobs that no search has listed for 45 days now
show as closed.

## Changing your AI provider or key

Only the admin account can see or change these; they apply to everyone. Open the **admin panel** (Account > Admin), **Server > AI provider**: pick the provider (it fills in the API address and suggests models),
choose the model, paste your key and click **Save**. It takes effect straight away; click
**Detect supported levels** afterwards, as each model offers different reasoning levels, and
**Test connection** to check the key. The key is saved in the `.env` file in the app folder and
is never shown again in full. Changing to a provider at a different address needs the key again,
so a saved key can't be sent anywhere new.

You can also run setup again with `--reconfigure` (macOS/Linux: `./setup.sh --reconfigure`), or
edit `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL` in `.env` and restart the app.

## Commute times (optional)

The commute on each job's page uses two free services. The admin adds their keys in the admin panel under **Server > Commute data** (they work straight away for everyone; **Test saved keys** checks them), or when setup
asks, or in `.env` followed by a restart:

- `TFNSW_API_KEY`: public transport in NSW. Sign up at https://opendata.transport.nsw.gov.au,
  create an application, and copy its API key. Without it the page shows driving only.
- `TOMTOM_API_KEY`: driving times with peak-hour traffic. Sign up at
  https://developer.tomtom.com and copy the default key. Without it, driving times come from
  OSRM's public server and ignore traffic (the page says so).

## Uninstalling

Close JobHuntPA first, then:

- **Windows:** *Settings > Apps > Installed apps > JobHuntPA > Uninstall*, or the Start-menu
  shortcut **Uninstall JobHuntPA**.
- **macOS:** double-click **`Uninstall JobHuntPA.command`** in the app folder (right-click > Open
  the first time).
- **Linux:** run `./uninstall.sh` in the app folder.

The uninstaller first offers to save a **backup** (your jobs, documents, workspaces, accounts,
settings, API keys and the server's key file `data/server-keys.json`) as
`JobHuntPA-backup-<date>.zip` in your Documents folder, readable only by you. Keep it private. It
then removes the shortcuts and the app folder. It asks before removing the shared browser
component, and leaves Python installed because other programs may use it. To restore a backup,
install JobHuntPA again and unzip the backup into the new app folder: the database and
`data/server-keys.json` must stay together, and you still sign in with your password.

## Troubleshooting

- **The browser shows nothing:** check the JobHuntPA window for error messages. If it says
  another copy is running, use that one, or restart your computer.
- **"LLM not configured" in the app:** your API settings are missing; see *Changing your AI
  provider or key*. Accounts other than the admin see "AI not set up": the admin sets it.
- **The first-run page says the setup code isn't right:** close JobHuntPA and start it again; it
  prints a fresh link, and the code is in `data/first-run.txt`. The code only works until the first
  account exists; after that, sign in instead.
- **Forgotten password:** on the sign-in page choose *Trouble signing in?* and use a recovery code to set
  a new password and keep your data. Without your password or a recovery code, your encrypted data
  can't be read, by you, the admin or anyone else.
- **Lost your authenticator app:** sign in with a recovery code instead of the app's code, then turn
  two-step sign-in off and on again in Settings > Two-step sign-in.
- **The last admin lost their password and every recovery code:** people can still sign in, but
  admins can't read names and email addresses any more ("Details unavailable until this person
  signs in"). Stop JobHuntPA and run `python -m backend.keys new-admin-key --email you@example.com`
  (in the app folder, with the app's Python: `.venv/bin/python` on macOS and Linux). That account
  becomes an admin with a new admin key; everyone's details are re-sealed to it when they next
  sign in (at once for people with background access).
- **Changing the server key:** the admin panel's *Replace the server key* button (Server) (it asks for your
  password again), or with JobHuntPA stopped, `python -m backend.keys rotate-kek`. The server key
  opens background-access keys and two-step secrets; rotating it is safe to interrupt, and the next
  start finishes it.
- **"JobHuntPA is waiting for its admin to sign in, to finish encrypting the data":** the one-time
  encryption after an update needs the first admin's password; it carries on when they sign in.
- **"Encrypting your data stopped (...)":** JobHuntPA tries again a minute later and whenever the
  admin signs in. If it keeps stopping, the JobHuntPA window says why; your data is safe meanwhile,
  and the backup `data/jobhunt.db.bak-before-encryption` was saved before anything changed.
- **JobHuntPA stops at start-up with "Restore data/server-keys.json from your backup":** the key file
  is missing or isn't the one that belongs to this database. Put back the `data/server-keys.json`
  from the same backup as the database. A new key file is never made while accounts exist.
- **The key file is lost and there is no backup:** nobody can sign in, because email addresses are
  found through that file. With JobHuntPA stopped (the command refuses while it is running), an active
  admin can make a new one: run
  `.venv/bin/python -m backend.keys recover-lost --admin-email you@example.com` (on Windows,
  `.venv\Scripts\python -m backend.keys ...`) from the JobHuntPA folder and type that admin's
  password when asked. It writes a new `data/server-keys.json` and works out every account's email
  lookup again, so everyone signs in as before and **keeps their data**. What everyone must then
  redo: **turn background access on again** (Settings > Background access; until then scheduled
  searches are skipped) and, if they used it, **set up two-step sign-in again**. If it says some
  accounts "could not be worked out", those accounts can't sign in until the original key file is
  back. Then back up the new file with the database.
- **Some company sites return no jobs:** they may block automated access or need the browser
  component; setup's step 3 installs it. On Linux you can fix a failed browser install with
  `.venv/bin/python -m playwright install --with-deps chromium`.

## Accessibility

JobHuntPA aims to meet **WCAG 2.2 level AA** in every theme, in desktop and phone browsers.

- **Keyboard:** everything works without a mouse. *Skip to main content* is the first thing Tab
  reaches; the tabs move with the arrow keys; dialogs keep focus inside, close with Escape and
  return focus to the button that opened them; focus is always visible and never hidden under the
  sticky header. The map has keyboard routes for everything: the list under it (**Show** opens a
  place's jobs), and in pin mode **Drop a pin at the cross** and **Move to the cross** instead of
  clicking and dragging. The theme picker is a radio group: arrow keys move through the themes.
- **Screen readers:** landmarks, one heading outline, labels on every field, and spoken status
  messages for searches, scoring, saves and errors. Form errors appear under the field they belong
  to and say how to fix it.
- **Colour:** every theme meets 4.5:1 for text and 3:1 for field borders, the focus ring and
  selected states (`tools/theme_contrast.py` checks 71 colour pairs per theme). Nothing is told by
  colour alone: scores are numbers, statuses and work modes are words, excluded jobs on the map are
  hollow dashed dots, the pin kinds have solid, dashed and dotted rings, and messages carry a mark.
- **Phones and zoom:** the app works from 320 px wide, at 200% zoom and with wider text spacing
  without sideways scrolling; wide tables turn into stacked rows; the header only sticks where
  there's room; touch screens get 44 px controls and 16 px text in fields (no zooming on iPhone).
- **Motion:** with *reduce motion* set on the device, spinners, progress bars and map animations
  stop.

**How it was tested** (October 2026; the account cards, admin panel, sign-up and recover pages have
only the automated checks in `tests/test_accessibility.py`, not this run): [axe-core](https://github.com/dequelabs/axe-core) 4.13 with
the WCAG 2.0, 2.1 and 2.2 A and AA rules plus best practices, in headless Chromium (Playwright),
on 19 screens and states of the dashboard and job page (every tab, every dialog, the chat, the map
in both modes, loading, empty and error states) at 320, 375, 768 and 1280 px wide: no violations. Its colour
contrast rule ran on 12 of those screens in all 28 themes: no violations. Scripts also pressed Tab
through every page (a visible focus ring on every stop, none hidden, no traps), drove the dialogs,
the chat, the tabs, the theme picker and the map by keyboard, measured touch targets (24 px or more,
or spaced), and checked reflow at 320 px, 200% zoom and WCAG text spacing. `tests/test_accessibility.py`
keeps the contrast and the pages' basics (language, viewport, skip link, headings, labels) checked
on every test run.

**Known limitations:**
- Not yet tried with real screen readers (NVDA, JAWS, VoiceOver, TalkBack) or by disabled users: the
  checks above are automated, plus a review of the code.
- The map tiles are OpenStreetMap's own pictures and stay light in every theme; street names on them
  are outside the app's control. The job dots and pins on the map aren't keyboard stops themselves:
  the list under the map is the keyboard route. Panning the map takes a drag, the arrow keys, or the
  zoom buttons and a double-click.
- Text from elsewhere (job ads, company research, AI answers) is shown as it comes.
- Messages in the corner disappear after a few seconds; they wait while the pointer or focus is on
  them, have a Dismiss button, and screen readers announce them.
- In Chrome on Windows, the arrow keys change a closed drop-down at once, so arrowing through the
  Workspace menu switches workspace at each step: open it first (Alt+Down).
- The colour-vision themes' colours were simulated for colour vision deficiencies in ProjectTimeline;
  the small lightness changes made here for contrast keep their hues but weren't simulated again.

## For developers

Setup lives in one place, `installer/installer.py` (standard library only), called by `setup.sh`
(Linux/macOS) and by `JobHuntPA-Setup.exe` (`installer/windows_setup.py`). Removal is
`installer/uninstaller.py`, called by `uninstall.sh` and built into `JobHuntPA-Uninstall.exe`. Starting goes through
`installer/launch.py`, called by `start.sh` and `JobHuntPA.exe` (`installer/windows_launch.py`).
**Any change to dependencies, configuration, data folders or how the app starts must update
these scripts**; `.github/workflows/ci.yml` runs the real setup and launch on Linux, macOS and
Windows on every push to catch breakage.

Manual run: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, copy
`.env.example` to `.env`, then `.venv/bin/python installer/launch.py`. Tests:
`python3 -m unittest discover -s tests -t .`

**Accounts in development and CI:** `JOBHUNT_ADMIN_EMAIL` and `JOBHUNT_ADMIN_PASSWORD` in the process
environment (not `.env`) create the first admin at start-up, which is how CI signs in
(`/api/auth/login`, then the session cookie). Every `/api` route needs a session except
`/api/health` and the sign-in endpoints; POST, PUT, PATCH and DELETE also need the page's
`X-CSRF-Token` (from `/api/auth/me`) and must not come from another site. The tests run on a throwaway
database, `.env` and key file, with `JOBHUNT_TESTING=1` and the fast `JOBHUNT_KDF_PROFILE=test`
(tests/__init__.py); `tests/helpers.py` gives signed-in clients (`with helpers.client() as c:`),
other accounts (`helpers.make_user()`) and the admin's key scope (`helpers.as_admin()`). Code and the
design notes: `backend/users.py` (accounts, first run, deleting an account), `backend/sessions.py`
(sessions, CSRF, the checks on every request), `backend/auth.py` (signing in, sign-up, the two-step code,
resetting with a recovery code), `backend/security.py` (password, two-step setup, recovery codes, other
sessions), `backend/totp.py` (authenticator codes), `backend/admin.py` (the admin panel's API) with
`backend/admin_key.py` (the admin key: grants, replacing it, healing details), `backend/signup.py` and
`backend/tokens.py` (sign-up modes and invitations), `backend/key_rotation.py` (the server key),
`backend/background.py` (background access), `backend/key_recovery.py` (the `recover-lost` command),
`backend/export.py` (data export), `backend/account.py` (preferences, the walkthrough step, deleting
an account), `docs/design/accounts.md`. `python -m backend.keys` has `bench-kdf`, `rotate-kek`,
`new-admin-key` and `recover-lost`. The pages' scripts beyond the dashboard's: `frontend/account.js`
(account cards), `background.js`, `export.js`, `admin.js` and `server-settings.js` (the admin panel),
`dialogs.js` (its dialogs) and `signup.js`.

**Encrypted columns** (`backend/crypto.py`, `docs/design/accounts.md` §2): personal columns use the
`Sealed*` types, which encrypt under the key of the request's account (or the background run's). The
rules:
- Never filter, sort, group, `DISTINCT` or run an SQL function on a sealed column: the comparator
  refuses comparisons when the query is built, and a compile hook refuses the rest
  (`SealedSqlError`). Decide in Python, or compare a blind index (`jobs.url_index`,
  `company_sources.careers_url_index`). `SealedAlways` columns (`jobs.hidden`, `excluded_reason`,
  the office override, a job's own content) can't even be tested for NULL.
- Check ownership through the plaintext `workspace_id` before loading a row (`workspaces.owned`);
  a row sealed under another account's key raises `KeyMismatch`, which the API answers with 404.
- Thread pools start work with `backend.threads.submit` (or `crypto.submit`), which carries the key
  scope into the worker; a plain `pool.submit` raises `crypto.Locked`.
- Write migrations of sealed columns with Core `update()`, never raw SQL text: after the one-time
  encryption (`backend/migrate_encryption.py`) strict mode refuses plaintext in a sealed column.
- Never log user data.

`JOBHUNT_DATA_DIR` moves the whole data folder (uploads included); tests and tools point it at a
throwaway folder, so they never touch `data/`. `python3 tools/bench_jobs_list.py` times the job list
on 5,000 made-up encrypted jobs in such a folder (target: under 1.5 s on a Raspberry Pi 4; run it on
the Pi to check there).

**README screenshots:** `python3 tools/readme_screenshots.py` (needs `pip install pillow`) builds a
throwaway database of made-up data with `tools/demo_data.py`, runs the app on it with fake AI
settings and no scheduler, and saves `docs/screenshots/*.png`. It never touches `data/` or `.env`.
Re-run it after changing the UI; `--only jobs,map` retakes just those.

**Accessibility checks:** `python3 tools/theme_contrast.py` lists, for every theme, the colour
pairs the app uses together and fails on any below WCAG AA (`--all` shows the passing ones too).
When you style something with a new combination of colour tokens, add it to `PAIRS` there. The
unit tests run the same check plus a static check of the pages (language, viewport, skip link,
headings, a name for every control). See [Accessibility](#accessibility) for the browser checks.

**Releasing the Windows programs:** bump `VERSION`, commit, then `git tag v$(cat VERSION) && git
push --tags`. `.github/workflows/release.yml` builds JobHuntPA-Setup.exe and JobHuntPA.exe on
Windows and attaches them to a GitHub Release. The setup program downloads the source of its own
tag, so the tag must exist on GitHub.

## How a search run works

`Run search` starts a background run (`backend/tasks.py`); the page polls its progress.
`backend/schedule.py` starts the same run at each workspace's scheduled times (public holidays
from the `holidays` package); runs never overlap, so a due search waits up to an hour for a
busy one. A workspace's slot runs under its owner's key (`users.background_key`, the data key
unwrapped with the server key) and is skipped, with a reason recorded for the schedule card, when
the owner hasn't turned on background access or the account is turned off.

1. **Listing** (`backend/adapters/`): SEEK search pages, then each enabled company source. A
   source URL is resolved in order: an APSJobs search URL (`apsjobs.py`); a known ATS board
   (Workable, Greenhouse, Lever, Ashby, SmartRecruiters, Workday) in the URL or linked from the
   page; an RSS/Atom job feed; schema.org `JobPosting` data; HTML job links with pagination;
   headless-browser rendering for JavaScript-only pages. Sites that answer with a bot check
   (Cloudflare, AWS WAF) are reported as errors, never bypassed.
2. **The shared pool** (`backend/pipeline.py`, `backend/pool.py`): each listed ad is upserted by
   URL into `postings`, shared by every workspace, and linked to the workspace through its `jobs`
   row, which holds everything personal plus any content of its own (an uploaded full ad). Listing
   pages are cached 30-60 minutes in `http_cache`. A detail page is fetched only when the pool has
   no full description of the ad, or one older than 30 days, and the ad passes the cheap filters:
   at most 60 per source per run (the rest follow on the next run). An ad another search fetched
   costs nothing. Closing is global: a 404/410 detail page, a complete listing (ATS feed, APSJobs,
   a careers page crawled to the end) that no longer shows the ad, or no listing showing it for 45
   days. After the sources, open pool ads that match the workspace's titles or include keywords
   and pass its filters are linked too (`match_pool`, at most 300 a run).
3. **Filtering** (`backend/filters.py`): dealbreakers, titles/keywords, salary floor, work mode,
   location pins. Excluded jobs stay in the DB with a reason ("Show excluded"). The office-days
   limit can be softened temporarily (`softened_job_ids`, `?office_slack=N` on the jobs and
   scoring endpoints) without changing stored exclusions.
4. **Duplicates** (`backend/duplicates.py`): jobs are compared in pairs (blocked by employer
   name and title words). Sure duplicates are merged: the copy with the best data stays listed and
   the others get `duplicate_of`, keeping their rows so later searches recognise their URLs.
   Doubtful pairs are stored in `job_duplicates` as suggestions; the user's "different jobs"
   answers are kept there too and never suggested again.
5. **Scoring** (`backend/scorer.py`): new jobs that passed the filters are scored (merged copies
   aren't). The scorer also reports the office named in the ad (`backend/offices.py`); an ad that
   names only a city keeps the city's coordinates and is flagged "office unknown". Failures are stored as errors (not as 0) and retried next time. A rejected API key
   stops the batch after one request.

## Scraping rules

- Every request goes through `backend/scraping/http.py`: robots.txt is checked for every URL
  (wildcards, longest match), `Crawl-delay` is honoured (at least 2 s per host), and the
  User-Agent identifies the app honestly. There is no browser impersonation, and blocks and
  CAPTCHAs are never worked around.
- SEEK's robots.txt allows search result pages but not job pages, so SEEK jobs carry the listing
  summary only. For a job you care about, click **Upload full ad** on its card: open the ad on
  SEEK, save the page (Ctrl+S / Cmd+S) and upload the file; the job is updated with the full ad,
  re-filtered and rescored. The uploaded ad is kept on your workspace's job only, never in the
  shared pool. SEEK may also refuse automated access outright (HTTP 403); then save
  SEEK search pages yourself and use *Search setup → Import saved pages*.
- Geocoding uses OpenStreetMap Nominatim under its usage policy (≤1 request/s, cached forever).
