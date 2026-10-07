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

PAGES = ("index.html", "job.html", "login.html", "first-run.html", "recover.html", "welcome.html", "admin.html",
         "signup.html", "reset-password.html", "verify-email.html", "billing.html")
AUTH_PAGES = ("login.html", "first-run.html", "recover.html", "signup.html", "reset-password.html", "verify-email.html")
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
                self.assertIn(theme, themes, f"the theme picker offers {theme}, but themes.css has no block for it")
        for theme in themes:
            self.assertIn(theme, offered, f"themes.css defines {theme}, but THEMES in common.js doesn't offer it")
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
        for name in ("app.js", "job.js", "common.js", "auth.js", "welcome.js", "account.js", "admin.js", "dialogs.js",
                     "signup.js", "server-settings.js", "export.js", "billing.js", "stripe-settings.js",
                     "free-searches.js"):
            problems += self.check_file(name)
        self.assertEqual(problems, [], "controls without a label or name:\n" + "\n".join(problems))


class AuthPagesTest(unittest.TestCase):
    """The sign-in pages (WCAG 2.2 3.3.8 accessible authentication, 3.3.7 redundant entry, 3.2.6 consistent help,
    1.3.5 input purpose)."""

    def inputs(self, page):
        return {n["attrs"].get("id"): n["attrs"] for n in page.nodes if n["tag"] == "input"}

    def test_fields_say_what_they_are_for_password_managers(self):
        expected = {
            "login.html": {"email": ("email", "username"), "password": ("password", "current-password")},
            "first-run.html": {"email": ("email", "username"), "password": ("password", "new-password"),
                               "name": ("text", "name")},
            "recover.html": {"email": ("email", "username"), "password": ("password", "new-password"),
                             "code": ("text", "one-time-code")},
            "signup.html": {"email": ("email", "username"), "password": ("password", "new-password"),
                            "name": ("text", "name")},
        }
        for name, fields in expected.items():
            inputs = self.inputs(parse(name))
            for field, (kind, autocomplete) in fields.items():
                with self.subTest(page=name, field=field):
                    self.assertEqual(inputs[field].get("type"), kind)
                    self.assertEqual(inputs[field].get("autocomplete"), autocomplete)
            passwords = [i for i in inputs.values() if i.get("type") == "password"]
            self.assertEqual(len(passwords), 1, f"{name}: one password field, no 'confirm password' (3.3.7)")

    def test_paste_and_password_managers_are_never_blocked(self):
        for name in AUTH_PAGES + ("auth.js", "signup.js", "common.js", "account.js"):
            src = (FRONTEND / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotRegex(src, r"on(paste|copy|cut|drop)\s*=", "no handlers that block pasting")
                self.assertNotRegex(src, r"addEventListener\(\s*['\"](paste|copy|cut|drop)['\"]")
                self.assertNotIn("captcha", src.lower())
        for name in AUTH_PAGES:
            for attrs in self.inputs(parse(name)).values():
                if attrs.get("type") in ("email", "password"):
                    self.assertNotEqual(attrs.get("autocomplete"), "off", f"{name}: autocomplete=off on a sign-in field")
                    self.assertNotIn("data-lpignore", attrs)
                    self.assertNotIn("data-1p-ignore", attrs)

    def test_show_password_is_a_toggle_button(self):
        for name in AUTH_PAGES:
            if name == "verify-email.html":
                continue  # no password on it
            page = parse(name)
            toggles = [n for n in page.nodes if n["tag"] == "button" and "pw-toggle" in n["attrs"].get("class", "")]
            with self.subTest(page=name):
                self.assertEqual(len(toggles), 1)
                self.assertEqual(toggles[0]["attrs"].get("aria-pressed"), "false")
                self.assertEqual(toggles[0]["attrs"].get("aria-controls"), "password")
                self.assertEqual(toggles[0]["attrs"].get("type"), "button")

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
        for name in ("index.html", "job.html", "admin.html", "billing.html"):
            page = parse(name)
            btn = next(n for n in page.nodes if n["attrs"].get("id") == "account-btn")
            with self.subTest(page=name):
                self.assertEqual(btn["attrs"].get("aria-expanded"), "false")
                self.assertEqual(btn["attrs"].get("aria-controls"), "account-panel")
                self.assertTrue(any(n["attrs"].get("id") == "account-signout" for n in page.nodes))


class CodeStepAndRecoveryTest(unittest.TestCase):
    """Two-step sign-in on login.html, the recovery-code path (recover.html) and "Trouble signing in?"."""

    @classmethod
    def setUpClass(cls):
        cls.login = parse("login.html")
        cls.recover = parse("recover.html")
        cls.js = (FRONTEND / "auth.js").read_text(encoding="utf-8")

    def by_id(self, page, ident):
        return next(n for n in page.nodes if n["attrs"].get("id") == ident)

    def test_the_code_field_is_a_one_time_code(self):
        code = self.by_id(self.login, "code")["attrs"]
        self.assertEqual(code.get("autocomplete"), "one-time-code")
        self.assertEqual(code.get("inputmode"), "numeric")
        self.assertEqual(code.get("type"), "text", "type=number would drop spaces and hyphens")
        self.assertNotIn("maxlength", code, "spaces and hyphens are allowed (3.3.8)")
        self.assertIn("code-hint", code.get("aria-describedby", ""))

    def test_the_code_step_keeps_password_managers_working(self):
        user = self.by_id(self.login, "mfa-username")["attrs"]
        self.assertEqual(user.get("autocomplete"), "username")
        self.assertEqual(user.get("aria-hidden"), "true")
        self.assertTrue(user.get("aria-label"))
        form = self.by_id(self.login, "mfa-form")
        self.assertIn("hidden", form["attrs"], "the code step shows only after the password")
        self.assertIn("novalidate", form["attrs"])

    def test_a_recovery_code_can_be_used_instead(self):
        switch = self.by_id(self.login, "mfa-switch")
        self.assertEqual((switch["tag"], switch["attrs"].get("type")), ("button", "button"))
        self.assertIn("Use a recovery code instead", switch["text"])
        self.assertIn("hidden", self.by_id(self.login, "recovery-field")["attrs"])
        self.assertIn("Start again", self.by_id(self.login, "mfa-restart")["text"], "a way back to the password step")
        self.assertIn("state.data.mfa_pending", self.js, "a reload shows the code step, not the password")
        self.assertIn("mfa_expired", self.js, "an expired half-open sign-in goes back to the password")

    def test_the_code_step_moves_focus_and_says_where_you_are(self):
        self.assertIn("document.title = 'Enter your code", self.js)
        self.assertIn(".focus()", self.js.split("function setCodeMode")[1].split("async function initLogin")[0])
        self.assertIn("showErrors([{ field, message", self.js, "an empty code is an error in the summary")

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
        second = self.by_id(self.recover, "code-field")
        self.assertIn("hidden", second["attrs"], "asked for only when the server says the account needs it")
        self.assertIn("Code from your authenticator app, or a second recovery code", self.by_id(self.recover, "code-field")["text"])
        self.assertEqual(self.by_id(self.recover, "recovery-code")["attrs"].get("autocomplete"), "off")
        self.assertIn("second_factor_required", self.js)
        self.assertIn("showRecovered", self.js)
        done = self.by_id(self.recover, "recover-done")
        self.assertIn("hidden", done["attrs"])

    def test_buttons_that_look_like_links_are_big_enough(self):
        css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        self.assertIn("min-height: 44px", re.search(r"\.btn-link \{([^}]*)\}", css).group(1))


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

    def test_its_ids_do_not_clash_with_the_admin_pages_own_dialog(self):
        # admin.html has a static confirm dialog (confirm-title, confirm-cancel, confirm-text): a duplicate id would
        # give this dialog the wrong accessible name
        admin = (FRONTEND / "admin.html").read_text(encoding="utf-8")
        ids = set(re.findall(r'\bid="(confirm-[\w-]+)"', self.body))
        self.assertTrue({"confirm-pw-title", "confirm-pw-intro", "confirm-pw-cancel", "confirm-pw-status"} <= ids)
        for name in ids:
            self.assertNotIn(f'id="{name}"', admin, f"{name} is also on admin.html")

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
        self.assertIn("aria-hidden=\"true\" aria-label=\"Email address\" value=\"${esc(email)}\"", self.body)
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
        for slot in ("account-background-slot", "account-email-slot", "account-export-slot"):
            self.assertNotIn(slot, self.js, "those slots belong to other phases")

    def test_each_card_is_a_headed_section_with_a_status_region(self):
        self.assertIn('<h3 id="${id}-heading" tabindex="-1">', self.js)
        self.assertIn('role="status"', self.js)
        for title in ("Password", "Two-step sign-in", "Recovery codes", "Signed-in devices", "Delete account"):
            self.assertIn(title, self.js)

    def test_password_fields_say_what_they_are_for(self):
        self.assertIn("pwField('acct-pw-current', 'Current password', 'current-password')", self.js)
        self.assertIn("pwField('acct-pw-new', 'New password', 'new-password'", self.js)
        self.assertIn("pwField('acct-del-password', 'Your password', 'current-password')", self.js)
        self.assertIn("pwField('acct-mfa-off-password', 'Your password', 'current-password')", self.js)
        self.assertIn('autocomplete="username"', self.js, "a hidden username field for password managers")
        self.assertIn('autocomplete="one-time-code"', self.js)
        self.assertIn('inputmode="numeric"', self.js)
        self.assertIn('class="btn pw-toggle" type="button" aria-controls="${id}" aria-pressed="false">Show password', self.js)

    def test_sign_out_others_is_ticked_by_default(self):
        self.assertRegex(self.js, r'<input id="acct-pw-others" type="checkbox" checked />\s*Sign out other devices')

    def test_everything_sensitive_goes_through_with_recent_auth(self):
        for path in ("/api/auth/mfa/setup", "/api/auth/mfa/enable", "/api/auth/mfa/disable", "/api/auth/recovery-codes",
                     "/api/auth/password", "/api/auth/sessions/end-others"):
            line = next(l for l in self.js.splitlines() if path in l and "post(" in l)
            self.assertTrue("withRecentAuth" in line or "recentWith" in line, f"{path} must go through withRecentAuth")
        self.assertIn("withRecentAuth(fn)", self.js)
        delete = next(l for l in self.js.splitlines() if "/api/account/delete" in l)
        self.assertIn("recentWith", delete)

    def test_two_step_setup_offers_every_way_to_enrol(self):
        self.assertIn("setup.qr_svg", self.js)
        self.assertIn("setup.secret_groups", self.js)
        self.assertIn("setup.otpauth_uri", self.js)
        self.assertIn("Copy key", self.js)
        self.assertIn("Can't scan?", self.js, "the key as text, for people who can't scan")
        self.assertIn("recovery code works instead of the app", self.js)

    def test_new_recovery_codes_reuse_the_first_run_step(self):
        self.assertIn("location.assign('first-run.html')", self.js)
        self.assertNotIn("codes-download", self.js, "the Copy / Download / Print step lives in first-run.html")

    def test_delete_says_what_goes_and_shows_the_servers_refusal(self):
        self.assertIn("can't be undone", self.js)
        for what in ("workspaces", "documents", "uploaded", "keys"):
            self.assertIn(what, self.js)
        self.assertIn("showError(form.querySelector('.acct-error')", self.js, "an admin's refusal is shown as the server wrote it")
        start = self.js.index("subscription === 'failed'")
        self.assertIn("link.href = 'login.html'", self.js[start:start + 900], "the failed-cancel message ends in a link")
        self.assertIn("link.focus()", self.js[start:start + 900])
        self.assertIn("e.message", self.js)

    def test_controls_are_big_enough_and_use_16px_text(self):
        css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        field = re.search(r"\.account-card input\[type=\"password\"\][^{]*\{([^}]*)\}", css).group(1)
        self.assertIn("font-size: 16px", field)
        self.assertIn("min-height: 44px", field)
        self.assertIn("min-height: 44px", re.search(r"\.account-card \.btn:not\(\.btn-sm\)[^{]*\{([^}]*)\}", css).group(1))
        self.assertRegex(css, r"\.qr-box svg \{[^}]*width: 100%")


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
                 "/job.html?id=3#notes", "/", "", "/signup.html?invite=x"]
        done = subprocess.run(["node", "-e", script, json.dumps(cases)], capture_output=True, text=True, check=True)
        out = json.loads(done.stdout)
        for bad in cases[:8] + [""]:
            self.assertEqual(out[bad], "./", repr(bad))
        self.assertEqual(out["/job.html?id=3#notes"], "/job.html?id=3#notes")
        self.assertEqual(out["/"], "/")
        for page in ("/first-run.html", "/signup.html?invite=x"):
            self.assertEqual(out[page], "./", page)

    def test_every_signed_in_page_shows_the_sign_in_notice_and_the_codes_reminder(self):
        """A sign-in can return to any page (?next=), and the notice is shown once, by the first page that asks."""
        for name in ("app.js", "job.js", "admin.js"):
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
        self.assertIn("/api/admin/plaintext-files/delete", guarded)
        self.assertIn("/api/auth/mfa/setup", guarded)
        for name in ("app.js", "job.js", "welcome.js", "account.js", "common.js", "admin.js", "export.js",
                     "background.js", "account-email.js", "email-settings.js"):
            for number, line in enumerate((FRONTEND / name).read_text(encoding="utf-8").splitlines(), 1):
                for path in guarded:
                    # A plain api(path) is a GET; the guard is on the calls that change something.
                    changes = "method:" in line or "post(" in line or "del(" in line
                    if f"'{path}'" in line and changes and ("api(" in line or "post(" in line or "del(" in line):
                        self.assertTrue(any(w in line for w in ("withRecentAuth", "recentWith", "reauth(")),
                                        f"{name}:{number} calls {path} without withRecentAuth")

    def test_the_recovery_codes_page_asks_for_the_password_when_it_is_needed(self):
        shown = self.auth[self.auth.index("async function initFirstRun"):self.auth.index("async function submitSetup")]
        self.assertIn("reauth_required", shown)
        self.assertIn("confirmPassword()", shown)

    def test_a_recovery_code_sign_in_says_how_many_are_left(self):
        start = self.auth.index("$('#mfa-form').addEventListener('submit'")
        step = self.auth[start:self.auth.index("$('#mfa-switch').addEventListener", start)]
        self.assertIn("recovery_codes_left", step)
        self.assertIn("SIGN_IN_NOTICE_KEY", step)
        self.assertIn("showSignInNotice();", self.app[self.app.index("function applyAccount"):])
        self.assertIn("sessionStorage.removeItem(SIGN_IN_NOTICE_KEY)", self.common)

    def test_the_code_step_after_a_reload_still_names_the_account_for_password_managers(self):
        self.assertIn("sessionStorage.setItem(EMAIL_KEY, email)", self.auth)
        self.assertIn("loginEmail = sessionStorage.getItem(EMAIL_KEY)", self.auth)
        self.assertIn("rememberEmail('')", self.auth, "forgotten once signed in")

    def test_results_on_the_account_cards_are_announced_through_the_permanent_live_region(self):
        say = next(l for l in self.account.splitlines() if l.startswith("  const say = "))
        self.assertIn("announce(text)", say)

    def test_focus_stays_in_the_password_card_after_a_change(self):
        changed = self.account[self.account.index("async function changePassword"):self.account.index("/* ---------- two-step")]
        self.assertRegex(changed, r"renderPassword\(\);\s*document\.getElementById\('acct-pw-heading'\)\?\.focus\(\)")

    def test_the_setup_key_has_no_name_attribute_a_code_element_cannot_take(self):
        self.assertRegex(self.account, r'<code class="mfa-secret">')
        self.assertNotIn("aria-labelledby", re.search(r"<code class=\"mfa-secret\"[^>]*>", self.account).group(0))

    def test_new_recovery_codes_ask_for_a_code_when_two_step_is_on(self):
        card = self.account[self.account.index("function renderCodes"):self.account.index("/* ---------- sign out")]
        self.assertIn('id="acct-rc-code"', card)
        self.assertIn("codeBody(code.value.trim())", card)
        self.assertIn("withRecentAuth(() => post('/api/auth/recovery-codes', body))", card)


