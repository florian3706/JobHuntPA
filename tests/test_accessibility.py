"""Accessibility regressions that need no browser (WCAG 2.2 AA).

- Every theme's colour pairs that the app uses together meet AA (tools/theme_contrast.py).
- The pages' HTML: a language, a viewport that allows zoom, a title, a skip link, a main landmark, a name for
  every form control, button and link, and no references to ids that don't exist.
- The markup app.js and job.js build: every control they write has a label or a name.

The full check (axe-core in every theme at 320 to 1280 px, keyboard, focus and reflow) needs a browser; the
README's Accessibility section says how it was run.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
sys.path.insert(0, str(ROOT / "tools"))
import theme_contrast  # noqa: E402

PAGES = ("index.html", "job.html", "login.html", "first-run.html", "recover.html", "welcome.html")
AUTH_PAGES = ("login.html", "first-run.html", "recover.html")
# Pages and scripts of the features taken out (docs/design/open-source.md §1.3): they must stay gone.
REMOVED_FILES = ("billing.html", "billing.js", "stripe-settings.js", "free-searches.js", "digest.js", "email-settings.js",
                 "account-email.js", "forgot.js", "reset-password.html", "reset-password.js", "verify-email.html",
                 "verify-email.js",
                 # the admin panel and sign-up for further accounts (Phase B)
                 "admin.html", "admin.js", "dialogs.js", "signup.html", "signup.js")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
FOCUSABLE = {"a", "button", "input", "select", "textarea", "summary"}
# Slots added with the Vanilla redesign that fall back to an older slot outside the Vanilla themes (themes.css)
FALLBACK_SLOTS = {"brand": "accent", "warn-fill": "warn", "check": "accent", "on-check": "on-accent",
                  "accent-edge": "accent"}
LABEL_SLOTS = [f"lbl-{kind}-{part}" for kind in ("neu", "pos", "cau", "neg", "inf", "spe") for part in ("bg", "fg")]


class ThemeContrastTest(unittest.TestCase):
    def test_every_used_pair_meets_aa(self):
        failing = [r for r in theme_contrast.check() if not r["ok"]]
        self.assertEqual(failing, [], "\n".join(
            f"{r['theme']}: --{r['fg']} on {r['bg']} is {r['ratio']:.2f}:1, needs {r['min']}:1 ({r['where']})" for r in failing))

    def test_every_theme_fills_every_slot(self):
        themes = theme_contrast.parse_themes((FRONTEND / "themes.css").read_text(encoding="utf-8"))
        offered = re.findall(r"\{ id: '([\w-]+)'", (FRONTEND / "common.js").read_text(encoding="utf-8"))
        self.assertIn("dark", themes)
        for theme in offered:
            if theme != "system":
                self.assertIn(theme, themes, f"the theme picker offers {theme}, but themes.css has no block for it")
        for theme in themes:
            self.assertIn(theme, offered, f"themes.css defines {theme}, but THEMES in common.js doesn't offer it")
        slots = set(themes["dark"])
        css = (FRONTEND / "themes.css").read_text(encoding="utf-8")
        for theme in themes:
            block = re.search(r'\[data-theme="%s"\][^{]*\{([^}]*)\}' % re.escape(theme), css).group(1)
            own = set(re.findall(r"--([\w-]+)\s*:", block))
            # --map-ink and the type settings are shared from :root, and the slots added with the Vanilla redesign
            # fall back to an older slot there; every other colour slot is set per theme.
            missing = {t for t in slots - own if t not in ("map-ink", "font-ui", "leading", "tracking", "word-spacing")
                       and t not in FALLBACK_SLOTS}
            self.assertEqual(missing, set(), f"{theme} doesn't set {sorted(missing)}")

    def test_the_vanilla_themes_set_every_slot_and_fill_their_labels(self):
        css = (FRONTEND / "themes.css").read_text(encoding="utf-8")
        themes = theme_contrast.parse_themes(css)
        vanilla = ("vanilla-light", "vanilla-dark", "vanilla-light-aa", "vanilla-dark-aa")
        for theme in vanilla:
            block = re.search(r'\[data-theme="%s"\] \{([^}]*)\}' % theme, css).group(1)
            own = set(re.findall(r"--([\w-]+)\s*:", block))
            self.assertEqual(set(FALLBACK_SLOTS) - own, set(), f"{theme} sets every new slot itself")
            self.assertEqual(set(LABEL_SLOTS) - own, set(), f"{theme} fills its status labels")
            for slot in LABEL_SLOTS + list(FALLBACK_SLOTS):
                self.assertTrue(theme_contrast.is_set(themes[theme], slot), f"{theme}: --{slot}")
        # The exact palettes are the design's vanilla.css values; the AA versions differ only where AA needed it.
        exact_dark, aa_dark = themes["vanilla-dark"], themes["vanilla-dark-aa"]
        self.assertEqual((exact_dark["accent"], exact_dark["accent-edge"], exact_dark["ok"], exact_dark["accent-text"]),
                         ("#008013", "#008013", "#62a36c", "#6699cc"))
        self.assertEqual((aa_dark["accent"], aa_dark["accent-edge"], aa_dark["card"]), ("#008013", "#939393", "#262626"))
        exact_light = themes["vanilla-light"]
        self.assertEqual((exact_light["warn"], exact_light["focus"], exact_light["accent-text"]),
                         ("#a86300", "#2e96ff", "#0066cc"))
        for theme in ("vanilla-light", "vanilla-light-aa"):
            self.assertEqual(themes[theme]["warn-fill"], "#cc7900")
        # The other themes leave the label fills unset (initial), so a preview inside a Vanilla page stays outlined,
        # and their new slots resolve to the slot they fall back to.
        shared = re.search(r"\n\[data-theme\] \{([^}]*)\}", css).group(1)
        for slot in LABEL_SLOTS:
            self.assertRegex(shared, r"--%s:\s*initial;" % slot)
        for slot, fallback in FALLBACK_SLOTS.items():
            self.assertRegex(shared, r"--%s:\s*var\(--%s\);" % (slot, fallback))
        for theme, tokens in themes.items():
            if theme not in vanilla:
                self.assertFalse(any(theme_contrast.is_set(tokens, slot) for slot in LABEL_SLOTS), theme)
                self.assertEqual(theme_contrast.token(tokens, "accent-edge"), theme_contrast.token(tokens, "accent"), theme)

    def test_the_aa_badge_is_earned(self):
        """aa: true (the picker's "WCAG 2.0 AA" badge) is set on exactly the themes whose pairs all meet AA; every
        theme without it is a project's exact palette with its AA version right before it in its family."""
        common = (FRONTEND / "common.js").read_text(encoding="utf-8")
        listing = common[common.index("const THEMES = ["):common.index("];", common.index("const THEMES = ["))]
        entries = [(m.group(1), m.group(0)) for m in re.finditer(r"\{ id: '([\w-]+)'[^}]*\}", listing)]
        info = {tid: {**dict(re.findall(r"(\w+): '([^']*)'", src)), "aa": "aa: true" in src} for tid, src in entries}
        order = [tid for tid, _ in entries]
        flagged = {t for t, i in info.items() if i["aa"]}
        self.assertEqual(flagged - {"system"}, theme_contrast.aa_themes(common))
        self.assertEqual(theme_contrast.aa_themes(), theme_contrast.passing_themes(),
                         "the badge is on exactly the themes that pass tools/theme_contrast.py")
        exact = [t for t in order if not info[t]["aa"]]
        self.assertEqual(len(exact), 16)
        for t in exact:
            sibling = f"{t}-aa"
            self.assertIn(sibling, flagged, f"{t} has an AA version")
            for key in ("label", "family", "mode"):
                self.assertEqual(info[t][key], info[sibling][key], f"{t}: {key}")
            self.assertEqual(order.index(sibling) + 1, order.index(t), f"{t} sits right after its AA version")
        for t in ("dark", "light", "dyslexia", "dyslexia-dark", "protanomaly", "monochromacy-dark"):
            self.assertIn(t, flagged, "the app's own palettes are built AA")
        # The default and System's two targets carry the badge
        self.assertIn("const DEFAULT_THEME = 'vanilla-dark-aa';", common)
        self.assertIn("? 'vanilla-dark-aa' : 'vanilla-light-aa') : pref;", common)
        self.assertTrue({"vanilla-dark-aa", "vanilla-light-aa", "system"} <= flagged)
        # The picker: the badge is a positive status label with the words in it, on its own line under the name and
        # the mode; the exact ones get the muted line in its place
        self.assertIn("const AA_BADGE = '<span class=\"p-status-label p-status-label--positive theme-aa\">"
                      "WCAG 2.0 AA</span>';", common)
        self.assertIn("const EXACT_NOTE = 'The project\\'s exact colours; some text is under 4.5:1';", common)
        self.assertIn("${t.aa ? `<span class=\"theme-badge\">${AA_BADGE}</span>` : `<span class=\"theme-mode theme-exact\">${EXACT_NOTE}</span>`}",
                      common)

    def test_saved_family_themes_move_to_their_aa_version_once(self):
        common = (FRONTEND / "common.js").read_text(encoding="utf-8")
        offered = re.findall(r"\{ id: '([\w-]+)'", common)
        exact = {t for t in offered if f"{t}-aa" in offered}
        self.assertIn("migrateStoredTheme();", common[common.index("function storedTheme()"):])
        self.assertIn("const EXACT_THEMES = THEMES.filter((t) => !t.aa).map((t) => t.id);", common)
        self.assertIn("const THEME_MIGRATED_KEY = 'jobhunt.themeMigrated';", common)
        scripts = set()
        for page in PAGES:
            src = (FRONTEND / page).read_text(encoding="utf-8")
            head = src[src.index("<script>"):src.index("</script>")]
            scripts.add(head)
            pattern = re.search(r"if \((/\^.*\$/)\.test\(t\)\)", head).group(1).strip("/")
            matched = {t for t in offered if re.fullmatch(pattern, t)}
            self.assertEqual(matched, exact, f"{page}: the first-paint migration covers exactly the exact palettes")
            self.assertIn('localStorage.getItem("jobhunt.themeMigrated") !== "aa"', head)
            self.assertIn('localStorage.setItem("jobhunt.theme", t);', head)
            self.assertIn('localStorage.setItem("jobhunt.themeMigrated", "aa");', head)
        self.assertEqual(len(scripts), 1, "every page has the same first-paint script")

    def test_ubuntu_sans_in_every_theme_but_the_dyslexia_ones(self):
        css = (FRONTEND / "themes.css").read_text(encoding="utf-8")
        for theme, tokens in theme_contrast.parse_themes(css).items():
            block = re.search(r'\[data-theme="%s"\][^{]*\{([^}]*)\}' % re.escape(theme), css).group(1)
            font = re.search(r"--font-ui:\s*([^;]+);", block)
            if theme.startswith("dyslexia"):
                self.assertTrue(font and font.group(1).startswith('"Atkinson Hyperlegible"'), theme)
            else:  # the shared type, or (the :root block) the same stack
                self.assertTrue(font is None or font.group(1).startswith('"Ubuntu Sans", '), theme)
        shared = re.search(r"\n\[data-theme\] \{([^}]*)\}", css).group(1)
        root = re.search(r":root, \[data-theme=\"dark\"\] \{([^}]*)\}", css).group(1)
        for where in (shared, root):
            self.assertIn('--font-ui: "Ubuntu Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;', where)

    def test_vanilla_dark_is_the_default_and_system_follows_the_vanilla_pair(self):
        common = (FRONTEND / "common.js").read_text(encoding="utf-8")
        self.assertIn("const DEFAULT_THEME = 'vanilla-dark-aa';", common)
        self.assertIn("? 'vanilla-dark-aa' : 'vanilla-light-aa') : pref;", common)
        ids = re.findall(r"\{ id: '([\w-]+)', label: '([^']+)', family: '([^']+)'", common)
        self.assertEqual(ids[:5], [("system", "System", "Vanilla"), ("vanilla-dark-aa", "Vanilla Dark", "Vanilla"),
                                   ("vanilla-dark", "Vanilla Dark", "Vanilla"),
                                   ("vanilla-light-aa", "Vanilla Light", "Vanilla"),
                                   ("vanilla-light", "Vanilla Light", "Vanilla")], "the Vanilla family is listed first")
        self.assertIn(("dark", "Dark", "JobHuntPA"), ids)
        self.assertIn(("light", "Light", "JobHuntPA"), ids)
        for page in PAGES:
            src = (FRONTEND / page).read_text(encoding="utf-8")
            self.assertIn('var t = "vanilla-dark-aa";', src, page)
            self.assertIn('t = localStorage.getItem("jobhunt.theme") || t;', src, page)
            self.assertIn('matches ? "vanilla-dark-aa" : "vanilla-light-aa";', src, page)
            self.assertNotIn('|| "dark"', src, page)


class FontsTest(unittest.TestCase):
    """Ubuntu Sans and Ubuntu Sans Mono ship in frontend/fonts/ with their licence and come from this server; the only
    font loaded from another site is the Dyslexia themes' Atkinson Hyperlegible, and only while one is on."""
    FILES = ("UbuntuSans[wdth,wght].woff2", "UbuntuSansMono[wght].woff2")

    def test_the_font_files_and_their_licence_ship_with_the_app(self):
        for name in self.FILES:
            data = (FRONTEND / "fonts" / name).read_bytes()
            self.assertEqual(data[:4], b"wOF2", name)
        licence = (FRONTEND / "fonts" / "UFL-1.0.txt").read_text(encoding="utf-8")
        self.assertIn("UBUNTU FONT LICENCE Version 1.0", licence)
        third_party = (ROOT / "THIRD-PARTY.md").read_text(encoding="utf-8")
        for name in (*self.FILES, "UFL-1.0.txt"):
            self.assertIn(f"`{name}`" if name != "UFL-1.0.txt" else "frontend/fonts/UFL-1.0.txt", third_party)
        shipped = {p.name for p in (FRONTEND / "fonts").iterdir()}
        self.assertEqual(shipped, {*self.FILES, "UFL-1.0.txt"}, "every font file in frontend/fonts/ has a row in THIRD-PARTY.md")

    def test_font_faces_point_at_the_shipped_files(self):
        css = (FRONTEND / "themes.css").read_text(encoding="utf-8")
        faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
        families = {}
        for face in faces:
            family = re.search(r'font-family:\s*"([^"]+)"', face).group(1)
            src = re.findall(r'url\("([^"]+)"\)', face)
            self.assertEqual(len(src), 1, family)
            self.assertTrue((FRONTEND / "themes.css").parent.joinpath(src[0]).is_file(), src[0])
            self.assertIn("font-display: swap;", face, family)
            families[family] = face
        self.assertEqual(set(families), {"Ubuntu Sans", "Ubuntu Sans Mono"})
        self.assertIn("font-weight: 100 800;", families["Ubuntu Sans"])
        self.assertIn("font-stretch: 75% 100%;", families["Ubuntu Sans"])
        self.assertIn("font-weight: 100 700;", families["Ubuntu Sans Mono"])
        for page in PAGES:  # every page loads themes.css, so every page has the faces
            self.assertIn('<link rel="stylesheet" href="./themes.css" />', (FRONTEND / page).read_text(encoding="utf-8"), page)

    def test_no_page_or_stylesheet_loads_fonts_from_another_site_but_the_dyslexia_one(self):
        hosts = re.compile(r"fonts\.(googleapis|gstatic)\.com|use\.typekit|fonts\.bunny|fontshare|assets\.ubuntu\.com", re.I)
        for path in sorted(FRONTEND.glob("*.html")) + sorted(FRONTEND.glob("*.css")):
            src = path.read_text(encoding="utf-8")
            self.assertIsNone(hosts.search(src), f"{path.name} loads a font from another site")
            self.assertNotIn("@import", src, path.name)
        for path in sorted(FRONTEND.glob("*.js")):
            src = path.read_text(encoding="utf-8")
            found = [m.group(0) for m in hosts.finditer(src)]
            if path.name == "common.js":
                # applyTheme(): the Dyslexia themes' font, the one stylesheet from Google Fonts
                self.assertEqual(found, ["fonts.googleapis.com"], path.name)
                self.assertIn("link.href = `https://fonts.googleapis.com/css2?family=${font}&display=swap`;", src)
                fonted = re.findall(r"\{ id: '([\w-]+)'[^}]*font: DYSLEXIA_FONT", src)
                self.assertEqual(fonted, ["dyslexia", "dyslexia-dark"])
            else:
                self.assertEqual(found, [], path.name)

    def test_the_app_serves_the_fonts_to_anyone(self):
        from tests import helpers  # sets the test environment first
        from fastapi.testclient import TestClient
        from backend.app import app
        helpers.started()
        with TestClient(app) as c:  # signed out: the sign-in page needs them too
            for name, kind in ((self.FILES[0], "font/woff2"), (self.FILES[1], "font/woff2"),
                               ("UFL-1.0.txt", "text/plain")):
                for path in (f"/fonts/{name}", "/fonts/" + name.replace("[", "%5B").replace("]", "%5D")):
                    r = c.get(path)
                    self.assertEqual(r.status_code, 200, path)
                    self.assertEqual(r.headers["content-type"].split(";")[0], kind, path)
                    self.assertEqual(r.content, (FRONTEND / "fonts" / name).read_bytes(), path)
            policy = c.get("/fonts/" + self.FILES[0]).headers["content-security-policy"]
            self.assertIn("font-src 'self' https://fonts.gstatic.com", policy)

    def test_ratio_maths(self):
        black, white = (0, 0, 0, 1), (1, 1, 1, 1)
        self.assertAlmostEqual(theme_contrast.ratio(black, white), 21.0, places=2)
        self.assertAlmostEqual(theme_contrast.ratio(theme_contrast.parse_color("#767676"), white), 4.54, places=2)
        # A 12% tint over white, composited
        tinted = theme_contrast.over((1, 0, 0, 0.12), white)
        self.assertAlmostEqual(tinted[1], 0.88, places=2)


class _Page(HTMLParser):
    """Collects elements with their attributes, parents and text, enough for the checks below."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes = []  # dicts: tag, attrs, parent index, text
        self.stack = []
        self.html_lang = None
        self.title = ""

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "html":
            self.html_lang = attrs.get("lang")
        node = {"tag": tag, "attrs": attrs, "parent": self.stack[-1] if self.stack else None, "text": "", "i": len(self.nodes)}
        self.nodes.append(node)
        if tag not in VOID:
            self.stack.append(node["i"])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack:
            self.stack.pop()

    def handle_endtag(self, tag):
        for depth in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[depth]]["tag"] == tag:
                del self.stack[depth:]
                break

    def handle_data(self, data):
        for i in self.stack:
            node = self.nodes[i]
            if node["tag"] == "title":
                self.title += data
            if not self._hidden(i):
                node["text"] += data

    def _hidden(self, i):
        """Inside aria-hidden="true" (not read out)."""
        while i is not None:
            if self.nodes[i]["attrs"].get("aria-hidden") == "true":
                return True
            i = self.nodes[i]["parent"]
        return False

    def ancestors(self, node):
        i = node["parent"]
        while i is not None:
            yield self.nodes[i]
            i = self.nodes[i]["parent"]


def read(name):
    return (FRONTEND / name).read_text(encoding="utf-8")


def parse(name):
    page = _Page()
    page.feed((FRONTEND / name).read_text(encoding="utf-8"))
    return page


def accessible_name(page, node, ids):
    a = node["attrs"]
    if a.get("aria-label", "").strip() or a.get("title", "").strip():
        return True
    if a.get("aria-labelledby"):
        return all(x in ids for x in a["aria-labelledby"].split())
    return bool(node["text"].strip())


class StaticHtmlTest(unittest.TestCase):
    def test_language_title_viewport(self):
        for name in PAGES:
            page = parse(name)
            with self.subTest(page=name):
                self.assertTrue(page.html_lang, "no lang on <html>")
                self.assertTrue(page.title.strip(), "no <title>")
                metas = [n["attrs"] for n in page.nodes if n["tag"] == "meta" and n["attrs"].get("name") == "viewport"]
                self.assertEqual(len(metas), 1, "needs one viewport meta")
                content = metas[0].get("content", "").replace(" ", "").lower()
                self.assertIn("width=device-width", content)
                self.assertNotIn("user-scalable=no", content, "pinch zoom must stay on (WCAG 1.4.4)")
                m = re.search(r"maximum-scale=([\d.]+)", content)
                self.assertTrue(m is None or float(m.group(1)) >= 2, "maximum-scale below 2 blocks zoom")

    def test_skip_link_main_and_headings(self):
        for name in PAGES:
            page = parse(name)
            ids = {n["attrs"]["id"] for n in page.nodes if "id" in n["attrs"]}
            with self.subTest(page=name):
                body = next(i for i, n in enumerate(page.nodes) if n["tag"] == "body")
                first = next(n for n in page.nodes[body + 1:] if n["tag"] in FOCUSABLE or "tabindex" in n["attrs"])
                self.assertEqual(first["tag"], "a", "the first focusable element must be the skip link")
                self.assertIn("skip", first["text"].lower())
                self.assertTrue(first["attrs"].get("href", "").startswith("#"))
                self.assertIn(first["attrs"]["href"][1:], ids, "the skip link's target doesn't exist")
                self.assertEqual(sum(n["tag"] == "main" for n in page.nodes), 1, "one <main>")
                self.assertEqual(sum(n["tag"] == "h1" for n in page.nodes), 1, "one <h1>")
                levels = [int(n["tag"][1]) for n in page.nodes if re.fullmatch(r"h[1-6]", n["tag"])]
                for before, after in zip(levels, levels[1:]):
                    self.assertLessEqual(after, before + 1, f"heading level jumps from h{before} to h{after}")

    def test_controls_buttons_and_links_have_names(self):
        for name in PAGES:
            page = parse(name)
            ids = {n["attrs"]["id"] for n in page.nodes if "id" in n["attrs"]}
            label_for = {n["attrs"].get("for") for n in page.nodes if n["tag"] == "label"}
            for node in page.nodes:
                a, tag = node["attrs"], node["tag"]
                where = f"{name}: <{tag} {' '.join(f'{k}={v!r}' for k, v in a.items())[:120]}>"
                if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "button", "reset", "image"):
                    labelled = (a.get("id") in label_for or any(p["tag"] == "label" for p in page.ancestors(node))
                                or a.get("aria-label", "").strip() or a.get("title", "").strip()
                                or (a.get("aria-labelledby") and all(x in ids for x in a["aria-labelledby"].split())))
                    self.assertTrue(labelled, f"no label: {where}")
                if tag == "button" or (tag == "a" and "href" in a):
                    self.assertTrue(accessible_name(page, node, ids), f"no accessible name: {where}")
                if tag == "img":
                    self.assertIn("alt", a, f"no alt: {where}")

    def test_ids_are_unique_and_references_exist(self):
        for name in PAGES:
            page = parse(name)
            ids = [n["attrs"]["id"] for n in page.nodes if "id" in n["attrs"]]
            with self.subTest(page=name):
                self.assertEqual(len(ids), len(set(ids)), f"duplicate ids: {sorted({i for i in ids if ids.count(i) > 1})}")
                known = set(ids)
                for node in page.nodes:
                    for attr in ("aria-labelledby", "aria-describedby", "aria-controls", "for"):
                        for ref in node["attrs"].get(attr, "").split():
                            self.assertIn(ref, known, f"{name}: {attr}={ref!r} on <{node['tag']}> points to no element")


class GeneratedMarkupTest(unittest.TestCase):
    """The controls app.js and job.js write into the page (job cards, dialogs, lists) need names too.
    A light static check: each <input>, <select>, <textarea> and <button> in their HTML templates has an
    aria-label, a title, a wrapping <label>, an id that a for="..." in the same file points to, or (buttons)
    text of its own."""

    TAG = re.compile(r"<(input|select|textarea|button)\b([^>]*)>", re.S)

    def check_file(self, name):
        src = (FRONTEND / name).read_text(encoding="utf-8")
        fors = set(re.findall(r'for="([^"]+)"', src))
        problems = []
        for m in self.TAG.finditer(src):
            tag, attrs = m.group(1), m.group(2)
            if re.search(r'type="(hidden|submit|reset)"', attrs) and tag == "input":
                continue
            if re.search(r"aria-label(ledby)?=|title=", attrs):
                continue
            idm = re.search(r'\bid="([^"]+)"', attrs)
            if idm and idm.group(1) in fors:
                continue
            if tag == "button":
                end = src.find("</button>", m.end())
                inner = re.sub(r"<[^>]*aria-hidden=\"true\"[^>]*>[^<]*</span>", "", src[m.end():end])
                if re.sub(r"<[^>]+>", "", inner).strip():
                    continue
            before = src[max(0, m.start() - 400):m.start()]
            if before.rfind("<label") > before.rfind("</label>"):
                continue
            line = src.count("\n", 0, m.start()) + 1
            problems.append(f"{name}:{line}: <{tag}{attrs[:80]}>")
        return problems

    def test_templates_label_their_controls(self):
        problems = []
        for path in sorted(FRONTEND.glob("*.js")):  # every script, so a new one (letter.js, ...) is checked too
            problems += self.check_file(path.name)
        self.assertEqual(problems, [], "controls without a label or name:\n" + "\n".join(problems))


class AuthPagesTest(unittest.TestCase):
    """The sign-in pages (WCAG 2.2 3.3.8 accessible authentication, 3.3.7 redundant entry, 3.2.6 consistent help,
    1.3.5 input purpose)."""

    def inputs(self, page):
        return {n["attrs"].get("id"): n["attrs"] for n in page.nodes if n["tag"] == "input"}

    def test_fields_say_what_they_are_for_password_managers(self):
        expected = {
            # A name, not an address: a text field that password managers take for the username.
            "login.html": {"name": ("text", "username"), "password": ("password", "current-password")},
            "first-run.html": {"name": ("text", "username"), "password": ("password", "new-password")},
            "recover.html": {"name": ("text", "username"), "password": ("password", "new-password")},
        }
        for name, fields in expected.items():
            inputs = self.inputs(parse(name))
            for field, (kind, autocomplete) in fields.items():
                with self.subTest(page=name, field=field):
                    self.assertEqual(inputs[field].get("type"), kind)
                    self.assertEqual(inputs[field].get("autocomplete"), autocomplete)
            passwords = [i for i in inputs.values() if i.get("type") == "password"]
            # (first-run.html has a second one for asking again before showing the recovery codes: one step at a time)
            self.assertEqual(len(passwords), 2 if name == "first-run.html" else 1,
                             f"{name}: one password field, no 'confirm password' (3.3.7)")

    def test_paste_and_password_managers_are_never_blocked(self):
        for name in AUTH_PAGES + ("auth.js", "common.js", "account.js"):
            src = (FRONTEND / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotRegex(src, r"on(paste|copy|cut|drop)\s*=", "no handlers that block pasting")
                self.assertNotRegex(src, r"addEventListener\(\s*['\"](paste|copy|cut|drop)['\"]")
                self.assertNotIn("captcha", src.lower())
        for name in AUTH_PAGES:
            for attrs in self.inputs(parse(name)).values():
                if attrs.get("type") == "password" or attrs.get("id") == "name":
                    self.assertNotEqual(attrs.get("autocomplete"), "off", f"{name}: autocomplete=off on a sign-in field")
                    self.assertNotIn("data-lpignore", attrs)
                    self.assertNotIn("data-1p-ignore", attrs)

    def test_show_password_is_a_toggle_button(self):
        for name in AUTH_PAGES:
            page = parse(name)
            toggles = [n for n in page.nodes if n["tag"] == "button" and "pw-toggle" in n["attrs"].get("class", "")]
            with self.subTest(page=name):
                self.assertEqual(len(toggles), 2 if name == "first-run.html" else 1)
                for toggle in toggles:
                    self.assertEqual(toggle["attrs"].get("aria-pressed"), "false")
                    self.assertIn(toggle["attrs"].get("aria-controls"), ("password", "show-password"))
                    self.assertEqual(toggle["attrs"].get("type"), "button")
                self.assertEqual(toggles[0]["attrs"].get("aria-controls"), "password")

    def test_error_summary_and_help_are_in_the_same_place(self):
        for name in AUTH_PAGES:
            page = parse(name)
            with self.subTest(page=name):
                summary = next(n for n in page.nodes if n["attrs"].get("id") == "error-summary")
                self.assertEqual(summary["attrs"].get("tabindex"), "-1", "the summary takes focus")
                sections = [n for n in page.nodes if n["tag"] == "section" and n["parent"] is not None
                            and page.nodes[n["parent"]]["tag"] == "main"]
                self.assertEqual(sections[-1]["attrs"].get("aria-labelledby"), "help-title",
                                 "'Trouble signing in?' is the last part of the page on every sign-in page (3.2.6)")
                self.assertIn("Trouble signing in?", next(n for n in page.nodes if n["attrs"].get("id") == "help-title")["text"])

    def test_fields_use_16px_text(self):
        css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        rule = re.search(r"\.auth-field input\[type=\"email\"\][^{]*\{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        self.assertIn("font-size: 16px", rule.group(1))
        self.assertIn("min-height: 44px", rule.group(1))

    def test_the_dashboard_pages_have_the_account_menu(self):
        for name in ("index.html", "job.html"):
            page = parse(name)
            btn = next(n for n in page.nodes if n["attrs"].get("id") == "account-btn")
            with self.subTest(page=name):
                self.assertEqual(btn["attrs"].get("aria-expanded"), "false")
                self.assertEqual(btn["attrs"].get("aria-controls"), "account-panel")
                self.assertTrue(any(n["attrs"].get("id") == "account-signout" for n in page.nodes))
                self.assertTrue(any(n["attrs"].get("id") == "account-name" for n in page.nodes))
                links = [n["attrs"].get("href") for n in page.nodes if n["tag"] == "a"]
                self.assertIn("./#settings", links, "the menu leads to Settings")
                self.assertNotIn("admin.html", links)


class SignInAndRecoveryTest(unittest.TestCase):
    """login.html (name and password, nothing after), the recovery-code path (recover.html) and "Trouble signing
    in?"."""

    @classmethod
    def setUpClass(cls):
        cls.login = parse("login.html")
        cls.recover = parse("recover.html")
        cls.js = (FRONTEND / "auth.js").read_text(encoding="utf-8")

    def by_id(self, page, ident):
        return next(n for n in page.nodes if n["attrs"].get("id") == ident)

    def test_the_sign_in_page_has_one_step(self):
        ids = {n["attrs"].get("id") for n in self.login.nodes}
        self.assertIn("login-form", ids)
        for gone in ("mfa-form", "code", "mfa-switch", "mfa-restart", "forgot-item", "forgot-btn"):
            self.assertNotIn(gone, ids)
        for gone in ("mfa", "/api/auth/forgot", "forgot.js", "second_factor_required", "second_recovery_code"):
            self.assertNotIn(gone, self.js + read("login.html") + read("recover.html"))

    def test_the_pages_of_removed_features_are_gone_and_nothing_links_to_them(self):
        for name in REMOVED_FILES:
            self.assertFalse((FRONTEND / name).exists(), name)
        every = "".join(p.read_text(encoding="utf-8") for p in FRONTEND.glob("*.html")) + \
            "".join(p.read_text(encoding="utf-8") for p in FRONTEND.glob("*.js"))
        for name in REMOVED_FILES:
            self.assertNotIn(name, every, f"something still loads or links to {name}")

    def test_trouble_signing_in_links_to_the_recovery_page_everywhere(self):
        for name in AUTH_PAGES:
            page = parse(name)
            links = [n["attrs"].get("href") for n in page.nodes if n["tag"] == "a"]
            with self.subTest(page=name):
                if name != "recover.html":
                    self.assertIn("recover.html", links)
                help_ = next(n for n in page.nodes if n["attrs"].get("id") == "help-title")
                self.assertEqual(help_["tag"], "h2")

    def test_the_recovery_form(self):
        form = self.by_id(self.recover, "recover-form")
        self.assertIn("novalidate", form["attrs"])
        self.assertNotIn("code-field", {n["attrs"].get("id") for n in self.recover.nodes}, "no second proof")
        self.assertEqual(self.by_id(self.recover, "recovery-code")["attrs"].get("autocomplete"), "off")
        self.assertIn("showRecovered", self.js)
        done = self.by_id(self.recover, "recover-done")
        self.assertIn("hidden", done["attrs"])

    def test_buttons_that_look_like_links_are_big_enough(self):
        css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        # .p-button--link is a .p-button (44 px: 2.75rem) drawn as a link
        self.assertIn("min-height: 2.75rem", re.search(r"\.p-button, \.btn \{([^}]*)\}", css).group(1))
        self.assertRegex(css, r"\.p-button--link \{[^}]*text-decoration: underline")


class ConfirmPasswordDialogTest(unittest.TestCase):
    """The "Confirm your password" dialog in common.js (WCAG 2.4.3 focus order, 3.3.1 errors, 3.3.7, 3.3.8)."""

    @classmethod
    def setUpClass(cls):
        src = (FRONTEND / "common.js").read_text(encoding="utf-8")
        cls.src = src
        cls.body = src[src.index("async function openConfirmDialog"):src.index("/* A notice at the top of the page")]

    def test_it_is_a_labelled_native_dialog(self):
        self.assertIn("createElement('dialog')", self.body)
        self.assertIn("aria-labelledby', 'confirm-pw-title'", self.body)
        self.assertIn("aria-describedby', 'confirm-pw-intro'", self.body)
        self.assertIn('<h2 id="confirm-pw-title">Confirm your password</h2>', self.body)
        self.assertIn("showModal()", self.body)

    def test_its_ids_do_not_clash_with_the_dashboards_own(self):
        # a duplicate id would give this dialog the wrong accessible name
        index = (FRONTEND / "index.html").read_text(encoding="utf-8")
        ids = set(re.findall(r'\bid="(confirm-[\w-]+)"', self.body))
        self.assertTrue({"confirm-pw-title", "confirm-pw-intro", "confirm-pw-cancel", "confirm-pw-status"} <= ids)
        for name in ids:
            self.assertNotIn(f'id="{name}"', index, f"{name} is also on index.html")

    def test_focus_goes_in_and_comes_back_and_escape_cancels(self):
        self.assertIn("const opener = document.activeElement", self.body)
        self.assertIn("opener.focus()", self.body)
        self.assertIn("input.focus()", self.body)
        self.assertIn("resolve(confirmed)", self.body)
        self.assertNotIn("preventDefault()", self.body.split("addEventListener('close'")[1][:200])
        self.assertNotRegex(self.body, r"addEventListener\('cancel'.*preventDefault", "Escape must not be swallowed")

    def test_fields_for_password_managers_and_the_show_button(self):
        self.assertIn('autocomplete="current-password"', self.body)
        self.assertRegex(self.body, r'name="username" autocomplete="username"')
        self.assertIn("aria-hidden=\"true\" aria-label=\"Name\" value=\"${esc(name)}\"", self.body)
        self.assertIn('class="btn pw-toggle" type="button" aria-controls="confirm-password" aria-pressed="false">Show password', self.body)
        self.assertNotRegex(self.body, r"addEventListener\(\s*['\"](paste|copy|cut|drop)['\"]")

    def test_errors_are_inline_and_announced_and_it_posts_to_reauth(self):
        self.assertIn("'/api/auth/reauth'", self.body)
        self.assertIn('class="field-error"', self.body.replace("note.className = 'field-error'", 'class="field-error"'))
        self.assertIn("aria-invalid", self.body)
        # The page's live regions are inert while the modal is open: the error is read as an alert inside the dialog
        # (and from the focused field), and "Checking…" goes to a status region inside it.
        self.assertIn("note.setAttribute('role', 'alert')", self.body)
        self.assertIn('<div id="confirm-pw-status" class="visually-hidden" role="status"></div>', self.body)
        self.assertIn("setText(status, 'Checking your password…')", self.body)
        self.assertNotIn("announce(", self.body)
        self.assertIn("Cancel", self.body)

    def test_two_requests_share_one_dialog(self):
        self.assertIn("if (!confirmDialogOpen)", self.src)

    def test_the_dialog_is_big_enough_on_a_phone(self):
        css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"dialog\.confirm-dialog \{[^}]*width: min\(28rem, calc\(100% - 32px\)\)")
        self.assertIn("min-height: 44px", re.search(r"\.confirm-actions \.btn \{([^}]*)\}", css).group(1))
        self.assertIn("font-size: 16px", re.search(r"\.confirm-box input\[type=\"password\"\][^{]*\{([^}]*)\}", css).group(1))

    def test_every_page_that_asks_loads_common_js(self):
        for name in ("index.html", "job.html", "welcome.html"):
            self.assertIn('src="./common.js"', (FRONTEND / name).read_text(encoding="utf-8"), name)


