"""The closure oracle of the forge recorder: a short module that refuses.

SP-RECORDER-REDACTS-BY-CLASS 0.17 (D3, D8). The recorder rewrites a
recording by class, but the guarantee never rests on that rewrite
recognising every shape a private value can take. This module judges the
built fixture with explicit sets and one short reader each: it refuses
when a known private value survives, when a token is host-shaped and is
neither a placeholder nor public, when an address is public and when a
mailbox is neither a placeholder nor a documentation literal.

It imports the standard library, its own tables
(:mod:`repoach.forge.closure_tables`) and the neutral reader of a text's
forms (:mod:`repoach.core.text_forms`, which imports the standard library
only) and nothing else, and shares no pattern, function or set with
``repoach.forge.recording``; the recorder hands it the known values as data
(:class:`Known`).
"""

from __future__ import annotations

import functools
import ipaddress
import re
from dataclasses import dataclass

from repoach.core import text_forms

from .closure_tables import (
    ACCOUNT_NAMESPACES,
    HOST_TLDS,
    IPV4_RESERVED,
    IPV6_DOCUMENTATION,
    PUBLIC_HOSTS,
    PUBLIC_SUFFIXES,
    REVERSE_DNS_HEADS,
    RFC5737_NETWORKS,
    TENANT_ROUTE_PUBLIC,
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

_AUTHORITY_END = frozenset('/<>"')
"""What ends a URL authority read for its userinfo, with any whitespace: ``/`` and the prose delimiters.

The userinfo is read by grammar, not by an allowlist of characters: everything between
``<scheme>://`` and the last ``@`` before the authority ends is the userinfo, whatever it holds
(``^ | # ? [ ] \\`` and the rest), so a character outside RFC 3986 fails closed. Only a ``<``, a
``>``, a double quote or a space, which no recorded URL carries unescaped, ends it, so
``<https://<host>><a@<host>>`` is prose.
"""


KINDS: tuple[str, ...] = ("login", "host", "segment", "id", "name", "mailbox")
"""The kinds of known value the recorder replaces, in the order they are judged."""

JUDGED_KINDS: tuple[str, ...] = (*KINDS, "deny-list")
"""Every kind :func:`residual` judges: the replaced ones, then the deny-list's, which is never replaced."""

_ALNUM = "A-Za-z0-9"


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
    """Whether ``path`` on the public ``host`` opens a tenant surface or names an account.

    Read by the one grammar of :func:`has_tenant_path` on ``<host>/<path>``, in every decoded
    form of the path, so the redactor rewrites exactly the URLs the oracle refuses.

    Args:
        host: A public host, in any case.
        path: The URL path after the host, its leading ``/`` included or not.

    Returns:
        ``True`` when a route of :data:`TENANT_ROUTES` with its identity segment, or an
        :data:`ACCOUNT_NAMESPACES` namespace, stands after ``host`` (``stackoverflow.com/c/<team>``,
        ``ghcr.io/<owner>/…``, ``pkg.go.dev/github.com/<owner>/…``).
    """
    folded = _canonical_host(host)
    return any(
        has_tenant_path(f"{folded}/{form.lstrip('/')}")
        for form in text_forms.decoded_forms(path or "")
    )


_SEGMENT_END = "/\\s?#\"'<>"
"""What ends a path segment read lexically: ``/``, whitespace, a query or fragment mark and the prose delimiters."""


def _route_pattern(suffix: str, route: str) -> str:
    """The pattern of one :data:`TENANT_ROUTES` route after its host: its segments, then its identity.

    ``*`` is any one segment. The identity is the segment after the route, unless it is one of
    the route's public words (:data:`TENANT_ROUTE_PUBLIC`); a route ending in ``*:`` reads it in
    the same segment instead, ``<namespace>:<title>``, a namespace of letters and digits (``_``
    inside) whose title is not led by ``_`` (``Mission:_Impossible`` is an article), unless the
    namespace, or its ``_talk`` namespace, is public.
    """
    parts = route.split("/")
    public = sorted(TENANT_ROUTE_PUBLIC.get(f"{suffix} {route}", frozenset()))
    named = parts[-1] == "*:"
    head = parts[:-1] if named else parts
    lead = "/+".join(rf"[^{_SEGMENT_END}]+" if p == "*" else re.escape(p) for p in head)
    if named:
        exempt = "".join(rf"(?!{re.escape(word)}(?:_talk)?:)" for word in public)
        return rf"{lead}/+{exempt}[^\W_]\w*:[^_{_SEGMENT_END}]"
    exempt = "".join(rf"(?!{re.escape(word)}[.,;:!)\]]*(?:[{_SEGMENT_END}]|$))" for word in public)
    return rf"{lead}/+{exempt}[^{_SEGMENT_END}]"


def _tenant_route_re() -> re.Pattern[str]:
    """One pattern for every tenant route and account namespace, read lexically after its host.

    The host is the suffix or any name under it, not preceded by a letter, digit or ``-``
    (``xstackoverflow.com`` is another host); an optional trailing dot and ``:<port>`` follow,
    then ``/`` (one or more) and, for :data:`TENANT_ROUTES`, the route and its identity
    (:func:`_route_pattern`), or, for :data:`ACCOUNT_NAMESPACES`, an optional ``v2/`` and a
    namespace segment led by its lead that is not one no account holds
    (``docker.io/library/…`` is public; a namespace followed by ``:``, as in
    ``ghcr.io/<owner>:tag``, is still read). Nothing before the host is required, so a scheme,
    ``//``, a bare host and a Markdown link are one case. The suffix is matched first and the
    labels before it are never read, so the scan stays linear.
    """
    alternatives = []
    host = r"\.?(?::[0-9]*)?/+"
    for suffix, routes in sorted(TENANT_ROUTES.items()):
        paths = "|".join(_route_pattern(suffix, route) for route in sorted(routes))
        alternatives.append(rf"{re.escape(suffix)}{host}(?:{paths})")
    for suffix, (lead, public) in sorted(ACCOUNT_NAMESPACES.items()):
        ends = rf"(?:[{_SEGMENT_END}:@]|$)"
        exempt = "".join(rf"(?!{re.escape(word)}{ends})" for word in sorted(public))
        segment = rf"{exempt}{re.escape(lead)}[^{_SEGMENT_END}]"
        alternatives.append(rf"{re.escape(suffix)}{host}(?:v2/+{segment}|(?!v2{ends}){segment})")
    return re.compile(rf"(?<![A-Za-z0-9-])(?:{'|'.join(alternatives)})", re.IGNORECASE)


_TENANT_ROUTE_RE = _tenant_route_re()


def has_tenant_path(text: str) -> bool:
    """Whether ``text`` holds a tenant route after its public host, whatever leads the host.

    A Stack Overflow for Teams route (``stackoverflow.com/c/<team>``) is judged by host and path
    only: after ``https://``, after ``//``, bare, after a port (``stackoverflow.com:443/c/<team>``)
    or inside a Markdown link, under ``www.`` or any other label of the suffix.

    Args:
        text: The text read, one decoded form of it.

    Returns:
        ``True`` when a tenant route of :data:`TENANT_ROUTES` stands after its host.
    """
    return _TENANT_ROUTE_RE.search(text) is not None


def tenant_identities(text: str) -> list[str]:
    """The identity segment of every tenant route or account namespace in ``text``, in order.

    The identity is the path segment holding the last character the route grammar reads
    (:func:`has_tenant_path`): the segment after a route, a ``<namespace>:<title>`` segment, or
    a namespace. The redactor checks that none survives its rewrite of a tenant URL.

    Args:
        text: The text read, one decoded form of it.

    Returns:
        Each identity segment as it stands in ``text``.
    """
    found = []
    for m in _TENANT_ROUTE_RE.finditer(text):
        last = m.end() - 1
        begin = text.rfind("/", 0, last) + 1
        end = last
        while end < len(text) and text[end] not in "/?#\"'<>" and not text[end].isspace():
            end += 1
        found.append(text[begin:end])
    return found


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
    """The labels of a dotted host name read by DNS label grammar, else ``None``.

    Every label is letters, digits and hyphens (RFC 1123 2.1), neither led nor ended by a hyphen.
    The last label is an IDNA ``xn--`` label or two characters or more led by a letter, so a
    private name whose last label holds a digit is a host (``gitlab.dc1``, ``ci.node1``, PR #319
    round 7, 4687852986), while an all-numeric last label is not (RFC 3696 2: ``v1.2.3``,
    ``1.2.3.4``, read as an address elsewhere). A run whose labels before the last are all
    digits is a version with a tag (``7.1.0.rc1``), never a host.
    """
    body = run[:-1] if run.endswith(".") else run
    labels = body.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    last = labels[-1]
    if _PUNYCODE_LABEL.fullmatch(last):
        return labels
    if len(last) < 2 or not (last[0].isascii() and last[0].isalpha()):
        return None
    if all(label.isdigit() for label in labels[:-1]):
        return None
    return labels


def _dotless_authority(text: str, start: int, host: str) -> bool:
    """Whether ``host`` at ``start`` is a one-label host: right after ``<scheme>://``, the whole authority.

    ``https://gitlab/ns/repo`` names a machine; ``ssh://git@<host>`` does not name ``git``, so an
    authority holding a userinfo (:func:`_url_userinfo_spans`, the one reader of it) is never
    read here, whatever the userinfo holds (``postgres://app:p#x@<host>``).
    """
    if "." in host or not _LABEL.fullmatch(host):
        return False
    if not (text.endswith("://", 0, start) and _scheme_before(text, start - 3)):
        return False
    return start not in _userinfo_begins(text)


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

    A userinfo is read by URL grammar (:func:`_url_userinfo_spans`), so ``https://u:abc=@<host>``
    and ``postgres://u:p^w@<host>`` lead their host whatever the password holds.
    """
    if text.endswith("://", 0, start) and _scheme_before(text, start - 3):
        return True
    if not text.endswith("@", 0, start):
        return False
    return _local_before(text, start - 1) != "" or _userinfo_before(text, start - 1)


@functools.lru_cache(maxsize=64)
def _url_userinfo_spans(text: str) -> dict[int, int]:
    """The offset of the ``@`` that ends each URL userinfo of ``text``, mapped to where it begins.

    Read by URL grammar: after each ``<scheme>://`` the authority runs to the first whitespace or
    :data:`_AUTHORITY_END` character, and its LAST ``@`` ends the userinfo, whatever characters
    stand before it (an unescaped ``@`` in a password included). The authorities of a text are
    disjoint, so each character is read once and the reader stays linear.

    Args:
        text: The text read.

    Returns:
        ``{offset of the "@": offset right after "<scheme>://"}``, one entry per URL with a userinfo.
    """
    spans: dict[int, int] = {}
    colon = text.find("://")
    while colon != -1:
        begin = end = colon + 3
        if _scheme_before(text, colon):
            last = -1
            while end < len(text) and not text[end].isspace() and text[end] not in _AUTHORITY_END:
                if text[end] == "@":
                    last = end
                end += 1
            if last != -1:
                spans[last] = begin
        colon = text.find("://", end)
    return spans


@functools.lru_cache(maxsize=64)
def _userinfo_begins(text: str) -> frozenset[int]:
    """Every offset right after a ``<scheme>://`` whose authority holds a userinfo (:func:`_url_userinfo_spans`)."""
    return frozenset(_url_userinfo_spans(text).values())


def _userinfo_start(text: str, at: int) -> int | None:
    """Where the URL userinfo the ``@`` at ``at`` ends begins (:func:`_url_userinfo_spans`).

    Args:
        text: The text read.
        at: The offset of an ``@`` of ``text``.

    Returns:
        The offset right after ``<scheme>://``, or ``None`` when no URL userinfo ends at ``at``.
    """
    return _url_userinfo_spans(text).get(at)


def _userinfo_before(text: str, at: int) -> bool:
    """Whether the ``@`` at ``at`` ends a URL's userinfo (:func:`_userinfo_start`)."""
    return _userinfo_start(text, at) is not None


def userinfos(text: str) -> list[Mailbox]:
    """Every URL userinfo of ``text`` with the host after it, in order, as a :class:`Mailbox`.

    The userinfo is read by URL grammar (:func:`_url_userinfo_spans`): everything between
    ``<scheme>://`` and the last ``@`` of the authority, whatever characters it holds and
    whatever the host is: ``https://u:abc=@pypi.org/`` holds ``u:abc=``, which no mailbox
    reader sees (``=`` ends no local part) on a public host, ``postgres://app:pw@localhost``
    holds ``app:pw`` on a one-label host, which is no mailbox host, and
    ``postgres://app:p^w@localhost`` holds ``app:p^w``. The host is the label run or the bracketed
    literal right after the ``@``, ``""`` when none stands there.

    Args:
        text: The text read.

    Returns:
        Each userinfo as ``Mailbox(local=<userinfo>, host=<host>)``, never decoded here
        (:func:`judge` reads every decoded form).
    """
    found = []
    for at, begin in sorted(_url_userinfo_spans(text).items()):
        userinfo = text[begin:at]
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
    """Every form ``text`` is judged in, fully casefolded: decoded layers and node-id account numbers.

    Args:
        text: One string of the built fixture.
        literals: Tool and project literals, blanked before decoding.

    Returns:
        :func:`repoach.core.text_forms.folded_forms` of ``text`` with its literals blanked.
    """
    return text_forms.folded_forms(_blank(text, literals))


def residual(text: str, known: Known) -> tuple[str, int] | None:
    """The first known value ``text`` still holds, as ``(kind, ordinal)``, or ``None``.

    Args:
        text: One string of the built fixture.
        known: The recording's known values.

    Returns:
        The kind and the 1-based ordinal within it of the first value found, in
        :data:`JUDGED_KINDS` order.
    """
    forms = forms_of(text, literals=known.literals)
    for kind in JUDGED_KINDS:
        for n, value in enumerate(known.values(kind), 1):
            pattern = text_forms.token_pattern(value)
            if any(pattern.search(form) for form in forms):
                return kind, n
    return None


def judge(text: str, known: Known) -> Refusal | None:
    r"""What the oracle refuses in ``text``: a known value, a tenant path, an unknown host, a public address, an unknown mailbox or a URL userinfo.

    A public host followed by a tenant route (:func:`has_tenant_path`) is refused as a
    ``tenant path`` whatever leads the host (a scheme, ``//``, nothing, a port, a Markdown link):
    the host is public, the customer's surface after it is not.

    A URL userinfo (:func:`userinfos`, read by URL grammar) is judged as the mailbox it spells,
    ``<userinfo>@<host>`` (:func:`is_literal_mailbox`): ``git`` on a placeholder-class host and a
    password-less user on a documentation host (``postgres://app@localhost``) name no one; a
    credential (a ``:``) and any userinfo on a public or an unknown host are refused, whatever
    characters they hold (``^``, ``|``, ``\``, ``[``, ``#``, ``?``, an ``@`` included).

    Args:
        text: One string of the built fixture.
        known: The recording's known values and literals.

    Returns:
        The first refusal, in the order above, or ``None`` when the oracle refuses nothing.
    """
    left = residual(text, known)
    if left is not None:
        return Refusal("residual", left[0], left[1])
    forms = text_forms.decoded_forms(_blank(text, known.literals))
    if any(has_tenant_path(form) for form in forms):
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