class SignupPageTest(unittest.TestCase):
    """signup.html + signup.js: open sign-up and invitation links, then the recovery codes (WCAG 2.2 as for the other
    sign-in pages)."""

    @classmethod
    def setUpClass(cls):
        cls.html = parse("signup.html")
        cls.js = (FRONTEND / "signup.js").read_text(encoding="utf-8")
        cls.src = (FRONTEND / "signup.html").read_text(encoding="utf-8")

    def by_id(self, ident):
        return next(n for n in self.html.nodes if n["attrs"].get("id") == ident)

    def test_scripts_load_in_order_and_the_invite_link_is_not_leaked(self):
        self.assertLess(self.src.index("common.js"), self.src.index("auth.js"))
        self.assertLess(self.src.index("auth.js"), self.src.index("signup.js"))
        meta = [n["attrs"] for n in self.html.nodes if n["tag"] == "meta" and n["attrs"].get("name") == "referrer"]
        self.assertEqual(meta[0].get("content"), "no-referrer", "the link carries a secret: no Referer to other sites")

    def test_the_form_is_a_native_form_with_our_own_messages(self):
        self.assertIn("novalidate", self.by_id("signup-form")["attrs"])
        self.assertEqual(self.by_id("signup-submit")["attrs"].get("type"), "submit")
        self.assertIn("autocomplete", self.by_id("email")["attrs"])

    def test_every_refusal_has_a_plain_message_and_the_codes_step_follows(self):
        for code in ("signup_closed", "invite_needed", "invite_invalid", "email_taken"):
            self.assertIn(code, self.js)
        self.assertIn("showCodes(res.data.recovery_codes, res.data.csrf)", self.js)
        self.assertIn("'/api/auth/signup'", self.js)
        self.assertIn("$('#page-title').focus()", self.js, "a refusal takes focus, so it is read out")

    def test_closed_step_and_codes_step_are_in_the_page(self):
        for ident in ("closed-step", "closed-text", "setup-step", "codes-step", "codes-list", "codes-continue", "codes-saved"):
            self.by_id(ident)