class AccountCardsTest(unittest.TestCase):
    """The Settings account cards (account.js): the slots they fill, and what their templates promise."""

    @classmethod
    def setUpClass(cls):
        cls.index = parse("index.html")
        cls.src = (FRONTEND / "index.html").read_text(encoding="utf-8")
        cls.js = (FRONTEND / "account.js").read_text(encoding="utf-8")

    def test_loaded_after_the_dashboard_and_fills_only_its_slots(self):
        self.assertLess(self.src.index("common.js"), self.src.index("account.js"))
        ids = {n["attrs"].get("id") for n in self.index.nodes}
        self.assertIn("account-security-slot", ids)
        self.assertIn("account-delete-slot", ids)
        for slot in ("account-background-slot", "account-export-slot"):
            self.assertNotIn(slot, self.js, "those slots belong to other phases")

    def test_each_card_is_a_headed_section_with_a_status_region(self):
        self.assertIn('<h3 id="${id}-heading" tabindex="-1">', self.js)
        self.assertIn('role="status"', self.js)
        for title in ("Password", "Recovery codes", "Signed-in devices", "Delete account"):
            self.assertIn(title, self.js)
        self.assertNotIn("Two-step", self.js)

    def test_password_fields_say_what_they_are_for(self):
        self.assertIn("pwField('acct-pw-current', 'Current password', 'current-password')", self.js)
        self.assertIn("pwField('acct-pw-new', 'New password', 'new-password'", self.js)
        self.assertIn("pwField('acct-del-password', 'Your password', 'current-password')", self.js)
        self.assertIn("pwField('acct-rc-password', 'Your password', 'current-password')", self.js)
        self.assertIn('autocomplete="username"', self.js, "a hidden username field for password managers")
        self.assertNotIn('autocomplete="one-time-code"', self.js, "no code field: there is no second step")
        self.assertIn('class="p-button pw-toggle" type="button" aria-controls="${id}" aria-pressed="false">Show password', self.js)

    def test_sign_out_others_is_ticked_by_default(self):
        self.assertRegex(self.js, r'<input id="acct-pw-others" type="checkbox" checked />\s*Sign out other devices')

    def test_everything_sensitive_goes_through_with_recent_auth(self):
        for path in ("/api/auth/recovery-codes", "/api/auth/password", "/api/auth/sessions/end-others"):
            line = next(l for l in self.js.splitlines() if path in l and "post(" in l)
            self.assertTrue("withRecentAuth" in line or "recentWith" in line, f"{path} must go through withRecentAuth")
        self.assertIn("withRecentAuth(fn)", self.js)
        delete = next(l for l in self.js.splitlines() if "post('/api/account/delete'" in l)
        self.assertIn("recentWith", delete)

    def test_new_recovery_codes_reuse_the_first_run_step(self):
        self.assertIn("location.assign('first-run.html')", self.js)
        self.assertNotIn("codes-download", self.js, "the Copy / Download / Print step lives in first-run.html")

    def test_delete_says_what_goes_and_shows_the_servers_refusal(self):
        self.assertIn("can't be undone", self.js)
        for what in ("workspaces", "documents", "uploaded", "keys"):
            self.assertIn(what, self.js)
        self.assertIn("showError(form.querySelector('.acct-error')", self.js, "an admin's refusal is shown as the server wrote it")
        self.assertIn("location.replace('login.html')", self.js)
        self.assertIn("e.message", self.js)

    def test_controls_are_big_enough_and_use_16px_text(self):
        css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        field = re.search(r"\.account-card input\[type=\"password\"\][^{]*\{([^}]*)\}", css).group(1)
        self.assertIn("font-size: 16px", field)
        self.assertIn("min-height: 44px", field)
        self.assertIn("min-height: 44px", re.search(r"\.account-card \.btn:not\(\.btn-sm\)[^{]*\{([^}]*)\}", css).group(1))


