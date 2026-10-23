"""Each page's scripts and style sheets fit together (no browser needed).

The pages load several classic scripts that share one global scope: a name declared at the top level of two of
them stops the second script at its first line (``const``, ``let``, ``class``) or quietly replaces the first
(``function``). A style sheet with a block left open swallows every rule after it into that block. Both happen
easily when separate pieces of work are merged, and neither shows in the other tests."""
import re
import unittest
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
TOP_LEVEL = re.compile(r"^(?:const|let|var|class|function|async function)\s+([A-Za-z_$][\w$]*)", re.M)


def scripts(page: str) -> list[str]:
    html = (FRONTEND / page).read_text(encoding="utf-8")
    return re.findall(r'<script src="\./([\w.-]+\.js)"', html)


def without_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


class PageScriptsTest(unittest.TestCase):
    def test_no_name_is_declared_twice_on_a_page(self):
        pages = sorted(p.name for p in FRONTEND.glob("*.html"))
        self.assertIn("index.html", pages)
        for page in pages:
            seen: dict[str, str] = {}
            with self.subTest(page=page):
                for name in scripts(page):
                    src = (FRONTEND / name).read_text(encoding="utf-8")
                    for ident in TOP_LEVEL.findall(src):
                        self.assertNotIn(ident, seen, f"{page}: {ident} is declared in both {seen.get(ident)} and {name}")
                        seen[ident] = name

    def test_every_script_a_page_loads_exists(self):
        for page in sorted(p.name for p in FRONTEND.glob("*.html")):
            for name in scripts(page):
                with self.subTest(page=page, script=name):
                    self.assertTrue((FRONTEND / name).is_file())

    def test_the_cover_letter_window_is_shared_by_both_pages(self):
        for page in ("index.html", "job.html"):
            with self.subTest(page=page):
                self.assertIn("letter.js", scripts(page))
        for name in ("app.js", "job.js"):
            src = (FRONTEND / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertIn("openLetterWindow(job", src)
                self.assertNotIn('id="cl-text"', src)  # the window's markup lives in letter.js only


class StyleSheetsTest(unittest.TestCase):
    def test_every_block_is_closed(self):
        for name in ("styles.css", "themes.css"):
            css = without_comments((FRONTEND / name).read_text(encoding="utf-8"))
            depth = 0
            for number, line in enumerate(css.splitlines(), 1):
                depth += line.count("{") - line.count("}")
                with self.subTest(sheet=name, line=number):
                    self.assertGreaterEqual(depth, 0, f"{name}:{number}: a }} closes nothing")
            with self.subTest(sheet=name):
                self.assertEqual(depth, 0, f"{name}: {depth} block(s) left open")

    def test_top_level_rules_are_not_inside_a_media_query(self):
        """A section heading comment (/* ===== ... */) starts at the top level: nothing before it is left open."""
        css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        for m in re.finditer(r"^/\* =+", css, re.M):
            before = without_comments(css[:m.start()])
            line = css.count("\n", 0, m.start()) + 1
            with self.subTest(line=line):
                self.assertEqual(before.count("{"), before.count("}"), f"styles.css:{line}: a block is still open here")


if __name__ == "__main__":
    unittest.main()
