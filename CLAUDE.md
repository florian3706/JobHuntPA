# JobHuntPA: notes for coding agents

## Keep setup and launch working (standing rule)

Non-technical users install this with one-time setup scripts. **Every change must keep them
working and be reflected in them**:

- New or changed dependency → `requirements.txt` (with version bounds); `installer/installer.py`
  installs from it.
- New setting in `.env` → `.env.example`, and prompt for it in `installer/installer.py`
  `configure_llm` (or a sibling step) if users must set it.
- New data folder, startup step, port or entry point → `installer/installer.py`,
  `installer/launch.py`, `setup.sh`, `start.sh`, `Setup.command`, `Start JobHuntPA.command`,
  `installer/windows_setup.py`, `installer/windows_launch.py`.
- Anything setup creates outside the app folder (shortcuts, registry entries, caches) must also be
  removed by `installer/uninstaller.py` (`uninstall.sh`, `Uninstall JobHuntPA.command`,
  JobHuntPA-Uninstall.exe). Anything users would want to keep belongs in `data/` or `.env`,
  which the uninstaller backs up.
- Update the README install/usage sections when user-visible steps change.
- `.github/workflows/ci.yml` runs the real setup + launch on Linux, macOS and Windows; keep it
  passing. Publish Windows programs by bumping `VERSION` and pushing tag `v<VERSION>`
  (`release.yml` builds and attaches the .exe files).

## Conventions

- Scraping only through `backend/scraping/http.py` (robots.txt, crawl delay, honest User-Agent).
- Every data query is scoped to a workspace (`X-Workspace` header → `current_workspace`).
- LLM settings are provider-neutral `LLM_*` variables; never hard-code a provider.
- Tests: `python3 -m unittest discover -s tests -t .`
