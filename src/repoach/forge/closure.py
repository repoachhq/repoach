"""The closure oracle of the forge recorder: a short module that refuses.

SP-RECORDER-REDACTS-BY-CLASS 0.17 (D3, D8). The recorder rewrites a
recording by class, but the guarantee never rests on that rewrite
recognising every shape a private value can take. This module judges the
built fixture with explicit sets and one short reader each: it refuses
when a known private value survives, when a token is host-shaped and is
neither a placeholder nor public, when an address is public and when a
mailbox is neither a placeholder nor a documentation literal.

It imports the standard library only and shares no pattern, function or
set with ``repoach.forge.recording``; the recorder hands it the known
values as data (:class:`Known`).
"""

from __future__ import annotations

import base64
import html
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import unquote

URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "ssh", "git", "ftp", "ftps"})
"""Schemes the convenience layer rewrites; any other ``<scheme>://`` leads a host as well."""

_ISO_COUNTRY_CODES = [
    "ad",
    "ae",
    "af",
    "ag",
    "ai",
    "al",
    "am",
    "ao",
    "aq",
    "ar",
    "as",
    "at",
    "au",
    "aw",
    "ax",
    "az",
    "ba",
    "bb",
    "bd",
    "be",
    "bf",
    "bg",
    "bh",
    "bi",
    "bj",
    "bl",
    "bm",
    "bn",
    "bo",
    "bq",
    "br",
    "bs",
    "bt",
    "bv",
    "bw",
    "by",
    "bz",
    "ca",
    "cc",
    "cd",
    "cf",
    "cg",
    "ch",
    "ci",
    "ck",
    "cl",
    "cm",
    "cn",
    "co",
    "cr",
    "cu",
    "cv",
    "cw",
    "cx",
    "cy",
    "cz",
    "de",
    "dj",
    "dk",
    "dm",
    "do",
    "dz",
    "ec",
    "ee",
    "eg",
    "eh",
    "er",
    "es",
    "et",
    "fi",
    "fj",
    "fk",
    "fm",
    "fo",
    "fr",
    "ga",
    "gb",
    "gd",
    "ge",
    "gf",
    "gg",
    "gh",
    "gi",
    "gl",
    "gm",
    "gn",
    "gp",
    "gq",
    "gr",
    "gs",
    "gt",
    "gu",
    "gw",
    "gy",
    "hk",
    "hm",
    "hn",
    "hr",
    "ht",
    "hu",
    "id",
    "ie",
    "il",
    "im",
    "in",
    "io",
    "iq",
    "ir",
    "is",
    "it",
    "je",
    "jm",
    "jo",
    "jp",
    "ke",
    "kg",
    "kh",
    "ki",
    "km",
    "kn",
    "kp",
    "kr",
    "kw",
    "ky",
    "kz",
    "la",
    "lb",
    "lc",
    "li",
    "lk",
    "lr",
    "ls",
    "lt",
    "lu",
    "lv",
    "ly",
    "ma",
    "mc",
    "md",
    "me",
    "mf",
    "mg",
    "mh",
    "mk",
    "ml",
    "mm",
    "mn",
    "mo",
    "mp",
    "mq",
    "mr",
    "ms",
    "mt",
    "mu",
    "mv",
    "mw",
    "mx",
    "my",
    "mz",
    "na",
    "nc",
    "ne",
    "nf",
    "ng",
    "ni",
    "nl",
    "no",
    "np",
    "nr",
    "nu",
    "nz",
    "om",
    "pa",
    "pe",
    "pf",
    "pg",
    "ph",
    "pk",
    "pl",
    "pm",
    "pn",
    "pr",
    "ps",
    "pt",
    "pw",
    "py",
    "qa",
    "re",
    "ro",
    "rs",
    "ru",
    "rw",
    "sa",
    "sb",
    "sc",
    "sd",
    "se",
    "sg",
    "sh",
    "si",
    "sj",
    "sk",
    "sl",
    "sm",
    "sn",
    "so",
    "sr",
    "ss",
    "st",
    "sv",
    "sx",
    "sy",
    "sz",
    "tc",
    "td",
    "tf",
    "tg",
    "th",
    "tj",
    "tk",
    "tl",
    "tm",
    "tn",
    "to",
    "tr",
    "tt",
    "tv",
    "tw",
    "tz",
    "ua",
    "ug",
    "um",
    "us",
    "uy",
    "uz",
    "va",
    "vc",
    "ve",
    "vg",
    "vi",
    "vn",
    "vu",
    "wf",
    "ws",
    "ye",
    "yt",
    "za",
    "zm",
    "zw",
]