class SignInPagesBehaviourTest(unittest.TestCase):
    """Fixes from review: where sign-in goes next and the account cards' announcements."""

    @classmethod
    def setUpClass(cls):
        cls.auth = (FRONTEND / "auth.js").read_text(encoding="utf-8")
        cls.common = (FRONTEND / "common.js").read_text(encoding="utf-8")
        cls.account = (FRONTEND / "account.js").read_text(encoding="utf-8")
        cls.app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("node"), "Node is needed to run nextPage()")
    def test_next_stays_on_this_site_whatever_the_url_parser_does_with_it(self):
        fn = re.search(r"function nextPage\(\) \{.*?\n\}\n", self.auth, re.S).group(0)
        script = ("global.location = { origin: 'http://127.0.0.1:8000', search: '' };\n"
                  f"{fn}\nconst out = {{}};\n"
                  "for (const next of JSON.parse(process.argv[1])) { location.search = '?next=' + encodeURIComponent(next);"
                  " out[next] = nextPage(); }\nconsole.log(JSON.stringify(out));")
        cases = ["/\t/evil.example/x", "/\n/evil.example", "//evil.example", "/\\evil.example",
                 "https://evil.example", "javascript:alert(1)", "/login.html", "/recover.html?x=1", "/first-run.html",
                 "/job.html?id=3#notes", "/", "", "/signup.html?invite=x",
                 "/.//evil.example/phish", "/..//evil.example", "/a/..//evil.example", "/./\\evil.example"]
        done = subprocess.run(["node", "-e", script, json.dumps(cases)], capture_output=True, text=True, check=True)
        out = json.loads(done.stdout)
        for bad in cases[:8] + [""] + cases[13:]:
            self.assertEqual(out[bad], "./", repr(bad))
        self.assertEqual(out["/job.html?id=3#notes"], "/job.html?id=3#notes")
        self.assertEqual(out["/"], "/")
        self.assertEqual(out["/first-run.html"], "./")

    def test_every_signed_in_page_shows_the_sign_in_notice_and_the_codes_reminder(self):
        """A sign-in can return to any page (?next=), and the notice is shown once, by the first page that asks."""
        for name in ("app.js", "job.js"):
            src = (FRONTEND / name).read_text(encoding="utf-8")
            self.assertIn("showSignInNotice()", src, name)
            self.assertIn("remindRecoveryCodes(me)", src, name)

    def test_every_call_to_a_recent_sign_in_endpoint_goes_through_the_dialog(self):
        """Any frontend call to an endpoint that answers reauth_required must be wrapped in withRecentAuth (or
        recentWith, which wraps it) so a stale session meets the dialog, not a dead-end message."""
        guarded: set[str] = set()
        for path in (ROOT / "backend").glob("*.py"):
            src = path.read_text(encoding="utf-8")
            prefix = re.search(r'APIRouter\(prefix="([^"]*)"', src)
            routes = list(re.finditer(r'@router\.(?:get|post|put|delete)\("([^"]+)"', src))
            for here, nxt in zip(routes, routes[1:] + [None]):
                if "require_recent_auth(" in src[here.end():nxt.start() if nxt else len(src)]:
                    guarded.add((prefix.group(1) if prefix else "") + here.group(1))
        self.assertIn("/api/settings/leftovers/delete", guarded)
        self.assertIn("/api/settings/server-key/rotate", guarded)
        self.assertIn("/api/auth/name", guarded)
        self.assertIn("/api/auth/recovery-codes", guarded)
        self.assertIn("/api/extension/keys", guarded)
        for name in ("app.js", "job.js", "welcome.js", "account.js", "common.js", "export.js", "restore.js",
                     "background.js", "extension.js", "server-settings.js"):
            for number, line in enumerate((FRONTEND / name).read_text(encoding="utf-8").splitlines(), 1):
                for path in guarded:
                    # A plain api(path) is a GET; the guard is on the calls that change something.
                    changes = "method:" in line or "post(" in line or "del(" in line
                    if f"'{path}'" in line and changes and ("api(" in line or "post(" in line or "del(" in line):
                        self.assertTrue(any(w in line for w in ("withRecentAuth", "recentWith", "reauth(")),
                                        f"{name}:{number} calls {path} without withRecentAuth")

    def test_the_recovery_codes_page_asks_for_the_password_when_it_is_needed(self):
        """The codes are not served to a session alone: the page keeps the ones made in this tab and otherwise asks for
        the password before it shows them."""
        shown = self.auth[self.auth.index("async function initFirstRun"):self.auth.index("async function submitSetup")]
        self.assertIn("stashedRecoveryCodes()", shown)
        self.assertIn("askForCodes(", shown)
        self.assertIn("'/api/auth/recovery-codes/show'", shown)
        self.assertNotIn("authCall('/api/auth/recovery-codes')", self.auth)
        self.assertIn("stashRecoveryCodes(made.recovery_codes)", self.account)

    def test_results_on_the_account_cards_are_announced_through_the_permanent_live_region(self):
        say = next(l for l in self.account.splitlines() if l.startswith("  const say = "))
        self.assertIn("announce(text)", say)

    def test_focus_stays_in_the_password_card_after_a_change(self):
        changed = self.account[self.account.index("async function changePassword"):self.account.index("/* ---------- recovery codes")]
        self.assertRegex(changed, r"renderPassword\(\);\s*document\.getElementById\('acct-pw-heading'\)\?\.focus\(\)")

    def test_new_recovery_codes_ask_for_the_password_only(self):
        card = self.account[self.account.index("function renderCodes"):self.account.index("/* ---------- sign out")]
        self.assertNotIn('id="acct-rc-code"', card)
        self.assertIn("recentWith(password.value, () => post('/api/auth/recovery-codes', { password: password.value }))", card)
        self.assertIn("pwField('acct-rc-password', 'Your password', 'current-password')", card)


class InstallSettingsCardsTest(unittest.TestCase):
    """The Settings tab's cards for the whole of this JobHuntPA (index.html + server-settings.js; they were in the
    admin panel): static checks that need no browser. The usage tables are written by script, so the checks read the
    templates in server-settings.js."""

    @classmethod
    def setUpClass(cls):
        cls.html = parse("index.html")
        cls.src = (FRONTEND / "index.html").read_text(encoding="utf-8")
        cls.js = (FRONTEND / "server-settings.js").read_text(encoding="utf-8")
        cls.ids = {n["attrs"].get("id"): n for n in cls.html.nodes if n["attrs"].get("id")}

    def test_the_moved_settings_are_on_the_settings_tab(self):
        for ident in ("llm-card", "llm-form", "llm-provider", "llm-model", "llm-base-url", "llm-key", "detect-levels",
                      "score-test", "usage-card", "usage-tiles", "address-card",
                      "public-url", "allowed-hosts", "address-save", "rotate-key", "plain-card", "plain-delete"):
            self.assertIn(ident, self.ids, ident)
        settings = self.src[self.src.index('id="tab-settings"'):self.src.index("</main>")]
        for ident in ("llm-card", "usage-card", "address-card", "rotate-card"):
            self.assertIn(f'id="{ident}"', settings, f"{ident} is on the Settings tab")
        self.assertNotIn("data-admin-only", self.src)
        self.assertNotIn("admin.html", self.src)

    def test_scripts_load_in_order(self):
        self.assertLess(self.src.index("./common.js"), self.src.index("./app.js"))
        self.assertLess(self.src.index("./app.js"), self.src.index("./server-settings.js"))

    def test_every_card_has_a_heading_that_says_it_is_for_the_whole_install(self):
        for ident in ("llm-card", "usage-card", "address-card", "rotate-card", "plain-card"):
            start = self.src.index(f'id="{ident}"')
            card = self.src[start:start + 600]
            with self.subTest(card=ident):
                self.assertRegex(card, r"<h3[^>]*>[^<]+<span class=\"p-scope-tag\">This JobHuntPA</span></h3>")

    def test_the_address_fields_are_labelled_and_described(self):
        for ident, hint in (("public-url", "public-url-hint"), ("allowed-hosts", "allowed-hosts-hint")):
            field = self.ids[ident]["attrs"]
            self.assertEqual(field.get("aria-describedby"), hint)
            self.assertIn(hint, self.ids)
            self.assertIn(f'<label class="p-label" for="{ident}">', self.src)
        self.assertEqual(self.ids["address-error"]["attrs"].get("role"), "alert")
        self.assertEqual(self.ids["address-status"]["attrs"].get("role"), "status")
        self.assertIn("setFieldError($('#public-url')", self.js)
        self.assertIn("setFieldError($('#allowed-hosts')", self.js)

    def test_the_usage_tables_have_captions_and_header_scopes_and_stack_on_phones(self):
        self.assertIn('<caption class="u-sr">${esc(caption)}</caption>', self.js)
        self.assertIn('scope="col"', self.js)
        self.assertIn('scope="row"', self.js)
        self.assertIn("p-table tab-stack-table usage-table", self.js)
        self.assertIn('data-label="${esc(heads[i + 1])}"', self.js)
        self.assertEqual(self.ids["usage-status"]["attrs"].get("role"), "status")
        self.assertEqual(self.ids["usage-tiles"]["tag"], "dl")

    def test_actions_that_need_the_password_go_through_withRecentAuth(self):
        for call in ("/api/settings/server-key/rotate", "/api/settings/leftovers/delete"):
            line = next(l for l in self.js.splitlines() if call in l)
            self.assertIn("withRecentAuth(", line, call)

    def test_what_the_server_sends_only_goes_in_escaped(self):
        for field in ("f.path", "r.name", "preset.note", "st.key.hint"):
            for m in re.finditer(re.escape(field), self.js):
                line = self.js[self.js.rfind("\n", 0, m.start()) + 1:self.js.find("\n", m.end())]
                self.assertIn("esc(", line, f"{field} written without esc(): {line.strip()}")
        self.assertNotIn("document.write", self.js)
        self.assertNotRegex(self.js, r"\son(click|submit|change)\s*=", "no handler attributes (CSP)")

    def test_the_name_card_needs_a_recent_sign_in(self):
        js = (FRONTEND / "account.js").read_text(encoding="utf-8")
        self.assertIn("card('acct-name', 'Your name'", js)
        line = next(l for l in js.splitlines() if "/api/auth/name" in l)
        self.assertIn("withRecentAuth(", line)
        self.assertIn('autocomplete="username"', js)


class ExportCardTest(unittest.TestCase):
    """The "Download your data" card (Settings tab, filled by export.js)."""

    @classmethod
    def setUpClass(cls):
        cls.js = (FRONTEND / "export.js").read_text(encoding="utf-8")
        cls.index = parse("index.html")

    def test_the_card_fills_its_slot_and_is_loaded_after_common_js(self):
        self.assertTrue(any(n["attrs"].get("id") == "account-export-slot" for n in self.index.nodes))
        src = (FRONTEND / "index.html").read_text(encoding="utf-8")
        self.assertLess(src.index("common.js"), src.index("export.js"))
        self.assertIn("getElementById('account-export-slot')", self.js)

    def test_heading_button_and_a_live_status(self):
        self.assertIn('<h3 id="export-title">', self.js)
        self.assertIn('<button id="export-btn" class="p-button p-button--positive" type="button">Download my data (.zip)</button>', self.js)
        self.assertIn('<span id="export-status" class="muted" role="status"></span>', self.js)
        self.assertIn("setBusy(btn, true)", self.js, "a working button uses aria-disabled, so focus stays on it")
        self.assertIn("not encrypted", self.js)
        self.assertIn("It is your safety net.", self.js, "the Settings tab says the export is the safety net (spec §1.2)")
        self.assertIn("it is your safety net", (FRONTEND / "first-run.html").read_text(encoding="utf-8"),
                      "and so does the first run")

    def test_a_post_through_withRecentAuth_and_a_blob(self):
        self.assertIn("method: 'POST'", self.js)
        self.assertIn("X-CSRF-Token", self.js)
        self.assertIn("withRecentAuth(() => fetchExport(formats))", self.js)
        self.assertIn("saveBlob(blob, name)", self.js)  # common.js saves it through a blob: address
        common = (FRONTEND / "common.js").read_text(encoding="utf-8")
        self.assertIn("URL.createObjectURL(blob)", common[common.index("function saveBlob"):])

    def test_busy_ends_and_every_outcome_is_said_in_the_status_region(self):
        self.assertIn("setBusy(btn, false)", self.js)
        for said in ("Making your file", "Done. Your browser saved", "Nothing was downloaded: confirm your password", "could not be downloaded"):
            self.assertIn(said, self.js)
        self.assertIn("Keep it somewhere safe", self.js)
        self.assertIn("e.status === 413", self.js, "a refusal says what to do (the server's own text)")


