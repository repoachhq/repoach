"""The forms a text is read in: its decoded layers, its Unicode normal forms and full casefolds, its IDNA A-labels.

SP-RECORDER-REDACTS-BY-CLASS (PR #319 round 8, 4688673733). Every reader that judges text for a
private value reads it here, the forge's closure oracle (:mod:`repoach.forge.closure`) and the
maintainer's deny-list (:mod:`repoach.forge.denylist`) alike, and the gate of
SP-NO-PERSONAL-DATA-GATE will too. The module imports the standard library only, so a lint
outside the forge package reads text the way the oracle does without depending on it.
"""

from __future__ import annotations

import base64
import html
import re
import unicodedata
from urllib.parse import unquote

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


def _escaped_char(match: re.Match[str]) -> str:
    code = match.group(1) or match.group(2)
    return chr(int(code, 16)) if code else "/"


def _unescape_once(text: str) -> str:
    return unquote(html.unescape(_STRING_ESCAPE.sub(_escaped_char, text)))


_LABEL_RUN_UNICODE = re.compile(r"(?:[^\W_]|[-.。．｡])+", re.I)
"""A run of Unicode letters, digits, ``-`` and dots: the grammar of a host name before IDNA (RFC 5890).

``。``, ``．`` and ``｡`` are the ideographic, fullwidth and halfwidth full stops,
which IDNA reads as label separators (RFC 3490 3.1).
"""


_IDNA_DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})


def _a_label(label: str) -> str:
    """``label`` as IDNA writes it on the wire: itself when ASCII, else ``xn--`` and its punycode, casefolded."""
    if label.isascii():
        return label
    folded = label.casefold()
    if folded.isascii():
        return folded
    return "xn--" + folded.encode("punycode").decode("ascii")


def ascii_labels(text: str) -> str:
    """``text`` with every Unicode label run written in IDNA A-labels, so the ASCII readers read its hosts.

    A run of Unicode letters, digits, ``-`` and dots (:data:`_LABEL_RUN_UNICODE`) that holds a
    non-ASCII character is split on its dots, each non-ASCII label becoming ``xn--<punycode>``:
    ``hans@müller.de`` is ``hans@xn--mller-kva.de`` and ``https://éclair.fr/x`` is
    ``https://xn--clair-9ra.fr/x``, a mailbox and a host like any other. ASCII text is returned
    as it is; ordinary prose keeps no host-shaped token it did not have, since a label run
    names a host only where :func:`repoach.forge.closure.hosts` and
    :func:`repoach.forge.closure.mailboxes` would read its ASCII twin.

    Args:
        text: The text read, one decoded form of it.

    Returns:
        ``text`` with its Unicode label runs in A-labels; never raises.
    """
    if text.isascii():
        return text

    def _run(match: re.Match[str]) -> str:
        run = match.group(0)
        if run.isascii():
            return run
        return ".".join(_a_label(label) for label in run.translate(_IDNA_DOTS).split("."))

    return _LABEL_RUN_UNICODE.sub(_run, text)


def _peeled(text: str) -> list[str]:
    """``text`` and each layer of string-escape, HTML-entity and percent encoding peeled off, up to four."""
    forms = [text]
    current = text
    for _ in range(_DECODE_ROUNDS):
        peeled = _unescape_once(current)
        if peeled == current:
            break
        forms.append(peeled)
        current = peeled
    return forms


def decoded_forms(text: str) -> list[str]:
    r"""Every form ``text`` is read in: each encoding layer peeled off, its Unicode normal forms, its A-labels.

    The layers are string-escape, HTML-entity and percent encoding, up to four. The
    string-escape layer is ``\uXXXX``, ``\xHH`` and ``\/``: the JSON and C-family escapes a
    pasted payload carries (PHP and Go encode ``/`` as ``\/``), so ``http:\/\/<host>\/<ns>`` is
    judged as its plain form. Then every form is read in NFC and in NFKC (a fullwidth
    ``ｈｔｔｐｓ：／／`` is ``https://``), each of those peeled again, and every form with its
    Unicode label runs in IDNA A-labels (:func:`ascii_labels`), so a host or a mailbox on an
    internationalised domain is read by the same grammar as an ASCII one. An ASCII text has
    no other form than its layers, in their order.
    """
    seeds = [text]
    for normal in ("NFC", "NFKC"):
        seed = unicodedata.normalize(normal, text)
        if seed not in seeds:
            seeds.append(seed)
    forms: list[str] = []
    for seed in seeds:
        for form in _peeled(seed):
            if form not in forms:
                forms.append(form)
    for form in list(forms):
        for normal in ("NFC", "NFKC"):
            extra = unicodedata.normalize(normal, form)
            if extra not in forms:
                forms.append(extra)
    for form in list(forms):
        extra = ascii_labels(form)
        if extra not in forms:
            forms.append(extra)
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


def folds(text: str) -> tuple[str, ...]:
    """The full casefolds of ``text`` (PR #319 round 7, 4687852996): NFC, then NFKC, each once.

    Each is normalised, casefolded and normalised again, since a casefold may leave a string
    unnormalised: ``Straße`` folds to ``strasse``, ``Müller`` typed decomposed (NFD) folds to the
    composed form, and a fullwidth ``Ｍüller`` folds, by NFKC, to the ASCII one. Both sides of a
    comparison are folded by this one function: the judged text (:func:`folded_forms`) and every
    value it is compared with (a deny-list entry, :mod:`repoach.forge.denylist`).

    Args:
        text: Any string.

    Returns:
        ``NFC(casefold(NFC(text)))``, then ``NFKC(casefold(NFKC(text)))`` when it differs.
    """
    canonical = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    compatible = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())
    return (canonical,) if compatible == canonical else (canonical, compatible)


def folded_forms(text: str) -> list[str]:
    """Every form ``text`` is judged in, fully casefolded: decoded layers and node-id account numbers.

    Args:
        text: One string to judge.

    Returns:
        Each decoded layer (:func:`decoded_forms`) casefolded, then its :func:`folds`, then each
        user node id's account number (:func:`node_id_numbers`), each once.
    """
    forms: list[str] = []
    for layer in decoded_forms(text):
        for form in (layer.casefold(), *folds(layer), *node_id_numbers(layer)):
            if form not in forms:
                forms.append(form)
    return forms