_RESERVED_COUNTRY_CODES = ["ac", "eu", "su", "uk"]
"""Country-level codes outside ISO 3166-1 (exceptional reservations and retired codes) that name hosts."""

_GENERIC_TLDS = [
    "com",
    "net",
    "org",
    "edu",
    "gov",
    "mil",
    "int",
    "info",
    "biz",
    "name",
    "pro",
    "dev",
    "app",
    "cloud",
    "tech",
    "site",
    "online",
    "xyz",
    "wiki",
    "page",
    "live",
    "run",
    "team",
    "works",
    "zone",
    "network",
    "systems",
    "services",
    "solutions",
    "software",
    "engineering",
    "digital",
    "agency",
    "studio",
    "host",
    "hosting",
    "domains",
    "email",
    "codes",
    "tools",
    "build",
    "one",
    "world",
    "global",
    "group",
    "company",
    "ltd",
    "inc",
    "llc",
    "gmbh",
]

_PRIVATE_USE_TLDS = [
    "internal",
    "local",
    "lan",
    "intranet",
    "home",
    "corp",
    "localdomain",
    "test",
    "example",
    "invalid",
    "localhost",
]

EXTENSION_TLDS: frozenset[str] = frozenset(
    {
        "ac",
        "am",
        "cc",
        "cr",
        "cu",
        "gd",
        "in",
        "md",
        "mk",
        "ml",
        "mm",
        "pl",
        "pm",
        "pro",
        "ps",
        "py",
        "rs",
        "sc",
        "sh",
        "so",
        "st",
        "sv",
        "tf",
    }
)
"""Country and generic codes that are also the extension of a source, build or binary file.

A compiler or a linter cites a three-label file name as ``<file>:<line>``
(``settings.local.py:10``, ``config.h.in:5``, ``kernel/foo.bar.cu:12:3``,
``core.alu.sv:33``), the one follow shape a file name shares with a host;
un-led, such a name is a file, not a host. The class: autoconf and
automake (``ac``, ``am``, ``in``, ``mk``), C++ and CUDA (``cc``, ``cu``),
Crystal (``cr``), GDScript (``gd``), Markdown (``md``), OCaml (``ml``),
Objective-C++ (``mm``), Perl (``pl``, ``pm``), Qt and Prolog (``pro``),
PostScript (``ps``), Python (``py``), Rust (``rs``), Scala scripts
(``sc``), shell (``sh``), shared objects (``so``), Smalltalk (``st``),
SystemVerilog (``sv``), Terraform (``tf``).
"""

HOST_TLDS: frozenset[str] = (
    frozenset(_ISO_COUNTRY_CODES)
    | frozenset(_RESERVED_COUNTRY_CODES)
    | frozenset(_GENERIC_TLDS)
    | frozenset(_PRIVATE_USE_TLDS)
) - EXTENSION_TLDS
"""The explicit list of last labels that make a run of three labels a host."""

REVERSE_DNS_HEADS: frozenset[str] = frozenset(
    {"com", "org", "net", "io", "dev", "me", "co", "edu", "gov"}
)
"""First labels of Java, Android and Maven names (``com.example.app``): never a host un-led."""