class ExportFileTypesTest(unittest.TestCase):
    """The file types on the "Download your data" card (Phase E)."""

    @classmethod
    def setUpClass(cls):
        cls.js = (FRONTEND / "export.js").read_text(encoding="utf-8")

    def test_a_labelled_group_of_three_checkboxes_markdown_ticked(self):
        self.assertIn('<fieldset class="form-group export-types" aria-describedby="export-types-hint">', self.js)
        self.assertIn("<legend>File types</legend>", self.js)
        self.assertIn("""[['md', 'Markdown (.md)'], ['docx', 'Word (.docx)'], ['odt', 'OpenDocument (.odt)']]""", self.js)
        self.assertIn('<label class="check"><input type="checkbox" id="export-type-${value}"', self.js,
                      "each box sits inside its label")
        self.assertIn("n === 0 ? ' checked' : ''", self.js)

    def test_none_ticked_is_an_error_said_next_to_the_group_and_announced(self):
        self.assertIn("Tick at least one file type.", self.js)
        self.assertIn("announce('Tick at least one file type.', true)", self.js)
        self.assertIn("fieldset.setAttribute('aria-invalid', 'true')", self.js)
        self.assertIn("'export-types-error export-types-hint'", self.js)
        self.assertIn("boxes[0].focus()", self.js)
        self.assertIn("body: JSON.stringify({ formats })", self.js)


class RestoreCardTest(unittest.TestCase):
    """The "Import from an export" card (Settings tab, filled by restore.js)."""

    @classmethod
    def setUpClass(cls):
        cls.js = (FRONTEND / "restore.js").read_text(encoding="utf-8")
        cls.src = (FRONTEND / "index.html").read_text(encoding="utf-8")
        cls.index = parse("index.html")

    def test_the_card_fills_its_own_slot_after_the_export_and_loads_after_common_js(self):
        ids = [n["attrs"].get("id") for n in self.index.nodes]
        self.assertIn("account-import-slot", ids)
        self.assertLess(ids.index("account-export-slot"), ids.index("account-import-slot"))
        self.assertLess(self.src.index("common.js"), self.src.index("restore.js"))
        self.assertIn("getElementById('account-import-slot')", self.js)

    def test_heading_labelled_file_field_and_a_live_status(self):
        self.assertIn('<h3 id="restore-title">Import from an export</h3>', self.js)
        self.assertIn('<label for="restore-file">Export file (.zip)</label>', self.js)
        self.assertIn('<input id="restore-file" type="file" accept=".zip,application/zip" />', self.js)
        self.assertIn('<span id="restore-status" class="muted" role="status"></span>', self.js)
        self.assertIn("setFieldError(input, 'Choose the .zip file", self.js)
        self.assertIn('<legend id="restore-legend">Workspaces in this export</legend>', self.js)

    def test_both_steps_post_the_zip_through_withRecentAuth(self):
        self.assertIn("withRecentAuth(() => postZip('/api/account/import/check', file))", self.js)
        self.assertIn("withRecentAuth(() => postZip(`/api/account/import?pick=", self.js)
        self.assertIn("'X-CSRF-Token': csrf", self.js)
        self.assertIn("method: 'POST'", self.js)
        for said in ("Nothing was ${what}: confirm your password", "Tick at least one workspace to import.",
                     "Imported ${plural("):
            self.assertIn(said, self.js)
        self.assertEqual(self.js.count("setBusy(check, true)"), 1)
        self.assertEqual(self.js.count("setBusy(go, false)"), 1)

    def test_ticking_no_workspace_is_a_field_error_on_the_list(self):
        """As on the export card's file types: a visible error tied to the fieldset, said at once (WCAG 3.3.1)."""
        self.assertIn('<p id="restore-pick-error" class="field-error" hidden></p>', self.js)
        self.assertIn("fieldset.setAttribute('aria-invalid', 'true')", self.js)
        self.assertIn("fieldset.setAttribute('aria-describedby', 'restore-pick-error')", self.js)
        self.assertIn("announce(text, true)", self.js)
        self.assertIn("pickInvalid('Tick at least one workspace to import.')", self.js)
        self.assertIn("fieldset.removeAttribute('aria-invalid')", self.js)
        self.assertIn("options.addEventListener('change'", self.js)

    def test_what_the_file_holds_goes_in_escaped(self):
        self.assertIn("${esc(w.name)}", self.js)
        self.assertIn("${esc(restoreCounts(w))}", self.js)
        self.assertIn("${Number(w.index)}", self.js)
        # every other ${...} in its HTML is the app's own text
        for expr in re.findall(r"\$\{([^}]+)\}", self.js[self.js.index("options.innerHTML = found"):self.js.index("pick.hidden = false")]):
            self.assertTrue(expr.startswith(("esc(", "Number(")), expr)

    def test_phones_and_controls_are_big_enough(self):
        css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        self.assertIn('#restore-card input[type="file"] { max-width: 100%; font-size: 16px; }', css)
        self.assertIn(".restore-option { align-items: flex-start; overflow-wrap: anywhere; }", css)


class SourcesBulkDialogTest(unittest.TestCase):
    """Careers pages in bulk (Search setup > Company sources; sources-bulk.js, open-source spec §1.14)."""

    @classmethod
    def setUpClass(cls):
        cls.js = (FRONTEND / "sources-bulk.js").read_text(encoding="utf-8")
        cls.html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        cls.index = parse("index.html")

    def node(self, node_id):
        found = [n for n in self.index.nodes if n["attrs"].get("id") == node_id]
        self.assertEqual(len(found), 1, node_id)
        return found[0]

    def test_loaded_after_the_dashboard_and_its_helpers(self):
        self.assertLess(self.html.index('src="./common.js"'), self.html.index('src="./sources-bulk.js"'))
        self.assertLess(self.html.index('src="./app.js"'), self.html.index('src="./sources-bulk.js"'))

    def test_a_labelled_native_dialog_opened_by_a_button_that_says_so(self):
        self.assertEqual(self.node("srcbulk-dialog")["tag"], "dialog")
        self.assertEqual(self.node("srcbulk-dialog")["attrs"].get("aria-labelledby"), "srcbulk-title")
        self.assertEqual(self.node("sources-bulk-open")["attrs"].get("aria-haspopup"), "dialog")
        self.assertIn("dlg.showModal()", self.js)
        self.assertIn("dlg.addEventListener('close'", self.js, "focus goes back to the button that opened it")

    def test_each_step_is_a_section_named_by_a_heading_that_takes_focus(self):
        for step in ("input", "review", "done"):
            section = self.node(f"srcbulk-step-{step}")
            self.assertEqual(section["attrs"].get("aria-labelledby"), f"srcbulk-{step}-heading")
            self.assertEqual(self.node(f"srcbulk-{step}-heading")["attrs"].get("tabindex"), "-1")
        self.assertIn("el(`srcbulk-${step}-heading`)).focus()", self.js)

    def test_the_format_is_shown_and_describes_the_box(self):
        self.assertEqual(self.node("srcbulk-text")["attrs"].get("aria-describedby"), "srcbulk-format")
        for words in ("company,url", "double quotes", "UTF-8", "optional"):
            self.assertIn(words, self.html)
        self.assertEqual(self.node("srcbulk-file")["attrs"].get("type"), "file")

    def test_it_says_nothing_is_fetched_and_the_flags_are_not_a_safety_verdict(self):
        self.assertIn("Nothing is fetched from these sites yet", self.html)
        self.assertIn("Nothing has been fetched from these sites yet", self.html)
        self.assertIn("it is not a safety check of the site", self.html)
        self.assertIn("can't be added", self.html)

    def test_addresses_are_text_never_links(self):
        """An address in the list must not be opened by a stray click before the person confirms."""
        self.assertNotIn("<a ", self.js.split("/* -- export")[0])
        self.assertNotIn("href", self.js.split("/* -- export")[0])

    def test_flagged_rows_start_unticked_and_blocked_rows_have_no_tick_box(self):
        self.assertIn("ticked: canAdd(row) && !row.flags.length", self.js)
        self.assertIn("const head = blocked", self.js)

    def test_counts_results_and_errors_are_said(self):
        self.assertEqual(self.node("srcbulk-count")["attrs"].get("role"), "status")
        self.assertEqual(self.node("sources-export-status")["attrs"].get("role"), "status")
        for said in ("announce(`Removed", "announce(`${summary}.`)", "setFieldError(box, e.message)",
                     "announce(`Nothing was added"):
            self.assertIn(said, self.js)
        self.assertIn("setBusy(btn, true)", self.js)
        self.assertIn("aria-describedby=\"${id}-info\"", self.js, "a tick box is described by its address and flags")

    def test_meaning_is_not_colour_alone_and_targets_are_big_enough(self):
        self.assertIn("<strong>${mark}:</strong>", self.js)
        self.assertIn(".srcbulk-head input[type=\"checkbox\"] { width: 24px; height: 24px;", self.css)
        self.assertIn("@media (pointer: coarse) { .srcbulk-row .btn-sm { min-height: 44px; } }", self.css)
        self.assertIn(".srcbulk-url { font-family", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css.split("/* Careers pages in bulk")[1].split("/* Map */")[0],
                      "long addresses wrap at 320 px")

    def test_no_inline_styles_or_handlers(self):
        self.assertNotRegex(self.js, r"style=|\bon[a-z]+=")


class PageLayoutsTest(unittest.TestCase):
    """AI mappings (open-source spec §1.15): the Settings card's list of job sites the AI worked out (extension.js),
    and a source read with a worked-out layout (app.js)."""

    @classmethod
    def setUpClass(cls):
        cls.ext = (FRONTEND / "extension.js").read_text(encoding="utf-8")
        cls.app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    def test_the_sites_are_a_list_named_by_its_heading_with_forget_buttons_that_name_their_site(self):
        self.assertIn('<h4 id="ext-sites-heading" tabindex="-1">', self.ext)
        self.assertIn('<ul class="ext-keys" aria-labelledby="ext-sites-heading">', self.ext)
        self.assertRegex(self.ext, r'data-ext-forget="\$\{esc\(m\.site\)\}">Forget<span class="visually-hidden"> '
                                   r'\$\{esc\(m\.site\)\}</span></button>')
        forget = self.ext[self.ext.index("async function forget("):]
        self.assertIn("say(`Forgot the layout of ${site}.`)", forget, "the result is announced (role=status)")
        self.assertIn("$('#ext-sites-heading').focus()", forget, "focus doesn't fall to the page when the list redraws")
        self.assertIn('role="alert"', self.ext[self.ext.index("async function loadSites("):])

    def test_a_source_says_it_is_read_with_a_layout_and_its_forget_button_names_it(self):
        self.assertIn("Read with a page layout the AI worked out", self.app)
        self.assertIn('data-act="forget" type="button">Forget layout<span class="u-sr"> of ${name}</span>',
                      self.app)
        self.assertIn("announce(`${src.label || src.url} is read the usual ways again.`)", self.app)
        forget = self.app[self.app.index("async function forgetLayout("):]
        forget = forget[:forget.index("\n}\n")]
        self.assertIn("document.getElementById(`src-${src.id}-test`)?.focus()", forget,
                      "focus stays on the source's row (its Test button) when the Forget layout button goes")


class WelcomePageTest(unittest.TestCase):
    """The sign-up walkthrough (welcome.html + welcome.js): static checks that need no browser. The steps are
    written by script, so the checks read the templates in welcome.js."""

    @classmethod
    def setUpClass(cls):
        cls.html = parse("welcome.html")
        cls.js = (FRONTEND / "welcome.js").read_text(encoding="utf-8")
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    def by_id(self, ident):
        return next(n for n in self.html.nodes if n["attrs"].get("id") == ident)

    def test_early_theme_script_and_shared_scripts(self):
        src = (FRONTEND / "welcome.html").read_text(encoding="utf-8")
        self.assertIn('localStorage.getItem("jobhunt.theme")', src)
        self.assertLess(src.index("jobhunt.theme"), src.index("themes.css"), "the theme is applied before the CSS loads")
        self.assertLess(src.index("common.js"), src.index("welcome.js"))

    def test_progress_is_a_labelled_list_with_a_text_count(self):
        nav = self.by_id("wiz-progress")
        self.assertEqual(nav["tag"], "nav")
        self.assertTrue(nav["attrs"].get("aria-label"))
        self.assertEqual(self.by_id("wiz-steps")["tag"], "ol")
        self.assertIn("Step ${wiz.idx + 1} of ${total}", self.js, "the count is written as words")
        self.assertIn('aria-current="step"', self.js, "the current mark is exposed")
        self.assertIn(">Done</span>", self.js, "a finished step says Done in words, not only a colour")

    def test_every_step_is_a_fieldset_with_a_legend_and_a_focusable_heading(self):
        shell = re.search(r"\$\('#wiz-body'\)\.innerHTML = `(.*?)`;", self.js, re.S)
        self.assertIsNotNone(shell, "every step is drawn by one template")
        self.assertIn("<fieldset", shell.group(1))
        self.assertIn("<legend>", shell.group(1))
        self.assertIn('<h2 id="step-title" tabindex="-1">', shell.group(1))
        self.assertIn("$('#step-title').focus()", self.js, "focus moves to each step's heading")
        self.assertIn("Step ${wiz.idx + 1} of ${total}: </span>", self.js, "the heading says which step it is")
        steps = re.findall(r"^  (\w+): \{\n    title: ", self.js, re.M)
        self.assertGreaterEqual(len(steps), 5, steps)
        self.assertEqual(self.js.count("    intro:"), len(steps), "every step explains itself")
        for group in re.findall(r'<fieldset class="form-group wiz-group">\s*(<\w+>)', self.js):
            self.assertEqual(group, "<legend>", "a group of checkboxes is named by its legend")

    def test_buttons_form_and_error_summary(self):
        for ident, label, kind in (("wiz-back", "Back", "button"), ("wiz-skip", "Skip", "button"), ("wiz-next", "Next", "submit")):
            node = self.by_id(ident)
            self.assertEqual((node["tag"], node["attrs"].get("type")), ("button", kind))
            self.assertIn(label, node["text"])
        summary = self.by_id("error-summary")
        self.assertEqual(summary["attrs"].get("tabindex"), "-1", "the summary takes focus")
        self.assertEqual(summary["attrs"].get("role"), "alert")
        self.assertIn("summary.focus()", self.js)
        self.assertIn("aria-invalid", self.js)
        self.assertIn("aria-describedby", self.js)
        self.assertIn("novalidate", self.by_id("wiz-form")["attrs"], "our own messages, not the browser's")

    def test_a_way_out_and_no_blocked_paste(self):
        self.assertIn("Skip the walkthrough", self.by_id("wiz-leave")["text"])
        self.assertNotRegex(self.js, r"addEventListener\(\s*['\"](paste|copy|cut|drop)['\"]")

    def test_targets_and_text_size(self):
        field = re.search(r"\.wiz-form input\[type=\"text\"\][^{]*\{([^}]*)\}", self.css).group(1)
        self.assertIn("min-height: 44px", field)
        self.assertIn("font-size: 16px", field)
        # Back, Skip and Next are full-size buttons (44 px: .p-button's 2.75rem), not dense ones
        self.assertIn("min-height: 2.75rem", re.search(r"\.p-button, \.btn \{([^}]*)\}", self.css).group(1))
        for ident in ("wiz-back", "wiz-skip", "wiz-next"):
            classes = self.by_id(ident)["attrs"]["class"].split()
            self.assertIn("p-button", classes)
            self.assertNotIn("is-dense", classes)
        mark = re.search(r"\.wiz-steps \.p-side-navigation__link \{([^}]*)\}", self.css).group(1)
        self.assertIn("min-height: 2.75rem", mark)

    def test_unbuilt_features_are_hidden_behind_a_feature_check(self):
        self.assertNotIn("digest", self.js, "there are no digest emails")
        steps = re.search(r"function buildSteps\(\) \{(.*?)\n\}", self.js, re.S).group(1)
        self.assertIn("if (wiz.features.scheduled_searches) steps.push('auto')", steps)

    def test_the_resume_step_explains_why_and_points_to_documents(self):
        self.assertRegex(self.js, r"fit scores?")
        self.assertIn("Documents tab", self.js)


