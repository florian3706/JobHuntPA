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
search for, and researches each employer.

## What you need

- A computer running Windows 10/11, macOS or Linux, and an internet connection.
- An **API key for an AI model (LLM)** that offers an OpenAI-compatible API, for example the
  [Meta Model API](https://dev.meta.ai/) (Muse Spark) or [OpenAI](https://platform.openai.com/).
  Setup asks for three things: the provider's API address, the model name and your key. Using the
  model costs money on your provider account. The app still collects and filters jobs without a
  key, but scoring, cover letters, title suggestions and company research need one.

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

## Install on macOS

1. On this page click the green **Code** button, then **Download ZIP**, and unzip it (for example
   into your Documents folder).
2. In the unzipped folder, **right-click `Setup.command` and choose Open** (the first time macOS
   asks whether you're sure, because it was downloaded: click **Open**).
3. Follow the questions in the Terminal window. If Python is missing, setup offers to install it
   with Homebrew or opens the python.org download page; install it and run `Setup.command` again.
4. Start JobHuntPA by double-clicking **`Start JobHuntPA.command`**. Keep the Terminal window
   open while you use it; close it to stop the app.

If double-clicking doesn't work: open Terminal, type `cd ` (with a space), drag the folder into
the window, press Enter, then run `./setup.sh` once and `./start.sh` to start.

## Install on Linux

1. Download the ZIP (green **Code** button, then **Download ZIP**) and unzip it, or
   `git clone https://github.com/florian3706/JobHuntPA.git`.
2. Open a terminal in the folder and run `./setup.sh`. If Python 3.10+ or its `venv` module is
   missing, setup shows the command to install it and offers to run it.
3. Start JobHuntPA from your applications menu (setup adds it) or with `./start.sh`.

## Using it

The dashboard opens at http://127.0.0.1:8000. Start in **Search setup** (job titles, keywords,
salary, work modes and company careers pages), upload your CV under **Documents**, drop pins for
where you can commute on the **Map**, then click **Run search** on the Jobs tab. Separate searches
can live in their own **workspaces** (switcher at the top right).

- **Suggested job titles** (Documents tab): the AI reads your documents and suggests titles to
  search for; add them to the current search or start a new workspace with them.
- **Cover letters**: *Draft cover letter* on any job writes a first draft from your documents
  (upload earlier cover letters as "Cover letter" documents to match your style). Edit it, then
  copy it or download it as a Word file. Always check it before sending.
- **Reasoning level** (Search setup > LLM): how long the AI thinks, set separately for scoring,
  cover letters and company research. *Detect supported levels* shows what your model accepts.

## Updating

- **Windows:** download the newest JobHuntPA-Setup.exe and run it again with the same folder.
- **macOS/Linux:** download the new ZIP and copy its contents over your folder (or `git pull`),
  then run setup again.

Your settings, documents, jobs and API key are kept.

## Changing your AI provider or key

Run setup again with `--reconfigure` (macOS/Linux: `./setup.sh --reconfigure`; Windows: run
JobHuntPA-Setup.exe again and edit the `.env` file in the install folder), or edit the `.env` file
in the app folder directly: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`. Restart the app afterwards.

## Uninstalling

Close JobHuntPA first, then:

- **Windows:** *Settings > Apps > Installed apps > JobHuntPA > Uninstall*, or the Start-menu
  shortcut **Uninstall JobHuntPA**.
- **macOS:** double-click **`Uninstall JobHuntPA.command`** in the app folder (right-click > Open
  the first time).
- **Linux:** run `./uninstall.sh` in the app folder.

The uninstaller first offers to save a **backup** (your jobs, documents, workspaces, settings and
API key) as `JobHuntPA-backup-<date>.zip` in your Documents folder. It then removes the shortcuts
and the app folder. It asks before removing the shared browser component, and leaves Python
installed because other programs may use it. To restore a backup, install JobHuntPA again and
unzip the backup into the new app folder.

## Troubleshooting

- **The browser shows nothing:** check the JobHuntPA window for error messages. If it says
  another copy is running, use that one, or restart your computer.
- **"LLM not configured" in the app:** your API settings are missing; see *Changing your AI
  provider or key*.
- **Some company sites return no jobs:** they may block automated access or need the browser
  component; setup's step 3 installs it. On Linux you can fix a failed browser install with
  `.venv/bin/python -m playwright install --with-deps chromium`.

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

**Releasing the Windows programs:** bump `VERSION`, commit, then `git tag v$(cat VERSION) && git
push --tags`. `.github/workflows/release.yml` builds JobHuntPA-Setup.exe and JobHuntPA.exe on
Windows and attaches them to a GitHub Release. The setup program downloads the source of its own
tag, so the tag must exist on GitHub.

## How a search run works

`Run search` starts a background run (`backend/tasks.py`); the page polls its progress.

1. **Listing** (`backend/adapters/`): SEEK search pages, then each enabled company source. A
   source URL is resolved in order: a known ATS board (Workable, Greenhouse, Lever, Ashby,
   SmartRecruiters, Workday) in the URL or linked from the page; schema.org `JobPosting` data;
   HTML job links with pagination; headless-browser rendering for JavaScript-only pages.
2. **Caching** (`backend/pipeline.py`): jobs are upserted by URL. A job stored with a full
   description is never fetched again. Listing pages are cached 30-60 minutes in `http_cache`.
   Detail pages are fetched only for new jobs that pass the cheap filters, at most 60 per source
   per run (the rest follow on the next run).
3. **Filtering** (`backend/filters.py`): dealbreakers, titles/keywords, salary floor, work mode,
   location pins. Excluded jobs stay in the DB with a reason ("Show excluded").
4. **Scoring** (`backend/scorer.py`): new jobs that passed the filters are scored. Failures are
   stored as errors (not as 0) and retried next time. A rejected API key stops the batch after
   one request.

## Scraping rules

- Every request goes through `backend/scraping/http.py`: robots.txt is checked for every URL
  (wildcards, longest match), `Crawl-delay` is honoured (at least 2 s per host), and the
  User-Agent identifies the app honestly. There is no browser impersonation, and blocks and
  CAPTCHAs are never worked around.
- SEEK's robots.txt allows search result pages but not job pages, so SEEK jobs carry the listing
  summary only. SEEK may also refuse automated access outright (HTTP 403). In both cases, save
  SEEK pages from your own browser and use *Search setup → Import saved pages*.
- Geocoding uses OpenStreetMap Nominatim under its usage policy (≤1 request/s, cached forever).
