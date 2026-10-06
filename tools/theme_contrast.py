#!/usr/bin/env python3
"""Contrast of every theme's colour pairs that the app actually uses together (WCAG 2.2 AA).

    python3 tools/theme_contrast.py            # report every theme; exit 1 if any pair fails
    python3 tools/theme_contrast.py --all      # also list the pairs that pass

Reads frontend/themes.css (one block of custom properties per theme) and checks the pairs listed
in PAIRS below against their AA minimum: 4.5:1 for text (1.4.3), 3:1 for the parts of controls and
graphics people need to see (1.4.11: focus rings, field borders, selected fills, map legend swatches).
Tinted backgrounds such as ``color-mix(in srgb, var(--warn) 12%, transparent)`` are composited over
the ground they sit on. tests/test_accessibility.py runs the same check, so a theme change that drops
a pair below AA fails the tests.

When you style something with a new combination of tokens, add it to PAIRS.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THEMES_CSS = ROOT / "frontend" / "themes.css"

TEXT = 4.5  # 1.4.3: text and images of text (all text in the app is under 18.66px bold / 24px)
UI = 3.0  # 1.4.11: user interface components and graphical objects

GROUNDS = ("bg", "panel", "card", "field")  # page, chrome, cards, wells (inputs, score badges)

# (foreground token, background, minimum, where it is used). A background is a token, or
# (token, percent, ground) for color-mix(in srgb, var(--token) percent%, transparent) laid on a ground.
PAIRS: list[tuple[str, object, float, str]] = []


def _add(fgs, bgs, minimum, where):
    for fg in fgs:
        for bg in bgs:
            PAIRS.append((fg, bg, minimum, where))


# Text on the grounds
_add(["text"], GROUNDS, TEXT, "body text, labels, inputs, buttons")
_add(["muted"], GROUNDS, TEXT, "secondary text, hints, field labels, placeholders")
_add(["accent-text"], GROUNDS, TEXT, "links, disclosure summaries, link buttons")
_add(["ok", "warn", "danger"], GROUNDS, TEXT, "status text (saved, failed, warnings), score badges on the field ground")
_add(["info"], ("card", "panel"), TEXT, "Onsite and Good-fit badges")
_add(["on-accent"], ["accent"], TEXT, "primary buttons, the selected tab, chip and map mode")
# Text on tinted backgrounds
_add(["ok"], [("ok", 15, "card"), ("ok", 15, "panel")], TEXT, "Remote badge")
_add(["warn"], [("warn", 14, "card"), ("warn", 14, "panel")], TEXT, "Hybrid badge")
_add(["info"], [("info", 14, "card"), ("info", 14, "panel")], TEXT, "Onsite badge")
_add(["muted"], [("quiet", 14, "card"), ("quiet", 14, "panel")], TEXT, "work mode unknown badge")
_add(["danger"], [("danger", 12, "bg"), ("danger", 12, "card"), ("danger", 12, "panel")], TEXT, "error boxes (.error) and the error summary on the sign-in pages")
_add(["text", "accent-text"], [("warn", 12, "panel"), ("warn", 12, "card")], TEXT, "notices (.notice) in dialogs and on the job page; page notices (.page-notice)")
_add(["text"], [("warn", 10, "card"), ("warn", 10, "panel")], TEXT, "callouts (.callout)")
_add(["text"], [("warn", 8, "bg"), ("warn", 8, "card")], TEXT, "office-days note, schedule warning")
_add(["text"], [("accent", 8, "bg"), ("accent", 10, "bg")], TEXT, "duplicates note, bulk-action bar")
_add(["text"], [("accent", 15, "field")], TEXT, "tags in tag inputs (and their remove buttons)")
_add(["text", "muted", "accent-text"], [("accent", 16, "panel")], TEXT, "your messages in the job chat")
# Non-text contrast
_add(["focus"], GROUNDS, UI, "keyboard focus ring")
_add(["focus"], [("accent", 10, "bg"), ("warn", 12, "panel"), ("warn", 12, "card")], UI, "focus ring on the bulk-action bar and on notices")
_add(["accent"], GROUNDS, UI, "selected tab, chip and map mode; checked boxes and radio buttons; progress bar")
_add(["border-strong"], ("bg", "panel", "card"), UI, "borders of text fields, selects, tag inputs and checkboxes")
_add(["ok", "quiet", "special", "info", "warn"], ["bg"], UI, "map legend swatches (job dots and pin rings)")

_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")


def parse_color(value: str) -> tuple[float, float, float, float]:
    """(r, g, b, alpha) in 0..1 from #rgb, #rrggbb, #rrggbbaa or rgb()/rgba()."""
    v = value.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return r, g, b, a
    m = re.match(r"rgba?\(([^)]*)\)", v)
    if not m:
        raise ValueError(f"not a colour: {value!r}")
    parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
    r, g, b = (float(p) / 255 for p in parts[:3])
    a = float(parts[3]) if len(parts) > 3 else 1.0
    return r, g, b, a