class BackgroundAccessCardTest(unittest.TestCase):
    """The Settings tab's "Background access" card (frontend/index.html + background.js, Phase G)."""

    @classmethod
    def setUpClass(cls):
        cls.html = parse("index.html")
        cls.js = (FRONTEND / "background.js").read_text(encoding="utf-8")
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        cls.ids = {n["attrs"]["id"]: n for n in cls.html.nodes if "id" in n["attrs"]}

    def test_the_card_sits_in_its_slot_under_a_heading(self):
        card = self.ids["background-card"]
        slot = self.ids["account-background-slot"]
        self.assertEqual(self.html.nodes[card["parent"]]["attrs"].get("id"), "account-background-slot")
        self.assertEqual(slot["tag"], "div")
        heading = self.ids["background-title"]
        self.assertEqual(heading["tag"], "h3")
        self.assertIn("Background access", heading["text"])

    def test_the_control_is_a_labelled_switch_described_by_its_explanation(self):
        switch = self.ids["background-switch"]
        self.assertEqual((switch["tag"], switch["attrs"].get("type"), switch["attrs"].get("role")),
                         ("input", "checkbox", "switch"))
        label = next(a for a in self.html.ancestors(switch) if a["tag"] == "label")
        self.assertIn("work while I'm signed out", label["text"], "a visible label that names what it does")
        self.assertEqual(switch["attrs"].get("aria-describedby"), "background-explain")
        self.assertIn("background-explain", self.ids)

    def test_errors_are_announced_and_progress_never_drops_focus(self):
        self.assertEqual(self.ids["background-error"]["attrs"].get("role"), "alert")
        self.assertIn("hidden", self.ids["background-error"]["attrs"])
        self.assertIn("announce(", self.js, "changes are said to screen readers")
        self.assertEqual(self.ids["background-retry"]["tag"], "button")  # a failed load can be tried again
        self.assertIn("hidden", self.ids["background-retry"]["attrs"])
        # a busy switch isn't disabled (that would drop keyboard focus); only the not-yet-loaded one is
        self.assertNotRegex(self.js, r"box\.disabled\s*=\s*(true|background\.busy)")
        self.assertIn("box.disabled = !background.loaded", self.js)

    def test_turning_it_on_asks_for_the_password_through_the_shared_helper(self):
        self.assertRegex(self.js, r"withRecentAuth\(\(\) => api\('/api/account/background'")
        self.assertIn("renderBackground(); // the switch goes back", self.js, "a refusal puts the switch back")

    def test_the_words_come_from_common_js_and_promise_nothing_that_does_not_exist(self):
        common = (FRONTEND / "common.js").read_text(encoding="utf-8")
        self.assertIn("Only you can unlock your data. JobHuntPA can't run searches while you're away.", common)
        self.assertIn("JobHuntPA keeps a key on the server so it can run scheduled searches while you're away.", common)
        self.assertIn("BACKGROUND_ON_COPY", self.js)
        self.assertIn("BACKGROUND_OFF_COPY", self.js)

    def test_the_generated_and_static_controls_have_names(self):
        self.assertEqual(GeneratedMarkupTest().check_file("background.js"), [])
        # the schedule card's "Turn on background access" button is written by app.js and has text
        app = (FRONTEND / "app.js").read_text(encoding="utf-8")
        self.assertRegex(app, r'<button class="link" type="button" data-focus-background>\$\{again \? \'Turn it off and on again\' : \'Turn on background access\'\}</button>')
        self.assertIn("data-focus-background", self.js)

    def test_the_schedule_card_needs_no_membership(self):
        app = (FRONTEND / "app.js").read_text(encoding="utf-8")
        start = app.index("function renderScheduleStatus")
        body = app[start:app.index("async function loadSchedule", start)]
        self.assertNotIn("membership", body)
        self.assertNotIn("billing.html", body)
        self.assertIn("data-focus-background", body)
        self.assertEqual(GeneratedMarkupTest().check_file("app.js"), [])

    def test_targets_are_big_enough_and_the_page_scripts_load_in_order(self):
        self.assertRegex(self.css, r"\.check input[^{]*\{[^}]*width: 18px")  # 24 px high row, 44 px on small screens
        self.assertIn("min-height: 44px", re.search(r"@media[^{]*\{[^@]*?\.check, \.check-field[^{]*\{([^}]*)\}", self.css, re.S).group(1))
        src = (FRONTEND / "index.html").read_text(encoding="utf-8")
        self.assertLess(src.index("./app.js"), src.index("./background.js"))
        self.assertIn("common.js", src)


class GhostJobPagesTest(unittest.TestCase):
    """The ghost-job circle under the fit score, its hover text, the why? disclosure, the Jobs filter and the job
    page section."""

    @classmethod
    def setUpClass(cls):
        read = lambda n: (FRONTEND / n).read_text(encoding="utf-8")  # noqa: E731
        cls.common, cls.app, cls.job = read("common.js"), read("app.js"), read("job.js")
        cls.index, cls.job_html, cls.css = read("index.html"), read("job.html"), read("styles.css")

    def test_the_circle_has_words_beside_it_and_in_its_hover_text(self):
        """🟢, 🟡 and 🔴 differ only in colour, so the rating is always in words too: hidden beside the circle for
        screen readers, and in the hover text with the signs that were found."""
        block = self.common[self.common.index("const GHOST_STRENGTH"):self.common.index("function ratingsHtml")]
        self.assertIn("const GHOST_DOT = { likely: '🔴', possible: '🟡' };", block)
        self.assertIn("GHOST_DOT[job.ghost?.level] || '🟢'", block)
        self.assertIn("several signs", block)
        self.assertIn("some signs", block)
        self.assertIn("'No strong signs of a ghost job'", block)
        meter = block[block.index("function ghostMeter"):]
        self.assertIn('<span aria-hidden="true">👻 ${ghostDot(job)}</span><span class="visually-hidden">${ghostWords(job)}</span>', meter)
        self.assertEqual(meter.count('title="${esc(ghostHover(job))}"'), 2)  # the plain one and the job page's link
        self.assertIn("if (!job.ghost) return ''", meter)
        hover = block[block.index("function ghostHover"):block.index("function ghostMeter")]
        self.assertIn("`• ${s.text}`", hover)  # each sign found, one a line
        self.assertIn(".join('\\n')", hover)
        self.assertIn("No signs of a ghost job were found.", hover)
        self.assertIn("'One weak sign' : 'Weak signs'", hover)  # weak signs under the flag are said too
        self.assertNotIn("%", block)  # no percentage: the app doesn't put a number on it
        self.assertNotIn("percent", self.common + self.app + self.job)

    def test_the_fit_score_has_its_icon_and_keeps_its_words(self):
        badge = self.common[self.common.index("const SCORE_ICON"):self.common.index("function fitHtml")]
        self.assertIn("const SCORE_ICON = '<span aria-hidden=\"true\">🎯 </span>';", badge)
        self.assertEqual(badge.count("${SCORE_ICON}"), 3)  # scored, not scored yet, scoring failed
        for words in ("Fit score ", "Not scored yet", "Scoring failed", " of 100"):
            self.assertIn(f'<span class="visually-hidden">{words}', badge)

    def test_the_circle_is_under_the_score_on_cards_the_map_list_and_the_job_page(self):
        ratings = self.common[self.common.index("function ratingsHtml"):self.common.index("function ghostWhy")]
        self.assertIn('<div class="ratings">${scoreBadge(job)}${filterWarnButton(job)}${ghostMeter(job, ghostHref)}</div>',
                      ratings)
        self.assertIn("${ratingsHtml(job)}", self.app)
        self.assertIn("${ratingsHtml(j)}", self.app)
        # The job page's summary says both in words (common.js fitLabel, ghostLabelHtml); the rating links to the explanation.
        self.assertIn('<span class="visually-hidden">Fit score </span>${fit.score}<span class="visually-hidden"> out of 100', self.common)
        self.assertIn("fitLabel(job, { large: true })", self.job)
        self.assertIn('href="#ghost-title" title="${esc(ghostHover(j))}">${esc(words)}<span class="visually-hidden">: why this rating</span>', self.job)
        self.assertIn("const words = level ? ghostWords(j) : 'Real vacancy: likely';", self.job)
        self.assertIn('<h2 id="ghost-title" class="jp-h2" tabindex="-1">', self.job_html)  # ... which can take the focus
        self.assertRegex(self.css, r"\.ratings \{[^}]*flex-direction: column")
        for src in (self.app, self.job):
            self.assertNotIn("scoreBadge(", src)  # nowhere a score without the circle under it
        # The map list has no "why?" line, so a flagged job is said in words there (the circle alone is colour).
        self.assertIn("possible ghost job (${GHOST_STRENGTH[j.ghost.level]})", self.app)

    def test_the_filter_warning_is_a_named_button_above_the_circle_that_opens_a_dialog(self):
        """A job the person included that doesn't meet their filters: ⚠️ between the score and the ghost-job circle, a
        real button whose name says what it is (the emoji is hidden from screen readers), opening a dialog on the Jobs
        tab, the map list and the job page."""
        block = self.common[self.common.index("function filterWarnWords"):self.common.index("function ratingsHtml")]
        button = block[block.index("function filterWarnButton"):block.index("const filterWarningsTitle")]
        self.assertIn("if (!(job.filter_warnings || []).length) return '';", button)  # only when there is something
        self.assertIn('<button class="filter-warn" type="button" data-act="filter-warnings" aria-haspopup="dialog"', button)
        self.assertIn('<span aria-hidden="true">⚠️</span><span class="visually-hidden">${filterWarnWords(job)}: show which</span>',
                      button)
        self.assertIn("`Doesn't meet ${n === 1 ? 'one of your filters' : `${n} of your filters`}`", block)
        body = block[block.index("function filterWarningsHtml"):]
        self.assertIn("${esc(upper(w))}", body)  # each rule escaped (its words are the person's own)
        self.assertIn('<ul class="filter-warnings">', body)
        # Wired on the card, in the map list and on the job page, each to its page's dialog
        self.assertIn("card.querySelector('[data-act=\"filter-warnings\"]')?.addEventListener('click', () => showFilterWarnings(job));",
                      self.app)
        self.assertIn("box.querySelectorAll('[data-act=\"filter-warnings\"]')", self.app)
        self.assertIn("openHtmlModal(filterWarningsTitle(job), filterWarningsHtml(job));", self.app)
        self.assertIn("$('#job-head [data-act=\"filter-warnings\"]')?.addEventListener('click', showFilterWarnings);", self.job)
        self.assertIn('<dialog id="modal" class="modal" aria-labelledby="modal-title">', self.job_html)
        self.assertIn("dlg.showModal();", self.job[self.job.index("function showFilterWarnings"):])
        self.assertIn("initModal();", self.job)
        self.assertRegex(self.css, r"(?m)^  \.filter-warn \{ min-height: 44px; \}")  # a touch-sized target

    def test_always_include_is_a_labelled_checkbox_that_keeps_focus(self):
        """"Always include": a real checkbox inside its label (a touch-sized .check-field), named on a card by the job's
        title too; after the card or the job page's header is redrawn, focus is back on it (or on the Jobs tab when the
        filters now leave the job out of the list), and a toast says what became of the job."""
        toggle = self.common[self.common.index("function includeToggle"):self.common.index("function includedWords")]
        self.assertIn('<label class="check-field include-toggle"><input type="checkbox" data-act="include"', toggle)
        self.assertIn('<span>Always include</span></label>', toggle)
        self.assertIn("${showsInclude(job) ? includeToggle(job, titleId) : ''}", self.app)
        card = self.app[self.app.index("async function setIncluded"):self.app.index("function showFilterWarnings")]
        self.assertIn("const back = focusFinder(box);", card)
        self.assertIn("(back() || $('#tab-jobs')).focus({ preventScroll: true });", card)
        self.assertIn("toast(includedWords(job, !state.filter.showExcluded), 'ok', 6000);", card)
        self.assertIn("${includeToggle(job)}", self.job)
        page = self.job[self.job.index("async function setIncluded"):self.job.index("function showFilterWarnings")]
        self.assertIn("renderHead();\n    $('#job-head [data-act=\"include\"]').focus();", page)
        self.assertIn("toast(includedWords(job), 'ok', 6000);", page)

    def test_add_a_job_opens_a_dialog_with_labelled_fields(self):
        self.assertIn('<button id="add-job-open" class="p-button is-dense" type="button" aria-haspopup="dialog"', self.index)
        dialog = self.app[self.app.index("function showAddJob"):self.app.index("function addJobResult")]
        self.assertIn('<label for="add-job-file">', dialog)
        self.assertIn('<input id="add-job-file" type="file"', dialog)
        self.assertIn('<label class="p-label" for="add-job-url">', dialog)
        self.assertIn('aria-describedby="add-job-url-hint"', dialog)
        self.assertIn('<p id="add-job-status" class="u-small u-muted" role="status"></p>', dialog)  # the outcome is announced
        result = self.app[self.app.index("function addJobResult"):self.app.index("async function addJobPages")]
        for value in ("esc(name)", "esc(j.title || 'Job')", "esc(j.company)", "esc(j.link)", "esc(j.filter_warnings.join('; '))"):
            self.assertIn(value, result)

    def test_the_circles_pill_is_plain_text_on_the_field_ground(self):
        rule = re.search(r"(?m)^\.ghost-dot \{([^}]*)\}", self.css).group(1)
        for part in ("background: var(--field)", "border: 1px solid var(--border)", "color: var(--text)", "cursor: help"):
            self.assertIn(part, rule)
        self.assertNotRegex(self.css, r"\.ghost-(?:h\d|pct|scale)")  # the percentage's colour steps and scale are gone

    def test_why_disclosure_is_a_native_details_with_a_full_name_and_escaped_text(self):
        why = self.common[self.common.index("function ghostWhy"):self.common.index("function modeOf")]
        self.assertIn("<details", why)
        self.assertIn('<summary><span aria-hidden="true">👻 ${ghostDot(job)} </span>Possible ghost job (${GHOST_STRENGTH[g.level]}): why?</summary>', why)
        self.assertIn("if (!g?.level || !(g.signals || []).length) return ''", why)  # only on a flagged job's card
        self.assertIn("esc(s.text)", why)
        self.assertIn("This may still be a real job", why)
        self.assertIn("${ghostWhy(job)}", self.app)

    def test_there_is_no_filter_that_hides_possible_ghost_jobs(self):
        """Open-source spec §1.10: the "Hide possible ghost jobs" filter is removed, with its count text."""
        for src in (self.index, self.app, self.common, self.job):
            self.assertNotIn("filter-hide-ghost", src)
            self.assertNotIn("hideGhost", src)
            self.assertNotIn("Hide possible ghost jobs", src)
            self.assertNotIn("of them possible ghost job", src)
        self.assertIn("hidden by filters` : ''}", self.app)  # the count says only how many the filters hide
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("Hide possible ghost jobs", readme)

    def test_job_page_reasons_are_collapsed_under_the_disclaimer(self):
        """Open-source spec §1.10: the circle and its level stay in view with the disclaimer; the reasons (every
        check, the colours, what to ask) open on request, in a native disclosure."""
        fn = self.job[self.job.index("/* ---------- is this a real vacancy?"):self.job.index("function renderFit")]
        self.assertIn("const GHOST_DISCLAIMER = 'This is worked out from your own searches only. It indicates whether an ad "
                      "may be a ghost job or a real position; it proves neither.';", fn)
        verdict = fn.index('<p class="ghost-big">')
        disclaimer = fn.index('<p class="hint ghost-disclaimer">${GHOST_DISCLAIMER}</p>')
        details = fn.index('<details id="ghost-why" class="ghost-details">')
        self.assertLess(verdict, disclaimer)
        self.assertLess(disclaimer, details)
        self.assertNotIn('<details id="ghost-why" class="ghost-details" open', fn)  # collapsed
        self.assertIn("<summary>${g.level ? 'Why this rating, and what to do' : 'Why this rating'}</summary>", fn)
        for inside in ('id="ghost-why-title"', 'id="ghost-key-title"', "What to do", "A rating from the signs above"):
            self.assertGreater(fn.index(inside), details, inside)
        # The header's circle links to the section and opens the reasons.
        self.assertIn("$('#job-head a.jp-ghost-link')?.addEventListener('click', () => { const why = $('#ghost-why'); if (why) why.open = true; });", self.job)
        self.assertIn("if (id === 'ghost-title') { const why = $('#ghost-why'); if (why) why.open = true; }", self.job)  # a #ghost-title link too
        self.assertRegex(self.css, r"\.ghost-details > summary \{[^}]*cursor: pointer")
        start = self.css.index("@media (pointer: coarse) {\n")
        self.assertIn(".ghost-details summary", self.css[start:self.css.index("\n}", start)])
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("it proves neither", readme)

    def test_job_page_section_is_named_and_shown_for_every_job(self):
        # (hidden only until the job has loaded)
        self.assertRegex(self.job_html, r'<section id="job-ghost-section"[^>]*aria-labelledby="ghost-title"[^>]*hidden>')
        self.assertIn('<h2 id="ghost-title" class="jp-h2" tabindex="-1">Is this a real vacancy?</h2>', self.job_html)
        fn = self.job[self.job.index("function renderGhost"):self.job.index("function renderFit")]
        self.assertIn("box.hidden = !g;", fn)
        self.assertNotIn("box.hidden = !g?.level", fn)
        self.assertIn("${g.level ? `<h3", fn)  # what to ask the employer: for a flagged job only
        self.assertIn("may well be a real job", fn)
        for ask in ("Is the role open now", "when do they need someone to start", "Why was the ad posted again"):
            self.assertIn(ask.lower(), fn.lower())

    def test_job_page_says_the_rating_in_words_why_and_what_the_colours_mean(self):
        fn = self.job[self.job.index("/* ---------- is this a real vacancy?"):self.job.index("function renderFit")]
        # The rating: the circle is decoration, the words say it.
        self.assertIn('<span class="ghost-big-dot" aria-hidden="true">👻 ${ghostDot(job)}</span>', fn)
        for words in ("<strong>Several signs</strong> suggest", "<strong>Some signs</strong> suggest", "<strong>No strong signs</strong>",
                      "<strong>No signs</strong> of a ghost job: none of the signs the app looks for were found."):
            self.assertIn(words, fn)
        self.assertIn("could not be made, so the rating rests on less", fn)
        # Every check, each with its state in words and an icon that is decoration.
        self.assertIn('<h3 class="ghost-sub" id="ghost-why-title">Why this rating</h3>', fn)
        self.assertIn('<ul class="ghost-checks" aria-labelledby="ghost-why-title">', fn)
        for words in ("Found", "Not found", "Could not be checked", "strong sign", "moderate sign", "weak sign"):
            self.assertIn(words, fn)
        for icon in ("⚠ ", "✓ ", "? "):
            self.assertIn(f'<span aria-hidden="true">{icon}</span>', fn)
        for escaped in ("esc(sign.text)", "esc(sign.detail)", "esc(check.label)", "esc(check.note)"):
            self.assertIn(escaped, fn)
        # What the three colours mean, each named in words, without overclaiming.
        self.assertIn('<h3 class="ghost-sub" id="ghost-key-title">What the colours mean</h3>', fn)
        for circle, name in (("🟢", "Green"), ("🟡", "Yellow"), ("🔴", "Red")):
            self.assertIn(f'<li><span aria-hidden="true">{circle} </span><strong>{name}:</strong>', fn)
        self.assertIn("A rating from the signs above, not proof", fn)
        self.assertNotIn("%", fn.replace("minmax(", ""))  # no percentage anywhere in the section

    def test_the_last_question_follows_the_signs(self):
        fn = self.job[self.job.index("function renderGhost"):self.job.index("function renderFit")]
        self.assertIn("s.code === 'evergreen_wording' || s.code === 'ad_signs'", fn)
        self.assertIn("building a list of candidates", fn)
        self.assertIn("Has the closing date or the number of openings changed?", fn)

    def test_why_summary_is_a_44px_target_on_touch_screens(self):
        start = self.css.index("@media (pointer: coarse) {\n")
        coarse = self.css[start:self.css.index("\n}", start)]  # (the whole media block)
        rule = re.search(r"[^{}]*\.ghost-why summary[^{}]*\{([^}]*)\}", coarse)
        self.assertIsNotNone(rule)
        self.assertIn("min-height: 44px", rule.group(1))
        link = re.search(r"a\.ghost-dot \{([^}]*)\}", coarse)  # the job page's link to the explanation
        self.assertIsNotNone(link)
        self.assertIn("min-height: 44px", link.group(1))

    def test_no_inline_styles_or_handlers_and_meaning_is_not_colour_alone(self):
        for src in (self.common[self.common.index("const GHOST_STRENGTH"):self.common.index("function modeOf")],
                    self.job[self.job.index("/* ---------- is this a real vacancy?"):self.job.index("function renderFit")]):
            self.assertNotRegex(src, r'style=|\bon[a-z]+=')
        self.assertIn(".ghost-dot", self.css)
        self.assertIn(".ghost-signs", self.css)
        self.assertIn(".ghost-checks", self.css)


