"""robots.txt parsing with Google/RFC 9309 semantics.

The stdlib ``urllib.robotparser`` ignores ``*`` / ``$`` wildcards and uses
first-match instead of longest-match, which gets real files (e.g. SEEK's
``Disallow: *?`` + ``Allow: *?keywords``) wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: Optional[float] = None


def _pattern_to_regex(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    body = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(body + ("$" if anchored else ""))


class RobotsRules:
    def __init__(self, text: str = "", agent: str = "*"):
        self.agent = agent.lower()
        self._groups = self._parse(text or "")
        self._group = self._select_group()

    @staticmethod
    def _parse(text: str) -> list[_Group]:
        groups: list[_Group] = []
        current: Optional[_Group] = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None:
                continue
            if key in ("allow", "disallow"):
                if value:
                    current.rules.append((key == "allow", value))
            elif key == "crawl-delay":
                try:
                    current.crawl_delay = float(value)
                except ValueError:
                    pass
        return groups

    def _select_group(self) -> Optional[_Group]:
        specific = [g for g in self._groups if any(a != "*" and a in self.agent for a in g.agents)]
        if specific:
            return _merge(specific)
        star = [g for g in self._groups if "*" in g.agents]
        return _merge(star) if star else None

    def allowed(self, url: str) -> bool:
        if self._group is None:
            return True
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        best_len, best_allow = -1, True
        for allow, pattern in self._group.rules:
            if _pattern_to_regex(pattern).match(path):
                length = len(pattern)
                if length > best_len or (length == best_len and allow):
                    best_len, best_allow = length, allow
        return best_allow

    @property
    def crawl_delay(self) -> Optional[float]:
        return self._group.crawl_delay if self._group else None


def _merge(groups: list[_Group]) -> _Group:
    merged = _Group()
    for g in groups:
        merged.agents.extend(g.agents)
        merged.rules.extend(g.rules)
        if g.crawl_delay is not None:
            merged.crawl_delay = g.crawl_delay
    return merged
