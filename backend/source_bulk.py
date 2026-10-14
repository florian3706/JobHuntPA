"""Careers pages in bulk (docs/design/open-source.md §1.14): the file format, the address flags and the export.

The format is CSV with the columns ``company,url``: a header row is optional, standard quoting (a company name that
contains a comma is put in double quotes, and a quote inside one is doubled), UTF-8. ``company`` may be empty; the
address then names the source, as for a single add.

Nothing in this module touches the network: no request and no name look-up (no DNS, no socket). :func:`address_flags`
judges an address by its shape alone, so it is not a safety verdict: a name that looks ordinary can still lead
anywhere. The endpoints are in ``backend/sources.py``.
"""
from __future__ import annotations

import csv
import io
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Union
from urllib.parse import unquote, urlsplit

MAX_TEXT = 512 * 1024  # characters of pasted text or file
MAX_ROWS = 1000
MAX_URL = 2048
MAX_COMPANY = 200
HEADER = ("company", "url")

# Flags that stop a row from being added at all (the rest only start it unticked).
BLOCKING = frozenset({"not_web", "local"})

# Link shorteners: an address on one of these hides where it leads.
SHORTENERS = frozenset({
    "bit.ly", "bitly.com", "bit.do", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "buff.ly", "is.gd", "v.gd",
    "rebrand.ly", "cutt.ly", "shorturl.at", "tiny.cc", "rb.gy", "lnkd.in", "s.id", "bl.ink", "t.ly", "short.io",
    "trib.al", "dlvr.it", "soo.gd", "x.co", "adf.ly", "shorte.st", "clck.ru", "qr.ae", "po.st", "mcaf.ee", "su.pr",
    "y2u.be", "tr.im", "j.mp", "aka.ms", "1url.com", "hyperurl.co", "tiny.one", "shorturl.com", "urlz.fr",
    "linktr.ee", "zpr.io", "kutt.it", "gg.gg", "u.to", "ln.run",
})

# Names that only mean something on a local network (RFC 6761, RFC 6762, RFC 8375, ICANN's ".internal"), and the
# private-use names routers and companies give their machines.
LOCAL_SUFFIXES = ("localhost", "local", "localdomain", "lan", "home", "home.arpa", "internal", "intranet", "corp",
                  "private")

# What IDNA turns into a full stop (UTS #46): a name written with one of these is read as dotted.
_DOTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")
_SCHEME_PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_HOST_AND_PORT = re.compile(r"^[^:/?#]+:\d*(?:[/?#]|$)")  # "example.com:8080/jobs": a site and a port, no scheme
_ASCII_HOST = re.compile(r"^[a-z0-9_.-]+$")
_IPV4_PART = re.compile(r"^(?:0[xX][0-9a-fA-F]*|[0-9]+)$")
_SAME_SCRIPT = {"CJK": "CJK", "HIRAGANA": "CJK", "KATAKANA": "CJK", "HANGUL": "CJK", "BOPOMOFO": "CJK",
                "IDEOGRAPHIC": "CJK"}


@dataclass(frozen=True)
class Flag:
    code: str    # not_web, ip, local, credentials, shortener, lookalike, http, port
    reason: str  # plain words, for the person


@dataclass
class AddressCheck:
    url: str   # the address as it would be added (https:// in front when it had no scheme)
    host: str  # the site's name as the address gives it ("" when there is none)
    flags: list[Flag] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.code in BLOCKING for f in self.flags)

    def add(self, code: str, reason: str) -> None:
        if not any(f.code == code and f.reason == reason for f in self.flags):
            self.flags.append(Flag(code, reason))


def _ipv4_number(host: str) -> Optional[ipaddress.IPv4Address]:
    """The IPv4 address a host names in the forms browsers and C libraries accept ("127.0.0.1", "127.1",
    "2130706433", "0x7f.0.0.1", "0177.0.0.1"), None when its last label isn't a number. Raises ValueError for a host
    that ends in a number but isn't a valid address (a browser refuses it)."""
    parts = host.split(".")
    if not parts or not _IPV4_PART.match(parts[-1]):
        return None
    if len(parts) > 4:
        raise ValueError(host)
    numbers = []
    for part in parts:
        if not _IPV4_PART.match(part):
            raise ValueError(host)
        if part[:2].lower() == "0x":
            numbers.append(int(part[2:] or "0", 16))
        elif len(part) > 1 and part[0] == "0":
            if not set(part) <= set("01234567"):
                raise ValueError(host)
            numbers.append(int(part, 8))
        else:
            numbers.append(int(part, 10))
    *front, last = numbers
    if any(n > 255 for n in front) or last >= 256 ** (5 - len(numbers)):
        raise ValueError(host)
    value = last
    for i, n in enumerate(front):
        value += n << (8 * (3 - i))
    return ipaddress.IPv4Address(value)