class CompanyResearchButtonsTest(unittest.TestCase):
    """Open-source spec §1.9: "News/Controversies" wherever shown, and a "Research again" button per section that
    says which section it is for, beside that section's own date."""

    @classmethod
    def setUpClass(cls):
        read = lambda n: (FRONTEND / n).read_text(encoding="utf-8")  # noqa: E731
        cls.common, cls.app, cls.job, cls.css = read("common.js"), read("app.js"), read("job.js"), read("styles.css")

    def test_the_section_is_called_news_controversies(self):
        profile = self.common[self.common.index("function companyProfileHtml"):self.common.index("function officeHtml")]
        self.assertIn("<dt>News/Controversies</dt>", profile)
        self.assertNotIn("<dt>Controversies</dt>", profile)
        self.assertIn("news: 'News/Controversies'", self.common)
        self.assertIn("News/Controversies item(s) found", self.app)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("News/Controversies", readme)

    def test_each_section_has_its_own_button_and_date(self):
        helper = self.common[self.common.index("const RESEARCH_SECTION_NAME"):self.common.index("const RESEARCH_PART_LABEL")]
        # The visible words are the start of the name (WCAG 2.5.3); the hidden rest says which section.
        self.assertIn('data-research-part="${part}">Research again<span class="visually-hidden">: ${RESEARCH_SECTION_NAME[part]} only</span></button>', helper)
        self.assertIn("${sectionDateNote(iso)}", helper)
        self.assertNotRegex(helper, r'style=|\bon[a-z]+=')
        self.assertIn("sectionResearch('sentiment', p.sentiment_researched_at)", self.common)
        self.assertIn("sectionResearch('news', p.news_researched_at)", self.common)
        self.assertIn('data-research-part="all">${p ? \'Research the whole profile again\' : \'Research this company\'}', self.common)
        self.assertRegex(self.css, r"\.section-research \{[^}]*flex-wrap: wrap")

    def test_both_pages_send_the_part_the_button_is_for(self):
        self.assertIn("$$('#modal-html [data-research-part]')", self.app)
        self.assertIn("{ name: job.company, part: b.dataset.researchPart }", self.app)
        self.assertIn("$$('#job-company [data-research-part]').forEach((b) => b.addEventListener('click', () => researchCompany(b)));", self.job)
        self.assertIn("JSON.stringify({ name: job.company, part: button.dataset.researchPart })", self.job)


class DocumentsTabSmallChangesTest(unittest.TestCase):
    """Open-source spec §1.11: the two labels, and the warning about long documents that points to Create summary."""

    @classmethod
    def setUpClass(cls):
        read = lambda n: (FRONTEND / n).read_text(encoding="utf-8")  # noqa: E731
        cls.app, cls.index, cls.css = read("app.js"), read("index.html"), read("styles.css")

    def test_the_two_labels_and_the_stored_kinds(self):
        self.assertIn("['preferences', 'Preferences'], ['other', 'Other/supporting']", self.app)
        self.assertEqual(self.index.count('<option value="preferences">Preferences</option>'), 2)
        self.assertIn('<option value="other">Other/supporting</option>', self.index)
        self.assertIn('<option value="other" selected>Other/supporting</option>', self.index)
        for old in ("Preferences (private)", "Other (supporting)"):
            self.assertNotIn(old, self.app + self.index)
        self.assertIn("cover letters never see them", self.index)  # the hint stays

    def test_the_warning_counts_what_the_ai_gets_of_the_ticked_documents_and_points_to_create_summary(self):
        self.assertIn("const DOCS_WARN_CHARS = 50000;", self.app)
        fn = self.app[self.app.index("function renderDocsSize"):self.app.index("function redrawDoc")]
        self.assertIn("state.docs.list.filter((d) => d.use_for_scoring).reduce((n, d) => n + Number(d.sent_len || 0), 0)", fn)
        self.assertIn("total > DOCS_WARN_CHARS", fn)
        self.assertIn("Create summary", fn)
        self.assertIn("$('#summary-make')", fn)
        self.assertIn("btn.focus(", fn)
        self.assertIn("esc(text)", fn)
        self.assertIn("announce(", fn)  # said once when it appears
        self.assertNotRegex(fn, r'style=|\bon[a-z]+=')
        self.assertNotIn("disabled", fn)  # a warning: nothing is blocked
        # It sits just above the card it points to, and is redrawn whenever the list or a row is.
        self.assertLess(self.index.index('id="docs-size-warning"'), self.index.index('id="summary-make"'))
        self.assertEqual(self.app.count("renderDocsSize();"), 2)


class MainLinkPagesTest(unittest.TestCase):
    """Open-source spec §1.12: the card's and the job page's main link is job.link (the employer's own page when a
    merged copy is on it); the listed ad then joins "Also on", and the employer's copy can still be split off."""

    @classmethod
    def setUpClass(cls):
        read = lambda n: (FRONTEND / n).read_text(encoding="utf-8")  # noqa: E731
        cls.common, cls.app, cls.job = read("common.js"), read("app.js"), read("job.js")

    def test_cards_and_the_job_page_use_the_main_link(self):
        self.assertIn("return job.link?.url ? job.link : { url: job.url, source: job.source, copy_id: null };", self.common)
        card = self.app[self.app.index("function renderJobCard"):self.app.index("card.querySelector('input[type=\"checkbox\"]')")]
        self.assertIn('<h3 class="job-title" id="${titleId}"><a href="${esc(link.url)}"', card)
        self.assertIn("(job.also_on || []).filter((c) => c.id !== link.copy_id)", card)
        self.assertIn("...(link.copy_id ? [`<span class=\"also-item\"><a href=\"${esc(job.url)}\"", card)
        self.assertIn("link.copy_id ? ` ${split(link.copy_id, link.source)}` : ''", card)
        self.assertIn('<span class="visually-hidden"> the ${esc(sourceLabel(source))} ad from this card</span>', card)
        head = self.job[self.job.index("function renderHead"):self.job.index("/* ---------- is this a real vacancy?")]
        self.assertIn('<a href="${esc(link.url)}" target="_blank" rel="noopener">${esc(job.title || \'—\')}<span class="visually-hidden">: the ad on ${esc(sourceLabel(link.source))}</span>', head)
        self.assertIn("...(job.also_on || []).filter((c) => c.id !== link.copy_id)", head)


class AppliedDatePagesTest(unittest.TestCase):
    """Open-source spec §1.12: the card (and the job page) shows the applied date while the job is in Applied, and
    nothing when there is none; a status change updates it in place, keeping focus on the status menu."""

    @classmethod
    def setUpClass(cls):
        read = lambda n: (FRONTEND / n).read_text(encoding="utf-8")  # noqa: E731
        cls.common, cls.app, cls.job, cls.css = read("common.js"), read("app.js"), read("job.js"), read("styles.css")

    def test_shown_only_in_applied_with_a_date(self):
        fn = self.common[self.common.index("function appliedHtml"):]
        fn = fn[:fn.index("\n}\n") + 2]
        self.assertIn("job.status === 'applied' && job.applied_at", fn)
        self.assertIn("Applied ${esc(fmtDate(job.applied_at).split(',')[0])}", fn)
        self.assertIn(": ''", fn)  # nothing at all otherwise
        self.assertIn('<span aria-hidden="true">📨 </span>', fn)  # the icon is decoration; the word says it
        self.assertIn('<span class="applied-slot">${appliedHtml(job)}</span>', self.app)
        self.assertIn('<span class="applied-slot">${appliedHtml(job)}</span>', self.job)
        self.assertIn(".applied-slot:empty { display: none; }", self.css)

    def test_a_status_change_updates_it_in_place(self):
        update = self.app[self.app.index("async function updateJobStatus"):self.app.index("function showUploadAd")]
        self.assertIn("job.applied_at = res.applied_at ?? job.applied_at ?? null;", update)
        self.assertIn("slot.innerHTML = appliedHtml(job)", update)
        bulk = self.app[self.app.index("async function bulkUpdateStatus"):self.app.index("async function rescoreJob")]
        self.assertIn("j.applied_at = res.applied_at[String(j.id)]", bulk)
        self.assertIn("$('#job-head .applied-slot').innerHTML = appliedHtml(job);", self.job)