class AdminPageTest(unittest.TestCase):
    """admin.html + admin.js: static checks that need no browser. The tables and dialogs are written by script, so
    the checks read the templates in admin.js."""

    @classmethod
    def setUpClass(cls):
        cls.html = parse("admin.html")
        cls.js = (FRONTEND / "admin.js").read_text(encoding="utf-8")
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        cls.src = (FRONTEND / "admin.html").read_text(encoding="utf-8")

    def by_id(self, ident):
        return next(n for n in self.html.nodes if n["attrs"].get("id") == ident)

    def test_every_section_has_a_heading_and_the_nav_points_at_them(self):
        nav = self.by_id("main")
        self.assertEqual(nav["tag"], "main")
        links = [n["attrs"]["href"] for n in self.html.nodes if n["tag"] == "a" and "admin-nav" in
                 self.html.nodes[n["parent"]]["attrs"].get("class", "")]
        self.assertEqual(links, ["#usage", "#users", "#signup", "#server", "#audit"])
        for target in links:
            section = self.by_id(target[1:])
            self.assertEqual(section["tag"], "section")
            self.assertTrue(section["attrs"].get("aria-labelledby"), f"{target} is named by its heading")

    def test_dialogs_are_native_and_named(self):
        for ident in ("person-dialog", "confirm-dialog"):
            dialog = self.by_id(ident)
            self.assertEqual(dialog["tag"], "dialog")
            self.assertTrue(dialog["attrs"].get("aria-labelledby"))
        dialogs = (FRONTEND / "dialogs.js").read_text(encoding="utf-8")
        self.assertIn("showModal()", dialogs)
        self.assertIn(".modal-title').focus()", dialogs, "focus moves to the dialog's heading")
        self.assertIn("openers.get(dlg.id)", dialogs, "focus goes back to the button that opened it")
        self.assertLess(self.src.index("./dialogs.js"), self.src.index("./server-settings.js"))
        self.assertLess(self.src.index("./dialogs.js"), self.src.index("./admin.js"))
        settings = (FRONTEND / "server-settings.js").read_text(encoding="utf-8")
        self.assertNotRegex(settings + self.js, r"(?<![\w.])confirm\(", "confirmations use the focus-managed dialog")

    def test_failures_inside_a_dialog_are_said_inside_it(self):
        """The page behind an open modal dialog is inert: a toast or a live region out there is neither seen nor
        heard, so errors in the membership dialog go to alert elements inside it."""
        for ident in ("mem-error", "grant-error", "grants-error"):
            self.assertRegex(self.js, rf'<p id="{ident}" class="error" role="alert" hidden></p>')
        start = self.js.index("function grantProblem")
        end = self.js.index("/* ================= sign-up and invitations")
        self.assertNotIn("toast(", self.js[start:end], "the membership dialog never reports through a toast")

    def test_tables_have_captions_and_header_scopes(self):
        self.assertIn('<caption class="visually-hidden">${esc(caption)}</caption>', self.js)
        self.assertIn('scope="col"', self.js)
        self.assertIn('scope="row"', self.js)
        self.assertIn('aria-sort="${dir}"', self.js)
        self.assertIn("'ascending' : 'descending'", self.js)
        self.assertEqual(self.js.count("role=\"table\""), 1, "one table builder, so every table is built the same way")

    def test_action_buttons_name_their_person(self):
        for phrase in ("Send reset email to ${l}", "Edit membership of ${l}", "Make ${l} an admin", "Disable ${l}",
                       "Enable ${l}", "Turn off two-step sign-in for ${l}", "Share the admin key with ${l} again",
                       "Make ${l} a regular user", "Delete the account of ${l}", "Unlock ${l}"):
            self.assertIn(phrase, self.js)
        self.assertIn('aria-label="${esc(name)}"', self.js, "names go through esc()")
        # The visible text starts the accessible name (WCAG 2.5.3)
        for visible, name in (("Send reset email", "Send reset email to"), ("Disable", "Disable ${l}"),
                              ("Make admin", "Make ${l} an admin")):
            self.assertRegex(self.js, r"'%s', `%s" % (re.escape(visible), re.escape(name)))

    def test_other_peoples_text_only_goes_in_escaped(self):
        # Names and emails are chosen by their owners: never into innerHTML except through esc().
        for field in ("u.email", "u.name", "p.email", "p.name", "i.email", "g.note", "r.who", "f.path"):
            for m in re.finditer(re.escape(field), self.js):
                line = self.js[self.js.rfind("\n", 0, m.start()) + 1:self.js.find("\n", m.end())]
                self.assertRegex(line, r"esc\(|label\(|=== |\|\||\?|&&|\.value =|textContent|\.slice|if \(|\.startsWith|\.includes",
                                 f"{field} written without esc(): {line.strip()}")
        self.assertNotIn("document.write", self.js)

    def test_cards_on_narrow_screens_and_a_sort_menu(self):
        self.assertIn("stack-table admin-table", self.js)
        self.assertRegex(self.css, r"@media \(max-width: 640px\) \{\s*\.sort-select \{ display: flex; \}")
        self.assertIn('id="users-sort"', self.js)
        self.assertIn('<label for="users-sort">Sort by</label>', self.js)

    def test_the_moved_settings_are_here_and_gone_from_the_dashboard(self):
        ids = {n["attrs"].get("id") for n in self.html.nodes}
        for ident in ("llm-form", "llm-provider", "llm-model", "llm-key", "detect-levels", "score-test", "keys-test",
                      "rotate-key", "plain-delete", "audit-more", "signup-form", "invite-form"):
            self.assertIn(ident, ids)
        index = parse("index.html")
        index_ids = {n["attrs"].get("id") for n in index.nodes}
        for ident in ("llm-form", "llm-provider", "llm-key", "commute-keys-card", "keys-test", "score-test", "detect-levels"):
            self.assertNotIn(ident, index_ids, f"{ident} moved to the admin panel")
        self.assertFalse(any("data-admin-only" in n["attrs"] and n["tag"] == "form" for n in index.nodes))

    def test_actions_that_need_the_password_go_through_withRecentAuth(self):
        for call in ("/role", "/regrant", "/mfa/disable", "/keys/rotate", "/plaintext-files/delete"):
            line = next(l for l in self.js.splitlines() if call in l and "post(" in l)
            self.assertIn("reauth(", line, call)
        self.assertIn("withRecentAuth(fn)", self.js)

    def test_the_cancel_checkbox_is_described_by_its_hint(self):
        self.assertIn('id="mem-cancel" type="checkbox" aria-describedby="mem-cancel-hint"', self.js)
        self.assertIn('id="mem-cancel-hint"', self.js)

    def test_ending_a_free_period_reports_what_stripe_said(self):
        start = self.js.index("else if (endBtn)")
        self.assertIn("r?.stripe?.problem", self.js[start:start + 1500])

    def test_warnings_for_one_admin_and_a_missing_key(self):
        self.assertIn("only_one_admin", self.js)
        self.assertIn("admin_key_available", self.js)
        self.assertIn("You are the only admin", self.js)


