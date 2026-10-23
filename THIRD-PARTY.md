# Other people's code in JobHuntPA

JobHuntPA itself is licensed under the GNU General Public License, version 3 only (`GPL-3.0-only`; see
[`LICENSE`](LICENSE)). It uses the programs and services below, each under its own licence. The licences were taken
from each package's own metadata (`License-Expression`, `License` and its licence files) in an install of
`requirements.txt` on Python 3.13, October 2026 (tzdata, a Windows-only package, from its wheel on PyPI); versions
move within the bounds in `requirements.txt`. The other parts below are not Python packages; their licences are
the projects' own statements, and for the AppImage's Python the libraries were checked in a built AppImage.

Every licence below may be combined with GPLv3. Apache-2.0 is compatible with version 3 of the GPL (not with
version 2), which is one reason the app is GPL-3.0 and not GPL-2.0. MPL-2.0 allows combination with the GPL (its
section 3.3; none of these files is marked "Incompatible With Secondary Licenses"). LGPL-2.1 allows conveying the
code under the GPL, and LGPL-3.0 is the GPLv3 with extra permissions, so it combines with GPLv3 code as it is. FreeType
(in Pillow's wheels) is used under its GPL-2.0-or-later option.

## Python packages (requirements.txt and what they pull in)

Installed by setup and bundled into the AppImage. "Direct" means listed in `requirements.txt`.

| Package | Version checked | Licence | Direct | Notes |
| --- | --- | --- | --- | --- |
| fastapi | 0.143.0 | MIT | yes | |
| uvicorn | 0.54.0 | BSD-3-Clause | yes | |
| SQLAlchemy | 2.1.4 | MIT | yes | |
| python-multipart | 0.0.32 | Apache-2.0 | yes | |
| pypdf | 6.20.1 | BSD-3-Clause | yes | |
| python-docx | 1.2.0 | MIT | yes | |
| fpdf2 | 2.8.9 | LGPL-3.0-only | yes | plain-text cover letter PDFs when LibreOffice isn't installed |
| odfpy | 1.4.1 | Apache-2.0, or GPL-2.0-or-later, or LGPL-2.1-or-later (the files' headers); the metadata lists all three | yes | used under any of them |
| httpx | 0.28.1 | BSD-3-Clause | yes | |
| anthropic | 1.13.0 | MIT | yes | vendors `httpx_aiohttp` (Apache-2.0, with its NOTICE) |
| beautifulsoup4 | 4.15.0 | MIT | yes | |
| lxml | 6.1.3 | BSD-3-Clause | yes | parts under the PSF licence 2 (from CPython); its binary wheels bundle libxml2, libxslt and libexslt (MIT), zlib (zlib) and libiconv (LGPL-2.1) |
| soupsieve | 3.0.3 | MIT | yes | |
| playwright | 1.63.0 | Apache-2.0 | yes | its driver bundles Node.js (MIT and the licences in its `LICENSE`) and code from Puppeteer (Apache-2.0) |
| holidays | 0.106 | MIT | yes | |
| tzdata | 2026.5 | Apache-2.0 | yes (Windows only) | IANA time-zone data (public domain) |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | yes | its wheels bundle OpenSSL 3 (Apache-2.0) |
| argon2-cffi | 25.1.0 | MIT | yes | |
| argon2-cffi-bindings | 26.1.0 | MIT | yes (Intel Macs pin it) | bundles the Argon2 reference code (CC0-1.0 or Apache-2.0) |
| annotated-doc | 0.0.5 | MIT | | |
| annotated-types | 0.8.0 | MIT | | |
| anyio | 4.15.1 | MIT | | |
| certifi | 2026.7.22 | MPL-2.0 | | Mozilla's CA certificates |
| cffi | 2.1.1 | MIT-0 | | |
| click | 8.5.0 | BSD-3-Clause | | |
| defusedxml | 0.7.1 | PSF-2.0 | | |
| docstring_parser | 0.18.0 | MIT | | |
| fonttools | 4.67.0 | MIT | | pulled in by fpdf2; the test fonts in its sources are under the SIL OFL 1.1 and Adobe's glyph list under BSD-3-Clause (its `LICENSE.external`) |
| greenlet | 3.5.6 | MIT AND PSF-2.0 | | |
| h11 | 0.16.0 | MIT | | |
| httpcore | 1.0.9 | BSD-3-Clause | | |
| httpcore2 | 2.13.1 | BSD-3-Clause | | |
| httpx2 | 2.13.1 | BSD-3-Clause | | |
| idna | 3.20 | BSD-3-Clause | | |
| jiter | 0.17.0 | MIT | | |
| opentelemetry-api | 1.45.1 | Apache-2.0 | | |
| Pillow | 13.0.0 | MIT-CMU | | pulled in by fpdf2; its binary wheels bundle libjpeg-turbo, libpng, libtiff, libwebp, libavif with dav1d and AOM, OpenJPEG, Little CMS, HarfBuzz, Brotli, XZ, zstd, zlib and libxcb under their own permissive licences, and FreeType (FTL or GPL-2.0-or-later); its `LICENSE` lists them all |
| pycparser | 3.11 | BSD-3-Clause | | |
| pydantic | 2.14.1 | MIT | | |
| pydantic_core | 2.50.1 | MIT | | |
| pyee | 13.0.1 | MIT | | |
| python-dateutil | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause (dual) | | |
| six | 1.17.0 | MIT | | |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | | |
| starlette | 1.8.0 | BSD-3-Clause | | |
| truststore | 0.10.5 | MIT | | |
| typing_extensions | 4.16.0 | PSF-2.0 | | |
| typing-inspection | 0.4.4 | MIT | | |

Each package's full licence text is in its `*.dist-info/licenses/` folder (or `*.dist-info/LICENSE`) in the
install's `site-packages`.

## Python itself, and the installers

| What | Licence | Where it comes in |
| --- | --- | --- |
| CPython 3.12 | PSF-2.0 | Windows setup installs it from python.org; the AppImage bundles a build from [python-build-standalone](https://github.com/astral-sh/python-build-standalone), which brings, among others, OpenSSL (Apache-2.0), SQLite (public domain), libffi, expat and mpdecimal (MIT/BSD-style), zlib, bzip2 and xz (permissive), libedit (BSD-3-Clause, not GNU readline) and Tcl/Tk (BSD-style) |
| [appimagetool](https://github.com/AppImage/appimagetool) and the AppImage runtime | MIT (the runtime includes libraries such as libfuse and squashfuse under their own licences; see the [type2-runtime](https://github.com/AppImage/type2-runtime) project) | `installer/appimage.sh` downloads appimagetool to build the AppImage; the runtime it writes is the first part of every AppImage |
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0-or-later with the bootloader exception (programs it builds may carry any licence) | `release.yml` builds the Windows `.exe` files with it; each `.exe` carries the CPython 3.12 runtime it was built with (PSF-2.0, and the libraries named in the CPython row) |

## Downloaded while the app runs, never shipped with it

| What | Licence | When |
| --- | --- | --- |
| Chromium (Playwright's build) | BSD-3-Clause and the licences of its components | only when you click **Download the browser** (Settings) for careers pages that need JavaScript; it is saved in your data folder |
| [Leaflet](https://leafletjs.com/) 1.9.4 | BSD-2-Clause | the Map tab loads it from unpkg.com |
| OpenStreetMap map tiles | map data © OpenStreetMap contributors, [ODbL 1.0](https://www.openstreetmap.org/copyright); the tiles' cartography CC BY-SA 2.0 | the Map tab draws them from `tile.openstreetmap.org`, under the [tile usage policy](https://operations.osmfoundation.org/policies/tiles/); the map shows the credit |
| OpenStreetMap Nominatim | the same data licence (ODbL 1.0) | the server looks addresses up there, under the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/) (at most one request a second, every answer cached) |
| Atkinson Hyperlegible (Google Fonts) | SIL Open Font License 1.1 | only while a Dyslexia theme is on, from fonts.googleapis.com and fonts.gstatic.com |

The app also opens links to Google Maps directions in your browser, and calls the AI provider you choose with your
own key; neither is code in JobHuntPA.

## Developer tools (not part of the app)

| What | Licence | Used by |
| --- | --- | --- |
| [axe-core](https://github.com/dequelabs/axe-core) 4.14 | MPL-2.0 | `tools/a11y_audit.py` (you install it with npm; it is never shipped) |
| Playwright's Chromium | as above | `tools/a11y_audit.py`, `tools/readme_screenshots.py` |
| [Pillow](https://python-pillow.org/) | MIT-CMU (its metadata) | `tools/readme_screenshots.py`; also an app package now, pulled in by fpdf2 (above) |
| appimagetool | MIT | `installer/appimage.sh` (above) |