class BrowserAuditRegressionsTest(unittest.TestCase):
    """Things the browser audit (tools/a11y_audit.py: axe-core, reflow, text spacing, keyboard) found or watches, kept
    as static checks so they can't creep back without a browser."""

    @classmethod
    def setUpClass(cls):
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        cls.app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    def test_the_company_icon_is_named_by_hidden_text_not_an_aria_label(self):
        """WCAG 2.5.3 (axe label-content-name-mismatch): the button shows "ⓘ" and a count; an aria-label would not
        contain what is shown. The name is visually hidden text, and it says how many controversies there are."""
        start = self.app.index("function companyIcon")
        body = self.app[start:self.app.index("function showCompany")]
        button = re.search(r'return `<button[^`]*`', body).group(0)
        self.assertNotIn("aria-label", button)
        self.assertIn('<span class="visually-hidden">${esc(label)}</span>', button)
        self.assertIn("News/Controversies ${n === 1 ? 'item' : 'items'}", body)

    def test_the_jobs_tab_check_boxes_are_full_size_targets_on_a_phone(self):
        """WCAG 2.5.8: Show excluded / closed / hidden wrap into rows close together at 320 px."""
        self.assertRegex(self.css, r"@media \(max-width: 480px\) \{ \.check-field input \{ width: 24px; height: 24px; \} \}")
        for ident in ("filter-show-excluded", "filter-show-closed", "filter-show-hidden"):
            self.assertRegex((FRONTEND / "index.html").read_text(encoding="utf-8"),
                             r'<label class="field check-field"><input id="%s" type="checkbox" />' % ident)

    def test_theme_miniatures_grow_instead_of_cutting_text_off(self):
        """WCAG 1.4.12: with wider text spacing the miniature's text must not be clipped by a fixed-size box."""
        for rule in (".tp-title", ".tp-muted"):
            line = next(l for l in self.css.splitlines() if l.startswith(rule + " {"))
            self.assertNotIn("nowrap", line)
            self.assertNotIn("overflow: hidden", line)
        self.assertRegex(self.css, r"\.theme-preview \{[^}]*min-height: 92px")
        self.assertNotRegex(self.css, r"\.tp-card \{[^}]*overflow: hidden")
        # (the audit's text-spacing check found these: the page part kept its content's height, and the row of
        # badge, score and button wrapped instead of running out of the narrow box)
        self.assertRegex(self.css, r"\.tp-page \{ flex: 1 0 auto;")
        self.assertRegex(self.css, r"\.tp-row \{[^}]*flex-wrap: wrap")
        self.assertRegex(self.css, r"\.tp-badge, \.tp-score, \.tp-btn \{ overflow-wrap: anywhere; \}")

    def test_no_orientation_lock(self):
        """WCAG 1.3.4: nothing may work in one orientation only."""
        for name in ("styles.css", "themes.css"):
            self.assertNotRegex((FRONTEND / name).read_text(encoding="utf-8"), r"orientation\s*:", name)
        for page in PAGES:
            self.assertNotIn("orientation", (FRONTEND / page).read_text(encoding="utf-8"), page)

    def test_every_dialog_is_labelled_and_closes_with_escape(self):
        """Every <dialog> is a native one (Escape closes it, focus is kept inside) with an accessible name."""
        for page in PAGES:
            html = (FRONTEND / page).read_text(encoding="utf-8")
            for tag in re.findall(r"<dialog\b[^>]*>", html):
                self.assertRegex(tag, r'aria-labelledby="[\w-]+"', f"{page}: {tag}")
        common = (FRONTEND / "common.js").read_text(encoding="utf-8")
        self.assertIn("document.createElement('dialog')", common)
        self.assertRegex(common, r"dlg\.setAttribute\('aria-labelledby'|aria-labelledby")

    def test_the_audit_tool_is_a_standalone_developer_script(self):
        """tools/a11y_audit.py: documented in the README, importable only for its own sake, needing nothing new."""
        tool = ROOT / "tools" / "a11y_audit.py"
        self.assertTrue(tool.is_file())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("tools/a11y_audit.py", readme)
        for folder in ("backend", "tests", "installer"):
            for src in (ROOT / folder).rglob("*.py"):
                if src.name == Path(__file__).name:
                    continue
                self.assertNotIn("a11y_audit", src.read_text(encoding="utf-8"), f"{src} must not use the audit tool")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("axe", requirements, "axe-core is a developer-only npm install, not an app dependency")
        done = subprocess.run([sys.executable, str(tool), "--list"], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        for group in ("index", "auth", "wizard", "job", "settings", "keyboard"):
            self.assertIn(group + ":", done.stdout)
        source = tool.read_text(encoding="utf-8")
        # the audit must reach the ghost-job pages (it fails a run that can't), and ignore a visually hidden clip
        self.assertIn("#job-ghost-section:not([hidden]) .ghost-check.found", source)
        self.assertIn("r.width <= 2 || r.height <= 2", source)


class CommuteLinksMarkupTest(unittest.TestCase):
    """Search setup > Commute (the starting points) and the job page's office list and Google Maps links (Phase C):
    labelled controls, results said in the live region, nothing that only works with a mouse, and layouts that wrap
    at 320 px."""

    @classmethod
    def setUpClass(cls):
        read = lambda n: (FRONTEND / n).read_text(encoding="utf-8")  # noqa: E731
        cls.index, cls.app, cls.job, cls.css = read("index.html"), read("app.js"), read("job.js"), read("styles.css")
        cls.card = cls.index[cls.index.index('<section id="starts-card"'):]
        cls.card = cls.card[:cls.card.index("</section>")]

    def test_the_add_form_is_a_labelled_group(self):
        self.assertIn('aria-labelledby="starts-title"', self.card)
        self.assertIn("<summary>Add a starting point</summary>", self.card)
        for ident in ("start-from", "start-place", "start-label", "start-use"):
            self.assertIn(f'<label class="p-label" for="{ident}">', self.card, ident)
        self.assertIn('aria-describedby="start-place-hint"', self.card)
        self.assertIn('<option value="transit">Public transport</option>', self.card)  # words, not icons

    def test_each_row_names_its_controls_and_changes_are_announced(self):
        block = self.app[self.app.index("function renderStarts()"):self.app.index("function showStartPlace()")]
        self.assertIn('<span class="u-sr">Used for, ${esc(name)}</span>', block)
        self.assertIn('Remove<span class="u-sr"> ${esc(name)}</span>', block)
        self.assertIn("announce(`Removed the starting point", block)
        self.assertIn("focusStartFrom(); // the row with the button is gone", block)
        adding = self.app[self.app.index("function initStarts()"):]
        self.assertIn("setFieldError($('#start-place'), 'Type an address, a station or a stop, or choose a pin under From.')",
                      adding)
        self.assertIn("announce(`Added the starting point", adding)

    def test_rows_and_offices_wrap_on_a_phone(self):
        # the starting points are a table whose rows become stacked blocks on a phone (.tab-stack-table)
        self.assertIn('<table class="p-table tab-stack-table">', self.index[self.index.index('id="starts-table-wrap"'):])
        self.assertRegex(self.css, r"\.tab-stack-table, \.tab-stack-table tbody, \.tab-stack-table tr, \.tab-stack-table th, "
                                   r"\.tab-stack-table td \{ display: block; \}")
        self.assertRegex(self.css, r"\.tab-stack-table th, \.tab-stack-table td \{[^}]*overflow-wrap: anywhere")
        self.assertRegex(self.css, r"\.jp-office__choice \{[^}]*overflow-wrap: anywhere")
        self.assertRegex(self.css, r"\.jp-directions a, \.job-page \.jp-commute-list a \{[^}]*overflow-wrap: anywhere")

    def test_the_office_list_is_a_group_of_radios_with_another_address(self):
        block = self.job[self.job.index("function officeListHtml"):self.job.index("function renderOffice")]
        self.assertIn('<fieldset class="jp-office__list">', block)
        self.assertIn('<legend class="p-form-help-text" id="office-cands-intro">', block)
        self.assertIn('<input class="p-radio" type="radio" name="office-choice" value="${i}" ${pickedIndex === i ? \'checked\' : \'\'} />', block)
        self.assertIn("${esc(o.address)}", block)  # the address is shown, not only a hover text
        self.assertIn('value="other"', block)
        self.assertIn("Another address", block)
        office = self.job[self.job.index("function renderOffice"):self.job.index("function renderThere")]
        self.assertIn('<label class="p-label" for="office-input">Office address or suburb</label>', office)
        self.assertIn("if (r.value === 'other') { $('#office-input').focus(); return; }", office)
        self.assertIn('aria-pressed="${job.location_verdict === \'ok\'}"', office)
        self.assertIn('aria-pressed="${job.location_verdict === \'too_far\'}"', office)
        self.assertRegex(self.css, r"\.jp-verdict \.p-button\[aria-pressed=\"true\"\] \{")

    def test_the_links_say_where_they_go_and_that_they_open_a_new_tab(self):
        block = self.job[self.job.index("function commuteLink"):self.job.index("function commuteBriefHtml")]
        self.assertIn("a.insertAdjacentHTML('beforeend', NEW_TAB);", block)
        self.assertIn("`${TRAVEL_TEXT[mode] || 'Directions'} from ${start.label}`", block)
        # In the directions table the starting point is the row's header; the link keeps it as hidden text.
        self.assertIn('<span class="visually-hidden"> from ${esc(start.label)}, in Google Maps</span>', block)
        load = self.job[self.job.index("async function loadCommute"):self.job.index("/* ---------- The ad, annotated")]
        self.assertIn("table.setAttribute('aria-labelledby', 'commute-links-title');", load)
        self.assertIn('id="commute-links-title"', load)
        self.assertIn('<th scope="col">From</th>', load)
        self.assertIn("th.scope = 'row';", load)
        self.assertIn("td.textContent = 'Not set';", load)  # a missing way of travelling is said, not left blank


class TasksTrayTest(unittest.TestCase):
    """The tasks tray in the header (tasks-tray.js, open-source spec §1.13): a disclosure button with the number of AI
    tasks running and the list it opens, on the dashboard and the job page."""

    @classmethod
    def setUpClass(cls):
        cls.js = (FRONTEND / "tasks-tray.js").read_text(encoding="utf-8")
        cls.common = (FRONTEND / "common.js").read_text(encoding="utf-8")
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    def nodes(self, page, node_id):
        found = [n for n in page.nodes if n["attrs"].get("id") == node_id]
        self.assertEqual(len(found), 1, node_id)
        return found[0]

    def test_both_pages_have_the_button_and_its_panel_in_the_header(self):
        for name in ("index.html", "job.html"):
            page = parse(name)
            with self.subTest(page=name):
                btn = self.nodes(page, "tasks-btn")
                self.assertEqual(btn["tag"], "button")
                self.assertEqual(btn["attrs"].get("type"), "button")
                self.assertEqual(btn["attrs"].get("aria-expanded"), "false")
                self.assertEqual(btn["attrs"].get("aria-controls"), "tasks-panel")
                self.assertIn("hidden", self.nodes(page, "tasks-panel")["attrs"])
                self.assertEqual(self.nodes(page, "tasks-list")["tag"], "ul")
                self.assertEqual(self.nodes(page, "tasks-list")["attrs"].get("aria-labelledby"), "tasks-title")
                count = self.nodes(page, "tasks-count")
                self.assertEqual(page.nodes[count["parent"]]["attrs"].get("id"), "tasks-btn", "the number is in the button")
                self.assertNotIn("aria-label", btn["attrs"], "its name is its text")
                name_text = " ".join(btn["text"].split())
                self.assertTrue(name_text.startswith("AI tasks"), name_text)  # the visible words start its name (2.5.3)
                self.assertIn("running", name_text)
                src = (FRONTEND / name).read_text(encoding="utf-8")
                header = src[src.index("<header"):src.index("</header>")]
                self.assertIn('id="tasks-menu"', header)
                self.assertLess(src.index('src="./common.js"'), src.index('src="./tasks-tray.js"'))
                page_js = "./app.js" if name == "index.html" else "./job.js"
                self.assertLess(src.index('src="./tasks-tray.js"'), src.index(f'src="{page_js}"'),
                                "the page fills in taskNames, which tasks-tray.js defines")

    def test_count_changes_are_announced_politely_and_the_count_is_written_once(self):
        block = self.js[self.js.index("function renderTasksCount"):self.js.index("function scheduleTasks")]
        self.assertIn("setText(badge, String(count));", block)
        self.assertIn("announce(count ? `${count} AI task${count === 1 ? '' : 's'} running.` : 'No AI tasks running.');", block)
        self.assertNotIn("announce(count, true)", block)
        self.assertNotRegex(block, r"announce\([^)]*,\s*true\)", "a count change is not an alert")
        self.assertIn("tray.count !== null && tray.count !== count", block, "nothing is said for the first count")
        self.assertNotIn("aria-live", self.js, "the list itself is not a live region (common.js says the count)")

    def test_the_panel_opens_and_closes_like_the_account_menu(self):
        block = self.js[self.js.index("function initTasksTray"):]
        self.assertIn("btn.setAttribute('aria-expanded', String(yes));", block)
        self.assertIn("if (e.key === 'Escape') { e.preventDefault(); open(false); btn.focus(); }", block)
        self.assertIn("!e.target.closest('#tasks-menu')", block)
        self.assertIn("menu.addEventListener('focusout'", block)

    def test_redrawing_keeps_the_focus_and_names_go_in_escaped(self):
        self.assertIn("if (tray.drawn !== html)", self.js)
        self.assertIn("if (again) again.focus();", self.js)
        self.assertIn("esc(job.title)", self.js)
        self.assertIn("esc(job.company)", self.js)
        self.assertIn("esc(name)", self.js)
        self.assertNotRegex(self.js, r"style=|\bon[a-z]+=")

    def test_it_says_what_for_which_since_when_and_how_far_and_cannot_cancel(self):
        for words in ("since ${esc(since)}", "${p.done} of ${p.total} done", ": 'a job'", "for a company",
                      "for a document", "(another workspace)"):
            self.assertIn(words, self.js)
        self.assertNotIn("cancel", self.js.lower().replace("nothing here cancels a task", ""))
        self.assertNotIn("<button", self.js, "no control in the list: there is nothing to cancel (§1.16)")

    def test_it_comes_from_the_server_and_looks_again_after_a_change(self):
        self.assertIn("api('/api/tasks')", self.js)
        self.assertIn("document.dispatchEvent(new Event('jobhunt:api-write'));", self.common)
        self.assertIn("document.addEventListener('jobhunt:api-write'", self.js)
        self.assertIn("document.visibilityState !== 'visible'", self.js)

    def test_theme_tokens_and_phones(self):
        block = self.css[self.css.index("/* The tasks tray"):self.css.index("/* Sign-in pages")]
        self.assertIn(".tasks-count.is-busy { background: var(--accent); color: var(--on-accent);", block)
        self.assertIn("background: var(--panel); color: var(--text); border: 1px solid var(--border-strong);", block)
        self.assertIn(".tasks-panel { position: static; margin-top: 6px; width: auto; }", block, "in the flow on a phone")
        self.assertIn("overflow-wrap: anywhere", block, "long job titles wrap at 320 px")
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,8}\b", "theme tokens only")


class FirstRunStepsTest(unittest.TestCase):
    """The first run's new steps (open-source spec §1.5): where the data lives (first-run.html), the AI provider
    (the walkthrough's first step) and the offer of the browser for JavaScript pages."""

    def node(self, page, ident):
        return next(n for n in page.nodes if n["attrs"].get("id") == ident)

    def test_the_folder_field_is_labelled_with_its_hint_and_its_check_is_announced(self):
        page = parse("first-run.html")
        field = self.node(page, "data-folder")
        self.assertEqual(field["attrs"].get("type"), "text")
        self.assertIn("data-folder-hint", field["attrs"].get("aria-describedby", ""))
        self.assertIn("data-folder-check", field["attrs"].get("aria-describedby", ""))
        self.assertTrue(any(n["tag"] == "label" and n["attrs"].get("for") == "data-folder" for n in page.nodes))
        self.assertEqual(self.node(page, "data-folder-check")["attrs"].get("role"), "status")  # 4.1.3
        token = self.node(page, "folder-token")
        self.assertEqual(token["attrs"].get("aria-describedby"), "folder-token-hint")
        for button in ("folder-check", "folder-submit"):
            self.assertIn(self.node(page, button)["attrs"].get("type"), ("button", "submit"))

    def test_the_folder_step_says_problems_in_the_error_summary_and_moves_on_by_itself(self):
        js = (FRONTEND / "auth.js").read_text(encoding="utf-8")
        self.assertIn("showErrors([{ field: 'data-folder'", js)  # under the field and in the summary (3.3.1)
        self.assertIn("announce('The data folder is ready. Now create your account.')", js)
        self.assertIn("waitForRestart", js)

    def test_the_ai_step_labels_every_field_and_can_be_skipped(self):
        js = (FRONTEND / "welcome.js").read_text(encoding="utf-8")
        start = js.index("  ai: {")
        step = js[start:js.index("  theme: {", start)]
        for ident, hint in (("wz-ai-provider", "wz-ai-note"), ("wz-ai-url", "wz-ai-url-hint"),
                            ("wz-ai-model", "wz-ai-model-hint"), ("wz-ai-key", "wz-ai-key-hint")):
            self.assertIn(f'<label class="p-label" for="{ident}">', step, ident)
            self.assertIn(f'id="{ident}"', step)
            self.assertIn(f'aria-describedby="{hint}"', step, ident)
            self.assertIn(f'id="{hint}"', step, ident)
        self.assertIn("skippable: true", step)
        self.assertIn('type="password"', step)
        self.assertIn('rel="noopener noreferrer"', js)

    def test_the_browser_offer_is_a_headed_region_with_real_buttons(self):
        page = parse("index.html")
        offer = self.node(page, "browser-offer")
        self.assertEqual(offer["tag"], "section")
        self.assertEqual(offer["attrs"].get("aria-labelledby"), "browser-offer-title")
        for button in ("browser-offer-yes", "browser-offer-no", "browser-download"):
            self.assertEqual(self.node(page, button)["attrs"].get("type"), "button")
        self.assertEqual(self.node(page, "browser-offer-status")["attrs"].get("role"), "status")
        self.assertEqual(self.node(page, "browser-state")["attrs"].get("role"), "status")
        js = (FRONTEND / "browser.js").read_text(encoding="utf-8")
        self.assertIn("$('#source-url')?.focus()", js, "Not now hides the offer: focus goes back to the form (2.4.3)")


if __name__ == "__main__":
    unittest.main()


class TabJumpListsTest(unittest.TestCase):
    """Search setup, Documents and Settings each have a labelled jump list whose links lead to cards that exist, with
    the card in view marked aria-current (app.js initJumpLists); at phone width the list is a strip."""

    @classmethod
    def setUpClass(cls):
        cls.html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        cls.app = (FRONTEND / "app.js").read_text(encoding="utf-8")
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    def test_each_list_is_a_labelled_nav_and_every_link_has_a_target(self):
        navs = re.findall(r'<nav class="tab-jump" aria-label="([^"]+)">(.*?)</nav>', self.html, re.S)
        self.assertEqual(len(navs), 3)
        self.assertEqual(len({label for label, _ in navs}), 3, "the labels tell the lists apart")
        for label, body in navs:
            links = re.findall(r'<a class="p-side-navigation__link" href="#([\w-]+)"', body)
            self.assertGreaterEqual(len(links), 4, label)
            for target in links:
                self.assertIn(f'id="{target}"', self.html, f"{label}: #{target} is not on the page")
            self.assertEqual(body.count('aria-current="true"'), 1, label)

    def test_the_card_in_view_is_marked_and_a_link_moves_focus_to_its_heading(self):
        self.assertIn("IntersectionObserver", self.app)
        self.assertIn("setAttribute('aria-current', 'true')", self.app)
        self.assertIn("heading.focus(", self.app)

    def test_on_a_phone_the_list_is_a_scrolling_strip(self):
        strip = self.css[self.css.index("Settings and the others: the jump list"):]
        self.assertRegex(strip, r"\.tab-jump \.p-side-navigation[^{]*\{[^}]*flex-direction: row; overflow-x: auto")


class CsFreeTabsTest(unittest.TestCase):
    """The reworked tabs and pages add no inline styles or handlers (Content-Security-Policy)."""

    def test_no_style_attributes_or_event_handlers(self):
        for name in ("index.html", "login.html", "recover.html", "first-run.html", "welcome.html"):
            html = (FRONTEND / name).read_text(encoding="utf-8")
            self.assertNotRegex(html, r'\sstyle="', name)
            self.assertNotRegex(html, r'\son[a-z]+="', name)
