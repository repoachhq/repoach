"""The closure oracle of the forge recorder: a short module that refuses.

SP-RECORDER-REDACTS-BY-CLASS 0.17 (D3, D8). The recorder rewrites a
recording by class, but the guarantee never rests on that rewrite
recognising every shape a private value can take. This module judges the
built fixture with explicit sets and one short reader each: it refuses
when a known private value survives, when a token is host-shaped and is
neither a placeholder nor public, when an address is public and when a
mailbox is neither a placeholder nor a documentation literal.

It imports the standard library and its own tables
(:mod:`repoach.forge.closure_tables`) only, and shares no pattern,
function or set with ``repoach.forge.recording``; the recorder hands it
the known values as data (:class:`Known`).
"""

from __future__ import annotations

import base64
import html
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import unquote

from .closure_tables import (
    HOST_TLDS,
    IPV4_RESERVED,
    IPV6_DOCUMENTATION,
    PUBLIC_HOSTS,
    PUBLIC_SUFFIXES,
    REVERSE_DNS_HEADS,
    RFC5737_NETWORKS,
    TENANT_ROUTES,
    UNSPECIFIED_BROADCAST,
)

LOCAL_CHARS = (
    "._%+-:${}" + "abcdefghijklmnopqrstuvwxyz" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "0123456789"
)
"""ASCII characters of a mailbox's local part, ``:`` and ``${}`` included so a credential is read as one.

Any Unicode letter or digit is one as well (RFC 6531, read by :func:`_local_before`), so
``josé@<host>`` is a mailbox. No other ASCII symbol is added: the local part decides whether a
mailbox is a placeholder (``git@``), and a ``key=git@`` or ``**git@`` must stay ``git``.
"""

_COMPARATORS = ("==", ">=", "<=", "~=", "!=")

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

_USERINFO_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    + "0123456789"
    + "-._~"
    + "%"
    + "!$&'()*+,;="
    + ":"
    + "{}"
)
"""The ASCII characters of a URL userinfo (RFC 3986 unreserved, ``%``, sub-delims, ``:``) and ``{}``.

``{}`` makes a ``${TOKEN}`` template one userinfo; any Unicode letter or digit is one as well (an
IRI). A ``<``, a ``>``, a quote or a space is none, so ``<https://<host>><a@<host>>`` is prose.
"""

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
        reason: ``residual``, ``tenant path``, ``unknown host``, ``public address``,
            ``unknown mailbox`` or ``url userinfo``.
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
            or address in UNSPECIFIED_BROADCAST
            or any(address in n for n in RFC5737_NETWORKS)
        )
    literal = _ipv6(folded)
    return literal is not None and (
        literal.is_loopback
        or literal.is_unspecified
        or any(literal in n for n in IPV6_DOCUMENTATION)
    )


def is_public_host(host: str) -> bool:
    """Whether ``host`` is one of :data:`PUBLIC_HOSTS`, or is or is under a suffix of :data:`PUBLIC_SUFFIXES`.

    Args:
        host: A host name, in any case, with or without its trailing dot.

    Returns:
        ``True`` for a public documentation, package or CDN host.
    """
    folded = _canonical_host(host)
    if folded in PUBLIC_HOSTS:
        return True
    return any(folded == s or folded.endswith("." + s) for s in PUBLIC_SUFFIXES)


def is_tenant_path(host: str, path: str) -> bool:
    """Whether ``path`` on the public ``host`` opens a tenant surface (:data:`TENANT_ROUTES`).

    Args:
        host: A public host, in any case.
        path: The URL path after the host, its leading ``/`` included or not.

    Returns:
        ``True`` when the first segment of ``path`` is a tenant route of ``host``'s suffix and a
        segment follows it (``stackoverflow.com/c/<team>``).
    """
    folded = _canonical_host(host)
    segments = path.lstrip("/").split("/")
    if len(segments) < 2 or not segments[1]:
        return False
    return any(
        (folded == suffix or folded.endswith("." + suffix)) and segments[0].casefold() in routes
        for suffix, routes in TENANT_ROUTES.items()
    )


def _path_after(text: str, end: int) -> str:
    """The URL path right after offset ``end``: from a ``/`` to the next space, ``?`` or ``#``."""
    if text[end : end + 1] != "/":
        return ""
    stop = end
    while stop < len(text) and not text[stop].isspace() and text[stop] not in "?#\"'<>":
        stop += 1
    return text[end:stop]


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


_PUNYCODE_LABEL = re.compile(r"xn--[a-z0-9-]+", re.I)
"""An IDNA A-label (RFC 5890): a top-level domain such as ``xn--p1ai`` is a last label."""


def _label_run(run: str) -> list[str] | None:
    """The labels of a dotted host name, its last label letters or an IDNA ``xn--`` label, else ``None``."""
    body = run[:-1] if run.endswith(".") else run
    labels = body.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    last = labels[-1]
    if _PUNYCODE_LABEL.fullmatch(last):
        return labels
    if len(last) < 2 or not (last.isascii() and last.isalpha()):
        return None
    return labels