LOCAL_CHARS = (
    "._%+-:${}" + "abcdefghijklmnopqrstuvwxyz" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "0123456789"
)
"""ASCII characters of a mailbox's local part, ``:`` and ``${}`` included so a credential is read as one.

Any Unicode letter or digit is one as well (RFC 6531, read by :func:`_local_before`), so
``josé@<host>`` is a mailbox. No other ASCII symbol is added: the local part decides whether a
mailbox is a placeholder (``git@``), and a ``key=git@`` or ``**git@`` must stay ``git``.
"""

IPV4_RESERVED = tuple(
    ipaddress.IPv4Network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
"""Private, loopback, documentation and reserved networks: an address in one is not public."""

_RFC5737 = tuple(
    ipaddress.IPv4Network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)

_UNSPECIFIED_BROADCAST = (
    ipaddress.IPv4Address("0.0.0.0"),
    ipaddress.IPv4Address("255.255.255.255"),
)
"""The unspecified and the limited-broadcast address (RFC 1122): a bind or a log line, never a machine."""

_IPV6_DOCUMENTATION = tuple(ipaddress.IPv6Network(n) for n in ("2001:db8::/32", "3fff::/20"))
"""The IPv6 documentation prefixes (RFC 3849, RFC 9637): a literal in one names no one."""

_COMPARATORS = ("==", ">=", "<=", "~=", "!=")

PUBLIC_SUFFIXES: frozenset[str] = frozenset(
    {
        "apache.org",
        "cloudflare.com",
        "crates.io",
        "docker.io",
        "docs.github.com",
        "docs.gitlab.com",
        "docs.rs",
        "ghcr.io",
        "go.dev",
        "golang.org",
        "jsdelivr.net",
        "k8s.io",
        "mend.io",
        "mozilla.org",
        "nuget.org",
        "npmjs.com",
        "npmjs.org",
        "npmmirror.com",
        "pypi.org",
        "pythonhosted.org",
        "python.org",
        "readthedocs.io",
        "readthedocs.org",
        "renovatebot.com",
        "stackoverflow.com",
        "unpkg.com",
        "wikipedia.org",
        "yarnpkg.com",
    }
)
"""Registrable suffixes of public documentation, package and CDN hosts (option A; 0.22: the yarn and npmmirror registries)."""

_CLASS_WORD_LOCAL = re.compile(r"(?:maintainer|author|bot|agent|service)(?:-[1-9][0-9]*)?")

_DOC_MAILBOX_HOST = re.compile(
    r"^example\.(com|org|net)$|\.(invalid|test|example|localhost|internal)$|^localhost$"
)

_RUN = re.compile(r"[A-Za-z0-9.-]+")

_BRACKETED = re.compile(r"\[([0-9a-f:.]{2,45})(?:%(?:25)?[A-Za-z0-9._~-]+)?\]", re.I)

_HEX = frozenset("0123456789abcdefABCDEF")

_DIGITS = frozenset("0123456789")

_SEGMENT_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.~%")

_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")

_SCHEME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "0123456789" + "+.-"
)
"""The characters of a URL scheme (RFC 3986 ``ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )``)."""

_STRING_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})|\\(/)")
"""The string-literal escapes of JSON and the C family that hide a delimiter: ``\\uXXXX``, ``\\xHH`` and ``\\/``."""

_DECODE_ROUNDS = 4

_NEW_NODE = re.compile(r"(?=(U_[-A-Za-z0-9_]{2,}))")

_LEGACY_NODE = re.compile(r"(?=(MDQ6VXNl[+/A-Za-z0-9]*={0,2}))")

_LEGACY_DECODED = re.compile(r"^04:User(\d+)")

_MSGPACK_WIDTH = {0xCC: 1, 0xCD: 2, 0xCE: 4, 0xCF: 8}

_NODE_LENGTHS = (4, 6, 7, 10, 15)

_NODE_DIGITS = 20
"""The most digits a synthetic ``U_<n>`` node id can carry (a 64-bit number); a longer run is no id and never an exception."""

KINDS: tuple[str, ...] = ("login", "host", "segment", "id", "name", "mailbox")
"""The kinds of known value the recorder replaces, in the order they are judged."""