class BillingPageTest(unittest.TestCase):
    """billing.html + billing.js (Phase I): the membership page."""

    @classmethod
    def setUpClass(cls):
        cls.html = parse("billing.html")
        cls.src = (FRONTEND / "billing.html").read_text(encoding="utf-8")
        cls.js = (FRONTEND / "billing.js").read_text(encoding="utf-8")
        cls.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        cls.ids = {n["attrs"]["id"]: n for n in cls.html.nodes if "id" in n["attrs"]}

    def test_scripts_theme_and_account_menu(self):
        self.assertLess(self.src.index("common.js"), self.src.index("billing.js"))
        self.assertIn("jobhunt.theme", self.src, "the theme is set before first paint")
        self.assertIn("initAccountMenu(me)", self.js)
        for name in ("index.html", "job.html", "admin.html"):
            self.assertIn('href="billing.html"', (FRONTEND / name).read_text(encoding="utf-8"), f"{name}'s account menu")

    def test_every_section_is_headed_and_results_are_live(self):
        for ident in ("me-card", "plans-card", "off-card", "without-card"):
            card = self.ids[ident]
            self.assertEqual(card["tag"], "section")
            self.assertIn(card["attrs"]["aria-labelledby"], self.ids)
        self.assertEqual(self.ids["billing-error"]["attrs"].get("role"), "alert")
        self.assertEqual(self.ids["billing-result"]["attrs"].get("role"), "status")
        self.assertEqual(self.ids["portal-status"]["attrs"].get("role"), "status")
        self.assertEqual(self.ids["plans-status"]["attrs"].get("role"), "status")
        self.assertIn("announce(", self.js, "the check after Checkout is announced")

    def test_the_test_mode_badge_is_text_not_colour(self):
        self.assertIn("Test mode", self.ids["test-badge"]["text"])
        self.assertIn("hidden", self.ids["test-badge"]["attrs"])
        self.assertIn("st.billing.test_mode", self.js)

    def test_checkout_return_portal_and_what_waits(self):
        self.assertIn("/api/billing/checkout", self.js)
        self.assertIn("/api/billing/verify", self.js)
        self.assertIn("/api/billing/portal", self.js)
        self.assertIn("session_id", self.js)
        self.assertIn("cancelled", self.js)
        self.assertIn("Nothing was charged", self.js)
        self.assertIn("location.assign(r.url)", self.js)
        self.assertIn("replaceState", self.js, "a reload must not check the same payment twice")
        self.assertIn("Manage payment and plan", self.ids["portal-btn"]["text"])
        self.assertIn("doesn't charge", self.ids["off-title"]["text"])
        text = self.ids["without-card"]["text"]
        for word in ("stays", "searches", "chat", "cover letters", "research"):
            self.assertIn(word, text)

    def test_plan_buttons_name_their_plan_and_start_with_their_visible_text(self):
        self.assertIn('aria-label="Become a member: ', self.js)
        self.assertIn(">Become a member</button>", self.js)
        self.assertIn("setBusy(btn, true)", self.js, "a working button keeps keyboard focus")
        self.assertEqual(GeneratedMarkupTest().check_file("billing.js"), [])

    def test_focus_moves_on_when_the_plans_disappear_and_a_past_due_member_is_sent_to_the_card(self):
        start = self.js.index("if (e.code === 'already_subscribed')")
        branch = self.js[start:self.js.index("return;", start)]
        self.assertIn(".focus()", branch, "the clicked plan button is gone after the page is redrawn")
        self.assertIn("'#portal-btn'", branch)
        self.assertIn("Update your payment method", self.js)
        self.assertIn("no_session", self.js)

    def test_everything_from_the_server_is_escaped_and_targets_are_big_enough(self):
        self.assertIn("esc(p.plan)", self.js)
        self.assertIn("esc(k)", self.js)
        self.assertRegex(self.css, r"\.billing-card \.btn \{[^}]*min-height: 44px")
        self.assertRegex(self.css, r"@media \(max-width: 480px\) \{ \.billing-facts \{ grid-template-columns: 1fr")  # 320 px