def parse_themes(css: str) -> dict[str, dict[str, str]]:
    """Theme id -> {token: value}. The first block (:root, dark) is the default every theme starts from."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    themes: dict[str, dict[str, str]] = {}
    base: dict[str, str] = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        tokens = dict(re.findall(r"--([\w-]+)\s*:\s*([^;]+);", body))
        if ":root" in selectors:
            base.update(tokens)
        for theme in re.findall(r'\[data-theme="([\w-]+)"\]', selectors):
            themes.setdefault(theme, {}).update(tokens)
    return {t: {**base, **tokens} for t, tokens in themes.items()}


def luminance(rgb) -> float:
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def over(fg, bg):
    """fg (with alpha) composited over the opaque bg."""
    a = fg[3]
    return tuple(a * f + (1 - a) * g for f, g in zip(fg[:3], bg[:3])) + (1.0,)


def token(tokens: dict[str, str], name: str):
    if name not in tokens:
        raise KeyError(f"--{name} is not defined")
    found = _COLOR.search(tokens[name])
    if not found:
        raise ValueError(f"--{name}: {tokens[name]!r} is not a colour")
    return parse_color(found.group(0))


def resolve(tokens: dict[str, str], bg) -> tuple[float, float, float, float]:
    if isinstance(bg, tuple):
        tint, pct, ground = bg
        r, g, b, a = token(tokens, tint)
        return over((r, g, b, a * pct / 100), resolve(tokens, ground))
    colour = token(tokens, bg)
    if colour[3] < 1:  # a translucent token over the page
        return over(colour, resolve(tokens, "bg"))
    return colour


def describe(bg) -> str:
    return f"{bg[0]} {bg[1]}% over {bg[2]}" if isinstance(bg, tuple) else bg


def check(css: str | None = None) -> list[dict]:
    """Every (theme, pair) with its ratio and whether it meets its minimum."""
    themes = parse_themes(css if css is not None else THEMES_CSS.read_text(encoding="utf-8"))
    rows = []
    for theme, tokens in themes.items():
        for fg, bg, minimum, where in PAIRS:
            back = resolve(tokens, bg)
            value = ratio(over(token(tokens, fg), back), back)
            rows.append({"theme": theme, "fg": fg, "bg": describe(bg), "ratio": value, "min": minimum,
                         "ok": value >= minimum, "where": where})
    return rows


def main() -> int:
    rows = check()
    show_all = "--all" in sys.argv
    failed = [r for r in rows if not r["ok"]]
    for theme in dict.fromkeys(r["theme"] for r in rows):
        mine = [r for r in rows if r["theme"] == theme and (show_all or not r["ok"])]
        n_fail = sum(not r["ok"] for r in rows if r["theme"] == theme)
        print(f"{theme}: {'all pass' if not n_fail else f'{n_fail} below AA'}")
        for r in mine:
            print(f"   {'ok  ' if r['ok'] else 'FAIL'} --{r['fg']} on {r['bg']}: {r['ratio']:.2f} (needs {r['min']}) {r['where']}")
    print(f"{len(rows)} pairs in {len({r['theme'] for r in rows})} themes; {len(failed)} below AA.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