class JobPageTabsTest(unittest.TestCase):
    """The job page (job.html, job.js): a sticky summary over in-page tabs (WAI-ARIA tabs: one tab list, arrow keys,
    the hash remembers the tab), gaps before matches wherever requirements are listed, the ad annotated with word
    labels that link both ways, the chat shared with the dashboard through chat.js, and a bottom action bar on phones."""

    @classmethod
    def setUpClass(cls):
        read = lambda n: (FRONTEND / n).read_text(encoding="utf-8")  # noqa: E731
        cls.html, cls.js, cls.chat, cls.app = read("job.html"), read("job.js"), read("chat.js"), read("app.js")
        cls.index, cls.css = read("index.html"), read("styles.css")
        cls.page = parse("job.html")
        cls.tabs = [n for n in cls.page.nodes if n["attrs"].get("role") == "tab"]
        cls.panels = {n["attrs"]["id"]: n for n in cls.page.nodes if n["attrs"].get("role") == "tabpanel"}

    def test_one_tab_list_of_six_tabs_each_controlling_a_panel_that_names_it(self):
        lists = [n for n in self.page.nodes if n["attrs"].get("role") == "tablist"]
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["attrs"].get("aria-label"), "Sections of this job")
        self.assertEqual([" ".join(t["text"].split()) for t in self.tabs], ["Overview", "Fit", "Company", "Commute", "The ad", "Chats"])
        for tab in self.tabs:
            a = tab["attrs"]
            self.assertEqual(tab["tag"], "button")
            self.assertEqual(a.get("type"), "button")
            panel = self.panels.get(a.get("aria-controls"))
            self.assertIsNotNone(panel, f"{a['id']} controls no panel")
            self.assertEqual(panel["attrs"].get("aria-labelledby"), a["id"])
            self.assertEqual(panel["attrs"].get("tabindex"), "-1")  # focus can land in a panel opened from a link
            selected = a.get("aria-selected") == "true"
            self.assertEqual(selected, a["id"] == "tab-overview")
            self.assertEqual("hidden" in panel["attrs"], not selected)
            if not selected:
                self.assertEqual(a.get("tabindex"), "-1")  # one tab stop: the arrow keys move between tabs
        self.assertEqual(len(self.panels), 6)

    def test_arrow_keys_and_the_hash(self):
        tabs = self.js[self.js.index("function initTabs()"):self.js.index("/* ---------- Overview")]
        self.assertIn("const keys = { ArrowRight: 1, ArrowLeft: -1, Home: 0, End: 0 };", tabs)
        self.assertIn("window.addEventListener('hashchange', () => applyHash());", tabs)
        select = self.js[self.js.index("function selectTab("):self.js.index("function tabOf(")]
        self.assertIn("tab.setAttribute('aria-selected', String(on));", select)
        self.assertIn("tab.tabIndex = on ? 0 : -1;", select)
        self.assertIn("panel.hidden = !on;", select)
        self.assertIn("history.replaceState(null, '', `#${name}`)", select)
        self.assertIn("if (focus === 'tab') byId(`tab-${name}`).focus();", select)
        self.assertIn("else if (focus === 'panel') byId(`panel-${name}`).focus({ preventScroll: true });", select)
        hash_ = self.js[self.js.index("function applyHash("):self.js.index("function initTabs()")]
        self.assertIn("if (TABS.includes(id)) { selectTab(id, { hash: false, focus }); return; }", hash_)
        self.assertIn("const tab = tabOf(el);", hash_)  # an id inside a tab opens that tab
        self.assertIn("el.focus({ preventScroll: true });", hash_)
        self.assertIn("applyHash({ focus: null });", self.js)  # at load: no focus is moved

    def test_gaps_come_before_matches_everywhere(self):
        groups = self.js[self.js.index("function requirementGroups"):self.js.index("/* The ghost-job rating as a label")]
        self.assertIn("gaps: [...unmatched.sort(mustFirst), ...notes], matched:", groups)
        overview = self.js[self.js.index("function renderVerdict"):self.js.index("/* ---------- is this a real vacancy?")]
        self.assertLess(overview.index("Gaps: what to prepare for"), overview.index("Matched: what to lean on"))
        fit = self.js[self.js.index("function renderFit()"):self.js.index("function renderFitHow")]
        self.assertLess(fit.index('id="gaps-title"'), fit.index('id="matched-title"'))
        glance = self.js[self.js.index("function renderGlance"):self.js.index("/* Upload the full ad")]
        self.assertLess(glance.index("Gaps: what to prepare for"), glance.index("Matched: what to lean on"))
        ad = self.js[self.js.index("function renderAd()"):self.js.index("function renderGlance")]
        self.assertIn("const ordered = [...gaps, ...matched];", ad)
        company = self.js[self.js.index("function renderCompany()"):self.js.index("function renderOtherJobs")]
        self.assertLess(company.index("Often criticised"), company.index("Often praised"))

    def test_the_ad_labels_are_links_with_words_anchored_both_ways(self):
        lab = self.js[self.js.index("function adLabelHtml"):self.js.index("function renderAd()")]
        self.assertIn("const words = `${kind === 'gap' ? 'Gap' : 'Matched'}${r.must_have === false ? ', nice to have' : ''}`;", lab)
        self.assertIn('<a class="p-status-label p-status-label--${kind === \'gap\' ? \'negative\' : \'positive\'} jp-ad-label" href="#jp-rail-r${r.i}"', lab)
        self.assertIn('<span class="visually-hidden">: ${esc(r.point)}. Go to it in Fit at a glance</span>', lab)
        ad = self.js[self.js.index("function renderAd()"):self.js.index("function renderGlance")]
        self.assertIn('<mark class="jp-mark ${cls}" id="jp-ad-r${rs[0].i}" tabindex="-1">', ad)
        self.assertIn("Not tied to one sentence: ", ad)  # a requirement no sentence matches: labelled under the heading
        glance = self.js[self.js.index("function renderGlance"):self.js.index("/* Upload the full ad")]
        self.assertIn('<li id="jp-rail-r${r.i}" class="u-row jp-glance__row"><a href="#jp-ad-r${r.i}">', glance)
        # The underline is a second cue only: the word label carries the meaning (and a mark is never colour alone).
        self.assertRegex(self.css, r"\.jp-mark--gap \{[^}]*text-decoration-color: var\(--danger\)")

    def test_the_summary_says_the_score_mode_and_rating_in_words(self):
        head = self.js[self.js.index("function renderHead()"):self.js.index("function initStatus()")]
        self.assertIn("fitLabel(job, { large: true })", head)
        self.assertIn('<span class="visually-hidden">Work mode: </span>${esc(modeWords(job))}', head)
        self.assertIn("ghostLabelHtml(job)", head)
        self.assertIn('<label class="u-sr" for="job-status">Application status</label>', self.html)
        self.assertIn('<h1 id="job-title" class="jp-summary__title">', self.html)
        self.assertEqual(self.html.count("<h1"), 1)
        # The More menu is a disclosure that Escape closes, returning focus to its button.
        more = self.js[self.js.index("function initMore()"):self.js.index("function closeMore()")]
        self.assertIn("btn.setAttribute('aria-expanded', String(yes));", more)
        self.assertIn("if (e.key === 'Escape') { e.preventDefault(); open(false); btn.focus(); }", more)
        self.assertIn('aria-expanded="false" aria-controls="job-more-menu"', self.html)

    def test_phones_get_a_bottom_action_bar_and_folded_rails(self):
        self.assertIn('<div id="job-bar" class="jp-bar" role="region" aria-label="Actions for this job" hidden></div>', self.html)
        place = self.js[self.js.index("function placeControls()"):self.js.index("/* The rails fold")]
        self.assertIn("if (phone.matches) { bar.appendChild(controls); bar.hidden = false; }", place)
        self.assertIn("const phone = window.matchMedia('(max-width: 719px)');", self.js)
        self.assertRegex(self.css, r"@media \(max-width: 719px\) \{[^@]*\.job-page \.jp-summary \{ position: static; \}")
        self.assertRegex(self.css, r"\.job-page \.jp-summary \{[^}]*position: sticky")
        self.assertRegex(self.css, r"\.job-page \.jp-tabs \.p-tabs__list \{[^}]*overflow-x: auto")
        folds = self.js[self.js.index("function setFolds()"):self.js.index("/* ---------- tabs")]
        self.assertIn("d.open = narrow.matches ? d.dataset.phoneOpen === 'true' : true;", folds)
        self.assertGreaterEqual(self.html.count('class="p-card jp-fold"'), 6)
        start = self.css.index("@media (pointer: coarse) {\n  .job-page .jp-fold__summary")
        self.assertIn("min-height: 44px", self.css[start:self.css.index("\n}", start)])

    def test_live_regions_and_headings_per_card(self):
        self.assertIn('<p id="company-status" class="u-small u-muted" role="status"></p>', self.html)
        self.assertIn('<span id="chat-status" class="muted" role="status"></span>', self.html)
        for ident in ("verdict-title", "next-title", "facts-title", "office-title", "directions-title", "ad-title", "chat-title"):
            self.assertIn(f'<h2 id="{ident}"', self.html)
        built = self.js[self.js.index("function renderFit()"):self.js.index("function renderOtherJobs")]
        for h2 in ('<h2 id="fit-title"', '<h2 id="gaps-title"', '<h2 id="matched-title"', '<h2 id="co-title"', '<h2 id="sentiment-title"', '<h2 id="news-title"'):
            self.assertIn(h2, built)

    def test_the_chat_is_one_module_for_both_pages(self):
        for page in (self.index, self.html):
            self.assertLess(page.index('src="./common.js"'), page.index('src="./chat.js"'))
        self.assertLess(self.index.index('src="./chat.js"'), self.index.index('src="./app.js"'))
        self.assertLess(self.html.index('src="./chat.js"'), self.html.index('src="./job.js"'))
        for fn in ("function openChat(", "function closeChat(", "function renderChatMessages(", "function sendChat(", "function initChat("):
            self.assertIn(fn, self.chat)
            self.assertNotIn(fn, self.app)
        self.assertIn("initChat({ findJob: (id) => state.jobs.find((j) => j.id === id), showJob: showJobCard, panel: true });", self.app)
        self.assertIn("initChat({ findJob: (id) => (id === job.id ? job : null) });", self.js)
        for ident in ("chat-title", "chat-threads", "chat-new", "chat-delete", "chat-messages", "chat-form", "chat-input", "chat-web", "chat-status", "chat-send"):
            for page in (self.index, self.html):
                self.assertIn(f'id="{ident}"', page, ident)
        for call in ("/api/chats?job_ids=", "`/api/chats/${id}`", "'/api/chats'", "`/api/chats/${c.threadId}/messages`", "{ method: 'DELETE' }"):
            self.assertIn(call, self.chat)
        self.assertIn('<input id="chat-web" class="p-switch" type="checkbox" role="switch" />', self.html)
        self.assertIn('<ul id="chat-thread-list" class="p-list jp-chat-threads" aria-label="Earlier chats about this job"></ul>', self.html)

    def test_no_inline_styles_handlers_or_colour_only_meaning(self):
        for name, src in (("job.js", self.js), ("chat.js", self.chat), ("job.html", self.html)):
            self.assertNotRegex(src, r"""\sstyle\s*=\s*["']""", name)
            self.assertNotRegex(src, r"""\son[a-z]+\s*=\s*["']""", name)
        self.assertIn("}Location OK</button>", self.js)  # the calls on the location are words
        self.assertIn("✓ </span>' : ''}Location OK", self.js)  # and the pressed one carries a tick in its text
class JobsTabViewsTest(unittest.TestCase):
    """The Jobs tab's Cards | Table | Board views, its Filters panel and the markup app.js writes for the table and
    the board (the design boards RecTable, RecBoard, RecPhoneList, RecPhoneBoard)."""

    @classmethod
    def setUpClass(cls):
        cls.index = parse("index.html")
        cls.src = (FRONTEND / "index.html").read_text(encoding="utf-8")
        cls.app = (FRONTEND / "app.js").read_text(encoding="utf-8")
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        cls.ids = {n["attrs"].get("id"): n for n in cls.index.nodes if n["attrs"].get("id")}

    def test_the_view_toggle_is_a_group_of_pressed_buttons_remembered_per_browser(self):
        group = self.ids["view-toggle"]
        self.assertEqual(group["attrs"].get("role"), "group")
        self.assertEqual(group["attrs"].get("aria-label"), "View")
        buttons = [n for n in self.index.nodes if n["parent"] == group["i"] and n["tag"] == "button"]
        self.assertEqual([b["attrs"].get("data-view") for b in buttons], ["cards", "table", "board"])
        self.assertEqual([b["attrs"].get("aria-pressed") for b in buttons], ["true", "false", "false"])
        self.assertEqual([b["text"].strip() for b in buttons], ["Cards", "Table", "Board"])
        self.assertIn("const VIEW_KEY = 'jobhunt.jobsView';", self.app)
        self.assertIn("localStorage.setItem(VIEW_KEY, view)", self.app)
        self.assertIn("b.textContent = 'List'", self.app, "on a phone the toggle reads List | Board")
        self.assertIn("window.matchMedia('(max-width: 720px)')", self.app)

    def test_the_filters_button_is_a_disclosure_with_a_count_and_the_old_filters_keep_their_ids(self):
        btn = self.ids["filters-toggle"]
        self.assertEqual(btn["attrs"].get("aria-expanded"), "false")
        self.assertEqual(btn["attrs"].get("aria-controls"), "filters-panel")
        panel = self.ids["filters-panel"]
        self.assertEqual(panel["tag"], "section")
        self.assertIn("hidden", panel["attrs"])
        self.assertEqual(panel["attrs"].get("aria-labelledby"), "filters-heading")
        for ident in ("filter-min-score", "score-output", "filter-mode", "filter-max-dist", "office-slack", "soften-field",
                      "filter-show-excluded", "filter-show-closed", "filter-show-hidden", "filters-clear", "filters-done"):
            with self.subTest(control=ident):
                self.assertIn("filters-panel", self.ancestor_ids(self.ids[ident]), f"{ident} sits in the Filters panel")
        for ident in ("filter-search", "sort-by", "status-chips", "select-all-shown", "jobs-count", "bulk-bar"):
            self.assertNotIn("filters-panel", self.ancestor_ids(self.ids[ident]), f"{ident} stays outside the panel")
        self.assertEqual(self.ids["filters-count"]["attrs"].get("class"), "p-status-label p-status-label--information")
        self.assertEqual(self.ids["filter-chips"]["attrs"].get("role"), "group")
        self.assertIn('class="p-chip p-chip--dismiss"', self.app)
        self.assertIn('aria-label="Remove the ${esc(a.name)} filter"', self.app)
        self.assertIn("function resetFilter(key)", self.app)

    def ancestor_ids(self, node):
        return [a["attrs"]["id"] for a in self.index.ancestors(node) if a["attrs"].get("id")]

    def test_the_run_strip_keeps_its_controls_and_the_three_views_have_a_box_each(self):
        for ident in ("search-run", "score-pending", "fill-info", "dupes-open", "run-status", "next-scheduled", "run-progress", "run-report",
                      "jobs-list", "jobs-table", "jobs-board", "jobs-empty", "jobs-error"):
            self.assertIn(ident, self.ids, ident)
        self.assertEqual(self.ids["run-status"]["attrs"].get("role"), "status")
        self.assertEqual(self.ids["jobs-count"]["attrs"].get("role"), "status")
        self.assertEqual(self.ids["run-progress"]["attrs"].get("role"), "progressbar")
        self.assertIn("hidden", self.ids["jobs-table"]["attrs"])
        self.assertIn("hidden", self.ids["jobs-board"]["attrs"])
        self.assertIn("p-table-wrap", self.ids["jobs-table"]["attrs"].get("class", ""), "the table scrolls sideways in its box, never the page")

    def test_the_table_is_a_real_table_with_a_caption_headers_and_sortable_columns(self):
        table = self.app[self.app.index("function renderTable"):self.app.index("function tableRowHtml")]
        self.assertIn('<table class="p-table jobs-table">', table)
        self.assertIn('<caption class="visually-hidden">Jobs, sorted by ${SORT_WORDS[state.filter.sort]', table)
        self.assertIn('<th scope="col"', table)
        self.assertIn('aria-sort="${dir}"', table)
        self.assertIn("$('#sort-by').value = b.dataset.sort", table, "the Sort menu stays in step with the column buttons")
        for col in ("Job", "Fit", "Mode", "Distance", "Salary", "Posted", "Status"):
            self.assertIn(f"'{col}'", table)
        row = self.app[self.app.index("function tableRowHtml"):self.app.index("function detailHtml")]
        self.assertIn('data-act="expand" aria-expanded="${open}" aria-controls="${moreId}"', row)
        self.assertIn('<tr id="${moreId}" class="jobs-table__detail', row, "the summary row is in the table, after its job")
        self.assertIn('<label class="visually-hidden" for="job-${job.id}-status">Application status of ${esc(name)}</label>', row)
        self.assertIn('href="./job.html?id=${job.id}&ws=${state.ws}"', row)
        detail = self.app[self.app.index("function detailHtml"):self.app.index("function toggleExpanded")]
        gaps, matched = detail.index("(fit.gaps || [])"), detail.index("filter((r) => r.matched)")
        self.assertLess(gaps, matched, "gaps come before matches")
        self.assertIn('aria-label="Requirements: gaps first, then matches"', detail)

    def test_the_board_has_a_column_a_status_with_a_heading_and_a_move_to_menu_on_every_card(self):
        board = self.app[self.app.index("function renderBoard"):self.app.index("async function moveJob")]
        self.assertIn("const BOARD_COLS = ['to_review', 'shortlisted', 'applied', 'interviewing'];", self.app)
        self.assertIn("const CLOSED_STATUSES = ['rejected', 'not_interested'];", self.app)
        self.assertIn('<section id="board-col-${s}" class="board-col', board)
        self.assertIn('<h3 id="board-col-${s}-h" class="board-col__head">', board)
        self.assertIn('data-act="closed-toggle" aria-expanded="${state.boardClosedOpen}" aria-controls="board-closed-list"', board)
        self.assertIn('<label class="visually-hidden" for="job-${job.id}-move">Move ${esc(name)} to</label>', board)
        self.assertIn('<select id="job-${job.id}-move" class="p-select" data-act="move">', board)
        self.assertIn('role="tablist" aria-label="Status columns"', board)
        self.assertIn("ArrowRight: i + 1", board)
        self.assertNotIn("draggable", board, "no drag and drop: Move to is the way")
        self.assertNotIn("dragstart", self.app)
        self.assertIn("$('#status-chips').hidden = board;", self.app, "the board's columns are the statuses")
        self.assertIn("board ? 'Order in columns' : 'Sort'", self.app)

    def test_the_header_folds_behind_a_menu_button_on_a_phone(self):
        btn = self.ids["header-menu-btn"]
        self.assertEqual(btn["attrs"].get("aria-expanded"), "false")
        self.assertEqual(btn["attrs"].get("aria-controls"), "header-meta")
        for ident in ("ws-select", "ws-new", "ws-rename", "ws-delete", "tasks-btn", "account-btn"):
            self.assertIn("header-meta", self.ancestor_ids(self.ids[ident]), ident)
        phone = self.css[self.css.index("@media (max-width: 720px) {", self.css.index("Phones (720 px and under)")):]
        self.assertIn(".header-menu-btn { display: inline-flex; }", phone[:phone.index("\n}\n")])
        self.assertIn(".header-meta.is-open { display: flex; }", phone[:phone.index("\n}\n")])
        self.assertIn(".header-menu-btn { display: none; }", self.css)

    def test_the_bulk_bar_docks_at_the_bottom_on_a_phone_and_the_table_stacks(self):
        phone = self.css[self.css.index("@media (max-width: 720px) {", self.css.index("Phones (720 px and under)")):]
        phone = phone[:phone.index("\n}\n")]
        self.assertIn(".bulk-bar { position: fixed; left: 0; right: 0; bottom: 0;", phone)
        self.assertIn(".jobs-table, .jobs-table tbody, .jobs-table tr, .jobs-table td { display: block; }", phone)
        self.assertIn(".board-col:not(.is-current) { display: none; }", phone)
        self.assertNotIn("inline", self.src[self.src.index('id="tab-jobs"'):self.src.index('id="tab-search"')].replace("inline-", ""))
        self.assertNotRegex(self.src, r'\sstyle="', "no style attributes (CSP)")