class FreeSearchNoteTest(unittest.TestCase):
    """The note beside "Run search" (index.html + free-searches.js)."""

    @classmethod
    def setUpClass(cls):
        cls.html = parse("index.html")
        cls.js = (FRONTEND / "free-searches.js").read_text(encoding="utf-8")
        cls.ids = {n["attrs"]["id"]: n for n in cls.html.nodes if "id" in n["attrs"]}

    def test_the_note_describes_the_button_and_loads_after_app_js(self):
        self.assertEqual(self.ids["search-run"]["attrs"].get("aria-describedby"), "free-search-note")
        self.assertIn("hidden", self.ids["free-search-note"]["attrs"])
        self.assertEqual(self.ids["free-search-note"]["attrs"].get("role"), "status", "a changed count is announced")
        src = (FRONTEND / "index.html").read_text(encoding="utf-8")
        self.assertLess(src.index("./app.js"), src.index("./free-searches.js"))

    def test_it_says_what_a_search_uses_and_links_to_the_membership_page(self):
        self.assertIn("Running a search uses 1 of your", self.js)
        self.assertIn('href="billing.html"', self.js)
        app = (FRONTEND / "app.js").read_text(encoding="utf-8")
        self.assertGreaterEqual(app.count("refreshFreeSearchNote"), 3, "after queueing, after a failed start, after a run ends")