JUDGED_KINDS: tuple[str, ...] = (*KINDS, "deny-list")
"""Every kind :func:`residual` judges: the replaced ones, then the deny-list's, which is never replaced."""

_ALNUM = "A-Za-z0-9"


def token_pattern(word: str) -> re.Pattern[str]:
    """``word`` as a whole token, casefolded: letters and digits are inside, everything else delimits.

    A ``%2F`` or ``%2f`` before the word is a delimiter as well, since
    ``projects/<ns>%2F<repo>`` is the one URL-encoded shape the forges'
    own argv carries.
    """
    return re.compile(
        rf"(?:(?<![{_ALNUM}])|(?<=%2[Ff])){re.escape(word)}(?![{_ALNUM}])", re.IGNORECASE
    )


@dataclass(frozen=True)
class Host:
    """A host-shaped token of a text.

    Attributes:
        host: The host as written, without its trailing dot.
        led: Whether a scheme ``://``, a ``<local>@`` or, for an address, ``//`` stands right before it;
            an address that is only followed is not led.
        start: Offset of the first character.
        end: Offset after the last character.
        literal: Whether it is a bracketed IPv6 literal, ``host`` then holding the brackets.
    """

    host: str
    led: bool
    start: int
    end: int
    literal: bool = False


@dataclass(frozen=True)
class Mailbox:
    """A ``<local>@<host>`` of a text.

    Attributes:
        local: The local part.
        host: The host after the ``@``, without its trailing dot.
        start: Offset of the first character of the local part.
        end: Offset after the host.
    """

    local: str
    host: str
    start: int
    end: int


@dataclass(frozen=True)
class Refusal:
    """What the oracle found, never a value.

    Attributes:
        reason: ``residual``, ``unknown host``, ``public address`` or ``unknown mailbox``.
        kind: The kind of the known value for a ``residual``, else ``""``.
        n: The ordinal of the known value within its kind, else ``0``.
    """

    reason: str
    kind: str = ""
    n: int = 0


@dataclass(frozen=True)
class Known:
    """The private values a built fixture must not hold, casefolded, by kind.

    Opaque: its ``repr`` is a count, so no value reaches a log, a
    traceback or ``pytest -l``.

    Attributes:
        entries: ``(kind, casefolded value)`` in order, each once.
        literals: Tool and project literals blanked before judging.
        tokens: ``(casefolded value, class token)`` the recorder replaces it by.
    """

    entries: tuple[tuple[str, str], ...]
    literals: tuple[str, ...] = ()
    tokens: tuple[tuple[str, str], ...] = ()

    def __repr__(self) -> str:
        """A count, never a value."""
        return f"<known: {len(self.entries)}>"

    def __len__(self) -> int:
        """How many values."""
        return len(self.entries)

    def mapping(self) -> dict[str, str]:
        """Casefolded value -> the class token it is replaced by."""
        return dict(self.tokens)

    def values(self, kind: str) -> tuple[str, ...]:
        """The values of one kind, in order."""
        return tuple(v for k, v in self.entries if k == kind)


def _canonical_host(host: str) -> str:
    return host.rstrip(".").casefold()


def is_placeholder_host(host: str) -> bool:
    """Whether ``host`` names no one: RFC 2606, loopback, unspecified, broadcast, RFC 5737, the placeholder."""
    folded = _canonical_host(host)
    if folded == "localhost":
        return True
    for base in ("example.com", "example.org", "example.net", "example", "test", "invalid"):
        if folded == base or folded.endswith("." + base):
            return True
    address = _ipv4(folded)
    if address is not None:
        return (
            address in ipaddress.IPv4Network("127.0.0.0/8")
            or address in _UNSPECIFIED_BROADCAST
            or any(address in n for n in _RFC5737)
        )
    literal = _ipv6(folded)
    return literal is not None and (
        literal.is_loopback
        or literal.is_unspecified
        or any(literal in n for n in _IPV6_DOCUMENTATION)
    )