def _not_public(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """True for an address of this computer, the local network or anything else not on the public internet
    (private, loopback, link-local, carrier-grade NAT, multicast, reserved), an IPv6 address that wraps one included."""
    inner = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        inner += [a for a in (ip.ipv4_mapped, ip.sixtofour, ip.teredo and ip.teredo[1]) if a]
        if ip in ipaddress.IPv6Network("64:ff9b::/96"):  # NAT64: the IPv4 address is in the last 32 bits
            inner.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return any(not a.is_global or a.is_multicast for a in inner)


def _scripts(label: str) -> set[str]:
    """The writing systems of a label's letters (digits, hyphens and the like don't count)."""
    out = set()
    for ch in label:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        first = name.split(" ")[0] if name else "UNKNOWN"
        out.add(_SAME_SCRIPT.get(first, first))
    return out


def address_flags(raw: str) -> AddressCheck:
    """What is unusual about an address, judged by its shape alone (no network use, no name look-up).

    Flags: ``not_web`` (not an http or https link, or not a valid one), ``ip`` (a raw IP address), ``local`` (the
    local network or this computer), ``credentials`` (sign-in details before an @), ``shortener`` (a link
    shortener), ``lookalike`` (punycode, letters from outside A to Z or mixed alphabets, percent-encoding in the
    name), ``http`` (plain http) and ``port`` (a port other than the usual one). ``not_web`` and ``local`` block the
    row (:data:`BLOCKING`)."""
    s = (raw or "").strip()
    check = AddressCheck(url=s, host="")
    if not s:
        check.add("not_web", "There is no address.")
        return check
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") or ch == "\\" for ch in s):
        check.add("not_web", "It has a space, a control character or a backslash, which a web address doesn't.")
        return check

    # The scheme: an address without one gets https://, as a single add does (sources.normalize_url).
    if "://" in s:
        scheme = s.split("://", 1)[0]
        if not _SCHEME.match(scheme):
            check.add("not_web", "It doesn't start with http:// or https://.")
            return check
    elif s.startswith("//"):
        s = "https:" + s
        scheme = "https"
    else:
        prefix = _SCHEME_PREFIX.match(s)
        if prefix and not _HOST_AND_PORT.match(s):  # mailto:, javascript:, data:, tel: ...
            name = prefix.group(1).lower()
            check.add("not_web", f"It needs // after {name}:." if name in ("http", "https")
                      else f"It is a “{name}:” address, not a web link.")
            return check
        s = "https://" + s
        scheme = "https"
    check.url = s
    scheme = scheme.lower()
    if scheme not in ("http", "https"):
        check.add("not_web", f"It is a “{scheme}:” address, not a web link (http or https).")
        return check

    try:
        parts = urlsplit(s)
    except ValueError:  # brackets that don't hold an IPv6 address, letters that fold into "/", "?" or "@"
        check.add("not_web", "Its site name is not a valid one.")
        return check
    netloc = parts.netloc
    if netloc.count("@") > 1:
        check.add("not_web", "It has more than one @ before the path, so programs disagree about which site it is.")
        return check
    try:
        port = parts.port
    except ValueError:
        check.add("not_web", "Its port isn't a number from 0 to 65535.")
        return check
    raw_host = parts.hostname or ""
    check.host = raw_host
    if not raw_host:
        check.add("not_web", "It has no site name.")
        return check

    # The name as a browser reads it: percent-encoding undone, letters folded (IDNA), the ideographic full stops
    # read as dots, one trailing dot dropped. The checks look at that name, so "%31%32%37.0.0.1" or "１２７.0.0.1"
    # count as 127.0.0.1.
    host = unicodedata.normalize("NFKC", unquote(raw_host)).translate(_DOTS).lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or ".." in host:
        check.add("not_web", "Its site name is not a valid one.")
        return check

    if any(ch.isascii() and not _ASCII_HOST.match(ch) for ch in host) and "[" not in netloc:
        check.add("not_web", "Its site name has characters a site name can't have.")
        return check
    if "@" in netloc:
        check.add("credentials", f"It carries sign-in details (a name or password before the @); the site is "
                                 f"really {raw_host}.")

    if "%" in raw_host:
        check.add("lookalike", "Its site name is percent-encoded (%), which hides what it says.")
    if not raw_host.isascii():
        mixed = [label for label in raw_host.split(".") if len(_scripts(label)) > 1]
        if mixed:
            names = sorted(_scripts(mixed[0]))
            check.add("lookalike", f"Its site name mixes alphabets ({', '.join(n.title() for n in names)}), a "
                                   f"way to imitate another name.")
        else:
            check.add("lookalike", "Its site name has letters from outside A to Z, which can look like others.")

    ip = None
    if "[" in netloc:  # [IPv6]
        try:
            ip = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            check.add("not_web", "Its IPv6 address is not a valid one.")
            return check
    else:
        try:
            ip = _ipv4_number(host)
        except ValueError:
            check.add("not_web", "Its site name looks like a numeric address but isn't a valid one.")
            return check
        if ip is None:
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                ip = None
    if ip is not None:
        check.add("ip", "It is a raw IP address, not a site name.")
        if _not_public(ip):
            check.add("local", "It is on your local network or this computer.")
    else:
        labels = host.split(".")
        if len(labels) == 1 or any(host == sfx or host.endswith("." + sfx) for sfx in LOCAL_SUFFIXES):
            check.add("local", "It is a name for your local network or this computer, not a public website.")
        if any(label.startswith("xn--") for label in labels):
            check.add("lookalike", "Its site name is encoded (punycode, “xn--”), which can hide letters that "
                                   "look like others.")
        bare = host.removeprefix("www.")
        if any(bare == s or bare.endswith("." + s) for s in SHORTENERS):
            check.add("shortener", "It is a link shortener, which hides where it leads.")

    if scheme == "http":
        check.add("http", "It uses plain http, so the page isn't encrypted on its way to you.")
    if port is not None and port != (443 if scheme == "https" else 80):
        check.add("port", f"It names an unusual port ({port}).")
    return check


# -- the file -------------------------------------------------------------------------------------------------------

class FormatError(ValueError):
    """The text as a whole can't be read; ``str(exc)`` is said to the person."""


@dataclass
class Row:
    line: int          # where the row starts in the text (1 is the first line)
    company: str
    url: str           # as written
    problem: str = ""  # why the row can't be read ("" when it can)


_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _unguard(cell: str) -> str:
    """Undoes the export's guard against spreadsheet formulas ("'=..." back to "=...")."""
    return cell[1:] if cell[:1] == "'" and cell[1:2] in _FORMULA else cell


def _guard(cell: str) -> str:
    """A cell a spreadsheet would run as a formula gets a ' in front (read back by :func:`_unguard`)."""
    return "'" + cell if cell[:1] in _FORMULA else cell


def one_line(name: str) -> str:
    """A company name on one line, without control characters."""
    return " ".join("".join(ch for ch in name if ch.isspace() or not unicodedata.category(ch).startswith("C")).split())


def parse_csv(text: str) -> list[Row]:
    """The rows of a pasted text or file in the bulk format. Blank lines are skipped; a first row that reads
    ``company,url`` (either order, any case) is the header and sets the column order. Raises FormatError when the
    text is too long or not CSV at all."""
    if len(text) > MAX_TEXT:
        raise FormatError(f"That is more than {MAX_TEXT // 1024} KB of text. Split it into smaller files.")
    text = text.removeprefix("\ufeff")
    if "\x00" in text:
        raise FormatError("That isn't a text file: it has a NUL character. Save it as CSV (UTF-8) and try again.")
    order = (0, 1)
    rows: list[Row] = []
    reader = csv.reader(io.StringIO(text, newline=""))
    start, first = 1, True
    try:
        for record in reader:
            line, start = start, reader.line_num + 1
            cells = [c.strip() for c in record]
            while cells and not cells[-1]:
                cells.pop()
            if not cells:
                continue
            if first:
                first = False
                lowered = tuple(c.lower() for c in cells)
                if len(lowered) == 2 and set(lowered) == set(HEADER):
                    order = (lowered.index("company"), lowered.index("url"))
                    continue
            if len(rows) >= MAX_ROWS:
                raise FormatError(f"That has more than {MAX_ROWS} rows. Split it into smaller files.")
            cells = [_unguard(c) for c in cells]
            if len(cells) > 2:
                rows.append(Row(line, one_line(cells[0]), "", f"It has {len(cells)} columns, not 2. Put a company "
                                                              f"name that contains a comma in double quotes."))
                continue
            if len(cells) == 1:
                only = cells[0]
                if "." not in only and ":" not in only:
                    rows.append(Row(line, one_line(only), "", "It has a company but no address."))
                    continue
                company, url = "", only
            else:
                company, url = one_line(cells[order[0]]), cells[order[1]]
            problem = ""
            if not url:
                problem = "It has no address."
            elif len(url) > MAX_URL:
                problem = f"Its address is longer than {MAX_URL} characters."
            elif len(company) > MAX_COMPANY:
                problem = f"Its company name is longer than {MAX_COMPANY} characters."
            rows.append(Row(line, company, url, problem))
    except csv.Error as exc:
        raise FormatError(f"Line {reader.line_num} can't be read as CSV ({exc}).") from None
    return rows


def to_csv(rows: list[tuple[str, str]]) -> str:
    """``(company, url)`` rows in the bulk format, with the header row, ready for :func:`parse_csv`."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for company, url in rows:
        writer.writerow([_guard(company or ""), _guard(url or "")])
    return out.getvalue()
