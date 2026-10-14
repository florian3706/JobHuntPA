# JobHuntPA: notes for coding agents

## Keep setup and launch working (standing rule)

Non-technical users install this with one-time setup scripts. **Every change must keep them
working and be reflected in them**:

- New or changed dependency → `requirements.txt` (with version bounds); `installer/installer.py`
  installs from it.
- New setting in `.env` → `.env.example`, and `backend/app_settings.py` (its fixed list of names and the
  value checks). Setup asks nothing: if users must set it, ask in the browser, in the first run
  (`frontend/first-run.html`, `welcome.html`) or a card on the Settings tab, saving through `app_settings`.
- New data folder, startup step, port or entry point → `installer/installer.py`,
  `installer/launch.py`, `setup.sh`, `start.sh`, `Setup.command`, `Start JobHuntPA.command`,
  `installer/windows_setup.py`, `installer/windows_launch.py`. A new table or data file is also either
  wiped or kept by the reset tool (`backend/reset.py`: `reset.sh`, `Reset JobHuntPA.command`,
  `installer/windows_reset.py`); `tests/test_reset.py` fails until it is listed.
- Anything setup creates outside the app folder (shortcuts, registry entries, caches) must also be
  removed by `installer/uninstaller.py` (`uninstall.sh`, `Uninstall JobHuntPA.command`,
  JobHuntPA-Uninstall.exe). Anything users would want to keep belongs in the data folder (with `.env` in
  it), which the uninstaller backs up. The data folder is chosen at the first run, not fixed next to the app
  (`backend/paths.py`: `JOBHUNT_DATA_DIR`, else the pointer `data-folder.json` in the per-user configuration
  folder, with an older install's `data/` next to the app coming first); never assume `data/` and never write
  anything but the pointer before the folder is chosen (`backend/data_folder.py`).
- Update the README install/usage sections when user-visible steps change.
- `.github/workflows/ci.yml` runs the real setup + launch on Linux, macOS and Windows; keep it
  passing. Publish by bumping `VERSION` and pushing tag `v<VERSION>` (`release.yml` builds and attaches the
  .exe files and the AppImage).
- New dependency, bundled file or outside script → a line in `THIRD-PARTY.md` with its licence (from the
  package's own metadata), which must be one GPL-3.0-only code may be combined with.

## Conventions

- Scraping only through `backend/scraping/http.py` (robots.txt, crawl delay, honest User-Agent).
- Every data query is scoped to a workspace (`X-Workspace` header → `current_workspace`).
- Jobs with `duplicate_of` set are merged copies of another job (`backend/duplicates.py`): anything
  that lists, scores or researches jobs must skip them (`Job.duplicate_of.is_(None)`).
- LLM settings are the provider-neutral `LLM_*` variables. Provider differences live only in
  `backend/llm_providers.py` (picked by `LLM_BASE_URL`'s host; `PRESETS` feeds the Settings tab and
  setup) and `backend/llm_anthropic.py` (Claude, through the official `anthropic` SDK). Everything
  else calls `llm_providers.chat` / `web_search`; never build provider requests elsewhere.
- `.env` is written only through `backend/app_settings.py`, which allows a fixed list of names and
  validates every value (the app can be reachable from other devices).
- Sealed (encrypted) columns, `backend/crypto.py` and `docs/design/accounts.md` §2: never filter, sort,
  group, `DISTINCT` or run an SQL function on one (decide in Python, or use a blind index); give none a
  `server_default`; check ownership through the plaintext `workspace_id` before loading a sealed row;
  start thread-pool work with `backend.threads.submit` (or `crypto.submit`), never `pool.submit`.
- Never log user data (emails, names, job text, documents, chats, tokens, keys); log the kind of failure
  and an id. `tests/test_log_leaks.py` gets a step when a feature handles personal text.
- Anything that adds, replaces or drops a key (recovery codes, background access, server key) changes
  everything in one transaction, then calls `db.scrub_wal()` after the commit.
- One account per install (`docs/design/open-source.md` §1.2): only `users.create_account` makes one, and it
  refuses once any exists. There are no roles: whoever is signed in may change every setting. An older install
  may still hold several accounts; each keeps its own workspaces. Nothing reachable over HTTP resets or wipes
  the install: that is the reset tool's (`backend/reset.py`), run on the computer with the app stopped.
- Every new endpoint states who may call it (open, signed in, recent sign-in) and gets a test for that access,
  including the refusal.
- Pages keep the Content-Security-Policy: no inline scripts, event-handler attributes, `<style>` or
  `style="..."` (use classes, `addEventListener`, `element.style`); a new outside script or stylesheet goes
  into `backend/security_headers.py`.
- Tests: `python3 -m unittest discover -s tests -t .`