def is_public_host(host: str) -> bool:
    """Whether ``host`` is, or is under, a registrable suffix of :data:`PUBLIC_SUFFIXES`."""
    folded = _canonical_host(host)
    return any(folded == s or folded.endswith("." + s) for s in PUBLIC_SUFFIXES)


def is_doc_mailbox_host(host: str) -> bool:
    """Whether a mailbox on ``host`` names no one (a documentation domain)."""
    return _DOC_MAILBOX_HOST.search(_canonical_host(host)) is not None


def _ipv4(text: str) -> ipaddress.IPv4Address | None:
    parts = text.split(".")
    if len(parts) != 4 or not all(p.isascii() and p.isdigit() and len(p) <= 3 for p in parts):
        return None
    if any(int(p) > 255 for p in parts):
        return None
    return ipaddress.IPv4Address(".".join(str(int(p)) for p in parts))


def _ipv6(text: str) -> ipaddress.IPv6Address | None:
    """``text`` as an IPv6 address in hex groups, its brackets dropped, else ``None``; never raises.

    A dotted tail is read through :func:`_ipv4` and the address is built
    from its canonical form, so ``::ffff:01.2.3.4`` is ``::ffff:1.2.3.4``
    rather than an exception.
    """
    body = text.strip("[]")
    leading, colon, mapped = body.rpartition(":")
    shaped_body = body
    if "." in mapped:
        tail_address = _ipv4(mapped)
        if tail_address is None:
            return None
        body = f"{leading}{colon}{tail_address}"
        shaped_body = f"{leading}{colon}0:0"
    head, gap, tail = shaped_body.partition("::")
    groups = [*(head.split(":") if head else []), *(tail.split(":") if tail else [])]
    shaped = all(0 < len(g) <= 4 and all(c in _HEX for c in g) for g in groups)
    fits = len(groups) < 8 if gap else len(groups) == 8
    return ipaddress.IPv6Address(body) if shaped and fits else None


def _bracketed(text: str) -> list[tuple[int, int, ipaddress.IPv6Address]]:
    """Every ``[<IPv6 address>]`` of ``text`` as ``(start, end, address)``."""
    found = []
    for m in _BRACKETED.finditer(text):
        address = _ipv6(m.group(1))
        if address is not None:
            found.append((m.start(), m.end(), address))
    return found


def _label_run(run: str) -> list[str] | None:
    body = run[:-1] if run.endswith(".") else run
    labels = body.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    last = labels[-1]
    if len(last) < 2 or not (last.isascii() and last.isalpha()):
        return None
    return labels


def _local_before(text: str, at: int) -> str:
    """The run of local-part characters right before the ``@`` at ``at``; empty unless it holds a letter or digit.

    A local-part character is one of :data:`LOCAL_CHARS` or any Unicode letter or digit.
    """
    begin = at
    while begin > 0 and (text[begin - 1] in LOCAL_CHARS or text[begin - 1].isalnum()):
        begin -= 1
    local = text[begin:at]
    return local if any(c.isalnum() for c in local) else ""


def _scheme_before(text: str, end: int) -> bool:
    """Whether a scheme ends right at ``end``: a run of :data:`_SCHEME_CHARS` holding a letter."""
    begin = end
    while begin > 0 and text[begin - 1] in _SCHEME_CHARS:
        begin -= 1
    return any(c.isalpha() for c in text[begin:end])


def _led(text: str, start: int) -> bool:
    """Whether ``<scheme>://``, ``<local>@`` or a URL's userinfo stands right before offset ``start``.

    A userinfo is whatever runs from ``<scheme>://`` to the ``@`` with no space and no ``/``, so
    ``https://u:abc=@<host>`` leads its host whatever the password ends with. Reads back, never copies.
    """
    if text.endswith("://", 0, start) and _scheme_before(text, start - 3):
        return True
    if not text.endswith("@", 0, start):
        return False
    return _local_before(text, start - 1) != "" or _userinfo_before(text, start - 1)