class StripeCardTest(unittest.TestCase):
    """The admin panel's "Payments (Stripe)" card (admin.html + stripe-settings.js) and the membership dialog."""

    @classmethod
    def setUpClass(cls):
        cls.html = parse("admin.html")
        cls.src = (FRONTEND / "admin.html").read_text(encoding="utf-8")
        cls.js = (FRONTEND / "stripe-settings.js").read_text(encoding="utf-8")
        cls.admin = (FRONTEND / "admin.js").read_text(encoding="utf-8")
        cls.ids = {n["attrs"]["id"]: n for n in cls.html.nodes if "id" in n["attrs"]}

    def test_the_card_is_in_the_server_section_under_a_heading(self):
        card = self.ids["stripe-card"]
        self.assertTrue(any(a["attrs"].get("id") == "server" for a in self.html.ancestors(card)))
        self.assertEqual(self.ids["stripe-title"]["tag"], "h3")
        self.assertIn("Payments (Stripe)", self.ids["stripe-title"]["text"])
        self.assertEqual(self.ids["stripe-status"]["attrs"].get("role"), "status")
        self.assertEqual(self.ids["stripe-summary"]["attrs"].get("role"), "status")
        self.assertIn("Sync now", self.ids["stripe-sync"]["text"])

    def test_keys_are_password_fields_that_password_managers_leave_alone(self):
        for ident in ("stripe-secret-key", "stripe-webhook-secret"):
            field = self.ids[ident]
            self.assertEqual(field["attrs"].get("type"), "password")
            self.assertEqual(field["attrs"].get("autocomplete"), "new-password")
        self.assertIn("readonly", self.ids["stripe-webhook-url"]["attrs"])

    def test_scripts_load_in_order_and_saving_asks_for_the_password(self):
        self.assertLess(self.src.index("server-settings.js"), self.src.index("stripe-settings.js"))
        self.assertLess(self.src.index("stripe-settings.js"), self.src.index("admin.js"))
        self.assertIn("initStripeSettings()", self.admin)
        self.assertIn("loadStripeSettings()", self.admin)
        self.assertRegex(self.js, r"withRecentAuth\(\(\) => api\('/api/admin/stripe', \{ method: 'PUT'")
        self.assertIn("setFieldError(input, e.message)", self.js, "a refused value is said under its field")
        self.assertIn("confirmDialog(", self.js, "removing the key asks first: it switches memberships off")
        self.assertEqual(GeneratedMarkupTest().check_file("stripe-settings.js"), [])

    def test_test_mode_is_said_in_words(self):
        self.assertIn("'Test mode'", self.js)

    def test_the_membership_dialog_shows_billing_and_offers_the_cancel_option(self):
        self.assertIn("Cancel their subscription at the period end", self.admin)
        self.assertIn("cancel_subscription_at_period_end", self.admin)
        self.assertIn("card_last4", self.admin)
        self.assertIn("also moves their next payment", self.admin)
        self.assertIn("r.stripe.problem", self.admin, "a free period that Stripe wasn't told about says so")
        self.assertRegex(self.admin, r'<label id="mem-cancel-row" class="check" hidden><input id="mem-cancel"')


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
        self.assertIn('<button id="export-btn" class="btn btn-primary" type="button">Download my data (.zip)</button>', self.js)
        self.assertIn('<span id="export-status" class="muted" role="status"></span>', self.js)
        self.assertIn("setBusy(btn, true)", self.js, "a working button uses aria-disabled, so focus stays on it")
        self.assertIn("not encrypted", self.js)

    def test_a_post_through_withRecentAuth_and_a_blob(self):
        self.assertIn("method: 'POST'", self.js)
        self.assertIn("X-CSRF-Token", self.js)
        self.assertIn("withRecentAuth(fetchExport)", self.js)
        self.assertIn("URL.createObjectURL(blob)", self.js)

    def test_busy_ends_and_every_outcome_is_said_in_the_status_region(self):
        self.assertIn("setBusy(btn, false)", self.js)
        for said in ("Making your file", "Done. Your browser saved", "Nothing was downloaded: confirm your password", "could not be downloaded"):
            self.assertIn(said, self.js)
        self.assertIn("Keep it somewhere safe", self.js)
        self.assertIn("e.status === 413", self.js, "a refusal says what to do (the server's own text)")


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
        self.assertIn("✓", self.js, "a finished step shows a tick, not only a colour")

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
        self.assertIn("min-height: 44px", re.search(r"\.wiz-actions \.btn \{([^}]*)\}", self.css).group(1))
        mark = re.search(r"\.wiz-mark span:first-child \{([^}]*)\}", self.css).group(1)
        self.assertRegex(mark, r"min-width: (2[4-9]|[3-9]\d)px")
        self.assertRegex(mark, r"[^-]height: (2[4-9]|[3-9]\d)px")

    def test_unbuilt_features_are_hidden_behind_a_feature_check(self):
        self.assertIn("me.features.digests", self.js)
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

    def test_the_schedule_card_says_a_membership_is_needed_and_links_to_the_page(self):
        app = (FRONTEND / "app.js").read_text(encoding="utf-8")
        start = app.index("function renderScheduleStatus")
        body = app[start:app.index("async function loadSchedule", start)]
        self.assertIn("sch.membership_required", body)
        self.assertIn('<a href="billing.html">See membership options</a>', body)
        self.assertLess(body.index("sch.membership_required"), body.index("data-focus-background"),
                        "a skip for a membership isn't answered with the background-access button")
        self.assertIn("!sch.membership_required", body, "no 'next search' note while it can't run")
        self.assertEqual(GeneratedMarkupTest().check_file("app.js"), [])

    def test_targets_are_big_enough_and_the_page_scripts_load_in_order(self):
        self.assertRegex(self.css, r"\.check input[^{]*\{[^}]*width: 18px")  # 24 px high row, 44 px on small screens
        self.assertIn("min-height: 44px", re.search(r"@media[^{]*\{[^@]*?\.check, \.check-field[^{]*\{([^}]*)\}", self.css, re.S).group(1))
        src = (FRONTEND / "index.html").read_text(encoding="utf-8")
        self.assertLess(src.index("./app.js"), src.index("./background.js"))
        self.assertIn("common.js", src)