def _dotless_authority(text: str, start: int, host: str) -> bool:
    """Whether ``host`` at ``start`` is a one-label host: right after ``<scheme>://``, the whole authority.

    ``https://gitlab/ns/repo`` names a machine; ``ssh://git@<host>`` does not name ``git``, so an
    authority holding an ``@`` (a userinfo) is never read here.
    """
    if "." in host or not _LABEL.fullmatch(host):
        return False
    if not (text.endswith("://", 0, start) and _scheme_before(text, start - 3)):
        return False
    end = start
    while end < len(text) and not text[end].isspace() and text[end] not in "/?#":
        if text[end] == "@":
            return False
        end += 1
    return True


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


def _userinfo_start(text: str, at: int) -> int | None:
    """Where the URL userinfo the ``@`` at ``at`` ends begins: ``<scheme>://`` then no space, ``/`` or ``@``.

    A userinfo holds no unescaped ``@`` (RFC 3986), so the scan back stops at the previous one:
    each character is read back once over all the ``@`` of a text, and the reader stays linear.

    Args:
        text: The text read.
        at: The offset of an ``@`` of ``text``.

    Returns:
        The offset right after ``<scheme>://``, or ``None`` when no URL userinfo ends at ``at``.
    """
    begin = at
    while begin > 0 and not text[begin - 1].isspace() and text[begin - 1] not in "/@":
        begin -= 1
    if text.endswith("://", 0, begin) and _scheme_before(text, begin - 3):
        return begin
    return None


def _userinfo_before(text: str, at: int) -> bool:
    """Whether the ``@`` at ``at`` ends a URL's userinfo (:func:`_userinfo_start`)."""
    return _userinfo_start(text, at) is not None


def userinfos(text: str) -> list[Mailbox]:
    """Every URL userinfo of ``text`` with the host after it, in order, as a :class:`Mailbox`.

    The userinfo is what stands between ``<scheme>://`` and its ``@``, read whatever it ends with
    and whatever the host is: ``https://u:abc=@pypi.org/`` holds ``u:abc=``, which no mailbox
    reader sees (``=`` ends no local part) on a public host, and ``postgres://app:pw@localhost``
    holds ``app:pw`` on a one-label host, which is no mailbox host. A run holding a character
    outside :data:`_USERINFO_CHARS` is no userinfo. The host is the label run or the bracketed
    literal right after the ``@``, ``""`` when none stands there.

    Args:
        text: The text read.

    Returns:
        Each userinfo as ``Mailbox(local=<userinfo>, host=<host>)``, never decoded here
        (:func:`judge` reads every decoded form).
    """
    found = []
    for at in (i for i, c in enumerate(text) if c == "@"):
        begin = _userinfo_start(text, at)
        if begin is None:
            continue
        userinfo = text[begin:at]
        if not all(c in _USERINFO_CHARS or c.isalnum() for c in userinfo):
            continue
        bracket = _BRACKETED.match(text, at + 1)
        run = bracket or _RUN.match(text, at + 1)
        host = run.group(0).rstrip(".-") if run is not None else ""
        found.append(Mailbox(userinfo, host, begin, at + 1 + len(host)))
    return found


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


def _port_after(text: str, end: int) -> bool:
    """Whether ``:<port digits>`` stands right after offset ``end``."""
    return text[end : end + 1] == ":" and text[end + 1 : end + 2] in _DIGITS


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
    host when it has three labels or more and a last label of
    :data:`HOST_TLDS`; followed by ``/<segment>`` only, its first label must
    also not be a reverse-DNS head (:data:`REVERSE_DNS_HEADS`).
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
            if _dotless_authority(text, start, host):
                found.append(Host(host, True, start, end))
            continue
        if led:
            found.append(Host(host, True, start, end))
        elif (
            len(labels) >= 3
            and labels[-1].casefold() in HOST_TLDS
            and (slashes or _followed(text, end))
            and (slashes or _port_after(text, end) or labels[0].casefold() not in REVERSE_DNS_HEADS)
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
    """What the oracle refuses in ``text``: a known value, a tenant path, an unknown host, a public address, an unknown mailbox or a URL userinfo.

    A public host followed by a tenant route (:func:`is_tenant_path`) is refused as a
    ``tenant path``: the host is public, the customer's surface after it is not.

    A URL userinfo (:func:`userinfos`) is judged as the mailbox it spells, ``<userinfo>@<host>``
    (:func:`is_literal_mailbox`): ``git`` on a placeholder-class host and a password-less user on a
    documentation host (``postgres://app@localhost``) name no one; a credential (a ``:``) and any
    userinfo on a public or an unknown host are refused, whatever character ends them.

    Args:
        text: One string of the built fixture.
        known: The recording's known values and literals.

    Returns:
        The first refusal, in the order above, or ``None`` when the oracle refuses nothing.
    """
    left = residual(text, known)
    if left is not None:
        return Refusal("residual", left[0], left[1])
    forms = decoded_forms(_blank(text, known.literals))
    for form in forms:
        for place in hosts(form):
            if is_public_host(place.host) and is_tenant_path(
                place.host, _path_after(form, place.end)
            ):
                return Refusal("tenant path")
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
    if any(not is_literal_mailbox(u) for form in forms for u in userinfos(form)):
        return Refusal("url userinfo")
    return None