def _userinfo_before(text: str, at: int) -> bool:
    """Whether the ``@`` at ``at`` ends a URL's userinfo: ``<scheme>://`` then no space and no ``/``."""
    begin = at
    while begin > 0 and not text[begin - 1].isspace() and text[begin - 1] != "/":
        begin -= 1
    return text.endswith("://", 0, begin) and _scheme_before(text, begin - 3)


def _runs(text: str) -> list[tuple[int, int, str]]:
    found = []
    for m in _RUN.finditer(text):
        begin, run = m.start(), m.group(0)
        if text[begin - 1 : begin] == "%" and run[:1] in _HEX and run[1:2] in _HEX:
            begin, run = begin + 2, run[2:]
        lead = len(run) - len(run.lstrip("-."))
        stripped = run[lead:].rstrip("-")
        if stripped:
            found.append((begin + lead, begin + lead + len(stripped), stripped))
    return found


def _version_context(text: str, start: int, end: int) -> bool:
    """Whether the quad at ``start:end`` stands in a version position, never an address.

    A path segment (a single ``/`` right before it, not ``//``:
    ``/ojdbc8/19.3.0.0/``), a comparator operand with any spaces
    (``pkg  >=  1.2.3.4``), a ``version`` label's value after any run of
    label separators (``Version: 1.2.3.4``, ``version = 1.2.3.4``), or a
    quad carrying a pre-release or build tag (``-``, ``+`` right after).
    """
    if text.endswith("/", 0, start) and not text.endswith("//", 0, start):
        return True
    unspaced = start
    while unspaced > 0 and text[unspaced - 1] in " \t":
        unspaced -= 1
    if text.endswith(_COMPARATORS, 0, unspaced):
        return True
    stem = start
    while stem > 0 and text[stem - 1] in " \t:=":
        stem -= 1
    labelled = text[max(0, stem - 7) : stem].casefold() == "version"
    return labelled or text[end : end + 1] in ("-", "+")


def _followed(text: str, end: int) -> bool:
    """Whether ``/<segment>`` or ``:<port digits>`` stands right after offset ``end``."""
    mark, after = text[end : end + 1], text[end + 1 : end + 2]
    return (mark == "/" and after in _SEGMENT_START) or (mark == ":" and after in _DIGITS)


def hosts(text: str) -> list[Host]:
    """Every host-shaped token of ``text``, in order: the one rule that makes a name a host.

    A dotted name is a host only when it is **led** or **followed**, and
    never otherwise, whatever its last label. Led by a scheme ``://`` or
    by ``<local>@``, a label run of two or more labels is a host. Led by
    ``//`` or followed by ``/<segment>`` or ``:<port digits>``, it is a
    host when it has three labels or more, a last label of
    :data:`HOST_TLDS` and a first label that is not a reverse-DNS head.
    A dotted IPv4 address follows the same rule whatever its octets: it is
    a host when it is led by a scheme, a ``<local>@`` or ``//``, or followed
    by ``/<segment>`` or ``:<port digits>``, outside a version context
    (:func:`_version_context`); a bare quad in prose is ordinary text. A
    quad right after a single ``/`` (not ``//``) is a path segment, a
    version context (``/ojdbc8/19.3.0.0/``), so ``1.2.3.4/24`` is still an
    address (followed) and ``x /8.8.8.8/y`` is not. A bracketed IPv6
    address, with or without a zone id, is a host when it is led.
    """
    found: list[Host] = []
    for start, end, run in _runs(text):
        led = _led(text, start)
        slashes = text.endswith("//", 0, start)
        address = _ipv4(run.rstrip("."))
        if (
            address is not None
            and (led or slashes or _followed(text, end))
            and not _version_context(text, start, end)
        ):
            found.append(Host(run.rstrip("."), led or slashes, start, end))
        labels = _label_run(run)
        host = run[:-1] if run.endswith(".") else run
        if labels is None:
            continue
        if led:
            found.append(Host(host, True, start, end))
        elif (
            len(labels) >= 3
            and labels[-1].casefold() in HOST_TLDS
            and labels[0].casefold() not in REVERSE_DNS_HEADS
            and (slashes or _followed(text, end))
        ):
            found.append(Host(host, False, start, end))
    for start, end, _address in _bracketed(text):
        if _led(text, start) or text.endswith("//", 0, start):
            found.append(Host(text[start:end], True, start, end, literal=True))
    return found


