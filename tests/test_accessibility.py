"""Accessibility regressions that need no browser (WCAG 2.2 AA).

- Every theme's colour pairs that the app uses together meet AA (tools/theme_contrast.py).
- The pages' HTML: a language, a viewport that allows zoom, a title, a skip link, a main landmark, a name for
  every form control, button and link, and no references to ids that don't exist.
- The markup app.js and job.js build: every control they write has a label or a name.

The full check (axe-core in every theme at 320 to 1280 px, keyboard, focus and reflow) needs a browser; the
README's Accessibility section says how it was run.
"""
from __future__ import annotations

import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
sys.path.insert(0, str(ROOT / "tools"))
import theme_contrast  # noqa: E402

PAGES = ("index.html", "job.html")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
FOCUSABLE = {"a", "button", "input", "select", "textarea", "summary"}


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
                self.assertIn(theme, themes, f"the Theme menu offers {theme}, but themes.css has no block for it")
        slots = set(themes["dark"])
        css = (FRONTEND / "themes.css").read_text(encoding="utf-8")
        for theme in themes:
            block = re.search(r'\[data-theme="%s"\][^{]*\{([^}]*)\}' % re.escape(theme), css).group(1)
            own = set(re.findall(r"--([\w-]+)\s*:", block))
            # --map-ink and the type settings are shared from :root; every colour slot is set per theme.
            missing = {t for t in slots - own if t not in ("map-ink", "font-ui", "leading", "tracking", "word-spacing")}
            self.assertEqual(missing, set(), f"{theme} doesn't set {sorted(missing)}")

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
        for name in ("app.js", "job.js", "common.js"):
            problems += self.check_file(name)
        self.assertEqual(problems, [], "controls without a label or name:\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