class EmailPagesTest(unittest.TestCase):
    """Phase H: reset-password.html, verify-email.html, "Email me a link", the Settings address card and the admin
    panel's Email card (WCAG 2.2 as for the other sign-in pages)."""

    @classmethod
    def setUpClass(cls):
        read = lambda n: (FRONTEND / n).read_text(encoding="utf-8")  # noqa: E731
        cls.reset, cls.verify = parse("reset-password.html"), parse("verify-email.html")
        cls.reset_src, cls.verify_src = read("reset-password.html"), read("verify-email.html")
        cls.reset_js, cls.verify_js = read("reset-password.js"), read("verify-email.js")
        cls.forgot_js, cls.card_js, cls.admin_js = read("forgot.js"), read("account-email.js"), read("email-settings.js")
        cls.admin_src, cls.admin_page = read("admin.html"), parse("admin.html")
        cls.login_src, cls.recover_src = read("login.html"), read("recover.html")

    @staticmethod
    def by_id(page, ident):
        return next(n for n in page.nodes if n["attrs"].get("id") == ident)

    def test_the_links_carry_a_secret_so_no_referrer_and_scripts_load_in_order(self):
        for src, js in ((self.reset_src, "reset-password.js"), (self.verify_src, "verify-email.js")):
            self.assertIn('<meta name="referrer" content="no-referrer" />', src)
            self.assertLess(src.index("common.js"), src.index("auth.js"))
            self.assertLess(src.index("auth.js"), src.index(js))

    def test_the_token_comes_from_the_fragment_and_leaves_the_address_bar(self):
        for js in (self.reset_js, self.verify_js):
            self.assertIn("location.hash", js)
            self.assertIn("history.replaceState", js)
            self.assertNotIn("location.search", js.replace("location.pathname + location.search", ""))
        self.assertIn("'/api/auth/reset/info'", self.reset_js)
        self.assertIn("'/api/auth/reset'", self.reset_js)
        self.assertIn("'/api/auth/verify-email'", self.verify_js)

    def test_reset_fields_say_what_they_are_for(self):
        pw = self.by_id(self.reset, "password")["attrs"]
        self.assertEqual((pw.get("type"), pw.get("autocomplete")), ("password", "new-password"))
        self.assertEqual(self.by_id(self.reset, "code")["attrs"].get("autocomplete"), "one-time-code")
        self.assertIn("novalidate", self.by_id(self.reset, "reset-form")["attrs"])
        toggle = next(n for n in self.reset.nodes if "pw-toggle" in n["attrs"].get("class", ""))
        self.assertEqual(toggle["attrs"].get("aria-pressed"), "false")

    def test_the_locked_warning_the_checkbox_and_the_recovery_link(self):
        self.assertIn("Your saved searches, documents and jobs are locked with your old password.", self.reset_src)
        self.assertIn("deletes them, and they can't be brought back.", self.reset_src)
        self.assertIn("I understand my saved data will be deleted", self.reset_src)
        self.assertIn('href="recover.html"', self.reset_src)
        self.assertIn("Your data will be kept", self.reset_src)
        self.assertIn("accept_wipe", self.reset_js)
        self.assertIn("'accept-wipe'", self.reset_js, "a refusal about the tick is written under the tick")

    def test_a_wipe_ends_in_the_first_run_recovery_codes_step(self):
        self.assertIn("showCodes(data.recovery_codes, data.csrf)", self.reset_js)
        for ident in ("codes-step", "codes-list", "codes-copy", "codes-download", "codes-print", "codes-saved",
                      "codes-continue", "codes-status", "setup-step"):
            self.by_id(self.reset, ident)
        self.assertIn("You no longer have the admin key. Ask another admin to share it with you again.", self.reset_src)
        self.assertIn("admin_key_lost", self.reset_js)

    def test_every_refusal_is_said_in_words_and_the_second_factor_is_asked_for(self):
        for code in ("link_invalid", "disabled", "second_factor_required"):
            self.assertIn(code, self.reset_js)
        self.assertIn("$('#page-title').focus()", self.reset_js)
        self.assertIn("announce(", self.reset_js)
        self.assertIn("link_invalid", self.verify_js)
        self.assertIn("email_taken", self.verify_js)

    def test_email_me_a_link_is_offered_only_when_email_is_on_and_reuses_the_typed_address(self):
        for src in (self.login_src, self.recover_src):
            self.assertIn('<li id="forgot-item" hidden>', src)
            self.assertIn('id="forgot-btn" class="btn-link" type="button">Email me a link', src)
            self.assertIn('id="forgot-status" class="muted" role="status"', src)
            self.assertLess(src.index("auth.js"), src.index("forgot.js"))
        self.assertIn("email_enabled", self.forgot_js)
        self.assertIn("$('#email').value", self.forgot_js, "WCAG 3.3.7: the address typed above is carried over")
        self.assertIn("'/api/auth/forgot'", self.forgot_js)
        self.assertIn("announce(message)", self.forgot_js)

    def test_the_settings_card_sits_in_its_own_slot_and_uses_recent_sign_in(self):
        index = (FRONTEND / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="account-address-slot"', index)
        self.assertLess(index.index("common.js"), index.index("account-email.js"))
        self.assertIn("getElementById('account-address-slot')", self.card_js)
        self.assertIn('<h3 id="acct-addr-heading" tabindex="-1">Your email address', self.card_js)
        self.assertIn('role="status"', self.card_js)
        self.assertIn("withRecentAuth(() => post('/api/auth/email/change'", self.card_js)
        self.assertIn("Send the confirmation again", self.card_js)
        self.assertIn("Change email address", self.card_js)
        self.assertIn('<label for="acct-addr-new">New email address</label>', self.card_js)
        self.assertIn('autocomplete="email"', self.card_js)
        self.assertIn('aria-expanded', self.card_js)
        self.assertIn("setFieldError(input, e.message)", self.card_js)

    def test_the_admin_email_card_has_labelled_fields_and_its_buttons(self):
        for ident, label in (("email-host", "Mail server"), ("email-port", "Port"), ("email-security", "Encryption"),
                             ("email-user", "User name"), ("email-password", "Password"), ("email-from", "From address"),
                             ("email-public", "Public address")):
            node = self.by_id(self.admin_page, ident)
            self.assertIn(f'for="{ident}"', self.admin_src)
            self.assertIn(label, self.admin_src[self.admin_src.index(f'for="{ident}"'):][:200])
            self.assertIsNotNone(node)
        self.assertEqual(self.by_id(self.admin_page, "email-password")["attrs"].get("type"), "password")
        self.assertIn("Spaces are removed from the password", self.admin_src)
        self.assertIn("Use this address", self.admin_src)
        self.assertIn("Send a test email", self.admin_src)
        self.assertEqual(self.by_id(self.admin_page, "email-test-status")["attrs"].get("role"), "status")
        self.assertEqual(self.by_id(self.admin_page, "email-error")["attrs"].get("role"), "alert")
        self.assertLess(self.admin_src.index("server-settings.js"), self.admin_src.index("email-settings.js"))
        self.assertLess(self.admin_src.index("email-settings.js"), self.admin_src.index("admin.js"))

    def test_small_fixes_in_the_email_pages(self):
        self.assertIn('aria-describedby="email-password-state email-password-hint"', self.admin_src)
        self.assertGreaterEqual(self.admin_js.count("withRecentAuth("), 2, "saving asks for the password")
        self.assertIn("document.title = \"This link doesn't work", self.reset_js)
        self.assertIn("I have a recovery code</a>: it lets you set a new password and keep your data.", self.verify_src)
        card = self.card_js
        self.assertLess(card.index("render();\n        say('Your address is already confirmed.')"),
                        card.index("acct-addr-heading').focus()"), "said after the rebuild, then focus moves to the heading")
        self.assertNotIn("if (await load()) { changing = false; render(); }", card)

    def test_the_admin_email_card_shows_only_set_or_not_set_and_names_a_refused_field(self):
        self.assertIn("password.set", self.admin_js)
        self.assertNotIn("st.password.value", self.admin_js)
        self.assertIn("EMAIL_FIELDS[ex.field]", self.admin_js)
        self.assertIn("setFieldError(", self.admin_js)
        self.assertIn("location.origin", self.admin_js)
        self.assertIn("'/api/settings/email/test'", self.admin_js)
        self.assertIn("status === 'failed'", self.admin_js)

    def test_invitations_offer_to_email_and_say_what_happened(self):
        self.assertIn('id="invite-send"', self.admin_src)
        self.assertIn('id="invite-result-note"', self.admin_src)
        admin_js = (FRONTEND / "admin.js").read_text(encoding="utf-8")
        self.assertIn("body.send = $('#invite-send').checked", admin_js)
        for status in ("queued", "not_set_up", "no_address", "busy", "off"):
            self.assertIn(status, admin_js)
        self.assertIn("initEmailSettings();", admin_js)
        self.assertIn("loadEmailSettings();", admin_js)


if __name__ == "__main__":
    unittest.main()