def is_address(host: str) -> bool:
    """Whether ``host`` is a dotted IPv4 address."""
    return _ipv4(_canonical_host(host)) is not None


def is_public_address(host: str) -> bool:
    """Whether ``host`` is a dotted IPv4 address outside every reserved network."""
    address = _ipv4(_canonical_host(host))
    return address is not None and not any(address in n for n in IPV4_RESERVED)


def public_addresses(text: str) -> list[str]:
    """Every un-led public bracketed IPv6 address of ``text``.

    A literal needs more than two colons: ``xs[2020::]`` and ``xs[::2]``
    are slices, and a slice holds two at most. A public IPv4 address is
    judged as a host (:func:`hosts`, :func:`judge`), never here.
    """
    found = []
    for start, end, address in _bracketed(text):
        literal = text[start:end]
        led = _led(text, start) or text.endswith("//", 0, start)
        if not led and address.is_global and literal.count(":") > 2:
            found.append(literal)
    return found


def mailboxes(text: str) -> list[Mailbox]:
    """Every ``<local>@<host>`` of ``text``, ``local`` read by :func:`_local_before`, ``host`` a label run or an IPv4."""
    found = []
    for at in (i for i, c in enumerate(text) if c == "@"):
        local = _local_before(text, at)
        begin = at - len(local)
        m = _RUN.match(text, at + 1)
        if m is None:
            continue
        run = m.group(0).rstrip("-")
        body = run[:-1] if run.endswith(".") else run
        if _label_run(run) is None and _ipv4(body) is None:
            continue
        if local.casefold().startswith("mailto:"):
            begin += 7
            local = local[7:]
        if any(c.isalnum() for c in local):
            found.append(Mailbox(local, body, begin, at + 1 + len(body)))
    return found


def is_literal_mailbox(box: Mailbox) -> bool:
    """Whether ``box`` is a placeholder or a documentation literal rather than a personal mailbox.

    A placeholder-class address (loopback, unspecified, broadcast, RFC
    5737) is a documentation host for a mailbox too: ``root@127.0.0.1``
    names no one, like ``root@localhost``. ``forge.example.com`` is not
    (NG4-5): only ``git@`` and the noreply placeholders stand on it.
    """
    local, host = box.local.casefold().lstrip("._%+-$"), _canonical_host(box.host)
    if local == "git" and is_placeholder_host(host):
        return True
    if is_address(host) and is_placeholder_host(host):
        return ":" not in local
    if host == "forge.example.com" and local in ("noreply", "no-reply"):
        return True
    suffix = ".users.noreply.forge.example.com"
    if ("." + host) == suffix and _CLASS_WORD_LOCAL.fullmatch(local):
        return True
    return ":" not in local and is_doc_mailbox_host(host)


def _escaped_char(match: re.Match[str]) -> str:
    code = match.group(1) or match.group(2)
    return chr(int(code, 16)) if code else "/"


def _unescape_once(text: str) -> str:
    return unquote(html.unescape(_STRING_ESCAPE.sub(_escaped_char, text)))


def decoded_forms(text: str) -> list[str]:
    r"""``text`` and each layer of string-escape, HTML-entity and percent encoding peeled off, up to four.

    The string-escape layer is ``\uXXXX``, ``\xHH`` and ``\/``: the JSON
    and C-family escapes a pasted payload carries (PHP and Go encode ``/``
    as ``\/``), so ``http:\/\/<host>\/<ns>`` is judged as its plain form.
    """
    forms = [text]
    current = text
    for _ in range(_DECODE_ROUNDS):
        peeled = _unescape_once(current)
        if peeled == current:
            break
        forms.append(peeled)
        current = peeled
    return forms


def _new_node_number(suffix: str) -> int | None:
    raw = base64.urlsafe_b64decode(suffix + "=" * (-len(suffix) % 4))
    if raw[:2] != b"\x92\x00":
        return None
    marker, body = raw[2], raw[3:]
    if marker < 0x80 and not body:
        return marker
    width = _MSGPACK_WIDTH.get(marker)
    if width is None or len(body) != width:
        return None
    return int.from_bytes(body, "big")


def _legacy_number(run: str) -> int | None:
    body = run[:40].rstrip("=")
    if len(body) % 4 == 1:
        body = body[:-1]
    decoded = base64.b64decode(body + "=" * (-len(body) % 4)).decode("ascii", errors="replace")
    match = _LEGACY_DECODED.match(decoded)
    return int(match.group(1)) if match else None


def node_id_numbers(text: str) -> list[str]:
    """The account number of every user node id ``text`` holds, wherever it stands in a longer run."""
    numbers: list[str] = []
    for m in _NEW_NODE.finditer(text):
        run = m.group(1)
        suffix = run[2:]
        if suffix.isdigit():
            found = [int(suffix)] if len(suffix) <= _NODE_DIGITS else []
        else:
            found = [
                n
                for k in _NODE_LENGTHS
                if k <= len(suffix)
                if (n := _new_node_number(suffix[:k])) is not None
            ]
        for number in found:
            if str(number) not in numbers:
                numbers.append(str(number))
    for m in _LEGACY_NODE.finditer(text):
        legacy = _legacy_number(m.group(1))
        if legacy is not None and str(legacy) not in numbers:
            numbers.append(str(legacy))
    return numbers


_LITERAL_PATTERNS: dict[tuple[str, ...], re.Pattern[str]] = {}


def _literal_pattern(literals: tuple[str, ...]) -> re.Pattern[str]:
    if literals not in _LITERAL_PATTERNS:
        alternation = "|".join(
            f"(?i:{re.escape(x)})" if x.startswith("docs.") else re.escape(x) for x in literals
        )
        _LITERAL_PATTERNS[literals] = re.compile(
            rf"(?<![{_ALNUM}])(?:{alternation})(?![{_ALNUM}-])"
        )
    return _LITERAL_PATTERNS[literals]


def _blank(text: str, literals: tuple[str, ...]) -> str:
    if not literals:
        return text
    return _literal_pattern(literals).sub(" ", text)


def forms_of(text: str, *, literals: tuple[str, ...] = ()) -> list[str]:
    """Every form ``text`` is judged in, casefolded: decoded layers and node-id account numbers."""
    forms: list[str] = []
    for layer in decoded_forms(_blank(text, literals)):
        for form in (layer.casefold(), *node_id_numbers(layer)):
            if form not in forms:
                forms.append(form)
    return forms


def residual(text: str, known: Known) -> tuple[str, int] | None:
    """The first known value ``text`` still holds, as ``(kind, ordinal)``, or ``None``."""
    forms = forms_of(text, literals=known.literals)
    for kind in JUDGED_KINDS:
        for n, value in enumerate(known.values(kind), 1):
            pattern = token_pattern(value)
            if any(pattern.search(form) for form in forms):
                return kind, n
    return None


def judge(text: str, known: Known) -> Refusal | None:
    """What the oracle refuses in ``text``: a known value, an unknown host, a public address or an unknown mailbox."""
    left = residual(text, known)
    if left is not None:
        return Refusal("residual", left[0], left[1])
    forms = decoded_forms(_blank(text, known.literals))
    for place in (h for form in forms for h in hosts(form)):
        if is_placeholder_host(place.host) or is_public_host(place.host):
            continue
        if place.led or not is_address(place.host):
            return Refusal("unknown host")
        if is_public_address(place.host):
            return Refusal("public address")
    if any(public_addresses(form) for form in forms):
        return Refusal("public address")
    if any(not is_literal_mailbox(b) for form in forms for b in mailboxes(form)):
        return Refusal("unknown mailbox")
    return None
